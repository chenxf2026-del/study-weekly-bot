#!/usr/bin/env python3
"""
render_review_formats.py — review report.md → 自包含 report.html (v1.1 A4 · best-effort, 免 pandoc)

飞书群评审除 md 外多发 html。用 python-markdown (pip, 进 venv) 渲, **不依赖系统 pandoc/chromium**。
PDF 留后 (需 chromium, 见 docs/v1.0/feishu-app-checklist.md)。

纪律:
- **best-effort**: markdown 库缺 / 渲染异常 → 返回 None, **绝不影响 report.md 投递** (worker/notify 降级)。
- 只渲 frontmatter 之后的正文 (panel_summary 等元数据不进 html body, 复用 _export_helpers 解析单一源)。
- 内容与 report.md 同源 → 脱敏由 notify 对 report.md 内容统一把关 (html 不重复内容闸, 仅文件名闸)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# 自包含、简洁可读的报告样式 (无外链, 无字体 CDN — 离线/飞书内打开均可)
_CSS = """<style>
:root{--ink:#1f2328;--mute:#656d76;--line:#d0d7de;--accent:#b8331f;--bg:#fff}
*{box-sizing:border-box}
body{margin:0;background:#f6f8fa;color:var(--ink);
 font:16px/1.7 -apple-system,"Segoe UI","Source Han Sans SC","PingFang SC",sans-serif}
main{max-width:860px;margin:24px auto;padding:40px 48px;background:var(--bg);
 border:1px solid var(--line);border-radius:10px}
h1,h2,h3,h4{line-height:1.3;margin:1.6em 0 .6em;font-weight:650}
h1{font-size:1.7em;border-bottom:2px solid var(--accent);padding-bottom:.3em}
h2{font-size:1.35em;border-bottom:1px solid var(--line);padding-bottom:.25em}
h3{font-size:1.12em}
a{color:var(--accent)}
code{background:#f0f1f3;padding:.12em .4em;border-radius:4px;font-size:.88em;
 font-family:"SF Mono",Consolas,monospace}
pre{background:#f6f8fa;border:1px solid var(--line);border-radius:8px;padding:14px 16px;overflow:auto}
pre code{background:none;padding:0}
blockquote{margin:1em 0;padding:.4em 1em;border-left:4px solid var(--line);color:var(--mute)}
table{border-collapse:collapse;width:100%;margin:1em 0;font-size:.92em}
th,td{border:1px solid var(--line);padding:7px 11px;text-align:left}
th{background:#f6f8fa}
hr{border:0;border-top:1px solid var(--line);margin:2em 0}
.muted{color:var(--mute);font-size:.85em}
</style>"""


def render_html(report_md_path: Path) -> Optional[Path]:
    """report.md → 同目录 report.html。成功返回 html 路径; 任何失败 (库缺/异常) → None。

    OP2 / sum_max_score / REVIEW 报告优先走机械富渲染 (render_op2_report_html: KPI 卡 /
    评级带 / 热力图 / 评委卡); 富渲染任何异常 → 静默降级到下方朴素 markdown 渲染, 绝不失投。
    """
    try:
        import render_op2_report_html
        rich = render_op2_report_html.render(report_md_path)
        if rich is not None:
            return rich   # sum_max 富渲染成功
        # rich 返回 None = 非 sum_max, 继续朴素渲染
    except Exception:
        pass  # 富渲染异常 (解析/库/数据) → 降级朴素渲染, 不影响投递

    try:
        import markdown  # pip 依赖 (requirements.txt); 缺则降级 None
    except Exception:
        return None
    try:
        if not report_md_path.exists():
            return None
        raw = report_md_path.read_text(encoding="utf-8", errors="replace")
        # 去 frontmatter (panel_summary 等元数据不进 body) — 复用解析单一源
        try:
            from _export_helpers import parse_frontmatter
            _, body_md = parse_frontmatter(raw)
        except Exception:
            body_md = raw
        # 交付前来源脱敏 (belt-and-suspenders: 老报告 report.md 若仍含内部路径/真名, 这里兜底)
        try:
            from desensitize import desensitize_sources
            body_md = desensitize_sources(body_md)
        except Exception:
            pass
        body_html = markdown.markdown(
            body_md, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
        title = report_md_path.parent.name or "review report"
        html = (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{title}</title>{_CSS}</head>"
            f"<body><main>{body_html}</main></body></html>"
        )
        out = report_md_path.with_suffix(".html")   # 按输入 stem 命名 (report.md→report.html, report-7block.md→report-7block.html)
        out.write_text(html, encoding="utf-8")
        return out
    except Exception:
        return None


def render_pdf(html_path: Path) -> Optional[Path]:
    """report.html → 同目录 report.pdf (Chrome/Chromium headless, A4)。

    best-effort: Chrome/Chromium 缺 (FileNotFoundError) / 渲染失败 / html 缺 / 任何异常 → None,
    **绝不影响 md+html 投递** (worker 降级)。VM 需系统 chromium (sudo apt install chromium)。
    """
    try:
        from _export_helpers import chrome_pdf
    except Exception:
        return None
    try:
        if not html_path.exists():
            return None
        pdf_path = html_path.with_suffix(".pdf")   # 按输入 stem (report.html→report.pdf, report-7block.html→report-7block.pdf)
        chrome_pdf(html_path, pdf_path)   # Chrome 缺 → FileNotFoundError; 失败 → CalledProcessError
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return pdf_path
        return None
    except Exception:
        return None


def render_all(report_md_path: Path) -> dict[str, Optional[Path]]:
    """report.md → {html, pdf} (各 best-effort)。html 成功才尝试 pdf (pdf 由 html 渲)。"""
    html = render_html(report_md_path)
    pdf = render_pdf(html) if html else None
    return {"html": html, "pdf": pdf}


if __name__ == "__main__":
    import sys
    res = render_all(Path(sys.argv[1])) if len(sys.argv) > 1 else {"html": None, "pdf": None}
    print(f"html={res['html']} pdf={res['pdf']}")
    sys.exit(0 if res["html"] else 1)
