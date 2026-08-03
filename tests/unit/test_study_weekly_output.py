"""test_study_weekly_output.py — 学习小组周报自省 v10 (v7.22) 输出 (M0)。

覆盖: 注册表完整性 (5 项扣分一维一项·各 1-2·上限10) / 等级阶梯 (A/B+/B/C/C-) /
区间校验 (越界拦截·算术机械化) / 六段式渲染 / frontmatter 解析 / 落盘 fail-close /
scene+panel 可加载 / 机械层样例 (总分算术+分档)。
注: v7.21 雅总金标准 (旧扣分制) 已不适用 v7.22, 校准金标准待雅总在新制下重评后补入。"""

from __future__ import annotations

import json

import pytest

import study_weekly_output as o


def _good_payload() -> o.V8Payload:
    p = o.V8Payload(member="张路", week="2026-W29")
    p.scores = {"anchor_problem": 19, "value_proof": 24, "decision_risk": 18,
                "time_loop": 18, "growth": 15}
    p.score_reasons = {"anchor_problem": "开篇三条关键结论直接锚定"}
    # v7.22: 每项 ≤2; 3 项各扣 2 → 合计 6, 保持 94−6=88 (B+) 不变
    p.deductions = [o.Deduction("d3_decision", 2, "卡点无 Plan B"),
                    o.Deduction("d2_progress", 2, "风险信号未独立成段"),
                    o.Deduction("d1_value", 2, "开篇未突出最重要一件事")]
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
        # v7.22: 5 项一维一项, 每项上限 2, 合计 10
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
        assert sum(hi for *_, hi, _ in o.DEDUCTIONS.values()) == 10   # v7.22: 上限合计 = 10

    def test_selfcheck_cli(self):
        assert o.main(["--selfcheck"]) == 0


class TestGrades:
    @pytest.mark.parametrize("total,grade", [
        (100, "A"), (90, "A"), (89, "B+"), (80, "B+"),
        (79, "B"), (70, "B"), (69, "C"), (60, "C"),
        (59, "C-"), (40, "C-"), (0, "C-")])     # v7.21: 5 档, <60=C-
    def test_ladder(self, total, grade):
        assert o.grade_for_total(total) == grade

    def test_no_d_grade(self):
        assert "D" not in {g for _, g, _ in o.GRADES}

    def test_has_c_minus(self):
        assert "C-" in {g for _, g, _ in o.GRADES}


class TestValidate:
    def test_good_payload_passes(self):
        assert o.validate_payload(_good_payload()) == []

    def test_dimension_over_max_blocked(self):
        p = _good_payload()
        p.scores["value_proof"] = 26          # 满分 25
        assert any("越界" in x for x in o.validate_payload(p))

    def test_missing_dimension_blocked(self):
        p = _good_payload()
        del p.scores["growth"]
        assert any("缺维度分" in x for x in o.validate_payload(p))

    def test_deduction_out_of_range_blocked(self):
        p = _good_payload()
        p.deductions = [o.Deduction("d5_cognition", 3, "x")]   # v7.22 区间 1-2, 3 越界
        assert any("扣分越界" in x for x in o.validate_payload(p))

    def test_deduction_min_1_ok(self):
        # 轻微扣分 1 分 (张路 维度3/4 各 1), 校验必须放行
        p = _good_payload()
        p.deductions = [o.Deduction("d4_time", 1, "缺上周对照")]
        assert o.validate_payload(p) == []

    def test_deduction_max_2_ok(self):
        # v7.22: 每项上限 2, 恰好 2 放行
        p = _good_payload()
        p.deductions = [o.Deduction("d3_decision", 2, "卡点无 Plan B")]
        assert o.validate_payload(p) == []

    def test_unknown_deduction_slug_blocked(self):
        p = _good_payload()
        p.deductions = [o.Deduction("no_plan_b", 2, "x")]   # 旧 v10 slug 已废
        assert any("未知扣分" in x for x in o.validate_payload(p))

    def test_duplicate_deduction_blocked(self):
        p = _good_payload()
        p.deductions = [o.Deduction("d3_decision", 2, "a"), o.Deduction("d3_decision", 1, "b")]
        assert any("重复" in x for x in o.validate_payload(p))

    def test_total_is_mechanical(self):
        # 总分不来自 LLM: 94 基础 − 6 扣 = 88
        base, ded, total = o.compute_total(_good_payload())
        assert (base, ded, total) == (94.0, 6.0, 88.0)
        assert o.grade_for_total(total) == "B+"


