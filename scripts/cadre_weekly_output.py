#!/usr/bin/env python3
"""
cadre_weekly_output.py — 干部周报 L1 单份「七块」输出渲染 (M1.5 · PRD §6)

七块 (docs/internal/prd-cadre-weekly-ai-review.md §6):
  ① 一句话判断      — ≤80 字 + 5 类管理状态归类
                     (经营闭环型 / 事项推进型 / 认知输出型 / 风险暴露型 / 过程断档型)
  ② 总分 + 分项分   — 综合分 + 6 维度分 + 最低 2 项
  ③ 原文证据        — ≥2 条 (评委抽取, 待人工核对原文)
  ④ 最值得保留的能力
  ⑤ 最影响利润转正的短板
  ⑥ 下周必须补充    — 1-3 条可执行
  ⑦ 组织进化建议

设计 (与 op2_output.render_op2_6section / build_workshop_ranking 同范式):
  - `render_cadre_weekly_7block(data)` 为**纯函数**渲染器 (无 IO, 全可测)。
  - `classify_management_status(dims)` 把 6 维得分**机械归类**到 5 类管理状态 —— 透明、
    确定、可测, 这是 M1.5 相对 op2 6 段式新增的核心逻辑; 未来 (M3) 若管线由 LLM 显式
    产出状态, 经 data.management_status 覆盖即可, 渲染器零改动。
  - `load_from_report(report_md_path, scene)` 从 report.md frontmatter (panel_summary)
    + reviews/*.md 机械装配数据, 供 CLI 出草稿 (评委要点 / 行动建议抽取, 人工替换原文摘录)。

首版无锚点, sum_max_score 100 分制。定级门槛同 panel: <50 重写 / 50-59 修改 / ≥60 复核
(2026-07-14 两步校准 60/80→50/70→revise 60, 见 panel.yaml score_threshold 注释)。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

VAULT_ROOT = Path(__file__).parent.parent.resolve()
REPORTS_DIR = VAULT_ROOT / "reports"
DEFAULT_SCENE = "cadre-weekly-ai-review"

sys.path.insert(0, str(VAULT_ROOT / "scripts"))


# ─── 5 类管理状态 (PRD §6 一句话判断归类) ────────────────────────────

STATUS_BUSINESS_LOOP = "经营闭环型"
STATUS_TASK_EXEC = "事项推进型"
STATUS_COGNITIVE = "认知输出型"
STATUS_RISK_SURFACE = "风险暴露型"
STATUS_PROFIT_GAP = "利润短板型"
STATUS_PROCESS_GAP = "过程断档型"

ALL_STATUSES = [
    STATUS_BUSINESS_LOOP, STATUS_TASK_EXEC, STATUS_COGNITIVE,
    STATUS_RISK_SURFACE, STATUS_PROFIT_GAP, STATUS_PROCESS_GAP,
]

# 一句话模板 (≤80 字; {strong}/{weak} 用短标签填充, 见 _LENS_*_SHORT)
_STATUS_ONELINER = {
    STATUS_BUSINESS_LOOP: "经营闭环型:商机到利润回款基本成闭环,{strong}扎实;守住{weak}即稳利润转正。",
    STATUS_TASK_EXEC:     "事项推进型:动作在推进但未闭到利润,{strong}是亮点,{weak}是最影响利润的短板。",
    STATUS_COGNITIVE:     "认知输出型:偏战略与组织认知,{strong}突出;经营落点偏弱,须补{weak}。",
    STATUS_RISK_SURFACE:  "风险暴露型:主动暴露风险为主,{strong}可取;下一步把{weak}接成动作与利润。",
    STATUS_PROFIT_GAP:    "利润短板型:执行/产品力在线,{strong}扎实;但利润链与客户转化未写实,先补{weak}。",
    STATUS_PROCESS_GAP:   "过程断档型:证据薄弱、闭环缺失,先把{weak}与原文证据、利润链写实。",
}

# ─── 定级 (门槛同 panel score_threshold: rewrite 50 / revise 60; 2026-07-14 校准) ──────

GRADE_REWRITE = "重写"
GRADE_REVISE = "修改"
GRADE_REVIEW = "复核"   # ≥ revise 线 → 进人工复核


def grade_for_pct(pct: float, rewrite: float = 50.0, revise: float = 60.0) -> str:
    if pct < rewrite:
        return GRADE_REWRITE
    if pct < revise:
        return GRADE_REVISE
    return GRADE_REVIEW


# ─── 6 维 slug → 能力 / 短板标签 (与 panel scoring_lenses_override 对齐) ─

# ④ 最值得保留的能力 (长标签, 用于块 ④)
_LENS_CAPABILITY = {
    "profit-cash":   "利润纪律与现金回款闭环",
    "customer-grow": "客户洞察与决策链穿透",
    "ai-product":    "AI 产品化与方案验证",
    "exec-loop":     "执行闭环与第一性原理",
    "org-evolve":    "组织带教与知识沉淀",
    "risk-honesty":  "风险治理与真实表达",
}

# ⑤ 最影响利润转正的短板 (落点须指向毛利/现金/回款/成本/成交率/交付/资源, PRD §6)
_LENS_SHORTBOARD = {
    "profit-cash":   "毛利/现金/回款/成本口径缺失",
    "customer-grow": "客户决策链与转化未写实",
    "ai-product":    "AI 停在学习概念、未落产品/验证",
    "exec-loop":     "缺硬指标/责任人/下周必验结果",
    "org-evolve":    "组织带教与知识沉淀单薄",
    "risk-honesty":  "只写推进、未暴露风险根因与预案",
}

# 一句话短标签 (控制 ≤80 字)
_LENS_STRONG_SHORT = {
    "profit-cash":   "利润现金纪律",
    "customer-grow": "客户与决策链",
    "ai-product":    "AI 产品化",
    "exec-loop":     "执行闭环",
    "org-evolve":    "组织带教",
    "risk-honesty":  "风险真实表达",
}
_LENS_WEAK_SHORT = {
    "profit-cash":   "毛利/现金/回款",
    "customer-grow": "客户转化链",
    "ai-product":    "AI 产品化落地",
    "exec-loop":     "硬指标闭环",
    "org-evolve":    "组织沉淀",
    "risk-honesty":  "风险暴露",
}


# ─── 数据模型 ────────────────────────────────────────────────────────

@dataclass
class CadreDimension:
    slug: str
    name: str
    score: float
    max_score: float

    @property
    def pct(self) -> float:
        return self.score / self.max_score * 100 if self.max_score else 0.0


@dataclass
class CadreWeekly7BlockData:
    brand_slug: str
    cadre_label: str                                   # 干部标识 (脱敏后, 如「干部A」/ 模版名)
    dimensions: list[CadreDimension]
    template_name: str = ""                            # 所属周报模版 (8 类之一), 可空
    doc_claims: list[str] = field(default_factory=list)  # ③ 周报原文要点 (Phase 0 从 doc 提取, 真·原文侧)
    evidence: list[str] = field(default_factory=list)  # ③ 评委关键指认 (据原文的评委引述/判断)
    strengths: list[str] = field(default_factory=list) # ④ 覆盖; 空则从 top 维度派生
    weaknesses: list[str] = field(default_factory=list)# ⑤ 覆盖; 空则从 bottom 维度派生
    next_week: list[str] = field(default_factory=list) # ⑥ 下周必须补充 (1-3)
    org_evolution: list[str] = field(default_factory=list)  # ⑦ 组织进化建议
    management_status: Optional[str] = None            # 覆盖; 空则 classify
    one_liner: Optional[str] = None                    # 覆盖; 空则生成
    grade: Optional[str] = None                        # 覆盖; 空则按门槛算
    rewrite_pct: float = 50.0
    revise_pct: float = 60.0


# ─── 分数聚合 / 排序 ─────────────────────────────────────────────────

def total_score(data: CadreWeekly7BlockData) -> tuple[float, float]:
    return (
        sum(d.score for d in data.dimensions),
        sum(d.max_score for d in data.dimensions),
    )


def overall_pct(dims: list[CadreDimension]) -> float:
    tot = sum(d.score for d in dims)
    mx = sum(d.max_score for d in dims)
    return tot / mx * 100 if mx > 0 else 0.0


def top_dims(dims: list[CadreDimension], n: int = 2,
             min_pct: Optional[float] = None) -> list[CadreDimension]:
    """占比降序前 n 维; min_pct 时仅取达标者 (全不达标回退最强 1 维)。"""
    ranked = sorted(dims, key=lambda d: d.pct, reverse=True)
    if min_pct is not None:
        strong = [d for d in ranked if d.pct >= min_pct]
        return strong[:n] if strong else ranked[:1]
    return ranked[:n]


def bottom_dims(dims: list[CadreDimension], n: int = 2) -> list[CadreDimension]:
    return sorted(dims, key=lambda d: d.pct)[:n]


# ─── 5 类管理状态归类 (M1.5 新增核心逻辑, 纯函数, 确定可测) ─────────────

def _pct_of(dims: list[CadreDimension], slug: str) -> float:
    for d in dims:
        if d.slug == slug:
            return d.pct
    return 0.0


def classify_management_status(dims: list[CadreDimension]) -> str:
    """按 6 维得分占比机械归类到 5 类管理状态 (优先级从上到下)。

    判定树 (透明、确定; 「有区分度的形态」先判, 整体薄弱兜底):
      1. 利润链≥80 且 执行≥70 且 客户/产品≥70     → 经营闭环型 (商机到利润回款成链)
      2. 风险治理为最高维 且 ≥70                  → 风险暴露型 (主动暴露风险主导)
      3. 组织进化为最高维且≥70 且 利润<70 且 客户<70 → 认知输出型 (偏战略组织, 经营落点弱)
      4. 执行或产品≥70 且 利润<60 且 客户<60       → 利润短板型 (执行/产品力在线, 经营未接上)
      5. overall < 60%                           → 过程断档型 (整体薄弱, 真断档)
      6. 兜底 (有内容、在推进, 但未闭到利润)         → 事项推进型

    ⚠ 风险暴露 / 认知输出 / 利润短板**先于** overall<60 判定: 这几类报告天然在部分经营维度
    得分低 (见 calibration §2 周月分离 / §3 角色适配), 不应被总分门槛当作「过程断档」压掉 ——
    判据是「形态」(某类维度突出/执行强), 不是总分高低。尤其「执行/产品力强但利润弱」应是
    「利润短板型」(补经营), 不是「过程断档型」(否定整体), 否则冤枉高执行力干部。
    """
    if not dims:
        return STATUS_PROCESS_GAP
    p_profit = _pct_of(dims, "profit-cash")
    p_cust = _pct_of(dims, "customer-grow")
    p_ai = _pct_of(dims, "ai-product")
    p_org = _pct_of(dims, "org-evolve")
    p_risk = _pct_of(dims, "risk-honesty")
    overall = overall_pct(dims)
    top_slug = max(dims, key=lambda d: d.pct).slug

    if p_profit >= 80.0 and _pct_of(dims, "exec-loop") >= 70.0 and max(p_cust, p_ai) >= 70.0:
        return STATUS_BUSINESS_LOOP
    if top_slug == "risk-honesty" and p_risk >= 70.0:
        return STATUS_RISK_SURFACE
    if top_slug == "org-evolve" and p_org >= 70.0 and p_profit < 70.0 and p_cust < 70.0:
        return STATUS_COGNITIVE
    # 执行/产品力在线但经营 (利润+客户) 双弱: 是「利润短板」不是「过程断档」——
    # 别用"断档/薄弱"这种否定整体的词压高执行力干部 (先于 overall<60 判, 否则被总分门槛压掉)。
    if max(_pct_of(dims, "exec-loop"), p_ai) >= 70.0 and p_profit < 60.0 and p_cust < 60.0:
        return STATUS_PROFIT_GAP
    if overall < 60.0:
        return STATUS_PROCESS_GAP
    return STATUS_TASK_EXEC


def _strong_short(dims: list[CadreDimension]) -> str:
    if not dims:
        return "—"
    d = max(dims, key=lambda x: x.pct)
    return _LENS_STRONG_SHORT.get(d.slug, d.name)


def _weak_short(dims: list[CadreDimension]) -> str:
    """一句话「最影响利润的短板」标签。利润链 (profit-cash) 偏弱 (<80) 时优先指它 ——
    它是利润转正最直接的维度; 否则退回占比最低维。避免头条把"利润链断口"错标成别的低分维
    (如占比更低但非利润直接项的风险治理), 误导干部去补错方向。与块⑤ derive_weaknesses 口径一致。"""
    if not dims:
        return "—"
    profit = next((d for d in dims if d.slug == "profit-cash"), None)
    if profit is not None and profit.pct < 80.0:
        return _LENS_WEAK_SHORT.get("profit-cash", profit.name)
    d = min(dims, key=lambda x: x.pct)
    return _LENS_WEAK_SHORT.get(d.slug, d.name)


def one_liner(dims: list[CadreDimension], status: Optional[str] = None,
              max_len: int = 80) -> str:
    """生成 ≤max_len 字的一句话判断 (status 缺则先归类)。"""
    status = status or classify_management_status(dims)
    tmpl = _STATUS_ONELINER.get(status, _STATUS_ONELINER[STATUS_PROCESS_GAP])
    text = tmpl.format(strong=_strong_short(dims), weak=_weak_short(dims))
    if len(text) > max_len:
        text = text[:max_len - 1] + "…"
    return text


# ─── ④/⑤ 能力 / 短板派生 ────────────────────────────────────────────

def _fmt(x: float) -> str:
    return f"{x:g}"


def derive_strengths(dims: list[CadreDimension], n: int = 2) -> list[str]:
    """④ 最值得保留能力: 占比 ≥70 的 top n 维 (全不达标取最强 1 维, 标注相对最强)。"""
    if not dims:
        return []
    strong = top_dims(dims, n=n, min_pct=70.0)
    weak_all = all(d.pct < 70.0 for d in dims)
    out = []
    for d in strong:
        cap = _LENS_CAPABILITY.get(d.slug, d.name)
        tag = "相对最强" if weak_all else "保留"
        out.append(f"{cap}({tag}) —— {d.name} {_fmt(d.score)}/{_fmt(d.max_score)}({d.pct:.0f}%)")
    return out


def derive_weaknesses(dims: list[CadreDimension], n: int = 2) -> list[str]:
    """⑤ 最影响利润转正短板: 占比最低 n 维; 利润链偏弱 (<80) 必纳入。"""
    if not dims:
        return []
    weak = bottom_dims(dims, n=n)
    profit = next((d for d in dims if d.slug == "profit-cash"), None)
    if profit is not None and profit not in weak and profit.pct < 80.0:
        weak = weak + [profit]
    out = []
    for d in weak:
        sb = _LENS_SHORTBOARD.get(d.slug, d.name)
        out.append(f"{sb} —— {d.name} 仅 {_fmt(d.score)}/{_fmt(d.max_score)}({d.pct:.0f}%)")
    return out


# ─── 渲染 (纯函数) ──────────────────────────────────────────────────

def render_cadre_weekly_7block(data: CadreWeekly7BlockData) -> str:
    """渲染干部周报 L1 七块 Markdown。"""
    dims = data.dimensions
    total, max_total = total_score(data)
    pct = total / max_total * 100 if max_total > 0 else 0.0
    status = data.management_status or classify_management_status(dims)
    liner = data.one_liner or one_liner(dims, status=status)
    grade = data.grade or grade_for_pct(pct, data.rewrite_pct, data.revise_pct)
    strengths = data.strengths or derive_strengths(dims)
    weaknesses = data.weaknesses or derive_weaknesses(dims)
    lowest2 = bottom_dims(dims, n=2)

    L: list[str] = []
    title_who = data.cadre_label or data.brand_slug
    L += [f"# 干部周报评审 · {title_who}", ""]
    if data.template_name:
        L += [f"> 所属模版:{data.template_name}", ""]

    # ── ① 一句话判断 ──────────────────────────────────────
    L += [
        "## ① 一句话判断",
        "",
        f"**管理状态**:`{status}`  ·  **定级**:{grade}",
        "",
        f"> {liner}",
        "",
        "_(管理状态由 6 维得分机械归类, 供参考; 最终以人工复核为准)_",
        "",
    ]

    # ── ② 总分 + 分项 ─────────────────────────────────────
    L += [
        "## ② 总分 + 分项分",
        "",
        f"**{_fmt(total)} / {_fmt(max_total)}** 分（{pct:.0f}%）· 定级 **{grade}**",
        "",
        "| 维度 | 得分 | 满分 | 占比 |",
        "|---|---|---|---|",
    ]
    for d in dims:
        flag = "🔴" if d.pct < 60 else ("🟡" if d.pct < 80 else "🟢")
        L.append(f"| {d.name} | {_fmt(d.score)} | {_fmt(d.max_score)} | {flag} {d.pct:.0f}% |")
    if lowest2:
        low_txt = "、".join(f"{d.name}（{d.pct:.0f}%）" for d in lowest2)
        L += ["", f"**最低 2 项**:{low_txt}"]
    L.append("")

    # ── ③ 原文要点 + 评委指认 ──────────────────────────────
    # 周报原文要点 = Phase 0 doc parser 从周报提取的核心 claims (真·原文侧);
    # 评委指认 = 各评委据原文的关键引述/判断。两者分列, 名副其实。
    L += ["## ③ 原文要点 + 评委指认", ""]
    if data.doc_claims:
        L.append("**周报原文要点**（Phase 0 从文档提取）")
        for c in data.doc_claims:
            L.append(f"- {c}")
        L.append("")
    if data.evidence:
        L.append("**评委关键指认**（据原文）")
        for ev in data.evidence:
            L.append(f"- {ev}")
        L.append("")
    if not data.doc_claims and not data.evidence:
        L += ["- _待抽取原文要点与评委指认_", ""]

    # ── ④ 最值得保留的能力 ────────────────────────────────
    L += ["## ④ 最值得保留的能力", ""]
    if strengths:
        for s in strengths:
            L.append(f"- {s}")
    else:
        L.append("- _暂无明显可保留能力项_")
    L.append("")

    # ── ⑤ 最影响利润转正的短板 ────────────────────────────
    L += ["## ⑤ 最影响利润转正的短板", ""]
    if weaknesses:
        for w in weaknesses:
            L.append(f"- {w}")
    else:
        L.append("- _暂无突出短板_")
    L.append("")

    # ── ⑥ 下周必须补充 ────────────────────────────────────
    L += ["## ⑥ 下周必须补充（1-3 条）", ""]
    if data.next_week:
        for item in data.next_week[:3]:
            L.append(f"- {item}")
    else:
        L.append("- _待明确 1-3 条可执行补充（如「补回毛利测算」「列出回款节点」「补客户决策链」）_")
    L.append("")

    # ── ⑦ 组织进化建议 ────────────────────────────────────
    L += ["## ⑦ 组织进化建议", ""]
    if data.org_evolution:
        for item in data.org_evolution[:3]:
            L.append(f"- {item}")
    else:
        L.append("- _可沉淀为 SOP / 知识库 / AI 卡 / 样例 Skill / 复盘机制 / 人才动作？待补_")
    L.append("")

    return "\n".join(L)


# ─── 从 report.md + reviews/*.md 装配 (loader, 供 CLI 出草稿) ───────────

def load_lenses(scene_slug: str, panel_path: Optional[str] = None) -> list[dict]:
    """从 scenes/<scene>/panel.yaml 取 scoring_lenses (slug/display_name_cn/max_score)。"""
    import panel_loader
    p = panel_path or f"scenes/{scene_slug}/panel.yaml"
    panel = panel_loader.resolve_panel(p)
    return [l for l in panel.get("scoring_lenses", []) if isinstance(l, dict)]


def _split_sections(body: str) -> dict[str, str]:
    """review body → {二级标题: 段落文本}。"""
    out: dict[str, str] = {}
    cur: Optional[str] = None
    buf: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^##\s+(.*?)\s*$", line)
        if m:
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            cur = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return out


def _clean_item(text: str) -> str:
    """清 markdown 加粗 / 前导编号标签, 取首句便于清单展示。"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text).strip()
    text = re.sub(r"^[A-Za-z]\.\s*", "", text).strip()
    # 取首个句号前的主句 (行动建议常一长段), 控制长度
    for sep in ("。", ":", "：", "; ", ";"):
        if sep in text:
            head = text.split(sep, 1)[0].strip()
            if len(head) >= 8:
                return head
    return text[:60].strip()


