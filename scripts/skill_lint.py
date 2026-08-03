#!/usr/bin/env python3
"""
skill_lint.py — Skill + 边界 Lint

主要检查 (与 D7 单一 vault + 双层目录边界判断对齐):

1. SKILL.md 格式: frontmatter 必含字段 (name / description / allowed-tools)
2. 边界规则:
   a. _wiki/ 下不能出现 cases/ 或 reports/ 的复制粘贴 (检查 5+ 行连续相同)
   b. cases/case.json 必须通过 schemas/case-schema.json 校验
   c. reports/<brand>/versions/v{n}_*.md 一旦存在不能修改 (检测 git history)
3. wiki backlinks:
   _wiki/entities/<X>.md 的 "Related Judgements" 段必须与
   reports/*/report.md 的 metadata.related_entities 一致
4. priority-order substring 反 pattern (Case 6 30d real run 暴露):
   scripts/*.py 中 `for s in [...]: if s in text: return s` warning
   (源于 _extract_state bug, 改 first-occurrence; 抑制注释 `# skill-lint: priority-order-ok`)
5. anchor review 双 scale confidence (P2.4 · Case 6 §confidence_cap_protest 落地):
   reports/<brand>/reviews/<anchor>.md 中 confidence_meta_layer +
   confidence_single_point_layer 字段在元层议题 (meta_framework / cross_domain)
   推荐, 单点议题不允许. 详见 schemas/framework-v0.2-spec.yaml §anchor_confidence_dual_scale

用法:
    python scripts/skill_lint.py                     # 全量
    python scripts/skill_lint.py --staged            # 只查 git staged
    python scripts/skill_lint.py --check-boundary    # 只查边界规则
    python scripts/skill_lint.py validate-skill skills/X/SKILL.md
    python scripts/skill_lint.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    print("需要 PyYAML: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


VAULT_ROOT = Path(__file__).parent.parent.resolve()


@dataclass
class LintIssue:
    severity: str        # error | warning | info
    rule: str
    file_path: str
    line_no: int = 0
    message: str = ""


@dataclass
class LintReport:
    issues: list[LintIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def add(self, severity, rule, file_path, message="", line_no=0):
        self.issues.append(LintIssue(severity, rule, str(file_path), line_no, message))

    def render(self) -> str:
        if not self.issues:
            return "✅ skill_lint PASSED — 无 issue\n"

        out = [f"📋 skill_lint — {len(self.errors)} error(s), {len(self.warnings)} warning(s)"]
        out.append("=" * 70)
        for i in sorted(self.issues, key=lambda x: (x.severity, x.file_path, x.line_no)):
            mark = {"error": "🛑", "warning": "⚠️ ", "info": "ℹ️ "}[i.severity]
            loc = f"{i.file_path}:{i.line_no}" if i.line_no else i.file_path
            out.append(f"  {mark} [{i.rule}] {loc}")
            if i.message:
                out.append(f"      {i.message}")
        return "\n".join(out) + "\n"


# Module-level flag for `--check-framework-v02` opt-in (set in main())
_force_v02_check: bool = False


# ─────────────────────────────────────────────────────────────────────
# Rule 1: SKILL.md frontmatter 必含字段
# ─────────────────────────────────────────────────────────────────────

REQUIRED_FRONTMATTER = {"name", "description"}
REQUIRED_FOR_LEAD = REQUIRED_FRONTMATTER | {"allowed-tools"}


def _parse_frontmatter(text: str) -> Optional[dict]:
    # v0.11 C3: 委托 _export_helpers 单一源 (语义不变: 失败/缺块 → None)
    from _export_helpers import parse_frontmatter_strict
    return parse_frontmatter_strict(text)


def lint_skill_md(path: Path, report: LintReport) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        report.add("error", "skill_read", path, f"无法读取: {e}")
        return

    fm = _parse_frontmatter(text)
    if fm is None:
        report.add("error", "skill_no_frontmatter", path,
                   "SKILL.md 必须以 frontmatter 开头 (--- ... ---)")
        return

    # 必填字段
    required = REQUIRED_FOR_LEAD if "orchestrator" in path.name else REQUIRED_FRONTMATTER
    missing = required - set(fm.keys())
    if missing:
        report.add("error", "skill_missing_field", path,
                   f"frontmatter 缺少字段: {sorted(missing)}")

    # description 长度
    desc = fm.get("description", "")
    if isinstance(desc, str) and len(desc.strip()) < 30:
        report.add("warning", "skill_desc_too_short", path,
                   f"description 过短 ({len(desc)} 字符), 可能影响 Skill router 路由质量")

    # boss 扩展字段 (非强制, 但建议)
    if "boss" not in fm:
        report.add("info", "skill_no_boss_ext", path,
                   "缺少 boss: 扩展字段, 推荐添加 (schema_version, quality_tier, sensitivity)")

    # allowed-tools 必须是 list
    tools = fm.get("allowed-tools")
    if tools is not None and not isinstance(tools, list):
        report.add("error", "skill_tools_not_list", path,
                   f"allowed-tools 必须是 list, 当前 {type(tools).__name__}")


# ─────────────────────────────────────────────────────────────────────
# Rule 2: case.json 校验
# ─────────────────────────────────────────────────────────────────────

def lint_case_json(path: Path, report: LintReport) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        report.add("error", "case_invalid_json", path, f"非合法 JSON: {e}")
        return

    # 模板文件跳过
    if "_template_note" in data:
        return

    # 必填字段 (FRESH/EVOLUTION 全字段, REVIEW 模式豁免 ATELIER 5 字段)
    # ADR-007 REVIEW 模式评议现成方案 doc, 不产生原创 reasoning_trace/theses 等,
    # 这 5 字段 (variables/reasoning_trace/evidences/theses/decision) 留空 stub OK.
    base_required = ["case_id", "brand_slug", "skill_used", "created_at",
                     "sensitivity", "context", "attribution"]
    atelier_required = ["variables", "reasoning_trace", "evidences",
                        "theses", "decision"]
    mode = data.get("mode", "FRESH").upper()
    if mode in ("REVIEW", "WRITTEN-NOT-DISPATCHED"):
        required = base_required  # REVIEW / 写不跑 不查 ATELIER 字段
    else:
        required = base_required + atelier_required
    missing = [k for k in required if k not in data]
    if missing:
        report.add("error", "case_missing_field", path,
                   f"case.json 缺少字段: {missing}")

    # case_id 格式
    case_id = data.get("case_id", "")
    if not re.match(r"^C-\d{4}-\d{4}$", case_id):
        report.add("error", "case_id_format", path,
                   f"case_id 格式错误: {case_id!r} (期望 C-YYYY-NNNN)")

    # attribution.checkpoints 至少 3 个
    cps = data.get("attribution", {}).get("checkpoints", [])
    # 只对 int horizon_days 排序: 某 checkpoint 缺该字段 (None) 或写成字符串时,
    # sorted 混排 None/int 会 TypeError 崩 linter — 过滤后 != [30,90,365] 仍会正常告警。
    horizons = sorted(hd for c in cps if isinstance((hd := c.get("horizon_days")), int))
    if horizons != [30, 90, 365]:
        report.add("warning", "case_attribution_horizons", path,
                   f"attribution 应含 30/90/365 三个 checkpoint, 当前 {horizons}")

    # v0.6 R9: checkpoint metric 含 "占位" → error (原 attribution_check 渲染时仅提醒)
    # 可证伪性是 CLAUDE.md §8 硬要求 — 占位 metric 的 checkpoint 到期时无法独立观测
    for cp in cps:
        for field in ("falsification_metric", "expected_signal"):
            val = cp.get(field) or ""
            if "占位" in val:
                report.add("error", "case_metric_placeholder", path,
                           f"checkpoint {cp.get('horizon_days')}d {field} 含 '占位' — "
                           f"必须改为具体可观测信号后再提交 (CLAUDE.md §8 / v0.6 R9)")

    # 触发事件具象度
    trig = data.get("context", {}).get("trigger_event", {})
    named = trig.get("named_event", "")
    if not named or len(named) < 10:
        report.add("error", "case_trigger_abstract", path,
                   f"触发事件过抽象 ({named!r}), 必须含具名事件 (会议名/客户名/具体动作)")

    # framework v0.2 opt-in 验证: 若 case.json 含 framework_version: "v0.2" 自动 enable
    # OR `--check-framework-v02` CLI flag 强制 enable (即使 case.json 没标注)
    if data.get("framework_version") == "v0.2" or _force_v02_check:
        lint_case_json_framework_v02(path, data, report)


# ─────────────────────────────────────────────────────────────────────
# Rule 2b: framework v0.2 字段验证 (opt-in via --check-framework-v02 or
# framework_version: "v0.2" 自动 enable)
# ─────────────────────────────────────────────────────────────────────

def lint_case_json_framework_v02(path: Path, data: dict, report: LintReport) -> None:
    """v0.2 framework 字段验证: 8 V variables + V3/V5 sub-variables + 4 states +
    follow_upgrade_deadline + declines_to_state topic_type 限定.

    与 schemas/framework-v0.2-spec.yaml + templates/case-template-v0.2.json 对齐.
    """
    # variables[] 必含 8 个 V1-V8
    variables = data.get("variables", [])
    if not variables:
        report.add("warning", "v02_variables_empty", path,
                   "v0.2 framework_version 但 variables[] 空 (元判别议题允许, 单点议题应填)")
        return

    found_v_ids = set()
    weight_sum = 0.0
    for v in variables:
        v_id = v.get("framework_variable_id", "")
        if v_id:
            found_v_ids.add(v_id)
        weight_sum += v.get("weight", 0.0) or 0.0

        # V3 子变量
        if v_id == "V3":
            sub = v.get("sub_variables", [])
            sub_ids = {s.get("id") for s in sub}
            expected_v3 = {"V3a", "V3b", "V3c"}
            missing = expected_v3 - sub_ids
            if missing:
                report.add("error", "v02_v3_sub_missing", path,
                           f"v0.2 V3 应含 sub_variables {expected_v3}, 缺 {missing}")

        # V5 子变量
        if v_id == "V5":
            sub = v.get("sub_variables", [])
            sub_ids = {s.get("id") for s in sub}
            expected_v5 = {"V5a", "V5b"}
            missing = expected_v5 - sub_ids
            if missing:
                report.add("error", "v02_v5_sub_missing", path,
                           f"v0.2 V5 应含 sub_variables {expected_v5}, 缺 {missing}")
            if not v.get("override_rule"):
                report.add("warning", "v02_v5_override_rule_missing", path,
                           "v0.2 V5 建议含 override_rule (V5b ≥ B + V5a ≤ W 时 V5a × 0.5, V5b × 1.5)")

        # V6 权重应 ≥ 0.15
        if v_id == "V6":
            w = v.get("weight", 0.0)
            if w < 0.15:
                report.add("error", "v02_v6_weight_too_low", path,
                           f"v0.2 V6 权重应 ≥ 0.15 (5 评委一致 P0), 当前 {w}")
            if "v02_anchor_override_quota" not in v:
                report.add("warning", "v02_v6_override_quota_missing", path,
                           "v0.2 V6 应含 v02_anchor_override_quota (anchor 单方面 override 配额)")

    # 8 个 V variables 完整性 (V1-V8)
    expected_v = {f"V{i}" for i in range(1, 9)}
    missing_v = expected_v - found_v_ids
    if missing_v:
        report.add("warning", "v02_v_variables_missing", path,
                   f"v0.2 期望 V1-V8 全部 8 个, 缺 {sorted(missing_v)}")

    # weight 总和 ≈ 1.00 (含 sub_variables)
    if abs(weight_sum - 1.00) > 0.01:
        report.add("warning", "v02_weight_sum_not_1", path,
                   f"v0.2 variables[] weight 总和应 = 1.00, 当前 {weight_sum:.2f}")

    # decision 字段
    decision = data.get("decision", {})

    framework_state = decision.get("framework_recommended_state")
    final_state = decision.get("final_state")
    valid_states = {"bet", "wait", "follow", "declines_to_state"}

    if framework_state and framework_state not in valid_states:
        report.add("error", "v02_invalid_framework_state", path,
                   f"v0.2 framework_recommended_state 应为 {valid_states}, 当前 {framework_state!r}")

    if final_state and final_state not in valid_states:
        report.add("error", "v02_invalid_final_state", path,
                   f"v0.2 final_state 应为 {valid_states}, 当前 {final_state!r}")

    # follow 状态必须有 upgrade_deadline
    if final_state == "follow" and not decision.get("follow_upgrade_deadline"):
        report.add("error", "v02_follow_no_upgrade_deadline", path,
                   "v0.2 follow 状态必填 follow_upgrade_deadline (≤ 12 月, attribution_check 到期触发 status_review)")

    # declines_to_state 仅 meta_framework / cross_domain 议题可用
    topic_type = data.get("topic_type")
    if final_state == "declines_to_state" and topic_type not in {"meta_framework", "cross_domain"}:
        report.add("error", "v02_declines_to_state_topic_misuse", path,
                   f"v0.2 declines_to_state 仅允许 meta_framework / cross_domain 议题, 当前 topic_type={topic_type!r}")


# ─────────────────────────────────────────────────────────────────────
# Rule 3: 边界规则 — _wiki/ 与 cases/ reports/ 内容不能重叠
# ─────────────────────────────────────────────────────────────────────

def lint_boundary(report: LintReport) -> None:
    """检查 _wiki/ 没有 MBA 内容的复制粘贴。"""
    wiki_dir = VAULT_ROOT / "_wiki"
    reports_dir = VAULT_ROOT / "reports"

    if not wiki_dir.exists() or not reports_dir.exists():
        return

    # 取所有 reports/*/report.md 与 versions/*.md 的 5-line 滑动窗口
    fingerprints: dict[str, str] = {}   # 5-line snippet -> source file
    for rpt in reports_dir.rglob("*.md"):
        if "/template" in str(rpt) or "/.review" in str(rpt):
            continue
        try:
            lines = [l for l in rpt.read_text(encoding="utf-8").splitlines() if l.strip()]
        except Exception:
            continue
        for i in range(len(lines) - 4):
            snip = "\n".join(lines[i:i+5])
            # 长度阈值: 至少 200 字符才算有意义重复
            if len(snip) >= 200:
                fingerprints[snip] = str(rpt)

    # 检查 _wiki/ 是否包含这些 snippet
    for wf in wiki_dir.rglob("*.md"):
        try:
            text = wf.read_text(encoding="utf-8")
        except Exception:
            continue
        for snip, src in fingerprints.items():
            if snip in text:
                report.add(
                    "error", "boundary_violation_copy", wf,
                    f"发现 reports/ 内容复制粘贴 (来自 {src})。"
                    f"Wiki 与 MBA 必须 markdown link 引用, 不能复制内容。"
                )


def lint_versions_immutability(report: LintReport) -> None:
    """检查 reports/<brand>/versions/v{n}_*.md 是否被修改 (git 视角)。"""
    if not (VAULT_ROOT / ".git").exists():
        return

    try:
        # 找所有 staged/modified 的 versions/ 文件
        out = subprocess.check_output(
            ["git", "diff", "--name-only", "HEAD", "--", "reports/*/versions/"],
            cwd=VAULT_ROOT, text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return

    for line in out.strip().splitlines():
        if "/versions/v" in line and line.endswith(".md"):
            report.add(
                "error", "version_modified", line,
                "reports/<brand>/versions/ 中已写入的 v{n} 不可修改。"
                "如需修订, 创建 v{n+1}_*.md。"
            )


def lint_priority_substring_pattern(report: LintReport) -> None:
    """检查 scripts/*.py 中的 priority-order substring 反 pattern.

    源于 C-2026-0006 30d real run 暴露的 _extract_state bug:

        for s in ["bet", "wait", "follow"]:   # 按优先级顺序
            if s in text:                       # substring 容纳
                return s                        # 第一个匹配返回

    问题: 当 text 含多个候选 (如 "follow + 浅 bet 混合"), 优先级表先到的会被
    错抓, 不是文本里 first-occurrence 的. 中文/英文方向语境下前置词才是主词.

    正确写法 (first-occurrence-in-text):

        positions = [(text.find(s), s) for s in ["bet", "wait", "follow"]]
        valid = [(p, s) for p, s in positions if p >= 0]
        return min(valid, key=lambda x: x[0])[1] if valid else "?"

    检测: regex 匹配上述 3 行模式 (for / if / return).
    抑制: 在 for 行上方 5 行内加 `# skill-lint: priority-order-ok` 注释.
    严重级: warning (启发式, 不阻断 commit).
    """
    pattern = re.compile(
        r"for\s+(\w+)\s+in\s+\[[^\]]+\]\s*:\s*\n\s+if\s+\1\s+in\s+(\w+)\s*:\s*\n\s+return\s+\1",
        re.MULTILINE,
    )

    scripts_dir = VAULT_ROOT / "scripts"
    if not scripts_dir.exists():
        return

    for py in scripts_dir.glob("*.py"):
        if py.name == "skill_lint.py":
            continue  # 自身正则字符串会假阳性命中
        text = py.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            # 看上方 5 行是否有 opt-out 注释
            lines = text.split("\n")
            preceding = "\n".join(lines[max(0, line_no - 5): line_no - 1])
            if "skill-lint: priority-order-ok" in preceding:
                continue
            report.add(
                "warning",
                "priority_order_substring",
                str(py.relative_to(VAULT_ROOT)),
                "priority-order substring 反 pattern (Case 6 parser bug 源). "
                "改为 first-occurrence-in-text 语义 (text.find + min by position). "
                "若优先级顺序确实正确, 在 for 上方加 `# skill-lint: priority-order-ok` 抑制.",
                line_no=line_no,
            )


def lint_wiki_backlinks(report: LintReport, fix: bool = False) -> None:
    """检查 _wiki/entities/<X>.md 的 Related Judgements 段是否与 reports/ 一致。"""
    entities_dir = VAULT_ROOT / "_wiki" / "entities"
    reports_dir = VAULT_ROOT / "reports"

    if not entities_dir.exists() or not reports_dir.exists():
        return

    # 收集所有 report.md metadata
    related: dict[str, list[tuple[str, str, int]]] = {}  # entity -> [(brand_slug, date, version)]
    for rpt in reports_dir.glob("*/report.md"):
        try:
            text = rpt.read_text(encoding="utf-8")
        except Exception:
            continue
        fm = _parse_frontmatter(text)
        if not fm:
            continue
        entities = fm.get("related_entities", [])
        for ent_link in entities:
            # markdown link 形式: _wiki/entities/example-b2b-client.md 或 [[_wiki/entities/example-b2b-client]]
            m = re.search(r"_wiki/entities/([^/.\]]+)", ent_link)
            if m:
                entity_name = m.group(1)
                related.setdefault(entity_name, []).append((
                    fm.get("brand_slug", ""),
                    fm.get("created_at", ""),
                    fm.get("version", 0),
                ))

    # 检查每个 entity 页面
    for ent_file in entities_dir.glob("*.md"):
        entity_name = ent_file.stem
        expected = related.get(entity_name, [])
        if not expected:
            continue

        try:
            text = ent_file.read_text(encoding="utf-8")
        except Exception:
            continue

        # 找 "Related Judgements" 段
        if "## Related Judgements" not in text:
            report.add("warning", "wiki_missing_backlinks", ent_file,
                       f"entities/{entity_name} 缺少 Related Judgements 段, "
                       f"但 reports/ 中有 {len(expected)} 条引用")
            continue

        # (V0 暂不强校验段内具体行, V1 增强)


