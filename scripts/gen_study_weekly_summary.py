#!/usr/bin/env python3
"""
gen_study_weekly_summary.py — 学习小组周汇总报告 (M2 · PRD §4.4 F4, 参照《8份周报评估报告》)

确定性生成 (零 LLM, 同日报/周报生成器范式): 扫 reports/study-weekly-*/scores.json,
按目标周聚合 → 总表排名 / 维度均分与共性短板 / 逐人简评 / 未交名单。
「核心标签」直接取个人评估的 core_label (个人评估时已产出, 汇总不再调 LLM)。

周归属规则 (D2 · 周一 20:00 截稿): 周一提交的周报归**上一周** (补交窗口),
周二~周日归当周。默认目标周 = 昨天所在 ISO 周 (周一跑 = 上一周)。

花名册: config/study_weekly_roster.yaml — 身份识别 (open_id → 姓名 → 文档标题含姓名)
与未交名单的分母。

用法:
  python3 scripts/gen_study_weekly_summary.py                # 目标周=昨天所在周, dry 打印
  python3 scripts/gen_study_weekly_summary.py --week 2026-W29 --write
输出: writing/study-weekly-summary-<week>.md
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
from pathlib import Path
from typing import Optional

VAULT_ROOT = Path(__file__).parent.parent.resolve()
REPORTS_DIR = VAULT_ROOT / "reports"
ROSTER_PATH = VAULT_ROOT / "config" / "study_weekly_roster.yaml"
OUT_DIR = VAULT_ROOT / "writing"
BRAND_PREFIX = "study-weekly-"

# 维度短板的固定诊断话术 (v8 立即改进动作的组级版; 零 LLM)
_DIM_DIAG = {
    "anchor_problem": "开篇岗位价值提炼不够, 建议人人加 1-2 句\"我的岗位价值是XX\"",
    "value_proof": "产出可衡量性不足, 定性描述偏多 — 补金额/时间/比例/数量",
    "decision_risk": "决策推动与风险揭示不足, 缺 Ownership — 补\"需谁决策/DDL/风险五要素\"",
    "time_loop": "上周计划完成度与归因最弱 — 加\"上周计划完成情况\"小节 + 具体日期",
    "growth": "方法沉淀不足或与工作脱节 — 只写能复用的学习 + 应用场景",
}


def attribute_week(dt: _dt.datetime) -> str:
    """提交时间 → 归属 ISO 周 (周一归上一周, D2 补交窗口)。"""
    d = dt.date()
    if d.isoweekday() == 1:
        d -= _dt.timedelta(days=7)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def default_target_week(now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now()
    y, w, _ = (now.date() - _dt.timedelta(days=1)).isocalendar()
    return f"{y}-W{w:02d}"


def load_roster(path: Path = ROSTER_PATH) -> list[dict]:
    """→ [{name, open_id, position}]; 缺/坏文件 → [] (fail-open, 未交名单降级为空)。"""
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        out = []
        for m in data.get("members") or []:
            if isinstance(m, dict) and m.get("name"):
                out.append({"name": str(m["name"]), "open_id": str(m.get("open_id") or ""),
                            "position": str(m.get("position") or "")})
        return out
    except Exception:  # noqa: BLE001
        return []


def resolve_member(roster: list[dict], *, open_id: str = "", doc_title: str = "") -> str:
    """身份三级识别 (PRD F3): open_id → 文档标题含姓名 → 空 (上层询问)。"""
    for m in roster:
        if open_id and m["open_id"] and m["open_id"] == open_id:
            return m["name"]
    for m in roster:
        if doc_title and m["name"] in doc_title:
            return m["name"]
    return ""


def collect_week(week: str, reports_dir: Path = REPORTS_DIR) -> list[dict]:
    """扫 study-weekly-* 的 scores.json, 取目标周; 同人多份取最新 (mtime)。"""
    rows: dict[str, tuple[float, dict]] = {}
    anon = 0
    if not reports_dir.is_dir():
        return []
    for d in sorted(reports_dir.iterdir()):
        if not d.is_dir() or not d.name.startswith(BRAND_PREFIX):
            continue
        sj = d / "scores.json"
        if not sj.is_file():
            continue
        try:
            data = json.loads(sj.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if data.get("week") != week:
            continue
        member = data.get("member") or ""
        if not member:
            anon += 1
            member = f"未识别成员#{anon}"
        mt = sj.stat().st_mtime
        if member not in rows or mt > rows[member][0]:
            rows[member] = (mt, data)
    return [v for _, v in sorted(rows.values(), key=lambda t: -t[1].get("total", 0))]


def collect_all(reports_dir: Path = REPORTS_DIR) -> list[dict]:
    """扫所有 study-weekly-* 的 scores.json (不按周过滤, 不去重) — 校准平铺用。"""
    out: list[dict] = []
    if not reports_dir.is_dir():
        return out
    for d in sorted(reports_dir.iterdir()):
        if not d.is_dir() or not d.name.startswith(BRAND_PREFIX):
            continue
        sj = d / "scores.json"
        if not sj.is_file():
            continue
        try:
            data = json.loads(sj.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        data["_brand"] = d.name
        out.append(data)
    return out


def brand_docnames() -> dict[str, str]:
    """从 done 队列读 brand_slug → doc_name (给平铺表标注是谁哪周的文档)。best-effort。"""
    out: dict[str, str] = {}
    try:
        import review_queue
        done = review_queue.DEFAULT_QUEUE_ROOT / "done"
        if done.is_dir():
            for jf in done.glob("RJ-*.json"):
                try:
                    j = json.loads(jf.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    continue
                if j.get("brand_slug"):
                    out[j["brand_slug"]] = j.get("doc_name") or ""
    except Exception:  # noqa: BLE001
        pass
    return out


_GRADE_ORDER = ["A", "B+", "B", "C", "C-"]   # v7.21 第三版: 取消 D, 低端由 C- 承接


def render_flat(rows: list[dict], docnames: dict[str, str]) -> str:
    """校准平铺表: 每份一行 (不去重), 按总分升序 (最严在上, 便于核口径) + 等级分布。"""
    rows_sorted = sorted(rows, key=lambda r: r.get("total", 0))
    dist = {g: 0 for g in _GRADE_ORDER}
    for r in rows:
        if r.get("grade") in dist:
            dist[r["grade"]] += 1
    n = len(rows)
    mean = (sum(r.get("total", 0) for r in rows) / n) if n else 0
    dist_str = " / ".join(f"{g}×{dist[g]}" for g in _GRADE_ORDER if dist[g]) or "—"
    from study_weekly_output import FRAMEWORK_LABEL as _fv   # 人面显示版本 (雅总口径 v7.22)
    lines = [
        f"# 学习小组周报 · {_fv} 校准平铺表 (试评)",
        "",
        f"> {n} 份周报全列 (不去重, 每人每周都在) · {_fv} 自省式诊断 · 评分不作绩效依据",
        f"> **等级分布**: {dist_str} · 均分 {mean:.0f}",
        "",
        "| 姓名 | 文档 (含周) | 基础分 | 反向扣分 | 总分 | 等级 | 核心标签 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows_sorted:
        doc = (docnames.get(r.get("_brand", ""), "") or "").rsplit(".md", 1)[0]
        lines.append(
            f"| {r.get('member') or '(未识别)'} | {doc} | {r.get('base', 0):.0f} "
            f"| −{r.get('deducted', 0):.0f} | **{r.get('total', 0):.0f}** "
            f"| {r.get('grade', '')} | {r.get('core_label', '')} |")
    if not rows:
        lines.append("| — | (无报告) | | | | | |")
    lines += ["", f"*gen_study_weekly_summary.py --flat · 按总分升序 · 框架 {_fv} (雅总定稿) · "
              "评分与等级不进任何绩效口径*", ""]
    return "\n".join(lines)


def render_summary(week: str, rows: list[dict], roster: list[dict]) -> str:
    from study_weekly_output import DIMENSIONS, DEDUCTIONS, FRAMEWORK_LABEL as _fv, grade_for_total
    n = len(rows)
    lines = [
        f"# 学习小组周报评估汇总 · {week}",
        "",
        f"> 评估方法: {_fv} 自省式量化诊断 (5 维基础分 100 + {len(DEDUCTIONS)} 项反向扣分) · "
        f"确定性汇总 (零 LLM) · 自省式诊断, 不作为绩效依据",
        f"> 本周收到 {n} 份周报" + (f" / 小组 {len(roster)} 人" if roster else ""),
        "",
        "## 一、总体评估结果",
        "",
        "| 排名 | 成员 | 5 维基础分 | 反向扣分 | 最终总分 | 等级 | 核心标签 |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {r.get('member','')} | {r.get('base',0):.0f} | {r.get('deducted',0):.0f} "
            f"| {r.get('total',0):.0f} | {r.get('grade') or grade_for_total(r.get('total',0))} "
            f"| {r.get('core_label','')} |")
    if not rows:
        lines.append("| — | (本周无提交) | | | | | |")
    # 维度均分与共性短板
    lines += ["", "## 二、维度平均分与共性短板", "",
              "| 维度 | 小组平均分 | 诊断 |", "|---|---|---|"]
    if rows:
        avgs = []
        for slug, cn, mx in DIMENSIONS:
            vals = [float(r.get("scores", {}).get(slug, 0)) for r in rows]
            avgs.append((slug, cn, mx, sum(vals) / len(vals)))
        weakest = sorted(avgs, key=lambda t: t[3] / t[2])[:2]
        weak_slugs = {t[0] for t in weakest}
        for slug, cn, mx, avg in avgs:
            mark = "⚠️ " if slug in weak_slugs else ""
            lines.append(f"| {cn} | {avg:.1f}/{mx} | {mark}{_DIM_DIAG[slug] if slug in weak_slugs else '—'} |")
    else:
        lines.append("| — | — | — |")
    # 逐人简评
    lines += ["", "## 三、逐人简评", ""]
    for r in rows:
        dims = " · ".join(
            f"{cn}{float(r.get('scores', {}).get(slug, 0)):.0f}/{mx}"
            for slug, cn, mx in DIMENSIONS)
        lines += [f"### {r.get('member','')}({r.get('grade','')}, {r.get('total',0):.0f} 分)", ""]
        if r.get("position_value"):
            lines.append(f"岗位价值: {r['position_value']}")
        lines.append(f"5 维: {dims} · 扣分合计 −{r.get('deducted',0):.0f}")
        sugs = r.get("suggestions") or []
        if sugs:
            lines.append("改进建议: " + "; ".join(str(s) for s in sugs[:3]))
        lines.append("")
    if not rows:
        lines += ["(本周无提交)", ""]
    # 未交名单
    if roster:
        submitted = {r.get("member") for r in rows}
        missing = [m["name"] for m in roster if m["name"] not in submitted]
        lines += ["## 四、未交名单", "",
                  ("、".join(missing) if missing else "无 — 全员已交 🎉"), ""]
    lines += ["---", "", f"*由 gen_study_weekly_summary.py 确定性生成 · 框架 {_fv} (雅总定稿) · "
              "评分与等级不进任何绩效口径*", ""]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="学习小组周汇总报告")
    ap.add_argument("--week", help="目标 ISO 周 (如 2026-W29); 缺省=昨天所在周")
    ap.add_argument("--write", action="store_true", help="写 writing/ (缺省只打印)")
    ap.add_argument("--flat", action="store_true",
                    help="校准平铺表: 所有 study-weekly 报告全列, 不去重不按周 (试评校准用)")
    args = ap.parse_args(argv)

    if args.flat:
        rows = collect_all()
        md = render_flat(rows, brand_docnames())
        if args.write:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            out = OUT_DIR / "study-weekly-calibration-flat.md"
            out.write_text(md, encoding="utf-8")
            print(f"✅ {out} — {len(rows)} 份 (平铺, 不去重)")
        else:
            print(md)
        return 0

    week = args.week or default_target_week()
    if not re.match(r"^\d{4}-W\d{2}$", week):
        print(f"❌ 非法周格式: {week} (应如 2026-W29)")
        return 2
    roster = load_roster()
    rows = collect_week(week)
    md = render_summary(week, rows, roster)
    if args.write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUT_DIR / f"study-weekly-summary-{week}.md"
        out.write_text(md, encoding="utf-8")
        print(f"✅ {out} — {len(rows)} 份")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
