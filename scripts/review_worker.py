#!/usr/bin/env python3
"""
review_worker.py — review-service 单 worker (v1.1 A2.2 · PRD §3)

轮询 claim 文件队列 (review_queue) → subprocess 跑 REVIEW 流水线 → 回写产物 → 回调。
单实例串行 (LLM 成本与并发控制); systemd 常驻 (templates/boss-review-worker.service.example)。

用法:
  python3 scripts/review_worker.py                 # 常驻轮询 (systemd 用)
  python3 scripts/review_worker.py --once          # 处理至多 1 个 job 后退出 (测试/联调)
  python3 scripts/review_worker.py --poll 10       # 轮询间隔秒 (默认 15)

回调 (callback): config/review_service.yaml `callback:` 字段 —
  log    : 仅打日志 (MVP 默认)
  feishu : 飞书卡片回推 (A3 实现 scripts/feishu_notify.py 后自动可用)
回调失败不影响 job 状态 (产物已落盘, 可人工补发)。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

VAULT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(VAULT_ROOT / "scripts"))

import review_access
import review_queue

PIPELINE = VAULT_ROOT / "scripts" / "run_pipeline_local.py"
# meeting-review (output_format=meeting_summary) 走专用定性总结流水线, 而非打分 run_pipeline (见 _build_cmd)
MEETING_SUMMARY_PIPELINE = VAULT_ROOT / "scripts" / "meeting_summary_pipeline.py"
JOB_TIMEOUT_SEC = 40 * 60   # REVIEW ~18min; 40min 硬超时 (留 LLM 抖动余量)
DEFAULT_POLL_SEC = 15


# ─── 回调注册表 (A3 加 feishu 实现行即可) ───────────────────────

def _callback_log(job: dict) -> None:
    status = job.get("status")
    print(f"[callback:log] job={job['job_id']} status={status} "
          f"submitter={job.get('submitter')} result={job.get('result') or job.get('error')}")


def _callback_feishu(job: dict) -> None:
    # A3 落地: scripts/feishu_notify.py (卡片 + redact 出站闸)。延迟 import 防 MVP 期硬依赖。
    import feishu_notify  # noqa: F401  (A3 交付)
    feishu_notify.notify_job(job)


CALLBACKS: dict[str, Callable[[dict], None]] = {
    "log": _callback_log,
    "feishu": _callback_feishu,
}


def _resolve_callback() -> Callable[[dict], None]:
    cfg = review_access.load_config() or {}
    name = str(cfg.get("callback", "log"))
    return CALLBACKS.get(name, _callback_log)


# ─── job 执行 ──────────────────────────────────────────────────

def _scene_panel_arg(scene_slug: str) -> Optional[str]:
    """返回 scene panel 的 vault 相对路径 (供 --panel 参数); 文件不存在返回 None。
    pipeline 的 _resolve_panel_path 支持 '.yaml' 结尾的路径形式 (PR4)。"""
    p = VAULT_ROOT / "scenes" / scene_slug / "panel.yaml"
    return f"scenes/{scene_slug}/panel.yaml" if p.exists() else None


def _scene_redact_enabled(scene_slug: str) -> Optional[bool]:
    """读 scene 的 redact_review 设置; scene 加载失败返回 None (由调用方 fallback 全局 cfg)。"""
    try:
        import scene_loader as sl
        scene = sl.load_scene(scene_slug)
        return getattr(scene.report, "redact_review", True) is not False
    except Exception:
        return None


def _redact_review_enabled(cfg: Optional[dict] = None) -> bool:
    """review 服务是否启用脱敏闸。缺省 True (fail-close)。仅显式 redact_review: false 关闭。

    单租户放开依据: 提交方=被评审文档拥有者, 文档本就要送 LLM 评审; 对内部收件人
    非机密。公网层 check_public_safe 不受影响。
    ⚠ 多公司推广前须改 recipient-aware (见 docs/v1.0/redact-gate-audit.md Finding 2)。
    """
    if cfg is None:
        cfg = review_access.load_config() or {}
    return cfg.get("redact_review", True) is not False


# 流水线调速旋钮: 与 BOSS_LLM_* 同样从 .env 热重载, 让运维不必重启 worker / 不必改 systemd,
# 在 .env 里加一行即可每单生效。用于把请求摊平、避开网关前置 WAF 的每分钟频控 (2026-06-30 op2)。
_TUNING_KEYS = (
    "LLM_MIN_INTERVAL_SEC",   # 调用间最小间隔秒数 (节流闸, 0=关)
    "PHASE_2_CONCURRENCY",    # Phase 2 调研 sub-agent 并发上限
    "PHASE_4_CONCURRENCY",    # Phase 4 评委并发上限
    "PHASE_4_MAX_ATTEMPTS",   # Phase 4 单评委超时/失败自动重试 (总尝试次数, 默认 2)
    "LLM_MAX_RETRIES",        # 退避重试次数
    "LLM_RETRY_BASE_SEC",     # 退避基数秒
    "LLM_RETRY_MAX_SEC",      # 退避上限秒
    "BOSS_LLM_TEMPERATURE",   # 采样温度 (评审可复现: 低温降分数漂移; 空=API 默认)
)


def _pipeline_env(job: Optional[dict] = None) -> dict[str, str]:
    """流水线子进程环境: 继承当前 + 用 .env 里**最新**的 BOSS_LLM_* 与调速旋钮覆盖。

    让"切模型"/"调速率"(改 .env, 经 llm_switch / 飞书 admin 命令) 在**下一单作业自动生效**,
    不必重启 worker。worker 启动时由 systemd EnvironmentFile 载入的是旧值, 这里每单热重载覆盖。
    .env 不存在 / 读失败 → 原样用当前 env (零行为变化)。

    另: 把 job_id / scene 经 env 透传给流水线, 让其 LLM 用量埋点能按 case/场景归集
    (管理台 M0 遥测)。埋点在流水线侧 fail-open, 缺这两个 env 只是 job_id/scene 记空。"""
    env = dict(os.environ)
    try:
        import llm_switch
        lines = llm_switch.read_env_lines(VAULT_ROOT / ".env")
        for k in (*llm_switch.MANAGED_KEYS, *_TUNING_KEYS):
            v = llm_switch.env_value(lines, k)
            if v is not None:
                env[k] = v
    except Exception:
        pass
    if job:
        if job.get("job_id"):
            env["BOSS_JOB_ID"] = str(job["job_id"])
        if job.get("scene_slug"):
            env["BOSS_SCENE_SLUG"] = str(job["scene_slug"])
            _apply_scene_llm(env, str(job["scene_slug"]))   # per-scene 模型覆盖 (缺省=全局)
    return env


def _scene_output_format(scene_slug: str) -> Optional[str]:
    """读 scene panel 的 output_format (在 panel.yaml, 不在 scene.yaml); 读失败返回 None。"""
    try:
        import scene_loader as sl
        import yaml
        scene = sl.load_scene(scene_slug)
        panel = yaml.safe_load(scene.panel_path.read_text(encoding="utf-8")) or {}
        return panel.get("output_format")
    except Exception:  # noqa: BLE001 — 读不到就当普通场景, 走 run_pipeline
        return None


def _apply_scene_llm(env: dict[str, str], scene_slug: str) -> None:
    """若 scene.yaml 配了 llm 覆盖, 用它替换 env 里的 BOSS_LLM_* (per-scene 模型)。

    fail-safe (SR): 场景无覆盖 / profile 缺失 / 缺 key / 解析失败 → **保持全局模型 + 打印告警,
    绝不中断作业**。缺省场景 (无 llm 块) bit 级等价于改前。"""
    try:
        import scene_loader as sl
        ov = getattr(sl.load_scene(scene_slug), "llm", None)
    except Exception:   # noqa: BLE001 — 场景加载失败不影响作业, 沿用全局
        return
    if not ov:
        return
    try:
        import llm_failover
        import llm_switch
        profiles = llm_switch.load_profiles()
        env_lines = llm_switch.read_env_lines(VAULT_ROOT / ".env")
        args = llm_failover.profile_call_args(ov.profile, profiles=profiles, env_lines=env_lines)
    except Exception as exc:   # noqa: BLE001 — profile 缺失/缺 key → 退回全局
        print(f"[worker] scene={scene_slug} llm 覆盖 profile={ov.profile!r} 不可用 ({exc}); 沿用全局模型",
              flush=True)
        return
    env["BOSS_LLM_PROVIDER"] = args.provider
    if args.base_url:
        env["BOSS_LLM_BASE_URL"] = args.base_url
    else:
        env.pop("BOSS_LLM_BASE_URL", None)     # 覆盖到无 base_url 的 provider (如 anthropic) 时清掉全局残留
    env["BOSS_LLM_MODEL_FAST"] = ov.model_fast or args.model_fast
    env["BOSS_LLM_MODEL_DEEP"] = ov.model_deep or args.model_deep
    env["BOSS_LLM_API_KEY"] = args.api_key or ""
    print(f"[worker] scene={scene_slug} per-scene 模型: profile={ov.profile} "
          f"deep={ov.model_deep or args.model_deep}", flush=True)


def _scene_has_llm_override(scene_slug: str) -> bool:
    """该 scene.yaml 是否配了 llm 覆盖块。决定 _build_cmd 是否让 env 里的 provider 生效。

    有覆盖 → _build_cmd 不硬传 --llm-provider, 交给 _pipeline_env→_apply_scene_llm 注入的
    BOSS_LLM_PROVIDER (否则显式 CLI 值压过 env, provider 与 model/base_url/key 错配)。
    读失败 / 无覆盖 → False (保持原行为: 传 review_service.yaml 的 llm_provider)。"""
    try:
        import scene_loader as sl
        return getattr(sl.load_scene(scene_slug), "llm", None) is not None
    except Exception:  # noqa: BLE001 — 读不到就当无覆盖, 走全局 provider
        return False


def _build_cmd(job: dict) -> list[str]:
    """组 REVIEW 流水线命令。有 job.scene_slug 时按 scene 选 panel 与脱敏配置。
    ★ output_format=meeting_summary 的场景 (meeting-review) 走专用定性总结流水线, 不走打分 run_pipeline。"""
    cfg = review_access.load_config() or {}
    defaults = cfg.get("defaults") or {}
    slug = job.get("scene_slug")

    # meeting_summary 分支 (仅此类场景生效; op2/workshop/默认全部不受影响)
    if slug and _scene_output_format(slug) == "meeting_summary":
        cmd = [
            sys.executable, str(MEETING_SUMMARY_PIPELINE),
            "--doc", str(job["doc"]),
            "--brand", str(job["brand_slug"]),
            "--scene", str(slug),
        ]
        # scope (攒批时的演讲人/材料列表) → deck 封面「资料范围」(feishu_events 攒批 finalize 时写入 job)
        if job.get("scope"):
            cmd += ["--scope", str(job["scope"])]
        return cmd

    # Panel 选择: scene panel 优先 (相对 vault root 路径); 否则用全局 defaults
    panel = (slug and _scene_panel_arg(slug)) or str(defaults.get("panel", "default"))

    cmd = [
        sys.executable, str(PIPELINE),
        "--review", str(job["doc"]),
        "--brand", str(job["brand_slug"]),
        "--panel", panel,
        # ★ 评审服务无人值守: 必须跳过 GATE 1 人工确认。否则非交互子进程 input() 抛
        # EOFError → GATE 默认拒绝 → Phase 1 后停、exit 0 → worker 误标 done 但无报告。
        "--auto-confirm",
    ]

    # ★ 崩溃恢复重试 (retries>0): 上次运行可能已写出 report.md 才崩溃 (worker 被杀 / VM 重启,
    # complete 未及回写 → recover_stale 退回 pending 重跑)。REVIEW 默认拒绝覆盖已存在 brand
    # (run_pipeline_local Phase 0 守卫) → 把本已成功的单在重试时永久判失败。故重试单显式传
    # --review-into 允许覆盖上次残留产物, 让重跑能正常收尾。
    if job.get("retries", 0) > 0:
        cmd.append("--review-into")

    # provider 决策 (P0 修 per-scene 覆盖冲突):
    #   · 无 scene llm 覆盖 → 传 review_service.yaml 的 llm_provider (原行为, bit 级等价)。
    #   · scene 配了 llm 覆盖 → **不传** --llm-provider, 让 _pipeline_env→_apply_scene_llm
    #     注入的 BOSS_LLM_PROVIDER 经 env 生效 (run_pipeline_local 的 --llm-provider 默认即读
    #     BOSS_LLM_PROVIDER)。否则显式 CLI 值会压过 env → 出现 provider=anthropic 但
    #     model/base_url/key 是别家网关 (如覆盖到 glm) 的错配 → 用 anthropic SDK 发非 anthropic
    #     模型名 → 401/模型不存在。models 本就走 env, 故这里只需让出 provider。
    if not (slug and _scene_has_llm_override(slug)):
        cmd += ["--llm-provider", str(defaults.get("llm_provider", "anthropic"))]

    # 脱敏决策: force_redact (群投递) 最高优先 (恒脱敏)。
    # 否则: 有 scene 时用 scene 级 redact_review; 无 scene 时用全局 cfg。
    if not job.get("force_redact"):
        redact = (
            _scene_redact_enabled(slug)
            if slug is not None
            else None
        )
        if redact is None:
            redact = _redact_review_enabled(cfg)
        if not redact:
            cmd.append("--no-redact")

    # C3: review_verify → REVIEW Phase 2 真 web 调研 (印证/挑战 doc claims, 深化报告)。
    # 需 VM .env 配 TAVILY_API_KEY / BRAVE_API_KEY; 无 key 时流水线优雅降级 = 旧 REVIEW。
    if defaults.get("review_verify"):
        cmd.append("--verify")
    return cmd


def _render_review_formats(brand_slug: str) -> None:
    """流水线成功后 best-effort 渲 report.html + report.pdf (md 之外多格式 · A4)。
    任何失败 (markdown 库缺 / Chrome 缺 / 渲染异常) 绝不影响 report.md 投递 — 吞掉打日志。
    pdf 需 VM 系统 chromium (sudo apt install chromium); 缺则只 md+html。"""
    try:
        import render_review_formats
        res = render_review_formats.render_all(VAULT_ROOT / "reports" / brand_slug / "report.md")
        if res["html"]:
            print(f"[worker] 多格式: html=✓ pdf={'✓' if res['pdf'] else '跳过(Chrome 缺?)'}")
        else:
            print("[worker] html 渲染跳过 (markdown 库缺? best-effort, 不影响 md)")
    except Exception as e:
        print(f"[worker] ⚠ 多格式渲染异常 (不影响 md 投递): {type(e).__name__}: {e}", file=sys.stderr)


def _render_seven_block(brand_slug: str, scene_slug: str, cadre_label: str = "") -> bool:
    """干部周报场景 (output_format=cadre_weekly_7block) 流水线成功后渲「七块」→ report-7block.md
    (M1.5 · PRD §6, 干部自看的简洁版: 一句话判断/保留能力/利润短板/下周补充/组织进化)。
    best-effort: 任何失败不影响全文 report.md 投递。返回是否成功。"""
    try:
        import cadre_weekly_output as cwo
        report_md = VAULT_ROOT / "reports" / brand_slug / "report.md"
        if not report_md.exists():
            return False
        data = cwo.load_from_report(report_md, scene=scene_slug, cadre_label=cadre_label or brand_slug)
        out = VAULT_ROOT / "reports" / brand_slug / "report-7block.md"
        out.write_text(cwo.render_cadre_weekly_7block(data), encoding="utf-8")
        # 七块也出 html + pdf (给干部可存档/好看的版本); best-effort, 缺 markdown/chromium 只降级
        html = pdf = None
        try:
            import render_review_formats
            res = render_review_formats.render_all(out)   # → report-7block.html / report-7block.pdf
            html, pdf = res.get("html"), res.get("pdf")
        except Exception as e:  # noqa: BLE001
            print(f"[worker] ⚠ 七块 html/pdf 渲染跳过: {type(e).__name__}: {e}", file=sys.stderr)
        print(f"[worker] 七块渲染: report-7block.md ✓ · html={'✓' if html else '—'} "
              f"pdf={'✓' if pdf else '—'} (干部周报场景)")
        return True
    except Exception as e:  # noqa: BLE001 — 七块失败不阻断全文投递
        print(f"[worker] ⚠ 七块渲染异常 (不影响全文投递): {type(e).__name__}: {e}", file=sys.stderr)
        return False


def _render_v8_report(brand_slug: str, job: Optional[dict] = None) -> bool:
    """学习小组场景 (output_format=study_weekly_v8) 流水线成功后渲 v8 六段式 → report-v8.md
    (study-weekly M1 · PRD §4.2)。区间校验不过即失败 (fail-close 不落脏数据);
    失败不影响全文 report.md 投递。返回是否成功。"""
    try:
        import study_weekly_output as swo
        review = VAULT_ROOT / "reports" / brand_slug / "reviews" / "v8-coach.md"
        if not review.exists():
            return False
        payload = swo.load_review_payload(review)
        # 身份与归属周 (M2): open_id → 花名册 → 文档标题含姓名; 周一提交归上一周 (补交窗口)
        try:
            import gen_study_weekly_summary as gsw
            from datetime import datetime as _dtn
            job = job or {}
            payload.member = gsw.resolve_member(
                gsw.load_roster(), open_id=str(job.get("submitter") or ""),
                doc_title=str(job.get("doc_name") or ""))
            payload.week = gsw.attribute_week(_dtn.now())
        except Exception:  # noqa: BLE001 — 识别失败不阻断个人报告 (member 留空, 汇总列未识别)
            pass
        swo.write_outputs(VAULT_ROOT / "reports" / brand_slug, payload)   # 校验不过抛 ValueError
        base, ded, total = swo.compute_total(payload)
        print(f"[worker] v8 渲染: report-v8.md ✓ 总分 {base:.0f}-{ded:.0f}={total:.0f} "
              f"({swo.grade_for_total(total)})")
        return True
    except Exception as e:  # noqa: BLE001 — v8 渲染失败不阻断全文投递
        print(f"[worker] ⚠ v8 渲染异常 (不影响全文投递): {type(e).__name__}: {e}", file=sys.stderr)
        return False


def _collect_artifacts(brand_slug: str) -> dict[str, str]:
    """流水线成功后收产物路径 (存在的才记)。"""
    base = VAULT_ROOT / "reports" / brand_slug
    out: dict[str, str] = {}
    for key, rel in (("report", "report.md"), ("html", "report.html"), ("pdf", "report.pdf"),
                     ("brief", "report-brief.md"),
                     ("seven_block", "report-7block.md"),        # 干部周报七块 (M1.5, 干部自看简洁版)
                     ("v8_report", "report-v8.md"),              # 学习小组 v8 六段式 (study-weekly M1)
                     ("pptx", "meeting-summary.pptx"),           # meeting-review 大屏 deck (best-effort)
                     ("revision_suggestions", "revision-suggestions.md")):
        p = base / rel
        if p.exists():
            out[key] = str(p.relative_to(VAULT_ROOT))
    return out


def process_one(queue_root: Path = review_queue.DEFAULT_QUEUE_ROOT,
                runner: Optional[Callable[..., Any]] = None) -> Optional[dict]:
    """claim 一个 job 并执行完。无任务返回 None; 否则返回终态 job。
    runner 可注入 (测试 mock subprocess.run)。"""
    job = review_queue.claim_next(queue_root)
    if job is None:
        return None
    job_id = job["job_id"]
    run = runner or subprocess.run
    print(f"[worker] 开始 job={job_id} brand={job.get('brand_slug')} doc={job.get('doc')}")
    try:
        proc = run(_build_cmd(job), capture_output=True, text=True,
                   timeout=JOB_TIMEOUT_SEC, cwd=str(VAULT_ROOT), env=_pipeline_env(job))
    except subprocess.TimeoutExpired:
        final = review_queue.fail(job_id, f"流水线超时 (> {JOB_TIMEOUT_SEC // 60}min)", queue_root)
    except Exception as e:
        final = review_queue.fail(job_id, f"worker 异常: {type(e).__name__}: {e}", queue_root)
    else:
        slug = job.get("scene_slug")
        seven_ok = False
        v8_ok = False
        if proc.returncode == 0:
            # study_weekly_v8 交付物是 report-v8.md, 全文 html/pdf 后面会被 pop 掉 (不混投),
            # 故不必跑 chromium 渲染全文 html/pdf — 轻量化省一步 (每份省 chromium 渲染开销)。
            if slug and _scene_output_format(slug) == "study_weekly_v8":
                v8_ok = _render_v8_report(job["brand_slug"], job)
            else:
                _render_review_formats(job["brand_slug"])   # A4: report.md → +html (collect 前, best-effort)
                if slug and _scene_output_format(slug) == "cadre_weekly_7block":
                    seven_ok = _render_seven_block(job["brand_slug"], slug, job.get("doc_name", ""))
        artifacts = _collect_artifacts(job["brand_slug"])
        if proc.returncode == 0 and "report" in artifacts:
            if seven_ok and "seven_block" in artifacts:
                # A 档 (用户选): 干部收「七块」(md + 好看的 html/pdf), 不投评审全文;
                # 全文 report.md 留痕 report_full 供审计。
                artifacts["report_full"] = artifacts["report"]        # 审计留痕: 全文 md 仍在产物记录
                artifacts["report"] = artifacts["seven_block"]        # 附件 = 七块 md
                artifacts["brief"] = artifacts["seven_block"]         # 卡片正文 = 七块 (截断展示)
                # html/pdf 换成七块自己的 (若渲成功); 否则去掉全文的 (不混投)
                for k, rel in (("html", "report-7block.html"), ("pdf", "report-7block.pdf")):
                    p = f"reports/{job['brand_slug']}/{rel}"
                    if (VAULT_ROOT / p).exists():
                        artifacts[k] = p
                    else:
                        artifacts.pop(k, None)
            elif v8_ok and "v8_report" in artifacts:
                # 学习小组: 成员收 v8 六段式诊断 (report-v8.md), 全文留痕供审计。
                artifacts["report_full"] = artifacts["report"]
                artifacts["report"] = artifacts["v8_report"]
                artifacts["brief"] = artifacts["v8_report"]           # 卡片正文 = 六段式
                artifacts.pop("html", None)                            # 不混投全文的 html/pdf
                artifacts.pop("pdf", None)
            artifacts["wiki_freshness"] = _wiki_freshness()
            final = review_queue.complete(job_id, artifacts, queue_root)
        elif proc.returncode == 0:
            # exit 0 但没产出 report.md: 流水线在中途优雅停止 (如 GATE 1 拒绝 / Phase 1 后退出)。
            # 不能误标 done 发空卡片 — 标 failed 让用户收到失败提示可重试。
            tail = (proc.stdout or proc.stderr or "")[-1200:]
            final = review_queue.fail(
                job_id, f"流水线 exit 0 但未产出 report.md (疑似中途停止/GATE 未跳过): {tail}", queue_root)
        else:
            tail = (proc.stderr or proc.stdout or "")[-1500:]
            final = review_queue.fail(job_id, f"流水线 exit {proc.returncode}: {tail}", queue_root)

    try:
        _resolve_callback()(final)
    except Exception as e:
        # 回调失败不影响 job 终态 — 产物已落盘, 可人工补发 (PRD §7 风险表)
        print(f"[worker] ⚠ 回调失败 (job 状态不受影响): {type(e).__name__}: {e}", file=sys.stderr)
    return final


def _wiki_freshness() -> str:
    """背景知识时间戳 (S0.5) — 进 result, A3 渲卡片字段。"""
    try:
        import data_freshness
        return data_freshness.freshness_label()
    except Exception:
        return "背景知识: 状态未知"


def main() -> int:
    ap = argparse.ArgumentParser(description="review-service 单 worker (A2.2)")
    ap.add_argument("--once", action="store_true", help="处理至多 1 个 job 后退出")
    ap.add_argument("--poll", type=int, default=DEFAULT_POLL_SEC, help="轮询间隔秒")
    args = ap.parse_args()

    # 单 worker: 启动那一刻不可能有 job 在真跑 → running/ 里的全是上次崩溃/重启遗留的孤儿,
    # 无条件 (stale_minutes=0) 退回 pending 重跑。否则频繁部署重启会把中断的单卡在 running 里,
    # 要等 90 分钟才回收 (2026-07-04: 多次重启部署留下 tsg 等孤儿单)。
    recovered = review_queue.recover_stale(stale_minutes=0)
    if recovered:
        print(f"[worker] 启动恢复: {len(recovered)} 个遗留 running 已退回 pending 重跑 {recovered}")

    if args.once:
        result = process_one()
        print(f"[worker] --once: {'处理完成 ' + result['job_id'] if result else '队列为空'}")
        return 0

    print(f"[worker] 常驻轮询启动 (间隔 {args.poll}s, 队列 {review_queue.DEFAULT_QUEUE_ROOT})")
    while True:
        if process_one() is None:
            time.sleep(args.poll)
    # noreturn


if __name__ == "__main__":
    sys.exit(main())