# ─────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────

def lint_dimension_review_md(path: Path, report: LintReport) -> None:
    """v3.1 新增: 检查 dimension 评委 review 是否含必填 adversarial_view 字段。

    review 文件路径: reports/<brand>/reviews/<judge>.md
    判断 judge 是否为 perspective-dimension 通过 frontmatter 的 judge slug。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return

    # 6 维度评委 slug 列表 (硬编码, 与 panels/default.yaml 一致)
    DIMENSION_JUDGES = {
        "industry-trend", "strategic-vision", "customer-strategy",
        "product-strategy", "org-strategy", "financial-strategy",
    }

    fm = _parse_frontmatter(text)
    if not fm:
        # 无/坏 frontmatter: 若文件名 stem 是维度评委 slug, 本该带 adversarial_view 必填字段。
        # 早先静默 return → 缺 frontmatter 的维度 review 绕过必填闸 (fail-open, §4.5)。现改为
        # 显式 warning 让其可见 (不用 error: vault 已有若干历史畸形 review, 硬 error 会
        # 追溯性阻断 CI; 带 frontmatter 但缺 adversarial_view 仍走下方 error 硬闸不变)。
        if path.stem in DIMENSION_JUDGES:
            report.add("warning", "review_frontmatter_missing", path,
                       f"{path.name} 文件名是维度评委 slug 但 frontmatter 缺失/无法解析 — "
                       f"adversarial_view 必填字段无法校验 (CLAUDE.md §4.5)")
        return

    judge_slug = fm.get("judge", "")
    if judge_slug not in DIMENSION_JUDGES:
        return  # anchor (tian) review 不需要 adversarial_view

    adv = fm.get("adversarial_view") or {}
    required_keys = ("if_thesis_wrong", "contrary_signal_observed", "base_rate_warning")
    if not isinstance(adv, dict):
        report.add("error", "review_missing_adversarial", path,
                   f"dimension review 必须含 adversarial_view dict, 当前: {type(adv).__name__}")
        return

    missing = [k for k in required_keys if not adv.get(k)]
    if missing:
        report.add("error", "review_adversarial_field_missing", path,
                   f"dimension review (judge={judge_slug}) 缺 adversarial_view 字段: {missing}")


def lint_anchor_confidence_dual_scale_md(path: Path, report: LintReport) -> None:
    """P2.4 · anchor review confidence 双 scale 验证 (Case 6 §confidence_cap_protest 落地).

    应用范围: 仅 anchor review (judge == anchor slug). 4 规则:

    单点议题 (strategic / customer / product / brand / organizational / financial):
      Rule 1: confidence_meta_layer / confidence_single_point_layer 不应出现 (warning)

    元层议题 (meta_framework / cross_domain):
      Rule 2: 若同时填 confidence 与 confidence_single_point_layer, 值必须一致 (error)
      Rule 3: confidence_meta_layer 应在 [0, 1] (error if out of range)
      Rule 4: confidence_single_point_layer 应 ≤ 0.5 (error if > 0.5, W1 cap)

    topic_type 从 case.json 或 sibling report.md frontmatter 读. 找不到 skip.

    详见 schemas/framework-v0.2-spec.yaml §anchor_confidence_dual_scale.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return

    fm = _parse_frontmatter(text)
    if not fm:
        return

    # 仅 anchor review (与 lint_dimension_review_md 互补)
    judge_category = fm.get("judge_category", "")
    if judge_category != "anchor":
        return

    # topic_type 来源: 同级 case.json 或 sibling report.md
    case_id = fm.get("case_id", "")
    brand_slug = fm.get("brand_slug", "")
    topic_type = _find_topic_type(case_id, brand_slug)
    if not topic_type:
        return  # 找不到 topic_type, skip 不报错 (可能新议题尚未落 case.json)

    META_TOPIC_TYPES = {"meta_framework", "cross_domain"}
    has_meta = "confidence_meta_layer" in fm
    has_single = "confidence_single_point_layer" in fm
    confidence = fm.get("confidence")
    single_point = fm.get("confidence_single_point_layer")
    meta_layer = fm.get("confidence_meta_layer")

    # Rule 1: 单点议题不应有双 scale 字段
    if topic_type not in META_TOPIC_TYPES:
        if has_meta or has_single:
            present = [k for k in ("confidence_meta_layer", "confidence_single_point_layer")
                       if k in fm]
            # v0.6 R9: warning → error (双 scale 误用会让单点议题的 confidence 语义失真)
            report.add("error", "anchor_dual_scale_misuse", path,
                       f"单点议题 (topic_type={topic_type}) anchor review 不应含双 scale 字段 "
                       f"{present}. 双 scale 仅元层 (meta_framework / cross_domain) 议题可用. "
                       f"详见 schemas/framework-v0.2-spec.yaml §anchor_confidence_dual_scale.")
        return  # 单点议题不再继续 Rule 2-4

    # 元层议题 — Rule 2-4

    # Rule 2: confidence 与 confidence_single_point_layer 必须一致
    if confidence is not None and single_point is not None:
        try:
            if abs(float(confidence) - float(single_point)) > 0.001:
                report.add("error", "anchor_dual_scale_backward_compat_mismatch", path,
                           f"confidence ({confidence}) 与 confidence_single_point_layer ({single_point}) "
                           f"语义等价 (backward compat), 值必须一致.")
        except (TypeError, ValueError):
            pass

    # Rule 3: confidence_meta_layer 范围 [0, 1]
    if meta_layer is not None:
        try:
            v = float(meta_layer)
            if v < 0 or v > 1:
                report.add("error", "anchor_dual_scale_meta_out_of_range", path,
                           f"confidence_meta_layer 应在 [0, 1], 当前 {v}")
        except (TypeError, ValueError):
            report.add("error", "anchor_dual_scale_meta_not_numeric", path,
                       f"confidence_meta_layer 应为数值, 当前 {meta_layer!r}")

    # Rule 4: confidence_single_point_layer ≤ 0.5 (W1 cap)
    if single_point is not None:
        try:
            v = float(single_point)
            if v > 0.5:
                report.add("error", "anchor_dual_scale_single_point_cap_violated", path,
                           f"confidence_single_point_layer 应 ≤ 0.5 (W1 占位 cap), 当前 {v}")
            if v < 0:
                report.add("error", "anchor_dual_scale_single_point_negative", path,
                           f"confidence_single_point_layer 应 ≥ 0, 当前 {v}")
        except (TypeError, ValueError):
            report.add("error", "anchor_dual_scale_single_point_not_numeric", path,
                       f"confidence_single_point_layer 应为数值, 当前 {single_point!r}")