def _list_items(section_body: str, limit: Optional[int] = None) -> list[str]:
    items: list[str] = []
    for line in section_body.splitlines():
        m = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)", line)
        if m:
            cleaned = _clean_item(m.group(1))
            if cleaned:
                items.append(cleaned)
    return items[:limit] if limit else items


def _first_para(section_body: str) -> str:
    for line in section_body.splitlines():
        s = line.strip().lstrip(">").strip()
        if s:
            return s
    return ""


def _extract_doc_claims(body_md: str, limit: int = 3) -> list[str]:
    """从 report.md §A「### 核心 claims (从 doc 提)」段抽 Phase 0 doc parser 提取的周报要点。
    这是**真·原文侧**证据 (doc 提取, 非评委意见), 用于 ③ 块「周报原文要点」。段缺则返回 []。"""
    import re
    m = re.search(r"###\s*核心\s*claims.*?\n(.*?)(?=\n#{2,3}\s|\Z)", body_md or "", re.S)
    if not m:
        return []
    out: list[str] = []
    for line in m.group(1).splitlines():
        s = line.strip()
        if s.startswith("- ") or s.startswith("* "):
            item = s[2:].strip()
            if item and not item.startswith("_"):     # 跳过占位 "_待…_"
                out.append(item)
    return out[:limit]


