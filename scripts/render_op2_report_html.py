#!/usr/bin/env python3
"""
render_op2_report_html.py — OP2 / sum_max_score / REVIEW 报告 → 富可视化 self-contained HTML。

**机械式 (纯 Python, 零 LLM)** 渲染器: 从 report.md + reviews/*.md + panel.yaml 直接生成
带 KPI 卡 / 评级带 / 7 维度×评委热力图 / 评委详情卡 / §A·B·C 正文的单文件 HTML。
与 LLM Set C (`templates/export-set-c-op2-v2-prompt.md`) 视觉语言一致, 但**确定性、快、无 LLM**
—— 因此可在 review worker 里无人值守跑, 接飞书自动回推 (见 render_review_formats.render_html)。

设计纪律:
- **best-effort**: 任何解析/渲染异常 → 抛给调用方, 由 render_review_formats 降级到朴素 markdown 渲染,
  绝不影响 report.md / 朴素 html 投递。
- **不编造**: 分数来自 report.md panel_summary + reviews scores; 金句/修订建议逐字取自 reviews / report.md。
- **自包含**: CSS 全内联, 无外链/字体 CDN, 便于飞书内打开 + Chrome headless 转 PDF。
- 锚点不写真名 (report.md 已按 CLAUDE.md §9 脱敏, 本渲染器只搬运)。
"""

from __future__ import annotations

import html as _html
import re
from pathlib import Path
from typing import Optional

import yaml

try:
    import markdown as _markdown
except Exception:  # pragma: no cover - 库缺由调用方降级
    _markdown = None

from _export_helpers import parse_frontmatter

try:
    from desensitize import desensitize_sources as _ds
except Exception:  # pragma: no cover - 模块缺则不脱敏 (原文渲染, 不阻断)
    def _ds(t):
        return t

VAULT_ROOT = Path(__file__).resolve().parent.parent

# 评委 stripe 配色 (anchor 用暖赤, dimension 循环取色) —— 对齐 internal v2 视觉 DNA
_ANCHOR_COLOR = "#b8331f"
_DIM_COLORS = ["#3f6b8c", "#7a4f9e", "#c98a2b", "#4a8c6a", "#a0506b", "#5a7d3f", "#8c6d3f"]


def _esc(s) -> str:
    return _html.escape(str(s if s is not None else ""))


# ─── 解析 ────────────────────────────────────────────────────────────

def _split_sections(body: str) -> dict[str, str]:
    """按 '## §A' / '## §B' / '## §C' / '## 30/90/365' 切正文, 返回 {key: markdown 段}。"""
    markers = [
        ("A", re.compile(r"^##\s*§A\b.*$", re.M)),
        ("B", re.compile(r"^##\s*§B\b.*$", re.M)),
        ("C", re.compile(r"^##\s*§C\b.*$", re.M)),
        ("ATTR", re.compile(r"^##\s*30/90/365\b.*$", re.M)),
    ]
    hits = []
    for key, rx in markers:
        m = rx.search(body)
        if m:
            hits.append((m.start(), key))
    hits.sort()
    out: dict[str, str] = {}
    for i, (pos, key) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(body)
        out[key] = body[pos:end].strip()
    return out


def _load_panel(report_dir: Path, fm: dict) -> dict:
    """拿到含 scoring_lenses (7 维 + max_score) + judges 的 panel 定义。

    ⚠ reports/<brand>/panel.yaml 是流水线 auto-write 的极简快照 (只有 judges 列表,
    无 lens 定义), 不能直接用。优先 panel_loader.resolve_panel(fm['panel'] scene 路径),
    它 merge extends 后产出 resolved `scoring_lenses` + `judges` (含 max_score / display_name_cn)。
    resolve 失败再回退读原始 yaml 候选 (best-effort)。
    """
    panel_ref = str(fm.get("panel", "") or "")
    # 1) 优先 resolve scene panel (含 merged scoring_lenses + judges)
    for p in ([VAULT_ROOT / panel_ref, Path(panel_ref)] if panel_ref else []):
        try:
            if p.exists():
                import panel_loader
                resolved = panel_loader.resolve_panel(str(p))
                if resolved:
                    return resolved
        except Exception:
            break  # resolve 不可用 → 走回退
    # 2) 回退: 读原始 yaml (scene panel 直读 → 至少拿到 scoring_lenses_override)
    for c in ([VAULT_ROOT / panel_ref, Path(panel_ref)] if panel_ref else []) + \
             [VAULT_ROOT / "scenes" / "op2-company" / "panel.yaml", report_dir / "panel.yaml"]:
        try:
            if c.exists():
                data = yaml.safe_load(c.read_text(encoding="utf-8")) or {}
                if data.get("scoring_lenses") or data.get("scoring_lenses_override"):
                    return data
        except Exception:
            continue
    return {}