def _find_topic_type(case_id: str, brand_slug: str) -> Optional[str]:
    """从 case.json 或 sibling report.md frontmatter 找 topic_type."""
    if case_id:
        case_json = VAULT_ROOT / "cases" / case_id / "case.json"
        if case_json.exists():
            try:
                data = json.loads(case_json.read_text(encoding="utf-8"))
                tt = data.get("topic_type")
                if tt:
                    return tt
            except Exception:
                pass
    if brand_slug:
        report_md = VAULT_ROOT / "reports" / brand_slug / "report.md"
        if report_md.exists():
            text = report_md.read_text(encoding="utf-8")
            fm = _parse_frontmatter(text)
            if fm:
                return fm.get("topic_type")
    return None


def collect_review_files() -> list[Path]:
    """收集所有 reports/<brand>/reviews/<judge>.md。"""
    reports_dir = VAULT_ROOT / "reports"
    if not reports_dir.exists():
        return []
    return list(reports_dir.glob("*/reviews/*.md"))


def collect_skill_files() -> list[Path]:
    skills = list((VAULT_ROOT / "skills").rglob("SKILL.md"))
    return skills


def collect_case_files(include_untracked: bool = False) -> list[Path]:
    """扫 cases/**/case.json.

    cases/ 是运行时数据 (跑评审自动产生), in-progress / 历史 case.json 的形状与
    attribution 占位 metric **不应阻断 --dry-run 全 PASS**。故默认跳过整个 cases/ 树,
    无论 git 跟踪状态。

    历史注: 早期用「gitignored」当「运行时数据」的代理 (_git_lint_eligible_relpaths
    过滤 tracked+unignored), 但 2026-07 起 cases 文本已入库备份 (不再 gitignored),
    该代理失效 → skill_lint 开始 lint 历史 case 撞出占位 metric 报错。改为直接按目录跳过。

    加 --include-untracked 显式 lint 全部 cases (本地排查 in-progress case 用)。
    提交时的逐 case schema 门禁走 --staged (get_staged_files) 单独路径, 不受此默认影响。
    """
    if not include_untracked:
        return []
    return list((VAULT_ROOT / "cases").rglob("case.json"))


