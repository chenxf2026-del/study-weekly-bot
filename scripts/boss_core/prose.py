"""boss_core.prose — report.md brief section prose 抽取器 (M0.1c, 从 run_pipeline_local 纯搬移)。

v3 brief section extractors · 直出 prose, 不出数字 / 评分。各段抽取器读 report.md,
grep 对应 heading 段, _strip_brief_noise 砍表格/dashboard/source 噪声后返回 prose。

无状态, 只依赖 stdlib + boss_core.docio (_extract_section_body) + boss_core.constants
(heading 候选)。run_pipeline_local 顶部 re-export 这些名字, 调用方与测试零改动。

注: _extract_attribution_prose 仍留在 run_pipeline_local (需 CASES_DIR / Logger / json,
本步不搬), 它经 rpl re-export 使用本模块的 _strip_brief_noise 与 constants 的
_ATTRIBUTION_HEADINGS。
"""

from __future__ import annotations

import re
from pathlib import Path

from boss_core.constants import (
    _CONSENSUS_HEADINGS,
    _COUNTER_HEADINGS,
    _DECISION_HEADINGS,
    _EVIDENCE_HEADINGS,
)
from boss_core.docio import _extract_section_body


def _strip_brief_noise(text: str) -> str:
    """从 grep 出的段去掉对 brief 读者无关的元数据噪声:
    - markdown 表格行 + 表格分隔行
    - dashboard 性 bullet (e.g. "- 维度加权均分 7.25", "- anchor_delta = -0.55")
    - source 引用尾巴 (Source: ... / raw §X.Y)
    """
    # 砍 dashboard bullet — 行首 "-" 后第一个词命中 dashboard 关键字
    dashboard_keywords = (
        "维度加权", "维度均分", "维度评委加权", "锚点心证", "anchor_delta",
        "anchor 心证", "anchor mean", "dim_weighted", "dim_mean",
        "counter_position 镜头", "panel_summary", "confidence_table",
        "镜头跨越", "镜头均分", "镜头平均", "Δ=", "Δ =",
    )
    out_lines = []
    for line in text.split("\n"):
        s = line.rstrip()
        # 跳 markdown 表格行
        if s.lstrip().startswith("|") and s.rstrip().endswith("|"):
            continue
        if re.match(r"^\s*\|?[\s\-:|]+\|?\s*$", s) and "-" in s:
            continue
        # 跳 dashboard bullet
        stripped = s.lstrip()
        if stripped.startswith(("- ", "* ")):
            after = stripped[2:].lstrip("*").strip()
            if any(kw in after for kw in dashboard_keywords):
                continue
        out_lines.append(s)
    cleaned = "\n".join(out_lines).strip()
    cleaned = re.sub(r"\(Source:\s*[^)]+\)", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _extract_conclusion_prose(report_path: Path) -> str:
    """提结论段 (`### 结论` / `### 主 Decision`). 保留首段 + 紧跟的"5 项必修"
    / "anchor_delta 解读" 等子段, 上限 800 字 prose, 砍 markdown 表格."""
    body = _extract_section_body(report_path, _DECISION_HEADINGS)
    if not body:
        return "_本案 Phase 5 未含 `### 结论` 或 `### 主 Decision` heading, 详见 [report.md](report.md)._"
    cleaned = _strip_brief_noise(body)
    if len(cleaned) > 800:
        cleaned = cleaned[:800] + "…"
    return cleaned


def _extract_reasoning_prose(report_path: Path) -> str:
    """提推论过程 — grep 共识段 (5 共识 / 6 dim 收敛点). 这是 6 评委共同识别的
    支撑链, 是结论的核心推论."""
    body = _extract_section_body(report_path, _CONSENSUS_HEADINGS)
    if not body:
        return "_本案 Phase 5 未含 `### 共识` heading, 详见 [report.md](report.md) 与 synthesis.md._"
    cleaned = _strip_brief_noise(body)
    if len(cleaned) > 1200:
        cleaned = cleaned[:1200] + "…"
    return cleaned


def _extract_evidence_prose(report_path: Path) -> str:
    """提论据 — grep 引用 evidence / 6 评委金句段. 这是结论与推论的硬支撑."""
    body = _extract_section_body(report_path, _EVIDENCE_HEADINGS)
    if not body:
        return "_本案 Phase 5 未含 `### 引用 evidence` 或 `### 6 评委核心金句` heading, 详见 [report.md](report.md) Phase 4 + synthesis.md §6._"
    cleaned = _strip_brief_noise(body)
    if len(cleaned) > 1500:
        cleaned = cleaned[:1500] + "…"
    return cleaned


def _extract_counter_prose(report_path: Path) -> str:
    """提反方与脆弱 — grep 矛盾 / 锚点视角的反方启示. 不是 adversarial_view
    列表, 是 Lead 写好的 prose 反方分析.

    候选 heading 按 _COUNTER_HEADINGS 顺序尝试. 关键: 若某 heading 命中但 body
    经 _strip_brief_noise (砍表格行) 后为空 (= report 该段是纯 markdown 表格,
    无 prose), 继续试下一候选 heading, 直到拿到非空 prose 或全部耗尽.
    """
    if not report_path.exists():
        return "_本案 report.md 不存在._"
    cleaned = ""
    for heading in _COUNTER_HEADINGS:
        body = _extract_section_body(report_path, [heading])
        if not body:
            continue
        cleaned = _strip_brief_noise(body)
        if cleaned:
            break
    if not cleaned:
        return "_本案 Phase 5 未含 `### 矛盾` 或 `### 锚点视角的反方启示` heading, 反方机制详见各评委 review 的 adversarial_view 字段._"
    if len(cleaned) > 800:
        cleaned = cleaned[:800] + "…"
    return cleaned