def _load_reviews(report_dir: Path) -> dict[str, dict]:
    """读 reviews/*.md → {judge_slug: {category, scores, confidence, adversarial, quote, display_name}}。"""
    out: dict[str, dict] = {}
    reviews_dir = report_dir / "reviews"
    if not reviews_dir.exists():
        return out
    for f in sorted(reviews_dir.glob("*.md")):
        try:
            fm, body = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        slug = fm.get("judge") or f.stem
        adv = fm.get("adversarial_view") or {}
        out[slug] = {
            "category": fm.get("judge_category", "dimension"),
            "display_name": fm.get("judge_display_name") or None,  # 缺则 None, 由 _jname 回退 panel 中文名
            "scores": fm.get("scores") or {},
            "confidence": fm.get("confidence"),
            # 交付前来源脱敏: adversarial / 金句里评委引用的内部路径 / 素材名 / 真名不进富 HTML
            "adversarial": {k: _ds(v) if isinstance(v, str) else v for k, v in adv.items()},
            "quote": _ds(_first_quote(body)),
        }
    return out


def _first_quote(body: str) -> str:
    """从 review body 抽 '## 一句话' 段的第一段文本 (人格化金句)。"""
    m = re.search(r"^##\s*一句话\s*$(.+?)(?=^##\s|\Z)", body, re.M | re.S)
    if not m:
        return ""
    for line in m.group(1).strip().splitlines():
        line = line.strip().lstrip(">").strip()
        if line:
            return line
    return ""


# ─── 着色 ────────────────────────────────────────────────────────────

def _ratio_color(ratio: float) -> str:
    """0..1 得分占比 → 背景色 (低=红 / 中=米 / 高=绿), 对齐热力图观感。"""
    ratio = max(0.0, min(1.0, ratio))
    if ratio >= 0.8:
        return "#cdebd6"
    if ratio >= 0.6:
        return "#e4f0d9"
    if ratio >= 0.45:
        return "#f4efdc"
    if ratio >= 0.3:
        return "#f6e0cf"
    return "#f3cfcf"


def _grade_class(grade: str) -> str:
    if "重写" in grade:
        return "g-rewrite"
    if "修改" in grade:
        return "g-revise"
    return "g-pass"


# ─── HTML 构件 ───────────────────────────────────────────────────────