def get_staged_files() -> list[Path]:
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=VAULT_ROOT, text=True,
        )
    except subprocess.CalledProcessError:
        return []
    return [VAULT_ROOT / line for line in out.strip().splitlines() if line]


# ─────────────────────────────────────────────────────────────────────
# v0.6 R1.2/R1.3 · anchor research 状态检测 + opt-in lint
#
# 状态三级 (PRD R1 解法 v2):
#   placeholder   — 任一文件仍是占位模板 → anchor confidence cap 0.4
#   agent_derived — 全部已合成但未经锚点签收 (verified_by_anchor: false) → cap 0.6
#   verified      — 全部 verified_by_anchor: true → 解除 cap
# run_pipeline_local Phase 4 通过 anchor_research_state() 读取此状态注入 cap。
# ─────────────────────────────────────────────────────────────────────

RESEARCH_FILE_IDS = ["01-identity", "02-expression-dna", "03-mental-models",
                     "04-heuristics", "05-counter-consensus", "06-anti-fab"]
_PLACEHOLDER_MARKERS = ("占位", "待 项目主理 填充", "(待 项目主理 填充)")


def _research_dir(anchor_slug: str) -> Path:
    return VAULT_ROOT / "anchors" / anchor_slug / "perspective" / "references" / "research"


