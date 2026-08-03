"""
classify_failure.py — Failure Card 6 类自动分类器

实现 skills/meta/failure-card-classifier/SKILL.md 的决策树, 输入 case.json 数据
+ 可选 contrary signal 描述, 返回 (type, confidence, reason, alternatives)。

6 类 (CLAUDE.md §7):
  fact_failure       输入事实有误
  framework_failure  Reasoning 框架错位 (跳步未标 / steps 缺)
  process_failure    流程执行不对 (Phase 跳了 / 缺 Wiki Context)
  scope_failure     议题错误归类 (panel 不匹配)
  validation_failure attribution metric 设计有问题 (不可观测)
  handover_failure   决策落地的 owner/deadline 失效

无明显信号 → 'unclassified', 升级 项目主理 / LLM 兜底。

用法 (Python):
    from classify_failure import classify
    result = classify(case_dict, contrary_signal="...", topic_type="strategic")
    # result.type, result.confidence, result.reason, result.alternatives

用法 (CLI):
    python classify_failure.py path/to/case.json [--contrary "..."] [--topic strategic]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


FAILURE_TYPES = (
    "fact_failure",
    "framework_failure",
    "process_failure",
    "scope_failure",
    "validation_failure",
    "handover_failure",
)


@dataclass
class Classification:
    type: str
    confidence: float
    reason: str
    alternatives: list[str] = field(default_factory=list)


# ─── 与 panels/default.yaml auto_judge_selection.rules 同步 ───
# Phase 1 PRD 选定的议题类型 → 期望 dimension 评委集合 (anchor tian 永远在, 不计入)
EXPECTED_PANELS: dict[str, list[str]] = {
    "strategic":       ["industry-trend", "strategic-vision", "financial-strategy", "org-strategy"],
    "product":         ["industry-trend", "product-strategy", "customer-strategy", "financial-strategy"],
    "organizational":  ["org-strategy", "strategic-vision", "financial-strategy", "product-strategy"],
    "customer":        ["customer-strategy", "product-strategy", "financial-strategy", "industry-trend"],
    "financial":       ["financial-strategy", "customer-strategy", "industry-trend", "strategic-vision"],
    "brand":           ["customer-strategy", "strategic-vision", "industry-trend", "product-strategy"],
}


# falsification_metric 的"不可观测"信号词 → validation_failure
# 这些词表达主观/模糊状态, 不可被另一人机械验证
ABSTRACT_METRIC_WORDS = [
    "客户满意度", "市场反响", "整体趋势", "用户感受",
    "可能", "或许", "大约", "差不多", "应该会",
    "satisfaction", "sentiment", "approximately", "around",
]


# Decision owner 的"部门"模式 → handover_failure
# CLAUDE.md §11.2: owner 必须是具体人名, 不能写部门
DEPARTMENT_PATTERN = re.compile(r"(部$|组$|队$|团队$|department$|team$|group$|事业部)")


# ─── 单项检查 helper ───

def _is_evidence_stale(evidences: list[dict], days: int = 365) -> tuple[bool, str]:
    """是否有 verified_at 超过 N 天的证据 → fact_failure 信号"""
    if not evidences:
        return False, ""
    threshold = datetime.now() - timedelta(days=days)
    for i, e in enumerate(evidences):
        v = e.get("verified_at")
        if not v:
            continue
        try:
            d = datetime.fromisoformat(v)
            if d < threshold:
                return True, f"evidence[{i}] verified_at={v} ({(datetime.now() - d).days}d ago, > {days}d threshold)"
        except (ValueError, TypeError):
            continue
    return False, ""


def _missing_jumps_marker(reasoning_trace: dict) -> bool:
    """reasoning_trace.jumps 为空但实际可能有跳步 → framework_failure 信号 (一)"""
    jumps = reasoning_trace.get("jumps")
    return jumps is None or jumps == []


def _step_count(reasoning_trace: dict) -> int:
    return len(reasoning_trace.get("steps", []))


def _has_wiki_background(context: dict) -> bool:
    """context.background_from_wiki 非空 → Phase 1 调了 sage-wiki query"""
    bg = context.get("background_from_wiki")
    return bool(bg) and len(bg) > 0


def _panel_matches_topic(judges: list[str], topic_type: str) -> tuple[bool, list[str], list[str]]:
    """评委名单是否匹配议题类型. 返回 (matches, mismatched_judges, missing_judges)"""
    expected = EXPECTED_PANELS.get(topic_type)
    if expected is None:
        return True, [], []  # unknown topic, don't judge
    dim_judges = [j for j in judges if j != "tian"]
    expected_set = set(expected)
    actual_set = set(dim_judges)
    overlap = expected_set & actual_set
    # 至少一半 expected dim 在 panel 里才算 ok
    matches = len(overlap) >= len(expected) / 2
    mismatched = list(actual_set - expected_set)
    missing = list(expected_set - actual_set)
    return matches, mismatched, missing


def _has_abstract_metric(checkpoints: list[dict]) -> tuple[bool, str]:
    """checkpoint.falsification_metric 含主观/模糊词 → validation_failure"""
    for cp in checkpoints:
        metric = cp.get("falsification_metric", "")
        for word in ABSTRACT_METRIC_WORDS:
            if word in metric:
                horizon = cp.get("horizon_days", "?")
                return True, f"checkpoint h{horizon}d: '{metric}' contains abstract word '{word}'"
    return False, ""


def _owner_is_department(decision: dict) -> tuple[bool, str]:
    """decision.actions[].owner 是部门名而非个人 → handover_failure"""
    actions = decision.get("actions", [])
    for i, a in enumerate(actions):
        owner = a.get("owner", "")
        if DEPARTMENT_PATTERN.search(owner):
            return True, f"action[{i}] owner='{owner}' looks like a department/team, not a person"
    return False, ""


def _machine_verifiable_done_is_vague(decision: dict) -> tuple[bool, str]:
    """machine_verifiable_done 抽象 → handover_failure (二)"""
    actions = decision.get("actions", [])
    for i, a in enumerate(actions):
        mvd = a.get("machine_verifiable_done", "")
        for word in ABSTRACT_METRIC_WORDS:
            if word in mvd:
                return True, f"action[{i}] machine_verifiable_done='{mvd}' too vague ('{word}')"
    return False, ""


def _contrary_indicates(signal: str, keywords: list[str]) -> bool:
    """contrary_signal 是否包含某类关键词"""
    if not signal:
        return False
    return any(k in signal for k in keywords)


# ─── 主决策树 ───

def classify(
    case: dict[str, Any],
    contrary_signal: str | None = None,
    topic_type: str | None = None,
) -> Classification:
    """
    对一个 case 进行 6 类分类。

    决策顺序与 SKILL.md 一致 — 先查 fact, 再 framework, 再 process, 再 scope,
    再 validation, 最后 handover。每条规则成立即 short-circuit 返回。

    contrary_signal: attribution-checker 给出的反向信号文本 (可选)
    topic_type: Phase 1 PRD 标定的议题类型 (strategic / product / 等)
    """

    # ── Rule 1: fact_failure ────────────────────────────────
    stale, stale_msg = _is_evidence_stale(case.get("evidences", []))
    contrary_fact = _contrary_indicates(
        contrary_signal or "",
        ["事实错", "数据过期", "来源造假", "fact wrong", "outdated", "stale"],
    )
    if stale and contrary_fact:
        return Classification(
            type="fact_failure", confidence=0.9,
            reason=f"{stale_msg}; contrary signal confirms fact error",
        )
    if stale:
        return Classification(
            type="fact_failure", confidence=0.75,
            reason=stale_msg,
            alternatives=["validation_failure"],  # 可能 metric 也滞后
        )
    if contrary_fact:
        return Classification(
            type="fact_failure", confidence=0.65,
            reason=f"contrary signal indicates fact error: '{contrary_signal}'",
        )

    # ── Rule 2: framework_failure ───────────────────────────
    rt = case.get("reasoning_trace", {})
    jumps_missing = _missing_jumps_marker(rt)
    steps_thin = _step_count(rt) < 3
    contrary_framework = _contrary_indicates(
        contrary_signal or "",
        ["框架", "跳步", "推理", "missing step", "framework", "reasoning"],
    )
    if (jumps_missing or steps_thin) and contrary_framework:
        reason_parts = []
        if jumps_missing:
            reason_parts.append("reasoning_trace.jumps empty")
        if steps_thin:
            reason_parts.append(f"reasoning_trace.steps thin (n={_step_count(rt)})")
        reason_parts.append("contrary signal points to framework issue")
        return Classification(
            type="framework_failure", confidence=0.8,
            reason="; ".join(reason_parts),
        )
    if jumps_missing and steps_thin:
        # 即便没有 contrary signal, 同时缺 jumps + steps 也是明确 framework 问题
        return Classification(
            type="framework_failure", confidence=0.65,
            reason=f"reasoning_trace.jumps empty AND steps thin (n={_step_count(rt)})",
        )

    # ── Rule 3: process_failure ─────────────────────────────
    no_wiki = not _has_wiki_background(case.get("context", {}))
    if no_wiki:
        return Classification(
            type="process_failure", confidence=0.75,
            reason="context.background_from_wiki missing (Phase 1 skipped sage-wiki query, CLAUDE.md §4.2)",
        )

    # ── Rule 4: scope_failure ───────────────────────────────
    judges = case.get("judges", [])
    if judges and topic_type:
        matches, mismatched, missing = _panel_matches_topic(judges, topic_type)
        if not matches:
            return Classification(
                type="scope_failure", confidence=0.7,
                reason=f"panel judges {sorted(judges)} mismatch topic '{topic_type}': mismatched={mismatched}, missing={missing}",
            )

    # ── Rule 5: validation_failure ──────────────────────────
    checkpoints = case.get("attribution", {}).get("checkpoints", [])
    abstract, abstract_msg = _has_abstract_metric(checkpoints)
    if abstract:
        return Classification(
            type="validation_failure", confidence=0.7,
            reason=abstract_msg,
        )

    # ── Rule 6: handover_failure ────────────────────────────
    dept, dept_msg = _owner_is_department(case.get("decision", {}))
    vague, vague_msg = _machine_verifiable_done_is_vague(case.get("decision", {}))
    if dept and vague:
        return Classification(
            type="handover_failure", confidence=0.85,
            reason=f"{dept_msg}; AND {vague_msg}",
        )
    if dept:
        return Classification(
            type="handover_failure", confidence=0.7,
            reason=dept_msg,
        )
    if vague:
        return Classification(
            type="handover_failure", confidence=0.6,
            reason=vague_msg,
            alternatives=["validation_failure"],
        )

    # ── unclassified ───────────────────────────────────────
    return Classification(
        type="unclassified", confidence=0.0,
        reason="no heuristic rule matched; escalate to LLM (Haiku) or 项目主理 manual review",
        alternatives=list(FAILURE_TYPES),
    )


def allocate_fc_id(failure_cards_dir: Path) -> str:
    """扫 failure_cards/ 取**当年**最大 FC-YYYY-NNNN 序号 +1 (v0.7 R12)。

    按年隔离序号 (PR #7 自审修复): 跨年取全局 max 会让 FC-2025-9999 之后的
    2026 年首张卡变成 FC-2026-10000, 溢出 4 位格式。
    """
    from datetime import datetime as _dt
    year = _dt.now().year
    max_n = 0
    if failure_cards_dir.is_dir():
        for f in failure_cards_dir.glob("FC-*.md"):
            m = re.match(r"FC-(\d{4})-(\d{4})\.md$", f.name)
            if m and int(m.group(1)) == year:
                max_n = max(max_n, int(m.group(2)))
    return f"FC-{year}-{max_n + 1:04d}"


def write_failure_card(
    case: dict[str, Any],
    classification: Classification,
    case_path: Path,
    failure_cards_dir: Path,
    detected_by: str = "manual",
    version_when_failed: int = 1,
) -> Path:
    """一体化入口 (v0.8 N8 backlog): 取号 + 渲染 + 写盘, 返回 fc_path。

    供三条路径复用: ① run_pipeline_local Phase 4 评委失败 (R12)
    ② Claude Code Skill 路径 (orchestrator Failure Mode #3) ③ attribution-checker 证伪触发。
    """
    fc_id = allocate_fc_id(failure_cards_dir)
    md = emit_failure_card_md(
        case, classification, case_path,
        fc_id=fc_id, version_when_failed=version_when_failed, detected_by=detected_by,
    )
    failure_cards_dir.mkdir(parents=True, exist_ok=True)
    fc_path = failure_cards_dir / f"{fc_id}.md"
    fc_path.write_text(md, encoding="utf-8")
    return fc_path


def emit_failure_card_md(
    case: dict[str, Any],
    classification: Classification,
    case_path: Path,
    template_path: Path | None = None,
    fc_id: str | None = None,
    version_when_failed: int = 1,
    detected_by: str = "attribution-checker | manual",
) -> str:
    """生成 Failure Card markdown (frontmatter + 6 段 body), 与 templates/failure-card-template.md 同构。

    fc_id 默认根据当前年 + case_id 后缀生成; 真生产应从 failure_cards/ 中扫最大序号 +1。
    """
    from datetime import datetime as _dt

    case_id = case.get("case_id", "C-YYYY-NNNN")
    brand_slug = case.get("brand_slug", "<brand>")
    detected_at = _dt.now().isoformat(timespec="seconds")
    fc_id = fc_id or f"FC-{_dt.now().year}-{case_id.split('-')[-1]}"

    alt_str = "[]" if not classification.alternatives else "\n  - " + "\n  - ".join(classification.alternatives)
    if classification.alternatives:
        alt_str = "\n  - " + "\n  - ".join(classification.alternatives)
        alt_block = f"alternatives:{alt_str}"
    else:
        alt_block = "alternatives: []"

    related_str = "\n  - ".join([
        f"cases/{case_id}/case.json",
        f"reports/{brand_slug}/versions/v{version_when_failed}_<date>.md",
    ])

    reason_safe = classification.reason.replace("'", "''")
    detected_by_safe = detected_by.replace("'", "''")  # 与 reason 同样防单引号破 yaml

    body = f"""---
fc_id: {fc_id}
type: {classification.type}
case_id: {case_id}
brand_slug: {brand_slug}
version_when_failed: {version_when_failed}
detected_at: '{detected_at}'
detected_by: '{detected_by_safe}'
classified_by: 'failure-card-classifier@v1'
classifier_confidence: {classification.confidence:.2f}
classifier_reason: '{reason_safe}'
{alt_block}
sensitivity: confidential
related_files:
  - {related_str}
status: open
fix_pr: null
fixed_at: null
---

# Failure Card {fc_id}

## 1. 失败摘要

(自动分类生成 · 项目主理 需补 2-4 句业务上下文)

case_id: `{case_id}` · brand: `{brand_slug}` · 版本: v{version_when_failed}

## 2. 失败模式分类

**类型**: `{classification.type}` (classifier_confidence: {classification.confidence:.2f})

**理由**: {classification.reason}

**备选**: {', '.join(classification.alternatives) if classification.alternatives else '(无, 决策树单一匹配)'}

## 3. 修复方向

(待 项目主理 填: 1-3 条可执行措施)

1. (TODO)

## 4. 影响范围

- 本判断 (v{version_when_failed}) 状态: 待 EVOLUTION 后置 superseded
- 同类判断回溯检查: TODO

## 5. 跟踪

- 修复 PR: N/A
- 验证方式: TODO
- 状态: `open`

## 6. 反思

(可选 · framework_failure / process_failure 必填)
"""
    return body


def main() -> int:
    ap = argparse.ArgumentParser(description="Failure Card 6 类自动分类")
    ap.add_argument("case_json", help="path to case.json")
    ap.add_argument("--contrary", help="contrary signal description (from attribution-checker)")
    ap.add_argument("--topic", choices=list(EXPECTED_PANELS.keys()),
                    help="议题类型 (Phase 1 PRD 标定)")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--emit-md", metavar="PATH",
                    help="把分类结果填进 templates/failure-card-template.md, 写到 PATH")
    ap.add_argument("--fc-id", help="覆盖默认生成的 fc_id")
    args = ap.parse_args()

    case_path = Path(args.case_json)
    if not case_path.exists():
        print(f"案件文件不存在: {case_path}", file=sys.stderr)
        return 2

    case = json.loads(case_path.read_text(encoding="utf-8"))
    result = classify(case, contrary_signal=args.contrary, topic_type=args.topic)

    if args.emit_md:
        md = emit_failure_card_md(case, result, case_path, fc_id=args.fc_id)
        out_path = Path(args.emit_md)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md, encoding="utf-8")
        print(f"emitted failure card: {out_path}")
        return 0

    if args.json:
        print(json.dumps({
            "type": result.type,
            "confidence": result.confidence,
            "reason": result.reason,
            "alternatives": result.alternatives,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"type:        {result.type}")
        print(f"confidence:  {result.confidence:.2f}")
        print(f"reason:      {result.reason}")
        if result.alternatives:
            print(f"alternatives: {', '.join(result.alternatives)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
