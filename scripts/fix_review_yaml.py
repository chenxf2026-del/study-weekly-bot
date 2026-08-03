#!/usr/bin/env python3
"""
fix_review_yaml.py — Phase 4 评委 review frontmatter yaml 纪律后处理器

P1 缺口 #2 (T11 实战 2026-05-26 发现, dev-plan v2.10 修):
评委 Agent 写 yaml frontmatter 含中文标点 / 百分号 / 长字符串 / 内嵌引号时,
yaml.safe_load 解析失败. 本脚本 auto-detect + auto-fix:

  - adversarial_view 三字段 (if_thesis_wrong / contrary_signal_observed / base_rate_warning)
    若值是单行长字符串 (含中文标点 / `%` / `:` / `"` / `'` 等), 自动用单引号包裹 + 内单引号双倍转义
  - doctrine_anchors_used 列表项 (Agent 自加字段, 非 schema), 若 '- key: value' 形式含双引号或冒号,
    转为 '- "key: value"' 整体字符串

自动修复 (2026-07-02 加, C-2026-0074 zouxu 实战):
  - **frontmatter 不在文件开头** (LLM 把分析正文写在 frontmatter 前, '---' 块被夹在正文中间):
    把嵌入的 `---\n<yaml judge/scores>\n---` 块提到文件顶部, 前置正文并入 body。
    否则 extract_frontmatter_text (要求 ^--- 顶格) 抽不到 → 该评委 scores/名字全丢 →
    Phase 5 聚合静默排除该评委, 拉低 dimension_total_mean (zouxu 那次 58 分被丢, 均值只算 5 位)。

不修复的情况 (留给人工):
  - 缺整段 frontmatter (通篇无成对 '---' 块, 或块内非 review 元数据)
  - 5 镜头 scores 字段缺失或非整数 (这是 schema 必填项, 缺则 verify 阻断)

用法:
  python scripts/fix_review_yaml.py <review.md>          # 单文件 in-place
  python scripts/fix_review_yaml.py <reviews_dir>        # 目录, 递归 *.md
  python scripts/fix_review_yaml.py --check <path>       # 仅检查, 不修改 (exit 0/1)

退出码:
  0 — 所有 frontmatter parse OK (修复后)
  1 — 有文件 frontmatter parse 仍失败 (修复无能为力)
  2 — 用法错误
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

import yaml


FRONTMATTER_RE = re.compile(r"^(---\n)(.*?)(\n---\n)", re.DOTALL)

# adversarial_view 必填三字段 (smoke_e2e.verify 强制)
ADVERSARIAL_FIELDS = ("if_thesis_wrong", "contrary_signal_observed", "base_rate_warning")


def _parse_frontmatter(text: str) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """
    Returns: (parsed_dict_or_None, frontmatter_raw_str_or_None, body_str_or_None)
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, None, None
    fm_str = m.group(2)
    body = text[m.end():]
    try:
        parsed = yaml.safe_load(fm_str)
        return parsed, fm_str, body
    except yaml.YAMLError:
        return None, fm_str, body


def _fix_adversarial_view_quoting(fm_str: str) -> str:
    """把 adversarial_view 下的三字段值 (不带引号 + 含特殊字符) 改为单引号包裹"""
    def fix_field(m):
        indent = m.group(1)
        key = m.group(2)
        val = m.group(3).strip()
        # 已是单引号或双引号包裹的不动
        if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
            return f"{indent}{key}: {val}"
        # 已是块标量 (|- / |+ / >-) 不动
        if val.startswith("|") or val.startswith(">"):
            return f"{indent}{key}: {val}"
        # auto-quote, 内单引号转双倍 (yaml 单引号字符串规则)
        val_escaped = val.replace("'", "''")
        return f"{indent}{key}: '{val_escaped}'"

    keys_pattern = "|".join(re.escape(k) for k in ADVERSARIAL_FIELDS)
    return re.sub(
        rf"^(  )({keys_pattern}):\s*(.+)$",
        fix_field,
        fm_str,
        flags=re.MULTILINE,
    )


def _fix_list_items_with_internal_colons_or_quotes(fm_str: str) -> str:
    """
    对任何 '- key: value' 形式的列表项, 若 value 含双引号 / 嵌套冒号,
    转为 '- "key: value"' 整段字符串. 这处理 doctrine_anchors_used 这类 Agent 自加字段.

    保守做法: 只处理 2-空格缩进 (顶层列表) 的 '- text: more text "with quotes"'
    """
    def fix_item(m):
        indent = m.group(1)
        text = m.group(2)
        # 已被单/双引号 wrap 的整体不动
        if text.startswith("'") or text.startswith('"'):
            return m.group(0)
        # 不含双引号也不含子冒号的不动
        if '"' not in text and ":" not in text[text.find(" "):] if " " in text else True:
            return m.group(0)
        # 试 yaml parse, 不报错说明 OK, 跳过
        try:
            yaml.safe_load(f"- {text}")
            return m.group(0)
        except yaml.YAMLError:
            pass
        # 包整段为单引号 (内单引号双倍)
        text_escaped = text.replace("'", "''")
        return f"{indent}- '{text_escaped}'"

    return re.sub(
        r"^(  )- (.+)$",
        fix_item,
        fm_str,
        flags=re.MULTILINE,
    )