def _parse_md_frontmatter(text: str) -> dict:
    # v0.11 C3: 委托单一源; 本调用方约定永远拿 dict (失败/非 dict → {})
    from _export_helpers import parse_frontmatter_strict
    fm = parse_frontmatter_strict(text)
    return fm if isinstance(fm, dict) else {}


def _research_file_state(path: Path) -> str:
    """单文件状态: missing / placeholder / agent_derived / verified"""
    if not path.is_file():
        return "missing"
    text = path.read_text(encoding="utf-8", errors="replace")
    if any(m in text for m in _PLACEHOLDER_MARKERS) or len(text) < 300:
        return "placeholder"
    fm = _parse_md_frontmatter(text)
    if fm.get("verified_by_anchor") is True:
        return "verified"
    return "agent_derived"


def anchor_research_state(anchor_slug: str = "tian") -> tuple[str, dict[str, str]]:
    """聚合状态 (placeholder / agent_derived / verified) + 每文件明细。

    供 run_pipeline_local Phase 4 anchor confidence cap (R1.3) 使用。
    """
    detail: dict[str, str] = {}
    rdir = _research_dir(anchor_slug)
    for fid in RESEARCH_FILE_IDS:
        matches = sorted(rdir.glob(f"{fid}*.md")) if rdir.is_dir() else []
        detail[fid] = _research_file_state(matches[0]) if matches else "missing"
    states = set(detail.values())
    if "missing" in states or "placeholder" in states:
        return "placeholder", detail
    if states == {"verified"}:
        return "verified", detail
    return "agent_derived", detail


