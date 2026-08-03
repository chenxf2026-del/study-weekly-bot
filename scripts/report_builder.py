#!/usr/bin/env python3
"""Report write-side helpers for boss-vault.

`report_model.py` is the read-side contract. This module is the write-side
contract for Phase 5 outputs: canonical report, immutable version snapshot, and
the lightweight brief. It intentionally stays mostly pure so the pipeline can
assemble text before touching rolling files.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Any


# Finding 3: anchor 评委 slug `tian` 在人面 prose 显示为「锚点」(不外露真名身份暗示)。
# 结构字段不变: frontmatter judges: 列表 / [[reviews/tian]] 链接 / panel_summary.anchor_tian_mean
# 仍用 slug (review 文件查找与 schema 契约)。仅散文展示改。
_ANCHOR_SLUGS = frozenset({"tian"})


def _display_panel(panel_judges: list[str]) -> str:
    return ", ".join("锚点" if j in _ANCHOR_SLUGS else j for j in panel_judges)


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _yaml_quote_inline(value: str) -> str:
    """YAML single-quoted inline 标量, 单引号按规范双写转义。

    用于 f-string 拼装 frontmatter 时安全嵌入异常消息 / LLM 短文本 / 用户 topic
    等可能含 `'` `:` `(` 的内容。调用前应已 strip 掉换行 (single-quoted 不容多行)。
    """
    return "'" + value.replace("'", "''") + "'"


def format_panel_summary_dual_scale_yaml(panel_summary: dict, indent: str = "  ") -> str:
    if "anchor_dual_scale_delta" not in panel_summary:
        return ""
    lines = [
        f"{indent}anchor_tian_meta_mean: {panel_summary['anchor_tian_meta_mean']}",
        f"{indent}anchor_tian_single_point_mean: {panel_summary['anchor_tian_single_point_mean']}",
        f"{indent}anchor_dual_scale_delta: {panel_summary['anchor_dual_scale_delta']}",
    ]
    if "anchor_dual_scale_explanation" in panel_summary:
        exp = str(panel_summary["anchor_dual_scale_explanation"]).replace("'", "''")
        lines.append(f"{indent}anchor_dual_scale_explanation: '{exp}'")
    return "\n" + "\n".join(lines)


def _yaml_scalar(value: Any) -> str:
    """frontmatter 标量渲染: None→null, bool→小写, str→single-quoted, 数字原样。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return _yaml_quote_inline(str(value))


def format_panel_summary_yaml(panel_summary: dict, indent: str = "  ") -> str:
    """panel_summary 的 frontmatter inner YAML 块, 按 scoring_mode 分支。

    weighted_average (默认/历史): dimension_weighted_mean / anchor_tian_mean /
      anchor_delta / delta_high / delta_explanation (+ 可选 dual_scale)。
    sum_max_score (op2-company / workshop-midyear): total_max / dimension_total_mean /
      grade / anchor_total / lens_means / grade_explanation。

    单一源, 供 report.md + version snapshot 共用 (替代 4 处内联复制)。"""
    if panel_summary.get("scoring_mode") == "sum_max_score":
        lines = [
            f"{indent}scoring_mode: sum_max_score",
            f"{indent}total_max: {panel_summary['total_max']}",
            f"{indent}dimension_total_mean: {panel_summary['dimension_total_mean']}",
            f"{indent}grade: {_yaml_scalar(panel_summary.get('grade'))}",
        ]
        # 锚点心证 5 镜头均分 (1-10, 独立尺子); 竞赛无锚点则省略
        if panel_summary.get("anchor_5lens_mean") is not None:
            lines.append(f"{indent}anchor_5lens_mean: {panel_summary['anchor_5lens_mean']}")
        lines.append(f"{indent}grade_explanation: {_yaml_quote_inline(str(panel_summary.get('grade_explanation', '')))}")
        lens_means = panel_summary.get("lens_means") or {}
        if lens_means:
            lines.append(f"{indent}lens_means:")
            for slug, val in lens_means.items():
                lines.append(f"{indent}  {slug}: {val}")
        no_score = panel_summary.get("judges_no_score") or []
        if no_score:
            lines.append(f"{indent}judges_no_score:  # 未出分, 未计入维度总分")
            for j in no_score:
                lines.append(f"{indent}  - {j}")
        return "\n".join(lines)

    # weighted_average (历史路径)
    dual_scale_yaml = format_panel_summary_dual_scale_yaml(panel_summary, indent)
    no_score_yaml = ""
    no_score = panel_summary.get("judges_no_score") or []
    if no_score:
        no_score_yaml = (f"\n{indent}judges_no_score:  # 未出分, 未计入维度加权均分\n"
                         + "\n".join(f"{indent}  - {j}" for j in no_score))
    return (
        f"{indent}dimension_weighted_mean: {panel_summary['dimension_weighted_mean']}\n"
        f"{indent}anchor_tian_mean: {panel_summary['anchor_tian_mean']}\n"
        f"{indent}anchor_delta: {panel_summary['anchor_delta']}\n"
        f"{indent}delta_high: {str(panel_summary['delta_high']).lower()}\n"
        f"{indent}delta_explanation: {_yaml_quote_inline(str(panel_summary['delta_explanation']))}"
        f"{dual_scale_yaml}"
        f"{no_score_yaml}"
    )


