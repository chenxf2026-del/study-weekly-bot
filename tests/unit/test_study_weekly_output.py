"""test_study_weekly_output.py — 学习小组周报点评 v12.1 输出 (M0)。

覆盖: 注册表完整性 (5 项扣分一维一项·各 1-2·全局≤3项/≤5分·离散档位集) /
等级阶梯 (A/B+/B/C, 无 C-) / 区间校验 (离散档位·全局扣分上限·越界拦截) /
总分 clamp [65,95] (封底/封顶) / 六段式渲染 / frontmatter 解析 / 落盘 fail-close /
scene+panel 可加载 / 机械层样例 (clamp 算术 + 分档)。"""

from __future__ import annotations

import json

import pytest

import study_weekly_output as o


def _good_payload() -> o.V8Payload:
    p = o.V8Payload(member="张路", week="2026-W29")
    # 全部落在各维档位集 (契约一); base = 16+20+16+16+12 = 80
    p.scores = {"anchor_problem": 16, "value_proof": 20, "decision_risk": 16,
                "time_loop": 16, "growth": 12}
    p.score_reasons = {"anchor_problem": "开篇三条关键结论直接锚定"}
    # v12.1: 3 项 (≤3), 累计 5 (≤5) → 80 − 5 = 75 (B)
    p.deductions = [o.Deduction("d3_decision", 2, "卡点无 Plan B"),
                    o.Deduction("d2_progress", 2, "风险信号未独立成段"),
                    o.Deduction("d1_value", 1, "开篇未突出最重要一件事")]
    p.position_value = "构建智能体产品线与评审体系"
    p.core_label = "闭环最好"
    p.suggestions = ["为每个卡点补 Plan B", "增加上周关注点对照"]
    p.rewrite_example = "原句: 高效推进 → 改写: 7/25 前交付 X, 预计节省 Y 小时/周"
    return p


