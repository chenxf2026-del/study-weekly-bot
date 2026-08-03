"""boss_core.loop.capture — 决策捕获 (M1 · 文本命令先行)。

owner 在飞书对评审建议做决策的纯逻辑层:
  「决策 <sid> 采纳」 / 「决策 <sid> 不采纳 理由:与当期预算冲突」

设计 (PRD §5.2 + D2 修订):
- v1 走文本命令 (零 SDK 风险, 所有机器人今天即可用); 卡片按钮 M1.5 待 VM
  lark-oapi 验证 card action 回调后加。
- 决策是 owner 的权利, 不限管理员; decider 记 open_id (身份由飞书保证)。
- append-only: 修正 = 再发一条新决策 (追加), 永不改写历史。
- 红线: 本模块只记录人的判断; 不含任何自动决策路径。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

import datetime as _dt

from boss_core.loop.models import ACTION_STATUSES, VERDICTS  # noqa: F401  (取值域来源单一)
from boss_core.loop.store import (
    append_checkin,
    append_decision,
    read_actions,
    read_suggestions,
    upsert_action,
)

# 中文判定词 → verdict (含常见同义)
VERDICT_WORDS = {
    "采纳": "adopt",
    "部分采纳": "partial", "部分": "partial",
    "不采纳": "reject", "拒绝": "reject",
    "校准": "calibrate",
}
_VERDICT_CN = {"adopt": "采纳", "partial": "部分采纳", "reject": "不采纳", "calibrate": "校准"}

_CMD_RE = re.compile(
    r"^\s*决策\s+(?P<sid>\S+)\s+(?P<word>部分采纳|不采纳|采纳|部分|拒绝|校准)"
    r"\s*(?:[,,·]?\s*理由\s*[::]?\s*)?(?P<reason>.*)$",
    re.S)
_SID_RE = re.compile(r"^(?P<brand>.+)-s(?P<n>\d+)$")

USAGE_LINES = [
    "用法 (直接回复本群/本聊):",
    "`决策 <建议ID> 采纳`",
    "`决策 <建议ID> 不采纳 理由:一句话`",
    "判定可选: **采纳 / 部分采纳 / 不采纳 / 校准** — 后三者必须带理由 (取舍理由即知识)。",
    "建议 ID 见评审完成卡片的「优先建议」区 (形如 `<brand>-s1`)。",
    "修正 = 再发一条新决策 (追加记录, 不改历史)。",
]


def parse_decision_command(text: str) -> Optional[dict]:
    """文本 → {sid, verdict, reason} 或 None (非本命令/语法不合)。"""
    m = _CMD_RE.match(text or "")
    if not m:
        return None
    return {
        "sid": m.group("sid").strip().strip("`"),
        "verdict": VERDICT_WORDS[m.group("word")],
        "reason": m.group("reason").strip(),
    }


def brand_from_sid(sid: str) -> Optional[str]:
    m = _SID_RE.match(sid or "")
    return m.group("brand") if m else None


def capture_decision(reports_root: Path, strategy_root: Path, *, sid: str, verdict: str,
                     reason: str, decider: str,
                     now: Optional[float] = None) -> tuple[bool, str, list[str]]:
    """执行捕获 → (ok, 卡片标题, 卡片行)。所有失败路径给可操作的提示, 不抛异常。"""
    brand = brand_from_sid(sid)
    if not brand:
        return False, "❓ 建议 ID 格式不对", [
            f"`{sid}` 不是合法建议 ID (应形如 `<brand>-s1`)。", *USAGE_LINES]
    payload = read_suggestions(Path(reports_root) / brand)
    if not payload:
        return False, "❓ 未找到该评审的建议清单", [
            f"`{brand}` 下没有 suggestions.json —— 可能是旧评审 (M0 上线前) 或 ID 打错。",
            "旧评审如需决策, 重跑一次评审即可生成建议清单。"]
    sug = next((s for s in payload.get("suggestions", []) if s.get("id") == sid), None)
    if sug is None:
        avail = [s["id"] for s in payload.get("suggestions", []) if s.get("priority")]
        return False, "❓ 未找到该建议", [
            f"`{sid}` 不在清单里。本评审的优先建议 ID:",
            " · ".join(f"`{a}`" for a in avail) or "(无)"]
    rec = {
        "suggestion_id": sid,
        "review_brand": brand,
        "scene": payload.get("scene") or "—",
        "suggestion_text": sug.get("text") or "",
        "decider": decider,
        "verdict": verdict,
        "reason": reason,
        "ts": int(now if now is not None else time.time()),
    }
    try:
        append_decision(strategy_root, rec)
    except ValueError as e:
        # 最常见: 非 adopt 缺理由 → 给针对性提示
        if "reason" in str(e):
            return False, "⚠️ 该判定必须带理由", [
                f"`{_VERDICT_CN.get(verdict, verdict)}` 是人对 AI 建议的取舍 —— **理由即知识**, 必填。",
                f"重发: `决策 {sid} {_VERDICT_CN.get(verdict, verdict)} 理由:一句话`"]
        return False, "⚠️ 决策未记录", [str(e)[:200]]
    lines = [
        f"建议 `{sid}` → **{_VERDICT_CN.get(verdict, verdict)}**"
        + (f" · 理由: {reason}" if reason else ""),
        f"建议原文: {str(sug.get('text') or '')[:120]}",
        "已入回路 (管理台「战略 OS · 决策记录」可查) · 修正 = 再发一条新决策 (追加不改史)。",
        "> AI 是参谋, 判断与担责在人 · 拒绝率永不作为个人绩效口径。",
    ]
    # M2: 采纳/部分采纳 → 自动生成行动 (owner=决策人, 期限 T+30)。已有同名行动则不重建
    # (修正决策不清空 check-in 历史)。生成失败不影响决策记录 (决策为主, 台账为辅)。
    if verdict in ("adopt", "partial"):
        try:
            a_line = _ensure_action(strategy_root, rec, now=rec["ts"])
            lines.insert(2, a_line)
        except Exception:  # noqa: BLE001
            lines.insert(2, "⚠ 行动台账生成失败 (决策已记录; 可稍后补)。")
    return True, "✅ 决策已记录", lines


def _ensure_action(strategy_root: Path, decision: dict, *, now: int,
                   due_days: int = 30) -> str:
    """采纳决策 → 行动台账条目 (id=建议 ID, 一建议一行动)。已存在 → 保留原样。"""
    sid = decision["suggestion_id"]
    existing = {a.get("id") for a in read_actions(strategy_root)}
    if sid in existing:
        return f"⏭ 行动 `{sid}` 已在台账 (沿用原 check-in 历史)。"
    due = (_dt.datetime.fromtimestamp(now) + _dt.timedelta(days=due_days)).strftime("%Y-%m-%d")
    upsert_action(strategy_root, {
        "id": sid,
        "origin": {"suggestion_id": sid},
        "scene": decision.get("scene") or "—",
        "title": str(decision.get("suggestion_text") or sid)[:80],
        "owner": decision.get("decider") or "—",
        "due": due,
        "created_ts": now,
        "checkins": [],
        "status": "ongoing",
    })
    return (f"⏭ 已生成行动 `{sid}` 进行动台账 (期限 {due} · owner=决策人)。"
            f"T+7/T+30 会跟催; 随时回复 `进展 {sid} 完成/进行中/受阻/放弃 [说明]` 更新。")


# ── check-in 回收 (「进展 <sid> 完成 [说明]」) ──

_STATUS_WORDS = {
    "完成": "done", "已完成": "done", "done": "done",
    "进行中": "ongoing", "推进中": "ongoing",
    "受阻": "blocked", "卡住": "blocked",
    "放弃": "dropped", "取消": "dropped",
}
_STATUS_CN = {"done": "已完成", "ongoing": "进行中", "blocked": "受阻", "dropped": "已放弃"}

_CHECKIN_RE = re.compile(
    r"^\s*进展\s+(?P<sid>\S+)\s+(?P<word>已完成|完成|done|进行中|推进中|受阻|卡住|放弃|取消)"
    r"\s*[,,·]?\s*(?P<note>.*)$", re.S)

CHECKIN_USAGE_LINES = [
    "用法 (直接回复):",
    "`进展 <行动ID> 完成` / `进展 <行动ID> 受阻 等模版组排期`",
    "状态可选: **完成 / 进行中 / 受阻 / 放弃**, 后面可带一句说明。",
    "行动 ID = 被采纳建议的 ID (跟催消息里有)。",
]


def parse_checkin_command(text: str) -> Optional[dict]:
    """文本 → {sid, status, note} 或 None。"""
    m = _CHECKIN_RE.match(text or "")
    if not m:
        return None
    return {"sid": m.group("sid").strip().strip("`"),
            "status": _STATUS_WORDS[m.group("word")],
            "note": m.group("note").strip()}


def capture_checkin(strategy_root: Path, *, sid: str, status: str, note: str,
                    by: str, now: Optional[float] = None) -> tuple[bool, str, list[str]]:
    """行动 check-in 回收 → (ok, 卡片标题, 卡片行)。checkins 只追加。"""
    ts = int(now if now is not None else time.time())
    try:
        append_checkin(strategy_root, sid, {"ts": ts, "status": status,
                                            "note": note, "by": by})
    except FileNotFoundError:
        avail = [a["id"] for a in read_actions(strategy_root)
                 if a.get("status") in ("ongoing", "blocked")][:6]
        return False, "❓ 未找到该行动", [
            f"`{sid}` 不在台账。进行中的行动:",
            " · ".join(f"`{a}`" for a in avail) or "(无)", *CHECKIN_USAGE_LINES]
    except ValueError as e:
        return False, "⚠️ 进展未记录", [str(e)[:200], *CHECKIN_USAGE_LINES]
    lines = [
        f"行动 `{sid}` → **{_STATUS_CN.get(status, status)}**"
        + (f" · {note}" if note else ""),
        "已记入台账 (管理台「战略 OS · 行动台账」可查) · check-in 只追加, 历史全程可溯。",
    ]
    if status == "blocked":
        lines.append("受阻仅在管理台高亮, **不会自动上报** — 是否升级由人决定。")
    return True, "✅ 进展已记录", lines


def suggestion_card_lines(payload: Optional[dict], *, max_n: int = 3) -> list[str]:
    """评审完成卡片的「优先建议」区块 (feishu_notify 用)。payload 为空 → []。"""
    if not payload:
        return []
    pri = [s for s in payload.get("suggestions", []) if s.get("priority")][:max_n]
    if not pri:
        return []
    lines = ["🎯 **优先建议 · 人机共判** (AI 是参谋, 判断在人):"]
    for i, s in enumerate(pri, 1):
        txt = str(s.get("text") or "").strip()
        lines.append(f"{i}. {txt[:90]}{'…' if len(txt) > 90 else ''}")
    first = pri[0]["id"]
    lines.append(f"回复即记录决策: `决策 {first} 采纳` · `决策 {first} 不采纳 理由:…`"
                 f" (判定: 采纳/部分采纳/不采纳/校准)")
    return lines
