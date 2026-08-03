#!/usr/bin/env python3
"""
feishu_notify.py — 评审完成/失败回推 (v1.1 A3.3 · review_worker callback="feishu")

done: 卡片 (brief 摘要 + wiki 新鲜度 + 边界声明) + report.md 文件附件
failed: 失败卡片 (礼貌 + 重试建议)

出站闸 (§9.2 fail-close): 卡片全文先过 redact_check.check_text — 命中则**不发原内容**,
改发"已生成但被出站闸拦截"提示 (报告留在 VM, 走内网/人工脱敏后给) + 记 blocked log。
文件附件同闸 (report.md 全文 + 文件名扫描 — 2026-06-14 事件: 内容脱敏但文件名含敏感词仍外泄)。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

VAULT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(VAULT_ROOT / "scripts"))

import feishu_client
import redact_check
import review_access
from feishu_events import _boundary_note, _card

BRIEF_MAX_CHARS = 1200
DELIVERY_NAME_MAX = 80   # 派生文件名去扩展名部分的字符上限 (飞书文件名不宜过长)


def _sanitize_filename(s: str) -> str:
    """把提交文档标题清成可当文件名的串: 去非法字符 / 换行 / 折叠空白 / 限长。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:DELIVERY_NAME_MAX].strip()


def _delivery_basename(job: dict) -> str:
    """派生投递文件名主体 (不含扩展名): 优先提交文档原标题, 回退 brand_slug。

    痛点: 每组报告磁盘名都是 report.pdf, 飞书里分不清哪组。改按提交文档标题命名。"""
    raw = job.get("doc_name") or job.get("brand_slug") or "评审报告"
    stem = Path(str(raw)).stem          # 去掉 .pdf/.docx 等原扩展名
    return _sanitize_filename(stem) or "评审报告"


def _env_or_dotenv(key: str) -> str:
    """取环境变量; 进程 env 缺失时**回落读一次 .env**。

    根因 (2026-07-21 study-weekly 踩坑): worker 是常驻进程, 其 os.environ 由 systemd
    EnvironmentFile 在**启动那一刻**载入。加**新 bot** 凭据到 .env 但没重启 worker 时,
    worker 的 os.environ 里没有 → 回推回退默认 bot → 飞书 400 → 报告发不出去。
    回落读 .env 让回推**不必重启 worker** 也能拿到新 bot 凭据 (与 _pipeline_env 热读 .env 同思路)。
    仅在 os.environ 缺失时才读 .env → 正常路径零开销; 读失败/文件不存在 → 返回空串 (行为同旧路径)。"""
    v = os.environ.get(key, "")
    if v:
        return v
    try:
        import llm_switch
        lines = llm_switch.read_env_lines(VAULT_ROOT / ".env")
        return llm_switch.env_value(lines, key) or ""
    except Exception:  # noqa: BLE001 — .env 读失败不阻断回推, 回退默认 bot (原行为)
        return ""


def _apply_scene_bot_creds(job: dict) -> None:
    """多 bot: 按 job.scene_slug 反查该 scene 的飞书 app 凭证, 设为本进程发送凭证。

    worker 是独立进程, 不像 feishu_ws 回调那样 set_bot_creds 过; 不设则 feishu_client
    回退默认 LARK_APP_ID bot → 报告从"错误的 bot"发出 (用户跟 A 说话却收到 B 的回复)。
    这里按 scene 把回推凭证对齐到用户对话的同一个 bot。凭据取 os.environ, 缺失回落 .env
    (见 _env_or_dotenv: 加新 bot 后不必重启 worker 即可回推)。

    无 scene_slug / 解析失败 / env+.env 均未配 → 不设, 回退默认单 bot (向后兼容)。
    """
    slug = job.get("scene_slug")
    if not slug:
        return
    try:
        import scene_loader as sl
        scene = sl.load_scene(slug)
        fe = getattr(scene, "feishu", None)
        if not fe or not getattr(fe, "configured", False):
            return
        app_id = _env_or_dotenv(fe.app_id_env)
        secret = _env_or_dotenv(fe.app_secret_env)
        if app_id and secret:
            feishu_client.set_bot_creds(app_id, secret)
        else:
            # os.environ 与 .env 里都没有该 scene 的 bot 凭据 → 回退默认 bot 发卡, 而默认 bot
            # 发不到属于该 scene app 的 open_id → 飞书 400。此时是真缺凭据 (非 worker 陈旧),
            # 需在 .env 补齐该 bot 的 app_id/secret。
            print(f"[feishu_notify] ⚠ scene={slug} 的 bot 凭据 env 与 .env 均缺失 "
                  f"({fe.app_id_env}/{fe.app_secret_env}) — 回推将回退默认 bot, "
                  f"发往该 scene app 的 open_id 会 400。请在 .env 补齐该 bot 凭据。",
                  file=sys.stderr, flush=True)
    except Exception as e:  # noqa: BLE001 — 凭证解析失败不应阻断回推, 回退默认 bot
        print(f"[feishu_notify] ⚠ scene bot 凭证解析失败 (scene={slug}): "
              f"{type(e).__name__}: {e}; 回退默认 bot", file=sys.stderr, flush=True)