def format_competition_frontmatter_yaml(competition_meta: dict | None) -> str:
    """竞赛场景 frontmatter: scene_slug / project_name / team + competition_summary 块。

    供 build_workshop_ranking.py 的输入契约 (load_entries 按 scene_slug 过滤,
    _entry_from_report 读 project_name/team/competition_summary)。无 meta → ""。"""
    if not competition_meta:
        return ""
    cs = competition_meta.get("competition_summary") or {}
    dim_scores = cs.get("dimension_scores") or {}
    lines = [
        f"scene_slug: {_yaml_scalar(competition_meta.get('scene_slug'))}",
        f"project_name: {_yaml_scalar(competition_meta.get('project_name'))}",
        f"team: {_yaml_scalar(competition_meta.get('team'))}",
        "competition_summary:",
        f"  total_score: {_yaml_scalar(cs.get('total_score'))}",
        "  dimension_scores:",
    ]
    for slug, val in dim_scores.items():
        lines.append(f"    {slug}: {val}")
    if not dim_scores:
        lines[-1] = "  dimension_scores: {}"
    return "\n".join(lines) + "\n"


def format_generation_yaml(generation: dict | None, indent: str = "  ") -> str:
    """生成元数据 frontmatter 块: 调用的 provider / 模型 / 端点, 便于跨报告做质量比较与归因。

    返回以换行收尾的块 (无则 "")。Phase 0-3 用 model_fast (解析/合成), Phase 4-5 用 model_deep
    (评委独立打分 + Lead 合议) — 固定映射写进 phase_models 便于复盘。"""
    if not generation:
        return ""
    lines = ["generation:"]
    for k in ("provider", "model_fast", "model_deep"):
        lines.append(f"{indent}{k}: {_yaml_scalar(generation.get(k))}")
    base_url = generation.get("base_url") or "default"
    lines.append(f"{indent}base_url: {_yaml_scalar(base_url)}")
    lines.append(f"{indent}phase_models: 'Phase0-3=model_fast · Phase4-5(评委+合议)=model_deep'")
    # profile 提速: 每 phase 的 LLM 墙钟秒 (串行 phase 即墙钟; phase_4 评委并行为累加)。看哪个 phase 最吃时间。
    pls = generation.get("phase_llm_seconds")
    if isinstance(pls, dict) and pls:
        rendered = "{" + ", ".join(f"{k}: {v}" for k, v in sorted(pls.items())) + "}"
        lines.append(f"{indent}phase_llm_seconds: {rendered}")
    # R8 (failover): 本 job 发生过跨端点故障切换时打标 — 混模型打分归因可见 (llm-failover PRD §5.4)
    if generation.get("failover"):
        lines.append(f"{indent}llm_failover: true")
        models_used = generation.get("models_used") or []
        rendered = "[" + ", ".join(_yaml_scalar(m) for m in models_used) + "]"
        lines.append(f"{indent}models_used: {rendered}")
    return "\n".join(lines) + "\n"


