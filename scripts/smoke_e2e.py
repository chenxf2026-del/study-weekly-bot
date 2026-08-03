#!/usr/bin/env python3
"""
smoke_e2e.py — 端到端烟雾测试 (T10, dev-plan v2.6)

不调真 LLM, 模拟 Phase 0-5 完整产物链, 检查所有契约都满足。
W0 期 Hermes 没上云时, 这是验证流水线骨架是否完整的唯一手段。

子命令:
  generate <output_root> [--brand <slug>] [--topic <type>] — 生成 fake brand 完整产物
  verify <output_root> [--brand <slug>] — 检查产物契约
  smoke <output_root> — generate + verify 一气呵成

产物结构 (与 CLAUDE.md §4.6 / PRD §4.3 对齐):
  <root>/cases/C-2026-9999/
    ├── case.json                (合规 case-schema.json)
    ├── synthesis.md             (Phase 3 产出)
    └── raw_evidence/dim_*.md    (Phase 2 sub-agent 产出 × 4)
  <root>/reports/<brand>/
    ├── report.md                (canonical, 含 panel_summary)
    ├── panel.yaml               (本议题绑定的评委)
    ├── versions/v1_<date>.md    (冻结快照)
    └── reviews/
        ├── tian.md              (anchor, 无 adversarial_view)
        ├── industry-trend.md    (dim, 必含 adversarial_view 三字段)
        ├── strategic-vision.md
        ├── financial-strategy.md
        └── org-strategy.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


VAULT_ROOT = Path(__file__).parent.parent.resolve()
SCHEMA_PATH = VAULT_ROOT / "schemas" / "case-schema.json"

CASE_ID = "C-2026-9999"
DEFAULT_BRAND = "test-brand-smoke"
DEFAULT_TOPIC = "strategic"

# panels/default.yaml auto_judge_selection.rules["strategic"]
STRATEGIC_DIMS = ["industry-trend", "strategic-vision", "financial-strategy", "org-strategy"]

DEFAULT_PANEL = ["tian"] + STRATEGIC_DIMS


# ──────────────────────────────────────────────────────────────
# 生成器
# ──────────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.now().date().isoformat()


def _now_iso() -> str:
    return datetime.now().isoformat()


def _build_case_json(brand_slug: str, judges: list[str], topic_type: str) -> dict[str, Any]:
    """构造合规 case (与 schemas/case-schema.json 对齐, 12 必填字段)"""
    return {
        "case_id": CASE_ID,
        "brand_slug": brand_slug,
        "skill_used": "tian-judgement-orchestrator",
        "skill_version": "1.0",
        "created_at": _now_iso(),
        "panel": "default",
        "judges": judges,
        "sensitivity": "confidential",
        "context": {
            "topic": f"测试议题 · {topic_type} 类 · {brand_slug}",
            "trigger_event": {
                "named_event": "smoke e2e 触发事件",
                "occurred_at": _now_iso(),
                "source_url": "lark://docx/smoke-e2e-test",
            },
            "time_window": {
                "deadline": "2026-12-31",
                "external_anchor": "Q4 财报日",
            },
            "constraints": [
                {"type": "legal", "description": "smoke 测试约束 A"},
                {"type": "equity", "description": "smoke 测试约束 B"},
                {"type": "resource", "description": "smoke 测试约束 C"},
            ],
            "background_from_wiki": [
                f"_wiki/entities/{brand_slug}.md",
                "_wiki/concepts/test-concept.md",
            ],
        },
        "variables": [
            {
                "name": "smoke 变量 A",
                "category": "macro",
                "current_value": 0.5,
                "flip_threshold": {"value": 0.7, "direction": "above"},
                "weight": 0.4,
                "data_source": "smoke fixture",
            },
            {
                "name": "smoke 变量 B",
                "category": "internal",
                "current_value": "ok",
                "flip_threshold": {"value": "degraded", "direction": "eq"},
                "weight": 0.3,
                "data_source": "smoke fixture",
            },
            {
                "name": "smoke 变量 C",
                "category": "other",
                "current_value": 10,
                "flip_threshold": {"value": 5, "direction": "below"},
                "weight": 0.3,
                "data_source": "smoke fixture",
            },
        ],
        "reasoning_trace": {
            "steps": [
                "step 1: 收集证据",
                "step 2: 权衡变量",
                "step 3: 取舍",
            ],
            "jumps": [
                {
                    "type": "weight_jump",
                    "description": "smoke 跳步标注",
                    "source": "smoke_e2e@fixture",
                    "confidence": 0.7,
                }
            ],
        },
        "evidences": [
            {
                "statement": f"smoke 证据 {i}",
                "source_url": f"https://example.org/smoke-{i}",
                "source_type": "document",
                "verified_at": _today(),
            }
            for i in (1, 2, 3)
        ],
        "theses": [
            {
                "statement": "smoke 主张",
                "counter_statement": "smoke 反方主张 (独立命题)",
                "evidence_refs": [0, 1],
            }
        ],
        "decision": {
            "actions": [
                {
                    "description": "smoke 动作 1",
                    "owner": "张三",
                    "deadline": "2026-09-30",
                    "machine_verifiable_done": "smoke fixture 网页 footer 含 'smoke-passed' 字符串",
                }
            ],
            "owner": "smoke owner",
            "deadline": "2026-09-30",
        },
        "attribution": {
            "checkpoints": [
                {
                    "horizon_days": 30, "check_at": "2026-06-29",
                    "falsification_metric": "smoke 30d 可观测信号",
                    "expected_signal": "footer 字符串改为 X",
                    "data_source": "websearch", "status": "pending",
                },
                {
                    "horizon_days": 90, "check_at": "2026-08-29",
                    "falsification_metric": "smoke 90d 占比指标",
                    "expected_signal": "占比 > 15%",
                    "data_source": "marsdata", "status": "pending",
                },
                {
                    "horizon_days": 365, "check_at": "2027-05-30",
                    "falsification_metric": "smoke 年度保留率",
                    "expected_signal": "保留率 > 80%",
                    "data_source": "feishu", "status": "pending",
                },
            ]
        },
    }


def _build_dimension_review(judge_slug: str, brand_slug: str, scores: dict[str, int]) -> str:
    """生成 dimension 评委 review.md (含 adversarial_view 必填三字段)"""
    return f"""---