def _fix_scalar_fields_with_embedded_quotes(fm_str: str) -> str:
    """`key: "…含未转义双引号…"` 这类双引号标量, LLM 在值内塞了裸双引号
    (如 rewrite_example: "…复述"战略 OS"…") → YAML 在内层双引号处提前闭合报错。
    检测并改用**单引号**包裹 (值内单引号 '' 转义; 双引号在单引号 YAML 里本就合法)。
    覆盖顶层与任意缩进的标量字段 (position_value / rewrite_example / core_label /
    score_reasons.* / deductions[].reason 等)。只碰双引号包裹且原样 parse 失败的行, 零误伤。"""
    def fix_line(m):
        indent, key, val = m.group(1), m.group(2), m.group(3)
        # 原样能 parse 说明合法 (内层双引号已转义 / 无内层双引号) → 不动
        try:
            yaml.safe_load(f"k: {val}")
            return m.group(0)
        except yaml.YAMLError:
            pass
        inner = val[1:-1]                      # 剥 LLM 加的外层双引号
        inner_sq = inner.replace("'", "''")    # 单引号 YAML: ' → ''
        return f"{indent}{key}: '{inner_sq}'"

    # 值以 " 开头、以 " 结尾 (行尾, 允许尾随空格); 贪婪 .* 吃到本行最后一个 "
    return re.sub(
        r'^([ \t]*)([A-Za-z_][A-Za-z0-9_]*): (".*")[ \t]*$',
        fix_line, fm_str, flags=re.MULTILINE,
    )


# frontmatter 行首的 KEY: (缩进 + 键名 + 冒号)。值在冒号后, 不动。
_KEY_LINE_RE = re.compile(r"^([ \t]*)([A-Za-z][A-Za-z0-9_]*)(:)(?=[ \t]|$)", re.MULTILINE)


def _lowercase_frontmatter_keys(fm_str: str) -> tuple[str, bool]:
    """把 frontmatter 每行行首的 `KEY:` 归一到小写 (值原样不动)。

    LLM 偶发把整段 key 大写 (JUDGE/SCORES/BIZ_VALUE/ADVERSARIAL_VIEW) → 下游按小写 schema key
    抽分抽不到 → 判"解析失败" (2026-07-04 workshop-ai-evaluator 实战)。且大写还让后续 adversarial
    quote-fix (按小写字段名匹配) 失效。**文本级**操作, 不依赖 parse (值含冒号/% 导致 parse 失败时
    仍能先归一 key, 再交给 quote-fix)。只在检测到大写 SCORES / JUDGE 时动手, 正常小写文件零副作用。"""
    if not re.search(r"^[ \t]*(SCORES|JUDGE)\b", fm_str, re.MULTILINE):
        return fm_str, False
    return _KEY_LINE_RE.sub(lambda m: f"{m.group(1)}{m.group(2).lower()}{m.group(3)}", fm_str), True


def _strip_wrapping_code_fence(text: str) -> str:
    """剥掉把整篇 review 包起来的 markdown 代码围栏 (GLM 偶发, frontmatter 不顶格)。
    复用 _export_helpers 单一源 (同一围栏正则); 导入失败时退化为不剥 (保持原行为)。"""
    try:
        from _export_helpers import strip_wrapping_code_fence
    except Exception:
        return text
    return strip_wrapping_code_fence(text)