class TestRegistry:
    def test_dimensions_sum_100(self):
        assert len(o.DIMENSIONS) == 5
        assert sum(mx for _, _, mx in o.DIMENSIONS) == 100

    def test_five_deductions_one_per_dim(self):
        assert len(o.DEDUCTIONS) == 5
        expect = {"d1_value": ("anchor_problem", 2), "d2_progress": ("value_proof", 2),
                  "d3_decision": ("decision_risk", 2), "d4_time": ("time_loop", 2),
                  "d5_cognition": ("growth", 2)}
        assert set(expect) == set(o.DEDUCTIONS)
        dims = set()
        for slug, (dim, hi) in expect.items():
            label, rdim, lo, rhi, act = o.DEDUCTIONS[slug]
            assert rdim == dim and rhi == hi and lo == 1, slug
            assert label and act
            dims.add(rdim)
        assert dims == set(o.DIM_MAX)                       # 恰好覆盖 5 维

    def test_discrete_value_sets(self):
        # 契约一: 每维恰 5 个档位值 = 满分×{1..5}/5
        for slug, _cn, mx in o.DIMENSIONS:
            assert o.DIM_ALLOWED[slug] == {mx // 5 * k for k in range(1, 6)}
            assert len(o.DIM_ALLOWED[slug]) == 5

    def test_global_deduction_caps(self):
        assert o.DEDUCTION_MAX_ITEMS == 3 and o.DEDUCTION_TOTAL_CAP == 5

    def test_clamp_window(self):
        assert (o.FINAL_FLOOR, o.FINAL_CAP) == (65.0, 95.0)

    def test_selfcheck_cli(self):
        assert o.main(["--selfcheck"]) == 0


class TestGrades:
    @pytest.mark.parametrize("total,grade", [
        (95, "A"), (90, "A"), (89, "B+"), (80, "B+"),
        (79, "B"), (70, "B"), (69, "C"), (65, "C"),
        (64, "C"), (0, "C")])       # v12.1: 4 档, <65 (理论上被 clamp) 也归 C
    def test_ladder(self, total, grade):
        assert o.grade_for_total(total) == grade

    def test_no_d_grade(self):
        assert "D" not in {g for _, g, _ in o.GRADES}

    def test_no_c_minus(self):
        assert "C-" not in {g for _, g, _ in o.GRADES}
        assert {g for _, g, _ in o.GRADES} == {"A", "B+", "B", "C"}


class TestValidate:
    def test_good_payload_passes(self):
        assert o.validate_payload(_good_payload()) == []

    def test_dimension_non_discrete_blocked(self):
        p = _good_payload()
        p.scores["value_proof"] = 17          # 在 [0,25] 但非档位值
        assert any("档位" in x for x in o.validate_payload(p))

    def test_dimension_over_max_blocked(self):
        p = _good_payload()
        p.scores["value_proof"] = 26          # 超满分, 也非档位
        assert any("档位" in x for x in o.validate_payload(p))

    def test_missing_dimension_blocked(self):
        p = _good_payload()
        del p.scores["growth"]
        assert any("缺维度分" in x for x in o.validate_payload(p))

    def test_deduction_out_of_range_blocked(self):
        p = _good_payload()
        p.deductions = [o.Deduction("d5_cognition", 3, "x")]   # 单项区间 1-2, 3 越界
        assert any("扣分越界" in x for x in o.validate_payload(p))

    def test_deduction_min_1_ok(self):
        p = _good_payload()
        p.deductions = [o.Deduction("d4_time", 1, "缺上周对照")]
        assert o.validate_payload(p) == []

    def test_deduction_max_2_ok(self):
        p = _good_payload()
        p.deductions = [o.Deduction("d3_decision", 2, "卡点无 Plan B")]
        assert o.validate_payload(p) == []

    def test_unknown_deduction_slug_blocked(self):
        p = _good_payload()
        p.deductions = [o.Deduction("no_plan_b", 2, "x")]      # 旧 slug 已废
        assert any("未知扣分" in x for x in o.validate_payload(p))

    def test_duplicate_deduction_blocked(self):
        p = _good_payload()
        p.deductions = [o.Deduction("d3_decision", 2, "a"), o.Deduction("d3_decision", 1, "b")]
        assert any("重复" in x for x in o.validate_payload(p))

    def test_deduction_total_cap_blocked(self):
        # v12.1: 累计 ≤5; 2+2+2=6 越限
        p = _good_payload()
        p.deductions = [o.Deduction("d1_value", 2, "a"), o.Deduction("d2_progress", 2, "b"),
                        o.Deduction("d3_decision", 2, "c")]
        assert any("累计超限" in x for x in o.validate_payload(p))

    def test_deduction_too_many_items_blocked(self):
        # v12.1: 触发项 ≤3; 4 项越限 (即便累计 4 分)
        p = _good_payload()
        p.deductions = [o.Deduction("d1_value", 1, "a"), o.Deduction("d2_progress", 1, "b"),
                        o.Deduction("d3_decision", 1, "c"), o.Deduction("d4_time", 1, "d")]
        assert any("项超限" in x for x in o.validate_payload(p))

    def test_total_is_mechanical(self):
        # 总分不来自 LLM: 80 基础 − 5 扣 = 75 → B
        base, ded, total = o.compute_total(_good_payload())
        assert (base, ded, total) == (80.0, 5.0, 75.0)
        assert o.grade_for_total(total) == "B"


class TestRender:
    def test_six_sections_present(self):
        md = o.render_personal_report(_good_payload())
        for s in ["① 岗位价值判断", "② 5 维基础分", "③ 触发的反向扣分项",
                  "④ 总分与等级", "⑤ 改进建议", "⑥ 重写示例"]:
            assert s in md
        assert "80 − 5 = 75" in md and "B" in md
        assert "不作为绩效依据" in md              # 红线页脚
        assert "核心贡献证明" in md and "16/20" in md   # 维度名 + 档位分

    def test_no_deductions_renders(self):
        p = _good_payload()
        p.deductions = []
        md = o.render_personal_report(p)
        assert "未触发任何反向扣分项" in md and "= 80" in md

    def test_clamp_floor_renders(self):
        # 原始分 15 → 封底 65 (C)
        p = _good_payload()
        p.scores = {"anchor_problem": 4, "value_proof": 5, "decision_risk": 4,
                    "time_loop": 4, "growth": 3}
        p.deductions = [o.Deduction("d1_value", 2, "a"), o.Deduction("d2_progress", 2, "b"),
                        o.Deduction("d3_decision", 1, "c")]
        assert o.validate_payload(p) == []
        base, ded, total = o.compute_total(p)
        assert (base, ded, total) == (20.0, 5.0, 65.0)
        md = o.render_personal_report(p)
        assert "封底 65" in md and "→ **C**" in md

    def test_clamp_cap_renders(self):
        # 原始分 100 → 封顶 95 (A)
        p = _good_payload()
        p.scores = {"anchor_problem": 20, "value_proof": 25, "decision_risk": 20,
                    "time_loop": 20, "growth": 15}
        p.deductions = []
        assert o.validate_payload(p) == []
        base, ded, total = o.compute_total(p)
        assert (base, ded, total) == (100.0, 0.0, 95.0)
        md = o.render_personal_report(p)
        assert "封顶 95" in md and "→ **A**" in md

    def test_report_shows_v12_label(self):
        md = o.render_personal_report(_good_payload())
        assert "框架 v12" in md
        assert "框架 v10" not in md and "框架 v7.22" not in md

    def test_report_has_grade_honesty_note(self):
        md = o.render_personal_report(_good_payload())
        assert o.GRADE_HONESTY_NOTE in md
        assert "档位仅作方向性参考" in md
        # 位置: 在 ④ 总分之后、⑤ 改进建议之前
        assert md.index("→ **") < md.index("档位仅作方向性参考") < md.index("## ⑤ 改进建议")


class TestFrameworkLabel:
    def test_label_is_v12(self):
        assert o.FRAMEWORK_LABEL == "v12"

    def test_internal_contract_id_is_v12_1(self):
        # 内部 frontmatter 契约 id (评委 SKILL.md 写 framework_version: v12.1)
        assert o.FRAMEWORK_VERSION == "v12.1"


class TestLoadAndWrite:
    REVIEW = """---
framework_version: v12.1
scores:
  anchor_problem: 16
  value_proof: 20
  decision_risk: 16
  time_loop: 16
  growth: 12
score_reasons:
  anchor_problem: 四条核心结论开篇明确
deductions:
  - slug: d3_decision
    points: 2
    reason: 关键卡点无 Plan B
  - slug: d4_time
    points: 2
    reason: 缺上周关注点对照
core_label: 推进扎实
position_value: 推动国际业务落地
suggestions:
  - 给关键事项补 DDL 与 Plan B
rewrite_example: 原句X → 改写Y
---

## 详评正文
(prose)
"""

    def test_load_review_payload(self, tmp_path):
        f = tmp_path / "v8-coach.md"
        f.write_text(self.REVIEW, encoding="utf-8")
        p = o.load_review_payload(f, member="何坚白", week="2026-W29")
        assert o.validate_payload(p) == []
        base, ded, total = o.compute_total(p)
        assert (base, ded, total) == (80.0, 4.0, 76.0)   # v12.1: 80 − (2+2) = 76 → B
        assert o.grade_for_total(total) == "B"
        assert p.member == "何坚白" and p.core_label == "推进扎实"

    def test_write_outputs_and_scores_json(self, tmp_path):
        p = _good_payload()
        out = o.write_outputs(tmp_path / "b1", p)
        assert out.is_file()
        data = json.loads((tmp_path / "b1" / "scores.json").read_text(encoding="utf-8"))
        assert data["total"] == 75.0 and data["grade"] == "B"
        assert data["member"] == "张路" and len(data["deductions"]) == 3

    def test_write_fail_close_on_invalid(self, tmp_path):
        p = _good_payload()
        p.scores["growth"] = 99
        with pytest.raises(ValueError):
            o.write_outputs(tmp_path / "b2", p)
        assert not (tmp_path / "b2" / "scores.json").exists()   # 不落脏数据


class TestMechanicalCases:
    """机械层锁死 (总分 = clamp(Σscores − Σdeductions, 65, 95), 阈值分档)。"""

    def _case(self, scores, deds):
        p = o.V8Payload()
        p.scores = scores
        p.deductions = [o.Deduction(s, pts, "") for s, pts in deds.items()]
        return p

    def test_deduction_cap_5(self):
        # 满扣 (3 项累计 5): base 83 → 78 → B
        p = self._case(
            {"anchor_problem": 16, "value_proof": 20, "decision_risk": 16,
             "time_loop": 16, "growth": 15},
            {"d1_value": 2, "d2_progress": 2, "d3_decision": 1})
        assert o.validate_payload(p) == []
        base, ded, total = o.compute_total(p)
        assert (base, ded, total) == (83.0, 5.0, 78.0)
        assert o.grade_for_total(total) == "B"

    def test_clamp_floor_to_65(self):
        # 原始分 35 → 封底 65 → C
        p = self._case(
            {"anchor_problem": 8, "value_proof": 10, "decision_risk": 8,
             "time_loop": 8, "growth": 6},
            {"d1_value": 2, "d2_progress": 2, "d3_decision": 1})
        assert o.validate_payload(p) == []
        base, ded, total = o.compute_total(p)
        assert (base, ded, total) == (40.0, 5.0, 65.0)
        assert o.grade_for_total(total) == "C"

    def test_clamp_cap_to_95(self):
        # 满分 100 → 封顶 95 → A
        p = self._case(
            {"anchor_problem": 20, "value_proof": 25, "decision_risk": 20,
             "time_loop": 20, "growth": 15}, {})
        assert o.validate_payload(p) == []
        base, ded, total = o.compute_total(p)
        assert (base, ded, total) == (100.0, 0.0, 95.0)
        assert o.grade_for_total(total) == "A"


class TestReviewTextValidation:
    """Phase 4 就地校验 (validate_review_text): 畸形 YAML / 越界 / 未知 slug / 非档位分都要拦,
    合法的放行 —— 让上层重试重生成, 不把带病 review 流到 verify 才 exit 5。"""

    GOOD = """---
framework_version: v12.1
scores: {anchor_problem: 16, value_proof: 20, decision_risk: 16, time_loop: 16, growth: 15}
deductions:
  - slug: d1_value
    points: 2
    reason: 开篇三件并列
core_label: 工程强
---
prose
"""

    def test_good_text_passes(self):
        assert o.validate_review_text(self.GOOD) == []

    def test_no_frontmatter_flagged(self):
        assert o.validate_review_text("just prose, no frontmatter") != []

    def test_unknown_slug_flagged(self):
        bad = self.GOOD.replace("slug: d1_value", "slug: no_plan_b")   # 旧 slug 已废
        assert any("未知扣分" in x for x in o.validate_review_text(bad))

    def test_out_of_range_points_flagged(self):
        bad = self.GOOD.replace("points: 2", "points: 9")             # 单项上限 2, 9 越界
        assert any("越界" in x for x in o.validate_review_text(bad))

    def test_non_discrete_score_flagged(self):
        bad = self.GOOD.replace("anchor_problem: 16", "anchor_problem: 17")   # 非档位值
        assert any("档位" in x for x in o.validate_review_text(bad))

    def test_missing_dimension_flagged(self):
        bad = self.GOOD.replace(", growth: 15", "")                   # 缺 growth
        assert any("缺维度分" in x for x in o.validate_review_text(bad))


class TestSceneWiring:
    def test_scene_and_panel_load(self):
        import scene_loader
        import panel_loader
        cfg = scene_loader.load_scene("study-weekly-reflect")
        assert cfg.feishu.bot_name == "学习小组周报评估"
        assert cfg.report.anchor_judge is None
        panel = panel_loader.resolve_panel(cfg.panel_path)
        judges = panel["judges"]
        assert len(judges) == 1 and judges[0]["slug"] == "v8-coach"
        assert panel_loader.total_max_score(panel) == 100
        # lens slugs 与输出模块的 5 维注册表一致 (契约)
        lens_slugs = [l["slug"] for l in panel["scoring_lenses"]]
        assert lens_slugs == [s for s, _, _ in o.DIMENSIONS]
