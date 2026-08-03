"""boss_core.loop — 战略 OS 回路数据底座 (M0 · prd-strategy-os)。

七节点组织回路的数据层: 建议 (suggestion) / 决策 (decision) / 行动 (action) /
指标 (metric)。纯逻辑 + 显式路径参数, 供 run_pipeline_local (Phase 5 落
suggestions.json)、M1 决策卡片回调、M2 台账 cron、管理台 collectors 共用。

纪律 (同 boss_core 总纲):
- 只依赖 stdlib (+ yaml 读 metrics), 绝不 import run_pipeline_local / dashboard_app。
- decisions 与 action checkins **append-only** (审计与校准数据的可信前提)。
- 数据契约 (denormalize scene/suggestion_text/title) 见
  docs/internal/strategy-os-frontend-design.md §3 — writer 必须遵守, 前端不做 join。
- Decision 永远是人 (PRD D6 红线): 本库只提供记录能力, 不含任何"自动决策"。
"""

from __future__ import annotations

from boss_core.loop.models import (
    ACTION_STATUSES,
    VERDICTS,
    validate_action,
    validate_decision,
)
from boss_core.loop.store import (
    append_checkin,
    append_decision,
    read_actions,
    read_decisions,
    read_metrics,
    read_suggestions,
    suggestions_path,
    upsert_action,
    write_suggestions,
)
from boss_core.loop.metrics import (
    metrics_context_block,
    metrics_for_scene,
    validate_metric,
)
from boss_core.loop.suggestions import extract_suggestions, parse_suggestion_items
from boss_core.loop.capture import (
    capture_checkin,
    capture_decision,
    parse_checkin_command,
    parse_decision_command,
    suggestion_card_lines,
)

__all__ = [
    "ACTION_STATUSES", "VERDICTS", "validate_action", "validate_decision",
    "append_checkin", "append_decision", "read_actions", "read_decisions",
    "read_metrics", "read_suggestions", "suggestions_path", "upsert_action",
    "write_suggestions", "extract_suggestions", "parse_suggestion_items",
    "capture_decision", "parse_decision_command", "suggestion_card_lines",
    "capture_checkin", "parse_checkin_command",
    "metrics_context_block", "metrics_for_scene", "validate_metric",
]
