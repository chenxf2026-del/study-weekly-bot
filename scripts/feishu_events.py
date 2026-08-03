#!/usr/bin/env python3
"""
feishu_events.py — 飞书事件处理 (v1.1 A3.2 · boss_server /feishu/event 调用)

处理 im.message.receive_v1:
  - 文件消息 (PDF/docx): 访问闸 → 下载到 cases/.review-inbox/ → enqueue → 回执卡片 (排队位)
  - 其他消息: 引导语卡片 ("请直接发送文档文件")

范围决策 (MVP): 只收**文件消息** — 飞书云文档 URL 的导出下载需要 drive 导出权限与格式转换,
留 Phase B (PRD §4)。

幂等: 飞书事件可能重推 — event_id 记入 .seen-events (保留最近 500), 重复直接 ignore。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

VAULT_ROOT = Path(__file__).parent.parent.resolve()
INBOX_DIR = VAULT_ROOT / "cases" / ".review-inbox"
SEEN_FILE_MAX = 500

import feishu_client
import redact_check
import review_access
import review_batch
import review_queue

ALLOWED_EXT = {".pdf", ".docx", ".doc", ".md", ".txt", ".pptx"}
BOUNDARY_NOTE = "⚖️ 评审结论为评委 panel 意见, 供决策参考, 不构成公司决策 (5 镜头评分体系见 handbook)。"


def _safe_inbox_dest(inbox: Path, ts: str, file_name: str, prefix: str = "") -> Path:
    """把飞书提供的 file_name 取 basename 后再拼进 inbox, 防路径穿越。

    file_name 来自飞书消息内容 (不可信), 含 `../` / 绝对路径的名字若直接拼接会写出
    inbox 之外 (任意文件写)。`Path(file_name).name` 只保留最后一段, 剥掉所有目录成分,
    保证结果 dest 的父目录恒为 inbox。三处下载落盘 (persona / 单份 review / batch) 共用。
    """
    return inbox / f"{prefix}{ts}-{Path(file_name).name}"


def _scene_scoring_mode(scene_slug: str | None) -> str | None:
    """resolve scene panel 的 scoring_mode (sum_max_score / weighted_average); 失败返回 None。"""
    if not scene_slug:
        return None
    try:
        import panel_loader
        resolved = panel_loader.resolve_panel(f"scenes/{scene_slug}/panel.yaml")
        return resolved.get("scoring_mode")
    except Exception:
        return None


def _scene_output_format(scene_slug: str | None) -> str | None:
    """读 scene panel 的 output_format (在 panel.yaml 顶层); 失败返回 None。与 review_worker 同源判据。"""
    if not scene_slug:
        return None
    try:
        import scene_loader as sl
        import yaml
        scene = sl.load_scene(scene_slug)
        panel = yaml.safe_load(scene.panel_path.read_text(encoding="utf-8")) or {}
        return panel.get("output_format")
    except Exception:  # noqa: BLE001 — 读不到就当普通场景 (发即评)
        return None


def _scene_is_meeting_summary(scene_slug: str | None) -> bool:
    """会议总结评审场景 (output_format==meeting_summary): 文件走「多演讲人攒批 + 触发词」而非发即评。"""
    return _scene_output_format(scene_slug) == "meeting_summary"


def _boundary_note(scene_slug: str | None = None) -> str:
    """受理卡边界声明; 按场景自适应:
    - study_weekly_v8 (自省诊断): 评分不作绩效依据 (雅总《周报自省式诊断》框架), 非"评审/决策参考";
    - sum_max (创赛/OP2 竞赛式): 自定义维度评分体系; 否则 5 镜头。"""
    if _scene_output_format(scene_slug) == "study_weekly_v8":
        return "🪞 自省式诊断, 评分与等级**不作为绩效依据** (雅总《周报自省式诊断》框架)。"
    rubric = "自定义维度评分体系" if _scene_scoring_mode(scene_slug) == "sum_max_score" else "5 镜头评分体系"
    return f"⚖️ 评审结论为评委 panel 意见, 供决策参考, 不构成公司决策 ({rubric}见 handbook)。"


# ─── 多场景支持 ────────────────────────────────────────────────

def _scene_to_cfg(scene: Any) -> dict:
    """SceneConfig → review_access 期望的 cfg dict (含白名单/配额/脱敏策略)。"""
    whitelist_env = getattr(scene.access, "whitelist_env", "") or ""
    raw = os.environ.get(whitelist_env, "") if whitelist_env else ""
    whitelist = [u.strip() for u in raw.split(",") if u.strip()]
    quota = getattr(scene.access, "quota_per_user_daily", 2)
    redact = getattr(scene.report, "redact_review", True)
    trusted = getattr(scene.report, "trusted_groups", None) or []
    # persona T0 名单: 约定 <whitelist_env 去 _WHITELIST>_T0 (如 XX_WHITELIST → XX_T0)
    t0_env = whitelist_env.replace("_WHITELIST", "_T0") if whitelist_env.endswith("_WHITELIST") else ""
    t0 = [u.strip() for u in os.environ.get(t0_env, "").split(",") if u.strip()] if t0_env else []
    fe_conf = getattr(scene, "feishu", None)
    bot_name = getattr(fe_conf, "bot_name", "") or ""
    app_id_env = getattr(fe_conf, "app_id_env", "") or ""
    return {
        "whitelist": whitelist,
        "quota": {"per_user_daily": quota},
        "redact_review": redact,
        "trusted_groups": trusted,
        "trust_p2p": bool(getattr(scene.access, "trust_p2p", False)),  # 单聊默认可信
        "persona_t0": t0,
        "bot_name": bot_name,           # 群里判定"是否 @ 到本 bot": 名字子串兜底
        "bot_open_id": _bot_open_id(app_id_env),  # 主判据 (精确, 改名不受影响); 取不到则空 → 回退 bot_name
        "allow_cloud_doc": bool(getattr(scene.access, "allow_cloud_doc", False)),  # 云文档直读 (M1)
        "group_open": bool(getattr(scene.access, "group_open", False)),  # 群内 @ 即放行白名单 (per-scene)
        # 群里分享标题含此关键词的飞书云文档 → 免 @ 自动评审 (study-weekly: "周报")。空 = 关 (仍需 @)。
        "auto_review_title_keyword": (getattr(scene.access, "auto_review_title_keyword", "") or "").strip() or None,
        "title_gate_notify": bool(getattr(scene.access, "title_gate_notify", False)),
    }


def _load_cfg(scene_slug: str | None) -> Optional[dict]:
    """加载访问控制配置: 有 scene_slug 时用 scene 配置, 否则回退全局 review_service.yaml。"""
    if scene_slug:
        try:
            import scene_loader as sl
            scene = sl.load_scene(scene_slug)
            return _scene_to_cfg(scene)
        except Exception:
            pass
    return review_access.load_config()


# ─── 幂等 ──────────────────────────────────────────────────────

def _seen_path(inbox: Path) -> Path:
    return inbox / ".seen-events"


# 幂等去重的「读-判-改-写」必须串行化: 飞书 WS 客户端在多线程里并发投递事件,
# 无锁的 read-modify-write 会丢条目 (两线程同读旧表 → 各自覆盖对方追加的 id → 去重漏判 → 重复 enqueue)。
_SEEN_LOCK = threading.Lock()


def _is_duplicate(event_id: str, inbox: Path = INBOX_DIR) -> bool:
    if not event_id:
        return False
    p = _seen_path(inbox)
    with _SEEN_LOCK:
        seen = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
        if event_id in seen:
            return True
        seen.append(event_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        # 原子写: 同目录临时文件 + os.replace, 防并发/崩溃写出半截 .seen-events
        tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}")
        tmp.write_text("\n".join(seen[-SEEN_FILE_MAX:]), encoding="utf-8")
        os.replace(tmp, p)
    return False


# ─── 卡片构造 (lark interactive card 最简结构) ─────────────────

def _card(title: str, lines: list[str], template: str = "blue") -> dict:
    return {
        "header": {"title": {"tag": "plain_text", "content": title},
                   "template": template},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
    }


def receipt_card(job_id: str, pos: int, scene_slug: str | None = None) -> dict:
    # study-weekly 自省诊断是轻量单评委路径 (~5 分钟/单); 通用多评委评审仍 ~18-25 分钟。
    eta = "约 5 分钟" if _scene_output_format(scene_slug) == "study_weekly_v8" else "约 18-25 分钟"
    return _card("📋 评审已受理", [
        f"任务号: `{job_id}` · 排队第 **{pos}** 位",
        f"单线评审, 每单{eta}; 完成后本卡片对话内回推结果。",
        _boundary_note(scene_slug),
    ])


def denied_card(reason: str) -> dict:
    return _card("⛔ 暂无法受理", [reason], template="grey")


def guide_card(scene_slug: str | None = None, cfg: dict | None = None) -> dict:
    """非文件消息时的引导语, 按场景能力自适应:
    - allow_cloud_doc: 提示可发飞书云文档链接 (study-weekly 已支持); 否则仅文件 + 其他机器人陆续开放;
    - study_weekly_v8: 单教练 v8 自省诊断措辞 (非"5-7 位评委")。"""
    allow_cloud = bool((cfg or {}).get("allow_cloud_doc"))
    kw = _auto_review_keyword(cfg)
    if _scene_output_format(scene_slug) == "study_weekly_v8":
        submit = ("发你的**周报**(飞书云文档链接或 PDF / Word / Markdown 文件)"
                  if allow_cloud else "发你的**周报**文件 (PDF / Word / Markdown)")
        first = (f"{submit}。标题含「{kw}」的飞书云文档**发到群里即自动评审**(不用 @); 其它文档或文件请 **@我**。"
                 if kw else f"{submit}, 群里记得 **@我**。")
        return _card("👋 学习小组周报评估", [
            first,
            *( [f"⚠️ **只评审标题含「{kw}」的文档** —— 云文档与直接发的 Word / PDF 同此规则, "
                f"标题不含「{kw}」的一律不进评审。"] if kw else [] ),
            "我按雅总**《周报自省式诊断》**框架做**自省诊断**, 约 5 分钟回**六段式个人报告** "
            "(5 维基础分 + 反向扣分 + 改进建议 + 重写示例)。",
            "发 **`/help`** 看全部命令; 发 **「汇总」** 出本周全组汇总。",
            _boundary_note(scene_slug),
        ])
    submit = ("发**飞书云文档链接**或**文件** (PDF / Word / Markdown)" if allow_cloud
              else "请**直接发送文档文件** (PDF / Word / Markdown)")
    lines = [f"{submit}, 我会派 5-7 位评委独立评审。"]
    if not allow_cloud:
        lines.append("暂不支持飞书云文档链接 (学习小组机器人已支持, 其他陆续开放)。")
    lines += ["发 **`/help`** 看全部命令用法。", _boundary_note(scene_slug)]
    return _card("👋 报告评审机器人", lines)


def batch_ack_card(count: int, file_name: str) -> dict:
    """会议总结评审: 文件已纳入攒批 (未开评) 的回执。"""
    return _card("📥 已收会议材料", [
        f"已收第 **{count}** 份：`{file_name}`",
        "多演讲人材料可**逐份发送**；全部发完后发一句 **「评审」**（或「开始」「汇总」）即开始生成多视角会议总结。",
        "发 **「取消」**（或「清空」）可丢弃本次已收材料、重新开始。",
        "⏳ 6 小时内未开始将自动清空，防跨会串料。",
    ], template="turquoise")


def batch_empty_card() -> dict:
    """会议总结评审: 收到触发词但本会话还没攒到材料。"""
    return _card("🤔 还没收到材料", [
        "本次还没收到会议材料。请先**逐份发送**各演讲人的纪要/演讲稿文件，再发 **「评审」** 开始生成。",
    ], template="grey")


def batch_cleared_card(count: int) -> dict:
    """会议总结评审: 已清空本次攒批 (用户发「取消/清空」)。"""
    return _card("🗑️ 已清空本次攒批", [
        f"已丢弃本会话攒的 **{count}** 份材料，可重新逐份发送。",
    ], template="grey")


# ─── 群投递脱敏策略 ────────────────────────────────────────────

def _group_redact_required(chat_id: str, cfg: dict) -> bool:
    """群投递是否必须过出站脱敏闸。优先级: trusted_groups > redact_group > 跟随 redact_review。

    - chat_id ∈ trusted_groups   → False (该群放开, 即便 redact_review:true 也放; recipient-aware)
    - redact_group: true         → True  (显式: 群恒强制脱敏, 即便 redact_review:false 也拦)
    - redact_group: false        → False (显式: 所有群放开)
    - 未配 redact_group (默认)    → **跟随 redact_review**: 单租户已设 redact_review:false 时群一并放开,
                                    无需再单独配置 (运营者既已对单聊放开, 全内部群同理)。

    ⚠ 机密边界: 放开 = 完整报告广播给全群。前提是该群成员全内部 + owner 同意
      (同 redact_review 放开)。出现外部/混合群时用 redact_group:true 收紧, 或仅 trusted_groups 精确放行。
    """
    if chat_id and chat_id in (cfg.get("trusted_groups") or []):
        return False
    rg = cfg.get("redact_group")
    if rg is True:
        return True
    if rg is False:
        return False
    # 默认跟随 redact_review (缺省 True = fail-close)
    return cfg.get("redact_review", True) is not False


# ─── admin 文字命令 (模型管理) ─────────────────────────────────

def _extract_message_text(message: dict) -> str:
    """从 text / post 消息取纯文本 (去掉 @mention 占位符 @_user_1 / @_all)。

    飞书把「1. …」这类开头的消息自动转成**富文本 post** (有序列表), msg_type=post,
    content 结构是 {title, content:[[{tag,text},...],...]} 而非 {text}。若只认 text,
    这类消息会漏判 persona → 落到评审引导卡 (2026-07-03 评测暴露)。故两种都提。
    """
    try:
        content = json.loads(message.get("content", "{}"))
    except (json.JSONDecodeError, TypeError):
        return ""
    raw = content.get("text")
    if raw is None and isinstance(content.get("content"), list):
        # 富文本 post: 拼 title + 所有 text run (tag=text/a 带 text 的元素)。
        parts: list[str] = []
        if content.get("title"):
            parts.append(str(content["title"]))
        for line in content["content"]:
            if not isinstance(line, list):
                continue
            for el in line:
                if isinstance(el, dict) and el.get("text"):
                    parts.append(str(el["text"]))
        raw = " ".join(parts)
    text = str(raw or "")
    text = re.sub(r"@_\w+", "", text)   # 群里 @机器人 → 文本含 "@_user_1 /help", 剥占位符
    return text.strip()


def _has_mention(message: dict) -> bool:
    """消息是否含 @mention (任意)。im.message 的 mentions 在 message 级。"""
    return bool(message.get("mentions"))


def _auto_review_keyword(cfg: Optional[dict]) -> Optional[str]:
    """场景的**标题关键词闸**; 未配 → None (该场景不设标题门槛)。

    配了之后是**全路径硬门槛** (2026-07-26 主理拍板): 云文档 / 直接发的文件 / 引用的文件,
    标题 (文件名) 不含此词一律**不进评审**。此前只在「免 @ 群分享」这一条路上生效, @ 机器人
    或单聊发任意文档都会被评 —— 与"只评周报"的意图不符。
    """
    return ((cfg or {}).get("auto_review_title_keyword") or "").strip() or None


def _title_gate_blocked(cfg: Optional[dict], title: str) -> Optional[str]:
    """标题闸: 场景配了关键词且标题不含 → 返回该关键词 (调用方据此拦下); 放行 → None。

    未配关键词的场景 (op2-* / meeting-review / persona 等) 恒放行, 行为完全不变。
    """
    kw = _auto_review_keyword(cfg)
    return kw if (kw and kw not in (title or "")) else None


def _title_gate_notify(cfg: Optional[dict], is_group: bool) -> bool:
    """被标题闸拦下时是否回卡 —— **群里恒静默, 只在单聊提示** (主理 2026-07-27 实测拍板)。

    两次真机反馈定下的口径:
    - **群里恒静默** (不受任何开关控制): 21 人群里成员本就常分享各类文档 (合集、图书版
      PDF …), 每份弹一张拒绝卡极吵。且 group_open 场景下发文件**不需要 @**, 所谓"显式
      提交"实际覆盖了群内全部文件。"不进入评审"最自然的表现就是什么都不做。
    - **单聊提示** (默认开): 一对一、低流量, 且对方明确是冲着机器人来的 —— 没反应会让人
      以为机器人坏了, 反复重发。这里的一张卡是帮忙不是噪声。

    `scene.access.title_gate_notify: false` 可把单聊也静音 (群里本就静音, 该开关对群无效)。
    """
    return (not is_group) and bool((cfg or {}).get("title_gate_notify", True))


def title_required_card(keyword: str, name: str) -> dict:
    """标题不含关键词时的说明卡 (仅在 title_gate_notify 开启时回; 默认静默)。"""
    return _card("未进入评审", [
        f"本机器人只评审标题含「**{keyword}**」的文档。",
        f"收到的标题: `{name or '(空)'}`",
        "",
        f"请把标题改成含「{keyword}」再发一次, 例: `张三-W30{keyword}`。",
    ], template="orange")


def _should_auto_review_doc(message: dict, cfg: Optional[dict], *, is_group: bool, at_bot: bool) -> bool:
    """群里**未 @** 本 bot 的消息, 若场景配了 auto_review_title_keyword 且消息里**含飞书云文档链接**
    → 免 @ 进云文档评审路径。

    只是**进入门槛**的廉价预判 (省得给每条群消息都拉文档): 真正是否评审仍在 _maybe_review_cloud_doc
    里 —— 拉到文档后用 require_title_keyword 核对**真实标题**含关键词才入队, 否则静默跳过。

    ⚠️ 不能只判"消息文本含关键词": 飞书分享文档时**原始消息文本往往只有 URL**, 标题是客户端按链接
    渲染的、不在文本里 (2026-07-22 真机实测: msg_type=text 但文本无「周报」→ 漏触发)。故改判"有文档
    链接" (extract_doc_refs), 关键词留给拉到真标题后核对。文本恰好含关键词的也放行 (纯文本提及场景)。
    单聊 / 已 @ / 未配关键词 一律不走此路 (各自既有逻辑不变)。"""
    if at_bot or not is_group:
        return False
    kw = _auto_review_keyword(cfg)
    if not kw:
        return False
    text = _extract_message_text(message)
    if kw in text:
        return True
    try:
        import feishu_docio
        return bool(feishu_docio.extract_doc_refs(text))
    except Exception:  # noqa: BLE001
        return False


def _review_group_open() -> bool:
    """群里是否放开评审白名单 (REVIEW_GROUP_OPEN 全局开关, 默认关)。
    开启后**群聊**里谁都能发文件评审 (配额仍生效); 单聊不受影响。改回收紧只需 unset + 重启。"""
    return os.environ.get("REVIEW_GROUP_OPEN", "").strip().lower() in ("1", "true", "yes", "on")


def _review_group_daily_cap() -> Optional[int]:
    """群放开时的单群日上限 (REVIEW_GROUP_DAILY_CAP, 默认 30)。防单群被刷爆。
    设为 0 / 负数 / 非数 → 不限 (None)。"""
    raw = os.environ.get("REVIEW_GROUP_DAILY_CAP", "30").strip()
    try:
        n = int(raw)
    except ValueError:
        return None
    return n if n > 0 else None


def _wl_bypass(is_group: bool, cfg) -> bool:
    """白名单 bypass: 群放开 (group_open / 全局 REVIEW_GROUP_OPEN) 或 单聊放开 (trust_p2p)。
    trust_p2p 语义: "能单聊本 bot 即已过人工授权" (app 可见范围人工审核, 同 persona)。配额仍生效。"""
    c = cfg or {}
    if is_group:
        return _review_group_open() or bool(c.get("group_open"))
    return bool(c.get("trust_p2p"))


def _mention_name(m: Any) -> str:
    """取一个 mention 的显示名 (兼容 dict 与 lark typed 对象)。"""
    if isinstance(m, dict):
        return str(m.get("name") or "")
    return str(getattr(m, "name", "") or "")


def _mention_open_id(m: Any) -> str:
    """取一个 mention 被 @者的 open_id (兼容 dict 与 lark typed 对象; 取不到返回 '')。
    dict: m['id']['open_id']; typed: m.id.open_id。"""
    idobj = m.get("id") if isinstance(m, dict) else getattr(m, "id", None)
    if idobj is None:
        return ""
    if isinstance(idobj, dict):
        return str(idobj.get("open_id") or "")
    return str(getattr(idobj, "open_id", "") or "")


# bot 自己的 open_id 缓存 (app_id → open_id)。open_id 恒定不变, 成功即永久缓存;
# 失败按 app_id 节流 60s 重试 (免每条事件打 API), 期间回退名字匹配。
_BOT_OID: dict[str, str] = {}
_BOT_OID_TRIED: dict[str, float] = {}


def _bot_open_id(app_id_env: str) -> str:
    """查本 bot 自己的 open_id (飞书 /bot/v3/info 的 bot.open_id), 按 app_id 缓存。

    凭证由 feishu_ws_client 在 job 线程内 set_bot_creds 设好 (thread-local), 故此处
    get_tenant_token 用的是当前 bot 的凭证。任何失败 → 返回 '' → 调用方回退名字匹配
    (fail-safe, 绝不阻断)。"""
    app_id = os.environ.get(app_id_env, "").strip() if app_id_env else ""
    if not app_id:
        return ""
    if app_id in _BOT_OID:
        return _BOT_OID[app_id]
    import time
    if time.time() - _BOT_OID_TRIED.get(app_id, 0.0) < 60.0:
        return ""   # 最近试过还没成功 → 先用名字兜底, 别每条事件都打 API
    _BOT_OID_TRIED[app_id] = time.time()
    oid = ""
    try:
        import httpx
        token = feishu_client.get_tenant_token()
        resp = httpx.get(f"{feishu_client.BASE}/bot/v3/info",
                         headers={"Authorization": f"Bearer {token}"}, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 0:
            oid = str((data.get("bot") or {}).get("open_id") or "")
    except Exception as e:  # noqa: BLE001 — fail-safe: 回退名字匹配
        print(f"[feishu_events] ⚠ 取 bot open_id 失败 ({app_id}): "
              f"{type(e).__name__}: {e}; 本次回退 bot_name 名字匹配", flush=True)
    if oid:
        _BOT_OID[app_id] = oid
    return oid


def _is_bot_mentioned(message: dict, cfg: Optional[dict]) -> bool:
    """群里是否**@到了本 bot** (而非 @别人)。只 @别人不该触发机器人 (2026-07-03 群里暴露)。

    主判据: **bot open_id 精确匹配** (cfg['bot_open_id'] == 某 mention 的 open_id) —
    运维在飞书后台改显示名也不受影响 (根治 2026-07-07 四连坐 bot_name 漂移)。
    兜底: 显示名子串匹配 (bot_name, 如「周报助手」⊂「周报助手的机器人」) — open_id
    未知/未命中时沿用旧逻辑, 保证零回归。cfg 无 bot_name (单 bot 回退) → 保守「有 @ 即算」。
    """
    mentions = message.get("mentions") or []
    if not mentions:
        return False
    # 主: open_id 精确匹配 (谁改显示名都不错)
    bot_oid = str((cfg or {}).get("bot_open_id") or "").strip()
    if bot_oid and any(_mention_open_id(m) == bot_oid for m in mentions):
        return True
    # 兜底: 显示名子串 (open_id 缺失或未命中时的向后兼容路径)
    bot_name = str((cfg or {}).get("bot_name") or "").strip()
    if not bot_name:
        return True
    return any(bot_name in _mention_name(m) for m in mentions)


def _try_parse_admin_command(message: dict):
    """text 消息 → admin 命令 (verb, args) 或 None。延迟 import 防硬依赖。"""
    try:
        import feishu_admin
    except Exception:
        return None
    return feishu_admin.parse_admin_command(_extract_message_text(message))


def _dispatch_admin_command(cmd, sender_id: str, reply_id: str, reply_id_type: str,
                            *, scene_slug: str | None = None) -> dict[str, Any]:
    """命令路由。/help + /模式(查询) 所有人可见; 切模型 (/model) + 切模式 (/模式 大会) 仅管理员。"""
    import feishu_admin
    verb, args = cmd
    # /模式 (人格模式切换) 是上游 persona 分身场景专有, 本仓不含该场景 → 直接拒。
    if verb == "mode":
        _safe_send(reply_id, denied_card("本机器人不支持人格模式切换。"), reply_id_type)
        return {"status": "denied", "reason": "mode command not supported"}
    # /help 对所有人开放 (帮助本身不涉及修改); 其余命令限管理员
    if verb != "help" and not feishu_admin.is_admin(sender_id):
        _safe_send(reply_id, denied_card(
            "模型管理命令仅限管理员 (BOSS_ADMIN_WHITELIST)。发 /help 看可用命令。"), reply_id_type)
        return {"status": "denied", "reason": "non-admin admin-command", "verb": verb}
    data = feishu_admin.handle_admin_command(verb, args)
    _safe_send(reply_id, _card(data["title"], data["lines"], data.get("template", "blue")), reply_id_type)
    return {"status": "admin_command", "verb": verb}


def _dispatch_loop_command(text: str, sender_id: str, reply_id: str, reply_id_type: str,
                           *, scene_slug: str | None = None) -> dict[str, Any]:
    """战略 OS M1/M2 回路命令:
    「决策 <sid> <判定> [理由:…]」 → 决策捕获 (采纳/部分 → 自动生成行动台账条目)
    「进展 <sid> <状态> [说明]」  → 行动 check-in 回收

    语法不合 → 对应用法卡 (不静默, 用户明确在尝试本命令)。成败都回执;
    telemetry 落事件 (fail-open, 不记正文)。"""
    from boss_core import loop as _loop
    stripped = text.strip()
    if stripped.startswith("进展"):
        parsed = _loop.parse_checkin_command(text)
        if parsed is None:
            _safe_send(reply_id, _card("🧭 进展命令用法",
                                       list(_loop.capture.CHECKIN_USAGE_LINES)), reply_id_type)
            return {"status": "loop_checkin_usage"}
        ok, title, lines = _loop.capture_checkin(
            VAULT_ROOT / "strategy", sid=parsed["sid"], status=parsed["status"],
            note=parsed["note"], by=sender_id)
        _safe_send(reply_id, _card(title, lines, "green" if ok else "orange"), reply_id_type)
        _loop_telemetry("loop_checkin", scene_slug, parsed["sid"], parsed["status"], ok)
        return {"status": "loop_checkin", "ok": ok, "sid": parsed["sid"]}
    parsed = _loop.parse_decision_command(text)
    if parsed is None:
        _safe_send(reply_id, _card("🧭 决策命令用法", list(_loop.capture.USAGE_LINES)),
                   reply_id_type)
        return {"status": "loop_decision_usage"}
    ok, title, lines = _loop.capture_decision(
        VAULT_ROOT / "reports", VAULT_ROOT / "strategy",
        sid=parsed["sid"], verdict=parsed["verdict"], reason=parsed["reason"],
        decider=sender_id)
    _safe_send(reply_id, _card(title, lines, "green" if ok else "orange"), reply_id_type)
    _loop_telemetry("loop_decision", scene_slug, parsed["sid"], parsed["verdict"], ok)
    return {"status": "loop_decision", "ok": ok, "sid": parsed["sid"],
            "verdict": parsed["verdict"]}


def _loop_telemetry(kind: str, scene: str | None, sid: str, detail: str, ok: bool) -> None:
    """回路命令遥测 (fail-open): 管理台活动流可见, 不记建议/说明正文。"""
    try:
        import telemetry
        telemetry.record_event(kind, scene=scene, sid=sid, detail=detail, ok=ok)
    except Exception:  # noqa: BLE001
        pass


# ─── review 场景: 文件 → 多评委评审队列 ─────────────────────────

def _dispatch_review_file(message: dict, sender_id: str, reply_id: str, reply_id_type: str,
                          *, scene_slug: str | None, is_group: bool, chat_id: str,
                          cfg: dict | None, inbox: Path,
                          file_message: dict | None = None) -> dict[str, Any]:
    """review 场景把文件送进多评委评审队列 (访问闸 → 下载 → enqueue)。
    file_message 非空时评**被引用的父文件消息** (群里「引用文件 + @机器人」); 缺省评 message 本身。
    鉴权/回推按 message 的发送者; 文件内容/下载从 file_message (缺省即 message) 取。"""
    src = file_message or message
    # 访问闸 (白名单 / 配额 — fail-close)。REVIEW_GROUP_OPEN 时群里放开白名单, 配额 + 单群日上限仍生效。
    bypass = _wl_bypass(is_group, cfg)
    _gid = chat_id if (bypass and is_group) else None      # 群上限只对真群挂; 单聊 bypass 不挂
    _gcap = _review_group_daily_cap() if (bypass and is_group) else None
    access = review_access.check_access(sender_id, config=cfg, bypass_whitelist=bypass,
                                        group_id=_gid, group_daily_cap=_gcap)
    if not access.allowed:
        _safe_send(reply_id, denied_card(access.reason), reply_id_type)
        return {"status": "denied", "reason": access.reason}

    try:
        content = json.loads(src.get("content", "{}"))
    except json.JSONDecodeError:
        content = {}
    file_key = content.get("file_key", "")
    file_name = content.get("file_name", "document.pdf")
    ext = Path(file_name).suffix.lower()
    if not file_key or ext not in ALLOWED_EXT:
        _safe_send(reply_id, denied_card(
            f"暂只支持 {' / '.join(sorted(ALLOWED_EXT))} 文件 (收到: {file_name})"), reply_id_type)
        return {"status": "denied", "reason": f"unsupported file: {file_name}"}

    # 标题闸 (2026-07-26): 场景配了关键词 → 文件名不含即不进评审。直接发的 word/pdf 与
    # 「引用文件 + @」都走这里, 故一处即覆盖两条路。
    # **群里恒静默, 单聊才提示** (2026-07-27 实测: 群里文件流量大, 每份弹拒绝卡极吵)。
    # 未入队 → 不占配额。
    if (_blocked_kw := _title_gate_blocked(cfg, file_name)):
        if _title_gate_notify(cfg, is_group):
            _safe_send(reply_id, title_required_card(_blocked_kw, file_name), reply_id_type)
        print(f"[feishu_events] 标题闸拦下 scene={scene_slug} kind=file "
              f"name={file_name!r} kw={_blocked_kw!r}", flush=True)
        return {"status": "skipped_no_title_keyword",
                "reason": f"文件名不含 {_blocked_kw!r}: {file_name!r}"}

    ts = datetime.now(timezone.utc).strftime("%m%d%H%M%S%f")   # %f 微秒: 防同秒并发提交的 dest/brand 互相覆盖 (同 batch :843)
    # brand: scene 级 brand_prefix-{ts}; 否则沿用旧格式 review-{user[:8]}-{ts}
    if scene_slug:
        try:
            import scene_loader as sl
            _s = sl.load_scene(scene_slug)
            _prefix = _s.report.brand_prefix or scene_slug
        except Exception:
            _prefix = scene_slug
    else:
        _prefix = f"review-{sender_id.replace('ou_', '')[:8]}"
    dest = _safe_inbox_dest(inbox, ts, file_name)   # basename 防路径穿越
    try:
        feishu_client.download_message_file(src.get("message_id", ""), file_key, dest)
    except Exception as e:
        _safe_send(reply_id, denied_card(f"文档下载失败 ({type(e).__name__}), 请重发一次"), reply_id_type)
        return {"status": "error", "reason": f"download failed: {e}"}

    brand = f"{_prefix}-{ts}"
    job_id = review_queue.enqueue({
        "submitter": sender_id,           # 鉴权/审计按人 (open_id)
        "notify_to": reply_id,            # 回推目标: 群=chat_id, 单聊=open_id
        "notify_id_type": reply_id_type,  # "chat_id" | "open_id"
        # 群 = 非单一收件人, 默认强制过出站闸 (即便单租户 redact_review:false 放开了单聊)。
        "force_redact": is_group and _group_redact_required(chat_id, cfg or {}),
        "doc": str(dest),
        "doc_name": file_name,
        "brand_slug": brand,
        "source": "feishu",
        **({"scene_slug": scene_slug} if scene_slug else {}),
    })
    pos = review_queue.position(job_id) or 1
    _safe_send(reply_id, receipt_card(job_id, pos, scene_slug), reply_id_type)
    return {"status": "accepted", "job_id": job_id, "queue_position": pos}


# ─── 云文档直读 (study-weekly M1 · scene.access.allow_cloud_doc 开关) ──

def cloud_doc_fail_card(kind: str) -> dict:
    import feishu_docio
    return _card("云文档读取失败", feishu_docio.permission_help_lines(kind), template="orange")


def _maybe_review_cloud_doc(message: dict, sender_id: str, reply_id: str, reply_id_type: str,
                            *, scene_slug: str | None, is_group: bool, chat_id: str,
                            cfg: Optional[dict], inbox: Path,
                            require_title_keyword: Optional[str] = None,
                            silent_skip: bool = False) -> Optional[dict]:
    """文本消息含飞书云文档链接 → 拉正文写 inbox → enqueue (与文件同路径)。

    返回 None = 未命中 (无链接 / 场景未开 allow_cloud_doc), 上层继续静默/引导逻辑。
    fail-open 兜底: 读取失败回分型指引卡, 绝不静默。"""
    if not (cfg or {}).get("allow_cloud_doc"):
        return None
    try:
        import feishu_docio
    except Exception:  # noqa: BLE001
        return None
    refs = feishu_docio.extract_doc_refs(_extract_message_text(message))
    if not refs:
        return None
    # 访问闸 (与文件路径同款 fail-close)
    bypass = _wl_bypass(is_group, cfg)
    access = review_access.check_access(
        sender_id, config=cfg, bypass_whitelist=bypass,
        group_id=chat_id if (bypass and is_group) else None,
        group_daily_cap=_review_group_daily_cap() if (bypass and is_group) else None)
    if not access.allowed:
        _safe_send(reply_id, denied_card(access.reason), reply_id_type)
        return {"status": "denied", "reason": access.reason}
    kind, token = refs[0]                      # 一条消息评第一篇 (多链接后续可扩)
    try:
        title, text = feishu_docio.fetch_doc_text(kind, token)
    except feishu_docio.DocioError as e:
        _safe_send(reply_id, cloud_doc_fail_card(e.kind), reply_id_type)
        return {"status": "error", "reason": f"cloud doc {e.kind}: {e.detail}"}
    except Exception as e:  # noqa: BLE001
        _safe_send(reply_id, cloud_doc_fail_card("api"), reply_id_type)
        return {"status": "error", "reason": f"cloud doc fetch: {type(e).__name__}"}
    # 标题闸: 用**真实文档标题**核对 (进入门槛用消息文本预判, 这里用真标题防误评)。
    # 2026-07-26 改为**全路径生效** —— 此前只在免 @ 群分享这条路上过滤, @ 机器人发任意
    # 云文档都会被评, 与"只评周报"的意图不符。
    # 回不回卡按触发方式分:
    #   - 免 @ 群分享 (silent_skip) → 静默跳过, 免刷屏 (本就没人叫机器人)
    #   - 显式 @ / 单聊        → 回卡说明, 否则用户以为机器人坏了
    # 两种都**不入队** → 不占配额 (配额按队列已有 job 计数)。
    if require_title_keyword and require_title_keyword not in (title or ""):
        if not silent_skip and _title_gate_notify(cfg, is_group):
            _safe_send(reply_id, title_required_card(require_title_keyword, title), reply_id_type)
        print(f"[feishu_events] 标题闸拦下 scene={scene_slug} kind=cloud_doc "
              f"title={title!r} kw={require_title_keyword!r}", flush=True)
        return {"status": "skipped_auto_no_keyword" if silent_skip else "skipped_no_title_keyword",
                "reason": f"标题不含 {require_title_keyword!r}: {title!r}"}
    ts = datetime.now(timezone.utc).strftime("%m%d%H%M%S%f")
    safe_title = re.sub(r"[^\w一-鿿.-]+", "-", title).strip("-")[:60] or "cloud-doc"
    dest = _safe_inbox_dest(inbox, ts, f"{safe_title}.md")
    dest.write_text(f"# {title}\n\n{text}", encoding="utf-8")
    if scene_slug:
        try:
            import scene_loader as sl
            _prefix = sl.load_scene(scene_slug).report.brand_prefix or scene_slug
        except Exception:  # noqa: BLE001
            _prefix = scene_slug
    else:
        _prefix = f"review-{sender_id.replace('ou_', '')[:8]}"
    job_id = review_queue.enqueue({
        "submitter": sender_id,
        "notify_to": reply_id,
        "notify_id_type": reply_id_type,
        "force_redact": is_group and _group_redact_required(chat_id, cfg or {}),
        "doc": str(dest),
        "doc_name": f"{title}.md",
        "brand_slug": f"{_prefix}-{ts}",
        "source": "feishu-cloud-doc",
        **({"scene_slug": scene_slug} if scene_slug else {}),
    })
    pos = review_queue.position(job_id) or 1
    _safe_send(reply_id, receipt_card(job_id, pos, scene_slug), reply_id_type)
    return {"status": "accepted", "job_id": job_id, "queue_position": pos,
            "cloud_doc": token}


# ─── 学习小组周汇总 (M2 · 「汇总」命令 → 确定性生成即回) ─────────

def _dispatch_study_weekly_summary(reply_id: str, reply_id_type: str) -> dict:
    """「汇总」命令: 生成目标周 (昨天所在周) 汇总 → 卡片摘要 + md 附件回群。零 LLM 秒回。"""
    try:
        import gen_study_weekly_summary as gsw
        week = gsw.default_target_week()
        roster = gsw.load_roster()
        rows = gsw.collect_week(week)
        md = gsw.render_summary(week, rows, roster)
        gsw.OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = gsw.OUT_DIR / f"study-weekly-summary-{week}.md"
        out.write_text(md, encoding="utf-8")
        head = [f"目标周 {week} · 收到 {len(rows)} 份"]
        for i, r in enumerate(rows[:8], 1):
            head.append(f"{i}. {r.get('member','')} {r.get('total',0):.0f} 分 "
                        f"({r.get('grade','')}) {r.get('core_label','')}")
        if roster:
            missing = [m['name'] for m in roster
                       if m['name'] not in {r.get('member') for r in rows}]
            head.append("未交: " + ("、".join(missing) if missing else "无 🎉"))
        head.append("完整报告见附件 · 自省式诊断, 不作为绩效依据")
        _safe_send(reply_id, _card(f"周报评估汇总 · {week}", head), reply_id_type)
        try:
            feishu_client.send_file(reply_id, out, reply_id_type)
        except Exception as e:  # noqa: BLE001 — 附件失败不影响卡片
            print(f"[feishu_events] ⚠ 汇总附件发送失败: {type(e).__name__}: {e}", flush=True)
        return {"status": "ok", "summary_week": week, "count": len(rows)}
    except Exception as e:  # noqa: BLE001
        _safe_send(reply_id, _card("汇总失败", [f"{type(e).__name__}: {e}", "请稍后重试"],
                                   template="orange"), reply_id_type)
        return {"status": "error", "reason": f"summary: {e}"}


# ─── meeting_summary 场景: 多演讲人攒批 + 触发词汇总 ─────────────
# 飞书一条消息一个文件, 多演讲人材料必须在应用层攒 (见 review_batch)。会议总结评审场景专用:
# 文件先缓冲进批次 (不 enqueue), 收到触发词「评审/开始/汇总」再合并成一份 → enqueue 一个 job。
# 其它 review 场景 (op2-* / persona) 不受影响, 仍发即评。

def _dispatch_review_batch_file(message: dict, sender_id: str, reply_id: str, reply_id_type: str,
                                *, scene_slug: str | None, is_group: bool, chat_id: str,
                                cfg: dict | None, inbox: Path,
                                file_message: dict | None = None) -> dict[str, Any]:
    """meeting_summary 场景: 文件**先攒批** (不 enqueue)。收到触发词才合并成一份评。
    攒批不建 job → 不耗配额 (check_access 只读扫队列); 一场会 = finalize 时 1 份配额。"""
    src = file_message or message
    bypass = _wl_bypass(is_group, cfg)
    _gid = chat_id if (bypass and is_group) else None      # 群上限只对真群挂; 单聊 bypass 不挂
    _gcap = _review_group_daily_cap() if (bypass and is_group) else None
    access = review_access.check_access(sender_id, config=cfg, bypass_whitelist=bypass,
                                        group_id=_gid, group_daily_cap=_gcap)
    if not access.allowed:
        _safe_send(reply_id, denied_card(access.reason), reply_id_type)
        return {"status": "denied", "reason": access.reason}

    try:
        content = json.loads(src.get("content", "{}"))
    except json.JSONDecodeError:
        content = {}
    file_key = content.get("file_key", "")
    file_name = content.get("file_name", "document.pdf")
    ext = Path(file_name).suffix.lower()
    if not file_key or ext not in ALLOWED_EXT:
        _safe_send(reply_id, denied_card(
            f"暂只支持 {' / '.join(sorted(ALLOWED_EXT))} 文件 (收到: {file_name})"), reply_id_type)
        return {"status": "denied", "reason": f"unsupported file: {file_name}"}

    ts = datetime.now(timezone.utc).strftime("%m%d%H%M%S%f")
    tmp = _safe_inbox_dest(inbox, ts, file_name, prefix=".batch-tmp-")   # basename 防路径穿越
    try:
        feishu_client.download_message_file(src.get("message_id", ""), file_key, tmp)
    except Exception as e:
        # HTTPStatusError (飞书返回非 2xx) / *Timeout 多为**文件过大**被资源接口拒收/超时 —— 给可操作提示。
        likely_big = type(e).__name__ in (
            "HTTPStatusError", "ReadTimeout", "WriteTimeout", "PoolTimeout", "ConnectTimeout")
        hint = ("（很可能**文件过大**，飞书下载接口拒收/超时）。会议材料请发**纪要 / 文字版 / PDF**"
                "（几 MB 即可），不必发上百 MB 的原始 PPT；或稍后重发一次。"
                if likely_big else "，请重发一次。")
        _safe_send(reply_id, denied_card(f"文档下载失败（{type(e).__name__}）{hint}"), reply_id_type)
        return {"status": "error", "reason": f"download failed: {e}"}

    # conv_key = reply_id (群=chat_id / 单聊=open_id): 一个会话攒一批, 与回推目标同源。
    try:
        st = review_batch.add(scene_slug or "meeting-review", reply_id, tmp, file_name)
    except Exception as e:  # noqa: BLE001
        _safe_send(reply_id, denied_card(f"材料缓冲失败 ({type(e).__name__}), 请重试"), reply_id_type)
        return {"status": "error", "reason": f"batch add failed: {e}"}
    _safe_send(reply_id, batch_ack_card(st.count, file_name), reply_id_type)
    return {"status": "batched", "count": st.count, "file": file_name}


def _dispatch_review_batch_finalize(sender_id: str, reply_id: str, reply_id_type: str,
                                    *, scene_slug: str | None, is_group: bool, chat_id: str,
                                    cfg: dict | None) -> dict[str, Any]:
    """meeting_summary 场景收到触发词: 把该会话攒的材料合并成一份 → enqueue 一个 job (含 scope=演讲人列表)。"""
    slug = scene_slug or "meeting-review"
    st = review_batch.peek(slug, reply_id)
    if not st or st.count == 0:
        _safe_send(reply_id, batch_empty_card(), reply_id_type)
        return {"status": "batch_empty"}

    # 配额在此 (创建 job 处) 校验一次: 一场会 = 一个 job = 一份配额。
    bypass = _wl_bypass(is_group, cfg)
    _gid = chat_id if (bypass and is_group) else None      # 群上限只对真群挂; 单聊 bypass 不挂
    _gcap = _review_group_daily_cap() if (bypass and is_group) else None
    access = review_access.check_access(sender_id, config=cfg, bypass_whitelist=bypass,
                                        group_id=_gid, group_daily_cap=_gcap)
    if not access.allowed:
        _safe_send(reply_id, denied_card(access.reason), reply_id_type)
        return {"status": "denied", "reason": access.reason}

    combined = review_batch.combine_and_clear(slug, reply_id)
    if not combined:
        _safe_send(reply_id, batch_empty_card(), reply_id_type)
        return {"status": "batch_empty"}
    doc_path, scope, names = combined

    ts = datetime.now(timezone.utc).strftime("%m%d%H%M%S")
    try:
        import scene_loader as sl
        _prefix = sl.load_scene(slug).report.brand_prefix or slug
    except Exception:
        _prefix = slug
    brand = f"{_prefix}-{ts}"
    job_id = review_queue.enqueue({
        "submitter": sender_id,
        "notify_to": reply_id,
        "notify_id_type": reply_id_type,
        "force_redact": is_group and _group_redact_required(chat_id, cfg or {}),
        "doc": str(doc_path),
        "doc_name": f"会议材料合集（{len(names)} 份·多演讲人）",
        "brand_slug": brand,
        "scene_slug": slug,
        "scope": scope,                    # → worker 透传 --scope → deck 封面「资料范围」
        "source": "feishu",
    })
    pos = review_queue.position(job_id) or 1
    _safe_send(reply_id, receipt_card(job_id, pos, slug), reply_id_type)
    return {"status": "accepted", "job_id": job_id, "queue_position": pos, "materials": len(names)}


def _maybe_review_reply_file(message: dict, sender_id: str, reply_id: str, reply_id_type: str,
                             *, scene_slug: str | None, is_group: bool, chat_id: str,
                             cfg: dict | None, inbox: Path) -> Optional[dict[str, Any]]:
    """review 场景「引用某文件消息 + @机器人」→ 评被引用的文件 (进多评委队列)。
    与 persona 的引用评法对齐 (2026-07-04 锚点: review bot 也支持 @+引用触发)。
    非「引用回复」或引用的不是文件 → None (调用方走原静默/引导)。"""
    parent_id = message.get("parent_id") or ""
    if not parent_id:
        return None
    try:
        parent = feishu_client.get_message(parent_id)
    except Exception as e:  # noqa: BLE001 — 取父消息失败 → 当普通文本, 不炸
        print(f"[feishu_events] ⚠ 取父消息失败 {parent_id}: {type(e).__name__}: {e}", flush=True)
        return None
    if (parent.get("message_type") or "") != "file":
        return None   # 引用的不是文件 → 非评审意图
    # meeting_summary 场景: 引用的文件也进攒批 (不立即评), 与直接发文件一致。
    if _scene_is_meeting_summary(scene_slug):
        return _dispatch_review_batch_file(
            message, sender_id, reply_id, reply_id_type,
            scene_slug=scene_slug, is_group=is_group, chat_id=chat_id, cfg=cfg,
            inbox=inbox, file_message=parent)
    return _dispatch_review_file(
        message, sender_id, reply_id, reply_id_type,
        scene_slug=scene_slug, is_group=is_group, chat_id=chat_id, cfg=cfg,
        inbox=inbox, file_message=parent)


# ─── 事件处理 ──────────────────────────────────────────────────

def _emit_msg_received(scene_slug: str | None, msg_type: str, chat_type: str,
                       persona_thread: str | None = None) -> None:
    """管理台活动流埋点: 记一条真人消息的元数据 (无正文)。fail-open。

    persona_thread: 分身场景的对话线程 id (单聊=open_id / 群=chat_id) —— **仅元数据(id), 非正文**,
    供日志页把该行链到「分身观测」对应线程看完整问答。正文仍只在登录后的分身观测页, 不进遥测库。"""
    try:
        import telemetry
        extra = {"persona_thread": persona_thread} if persona_thread else {}
        telemetry.record_event("msg_received", scene=scene_slug,
                               msg_type=msg_type, chat_type=chat_type, **extra)
    except Exception:  # noqa: BLE001
        pass


def handle(body: dict[str, Any], inbox: Path = INBOX_DIR,
           scene_slug: str | None = None) -> dict[str, Any]:
    """boss_server /feishu/event 转入 + 飞书长连接回调共用。永远返回 200 语义 dict —
    任何业务失败都回卡片告知用户, 不让飞书重推。
    scene_slug: 多 bot 时由 feishu_ws_client 注入, 用于加载 scene 级访问控制配置。"""
    header = body.get("header") or {}
    event_type = header.get("event_type", "")
    event_id = header.get("event_id", "")

    if event_type != "im.message.receive_v1":
        return {"status": "ignored", "reason": f"未订阅处理的事件: {event_type or 'unknown'}"}
    if _is_duplicate(event_id, inbox):
        return {"status": "ignored", "reason": "duplicate event_id"}

    event = body.get("event") or {}
    message = event.get("message") or {}
    sender = event.get("sender") or {}
    sender_id = (sender.get("sender_id") or {}).get("open_id", "")
    sender_type = sender.get("sender_type", "")
    msg_type = message.get("message_type", "")
    chat_type = message.get("chat_type", "")
    chat_id = message.get("chat_id", "")
    is_group = chat_type == "group"

    # 群回推到 chat_id (全群可见); 单聊回推到提交者 open_id。
    reply_id = chat_id if is_group else sender_id
    reply_id_type = "chat_id" if is_group else "open_id"

    # 加载访问控制配置: 多 bot 时按 scene; 单 bot 时用全局 review_service.yaml。
    cfg = _load_cfg(scene_slug)

    # 运营日志: 每个收到的事件记一行 (灰度期抓 open_id 填白名单 + 审计谁在用)。
    # 不含消息正文 (隐私 + 出站闸只管外发, 入站日志只记元数据)。
    print(f"[feishu_events] 收到事件 event_id={event_id} sender_open_id={sender_id!r} "
          f"sender_type={sender_type!r} chat_type={chat_type!r} chat_id={chat_id!r} msg_type={msg_type!r}", flush=True)

    if not sender_id:
        return {"status": "ignored", "reason": "无 sender open_id"}

    # 防 bot↔bot 循环: 群里开 im:message.group_msg 后能看到所有消息, 含其他机器人 (同机 Hermes)
    # 与本机器人自己发的报告附件。只处理真人 (sender_type=user) 消息; 显式非 user 一律忽略。
    if sender_type and sender_type != "user":
        return {"status": "ignored", "reason": f"非真人发送者 ({sender_type})"}

    # 管理台活动流埋点 (M0): 真人来一条记一条, 只记元数据 (scene / 消息类型 / 群或单聊),
    # 永不记正文。fail-open: 遥测挂了不影响消息处理。
    # (上游的分身场景会在此带上对话线程 id; 本仓无该场景, 恒 None。)
    _emit_msg_received(scene_slug, msg_type, chat_type, persona_thread=None)

    if msg_type != "file":
        # 文字命令 (/model /models /help): 单聊直接处理; 群里需 @机器人 (有 mention) 才处理,
        # 否则群里每条消息都回卡 = 刷屏。命令解析前已剥 @mention 占位符。
        # text = 纯文本; post = 富文本 (飞书把「1. …」开头自动转成有序列表 post) — 都当文字处理。
        if msg_type in ("text", "post"):
            # ★ meeting_summary 触发词放开 @ 要求: 群里发文件本就不需 @ 即入批, 故「评审/开始/汇总」
            #   也不该强制 @ (2026-07-09 群里踩坑: 发「评审」不 @ 机器人 → 静默忽略)。
            #   仅当该会话**已有攒批**时才接 (无批不响应, 避免群内噪声; @机器人 的显式触发仍走下方常规路径)。
            if (_scene_is_meeting_summary(scene_slug)
                    and review_batch.is_trigger(_extract_message_text(message))
                    and review_batch.peek(scene_slug or "meeting-review", reply_id) is not None):
                return _dispatch_review_batch_finalize(
                    sender_id, reply_id, reply_id_type,
                    scene_slug=scene_slug, is_group=is_group, chat_id=chat_id, cfg=cfg)
            # meeting_summary「取消/清空」: 清空本会话攒批 (同免 @; 仅当有攒批时响应, 避免噪声)。
            if _scene_is_meeting_summary(scene_slug) and review_batch.is_cancel(_extract_message_text(message)):
                _slug = scene_slug or "meeting-review"
                _st = review_batch.peek(_slug, reply_id)
                if _st is not None:
                    review_batch.clear(_slug, reply_id)
                    _safe_send(reply_id, batch_cleared_card(_st.count), reply_id_type)
                    return {"status": "batch_cleared", "count": _st.count}
            # 战略 OS M1/M2 · 回路文本命令: 「决策 <sid> 采纳…」 / 「进展 <sid> 完成…」。
            # 所有人可用 — 决策/进展是 owner 的权利, 不限管理员; 身份记 open_id。
            _loop_txt = _extract_message_text(message)
            if (_loop_txt.strip().startswith(("决策", "进展"))
                    and (not is_group or _is_bot_mentioned(message, cfg))):
                return _dispatch_loop_command(_loop_txt, sender_id, reply_id, reply_id_type,
                                              scene_slug=scene_slug)
            cmd = _try_parse_admin_command(message)
            if cmd is not None and (not is_group or _is_bot_mentioned(message, cfg)):
                return _dispatch_admin_command(cmd, sender_id, reply_id, reply_id_type,
                                               scene_slug=scene_slug)
            # review 场景: 「引用文件 + @机器人」→ 评被引用的文件 (进多评委队列)。
            # 群里需 @到本机器人; 单聊直接认引用。非引用文件的普通文本仍走下方静默/引导。
            # 触发门槛: 单聊 / 群里 @到本 bot = 显式触发 (_at_bot); 群里未 @ 但分享了标题含关键词的
            # 云文档 = 免@自动触发 (_auto_doc, 仅 study-weekly 类场景开 auto_review_title_keyword)。
            _at_bot = (not is_group) or _is_bot_mentioned(message, cfg)
            _auto_doc = _should_auto_review_doc(message, cfg, is_group=is_group, at_bot=_at_bot)
            # 免@自动评审灰度诊断: 群里未@ 且场景配了关键词时记一行判定 (无正文, 仅 msg_type + 有无文档链接
            # + 判定结果)。decision=False 多半是消息无可提取文档链接 (标题不在文本里); True 但无报告则卡在
            # 拉文档/真标题核对。上线稳定后可删。
            if is_group and not _at_bot and _auto_review_keyword(cfg):
                try:
                    import feishu_docio as _fd
                    _nrefs = len(_fd.extract_doc_refs(_extract_message_text(message)))
                except Exception:  # noqa: BLE001
                    _nrefs = -1
                print(f"[feishu_events] auto-review 判定 scene={scene_slug} msg_type={msg_type} "
                      f"doc_refs={_nrefs} decision={_auto_doc}", flush=True)
            if _at_bot or _auto_doc:
                # 以下三条 (会议攒批触发 / 引用文件评审 / 「汇总」命令) 仍需**显式 @** —— 免@自动触发
                # 只放行"标题含关键词的云文档", 不放行命令与引用评审, 免群里误触。
                if _at_bot and _scene_is_meeting_summary(scene_slug) and review_batch.is_trigger(_extract_message_text(message)):
                    return _dispatch_review_batch_finalize(
                        sender_id, reply_id, reply_id_type,
                        scene_slug=scene_slug, is_group=is_group, chat_id=chat_id, cfg=cfg)
                if _at_bot:
                    handled = _maybe_review_reply_file(
                        message, sender_id, reply_id, reply_id_type,
                        scene_slug=scene_slug, is_group=is_group, chat_id=chat_id, cfg=cfg, inbox=inbox)
                    if handled is not None:
                        return handled
                    # 学习小组场景 (M2): 「汇总」→ 即时出周汇总报告 (确定性, 零 LLM)。
                    if (_scene_output_format(scene_slug) == "study_weekly_v8"
                            and _extract_message_text(message).strip() in ("汇总", "汇总报告")):
                        return _dispatch_study_weekly_summary(reply_id, reply_id_type)
                # 云文档直读 (M1, 场景开 allow_cloud_doc): 消息含飞书文档链接 → 拉正文即评。
                # 标题闸**全路径生效** (2026-07-26): 拉到真实标题后核对含关键词才入队。
                # 免 @ 自动触发时静默跳过 (免刷屏); 显式 @ / 单聊则回卡说明。
                handled = _maybe_review_cloud_doc(
                    message, sender_id, reply_id, reply_id_type,
                    scene_slug=scene_slug, is_group=is_group, chat_id=chat_id, cfg=cfg, inbox=inbox,
                    require_title_keyword=_auto_review_keyword(cfg),
                    silent_skip=(_auto_doc and not _at_bot))
                if handled is not None:
                    return handled
        # 群里非命令消息静默忽略 (不刷屏); 单聊保持引导。
        if is_group:
            # 诊断: 有 @ 却被忽略 —— 多半是 scene.bot_name 与飞书真实显示名不符, @ 判不出是叫本 bot,
            # 导致命令/引用评审静默失效 (2026-07-04 市值管理群踩坑)。打一行日志让漂移可见, 不然极难查。
            if _has_mention(message) and not _is_bot_mentioned(message, cfg):
                _names = [_mention_name(m) for m in (message.get("mentions") or [])]
                print(f"[feishu_events] ⚠ 群内有 @ 但未匹配 bot_name={ (cfg or {}).get('bot_name')!r} "
                      f"(被 @ 的名字: {_names}); 若应触发本 bot, 检查 scene.feishu.bot_name 是否为真实显示名子串",
                      flush=True)
            return {"status": "ignored", "reason": f"群内非命令消息 ({msg_type}), 静默忽略"}
        _safe_send(reply_id, guide_card(scene_slug, cfg), reply_id_type)
        return {"status": "guided", "reason": f"非文件消息 ({msg_type})"}

    # meeting_summary 场景 (会议总结评审): 文件先**攒批** (不立即评), 收到触发词再合并成一份 →
    # 支持一次提交多演讲人材料。其它 review 场景 (op2-*) 不受影响, 仍发即评。
    if _scene_is_meeting_summary(scene_slug):
        return _dispatch_review_batch_file(
            message, sender_id, reply_id, reply_id_type,
            scene_slug=scene_slug, is_group=is_group, chat_id=chat_id, cfg=cfg, inbox=inbox)

    # review 场景: 文件 → 多评委评审队列 (访问闸 / 白名单 / 配额 / 群护栏在内)。
    return _dispatch_review_file(
        message, sender_id, reply_id, reply_id_type,
        scene_slug=scene_slug, is_group=is_group, chat_id=chat_id, cfg=cfg, inbox=inbox)


def _safe_send(receive_id: str, card: dict, receive_id_type: str = "open_id") -> Optional[str]:
    """回卡片失败不抛 (事件处理永远 200, 防飞书重推风暴); 失败仅打日志。
    receive_id_type=open_id 发个人 / =chat_id 发群。
    返回飞书 message_id (供流式 update_card 逐步渲染; 失败或取不到 → None)。"""
    try:
        return feishu_client.send_card(receive_id, card, receive_id_type)
    except Exception as e:
        print(f"[feishu_events] ⚠ 卡片发送失败 receive_id={receive_id} "
              f"({receive_id_type}): {type(e).__name__}: {e}")
        return None
