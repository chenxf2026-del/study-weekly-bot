"""test_panel_extends.py — panel extends 继承 + scoring_mode (PRD v1.1 · R2)"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import panel_loader as pl


# ─────────────────────── fixtures ───────────────────────

def _write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def parent_panel(tmp_path) -> Path:
    """最小父 panel: 1 anchor + 3 dimension, 5 镜头 weighted_average。"""
    data = {
        "name": "default",
        "anchor_slug": "tian",
        "anchor_judge": "tian",
        "judges": [
            {"slug": "tian", "judge_category": "anchor", "skill_path": "anchors/tian/perspective/SKILL.md"},
            {"slug": "industry-trend", "judge_category": "dimension", "skill_path": "skills/industry-trend-perspective/SKILL.md"},
            {"slug": "org-strategy", "judge_category": "dimension", "skill_path": "skills/org-strategy-perspective/SKILL.md"},
            {"slug": "financial-strategy", "judge_category": "dimension", "skill_path": "skills/financial-strategy-perspective/SKILL.md"},
        ],
        "scoring_lenses": ["reasoning_soundness", "falsifiability", "real_world_resilience"],
    }
    return _write(tmp_path / "panels" / "default.yaml", data)


# ─────────────────────── 无继承 (向后兼容) ───────────────────────

class TestNoExtends:
    def test_plain_panel_returns_asis(self, parent_panel, tmp_path):
        resolved = pl.resolve_panel(parent_panel, root=tmp_path)
        assert resolved["name"] == "default"
        assert len(resolved["judges"]) == 4
        assert resolved["scoring_mode"] == "weighted_average"   # 默认补全
        assert resolved["scoring_lenses"] == ["reasoning_soundness", "falsifiability", "real_world_resilience"]


# ─────────────────────── 继承合并 ───────────────────────

class TestExtendsMerge:
    def test_inherits_parent_judges(self, parent_panel, tmp_path):
        child = _write(tmp_path / "scenes" / "s" / "panel.yaml",
                       {"name": "child", "extends": "panels/default.yaml"})
        resolved = pl.resolve_panel(child, root=tmp_path)
        assert resolved["name"] == "child"                      # child 覆盖标量
        assert len(resolved["judges"]) == 4                     # judges 沿用父
        assert resolved["anchor_judge"] == "tian"               # 沿用父

    def test_judges_drop(self, parent_panel, tmp_path):
        child = _write(tmp_path / "scenes" / "s" / "panel.yaml",
                       {"name": "child", "extends": "panels/default.yaml",
                        "judges_drop": ["org-strategy"]})
        resolved = pl.resolve_panel(child, root=tmp_path)
        slugs = [j["slug"] for j in resolved["judges"]]
        assert "org-strategy" not in slugs
        assert len(slugs) == 3

    def test_judges_add(self, parent_panel, tmp_path):
        child = _write(tmp_path / "scenes" / "s" / "panel.yaml",
                       {"name": "child", "extends": "panels/default.yaml",
                        "judges_add": [{"slug": "bg-fit", "judge_category": "dimension",
                                        "skill_path": "scenes/s/judges/bg-fit/SKILL.md"}]})
        resolved = pl.resolve_panel(child, root=tmp_path)
        slugs = [j["slug"] for j in resolved["judges"]]
        assert "bg-fit" in slugs
        assert len(slugs) == 5

    def test_judges_add_same_slug_overrides(self, parent_panel, tmp_path):
        child = _write(tmp_path / "scenes" / "s" / "panel.yaml",
                       {"name": "child", "extends": "panels/default.yaml",
                        "judges_add": [{"slug": "industry-trend", "judge_category": "dimension",
                                        "skill_path": "custom/path.md", "weight": 2.0}]})
        resolved = pl.resolve_panel(child, root=tmp_path)
        it = [j for j in resolved["judges"] if j["slug"] == "industry-trend"]
        assert len(it) == 1                                     # 不重复
        assert it[0]["skill_path"] == "custom/path.md"          # 用新定义

    def test_judges_override_replaces_all(self, parent_panel, tmp_path):
        child = _write(tmp_path / "scenes" / "s" / "panel.yaml",
                       {"name": "child", "extends": "panels/default.yaml",
                        "judges_override": [
                            {"slug": "tian-methodology", "role": "anchor", "skill_path": "a"},
                            {"slug": "business-results", "role": "dimension", "skill_path": "b"},
                        ]})
        resolved = pl.resolve_panel(child, root=tmp_path)
        slugs = [j["slug"] for j in resolved["judges"]]
        assert slugs == ["tian-methodology", "business-results"]

    def test_scoring_weights_overlay(self, parent_panel, tmp_path):
        child = _write(tmp_path / "scenes" / "s" / "panel.yaml",
                       {"name": "child", "extends": "panels/default.yaml",
                        "scoring_weights": {"falsifiability": 1.5}})
        resolved = pl.resolve_panel(child, root=tmp_path)
        assert resolved["scoring_weights"]["falsifiability"] == 1.5

    def test_anchor_judge_null(self, parent_panel, tmp_path):
        child = _write(tmp_path / "scenes" / "s" / "panel.yaml",
                       {"name": "child", "extends": "panels/default.yaml",
                        "anchor_judge": None})
        resolved = pl.resolve_panel(child, root=tmp_path)
        assert resolved["anchor_judge"] is None
        assert pl.has_anchor(resolved) is False


# ─────────────────────── scoring_lenses_override ───────────────────────

class TestLensesOverride:
    def test_competition_replaces_lenses(self, parent_panel, tmp_path):
        child = _write(tmp_path / "scenes" / "ws" / "panel.yaml",
                       {"name": "ws", "extends": "panels/default.yaml",
                        "anchor_judge": None,
                        "scoring_lenses_override": [
                            {"slug": "innovation", "display_name_cn": "创新度"},
                            {"slug": "feasibility", "display_name_cn": "可行性"},
                        ]})
        resolved = pl.resolve_panel(child, root=tmp_path)
        slugs = [l["slug"] for l in resolved["scoring_lenses"]]
        assert slugs == ["innovation", "feasibility"]


# ─────────────────────── scoring_mode = sum_max_score ───────────────────────

class TestSumMaxScore:
    def _co_level_lenses(self):
        """公司级 OP2 维度 (PRD §4.2), 合计正好 100。"""
        return [
            {"slug": "co_biz_judgment", "display_name_cn": "公司级经营判断", "max_score": 20},
            {"slug": "bg_bu_cascade", "display_name_cn": "BG/BU承接机制", "max_score": 20},
            {"slug": "biz_tradeoff", "display_name_cn": "业务结构取舍", "max_score": 15},
            {"slug": "ai_hard_action", "display_name_cn": "AI硬动作", "max_score": 15},
            {"slug": "value_mgmt_link", "display_name_cn": "价值经营联动", "max_score": 15},
            {"slug": "ownership_rhythm", "display_name_cn": "牵引机制", "max_score": 10},
            {"slug": "one_page_quality", "display_name_cn": "一页表达", "max_score": 5},
        ]

    def _universal_lenses(self):
        """OP2 通用维度 (PRD §4.1) — 源文档实际合计 110 (标注 100, 不一致)。"""
        return [
            {"slug": "problem_reality", "display_name_cn": "问题真实性", "max_score": 20},
            {"slug": "h2_target_quant", "display_name_cn": "H2目标量化", "max_score": 15},
            {"slug": "biz_result_link", "display_name_cn": "经营结果关联", "max_score": 15},
            {"slug": "action_effective", "display_name_cn": "举措有效性", "max_score": 20},
            {"slug": "org_ownership", "display_name_cn": "组织责任", "max_score": 25},
            {"slug": "ai_in_process", "display_name_cn": "AI融合", "max_score": 15},
        ]

    def test_sum_max_score_total_100(self, parent_panel, tmp_path):
        child = _write(tmp_path / "scenes" / "op2" / "panel.yaml",
                       {"name": "op2", "extends": "panels/default.yaml",
                        "scoring_mode": "sum_max_score",
                        "scoring_lenses_override": self._co_level_lenses()})
        resolved = pl.resolve_panel(child, root=tmp_path)
        assert resolved["scoring_mode"] == "sum_max_score"
        assert pl.total_max_score(resolved) == 100

    def test_sum_max_score_accepts_non_100_total(self, parent_panel, tmp_path):
        """通用维度合计 110 仍可解析 (源文档不一致, 等级按百分比归一)。"""
        child = _write(tmp_path / "scenes" / "op2" / "panel.yaml",
                       {"name": "op2", "extends": "panels/default.yaml",
                        "scoring_mode": "sum_max_score",
                        "scoring_lenses_override": self._universal_lenses()})
        resolved = pl.resolve_panel(child, root=tmp_path)
        assert pl.total_max_score(resolved) == 110

    def test_max_score_must_be_positive_int(self, parent_panel, tmp_path):
        lenses = self._co_level_lenses()
        lenses[0]["max_score"] = -5
        child = _write(tmp_path / "scenes" / "op2" / "panel.yaml",
                       {"name": "op2", "extends": "panels/default.yaml",
                        "scoring_mode": "sum_max_score",
                        "scoring_lenses_override": lenses})
        with pytest.raises(pl.PanelError, match="正整数"):
            pl.resolve_panel(child, root=tmp_path)

    def test_sum_max_score_requires_max_score_field(self, parent_panel, tmp_path):
        child = _write(tmp_path / "scenes" / "op2" / "panel.yaml",
                       {"name": "op2", "extends": "panels/default.yaml",
                        "scoring_mode": "sum_max_score",
                        "scoring_lenses_override": [{"slug": "x", "display_name_cn": "X"}]})
        with pytest.raises(pl.PanelError, match="须含 max_score"):
            pl.resolve_panel(child, root=tmp_path)

    def test_bad_scoring_mode(self, parent_panel, tmp_path):
        child = _write(tmp_path / "scenes" / "op2" / "panel.yaml",
                       {"name": "op2", "extends": "panels/default.yaml",
                        "scoring_mode": "bogus"})
        with pytest.raises(pl.PanelError, match="非法 scoring_mode"):
            pl.resolve_panel(child, root=tmp_path)


# ─────────────────────── grade_for_score ───────────────────────

    def _panel_total(self, total):
        # 构造 total 分的 sum_max_score panel (单镜头)
        return {"scoring_mode": "sum_max_score",
                "scoring_lenses": [{"slug": "x", "max_score": total}]}

    def test_default_thresholds_total_100(self):
        panel = self._panel_total(100)
        assert pl.grade_for_score(panel, 50) == "重写"
        assert pl.grade_for_score(panel, 70) == "修改"
        assert pl.grade_for_score(panel, 85) == "人工评审"

    def test_boundary_total_100(self):
        panel = self._panel_total(100)
        assert pl.grade_for_score(panel, 60) == "修改"          # >= rewrite
        assert pl.grade_for_score(panel, 80) == "人工评审"       # >= revise

    def test_percentage_normalized_for_110_total(self):
        """总分 110 时, 等级按百分比判定: 66/110=60% → 修改边界。"""
        panel = self._panel_total(110)
        assert pl.grade_for_score(panel, 65) == "重写"          # 59.1% < 60
        assert pl.grade_for_score(panel, 66) == "修改"          # 60% 命中 rewrite 边界
        assert pl.grade_for_score(panel, 88) == "人工评审"       # 80% 命中 revise 边界


# ─────────────────────── 继承层数限制 ───────────────────────

class TestDepthLimit:
    def test_two_level_extends_rejected(self, parent_panel, tmp_path):
        mid = _write(tmp_path / "panels" / "mid.yaml",
                     {"name": "mid", "extends": "panels/default.yaml"})
        grandchild = _write(tmp_path / "scenes" / "s" / "panel.yaml",
                            {"name": "gc", "extends": "panels/mid.yaml"})
        with pytest.raises(pl.PanelError, match="1 层继承"):
            pl.resolve_panel(grandchild, root=tmp_path)


# ─────────────────────── 真实 default.yaml 自检 ───────────────────────

def test_repo_default_panel_resolves():
    """真实 panels/default.yaml 无 extends, 应原样解析通过。"""
    resolved = pl.resolve_panel(pl.VAULT_ROOT / "panels" / "default.yaml")
    assert resolved["name"] == "default"
    assert resolved["scoring_mode"] == "weighted_average"
    assert any(j["slug"] == "tian" for j in resolved["judges"])