def _redact_review_enabled(job: Optional[dict] = None) -> bool:
    """review 服务回推是否过脱敏闸。缺省 True (fail-close); 仅 redact_review: false 关闭。
    与 review_worker._redact_review_enabled 同义 (单租户灰度放开, 见 redact-gate-audit.md)。

    群投递例外 (A4): job.force_redact=True 时无条件过闸 — 群=非单一收件人, 不享受
    单聊 redact_review:false 的放开 (recipient-aware, redact-gate-audit.md Finding 2)。"""
    if job and job.get("force_redact"):
        return True
    cfg = review_access.load_config() or {}
    return cfg.get("redact_review", True) is not False


def _build_done_card(job: dict) -> tuple[dict, str]:
    """返回 (card, 卡片纯文本) — 纯文本供 redact 扫描。"""
    result = job.get("result") or {}
    brief_text = ""
    brief_rel = result.get("brief")
    if brief_rel:
        p = VAULT_ROOT / brief_rel
        if p.exists():
            brief_text = p.read_text(encoding="utf-8", errors="replace")[:BRIEF_MAX_CHARS]
    lines = [
        f"文档: **{job.get('doc_name', job.get('brand_slug', ''))}**",
        f"任务号: `{job['job_id']}`",
        # 背景知识新鲜度字段不在评审完成卡片展示 (内含 data_sync cron 运维提示, 非用户关切)。
        # 仍保留在 result.wiki_freshness (审计 / JSON) 与 CLI `data_freshness.py` 巡检。
        "—" * 12,
        brief_text or "(摘要文件缺失 — 完整报告见附件)",
        "—" * 12,
        # 战略 OS M1: 优先建议区块 + 决策命令提示 (fail-open; 行进 lines → plain,
        # 与摘要走同一出站 redact 闸, 不额外开口子)
        *_loop_suggestion_lines(job),
        _boundary_note(job.get("scene_slug")),   # 按场景: study-weekly 用自省诊断话术, 非通用"评委 panel"
    ]
    card = _card("✅ 评审完成", [l for l in lines if l])
    plain = "\n".join(str(l) for l in lines)
    return card, plain


def _loop_suggestion_lines(job: dict) -> list[str]:
    """评审完成卡片的「优先建议 + 决策命令」区块 (战略 OS M1)。fail-open → []。"""
    try:
        from boss_core import loop as _loop
        brand = job.get("brand_slug") or ""
        if not brand:
            return []
        payload = _loop.read_suggestions(VAULT_ROOT / "reports" / brand)
        out = _loop.suggestion_card_lines(payload)
        return [*out, "—" * 12] if out else []
    except Exception:  # noqa: BLE001
        return []


def _build_failed_card(job: dict) -> dict:
    err = job.get("error", "") or ""
    # 文档读不出文字 (扫描件 / 纯图片 PDF): 泛泛的"请重发"会让用户反复重发同一份扫描件。
    # 给针对性提示 — 必须换文字版, 重发同一份无用 (run_pipeline_local: REVIEW doc 内容过短)。
    if "内容过短" in err or "无可提取文字" in err:
        return _card("📄 文档无法评审 (提取不到文字)", [
            f"任务号: `{job['job_id']}`",
            f"文档 **{job.get('doc_name', '')}** 提取不到文字 — 多半是扫描件 / 纯图片 PDF。",
            "请改发**带文字层**的版本:能选中文字的 PDF,或 Word / Markdown。",
            "⚠️ 重发同一份扫描件仍会失败 (暂不支持 OCR)。",
        ], template="orange")
    return _card("❌ 评审未完成", [
        f"任务号: `{job['job_id']}`",
        "本次评审执行失败, 已记录排障。请稍后重发文档再试一次;",
        "若连续失败请联系项目主理。",
    ], template="red")


def _redact_blocked_card(job: dict, n_hits: int) -> dict:
    return _card("⚠️ 评审完成 (内容被出站闸拦截)", [
        f"任务号: `{job['job_id']}` — 报告已生成, 但含 {n_hits} 处敏感内容,",
        "按 §9.2 出站闸策略不经飞书外发。请联系项目主理走内网获取,",
        "或等待人工脱敏版本。",
        _boundary_note(job.get("scene_slug")),
    ], template="orange")