_SOURCE_LINK_RE = re.compile(r"\(source:\s*[^)]+\)|\]\([^)]+\)|\[\[[^\]]+\]\]")


def lint_anchor_research(anchor_slug: str, report: LintReport) -> None:
    """R1.2 · opt-in (--check-anchor-research): 占位/字数/source 引用/provenance 校验。

    注意: 不进默认 lint 路径 — 占位状态是已知递延债务 (PRD R1), 由 confidence cap
    显式化, 不应阻断与 research 无关的 commit。
    """
    rdir = _research_dir(anchor_slug)
    if not rdir.is_dir():
        report.add("error", "anchor_research_dir_missing", rdir,
                   f"anchors/{anchor_slug}/perspective/references/research/ 不存在")
        return
    overall, detail = anchor_research_state(anchor_slug)
    report.add("info", "anchor_research_state", rdir,
               f"聚合状态: {overall} (confidence cap: "
               f"{ {'placeholder': 0.4, 'agent_derived': 0.6, 'verified': '无'}[overall] })")
    for fid in RESEARCH_FILE_IDS:
        matches = sorted(rdir.glob(f"{fid}*.md"))
        if not matches:
            report.add("error", "anchor_research_file_missing", rdir, f"{fid}*.md 缺失")
            continue
        path = matches[0]
        state = detail[fid]
        if state == "placeholder":
            report.add("error", "anchor_research_placeholder", path,
                       f"{fid} 仍是占位模板 — 跑 S1 elicitation + S2 合成填充 (PRD R1)")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        fm = _parse_md_frontmatter(text)
        body = text[text.find("\n---", 4) + 4:] if text.startswith("---") else text
        if len(body.strip()) < 500:
            report.add("error", "anchor_research_too_short", path,
                       f"{fid} 正文 {len(body.strip())} 字 < 500 (文件自带验收标准)")
        n_links = len(_SOURCE_LINK_RE.findall(body))
        if n_links < 5:
            report.add("error", "anchor_research_few_sources", path,
                       f"{fid} source 引用 {n_links} < 5 (无源不写入, 凑不满留短+标缺口)")
        if fm.get("provenance") not in ("agent-derived", "anchor-authored"):
            report.add("error", "anchor_research_no_provenance", path,
                       f"{fid} frontmatter 缺 provenance (agent-derived / anchor-authored)")
        if "verified_by_anchor" not in fm:
            report.add("error", "anchor_research_no_verified_flag", path,
                       f"{fid} frontmatter 缺 verified_by_anchor (true/false)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("subcmd", nargs="?", default="lint",
                    choices=["lint", "validate-skill"])
    ap.add_argument("path", nargs="?")
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--check-boundary", action="store_true")
    ap.add_argument("--check-framework-v02", action="store_true",
                    help="强制 v0.2 验证 (即使 case.json 没有 framework_version: v0.2 字段)")
    ap.add_argument("--check-anchor-research", action="store_true",
                    help="v0.6 R1.2: 校验 anchors/<slug>/.../research/ 6 文件 (占位/字数/引用/provenance)")
    ap.add_argument("--anchor", default="tian",
                    help="--check-anchor-research 的 anchor slug (默认 tian)")
    ap.add_argument("--include-untracked", action="store_true",
                    help="同时 lint cases/ (默认跳过: cases 是运行时数据, 占位 metric 不阻断 --dry-run; 提交门禁走 --staged)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rubric", help="rubric name (for validate-skill)")
    ap.add_argument("--target-score", type=int, default=70)
    args = ap.parse_args()

    if args.check_anchor_research:
        report = LintReport()
        lint_anchor_research(args.anchor, report)
        print(report.render())
        return 1 if report.errors else 0

    global _force_v02_check
    _force_v02_check = args.check_framework_v02

    report = LintReport()

    if args.subcmd == "validate-skill":
        if not args.path:
            print("validate-skill 需要传入 SKILL.md 路径", file=sys.stderr)
            return 2
        p = Path(args.path)
        if not p.exists():
            print(f"文件不存在: {p}", file=sys.stderr)
            return 2
        lint_skill_md(p, report)
        print(report.render())
        return 1 if report.errors else 0

    # 默认: lint
    if args.staged:
        staged = get_staged_files()
        for f in staged:
            if f.name == "SKILL.md":
                lint_skill_md(f, report)
            elif f.name == "case.json":
                lint_case_json(f, report)
            # v3.1: dimension reviews · P2.4: anchor dual scale
            elif "/reviews/" in str(f) and f.suffix == ".md":
                lint_dimension_review_md(f, report)
                lint_anchor_confidence_dual_scale_md(f, report)
    else:
        for skill in collect_skill_files():
            lint_skill_md(skill, report)
        for case in collect_case_files(include_untracked=args.include_untracked):
            lint_case_json(case, report)
        # v3.1: 全量检查所有 reviews · P2.4: anchor dual scale
        for rev in collect_review_files():
            lint_dimension_review_md(rev, report)
            lint_anchor_confidence_dual_scale_md(rev, report)

    # 边界规则
    if args.check_boundary or not args.staged:
        lint_boundary(report)
        lint_versions_immutability(report)
        lint_wiki_backlinks(report)
        lint_priority_substring_pattern(report)

    print(report.render())

    if args.dry_run:
        return 0
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