# 嵌在正文中间的 frontmatter 块 (成对 --- 之间的 yaml, 含 review 元数据键)
_EMBEDDED_FM_RE = re.compile(r"(?:\A|\n)---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)


# 块内含 review 元数据键 (judge:/scores:, 大小写不敏感) → 认作 frontmatter
_FM_KEY_RE = re.compile(r"^(judge|scores)[ \t]*:", re.MULTILINE | re.IGNORECASE)


def _hoist_embedded_frontmatter(text: str) -> tuple[str, bool]:
    """LLM 把分析正文写在 frontmatter 前时, 把嵌入的 `---\\n<yaml>\\n---` 块提到文件顶部。

    只在: (1) 文件不以 '---' 开头 且 (2) 正文中存在一个成对 --- 块, 块内含 review 元数据键
    (judge:/scores:) 时才动手。**宽松检测**: 只看键在不在, 不要求整块先 parse 成功 —— 值含
    未加引号的冒号/% (adversarial 长句) 时 parse 会失败, 提上去后交给下游 quote-fix 修
    (2026-07-04 huangrenxun 实战: 正文前置 + `(source: …)` 冒号双重卡住)。
    把该块移到最前, frontmatter 前后的正文并入 body。非该情形原样返回 (原文, False), 零副作用。
    """
    if text.lstrip().startswith("---"):
        return text, False  # 已在顶部 (或纯 --- 开头), 交给常规流程
    # 扫**所有** '---' 分隔行, 试每一对相邻分隔 —— 不能用 finditer 的非重叠成对, 否则当 LLM 把
    # 思考过程也用 `---…---` 包住时 (前言块), 它会吃掉共享的 '---', 使真 frontmatter (judge:) 那块
    # 少一个上分隔而匹配不到 → fail-no-frontmatter (2026-07 干部周报 backfill 实战)。
    delims = list(re.finditer(r"^---[ \t]*$", text, re.MULTILINE))
    for i in range(len(delims) - 1):
        block = text[delims[i].end():delims[i + 1].start()].strip("\n")
        if not block:
            continue
        # 宽松: 块内有 judge:/scores: 键即认作 frontmatter (不要求 parse 成功);
        # 兜底: 或能 parse 成含键 dict (键名非常规时)。
        if not _FM_KEY_RE.search(block):
            try:
                parsed = yaml.safe_load(block)
            except yaml.YAMLError:
                parsed = None
            if not (isinstance(parsed, dict) and ("scores" in parsed or "judge" in parsed)):
                continue
        leading = text[:delims[i].start()].strip()    # frontmatter 前的正文 (含前言块, 并入 body)
        trailing = text[delims[i + 1].end():].strip()  # frontmatter 后的正文
        body = "\n\n".join(p for p in (leading, trailing) if p)
        new_text = f"---\n{block}\n---\n" + (f"\n{body}\n" if body else "")
        return new_text, True
    return text, False


def fix_review_frontmatter(text: str) -> tuple[str, bool, str]:
    """
    Returns: (fixed_text, was_modified, status_msg)
    status_msg ∈ {"ok-noop", "ok-fixed", "fail-parse-still", "fail-no-frontmatter"}
    """
    original = text
    # GLM 偶尔把整篇 review 包进 ```markdown … ``` → frontmatter 不顶格。先剥围栏再判。
    text = _strip_wrapping_code_fence(text)
    # LLM 把分析正文写在 frontmatter 前 → 把 frontmatter 块提到顶部 (C-2026-0074 zouxu)。
    text, hoisted = _hoist_embedded_frontmatter(text)
    fence_stripped = (text != original)  # 剥围栏或 hoist 任一发生都需写回

    parsed, fm_str, body = _parse_frontmatter(text)
    if fm_str is None:
        return original, False, "fail-no-frontmatter"

    # LLM 把 frontmatter key 整段大写 (JUDGE/SCORES/...) → 先文本级归一到小写,
    # 再交给后续 parse / quote-fix (值原样不动)。
    fm_str, case_fixed = _lowercase_frontmatter_keys(fm_str)
    if case_fixed:
        try:
            parsed = yaml.safe_load(fm_str)
        except yaml.YAMLError:
            parsed = None

    if parsed is not None:
        # 剥围栏 / hoist / 大写归一 任一发生都需写回小写化的 fm_str; 否则无需修
        if fence_stripped or case_fixed:
            return f"---\n{fm_str}\n---\n{body or ''}", True, "ok-fixed"
        return original, False, "ok-noop"

    # 尝试三步修复 (在已剥围栏 + 已小写化的文本上)
    fixed_fm = _fix_adversarial_view_quoting(fm_str)
    fixed_fm = _fix_list_items_with_internal_colons_or_quotes(fixed_fm)
    fixed_fm = _fix_scalar_fields_with_embedded_quotes(fixed_fm)

    if fixed_fm == fm_str and not (fence_stripped or case_fixed):
        # 无可修, 还是 parse 失败
        return original, False, "fail-parse-still"

    # 验修复后能 parse
    try:
        yaml.safe_load(fixed_fm)
    except yaml.YAMLError:
        return original, False, "fail-parse-still"

    new_text = f"---\n{fixed_fm}\n---\n{body or ''}"
    return new_text, True, "ok-fixed"


def process_file(path: Path, check_only: bool = False) -> str:
    """处理单文件. 返回 status_msg."""
    text = path.read_text(encoding="utf-8")
    new_text, modified, status = fix_review_frontmatter(text)
    if modified and not check_only:
        path.write_text(new_text, encoding="utf-8")
    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 4 review yaml frontmatter 后处理")
    ap.add_argument("path", help="单文件 .md 或目录 (递归 *.md)")
    ap.add_argument("--check", action="store_true", help="仅检查不修改 (exit 0 全过 / 1 有 fail)")
    args = ap.parse_args(argv)

    p = Path(args.path)
    if not p.exists():
        print(f"path 不存在: {p}", file=sys.stderr)
        return 2

    files = [p] if p.is_file() else sorted(p.rglob("*.md"))
    if not files:
        print(f"未找到 .md 文件: {p}", file=sys.stderr)
        return 2

    stats: dict[str, int] = {}
    fails: list[Path] = []
    for f in files:
        status = process_file(f, check_only=args.check)
        stats[status] = stats.get(status, 0) + 1
        if status.startswith("fail"):
            fails.append(f)

    for s, n in sorted(stats.items()):
        print(f"  {s}: {n}")
    for f in fails:
        print(f"  ✗ {f}: parse still fails (人工修)")

    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
