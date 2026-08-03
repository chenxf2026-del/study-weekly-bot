#!/usr/bin/env python3
"""
desensitize.py — 报告交付前的"来源脱敏"transform (非阻断闸)。

问题 (C-2026-0074/0075 实战): 评委在 review 的 adversarial_view / 金句里, 按 anti-fabrication
纪律引用了**内部原始来源** —— vault 路径 (anchors/<slug>/raw/...)、原始素材文件名
(ai-coding-tixiao.md)、synthesis/Phase 内部引用 (synthesis M3 / Phase1 Context xxx.md)、
内部会议纪要名 (智能纪要：...)、真名。这些不该出现在交付的报告 (report.md / 富 HTML /
PDF) 里 —— 它们是内部审计线索, 只应留在 reviews/*.md 原文与 case.json, 不进 deliverable。

与 `redact_check.py` 的区别:
- redact_check 是 **fail-close 阻断闸** (出站到公网 / EvoMap 前命中即拦), 不改写内容;
- 本模块是 **transform** —— 在报告落盘 / 渲染成交付件前, 把来源引用**改写成通用占位**,
  报告仍可读、仍看得出"有佐证", 只是不暴露具体内部路径 / 真名。

纪律: 只脱敏"来源引用"这一类, 不动评委判断正文; 保留 `data_source:` (attribution checkpoint
的合法字段, 值本就是通称如"月度经营分析会纪要")。原始 reviews/*.md 不改, 审计可回溯。
"""

from __future__ import annotations

import re

# 人名 → 占位 (交付件不写真名)。
# ★ 本仓改动: 上游在此写死了**它自己的**锚点真名; 剥离时移除 —— 那个名字在本部署里
#   什么也保护不了, 只会把它自己泄露给本仓每一位读者。本部署要屏蔽的名字填
#   config/redact_local.yaml 的 mask_names ({真名: 占位}); 不配则本步为 no-op。
try:
    from redact_check import LOCAL_MASK_NAMES as _MASK_NAMES
except Exception:  # noqa: BLE001 — 独立调用 (python3 desensitize.py) 时也要能跑
    _MASK_NAMES = {}

# (source: ...) 括号引用从句 (中/英文括号; 内部不含嵌套括号)。
# 负向后顾 (?<![A-Za-z_]) 保护 data_source / xxx_source 不被误伤。
_SOURCE_PAREN_RE = re.compile(
    r"[（(][^（()）]*?(?<![A-Za-z_])source\s*[:：][^（()）]*?[)）]", re.I)

# 行内 source: ... 到句末 (。/换行/右括号前); 前置标点可选一并吃掉。同样护 data_source。
_SOURCE_INLINE_RE = re.compile(
    r"[,;，；、]?\s*(?<![A-Za-z_])source\s*[:：]\s*[^。\n)）]*", re.I)

# 内部会议纪要名 (智能纪要：xxx)
_MEETING_RE = re.compile(r"[（(]?\s*智能纪要\s*[:：][^。\n)）]*[)）]?")

# vault 内部路径 (anchors/... _wiki/... raw/... 等) → 通用
_VAULT_PATH_RE = re.compile(
    r"(?:anchors|_wiki|raw|backups|cases|reports|writing)/[^\s，,。；;：:)）】\]]+")

# 残留清理
_EMPTY_PAREN_RE = re.compile(r"[（(]\s*[)）]")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([。，；;])")


def desensitize_sources(text: str) -> str:
    """把交付报告文本里的内部来源引用改写成通用占位, 屏蔽锚点真名。幂等、纯字符串、无 IO。

    - `(source: 内部路径/文件/synthesis…)`  → `（内部佐证）`
    - 行内 `source: 内部引用`                → 删除 (到句末)
    - `智能纪要：<会议名>`                    → `内部会议纪要`
    - vault 路径 (anchors/… _wiki/… raw/…)   → `内部资料`
    - config/redact_local.yaml 的 mask_names   → 对应占位 (不配则跳过)
    - **保留** `data_source:` 等合法字段
    """
    if not text:
        return text
    text = _SOURCE_PAREN_RE.sub("（内部佐证）", text)
    text = _MEETING_RE.sub("内部会议纪要", text)
    text = _SOURCE_INLINE_RE.sub("", text)
    text = _VAULT_PATH_RE.sub("内部资料", text)
    for _real, _placeholder in _MASK_NAMES.items():
        text = text.replace(_real, _placeholder)
    text = _EMPTY_PAREN_RE.sub("", text)
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    return text


if __name__ == "__main__":
    import sys
    data = sys.stdin.read() if len(sys.argv) < 2 else sys.argv[1]
    sys.stdout.write(desensitize_sources(data))