def format_panel_summary_oneline(panel_summary: dict) -> str:
    """§B 评委评议段头的一句话分数摘要, 按 scoring_mode 分支。"""
    if panel_summary.get("scoring_mode") == "sum_max_score":
        s = (f"维度总分 = {panel_summary['dimension_total_mean']}/{panel_summary['total_max']}")
        if panel_summary.get("grade"):
            s += f" · 等级 = {panel_summary['grade']}"
        if panel_summary.get("anchor_5lens_mean") is not None:
            s += f" · 锚点心证 = {panel_summary['anchor_5lens_mean']}/10 (5 镜头)"
        return s
    return (f"dim_weighted_mean = {panel_summary['dimension_weighted_mean']} · "
            f"anchor = {panel_summary['anchor_tian_mean']} · "
            f"anchor_delta = {panel_summary['anchor_delta']}")


def assemble_report_md(
    brand_slug: str,
    topic: str,
    panel_name: str,
    case_id: str,
    panel_judges: list[str],
    version: int,
    panel_summary: dict,
    body_prose: str,
    review_mode_data: Optional[dict] = None,
    generation: Optional[dict] = None,
) -> str:
    judges_yaml = "\n".join(f"  - {j}" for j in panel_judges)
    review_links = "\n".join(f"- [[reviews/{j}]]" for j in panel_judges)
    ps_yaml = format_panel_summary_yaml(panel_summary)
    ps_oneline = format_panel_summary_oneline(panel_summary)
    generation_yaml = format_generation_yaml(generation)
    n_judges = len(panel_judges)

    if review_mode_data:
        doc_title = review_mode_data.get("doc_title", "<unknown>")
        doc_path = review_mode_data.get("doc_path", "<unknown>")
        doc_summary = review_mode_data.get("doc_summary", "")
        claims = review_mode_data.get("claims", [])
        decisions = review_mode_data.get("decisions", [])
        revision_suggestions_block = review_mode_data.get("revision_suggestions_block", "(待 _compile_revision_suggestions 填充)")
        competition_yaml = format_competition_frontmatter_yaml(review_mode_data.get("competition_meta"))
        claims_md = "\n".join([f"- {c}" for c in claims])
        decisions_md = "\n".join([
            f"- **action**: {d.get('action', 'TBD')}"
            + (f" · owner: {d['owner']}" if d.get("owner") else "")
            + (f" · deadline: {d['deadline']}" if d.get("deadline") else "")
            for d in decisions
        ])
        return f"""---
brand_slug: {brand_slug}
case_id: {case_id}
version: {version}
mode: REVIEW
review_doc_path: {doc_path}
created_at: '{now_iso()}'
panel: {panel_name}
{generation_yaml}judges:
{judges_yaml}
panel_summary:
{ps_yaml}
{competition_yaml}sensitivity: confidential
---

# REVIEW · {doc_title} · v{version}

> ADR-007 REVIEW mode · 评议方案 doc, 不是研究 open question
> 被评议: `{doc_path}` · 议题 (从 doc 推): {topic}

---

## §A · 原方案摘要 (Phase 0 doc parser)

{doc_summary}

### 核心 claims (从 doc 提)
{claims_md}

### Decisions (doc 提议)
{decisions_md}

---

## §B · {n_judges} 评委评议

> panel = {_display_panel(panel_judges)}
> {ps_oneline}

{body_prose}

### 评委明细
{review_links}

---

## §C · 修订建议清单

> ADR-007 REVIEW 独有 deliverable · {n_judges} 评委 "如果是我会改的 3 点" 聚合

{revision_suggestions_block}

---

## 30/90/365 attribution

> 跟踪 §C 修订建议是否落实 (REVIEW 推荐 vs 实际执行差距)
> 详见 `cases/{case_id}/case.json` attribution 段
"""

    return f"""---
brand_slug: {brand_slug}
case_id: {case_id}
version: {version}
created_at: '{now_iso()}'
panel: {panel_name}
{generation_yaml}judges:
{judges_yaml}
panel_summary:
{ps_yaml}
sensitivity: confidential
---

# Report · {brand_slug} · v{version}

## Phase 1 — Context

议题: {topic or '(EVOLUTION 重判, 见上版)'}

### Background from Wiki

> CLAUDE.md §3.1 单向引用. 链接为快照引用, Wiki 内容由 sage-wiki 自动更新。
> 本判断书冻结于触发日, 如 Wiki 后续修订, 以版本快照为准。

- 公司档案: [[_wiki/entities/{brand_slug}]]
- 关键人物: [[_wiki/people/anchor]] (待 sage-wiki 编译真 entity 名)

{body_prose}

## 评委 reviews

{review_links}
"""


