"""test_study_weekly_summary.py — 学习小组周汇总 (M2)。

覆盖: 周归属 (周一归上周) / 默认目标周 / 花名册加载与三级识别 / collect (周过滤·同人取最新) /
渲染 (总表·⚠️短板·未交名单) / worker member+week 注入 / 「汇总」命令 dispatch。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

import gen_study_weekly_summary as g


def _score_row(member, total, week="2026-W29", label="标签"):
    base = total + 5
    return {"framework_version": "v10", "member": member, "week": week,
            "scores": {"anchor_problem": 15, "value_proof": 20, "decision_risk": 15,
                       "time_loop": 15, "growth": 10},
            "deductions": [{"slug": "d2_progress", "points": 5, "reason": "x"}],
            "base": base, "deducted": 5, "total": total, "grade": "B",
            "core_label": label, "position_value": f"{member}的岗位", "suggestions": ["建议1"]}


class TestWeekAttribution:
    def test_monday_rolls_back(self):
        assert g.attribute_week(dt.datetime(2026, 7, 20, 10)) == "2026-W29"   # 周一 → 上周
        assert g.attribute_week(dt.datetime(2026, 7, 21, 10)) == "2026-W30"   # 周二 → 当周
        assert g.attribute_week(dt.datetime(2026, 7, 19, 23)) == "2026-W29"   # 周日 → 当周

    def test_default_target_week(self):
        assert g.default_target_week(dt.datetime(2026, 7, 20, 20)) == "2026-W29"  # 周一晚 cron
        assert g.default_target_week(dt.datetime(2026, 7, 23, 12)) == "2026-W30"


class TestRoster:
    def test_load_real_roster_8_members(self):
        roster = g.load_roster()
        assert len(roster) == 8
        assert {m["name"] for m in roster} >= {"宋鹏飞", "张路", "陈晓雪"}

    def test_missing_file_fail_open(self, tmp_path):
        assert g.load_roster(tmp_path / "nope.yaml") == []

    def test_resolve_three_levels(self):
        roster = [{"name": "张路", "open_id": "ou_z", "position": ""},
                  {"name": "赵然", "open_id": "", "position": ""}]
        assert g.resolve_member(roster, open_id="ou_z") == "张路"
        assert g.resolve_member(roster, doc_title="赵然周报W29.md") == "赵然"
        assert g.resolve_member(roster, open_id="ou_unknown", doc_title="无名氏") == ""


class TestCollect:
    def _write(self, root, brand, data, mtime=None):
        d = root / brand
        d.mkdir(parents=True, exist_ok=True)
        p = d / "scores.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        if mtime:
            import os
            os.utime(p, (mtime, mtime))

    def test_filters_week_and_latest_wins(self, tmp_path):
        self._write(tmp_path, "study-weekly-1", _score_row("张路", 80), mtime=1000)
        self._write(tmp_path, "study-weekly-2", _score_row("张路", 88), mtime=2000)   # 同人取最新
        self._write(tmp_path, "study-weekly-3", _score_row("赵然", 70, week="2026-W28"))  # 别周
        self._write(tmp_path, "op2-x-1", _score_row("外人", 99))                      # 非本场景前缀
        rows = g.collect_week("2026-W29", tmp_path)
        assert len(rows) == 1 and rows[0]["total"] == 88

    def test_sorted_desc(self, tmp_path):
        self._write(tmp_path, "study-weekly-a", _score_row("甲", 60))
        self._write(tmp_path, "study-weekly-b", _score_row("乙", 90))
        rows = g.collect_week("2026-W29", tmp_path)
        assert [r["member"] for r in rows] == ["乙", "甲"]

    def test_collect_all_no_dedup_no_week_filter(self, tmp_path):
        """校准平铺: 同人多份都留 (不去重), 别周也留 (不按周过滤)。"""
        self._write(tmp_path, "study-weekly-1", _score_row("张路", 72, week="2026-W30"))
        self._write(tmp_path, "study-weekly-2", _score_row("张路", 63, week="2026-W29"))  # 同人别周
        self._write(tmp_path, "op2-x", _score_row("外人", 99))                          # 非本场景前缀
        rows = g.collect_all(tmp_path)
        assert len(rows) == 2 and {r["total"] for r in rows} == {72, 63}
        assert all(r["_brand"].startswith("study-weekly-") for r in rows)


class TestFlat:
    def test_flat_distribution_and_sort(self):
        rows = [_score_row("宋鹏飞", 19), _score_row("张路", 72), _score_row("胡婷婷", 43)]
        for r, gr in zip(rows, ["C-", "B", "C-"]):   # v7.21: 19→C-, 72→B, 43→C-
            r["grade"] = gr
        rows[0]["_brand"] = "study-weekly-b1"
        md = g.render_flat(rows, {"study-weekly-b1": "宋鹏飞周报.md"})
        # 等级分布 + 均分
        assert "B×1 / C-×2" in md and "均分 45" in md
        # 按总分升序: 宋鹏飞(19) 在张路(72) 之前
        assert md.index("宋鹏飞") < md.index("张路")
        assert "宋鹏飞周报" in md   # 文档名 join


class TestRender:
    def test_full_summary_shape(self):
        roster = g.load_roster()
        rows = [_score_row("张路", 88, label="闭环最好"), _score_row("赵然", 71)]
        md = g.render_summary("2026-W29", rows, roster)
        assert "总体评估结果" in md and "| 1 | 张路 |" in md and "闭环最好" in md
        assert "维度平均分与共性短板" in md and "⚠️" in md
        assert "逐人简评" in md and "张路的岗位" in md
        assert "未交名单" in md and "宋鹏飞" in md            # 未提交成员在名单里
        assert "张路" in md and "不作为绩效依据" in md

    def test_empty_week(self):
        md = g.render_summary("2026-W30", [], g.load_roster())
        assert "本周无提交" in md


class TestWorkerInjection:
    REVIEW = """---