def notify_job(job: dict) -> None:
    """review_worker 回调入口。回调失败由 worker 捕获 (不影响 job 终态)。"""
    if job.get("no_notify"):
        # 回填/试评: 只落库产报告, 不逐条推给成员 (避免历史周报评估挨个弹到各成员单聊)。
        print(f"[feishu_notify] job={job.get('job_id')} no_notify — 跳过回推 (仅落库)")
        return
    to = job.get("notify_to") or job.get("submitter")
    if not to:
        print(f"[feishu_notify] job={job.get('job_id')} 无 notify_to/submitter, 跳过")
        return
    id_type = job.get("notify_id_type", "open_id")   # 群 job = chat_id, 单聊 = open_id

    # 多 bot: 回推前对齐到 job.scene_slug 对应的 bot (否则 fallback 默认 bot → 串台)
    _apply_scene_bot_creds(job)

    if job.get("status") != "done":
        card = _build_failed_card(job)
        # 失败卡的 "内容过短" 分支含 doc_name (可能是客户名) — 早先失败路径完全绕过出站闸。
        # 群投递/force_redact 时把 doc_name 过一遍脱敏闸, 命中则退到不含 doc_name 的通用失败卡。
        if _redact_review_enabled(job):
            blocked, hits = redact_check.check_text(
                str(job.get("doc_name", "")), path=f"feishu-failcard:{job['job_id']}")
            if blocked:
                redact_check.log_blocked(hits)
                card = _card("❌ 评审未完成", [
                    f"任务号: `{job['job_id']}`",
                    "文档无法评审 (提取不到文字, 且文件名含敏感信息不便在群内展示)。",
                    "请改发**带文字层**的版本 (可选中文字的 PDF / Word / Markdown)。",
                    "⚠️ 重发同一份扫描件仍会失败 (暂不支持 OCR)。",
                ], template="orange")
        feishu_client.send_card(to, card, id_type)
        return

    redact_on = _redact_review_enabled(job)   # 群 job (force_redact) 无条件过闸
    card, plain = _build_done_card(job)
    if redact_on:
        blocked, hits = redact_check.check_text(plain, path=f"feishu-card:{job['job_id']}")
        if blocked:
            redact_check.log_blocked(hits)
            feishu_client.send_card(to, _redact_blocked_card(job, len(hits)), id_type)
            return
    feishu_client.send_card(to, card, id_type)

    # 完整报告附件 — md + html 多格式 (A4)。md/html 同源, **内容闸只查一次** (report.md);
    # 命中 → 整体不外发 (发拦截卡)。各格式各查**文件名**闸 (2026-06-14 事件: 内容脱敏但文件名
    # 含敏感词仍经 URL / 搜索引擎外泄), 文件名命中只跳过该文件, 不拦其余。
    result = job.get("result") or {}
    report_rel = result.get("report")
    rp = (VAULT_ROOT / report_rel) if report_rel else None
    if rp and rp.exists():
        if redact_on:
            _, c_hits = redact_check.check_text(
                rp.read_text(encoding="utf-8", errors="replace"),
                path=f"feishu-file:{job['job_id']}")
            if c_hits:
                redact_check.log_blocked(c_hits)
                feishu_client.send_card(to, _redact_blocked_card(job, len(c_hits)), id_type)
                return
        base = _delivery_basename(job)           # 按提交文档标题命名, 区分各组
        for key in ("report", "html", "pdf"):   # report.md + report.html + report.pdf (同源)
            rel = result.get(key)
            if not rel:
                continue
            fp = VAULT_ROOT / rel
            if not fp.exists():
                continue
            # 展示名 = <文档标题>·AI评审报告<原扩展名> (磁盘名仍是通用 report.*)
            display = f"{base}·AI评审报告{fp.suffix}"
            if redact_on:
                # 查**展示名**的文件名闸 (标题可能含敏感词, 2026-06-14 事件教训);
                # 命中 → 退回通用磁盘名 report.* (仍投递, 只是不裸露标题), 而非丢掉整份报告。
                _, fn_hits = redact_check.check_filename(display)
                if fn_hits:
                    redact_check.log_blocked(fn_hits)
                    display = fp.name
            feishu_client.send_file(to, fp, id_type, display_name=display)

        # 大屏 PPTX (meeting_summary 场景, 如 meeting-review): deck 的**真名口径**大屏版
        # (显示锚点称谓, 见 meeting_summary_pptx)。与 report.md 不同源、是二进制, 无法过内容闸
        # 逐字扫描, 故只在**出站脱敏已关闭**的场景 (redact_review: false, 如公司内部 meeting-review)
        # 才随卡外发 —— 那种场景 md/html/pdf 本就以真名口径投递, pptx 一致。redact 开启的场景
        # pptx 留在 VM (原设计: 内部大屏专用, scp 取), 不经飞书外发, 保持 §9.2 fail-close。
        pptx_rel = result.get("pptx")
        if pptx_rel and not redact_on:
            pfp = VAULT_ROOT / pptx_rel
            if pfp.exists():
                feishu_client.send_file(
                    to, pfp, id_type, display_name=f"{base}·AI评审报告-大屏{pfp.suffix}")
