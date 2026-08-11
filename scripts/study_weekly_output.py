#!/usr/bin/env python3
"""
study_weekly_output.py — 学习小组周报点评 v12.1 个人诊断输出 (M0 · PRD §4.2)

框架源: 《周报点评标准 v12.1》(doctrine 全文见
scenes/study-weekly-reflect/judges/v8-coach/SKILL.md)。

设计 (与 cadre_weekly_output 同范式, 纯函数可测):
  - DEDUCTIONS: 5 项反向扣分注册表 (一维一项, slug/标签/维度/区间/改进动作) —— 机器可读版。
  - DIM_ALLOWED: v12.1 契约一 (离散映射) —— 每维得分只能取 5 个档位值, 校验即拦非档位分。
  - validate_payload(): 区间校验 (维度分为合法档位 / 扣分 slug 合法且在区间 / 全局扣分上限 /
    类型正确), 越界即返回问题清单 —— **落盘前必须为空** (PRD G4)。
  - 总分与等级不信任 LLM 算术: total/grade 一律由 compute_total()/grade_for_total()
    从 scores/deductions 机械计算; 总分按 v12.1 契约五/六 clamp 到 [65, 95]。
  - render_personal_report(): 六段式 md 确定性渲染。
  - load_review_payload(): 解析评委 review frontmatter → payload。
  - write_scores_json(): 结构化落盘 (周汇总 gen_study_weekly_summary 的数据源)。

CLI:
  python3 scripts/study_weekly_output.py <brand>          # 从 reports/<brand>/ 渲染
  python3 scripts/study_weekly_output.py --selfcheck      # 注册表完整性自检
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

VAULT_ROOT = Path(__file__).parent.parent.resolve()
REPORTS_DIR = VAULT_ROOT / "reports"

FRAMEWORK_VERSION = "v12.1"     # 内部 frontmatter 契约 id (评委 SKILL.md 写 framework_version: v12.1)
# 人面显示版本 = 点评标准口径。内部契约 id 用 v12.1 与文档一致; 报告体/汇总表面向成员显示 v12。
FRAMEWORK_LABEL = "v12"

# v12.1 契约五/六 (满分封印 + 低分保护): 最终总分强制 clamp 到 [65, 95]。
FINAL_FLOOR, FINAL_CAP = 65.0, 95.0

# 单次 AI 评分的边界诚实标注 (方案 ②+③ · 2026-07-22): temp 0 实测评委仍非确定, 方差 ~±5 分,
# 边界分 (阈值 65/70/80/90) 附近单次会翻档。噪声近 ±5 → 几乎任一分都贴边界, 故不做"仅近边界才
# 提示"的条件判断, 而是常驻声明: 档位方向性参考, 价值在诊断内容。
GRADE_HONESTY_NOTE = (
    "> ⚠️ 评分为**单次 AI 自省诊断**, 边界分附近约有 ±1 档波动; "
    "请以 ②–⑥ 的诊断内容为改进依据, **档位仅作方向性参考, 不作绩效**。"
)

# ─── 五维 (slug → (中文名, 满分)) · 顺序即 v12 顺序 ────────────────────
# v12.1: 名称与权重与 v10 一致 (20/25/20/20/15)。
DIMENSIONS: list[tuple[str, str, int]] = [
    ("anchor_problem", "核心贡献证明", 20),
    ("value_proof", "关键进展与信号", 25),
    ("decision_risk", "决策推动与卡点", 20),
    ("time_loop", "时间节奏与闭环", 20),
    ("growth", "认知更新", 15),
]
DIM_MAX = {slug: mx for slug, _, mx in DIMENSIONS}
DIM_CN = {slug: cn for slug, cn, _ in DIMENSIONS}

# ─── 契约一 (离散映射) · 每维得分只能取 5 个档位值 (满分×{1..5}/5) ────────
# v12.1 5 分制映射: 1..5 档 → 满分×档/5。各维满分均可被 5 整除 (20/25/20/20/15 → 步长 4/5/4/4/3),
# 故档位集是整数。校验层拦非档位分 (如 17), 触发上层重试 —— 杜绝插值/小数导致的评分抖动。
DIM_ALLOWED: dict[str, set[int]] = {
    slug: {mx // 5 * k for k in range(1, 6)} for slug, _, mx in DIMENSIONS
}

# ─── 5 项反向扣分注册表 (v12.1 §二 "5 项各 1-2 分, 一维一项") ──────────────
# slug → (问题表述, 所属维度 slug, 扣分下限, 上限, 立即改进动作)
# v12.1 变更 (相对 v7.22): 单项区间不变 (各 1-2), 但**全局收紧** —— 触发项 ≤ 3, 累计扣分 ≤ 5
#   (见 DEDUCTION_MAX_ITEMS / DEDUCTION_TOTAL_CAP)。触发即扣 (轻微 1 / 明显 2), 未触发不列入 (=0)。
#   **区分度主要由基础分承担, 扣分只作小幅校正**; 全局上限确保总分不因扣分断崖 (再叠 clamp 65 封底)。
DEDUCTIONS: dict[str, tuple[str, str, int, int, str]] = {
    "d1_value": ("价值证明不足或过度包装", "anchor_problem", 1, 2,
                 "开篇 3 句话锁定本周核心贡献: 最大成果 + 最大风险/卡点 + 关键决策; "
                 "去掉\"高效推进\"\"显著提升\"等不可验证形容词"),
    "d2_progress": ("关键进展与信息缺失", "value_proof", 1, 2,
                    "每事项给\"关键进展/信息增量/风险信号/协同价值\", 重要项目补状态更新"),
    "d3_decision": ("决策推动与卡点揭示不足", "decision_risk", 1, 2,
                    "每重大事项写 决策点 + 决策人 + DDL + 卡点 + 影响 + 建议 + Plan B"),
    "d4_time": ("时间节奏与闭环管理不足", "time_loop", 1, 2,
                "阶段定位 + 节奏 + \"上周关注点→本周进展\"周期间对照, 时间改具体日期"),
    "d5_cognition": ("认知更新不足", "growth", 1, 2,
                     "写\"我因此更新了什么判断 / 什么方法可复用\", 带应用场景"),
}

# v12.1 §二 全局限制 (防抖关键): 全篇触发扣分项数 / 累计扣分, 双上限。
DEDUCTION_MAX_ITEMS = 3      # 触发项 ≤ 3
DEDUCTION_TOTAL_CAP = 5      # 累计扣分 ≤ 5

# ─── 等级 (v12.1 §三 · 4 档 A/B+/B/C · 对应 clamp 后的 65~95) ──────────────
# v12.1 取消 <60 的 C-: 任何低于 65 的原始分都被 clamp 托底到 65, 归入 C 档 (65-69)。
GRADES: list[tuple[int, str, str]] = [
    (90, "A", "完成度高, 有少量可优化细节"),
    (80, "B+", "整体良好, 稍有改进空间"),
    (70, "B", "中等水平, 建议对照标准优化"),
    (65, "C", "核心内容有缺失, 需重点调整"),
]


def grade_for_total(total: float) -> str:
    for floor, grade, _ in GRADES:
        if total >= floor:
            return grade
    return "C"


def grade_meaning(grade: str) -> str:
    for _, g, meaning in GRADES:
        if g == grade:
            return meaning
    return ""


# ─── payload ─────────────────────────────────────────────────────────

@dataclass
class Deduction:
    slug: str
    points: float
    reason: str = ""


@dataclass
class V8Payload:
    """一份周报的 v8 结构化评估 (机器可读真源)。"""
    member: str = ""                       # 成员姓名 (roster/识别, M2 前可空)
    week: str = ""                         # ISO 周, 如 2026-W29 (M2 前可空)
    framework_version: str = FRAMEWORK_VERSION
    scores: dict = field(default_factory=dict)          # slug → 分
    score_reasons: dict = field(default_factory=dict)   # slug → 一句话理由
    deductions: list = field(default_factory=list)      # [Deduction]
    position_value: str = ""               # ① 岗位价值判断
    core_label: str = ""                   # 核心标签 (汇总用)
    suggestions: list = field(default_factory=list)     # ⑤ 改进建议 2-3 条
    rewrite_example: str = ""              # ⑥ 重写示例


def compute_total(p: V8Payload) -> tuple[float, float, float]:
    """→ (基础分, 扣分合计, 总分) — 机械计算, 不信任 LLM 算术。
    v12.1 契约五/六: 总分 = clamp(Σscores − Σdeductions, 65, 95)。"""
    base = sum(float(v) for v in p.scores.values())
    ded = sum(float(d.points) for d in p.deductions)
    total = min(FINAL_CAP, max(FINAL_FLOOR, base - ded))
    return base, ded, total


def validate_payload(p: V8Payload) -> list[str]:
    """区间校验 (PRD G4)。返回问题清单, 空 = 合法; 非空则**禁止落盘**。"""
    problems: list[str] = []
    if p.framework_version != FRAMEWORK_VERSION:
        problems.append(f"framework_version={p.framework_version!r} ≠ {FRAMEWORK_VERSION}")
    # 5 维齐全且为合法档位值 (v12.1 契约一: 离散映射, 非档位分即拦)
    for slug, cn, mx in DIMENSIONS:
        if slug not in p.scores:
            problems.append(f"缺维度分: {cn} ({slug})")
            continue
        try:
            v = float(p.scores[slug])
        except (TypeError, ValueError):
            problems.append(f"维度分非数值: {slug}={p.scores[slug]!r}")
            continue
        if v not in DIM_ALLOWED[slug]:
            allowed = sorted(DIM_ALLOWED[slug])
            problems.append(f"维度分非档位值: {cn} {v:g} ∉ {allowed}")
    for slug in p.scores:
        if slug not in DIM_MAX:
            problems.append(f"未知维度 slug: {slug}")
    # 扣分项合法 + 单项区间内 + 不重复
    seen = set()
    ded_sum = 0.0
    for d in p.deductions:
        reg = DEDUCTIONS.get(d.slug)
        if reg is None:
            problems.append(f"未知扣分 slug: {d.slug}")
            continue
        label, _dim, lo, hi, _act = reg
        if d.slug in seen:
            problems.append(f"扣分项重复: {d.slug}")
        seen.add(d.slug)
        try:
            pts = float(d.points)
        except (TypeError, ValueError):
            problems.append(f"扣分非数值: {d.slug}={d.points!r}")
            continue
        if not (lo <= pts <= hi):
            problems.append(f"扣分越界: 「{label}」{pts} ∉ [{lo}, {hi}]")
        ded_sum += pts
    # v12.1 §二 全局限制: 触发项 ≤ 3, 累计扣分 ≤ 5
    if len(p.deductions) > DEDUCTION_MAX_ITEMS:
        problems.append(f"扣分项超限: {len(p.deductions)} 项 > {DEDUCTION_MAX_ITEMS} 项")
    if ded_sum > DEDUCTION_TOTAL_CAP:
        problems.append(f"扣分累计超限: {ded_sum:g} > {DEDUCTION_TOTAL_CAP}")
    return problems


# ─── 渲染 (六段式, 确定性) ────────────────────────────────────────────

def render_personal_report(p: V8Payload) -> str:
    base, ded, total = compute_total(p)
    raw = base - ded
    grade = grade_for_total(total)
    head_member = f"{p.member} · " if p.member else ""
    head_week = f"{p.week} · " if p.week else ""
    lines = [
        f"# 周报自省诊断 · {head_member}{head_week}{grade}({total:.0f} 分)",
        "",
        f"> 框架 {FRAMEWORK_LABEL} · 周报不是\"工作记录\", 而是\"价值证明\" · "
        f"自省式诊断, 不作为绩效依据",
        "",
        "## ① 岗位价值判断",
        "",
        p.position_value or "(未给出)",
        "",
        "## ② 5 维基础分",
        "",
        "| 维度 | 得分 | 理由 |",
        "|---|---|---|",
    ]
    for slug, cn, mx in DIMENSIONS:
        v = p.scores.get(slug, 0)
        reason = p.score_reasons.get(slug, "")
        lines.append(f"| {cn} | {float(v):.0f}/{mx} | {reason} |")
    lines += [f"| **基础分小计** | **{base:.0f}/100** | |", ""]
    lines += ["## ③ 触发的反向扣分项", ""]
    if p.deductions:
        lines += ["| 问题 | 扣分 | 原因 | 立即改进动作 |", "|---|---|---|---|"]
        for d in p.deductions:
            label, _dim, _lo, _hi, act = DEDUCTIONS[d.slug]
            lines.append(f"| {label} | −{float(d.points):.0f} | {d.reason} | {act} |")
        lines.append(f"| **扣分合计** | **−{ded:.0f}** | | |")
    else:
        lines.append("未触发任何反向扣分项。")
    if raw < FINAL_FLOOR:
        total_expr = f"{base:.0f} − {ded:.0f} = {raw:.0f} → 封底 {FINAL_FLOOR:.0f} = {total:.0f}"
    elif raw > FINAL_CAP:
        total_expr = f"{base:.0f} − {ded:.0f} = {raw:.0f} → 封顶 {FINAL_CAP:.0f} = {total:.0f}"
    else:
        total_expr = f"{base:.0f} − {ded:.0f} = {total:.0f}"
    lines += [
        "",
        "## ④ 总分与等级",
        "",
        f"**总分 = {total_expr}** → **{grade}**({grade_meaning(grade)})",
        "",
        GRADE_HONESTY_NOTE,
        "",
        "## ⑤ 改进建议",
        "",
    ]
    if p.suggestions:
        lines += [f"{i}. {s}" for i, s in enumerate(p.suggestions, 1)]
    else:
        lines.append("(无)")
    lines += ["", "## ⑥ 重写示例", "", p.rewrite_example or "(无)", ""]
    if p.core_label:
        lines += [f"> 核心标签: {p.core_label}", ""]
    return "\n".join(lines)


# ─── review frontmatter 解析 / scores.json 落盘 ───────────────────────

def _parse_frontmatter(text: str) -> dict:
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}
    try:
        import yaml
        data = yaml.safe_load(m.group(1))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def load_review_payload(review_md: Path, *, member: str = "", week: str = "") -> V8Payload:
    """评委 review (frontmatter 契约, SKILL.md §五) → V8Payload。缺失字段留空由校验拦。"""
    text = review_md.read_text(encoding="utf-8", errors="replace")
    fm = _parse_frontmatter(text)
    p = V8Payload(member=member, week=week)
    p.framework_version = str(fm.get("framework_version") or FRAMEWORK_VERSION)
    scores = fm.get("scores") or {}
    if isinstance(scores, dict):
        p.scores = dict(scores)
    reasons = fm.get("score_reasons") or {}
    if isinstance(reasons, dict):
        p.score_reasons = {str(k): str(v) for k, v in reasons.items()}
    for d in (fm.get("deductions") or []):
        if isinstance(d, dict) and d.get("slug") is not None:
            p.deductions.append(Deduction(
                slug=str(d["slug"]), points=d.get("points", 0),
                reason=str(d.get("reason") or "")))
    p.position_value = str(fm.get("position_value") or "")
    p.core_label = str(fm.get("core_label") or "")
    sug = fm.get("suggestions") or []
    if isinstance(sug, list):
        p.suggestions = [str(s) for s in sug]
    p.rewrite_example = str(fm.get("rewrite_example") or "")
    return p


def validate_review_text(review_text: str) -> list[str]:
    """从 review 文本 (frontmatter) 解析 payload 并区间校验; 返回问题清单 (空=合法)。
    供 Phase 4 就地校验用: LLM 偶发吐畸形 YAML 或越界扣分, 不过则上层重试重新生成,
    避免带病 review 流到 Phase 5 / verify 才 exit 5 (成员收到"评审失败")。
    注意: 传入前应先过 fix_review_yaml.fix_review_frontmatter 规整 (剥围栏/提 frontmatter)。"""
    fm = _parse_frontmatter(review_text)
    if not fm:
        return ["frontmatter 解析失败 (空或非法 YAML)"]
    p = V8Payload()
    p.framework_version = str(fm.get("framework_version") or FRAMEWORK_VERSION)
    scores = fm.get("scores") or {}
    if isinstance(scores, dict):
        p.scores = dict(scores)
    for d in (fm.get("deductions") or []):
        if isinstance(d, dict) and d.get("slug") is not None:
            p.deductions.append(Deduction(
                slug=str(d["slug"]), points=d.get("points", 0),
                reason=str(d.get("reason") or "")))
    return validate_payload(p)


def scores_json_payload(p: V8Payload) -> dict:
    """结构化落盘 (周汇总数据源)。校验必须先过。"""
    base, ded, total = compute_total(p)
    return {
        "framework_version": p.framework_version,
        "member": p.member, "week": p.week,
        "scores": {s: float(p.scores[s]) for s, _, _ in DIMENSIONS},
        "deductions": [{"slug": d.slug, "points": float(d.points), "reason": d.reason}
                       for d in p.deductions],
        "base": base, "deducted": ded, "total": total,
        "grade": grade_for_total(total),
        "core_label": p.core_label,
        "position_value": p.position_value,
        "suggestions": list(p.suggestions),
    }


def write_outputs(brand_dir: Path, p: V8Payload) -> Path:
    """校验 → 渲染 md + scores.json。校验不过抛 ValueError (fail-close, 不落脏数据)。"""
    problems = validate_payload(p)
    if problems:
        raise ValueError("v8 校验不过: " + "; ".join(problems))
    brand_dir.mkdir(parents=True, exist_ok=True)
    (brand_dir / "report-v8.md").write_text(render_personal_report(p), encoding="utf-8")
    (brand_dir / "scores.json").write_text(
        json.dumps(scores_json_payload(p), ensure_ascii=False, indent=2), encoding="utf-8")
    return brand_dir / "report-v8.md"


# ─── CLI ─────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="学习小组周报点评 v12.1 输出渲染")
    ap.add_argument("brand", nargs="?", help="reports/<brand>/ (读 reviews/v8-coach.md)")
    ap.add_argument("--selfcheck", action="store_true", help="注册表完整性自检")
    args = ap.parse_args(argv)
    if args.selfcheck:
        assert len(DIMENSIONS) == 5 and sum(mx for _, _, mx in DIMENSIONS) == 100
        assert len(DEDUCTIONS) == 5                          # 一维一项
        dims_covered = set()
        for slug, (label, dim, lo, hi, act) in DEDUCTIONS.items():
            assert dim in DIM_MAX and 0 < lo <= hi, slug
            assert hi == 2, slug                             # 每项 1-2 分
            dims_covered.add(dim)
        assert dims_covered == set(DIM_MAX)                  # 5 项恰好覆盖 5 维
        for slug in DIM_MAX:                                 # 契约一: 每维恰 5 个档位值
            assert len(DIM_ALLOWED[slug]) == 5, slug
        assert DEDUCTION_MAX_ITEMS == 3 and DEDUCTION_TOTAL_CAP == 5   # v12.1 全局上限
        assert {g for _, g, _ in GRADES} == {"A", "B+", "B", "C"}     # 4 档 (无 C-)
        assert (FINAL_FLOOR, FINAL_CAP) == (65.0, 95.0)               # clamp 区间
        print("✅ v12.1 注册表自检通过 — 5 维=100 分 (各 5 档位), "
              "5 项扣分 (一维一项, 各 1-2) 全局上限 ≤3 项/≤5 分, "
              "4 档 (A/B+/B/C), 总分 clamp [65, 95]")
        return 0
    if not args.brand:
        print("用法: study_weekly_output.py <brand> | --selfcheck")
        return 2
    brand_dir = REPORTS_DIR / args.brand
    review = brand_dir / "reviews" / "v8-coach.md"
    if not review.is_file():
        print(f"❌ 缺评委 review: {review}")
        return 1
    p = load_review_payload(review)
    problems = validate_payload(p)
    if problems:
        print("❌ v8 校验不过:")
        for pr in problems:
            print(f"  · {pr}")
        return 1
    out = write_outputs(brand_dir, p)
    base, ded, total = compute_total(p)
    print(f"✅ {out} — 总分 {base:.0f}−{ded:.0f}={total:.0f} ({grade_for_total(total)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