class TestRender:
    def test_six_sections_present(self):
        md = o.render_personal_report(_good_payload())
        for s in ["① 岗位价值判断", "② 5 维基础分", "③ 触发的反向扣分项",
                  "④ 总分与等级", "⑤ 改进建议", "⑥ 重写示例"]:
            assert s in md
        assert "94 − 6 = 88" in md and "B+" in md
        assert "不作为绩效依据" in md              # 红线页脚
        assert "核心贡献证明" in md and "19/20" in md   # v10 维度名

    def test_no_deductions_renders(self):
        p = _good_payload()
        p.deductions = []
        md = o.render_personal_report(p)
        assert "未触发任何反向扣分项" in md and "= 94" in md

    def test_report_shows_v7_22_label_not_v10(self):
        # 报告体人面版本标签 = 雅总口径 v7.22;内部契约 id (FRAMEWORK_VERSION) 仍是 v10。
        md = o.render_personal_report(_good_payload())
        assert "框架 v7.22" in md
        assert "框架 v10" not in md

    def test_report_has_grade_honesty_note(self):
        # 方案 ②+③: 单次评分 ±1 档波动的边界诚实标注常驻在 ④ 段之后。
        md = o.render_personal_report(_good_payload())
        assert o.GRADE_HONESTY_NOTE in md
        assert "档位仅作方向性参考" in md
        # 位置: 在 ④ 总分之后、⑤ 改进建议之前
        assert md.index("→ **") < md.index("档位仅作方向性参考") < md.index("## ⑤ 改进建议")


class TestFrameworkLabel:
    def test_label_is_v7_21(self):
        assert o.FRAMEWORK_LABEL == "v7.22"

    def test_internal_contract_id_unchanged(self):
        # 内部 frontmatter 契约 id 不动 (评委 SKILL.md 仍写 framework_version: v10)
        assert o.FRAMEWORK_VERSION == "v10"


class TestLoadAndWrite:
    REVIEW = """---
framework_version: v10
scores:
  anchor_problem: 17
  value_proof: 21
  decision_risk: 17
  time_loop: 16
  growth: 13
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
        assert (base, ded, total) == (84.0, 4.0, 80.0)   # v7.22: 84 − (2+2) = 80 → B+
        assert o.grade_for_total(total) == "B+"
        assert p.member == "何坚白" and p.core_label == "推进扎实"

    def test_write_outputs_and_scores_json(self, tmp_path):
        p = _good_payload()
        out = o.write_outputs(tmp_path / "b1", p)
        assert out.is_file()
        data = json.loads((tmp_path / "b1" / "scores.json").read_text(encoding="utf-8"))
        assert data["total"] == 88.0 and data["grade"] == "B+"
        assert data["member"] == "张路" and len(data["deductions"]) == 3

    def test_write_fail_close_on_invalid(self, tmp_path):
        p = _good_payload()
        p.scores["growth"] = 99
        with pytest.raises(ValueError):
            o.write_outputs(tmp_path / "b2", p)
        assert not (tmp_path / "b2" / "scores.json").exists()   # 不落脏数据


class TestMechanicalCases:
    """机械层锁死 (总分 = Σscores − Σdeductions, 阈值分档) —— 与框架版本无关。

    ⚠️ 注意: v7.21 的雅总金标准 (陈晓雪 75−11=64 C · 张路 89−4=85 B+) 是在旧扣分制
    (每项 6/6/6/6/5) 下手评的; v7.22 收紧为每项 1-2, 那套手评分不再适用。张路案例的扣分
    (d1=2/d3=1/d4=1) 恰好都 ≤2, v7.22 下仍合法, 保留作机械层样例; 陈晓雪原案扣分越 v7.22
    上限, 换成一个 v7.22 满扣 (5×2=10) 样例。**v7.22 的校准金标准需雅总在新制下重评后补入。**"""

    def _case(self, scores, deds):
        p = o.V8Payload()
        p.scores = scores
        p.deductions = [o.Deduction(s, pts, "") for s, pts in deds.items()]
        return p

    def test_max_deductions_10_v7_22(self):
        # v7.22 满扣: 5 项各 2 = 10; base 84 → 74 → B
        p = self._case(
            {"anchor_problem": 16, "value_proof": 21, "decision_risk": 16,
             "time_loop": 16, "growth": 15},
            {"d1_value": 2, "d2_progress": 2, "d3_decision": 2, "d4_time": 2, "d5_cognition": 2})
        assert o.validate_payload(p) == []
        base, ded, total = o.compute_total(p)
        assert (base, ded, total) == (84.0, 10.0, 74.0)
        assert o.grade_for_total(total) == "B"

    def test_zhanglu_89_4_85_Bplus(self):
        # 张路案例扣分 (d1=2/d3=1/d4=1) 全 ≤2, v7.22 下仍合法 → 89−4=85 B+
        p = self._case(
            {"anchor_problem": 17, "value_proof": 25, "decision_risk": 16,
             "time_loop": 16, "growth": 15},
            {"d1_value": 2, "d3_decision": 1, "d4_time": 1})
        assert o.validate_payload(p) == []
        base, ded, total = o.compute_total(p)
        assert (base, ded, total) == (89.0, 4.0, 85.0)
        assert o.grade_for_total(total) == "B+"


class TestReviewTextValidation:
    """Phase 4 就地校验 (validate_review_text): 畸形 YAML / 越界 / 未知 slug 都要拦,
    合法的放行 —— 让上层重试重生成, 不把带病 review 流到 verify 才 exit 5。"""

    GOOD = """---
framework_version: v10
scores: {anchor_problem: 17, value_proof: 25, decision_risk: 16, time_loop: 16, growth: 15}
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
        bad = self.GOOD.replace("points: 2", "points: 9")             # v7.22 d1 上限 2, 9 越界
        assert any("越界" in x for x in o.validate_review_text(bad))

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