def assemble_version_snapshot(
    brand_slug: str,
    topic: str,
    case_id: str,
    version: int,
    panel_summary: dict,
    body_prose: str,
    generation: Optional[dict] = None,
) -> str:
    ps_yaml = format_panel_summary_yaml(panel_summary)
    generation_yaml = format_generation_yaml(generation)
    return f"""---
brand_slug: {brand_slug}
case_id: {case_id}
version: {version}
frozen_at: '{now_iso()}'
immutable: true
{generation_yaml}panel_summary:
{ps_yaml}
---

# Frozen Snapshot · {brand_slug} · v{version}

⚠️ 此快照永久不可变 (CLAUDE.md §2.2). EVOLUTION 模式应写 v{version + 1}, 不修改本版。

议题: {topic or '(EVOLUTION 重判)'}

panel_summary:
{ps_yaml}

{body_prose}
"""


DECISION_HEADINGS = ["### 结论", "### 主 Decision", "### Decision"]
CONSENSUS_HEADINGS = ["### 共识", "### 6 dim 收敛点", "### 主共识"]
EVIDENCE_HEADINGS = [
    "### 引用 evidence", "### 6 评委核心金句", "### 评委核心金句",
    "### Background from Wiki / raw (双源)", "### Background from Wiki + raw (双源)",
    "### Background from Wiki / raw (单向引用)",
    "### Background from Wiki / raw", "### Background from Wiki", "### 关键 evidence",
]
COUNTER_HEADINGS = [
    "### 锚点视角的反方启示", "### 关键 reasoning jumps",
    "### 矛盾", "### 4 跨维度矛盾", "### 锚点视角",
]
ATTRIBUTION_HEADINGS = [
    "### Attribution Checkpoints", "## Phase 6 — Attribution",
    "### 30/90/365", "### Attribution",
]


def extract_section_body(report_path: Path, heading_candidates: list[str]) -> Optional[str]:
    if not report_path.exists():
        return None
    try:
        text = report_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for heading in heading_candidates:
        pattern = re.compile(rf"^{re.escape(heading)}[ \t]*[^\n]*$", re.MULTILINE)
        m = pattern.search(text)
        if not m:
            continue
        start = m.end()
        rest = text[start:]
        next_m = re.search(r"^#{2,3}[ \t]+", rest, re.MULTILINE)
        body = rest[:next_m.start()] if next_m else rest
        return body.strip()
    return None


def strip_brief_noise(text: str) -> str:
    dashboard_keywords = (
        "维度加权", "维度均分", "维度评委加权", "锚点心证", "anchor_delta",
        "anchor 心证", "anchor mean", "dim_weighted", "dim_mean",
        "counter_position 镜头", "panel_summary", "confidence_table",
        "镜头跨越", "镜头均分", "镜头平均", "Δ=", "Δ =",
    )
    out_lines = []
    for line in text.split("\n"):
        s = line.rstrip()
        if s.lstrip().startswith("|") and s.rstrip().endswith("|"):
            continue
        if re.match(r"^\s*\|?[\s\-:|]+\|?\s*$", s) and "-" in s:
            continue
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


def extract_conclusion_prose(report_path: Path) -> str:
    body = extract_section_body(report_path, DECISION_HEADINGS)
    if not body:
        return "_本案 Phase 5 未含 `### 结论` 或 `### 主 Decision` heading, 详见 [report.md](report.md)._"
    cleaned = strip_brief_noise(body)
    return cleaned[:800] + "…" if len(cleaned) > 800 else cleaned