_CSS = """<style>
:root{--ink:#2b2622;--mute:#7a716a;--line:#e0d8cc;--bg:#faf7f1;--card:#fff;
 --accent:#b8331f;--paper:#fffdf9}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.7 -apple-system,"Segoe UI","Source Han Sans SC","PingFang SC",sans-serif}
.page{max-width:960px;margin:20px auto;padding:34px 40px;background:var(--paper);
 border:1px solid var(--line);border-radius:10px}
h1{font-family:Georgia,"Songti SC",serif;font-size:1.9em;line-height:1.25;margin:.2em 0 .3em}
h2{font-family:Georgia,"Songti SC",serif;font-size:1.3em;border-bottom:1px solid var(--line);
 padding-bottom:.3em;margin:1.8em 0 .7em}
h3{font-size:1.08em;margin:1.3em 0 .5em}
.sub{color:var(--mute);font-size:1em;margin:.2em 0 1em}
.meta{color:var(--mute);font-size:.82em;letter-spacing:.02em;margin-bottom:1.4em}
.badges span{display:inline-block;font-size:.72em;font-weight:700;letter-spacing:.06em;
 padding:2px 9px;border-radius:4px;margin-right:6px;background:#efe7da;color:#6a5f4f}
.badges .review{background:#b8331f;color:#fff}
/* KPI 卡 */
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:18px 0 8px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:14px 16px}
.kpi .lbl{font-size:.72em;letter-spacing:.08em;color:var(--mute);text-transform:uppercase}
.kpi .val{font-family:Georgia,serif;font-size:2.1em;font-weight:700;margin:.1em 0}
.kpi .note{font-size:.76em;color:var(--mute);line-height:1.4}
.kpi.g-rewrite .val{color:#b8331f}.kpi.g-revise .val{color:#c98a2b}.kpi.g-pass .val{color:#3f8c5a}
/* 评级带 */
.bands{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:10px 0 6px}
.band{border:1px solid var(--line);border-radius:8px;padding:10px 12px;text-align:center;
 background:var(--card);color:var(--mute)}
.band .t{font-weight:700;color:var(--ink)}.band .r{font-size:.78em}
.band.on{border-width:2px;box-shadow:0 1px 6px rgba(0,0,0,.06)}
.band.on.g-rewrite{border-color:#b8331f;color:#b8331f}
.band.on.g-revise{border-color:#c98a2b;color:#c98a2b}
.band.on.g-pass{border-color:#3f8c5a;color:#3f8c5a}
/* 评委 chip */
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.chip{border:1px solid var(--line);border-left-width:5px;border-radius:7px;padding:7px 11px;
 background:var(--card);font-size:.82em;min-width:150px}
.chip .n{font-weight:650}.chip .s{font-family:Georgia,serif;font-size:1.15em;font-weight:700}
.chip .c{color:var(--mute);font-size:.85em}
/* 热力图 */
table.heat{border-collapse:collapse;width:100%;margin:12px 0;font-size:.8em}
table.heat th,table.heat td{border:1px solid var(--line);padding:6px 7px;text-align:center}
table.heat th.lens{text-align:left}table.heat td.lens{text-align:left;font-weight:600}
table.heat .mean{background:#efe7da;font-weight:700}
/* 评委卡 */
.jcard{border:1px solid var(--line);border-left-width:5px;border-radius:9px;padding:14px 16px;
 margin:12px 0;background:var(--card)}
.jcard .head{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.jcard .name{font-weight:700;font-size:1.05em}.jcard .doc{color:var(--mute);font-size:.78em}
.jcard .big{font-family:Georgia,serif;font-size:1.7em;font-weight:700}
.lens-row{display:flex;flex-wrap:wrap;gap:8px;margin:9px 0}
.lens-cell{border:1px solid var(--line);border-radius:6px;padding:5px 9px;font-size:.78em;text-align:center}
.lens-cell .ls{font-weight:700;font-family:Georgia,serif}
.quote{font-style:italic;color:#4a423b;border-left:3px solid var(--line);padding:.3em .9em;margin:.7em 0}
.adv{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:8px}
.adv div{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:7px 9px;font-size:.76em}
.adv .k{font-weight:700;color:#b8331f;font-size:.9em;letter-spacing:.03em}
/* 正文 markdown 段 */
.prose h2{margin-top:1.4em}
.prose table{border-collapse:collapse;width:100%;margin:1em 0;font-size:.86em}
.prose th,.prose td{border:1px solid var(--line);padding:6px 10px;text-align:left}
.prose th{background:#efe7da}
.prose blockquote{border-left:3px solid var(--line);color:var(--mute);margin:.8em 0;padding:.3em 1em}
.prose code{background:#f0ece3;padding:.1em .4em;border-radius:4px;font-size:.9em}
.warn{background:#fbecea;border:1px solid #e6b8b0;color:#8a3327;border-radius:8px;
 padding:9px 13px;margin:10px 0;font-size:.85em;font-weight:600}
.foot{color:var(--mute);font-size:.78em;border-top:1px solid var(--line);margin-top:2em;padding-top:1em}
@media print{body{background:#fff}.page{box-shadow:none;border:none;margin:0;max-width:none}}
</style>"""


