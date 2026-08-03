"""
_export_helpers.py — boss Phase 6 export 共享 helper 模块.

下划线前缀 = Python 惯例的"内部模块". 由 `export_case_report.py` (Set A) 与
`export_phase6.py` (Set B/C dispatcher) 共用. 不应被外部直接 CLI 调用.

抽取范围 (P1.2):
  - VAULT_ROOT + CHROME 路径常量
  - parse_frontmatter(text) → (dict, body) yaml frontmatter 解析
  - find_topic_from_h1(body) → str  从 # H1 'Report · <topic> · v<n>' 抽中段
  - display_path(p) → str  vault 内用相对路径, 外部用绝对路径
  - chrome_pdf(html_path, pdf_path) → None  Chrome headless 渲 PDF

不抽 (各自专有):
  - assemble_markdown (Set A 7 段合并)
  - compute_nn / derive_short_slug / render_template (phase6 dispatcher)
  - markdown_to_html (Set A pandoc + HTML_CSS)
  - redact_check_strict (phase6 finalize-b)
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# ─── 路径常量 ───────────────────────────────────────────────────────

VAULT_ROOT = Path(__file__).parent.parent.resolve()

# v0.6 R8 · Chrome 探测链 (跨平台): macOS .app → PATH 上的常见二进制
_CHROME_MACOS = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_CHROME_PATH_CANDIDATES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
)


def find_chrome() -> str | None:
    """返回可用的 Chrome/Chromium 可执行路径; 全缺返回 None (调用方降级 md/html)。"""
    if Path(_CHROME_MACOS).exists():
        return _CHROME_MACOS
    import shutil
    for name in _CHROME_PATH_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


# 向后兼容: 旧调用方仍 import CHROME (None 表示未找到)
CHROME = find_chrome() or _CHROME_MACOS


# ─── frontmatter + H1 解析 ─────────────────────────────────────────


# GLM 等模型偶尔把整篇结构化输出 (review / report) 包进 markdown 代码围栏
# (```markdown … ``` / ```yaml … ```), 导致 frontmatter 不顶格 → ^--- 不匹配 → 解析失败
# (fail-no-frontmatter, 2026-06-18 灰度实测 strategic-vision.md)。下面工具仅当**开头第一行**
# 是围栏行时剥它 + 配对的结尾围栏, 非围栏开头原样返回 (不误伤正文内代码块)。
_FENCE_OPEN_RE = re.compile("^\\ufeff?" + r"[ \t]*```[ \t]*[A-Za-z0-9_.+-]*[ \t]*\r?\n")
_FENCE_CLOSE_RE = re.compile(r"\r?\n[ \t]*```[ \t]*\r?\n?[ \t\r\n]*\Z")


def strip_wrapping_code_fence(text: str) -> str:
    """剥掉把整篇文档包起来的 markdown 代码围栏 (仅开头是围栏时)。非围栏开头原样返回。"""
    m = _FENCE_OPEN_RE.match(text)
    if not m:
        return text
    return _FENCE_CLOSE_RE.sub("\n", text[m.end():])


def _match_frontmatter(text: str):
    """匹配顶格 frontmatter; 顶格失败时剥一层代码围栏后再试 (GLM 围栏兜底)。
    返回 (match_or_None, 用于切 body 的实际文本)。仅在顶格匹配失败时才动 (clean 文件零行为变化)。"""
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if m:
        return m, text
    stripped = strip_wrapping_code_fence(text)
    if stripped != text:
        m2 = re.match(r"^---\n(.*?)\n---\n", stripped, re.DOTALL)
        if m2:
            return m2, stripped
    return None, text


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse markdown yaml frontmatter.

    Returns (frontmatter_dict, body). 空 frontmatter / 无效 yaml / 无 --- 都返回
    ({}, original_text). 调用方不需要 None 检查.

    懒导入 yaml (调用方已 import, 这里避免循环).
    """
    m, work = _match_frontmatter(text)
    if not m:
        return {}, text
    try:
        import yaml
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    return fm, work[m.end():]