judge: {judge_slug}
judge_display_name: {judge_slug} (smoke)
judge_category: dimension
brand_slug: {brand_slug}
case_id: {CASE_ID}
version: 1
reviewed_at: '{_now_iso()}'
scores:
  reasoning_soundness: {scores.get('reasoning_soundness', 7)}
  evidence_thesis_coupling: {scores.get('evidence_thesis_coupling', 6)}
  counter_position_treatment: {scores.get('counter_position_treatment', 6)}
  falsifiability: {scores.get('falsifiability', 7)}
  real_world_resilience: {scores.get('real_world_resilience', 6)}
confidence: 0.7
adversarial_view:
  if_thesis_wrong: 'smoke fixture · 假如本判断错了, 最脆弱的一环是 X 假设。'
  contrary_signal_observed: 'smoke fixture · 近期反向信号 Y 数据下滑。'
  base_rate_warning: 'smoke fixture · 同类决策 base rate 约 40%, 需对齐。'
wiki_entities_referenced:
  - _wiki/entities/{brand_slug}.md
---

## 一句话
{judge_slug} 视角下, smoke fixture 论点在短期可成立, 长期需校准。

## 关键缺口
smoke fixture: 缺少 Z 维度的实证。

## 行动建议
1. smoke 补 Z 维度证据
2. smoke 30 天复核
"""


def _build_anchor_review(brand_slug: str, scores: dict[str, int]) -> str:
    """生成 anchor (tian) review · 不含 adversarial_view"""
    return f"""---
judge: tian
judge_display_name: 锚点 (anchor)
judge_category: anchor
brand_slug: {brand_slug}
case_id: {CASE_ID}
version: 1
reviewed_at: '{_now_iso()}'
scores:
  reasoning_soundness: {scores.get('reasoning_soundness', 5)}
  evidence_thesis_coupling: {scores.get('evidence_thesis_coupling', 5)}
  counter_position_treatment: {scores.get('counter_position_treatment', 4)}
  falsifiability: {scores.get('falsifiability', 5)}
  real_world_resilience: {scores.get('real_world_resilience', 5)}
confidence: 0.6
wiki_entities_referenced:
  - _wiki/people/锚点.md
---

## 一句话
smoke fixture · 锚点心证版本: 本议题阈值尚未达到, 时机偏早。

## 关键缺口
smoke fixture: 跳步 (timing_jump) 未充分识别。
"""


def _build_synthesis_md(brand_slug: str, judges: list[str]) -> str:
    return f"""---