def _kpi_block(ps: dict, n_judges: int, is_competition: bool = False) -> str:
    total_mean = ps.get("dimension_total_mean", "?")
    total_max = ps.get("total_max", 100)
    grade = str(ps.get("grade", "?"))
    anchor = ps.get("anchor_5lens_mean")
    gc = _grade_class(grade)
    if is_competition:
        # 竞赛: 无评审等级 (None)、无锚点心证 → 只留总分 + 评委数
        cells = [
            ("总分", f"{total_mean}", f"满分 {total_max} · 竞赛评分 (仅排名, 无定级)"),
            ("评委数", str(n_judges), "AI 智能评价助手 · 独立打分"),
        ]
        grade_idx = -1
    else:
        cells = [
            ("维度评委总分", f"{total_mean}", f"满分 {total_max} · 维度评委均值"),
            ("评审等级", grade, _esc(ps.get("grade_explanation", ""))[:60]),
        ]
        anchor_txt = f"{anchor} / 10" if anchor is not None else "—"
        cells.append(("锚点心证", anchor_txt, "5 镜头 · 独立尺 · 不计入总分"))
        cells.append(("评委数", str(n_judges), "1 anchor + N dimension · 独立打分"))
        grade_idx = 1
    html = ['<div class="kpis">']
    for i, (lbl, val, note) in enumerate(cells):
        cls = f"kpi {gc}" if i == grade_idx else "kpi"
        html.append(f'<div class="{cls}"><div class="lbl">{_esc(lbl)}</div>'
                     f'<div class="val">{_esc(val)}</div><div class="note">{note}</div></div>')
    html.append("</div>")
    return "".join(html)


def _band_block(ps: dict, panel: dict) -> str:
    thr = panel.get("score_threshold") or {}
    rewrite = thr.get("rewrite", 60)
    revise = thr.get("revise", 80)
    grade = str(ps.get("grade", ""))
    gc = _grade_class(grade)
    bands = [
        ("g-rewrite", "重写", f"< {rewrite}"),
        ("g-revise", "修改", f"{rewrite}–{revise}"),
        ("g-pass", "进人工评审", f"≥ {revise}"),
    ]
    html = ['<div class="bands">']
    for cls, t, r in bands:
        on = " on " + gc if cls == gc else ""
        html.append(f'<div class="band{on}"><div class="t">{t}</div><div class="r">{r}</div></div>')
    html.append("</div>")
    return "".join(html)


def _judge_meta(panel: dict) -> dict[str, dict]:
    """panel judges → {slug: {name, category}}。兼容 resolved `judges` 与原始 `judges_override`。"""
    out = {}
    for j in (panel.get("judges_override") or panel.get("judges") or []):
        if not isinstance(j, dict):
            continue
        out[j.get("slug")] = {
            "name": j.get("display_name_cn") or j.get("slug"),
            "category": j.get("judge_category", "dimension"),
        }
    return out


def _lens_defs(panel: dict) -> list[dict]:
    """7 OP2 维度定义 (按 max_score 降序)。兼容 resolved `scoring_lenses` 与原始 `scoring_lenses_override`。"""
    lenses = list(panel.get("scoring_lenses") or panel.get("scoring_lenses_override") or [])
    lenses.sort(key=lambda x: -(x.get("max_score") or 0))
    return lenses


def _color_for(slug: str, category: str, dim_index: int) -> str:
    if category == "anchor":
        return _ANCHOR_COLOR
    return _DIM_COLORS[dim_index % len(_DIM_COLORS)]


def _jname(slug: str, rv: dict, jmeta: dict) -> str:
    """评委显示名: review 自起的角色化名优先, 缺则回退 panel 中文 display_name_cn, 最后 slug。

    (zouxu 那次 review 没自起名 → 回退到 panel 的 '安全平台化评委·邹叙视角（Palo Alto）',
    避免显示成拼音 slug 'zouxu'; 其余评委保留各自 review 自起的角色名。)
    过 _ds: 锚点评委名 '<真名>方法论评委' → '锚点方法论评委', 与 report.md §C 一致 (§9 交付不写真名)。
    """
    return _ds((rv or {}).get("display_name") or jmeta.get(slug, {}).get("name") or slug)


def _rewrite_review_links(md_text: str, name_map: dict) -> str:
    """把正文「评委明细」里的 Obsidian 链接 [[reviews/<slug>]] 转成指向本页评委详情卡
    的锚点链接 [<显示名>](#judge-<slug>)。

    为什么: OP2 渲染器不认 Obsidian [[]] 语法 (那是 render_internal_doc_html 的活),
    该段原样穿过 markdown 库 → 渲成死链文本 '[[reviews/tian]]', 看起来'没有内容'。
    转成锚点链接后, 「评委明细」变成可点击的评委名录, 跳到上方对应详情卡。
    """
    def repl(m: "re.Match") -> str:
        slug = m.group(1).strip()
        name = (name_map.get(slug) or slug).replace("[", "").replace("]", "")
        return f"[{name}](#judge-{slug})"
    return re.sub(r"\[\[reviews/([^\]|]+)\]\]", repl, md_text)


