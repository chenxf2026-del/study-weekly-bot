"""boss_core.loop.models — 回路对象的取值域与校验 (纯数据, 无依赖)。

对象 shape (dict, 不上 dataclass 仪式; 字段契约见 strategy-os-frontend-design.md §3):

decision  = {suggestion_id, review_brand, scene*, suggestion_text*, decider,
             verdict ∈ VERDICTS, reason (reject/partial/calibrate 必填), ts: epoch}
             * = denormalize 必带 (前端不做跨文件 join)
action    = {id, origin: {suggestion_id}|{milestone_id}, scene*, title*, owner,
             due: ISO 日期, checkins: [{ts, status ∈ ACTION_STATUSES, note}],
             status ∈ ACTION_STATUSES}
suggestion (suggestions.json 内条目) = {id, judge, text, priority: bool}
"""

from __future__ import annotations

VERDICTS = ("adopt", "partial", "reject", "calibrate")
# 拒绝/部分/校准是"人对 AI 的取舍", 理由即知识 — 必填 (PRD §5.2)
_REASON_REQUIRED = ("partial", "reject", "calibrate")
ACTION_STATUSES = ("ongoing", "done", "blocked", "dropped")

_DECISION_REQUIRED = ("suggestion_id", "review_brand", "scene", "suggestion_text",
                      "decider", "verdict", "ts")
_ACTION_REQUIRED = ("id", "origin", "scene", "title", "owner", "due", "status")


def validate_decision(rec: dict) -> list[str]:
    """决策记录校验 → 问题列表 (空 = 合法)。写入前调用, fail-close。"""
    errs = [f"缺字段 {k}" for k in _DECISION_REQUIRED if not rec.get(k)]
    v = rec.get("verdict")
    if v and v not in VERDICTS:
        errs.append(f"verdict 非法: {v} (须 ∈ {VERDICTS})")
    if v in _REASON_REQUIRED and not str(rec.get("reason") or "").strip():
        errs.append(f"verdict={v} 必须带 reason (人对 AI 的取舍理由即知识)")
    return errs


def validate_action(rec: dict) -> list[str]:
    """行动记录校验 → 问题列表 (空 = 合法)。"""
    errs = [f"缺字段 {k}" for k in _ACTION_REQUIRED if not rec.get(k)]
    st = rec.get("status")
    if st and st not in ACTION_STATUSES:
        errs.append(f"status 非法: {st} (须 ∈ {ACTION_STATUSES})")
    for i, c in enumerate(rec.get("checkins") or []):
        if not isinstance(c, dict) or c.get("status") not in ACTION_STATUSES:
            errs.append(f"checkins[{i}] 非法 (须 dict 且 status ∈ {ACTION_STATUSES})")
    return errs