def extract_reasoning_prose(report_path: Path) -> str:
    body = extract_section_body(report_path, CONSENSUS_HEADINGS)
    if not body:
        return "_本案 Phase 5 未含 `### 共识` heading, 详见 [report.md](report.md) 与 synthesis.md._"
    cleaned = strip_brief_noise(body)
    return cleaned[:1200] + "…" if len(cleaned) > 1200 else cleaned


def extract_evidence_prose(report_path: Path) -> str:
    body = extract_section_body(report_path, EVIDENCE_HEADINGS)
    if not body:
        return "_本案 Phase 5 未含 `### 引用 evidence` 或 `### 6 评委核心金句` heading, 详见 [report.md](report.md) Phase 4 + synthesis.md §6._"
    cleaned = strip_brief_noise(body)
    return cleaned[:1500] + "…" if len(cleaned) > 1500 else cleaned


def extract_counter_prose(report_path: Path) -> str:
    if not report_path.exists():
        return "_本案 report.md 不存在._"
    cleaned = ""
    for heading in COUNTER_HEADINGS:
        body = extract_section_body(report_path, [heading])
        if not body:
            continue
        cleaned = strip_brief_noise(body)
        if cleaned:
            break
    if not cleaned:
        return "_本案 Phase 5 未含 `### 矛盾` 或 `### 锚点视角的反方启示` heading, 反方机制详见各评委 review 的 adversarial_view 字段._"
    return cleaned[:800] + "…" if len(cleaned) > 800 else cleaned


def extract_attribution_prose(case_id: str, cases_dir: Path, report_path: Optional[Path] = None) -> str:
    case_json_path = cases_dir / case_id / "case.json"
    if case_json_path.exists():
        try:
            cdata = json.loads(case_json_path.read_text(encoding="utf-8"))
            cps = ((cdata.get("attribution") or {}).get("checkpoints") or [])
            lines: list[str] = []
            for c in cps:
                if not isinstance(c, dict):
                    continue
                h = c.get("horizon_days", "?")
                when = c.get("check_at", "?")
                metric = (c.get("falsification_metric") or "").strip()
                metric_short = metric.split("\n")[0][:200]
                if len(metric) > 200:
                    metric_short += "…"
                lines.append(f"- **{h} 天 ({when})**: {metric_short}")
            if lines:
                return "\n".join(lines)
        except (json.JSONDecodeError, OSError):
            pass
    if report_path is not None and report_path.exists():
        body = extract_section_body(report_path, ATTRIBUTION_HEADINGS)
        if body:
            cleaned = strip_brief_noise(body)
            return cleaned[:1200] + "…" if len(cleaned) > 1200 else cleaned
    return "_本案 case.json 与 report.md 均未含 attribution checkpoint, 详见 [report.md](report.md)._"


def estimate_lines(path: Path) -> str:
    try:
        return str(len(path.read_text(encoding="utf-8").splitlines()))
    except (OSError, UnicodeDecodeError):
        return "?"


def assemble_report_brief(
    brand_slug: str,
    topic: str,
    panel_name: str,
    case_id: str,
    version: int,
    reports_dir: Path,
    cases_dir: Path,
) -> str:
    report_path = reports_dir / brand_slug / "report.md"
    conclusion = extract_conclusion_prose(report_path)
    reasoning = extract_reasoning_prose(report_path)
    evidence = extract_evidence_prose(report_path)
    counter = extract_counter_prose(report_path)
    attribution = extract_attribution_prose(case_id, cases_dir, report_path)
    return f"""---
brand_slug: {brand_slug}
case_id: {case_id}
version: {version}
created_at: '{now_iso()}'
brief_of: reports/{brand_slug}/report.md
panel: {panel_name}
sensitivity: confidential
---

# 判断书摘要 · {brand_slug} · v{version}

> 议题 + 结论 + 推论过程 + 论据 + 反方 + 验证锚点. 评分 / scores / 合议数字在 [report.md](report.md).

## 议题

{topic or '(EVOLUTION 重判 · 见上版)'}

## 结论

{conclusion}

## 推论过程

{reasoning}

## 论据

{evidence}

## 反方与脆弱

{counter}

## 30/90/365 验证锚点

{attribution}

---

完整推理 / 评委 reviews / 数字 dashboard 见 [report.md](report.md) ({estimate_lines(report_path)} 行).
"""