def _dim_total(scores: dict):
    """dimension 评委总分; 无任何数值分 → None (显示 '未出分', 区别于真打 0 分)。"""
    vals = [v for v in (scores or {}).values() if isinstance(v, (int, float))]
    return sum(vals) if vals else None


def _chips_block(order, reviews, jmeta, lenses, total_max: int = 100) -> str:
    html = ['<div class="chips">']
    di = 0
    for slug in order:
        rv = reviews.get(slug, {})
        cat = rv.get("category") or jmeta.get(slug, {}).get("category", "dimension")
        name = _jname(slug, rv, jmeta)
        color = _color_for(slug, cat, di)
        if cat != "anchor":
            di += 1
        conf = rv.get("confidence")
        conf_txt = f"conf {conf}" if conf is not None else ""
        if cat == "anchor":
            vals = [rv.get("scores", {}).get(k) for k in
                    ("reasoning_soundness", "evidence_thesis_coupling",
                     "counter_position_treatment", "falsifiability", "real_world_resilience")]
            vals = [v for v in vals if isinstance(v, (int, float))]
            score = f"{sum(vals)/len(vals):.1f}/10" if vals else "—"
        else:
            tot = _dim_total(rv.get("scores", {}))
            score = f"{tot}/{total_max}" if tot is not None else "未出分"
        html.append(
            f'<div class="chip" style="border-left-color:{color}">'
            f'<div class="n">{_esc(name)}</div>'
            f'<div><span class="s">{_esc(score)}</span> <span class="c">{_esc(conf_txt)}</span></div></div>')
    html.append("</div>")
    return "".join(html)


def _heatmap_block(dim_order, reviews, jmeta, lenses, ps) -> str:
    if not lenses or not dim_order:
        return ""
    lens_means = ps.get("lens_means") or {}
    html = ['<table class="heat"><thead><tr><th class="lens">维度 (满分)</th>']
    for slug in dim_order:
        name = _jname(slug, reviews.get(slug, {}), jmeta)
        html.append(f"<th>{_esc(name)}</th>")
    html.append('<th class="mean">维均</th></tr></thead><tbody>')
    for lens in lenses:
        lslug = lens.get("slug")
        lmax = lens.get("max_score") or 1
        html.append(f'<tr><td class="lens">{_esc(lens.get("display_name_cn") or lslug)} '
                    f'<span style="color:#9a9088">/{lmax}</span></td>')
        for slug in dim_order:
            v = reviews.get(slug, {}).get("scores", {}).get(lslug)
            if isinstance(v, (int, float)):
                bg = _ratio_color(v / lmax)
                html.append(f'<td style="background:{bg}">{v}</td>')
            else:
                html.append("<td>—</td>")
        lm = lens_means.get(lslug)
        html.append(f'<td class="mean">{lm if lm is not None else "—"}</td>')
        html.append("</tr>")
    html.append("</tbody></table>")
    return "".join(html)


_LENS5 = [
    ("reasoning_soundness", "推理一致性"), ("evidence_thesis_coupling", "证据-论点耦合"),
    ("counter_position_treatment", "反方处理"), ("falsifiability", "可证伪性"),
    ("real_world_resilience", "现实韧性"),
]


