"""boss_core.constants — brief section 抽取用 heading 候选常量 (M0.1c, 从 run_pipeline_local 纯搬移)。

纯数据 (无依赖)。prior 5 case 用前置候选, Case 6 + 未来范式用后置候选;
prose.py 的各段抽取器按序 prefix-match 命中第一个 heading。
run_pipeline_local 顶部 re-export 这些名字, 调用方 (含仍留在 rpl 的
_extract_attribution_prose) 零改动。
"""

from __future__ import annotations

# Heading 候选 (prior 5 case 用前者, Case 6 + 未来用后者)
_DECISION_HEADINGS = ["### 结论", "### 主 Decision", "### Decision"]
_CONSENSUS_HEADINGS = ["### 共识", "### 6 dim 收敛点", "### 主共识"]
_EVIDENCE_HEADINGS = [
    # Case 6 范式: 引用 evidence + 6 评委核心金句
    "### 引用 evidence", "### 6 评委核心金句", "### 评委核心金句",
    # prior 5 案范式 fallback: Phase 1 Background from Wiki / raw 列了源
    "### Background from Wiki / raw (双源)", "### Background from Wiki + raw (双源)",
    "### Background from Wiki / raw (单向引用)",
    "### Background from Wiki / raw", "### Background from Wiki", "### 关键 evidence",
]
_COUNTER_HEADINGS = [
    # prose-friendly heading 先, 表格-heavy heading 后
    "### 锚点视角的反方启示", "### 关键 reasoning jumps",
    "### 矛盾", "### 4 跨维度矛盾", "### 锚点视角",
]
_ATTRIBUTION_HEADINGS = [
    "### Attribution Checkpoints", "## Phase 6 — Attribution",
    "### 30/90/365", "### Attribution",
]