framework_version: v10
scores: {anchor_problem: 15, value_proof: 18, decision_risk: 12, time_loop: 13, growth: 10}
deductions: []
core_label: 标签
---
"""

    def test_member_week_injected(self, tmp_path, monkeypatch):
        import review_worker as rw
        monkeypatch.setattr(rw, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(g, "load_roster",
                            lambda *a, **k: [{"name": "张路", "open_id": "ou_z", "position": ""}])
        brand = tmp_path / "reports" / "study-weekly-9"
        (brand / "reviews").mkdir(parents=True)
        (brand / "reviews" / "v8-coach.md").write_text(self.REVIEW, encoding="utf-8")
        assert rw._render_v8_report("study-weekly-9", {"submitter": "ou_z", "doc_name": "x.md"})
        data = json.loads((brand / "scores.json").read_text(encoding="utf-8"))
        assert data["member"] == "张路" and data["week"].startswith("202")


class TestSummaryDispatch:
    def test_command_generates_and_sends(self, monkeypatch, tmp_path):
        import feishu_events as fe
        sent, files = [], []
        monkeypatch.setattr(fe, "_safe_send", lambda rid, card, rt: sent.append(card))
        monkeypatch.setattr(fe.feishu_client, "send_file",
                            lambda rid, path, rt: files.append(path))
        monkeypatch.setattr(g, "OUT_DIR", tmp_path)
        monkeypatch.setattr(g, "collect_week", lambda w, *a: [_score_row("张路", 88)])
        r = fe._dispatch_study_weekly_summary("oc_1", "chat_id")
        assert r["status"] == "ok" and r["count"] == 1
        assert sent and files and files[0].is_file()
        card_text = json.dumps(sent[0], ensure_ascii=False)
        assert "张路" in card_text and "未交" in card_text
