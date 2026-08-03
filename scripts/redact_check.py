#!/usr/bin/env python3
"""
redact_check.py — 出站脱敏闸 (fail-close)

任何输出到外部网络的内容必经此闸。命中任何 SENSITIVE_PATTERNS 中的 regex 即阻断。

★ 本仓改动 (剥离自上游 boss-vault):
  上游版本在代码里写死了**它自己的**锚点真名 / 团队真名 / 客户名 / 机构名 —— 那份表
  在本部署里保护不了任何东西, 却会把那些名字泄露给本仓的每一位读者。故已全部移除。
  **结构性**规则 (精确财务数字 / 精确百分比 / lark:// 深链 / case_id / fc_id) 原样保留。
  本部署自己要保护的词填 `config/redact_local.yaml` (gitignored, 见 .example)。

用法:
    # 检查文件
    python scripts/redact_check.py path/to/output.json
    python scripts/redact_check.py path/to/output.md

    # 检查 stdin
    cat content.txt | python scripts/redact_check.py -

    # 批量检查 staged git changes
    python scripts/redact_check.py --staged --fail-close

    # 干跑 (用 test fixtures)
    python scripts/redact_check.py --dry-run --test-fixtures

退出码:
    0 — 通过, 可发布
    1 — 阻断, 含敏感内容
    2 — 用法错误
    3 — 系统错误 (fail-close, 默认按阻断处理)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

# ─────────────────────────────────────────────────────────────────────
# 敏感正则 —— 只保留**结构性**规则 (与"具体是谁"无关, 换个部署照样成立)。
#
# 具体的人名 / 客户名 / 机构名属于**本部署自己的机密**, 不写在代码里 ——
# 写进代码就等于: 谁能读这个仓, 谁就拿到了这份名单。填 config/redact_local.yaml。
#
# (上游 boss-vault 的版本在此写死了它自己的锚点真名 / 团队真名 / 客户名。剥离本仓时
#  已整体移除 —— 那些名字在本部署里保护不了任何东西, 只会泄露它们本身。)
#
# 新增/删除结构性规则需经 owner review (改的是全员出站行为)。
# ─────────────────────────────────────────────────────────────────────

SENSITIVE_PATTERNS: list[tuple[str, str, str]] = [
    # (pattern_id, regex, human_description)
    #
    # 注意: Python \b 对中文不可靠 (中文字符不算 word char), 中文模式不用 \b。

    # 链接 — 飞书内部深链外发即失效, 且暴露内部结构
    ("url_lark",        r"lark://",                                    "飞书内部 deeplink"),
    ("url_feishu_app",  r"feishu://",                                  "飞书 app 内 link"),

    # 数字 — 量级可留 ("约 10%"), 精确值不外发
    ("num_money",       r"(?:\d{4,}|\d{1,3}(?:,\d{3})+)\.\d{2}\s*(亿|万|RMB|元|USD|\$)?", "精确财务数字 (含千分位)"),
    ("num_pct_exact",   r"\d+\.\d{1,2}%",                              "精确百分比 (留量级 '约 10%', 删 '14.3%')"),

    # 内部标识
    ("id_case",         r"\bC-\d{4}-\d{4}\b",                          "case_id"),
    ("id_fc",           r"\bFC-\d{4}-\d{4}\b",                         "failure card id"),
]


# ─── 本部署自己的敏感词 (config/redact_local.yaml · gitignored) ──────
# 缺文件 / YAML 写错 / 没装 yaml → 一律降级为空表, 本闸的结构性规则不受影响。
# 故意**不**在这一层 fail-close: 没配本地词表是刚部署时的常态, 不该让出站整体瘫掉;
# 真正的 fail-close 在「命中即不发原文」那一层 (见 feishu_notify)。

_LOCAL_CFG = Path(__file__).resolve().parent.parent / "config" / "redact_local.yaml"


def _load_local_terms() -> tuple[list[str], list[str], dict[str, str]]:
    """读本地敏感词表 → (出站词, 文件名词, 交付脱敏映射)。任何异常降级为空。"""
    try:
        import yaml
        data = yaml.safe_load(_LOCAL_CFG.read_text(encoding="utf-8")) or {}
        return ([str(t) for t in (data.get("blocked_terms") or []) if str(t).strip()],
                [str(t) for t in (data.get("blocked_filename_terms") or []) if str(t).strip()],
                {str(k): str(v) for k, v in (data.get("mask_names") or {}).items()})
    except Exception:  # noqa: BLE001
        return [], [], {}


def _local_patterns(terms: list[str], prefix: str) -> list[tuple[str, str, str]]:
    """本地词 (纯字符串, 非正则) → pattern 三元组; 转义后大小写不敏感匹配。"""
    return [(f"{prefix}_{i}", f"(?i){re.escape(t)}",
             f"本地敏感词 {t!r} (config/redact_local.yaml)")
            for i, t in enumerate(terms)]


_LOCAL_TERMS, _LOCAL_FN_TERMS, LOCAL_MASK_NAMES = _load_local_terms()
SENSITIVE_PATTERNS += _local_patterns(_LOCAL_TERMS, "local")


# 文件名级敏感规则 —— 内容脱干净了但文件名带机密词一样会泄露 (附件名会进对方的
# 下载记录 / 被搜索引擎索引 / 随 URL 分享转发)。出站附件 send_file 前须过 check_filename。
#
# 同 SENSITIVE_PATTERNS: 具体词不写死在代码里, 走 config/redact_local.yaml 的
# blocked_filename_terms。
FILENAME_SENSITIVE_PATTERNS: list[tuple[str, str, str]] = list(
    _local_patterns(_LOCAL_FN_TERMS, "fn_local"))


# 例外白名单: 这些字符串即使匹配上面的正则, 也允许通过
# 用于 V0 期 documentation / 范例文本
WHITELIST: list[str] = [
    "C-2026-0000",                    # 占位 case id
    "C-2026-NNNN",                    # template 占位
    "FC-2026-0000",
    "FC-YYYY-NNNN",
    "https://example.com",
    # 在 documentation 中明确标注的脱敏示例
    "[REDACTED]",
    "<COMPANY>",
    "<PERSON>",
    "<TOPIC>",
]


# 路径白名单: 框架自身的文件 (README/config/docs/panels/schemas/tests/scripts) 允许
# 合法引用方法论术语与占位 case_id, 不含真实数据。仅对 --staged 模式生效;
# 显式 scan_path("README.md") 仍会扫描。
#
# 本仓是 private, 运行产物 (cases/ reports/ writing/) 含成员周报原文与评分, 已在
# .gitignore 中排除 —— 它们不会被 staged, 故不需要也**不应**进本白名单。
#
# 维护规则: 新增路径需 owner review。
PATH_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "CLAUDE.md",
    "README.md",
    "Makefile",
    "requirements.txt",
    ".gitignore",
    ".env.example",
    "config/",
    "docs/",
    "ops/",
    "panels/",
    "scenes/",
    "schemas/",
    "scripts/",
    "tests/",
    ".github/",
)


def _is_path_allowlisted(path_str: str) -> bool:
    """staged scan 时跳过框架文件 (允许它们合法引用方法论术语)。

    1. 框架目录前缀 (PATH_ALLOWLIST_PREFIXES)
    2. README.md / .gitkeep — 任何目录下的结构性元数据
       (即使在 raw/ backups/ 等数据目录, 也只是描述目录用途, 不含真实数据)
    """
    if path_str.startswith(PATH_ALLOWLIST_PREFIXES):
        return True
    name = path_str.rsplit("/", 1)[-1]
    if name in ("README.md", ".gitkeep"):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────

@dataclass
class Hit:
    pattern_id: str
    description: str
    matched_text: str
    line_no: int
    file_path: str
    snippet: str            # 命中上下文 (前后 30 字符)


def check_text(text: str, path: str = "<stdin>") -> tuple[bool, list[Hit]]:
    """公开 API · 扫描一段文本, 返回 (blocked, hits)。

    blocked = True 当且仅当 hits 非空。供测试 / 上游脚本 / Hermes 出站闸调用。
    path 仅用于报告生成, 不走 PATH_ALLOWLIST_PREFIXES (allowlist 仅在 scan_staged 应用)。
    """
    hits = _scan_text(text, path)
    return len(hits) > 0, hits


def check_filename(filename: str) -> tuple[bool, list[Hit]]:
    """公开 API · 扫描**文件名** (basename), 返回 (blocked, hits)。

    出站附件 (feishu send_file 等) 在发文件前须过此闸: 2026-06-14 事件证明
    内容脱敏但文件名含敏感词仍会经 URL / 搜索引擎外泄。只匹配 basename。
    """
    name = Path(filename).name
    hits: list[Hit] = []
    for pattern_id, regex, desc in FILENAME_SENSITIVE_PATTERNS:
        for m in re.finditer(regex, name):
            hits.append(Hit(
                pattern_id=pattern_id,
                description=desc,
                matched_text=m.group(0),
                line_no=0,                 # 0 = filename-level 哨兵 (同 check_public_safe)
                file_path=name,
                snippet=name,
            ))
    return len(hits) > 0, hits


def _scan_text(text: str, file_path: str = "<stdin>") -> list[Hit]:
    """扫描一段文本, 返回所有命中。"""
    hits: list[Hit] = []
    lines = text.splitlines()
    for line_idx, line in enumerate(lines, start=1):
        for pattern_id, regex, desc in SENSITIVE_PATTERNS:
            for m in re.finditer(regex, line):
                matched = m.group(0)

                # 检查白名单
                if any(w in line for w in WHITELIST):
                    if any(w in matched or matched in w for w in WHITELIST):
                        continue

                start = max(0, m.start() - 30)
                end = min(len(line), m.end() + 30)
                snippet = line[start:end].replace("\n", "\\n")

                hits.append(Hit(
                    pattern_id=pattern_id,
                    description=desc,
                    matched_text=matched,
                    line_no=line_idx,
                    file_path=file_path,
                    snippet=snippet,
                ))
    return hits


def scan_path(p: Path) -> list[Hit]:
    """扫描一个文件 (md/txt/json) 或目录。

    PATH_ALLOWLIST_PREFIXES 在此处也尊重 (v0.3.0 起 · ADR-003 §5.2):
    框架文件 / demo fixture / anchors sub-vault 允许合法引用方法论术语 (例:
    'C-9999-0001' demo case_id)。

    历史: 仅 scan_staged 走 allowlist; v0.3.0 起 scan_path 同步, 让 D3
    GitHub Actions workflow 直接调用 scan_path 也能正确处理 demo case。
    """
    if p.is_dir():
        hits: list[Hit] = []
        for f in p.rglob("*"):
            # 跳过 node_modules / build artifact (第三方依赖含合法人名/百分比)
            if any(part in {"node_modules", ".vitepress"} for part in f.parts):
                continue
            if f.is_file() and f.suffix in {".md", ".txt", ".json", ".yaml", ".yml"}:
                hits.extend(scan_path(f))
        return hits

    if not p.is_file():
        # fail-close: 路径不是可读普通文件 (不存在 / 坏符号链接 / git-quotepath 转义未解开的
        # CJK 文件名等) 不能静默判"干净" — 与下方读失败 fail-close 一致 (修早先 return [] 的 fail-open)
        return [Hit(
            pattern_id="_missing_file",
            description=f"路径不是可读文件, 无法扫描 (fail-close): {p}",
            matched_text="",
            line_no=0,
            file_path=str(p),
            snippet="",
        )]

    # allowlist: 计算相对 vault root 的路径再判断
    vault_root = Path(__file__).parent.parent.resolve()
    try:
        rel = str(p.resolve().relative_to(vault_root))
    except ValueError:
        rel = str(p)
    if _is_path_allowlisted(rel):
        return []

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        # fail-close: 读取失败按阻断处理
        return [Hit(
            pattern_id="_read_error",
            description=f"无法读取文件: {e}",
            matched_text="",
            line_no=0,
            file_path=str(p),
            snippet="",
        )]

    return _scan_text(text, str(p))


def scan_staged() -> list[Hit]:
    """扫描 git staged 文件。"""
    try:
        # core.quotepath=false: 让 CJK 文件名以原始 UTF-8 输出, 不做八进制转义+引号包裹
        # (否则 "\346\226\207.md" 这种被转义的路径 Path() 后不是真实文件 → scan_path 漏扫)
        out = subprocess.check_output(
            ["git", "-c", "core.quotepath=false",
             "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"git 错误: {e}", file=sys.stderr)
        sys.exit(3)

    hits: list[Hit] = []
    for line in out.strip().splitlines():
        if not line:
            continue
        # 跳过我们不关心的 binary/lock 文件
        if any(line.endswith(s) for s in (".png", ".jpg", ".pdf", ".lock", ".bin")):
            continue
        # 路径白名单: 框架文件 (CLAUDE.md/docs/skills/...) 合法引用方法论术语
        if _is_path_allowlisted(line):
            continue
        hits.extend(scan_path(Path(line)))
    return hits


def render_report(hits: list[Hit], output_format: str = "text") -> str:
    """渲染检测报告。"""
    if not hits:
        return "✅ redact_check PASSED — 未发现敏感内容\n"

    if output_format == "json":
        return json.dumps([h.__dict__ for h in hits], ensure_ascii=False, indent=2)

    lines = [
        f"🛑 redact_check BLOCKED — 发现 {len(hits)} 处敏感内容",
        "=" * 70,
    ]
    for h in hits:
        lines.append(f"  [{h.pattern_id}] {h.description}")
        lines.append(f"    file: {h.file_path}:{h.line_no}")
        lines.append(f"    matched: {h.matched_text!r}")
        lines.append(f"    context: ...{h.snippet}...")
        lines.append("")
    lines.append("=" * 70)
    lines.append("修复建议:")
    lines.append("  1. 用通用代号替换 (例: 某 B2B 客户 → 某 B2B 安全公司)")
    lines.append("  2. 数字降量级 (14.3% → 约 10-15%)")
    lines.append("  3. 飞书 link 去掉 lark:// 前缀, 改为 [内部文档]")
    lines.append("  4. 如确认无敏感性, 在 WHITELIST 中加入显式例外 (需 PR review)")
    return "\n".join(lines)


def log_blocked(hits: list[Hit]) -> None:
    """阻断时写到 failure_cards/blocked-publish.log。"""
    log_path = Path("failure_cards") / "blocked-publish.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"\n## [{datetime.now().isoformat()}] blocked\n")
        for h in hits:
            f.write(f"- [{h.pattern_id}] {h.file_path}:{h.line_no} — {h.matched_text!r}\n")


def _test_fixtures() -> list[tuple[str, bool]]:
    """内置 fixture, 用于 --dry-run --test-fixtures。

    只覆盖**结构性**规则 —— 具体人名/客户名走 config/redact_local.yaml, 每个部署不同,
    不适合写死成 fixture。加了本地词后想验证: 直接
        echo "<你的敏感词>" | python3 scripts/redact_check.py -
    """
    return [
        # (input_text, should_be_blocked)
        ("访谈链接: lark://docx/abc123", True),
        ("详见 feishu://docs/xyz", True),
        ("Q3 营收增长 14.3%", True),                 # 精确百分比
        ("Q3 营收 87654.32 亿元", True),             # 精确财务数字
        ("成本 1,234,567.89 元", True),              # 千分位形态
        ("case_id C-2026-0001", True),
        ("复盘 FC-2026-0001 的分类", True),

        # 应该通过的:
        ("[REDACTED] 的判断框架", False),
        ("营收增长约 14%", False),                   # 不精确, 不命中
        ("营收增长 10%", False),                     # 整数百分比不命中
        ("C-YYYY-NNNN 是模板占位符", False),         # 白名单
        ("本周产出 3 个模块, 复用率提升明显", False),  # 典型周报正文, 不该误伤
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="出站脱敏闸 (fail-close)")
    ap.add_argument("path", nargs="?", help="要检查的文件/目录, 或 '-' 表示 stdin")
    ap.add_argument("--staged", action="store_true", help="检查 git staged 文件 (pre-commit hook 用)")
    ap.add_argument("--stdin", action="store_true", help="从 stdin 读 (Hermes 出站闸用, T26)")
    ap.add_argument("--dry-run", action="store_true", help="只检查, 不阻断 commit, 不写 log")
    ap.add_argument("--test-fixtures", action="store_true", help="跑内置 fixture")
    ap.add_argument("--fail-close", action="store_true",
                    help="显式声明 fail-close 语义: 命中即 exit 1 + 写 log. 默认行为, flag 用于 Hermes 场景的显式合约")
    ap.add_argument("--json", action="store_true", help="JSON 输出 (Hermes 解析 block 原因用)")
    args = ap.parse_args()

    if args.test_fixtures:
        passed = failed = 0
        for text, expected_block in _test_fixtures():
            hits = _scan_text(text)
            actual_block = len(hits) > 0
            ok = (actual_block == expected_block)
            mark = "✅" if ok else "❌"
            print(f"  {mark} '{text}' → blocked={actual_block} (expected {expected_block})")
            if ok:
                passed += 1
            else:
                failed += 1
        print(f"\n{passed} passed, {failed} failed")
        return 0 if failed == 0 else 1

    # 收集 hits
    if args.staged:
        hits = scan_staged()
    elif args.stdin or args.path == "-":
        text = sys.stdin.read()
        hits = _scan_text(text, file_path="<stdin>")
    elif args.path:
        hits = scan_path(Path(args.path))
    else:
        ap.print_help()
        return 2

    # 输出
    print(render_report(hits, output_format="json" if args.json else "text"))

    if hits and not args.dry_run:
        log_blocked(hits)
        # fail-close 显式声明: 命中即阻断, 调用方 (Hermes / pre-commit) 自己决定降级策略
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(3)
    except Exception as e:
        # fail-close: 任何未捕获异常都按阻断处理
        print(f"🛑 redact_check fail-close 触发: {e}", file=sys.stderr)
        sys.exit(3)