def _judge_cards(order, reviews, jmeta, lenses, total_max: int = 100) -> str:
    html = []
    di = 0
    for slug in order:
        rv = reviews.get(slug)
        if not rv:
            continue
        cat = rv.get("category") or jmeta.get(slug, {}).get("category", "dimension")
        name = _jname(slug, rv, jmeta)
        color = _color_for(slug, cat, di)
        if cat != "anchor":
            di += 1
        sc = rv.get("scores", {})
        # 大分 + lens 小分行
        if cat == "anchor":
            cells = [(nm, sc.get(k)) for k, nm in _LENS5]
            vals = [v for _, v in cells if isinstance(v, (int, float))]
            big = f"{sum(vals)/len(vals):.1f}/10" if vals else "—"
            unit_lbl = "锚点 · 5 镜头独立尺"
        else:
            cells = [(l.get("display_name_cn") or l.get("slug"), sc.get(l.get("slug")),
                      l.get("max_score")) for l in lenses]
            tot = _dim_total(sc)
            big = f"{tot}/{total_max}" if tot is not None else "未出分"
            unit_lbl = f"维度 · {total_max} 分制"
        lens_html = ['<div class="lens-row">']
        for c in cells:
            if cat == "anchor":
                nm, v = c
                lens_html.append(f'<div class="lens-cell">{_esc(nm)}<br><span class="ls">'
                                 f'{v if v is not None else "—"}</span></div>')
            else:
                nm, v, mx = c
                lens_html.append(f'<div class="lens-cell">{_esc(nm)}<br><span class="ls">'
                                 f'{v if v is not None else "—"}</span><span style="color:#9a9088">'
                                 f'/{mx}</span></div>')
        lens_html.append("</div>")
        star = " ⭐" if cat == "anchor" else ""
        # id 供正文「评委明细」的 [[reviews/<slug>]] 链接跳转到本卡 (见 _rewrite_review_links)
        parts = [f'<div class="jcard" id="judge-{_esc(slug)}" style="border-left-color:{color}">',
                 f'<div class="head"><div><span class="name">{_esc(name)}{star}</span> '
                 f'<span class="doc">{_esc(unit_lbl)}</span></div>'
                 f'<div class="big" style="color:{color}">{_esc(big)}</div></div>',
                 "".join(lens_html)]
        q = rv.get("quote")
        if q:
            parts.append(f'<div class="quote">“{_esc(q)}”</div>')
        adv = rv.get("adversarial") or {}
        if cat != "anchor" and any(adv.values()):
            parts.append(
                '<div class="adv">'
                f'<div><div class="k">IF_THESIS_WRONG</div>{_esc(adv.get("if_thesis_wrong",""))}</div>'
                f'<div><div class="k">CONTRARY_SIGNAL</div>{_esc(adv.get("contrary_signal_observed",""))}</div>'
                f'<div><div class="k">BASE_RATE</div>{_esc(adv.get("base_rate_warning",""))}</div>'
                '</div>')
        parts.append("</div>")
        html.append("".join(parts))
    return "".join(html)


def _md(section_md: str) -> str:
    if not section_md:
        return ""
    if _markdown is None:
        return "<pre>" + _esc(section_md) + "</pre>"
    return _markdown.markdown(
        section_md, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])


# ─── 主入口 ──────────────────────────────────────────────────────────