case_id: {CASE_ID}
brand_slug: {brand_slug}
phase: 3
generated_at: '{_now_iso()}'
---

# Synthesis · smoke e2e

## 执行摘要

smoke fixture · 本议题核心论点 X, 时间窗 Q3。

## 杠杆地图

- 变量 A (weight 0.4): 当前 0.5, flip 阈值 0.7
- 变量 B (weight 0.3): ok → degraded
- 变量 C (weight 0.3): 当前 10, flip 阈值 < 5

## 脆弱边缘

最易证伪点: 客户接受度低于预期 (变量 A 反向)。

## 跨维度矛盾

industry-trend 与 strategic-vision 在时机判断上分歧 2 分。

## 引用索引

- raw_evidence/dim_*.md × {len(judges) - 1}
- _wiki/entities/{brand_slug}.md
"""


def _build_report_md(brand_slug: str, judges: list[str]) -> str:
    """canonical report.md, 含 panel_summary frontmatter (anchor_delta 必填)"""
    return f"""---
brand_slug: {brand_slug}
case_id: {CASE_ID}
version: 1
created_at: '{_now_iso()}'
panel: default
judges: {judges}
panel_summary:
  dimension_weighted_mean: 6.5
  anchor_tian_mean: 4.8
  anchor_delta: 1.7
  delta_high: false
  delta_explanation: smoke fixture · 维度比 anchor 乐观 1.7 分, 未超阈值 2.0
sensitivity: confidential
---

# Report · smoke e2e · {brand_slug} v1

## Phase 1 — Context

### Background from Wiki

- 公司档案: [[_wiki/entities/{brand_slug}]]
- 关键人物: [[_wiki/people/锚点]]

## Phase 5 — Lead Merge

dimension_weighted_mean = 6.5, anchor_tian_mean = 4.8, anchor_delta = +1.7。

详见各评委 review:
{chr(10).join(f'- [[reviews/{j}]]' for j in judges)}
"""


def _build_panel_yaml(judges: list[str]) -> str:
    return f"""# panel.yaml · smoke e2e
# 与 panels/default.yaml 一致, 此 brand 绑定使用
name: default
display_name: 默认 panel (smoke fixture)
brand_slug: {DEFAULT_BRAND}
case_id: {CASE_ID}
judges:
{chr(10).join(f'  - {j}' for j in judges)}
auto_selected_by: smoke_e2e@fixture
"""


def _build_versions_md(brand_slug: str, judges: list[str]) -> str:
    """冻结快照内容 — report.md 的 v1 snapshot, 不可变"""
    return f"""---
brand_slug: {brand_slug}
case_id: {CASE_ID}
version: 1
frozen_at: '{_now_iso()}'
immutable: true
---

# Frozen Snapshot · {brand_slug} · v1

panel_summary:
  dimension_weighted_mean: 6.5
  anchor_tian_mean: 4.8
  anchor_delta: 1.7

⚠️ 此快照不可变。EVOLUTION 模式应写 v2, 不修改 v1。
"""


def _build_raw_evidence_md(dim: str, brand_slug: str) -> str:
    return f"""---
case_id: {CASE_ID}
brand_slug: {brand_slug}
dimension: {dim}
phase: 2
sub_agent: smoke@fixture
generated_at: '{_now_iso()}'
---

# Raw Evidence · {dim}

## 证据 1
smoke fixture: 从 {dim} 视角的证据 1, 来源 https://example.org/smoke-{dim}-1

## 证据 2
smoke fixture: 从 {dim} 视角的证据 2, 来源 https://example.org/smoke-{dim}-2