def load_from_report(report_md_path: Path | str,
                     scene: str = DEFAULT_SCENE,
                     lenses: Optional[list[dict]] = None,
                     cadre_label: str = "") -> CadreWeekly7BlockData:
    """读 report.md frontmatter (panel_summary) + reviews/*.md 机械装配草稿数据。

    - 分项分: panel_summary.lens_means × panel scoring_lenses (slug→name/max)
    - 定级: panel_summary.grade (缺则按占比算)
    - ③ 证据: 各 review「一句话」金句 (标注评委, 草稿, 待替换原文摘录)
    - ⑥ 下周: exec-loop / cfo-profit review「行动建议」首项
    - ⑦ 组织: 命中 SOP/知识库/复盘/AI 卡/沉淀 的行动建议
    """
    from _export_helpers import parse_frontmatter

    report_md_path = Path(report_md_path)
    fm, body = parse_frontmatter(report_md_path.read_text(encoding="utf-8", errors="replace"))
    ps = fm.get("panel_summary") or {}
    doc_claims = _extract_doc_claims(body)   # ③ 周报原文要点 (§A 核心 claims, 真·doc 提取)
    brand = fm.get("brand_slug") or report_md_path.parent.name

    if lenses is None:
        lenses = load_lenses(scene)
    lens_means = ps.get("lens_means") or {}
    dims = [
        CadreDimension(
            slug=str(l.get("slug")),
            name=str(l.get("display_name_cn") or l.get("slug")),
            score=float(lens_means.get(l.get("slug"), 0.0) or 0.0),
            max_score=float(l.get("max_score", 0) or 0),
        )
        for l in lenses
    ]

    # 读 reviews/*.md 分段
    reviews: dict[str, dict[str, str]] = {}
    reviews_dir = report_md_path.parent / "reviews"
    if reviews_dir.exists():
        for rf in sorted(reviews_dir.glob("*.md")):
            try:
                rfm, rbody = parse_frontmatter(rf.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            slug = rfm.get("judge") or rf.stem
            secs = _split_sections(rbody)
            reviews[slug] = {
                "display": rfm.get("judge_display_name") or slug,
                "quote": _first_para(secs.get("一句话", "")),
                "gaps": secs.get("关键缺口", ""),
                "actions": secs.get("行动建议", ""),
            }

    # ③ 证据: 评委金句 (草稿)
    evidence = []
    for slug, r in reviews.items():
        if r["quote"]:
            evidence.append(f"〔{r['display']}〕{r['quote']}")
    evidence = evidence[:3]

    # ⑥ 下周: 执行 / CFO 评委行动建议首项 (最高优先级)
    next_week: list[str] = []
    for slug in ("first-principles-exec", "exec-loop", "cfo-profit"):
        if slug in reviews:
            items = _list_items(reviews[slug]["actions"], limit=1)
            next_week += items
    if not next_week:  # 回退: 任一评委行动建议
        for r in reviews.values():
            next_week += _list_items(r["actions"], limit=1)
            if next_week:
                break
    next_week = _dedup(next_week)[:3]

    # ⑦ 组织进化: 命中沉淀关键词的行动建议
    org_kw = ("SOP", "知识库", "复盘", "AI 卡", "AI卡", "沉淀", "样例", "带教", "机制", "培养")
    org: list[str] = []
    for r in reviews.values():
        for item in _list_items(r["actions"]):
            if any(k in item for k in org_kw):
                org.append(item)
    org = _dedup(org)[:3]

    return CadreWeekly7BlockData(
        brand_slug=brand,
        cadre_label=cadre_label or brand,
        dimensions=dims,
        doc_claims=doc_claims,
        evidence=evidence,
        next_week=next_week,
        org_evolution=org,
        grade=(str(ps.get("grade")) if ps.get("grade") else None),
    )


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


# ─── CLI ───────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="干部周报 L1 七块输出渲染 (M1.5)")
    ap.add_argument("report", help="reports/<brand>/report.md 路径")
    ap.add_argument("--scene", default=DEFAULT_SCENE, help=f"场景 slug (默认 {DEFAULT_SCENE})")
    ap.add_argument("--cadre-label", default="", help="干部标识 (脱敏后, 默认 brand_slug)")
    ap.add_argument("--output", default=None, help="输出 .md 路径 (默认打印到 stdout)")
    args = ap.parse_args(argv)

    report_path = Path(args.report)
    if not report_path.exists():
        print(f"❌ report 不存在: {report_path}", file=sys.stderr)
        return 2
    try:
        data = load_from_report(report_path, scene=args.scene, cadre_label=args.cadre_label)
    except Exception as e:  # noqa: BLE001
        print(f"❌ 装配失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    md = render_cadre_weekly_7block(data)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"✅ 七块报告写入: {out}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