def render(report_md_path: Path) -> Optional[Path]:
    """OP2 report.md → 同目录 report.html (富可视化)。解析/渲染失败抛异常, 由调用方降级。"""
    report_md_path = Path(report_md_path)
    raw = report_md_path.read_text(encoding="utf-8", errors="replace")
    fm, body = parse_frontmatter(raw)
    ps = fm.get("panel_summary") or {}
    if ps.get("scoring_mode") != "sum_max_score":
        return None  # 非 op2/sum_max → 交回调用方走朴素渲染

    report_dir = report_md_path.parent
    panel = _load_panel(report_dir, fm)
    reviews = _load_reviews(report_dir)
    jmeta = _judge_meta(panel)
    lenses = _lens_defs(panel)

    # 评委顺序: panel judges_override 顺序优先, 否则 frontmatter judges
    order = [j.get("slug") for j in (panel.get("judges_override") or [])] or list(fm.get("judges") or [])
    order = [s for s in order if s]  # 去空
    # 补: reviews 里有但 order 没列的
    for s in reviews:
        if s not in order:
            order.append(s)

    def _cat(slug):
        return reviews.get(slug, {}).get("category") or jmeta.get(slug, {}).get("category", "dimension")
    dim_order = [s for s in order if _cat(s) != "anchor"]

    sections = _split_sections(body)
    topic = fm.get("topic") or _first_h1(body) or report_dir.name
    review_doc = fm.get("review_doc_path", "")
    review_doc = Path(str(review_doc)).name if review_doc else ""
    case_id = fm.get("case_id", "")
    version = fm.get("version", 1)
    sensitivity = fm.get("sensitivity", "confidential")

    # 竞赛 (workshop): panel anchor_judge: null (OP2 判断书是 tian) → 换外壳:
    # 无 OP2 门槛卡 / 无 30/90/365 attribution / 无锚点心证, 分制按实际 (10) 不写死 100。
    is_competition = not panel.get("anchor_judge")
    total_max = ps.get("total_max", 100)
    n_lenses = len(lenses) or 0
    if is_competition:
        badge_lbl = f"竞赛 · {total_max} 分制"
        sub_line = f'{len(order)} 评委 AI 评价 · {total_max} 分制 · 排名用 (无门槛/无归因)'
    else:
        badge_lbl = "OP2 · 100 分制"
        sub_line = f'{len(order)} 评委独立评议 · {total_max} 分制 · 30/90/365 attribution'

    head = [
        '<div class="page">',
        '<div class="badges"><span class="review">REVIEW</span>'
        f'<span>INTERNAL v2</span><span>{_esc(badge_lbl)}</span></div>',
        f"<h1>{_esc(topic)}</h1>",
        f'<div class="sub">{sub_line}</div>',
        f'<div class="meta">CASE {_esc(case_id)} · v{_esc(version)} · sensitivity: {_esc(sensitivity)}'
        + (f' · 被评议: {_esc(review_doc)}' if review_doc else "") + "</div>",
        _kpi_block(ps, len(order), is_competition=is_competition),
    ]
    if not is_competition:
        head.append(_band_block(ps, panel))   # 竞赛无门槛分级, 不渲染重写/修改/人工评审带
    _nsj = ps.get("judges_no_score") or []
    if _nsj:
        names = ", ".join(_jname(s, reviews.get(s, {}), jmeta) for s in _nsj)
        head.append(f'<div class="warn">⚠ {len(_nsj)} 位评委未出分, 未计入维度总分: {_esc(names)}</div>')
    head += [
        "<h2>评委编组</h2>",
        _chips_block(order, reviews, jmeta, lenses, total_max=total_max),
    ]

    heat = _heatmap_block(dim_order, reviews, jmeta, lenses, ps)
    if heat:
        head.append("<h2>维度 × 评委 得分热力图</h2>")
        head.append('<div class="sub" style="font-size:.85em">单元格按"占该维满分比例"着色 '
                     '(绿高/红低); 各维满分不同, 不跨维比绝对分。</div>')
        head.append(heat)

    cards = _judge_cards(order, reviews, jmeta, lenses, total_max=total_max)
    if cards:
        head.append("<h2>评委详情</h2>")
        head.append(cards)

    # 竞赛无 30/90/365 归因 → 不渲染 ATTR 段 (即便正文有也跳过)
    _secs = (("A", "§A · 原方案摘要"), ("B", "§B · 评委评议"), ("C", "§C · 修订建议清单"))
    if not is_competition:
        _secs = _secs + (("ATTR", "30/90/365 Attribution"),)
    # 评委 slug → 显示名, 供「评委明细」的 [[reviews/<slug>]] 链接转成跳到详情卡的锚点
    name_map = {s: _jname(s, reviews.get(s, {}), jmeta) for s in order}
    for key, title in _secs:
        seg = sections.get(key)
        if seg:
            # 去掉段自身的 "## §X ..." 首行 (用统一 h2 标题), 其余 markdown 渲染
            seg_body = re.sub(r"^##\s.*$", "", seg, count=1, flags=re.M).strip()
            seg_body = _rewrite_review_links(seg_body, name_map)  # [[reviews/x]] → 锚点链接
            head.append(f"<h2>{_esc(title)}</h2>")
            head.append(f'<div class="prose">{_md(_ds(seg_body))}</div>')  # _ds: 来源脱敏

    _mode_lbl = "竞赛排名" if is_competition else "REVIEW"
    head.append(
        '<div class="foot">boss-vault Phase 6 · 机械渲染 (render_op2_report_html) · '
        f'panel: {_esc(fm.get("panel","op2-company"))} · {total_max} 分制 {n_lenses} 维度 · '
        f'{_mode_lbl} 模式 · 评委独立打分互不可见 · confidential 仅内部</div>')
    head.append("</div>")

    doc = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width,initial-scale=1">'
           f"<title>{_esc(topic)}</title>{_CSS}</head><body>" + "".join(head) + "</body></html>")

    out = report_md_path.with_name("report.html")
    out.write_text(doc, encoding="utf-8")
    return out


def _first_h1(body: str) -> str:
    m = re.search(r"^#\s+(.+)$", body, re.M)
    return m.group(1).strip() if m else ""


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: render_op2_report_html.py <report.md>", file=sys.stderr)
        sys.exit(2)
    res = render(Path(sys.argv[1]))
    print(f"wrote={res}")
    sys.exit(0 if res else 1)