## 证据 3
smoke fixture: 从 {dim} 视角的证据 3, 来源 _wiki/concepts/test-concept.md
"""


def generate(output_root: Path, brand_slug: str = DEFAULT_BRAND,
             topic_type: str = DEFAULT_TOPIC) -> dict[str, Any]:
    """生成完整的 fake brand 产物。返回 {created_files: [...], brand: ..., case_id: ...}"""

    judges = list(DEFAULT_PANEL)  # tian + 4 dims for strategic
    dims = [j for j in judges if j != "tian"]
    created = []

    # Phase 2: raw_evidence
    case_dir = output_root / "cases" / CASE_ID
    raw_dir = case_dir / "raw_evidence"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for dim in dims:
        f = raw_dir / f"dim_{dim.replace('-', '_')}.md"
        f.write_text(_build_raw_evidence_md(dim, brand_slug), encoding="utf-8")
        created.append(str(f.relative_to(output_root)))

    # Phase 3: synthesis
    f = case_dir / "synthesis.md"
    f.write_text(_build_synthesis_md(brand_slug, judges), encoding="utf-8")
    created.append(str(f.relative_to(output_root)))

    # case.json (放最后, 字段最完整)
    case = _build_case_json(brand_slug, judges, topic_type)
    f = case_dir / "case.json"
    f.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
    created.append(str(f.relative_to(output_root)))

    # Phase 4: reviews
    reports_dir = output_root / "reports" / brand_slug
    reviews_dir = reports_dir / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    # anchor (tian) review · 无 adversarial_view
    f = reviews_dir / "tian.md"
    f.write_text(_build_anchor_review(brand_slug, {}), encoding="utf-8")
    created.append(str(f.relative_to(output_root)))
    # dim reviews · 必含 adversarial_view
    for dim in dims:
        f = reviews_dir / f"{dim}.md"
        f.write_text(_build_dimension_review(dim, brand_slug, {}), encoding="utf-8")
        created.append(str(f.relative_to(output_root)))

    # Phase 5: report.md + versions/v1 + panel.yaml
    f = reports_dir / "report.md"
    f.write_text(_build_report_md(brand_slug, judges), encoding="utf-8")
    created.append(str(f.relative_to(output_root)))

    f = reports_dir / "panel.yaml"
    f.write_text(_build_panel_yaml(judges), encoding="utf-8")
    created.append(str(f.relative_to(output_root)))

    versions_dir = reports_dir / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    f = versions_dir / f"v1_{_today()}.md"
    if f.exists():
        f.unlink()  # 重跑场景: 旧 fixture 是 444 不可写, 删除重建 (目录可写即可删)
    f.write_text(_build_versions_md(brand_slug, judges), encoding="utf-8")
    os.chmod(f, 0o444)  # 不可变契约 (R3, v0.6) — 与 run_pipeline_local Phase 5 一致
    created.append(str(f.relative_to(output_root)))

    return {
        "brand_slug": brand_slug,
        "case_id": CASE_ID,
        "topic_type": topic_type,
        "judges": judges,
        "created_files": created,
    }


# ──────────────────────────────────────────────────────────────
# 校验器
# ──────────────────────────────────────────────────────────────

REQUIRED_ADVERSARIAL_FIELDS = {"if_thesis_wrong", "contrary_signal_observed", "base_rate_warning"}
REQUIRED_PANEL_SUMMARY_FIELDS = {"dimension_weighted_mean", "anchor_tian_mean", "anchor_delta", "delta_high"}
# sum_max_score (op2-company / workshop-midyear): 自定义维度满分相加, panel_summary 走另一组字段
REQUIRED_PANEL_SUMMARY_FIELDS_SUM_MAX = {"scoring_mode", "total_max", "dimension_total_mean", "grade"}
VERSIONS_FILENAME_RE = re.compile(r"^v\d+_\d{4}-\d{2}-\d{2}\.md$")
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)

# REVIEW 降级交付 (A4): 单个维度评委结构化输出异常 (GLM 偶发 yaml 坏, 修复兜底也救不回) 不该
# 废掉整条 ~$2 已生成报告。REVIEW 模式 + report.md 结构合法 + 异常 ≤ MAX 且正常维度评委 ≥ MIN_OK 时,
# 把这些**评委级**问题降为 warning (该评委已被 Phase 5 merge 排除, panel_summary 仍有效)。
# FRESH (锚点自身高风险判断) 仍严格硬失败, 让人察觉。报告结构 / case.json / 三段式等契约不降级。
_DEGRADE_MODES = {"REVIEW"}
_DEGRADE_MIN_OK_DIM = 2          # 至少 2 位维度评委正常 (否则 panel 失去意义)
_DEGRADE_ISSUE_DENOM = 3         # 容忍上限 = 维度评委总数 // 3 (6 判→2, 3-5 判→1); >1/3 异常 = 系统性, 硬失败
_ANCHOR_REVIEW_STEMS = {"tian"}  # anchor 基线评委文件名 (parse 失败拿不到 category 时用文件名兜底; anchor 异常不降级)


def _review_issues_degradable(n_issues: int, case_mode: str, report_ok: bool, dim_ok: int) -> bool:
    """维度评委级问题是否可降为 warning (而非硬 error) — 仅 REVIEW 模式 + 报告结构合法 +
    异常数 ≤ 容忍上限 且正常维度评委 ≥ MIN_OK 时才降级。FRESH / 报告坏 / 越界 → False (硬失败)。

    容忍上限**随 panel 规模缩放** (2026-07 干部周报 885 backfill: 6 评委 panel 偶发 2 位
    评委 YAML 解析失败, 旧硬编码上限 1 → 整份丢弃太浪费)。总维度评委数 = dim_ok + n_issues,
    上限 = max(1, 总数 // 3): 6 判容忍 2、3-5 判容忍 1 —— 大 panel 容忍更多 flaky 评委, 小 panel
    仍严格。配合 MIN_OK=2 下限双重保护 (2 失败在 4 判 panel 只剩 2 位, 恰卡下限)。"""
    total_dim = dim_ok + n_issues                                  # 本 panel 维度评委总数
    max_tolerable = max(1, total_dim // _DEGRADE_ISSUE_DENOM)      # 随规模缩放, 至少容忍 1
    return (
        n_issues > 0
        and case_mode in _DEGRADE_MODES
        and report_ok
        and n_issues <= max_tolerable
        and dim_ok >= _DEGRADE_MIN_OK_DIM
    )


def _parse_frontmatter(text: str) -> dict | None:
    """strict 解析 — smoke 是契约检查器, yaml 非法即 fail 是特性 (fix_review_yaml 应已修复)。
    v0.11 C3: 委托 _export_helpers 单一源 (语义不变: 失败/缺块 → None)。"""
    from _export_helpers import parse_frontmatter_strict
    return parse_frontmatter_strict(text)


def verify(output_root: Path, brand_slug: str = DEFAULT_BRAND) -> dict[str, Any]:
    """跑所有契约检查, 返回 verification report.

    REVIEW mode (ADR-007): 跳 raw_evidence 检查, 加 inv-3 §A/§B/§C 三段式 check.
    """
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[str] = []

    case_dir = output_root / "cases" / CASE_ID
    reports_dir = output_root / "reports" / brand_slug

    # ── 1. case.json 通过 schema 校验 ──
    case_json_path = case_dir / "case.json"
    case_mode = "FRESH"  # default
    if not case_json_path.exists():
        errors.append(f"case.json missing: {case_json_path}")
    else:
        try:
            import jsonschema
            schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
            case_data = json.loads(case_json_path.read_text(encoding="utf-8"))
            jsonschema.validate(case_data, schema)
            checks.append("case.json passes schemas/case-schema.json")
            case_mode = case_data.get("mode", "FRESH")  # ADR-007: read mode for REVIEW awareness
            if case_mode == "REVIEW":
                checks.append("case.json mode=REVIEW (ADR-007)")
        except jsonschema.ValidationError as e:
            errors.append(f"case.json schema validation failed: {e.message}")
        except Exception as e:
            errors.append(f"case.json check error: {type(e).__name__}: {e}")

    # ── 2. dimension reviews 必含 adversarial_view 三字段 ──
    # REVIEW 降级: 维度评委级问题先收集 (review_issues), 在 report.md 校验后统一裁决 error/warning。
    reviews_dir = reports_dir / "reviews"
    review_issues: list[str] = []   # 维度评委级可降级问题 (parse 失败 / 缺 adversarial 字段)
    dim_ok = 0                       # 完全合规的维度评委数
    if not reviews_dir.exists():
        errors.append(f"reviews/ missing: {reviews_dir}")
    else:
        for review in reviews_dir.glob("*.md"):
            fm = _parse_frontmatter(review.read_text(encoding="utf-8"))
            if fm is None:
                # parse 失败拿不到 category — anchor 用文件名兜底 (anchor 异常=硬错, 不降级)
                if review.stem in _ANCHOR_REVIEW_STEMS:
                    errors.append(f"{review.name}: frontmatter parse failed (anchor 基线, 不可降级)")
                else:
                    review_issues.append(f"{review.name}: frontmatter parse failed")
                continue
            category = fm.get("judge_category")
            if fm.get("framework_version"):
                # 自省诊断评委 (study-weekly, framework_version v8/v10/...): judge_category=dimension
                # 但按框架契约**不写** adversarial_view —— 评估对象是周报本身, 反向检视内化为
                # 反向扣分 + 5 维扣分, 而非"反命题"。故不套通用维度评委的 adversarial 三字段要求,
                # 视为合规 (版本无关: 只要带 framework_version 即自省评委, 未来版号不用改此处)。
                dim_ok += 1
                fv = fm.get("framework_version")
                checks.append(f"{review.name}: 自省评委 (framework {fv}, 无需 adversarial_view)")
            elif category == "dimension":
                av = fm.get("adversarial_view") or {}
                missing = REQUIRED_ADVERSARIAL_FIELDS - set(av.keys())
                if missing:
                    review_issues.append(f"{review.name}: missing adversarial_view fields {sorted(missing)}")
                else:
                    dim_ok += 1
                    checks.append(f"{review.name}: adversarial_view 三字段齐全")
            elif category == "anchor":
                checks.append(f"{review.name}: anchor review (无需 adversarial_view)")
            else:
                warnings.append(f"{review.name}: unknown judge_category '{category}'")

    # ── 3. report.md 含 panel_summary frontmatter ──
    report_ok = False                # 报告结构合法 (REVIEW 降级裁决的前提)
    report_md = reports_dir / "report.md"
    if not report_md.exists():
        errors.append(f"report.md missing: {report_md}")
    else:
        fm = _parse_frontmatter(report_md.read_text(encoding="utf-8"))
        if fm is None:
            errors.append("report.md: frontmatter parse failed")
        else:
            ps = fm.get("panel_summary") or {}
            if ps.get("scoring_mode") == "sum_max_score":
                required = REQUIRED_PANEL_SUMMARY_FIELDS_SUM_MAX
                ok_label = "report.md: panel_summary 必填字段齐全 (sum_max_score: total_max/total_mean/grade)"
            else:
                required = REQUIRED_PANEL_SUMMARY_FIELDS
                ok_label = "report.md: panel_summary 必填字段齐全 (含 anchor_delta)"
            missing = required - set(ps.keys())
            if missing:
                errors.append(f"report.md: panel_summary missing {sorted(missing)}")
            else:
                report_ok = True
                checks.append(ok_label)

    # ── REVIEW 降级交付裁决 (A4): 维度评委级问题在 REVIEW 模式 + report.md 合法 + 数量受控时
    # 降为 warning (该评委已被 merge 排除, ~$2 报告照常交付); 否则 (FRESH / 越界) 仍是硬 error。──
    if review_issues:
        if _review_issues_degradable(len(review_issues), case_mode, report_ok, dim_ok):
            for msg in review_issues:
                warnings.append(f"[降级交付] {msg}")
            warnings.append(
                f"REVIEW 降级交付: {len(review_issues)} 位维度评委结构化输出异常已从合议排除, "
                f"其余 {dim_ok} 位正常 — 报告照常交付 (panel_summary 已反映)")
        else:
            errors.extend(review_issues)

    # ── 4. versions/v{n}_*.md 文件命名匹配正则 ──
    versions_dir = reports_dir / "versions"
    if not versions_dir.exists():
        errors.append(f"versions/ missing: {versions_dir}")
    else:
        v_files = list(versions_dir.glob("v*.md"))
        if not v_files:
            errors.append("versions/ has no v{n}_*.md files")
        else:
            for vf in v_files:
                if not VERSIONS_FILENAME_RE.match(vf.name):
                    errors.append(f"versions/{vf.name}: filename does not match ^v\\d+_\\d{{4}}-\\d{{2}}-\\d{{2}}\\.md$")
                else:
                    checks.append(f"versions/{vf.name}: filename 合规")
                # 不可变契约 (R3, v0.6): 写入后 chmod 444, 任何 write bit 都算违约
                if vf.stat().st_mode & 0o222:
                    errors.append(f"versions/{vf.name}: 可写 (期望 chmod 444 不可变)")
                else:
                    checks.append(f"versions/{vf.name}: chmod 444 不可变")

    # ── 5. panel.yaml 是合法 yaml ──
    panel_yaml = reports_dir / "panel.yaml"
    if not panel_yaml.exists():
        errors.append(f"panel.yaml missing: {panel_yaml}")
    else:
        try:
            import yaml
            yaml.safe_load(panel_yaml.read_text(encoding="utf-8"))
            checks.append("panel.yaml: valid YAML")
        except Exception as e:
            errors.append(f"panel.yaml parse error: {e}")

    # ── 6. raw_evidence/dim_*.md ──
    # ADR-007 REVIEW: Phase 2 默认跳, raw_evidence 不存在合理. --verify mode 才检.
    raw_dir = case_dir / "raw_evidence"
    if case_mode == "REVIEW":
        # REVIEW: 跳 raw_evidence 强制检查 (Phase 2 默认跳)
        if raw_dir.exists() and list(raw_dir.glob("dim_*.md")):
            checks.append(f"raw_evidence/ (REVIEW --verify): {len(list(raw_dir.glob('dim_*.md')))} 个")
        else:
            checks.append("raw_evidence/ skipped (REVIEW mode default, Phase 2 跳)")
    else:
        if not raw_dir.exists():
            errors.append(f"raw_evidence/ missing: {raw_dir}")
        else:
            evidence_files = list(raw_dir.glob("dim_*.md"))
            if len(evidence_files) < 3:
                errors.append(f"raw_evidence/: 仅 {len(evidence_files)} 个 dim_*.md, 期望 ≥ 3")
            else:
                checks.append(f"raw_evidence/: {len(evidence_files)} 个 dim_*.md")

    # ── 7. synthesis.md 存在 ──
    synth = case_dir / "synthesis.md"
    if not synth.exists():
        errors.append(f"synthesis.md missing: {synth}")
    else:
        checks.append("synthesis.md exists")

    # ── 8. ADR-007 inv-3: REVIEW report.md 必有 §A/§B/§C 三段式 ──
    if case_mode == "REVIEW":
        if report_md.exists():
            text = report_md.read_text(encoding="utf-8")
            for sec_marker in ["§A", "§B", "§C"]:
                if sec_marker not in text:
                    errors.append(f"report.md (REVIEW): 缺 inv-3 三段式标记 '{sec_marker}'")
                else:
                    checks.append(f"report.md (REVIEW): 含 {sec_marker} 段")
        # revision-suggestions.md 必存在
        rs_path = reports_dir / "revision-suggestions.md"
        if not rs_path.exists():
            errors.append(f"revision-suggestions.md missing (REVIEW 必有): {rs_path}")
        else:
            checks.append("revision-suggestions.md exists (REVIEW)")

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
    }


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def _print_report(result: dict[str, Any]) -> None:
    print(f"checks passed: {len(result['checks'])}")
    for c in result["checks"]:
        print(f"  ✅ {c}")
    if result["warnings"]:
        print(f"\nwarnings: {len(result['warnings'])}")
        for w in result["warnings"]:
            print(f"  ⚠️  {w}")
    if result["errors"]:
        print(f"\nERRORS: {len(result['errors'])}")
        for e in result["errors"]:
            print(f"  ❌ {e}")
    print()
    print("=" * 60)
    print("✅ e2e smoke PASSED" if result["passed"] else "❌ e2e smoke FAILED")
    print("=" * 60)


def smoke_scene(slug: str) -> dict[str, Any]:
    """按 scene slug 校验 scene → panel → judge skill_paths 全链路。

    适合本地快速回归，不调真 LLM。
    返回 {passed, errors, warnings, checks, scene_type, scoring_mode, total_max}。
    """
    sys.path.insert(0, str(VAULT_ROOT / "scripts"))
    try:
        import scene_loader as sl
        import panel_loader as pl
    except ImportError as e:
        return {"passed": False, "errors": [f"import failed: {e}"], "warnings": [], "checks": []}

    errors: list[str] = []
    warnings: list[str] = []
    checks: list[str] = []
    meta: dict[str, Any] = {}

    # 1. 加载 scene
    try:
        scene = sl.load_scene(slug)
        checks.append(f"scene {slug!r} loads OK (type={scene.scene_type})")
        meta["scene_type"] = scene.scene_type
    except sl.SceneError as e:
        return {"passed": False, "errors": [f"scene load failed: {e}"], "warnings": [], "checks": [],
                "scene_type": None, "scoring_mode": None, "total_max": None}

    # 2. competition 约束
    if scene.scene_type == "competition":
        if scene.report.anchor_judge is not None:
            errors.append(f"competition scene must have anchor_judge=null, got {scene.report.anchor_judge!r}")
        else:
            checks.append("competition: anchor_judge=null ✓")
        if not scene.report.show_scores_publicly:
            warnings.append("competition: show_scores_publicly=false (建议为 true)")
        else:
            checks.append("competition: show_scores_publicly=true ✓")

    # 3. 解析 panel
    panel_path = scene.panel_path
    if panel_path is None:
        errors.append("panel_path is None — 找不到 panel 文件")
        return {"passed": False, "errors": errors, "warnings": warnings, "checks": checks,
                "scene_type": meta.get("scene_type"), "scoring_mode": None, "total_max": None}
    try:
        resolved = pl.resolve_panel(panel_path)
        scoring_mode = resolved.get("scoring_mode", "weighted_average")
        meta["scoring_mode"] = scoring_mode
        checks.append(f"panel resolves OK (scoring_mode={scoring_mode})")
    except Exception as e:
        errors.append(f"panel resolve failed: {e}")
        return {"passed": False, "errors": errors, "warnings": warnings, "checks": checks,
                "scene_type": meta.get("scene_type"), "scoring_mode": None, "total_max": None}

    # 4. judge skill_paths 存在
    judges = resolved.get("judges") or []
    if not judges:
        errors.append("panel has no judges")
    for j in judges:
        sp = j.get("skill_path")
        slug_j = j.get("slug", "?")
        if not sp:
            warnings.append(f"judge {slug_j!r}: no skill_path")
            continue
        full = VAULT_ROOT / sp
        if full.is_file():
            checks.append(f"judge {slug_j!r}: skill_path exists")
        else:
            errors.append(f"judge {slug_j!r}: skill_path NOT found: {sp}")

    # 5. sum_max_score 总分校验
    total_max = None
    if scoring_mode == "sum_max_score":
        try:
            total_max = pl.total_max_score(resolved)
            meta["total_max"] = total_max
            if total_max <= 0:
                errors.append(f"sum_max_score total={total_max} — 无效 (须 > 0)")
            else:
                checks.append(f"sum_max_score: total_max={total_max} ✓")
        except Exception as e:
            errors.append(f"total_max_score error: {e}")

    # 6. anchor_judge 一致性 (review 场景)
    if scene.scene_type != "competition":
        panel_anchor = resolved.get("anchor_judge")
        scene_anchor = scene.report.anchor_judge
        if panel_anchor != scene_anchor:
            errors.append(f"anchor mismatch: scene={scene_anchor!r}, panel={panel_anchor!r}")
        else:
            checks.append(f"anchor_judge={scene_anchor!r} 一致 ✓")

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "scene_type": meta.get("scene_type"),
        "scoring_mode": meta.get("scoring_mode"),
        "total_max": total_max,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="e2e smoke test (T10)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_gen = sub.add_parser("generate", help="生成 fake brand 完整产物")
    p_gen.add_argument("output_root")
    p_gen.add_argument("--brand", default=DEFAULT_BRAND)
    p_gen.add_argument("--topic", default=DEFAULT_TOPIC)

    p_ver = sub.add_parser("verify", help="校验产物契约")
    p_ver.add_argument("output_root")
    p_ver.add_argument("--brand", default=DEFAULT_BRAND)

    p_smoke = sub.add_parser("smoke", help="generate + verify")
    p_smoke.add_argument("output_root")
    p_smoke.add_argument("--brand", default=DEFAULT_BRAND)
    p_smoke.add_argument("--topic", default=DEFAULT_TOPIC)

    p_scene = sub.add_parser("smoke-scene", help="scene 全链路快速校验 (不调 LLM)")
    p_scene.add_argument("scene_slug", help="scenes/ 下的 scene slug")

    args = ap.parse_args()
    out = Path(args.output_root) if hasattr(args, "output_root") else None

    if args.cmd == "generate":
        out.mkdir(parents=True, exist_ok=True)
        result = generate(out, brand_slug=args.brand, topic_type=args.topic)
        print(f"generated {len(result['created_files'])} files under {out}")
        for f in result["created_files"]:
            print(f"  + {f}")
        return 0

    if args.cmd == "verify":
        result = verify(out, brand_slug=args.brand)
        _print_report(result)
        return 0 if result["passed"] else 1

    if args.cmd == "smoke":
        out.mkdir(parents=True, exist_ok=True)
        gen_result = generate(out, brand_slug=args.brand, topic_type=args.topic)
        print(f"generated {len(gen_result['created_files'])} files under {out}\n")
        ver_result = verify(out, brand_slug=args.brand)
        _print_report(ver_result)
        return 0 if ver_result["passed"] else 1

    if args.cmd == "smoke-scene":
        result = smoke_scene(args.scene_slug)
        print(f"=== smoke-scene: {args.scene_slug} ===")
        print(f"scene_type={result.get('scene_type')}  "
              f"scoring_mode={result.get('scoring_mode')}  "
              f"total_max={result.get('total_max')}")
        _print_report(result)
        return 0 if result["passed"] else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