# v0.11 C3 · frontmatter 解析单一源 (仓库级统一)
#
# 此前同一 strict 模式在 8+ 模块各有拷贝 (skill_lint ×2 / monthly_review /
# smoke_e2e / run_pipeline_local ×2 / framework_compare)。
# 两种语义并存且都合法:
#   - parse_frontmatter (上)      : 宽松 — 失败返 ({}, body), 导出链用
#   - parse_frontmatter_strict (下): 严格 — 失败/缺块/空块返 None, "跳过该文件"语义
# 外围独立工具 (build_* / render_*) 留自然汰换, 不在本轮强迁。


def extract_frontmatter_text(text: str) -> "str | None":
    """抽 frontmatter 原文 (不解析)。无 ---块 返回 None。
    供需要自定义 fallback 解析的调用方 (如 run_pipeline_local review 解析) 复用正则单源。
    顶格匹配失败时容忍一层 GLM 代码围栏 (strip_wrapping_code_fence)。"""
    m, _ = _match_frontmatter(text)
    return m.group(1) if m else None


def parse_frontmatter_strict(text: str) -> "dict | None":
    """严格解析: 无 ---块 / yaml 非法 / 空 frontmatter → None (跳过文件语义)。
    与历史各拷贝逐位一致 (safe_load 结果原样返回, 空块 safe_load("")=None)。"""
    fm_text = extract_frontmatter_text(text)
    if fm_text is None:
        return None
    try:
        import yaml
        return yaml.safe_load(fm_text)
    except Exception:
        return None


def find_topic_from_h1(body: str) -> str:
    """从 # H1 'Report · <topic> · v<n>' 抽中段.

    支持的格式:
      - '# Report · 某议题 · v1' → '某议题'
      - '# 某议题' → '某议题'
      - '# Some title · v3' → 'Some title'
      - 无 H1 → '(议题未知)'
    """
    m = re.search(
        r"^#\s+(?:Report\s*·\s*)?(.+?)(?:\s*·\s*v\d+)?\s*$",
        body, re.MULTILINE,
    )
    return m.group(1).strip() if m else "(议题未知)"


# ─── 路径显示 ──────────────────────────────────────────────────────


def display_path(p: Path) -> str:
    """vault 内用相对路径, 外部用绝对路径.

    Bug #1 (commit 1591d86) 来源: `relative_to(VAULT_ROOT)` 在 --out-dir
    指向 vault 外时抛 ValueError, 即使文件已写出, 脚本也崩溃. 此 helper 用
    try-except 守, vault 外的路径退化为绝对路径展示.
    """
    try:
        return str(p.relative_to(VAULT_ROOT))
    except ValueError:
        return str(p)


# ─── Chrome PDF ────────────────────────────────────────────────────


def chrome_pdf(html_path: Path, pdf_path: Path) -> None:
    """Chrome headless 渲 PDF.

    flags 与 prior 6 案手动跑一致:
      - `--no-pdf-header-footer` 去 header/footer
      - `--disable-gpu` macOS headless 推荐
      - `--print-to-pdf=<abs>` 输出绝对路径

    抛错:
      - FileNotFoundError: Chrome/Chromium 全缺 (v0.6 R8: 调用方应 catch 后降级
        md/html, 等价 --export-skip-pdf, 并提示安装 chromium)
      - subprocess.CalledProcessError: Chrome 返回非 0
    """
    chrome = find_chrome()
    if not chrome:
        raise FileNotFoundError(
            "Chrome/Chromium 未找到 (探测: macOS Chrome.app → google-chrome → "
            "chromium)。PDF 跳过 — 装 chromium 或用 --export-skip-pdf。"
        )
    subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={pdf_path.absolute()}",
         f"file://{html_path.absolute()}"],
        check=True, capture_output=True,
    )
