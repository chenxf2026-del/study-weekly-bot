"""boss_core.loop.suggestions — 从评委 review 文本抽结构化建议 (M0 · 纯函数)。

review 末段有 `## 修订建议 (REVIEW)` (或人格评委的 `## 行动建议/改进建议`), 内含
编号/圆点条目。本模块把它们抽成带 ID 的结构化条目, 供 Phase 5 落
reports/<brand>/suggestions.json — 回路 (决策/行动) 从此有锚。

标题匹配语义与 run_pipeline_local._compile_revision_suggestions 保持一致
(宽容 # 数量/粗体/前缀; revision 优先, action 回退); 正则在此**独立镜像**
而非 import — boss_core 禁反向依赖 CLI (总纲 §6 R-b)。
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Iterable, Optional

_REVISION_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}(?:#{1,4}[ \t]*|\*\*[ \t]*)[^\n#*]{0,10}修订建议")
_ACTION_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}(?:#{1,4}[ \t]*|\*\*[ \t]*)[^\n#*]{0,10}(?:行动建议|改进建议)")
_NEXT_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,4}[ \t]")
# 条目起始: "1. " / "1、" / "1) " / "- " / "* "
_ITEM_START_RE = re.compile(r"^[ \t]*(?:\d+[.、)][ \t]+|[-*][ \t]+)(.+)$")


def _section_after(text: str, m: "re.Match[str]") -> str:
    """标题行之后 → 下一个 markdown 标题 (或文末) 的正文。"""
    nl = text.find("\n", m.start())
    rest = text[nl + 1:] if nl >= 0 else ""
    nxt = _NEXT_HEADING_RE.search(rest)
    return (rest[: nxt.start()] if nxt else rest).strip()


def parse_suggestion_items(body: str) -> list[str]:
    """建议段正文 → 条目文本列表。

    条目 = 编号/圆点行 + 其后的续行 (到空行/标题/引注止)。围栏行 (```) 直接剥掉 —
    评委偶把整段包进 ```markdown, 建议文本本身不需要代码块。
    """
    items: list[str] = []
    cur: Optional[list[str]] = None
    for ln in body.splitlines():
        if ln.strip().startswith("```"):
            continue
        m = _ITEM_START_RE.match(ln)
        if m:
            if cur:
                items.append(" ".join(cur).strip())
            cur = [m.group(1).strip()]
            continue
        if cur is not None:
            s = ln.strip()
            if not s or s.startswith(("#", ">")):
                items.append(" ".join(cur).strip())
                cur = None
            else:
                cur.append(s)
    if cur:
        items.append(" ".join(cur).strip())
    return [i for i in items if i]


def extract_suggestions(review_texts: dict[str, str], *, brand: str, scene: str,
                        version: int, anchor_judges: Iterable[str] = ()) -> dict:
    """{judge: review 全文} → suggestions.json payload。

    - 每条建议 ID = <brand>-s<n> (全局稳定序: anchor 评委在前, 其余按传入序)。
    - priority: 前 3 条 True (PRD D5: 只追 3 条优先建议; anchor 在前 → anchor 的建议优先)。
    - 无建议段的评委跳过; 全空 → suggestions 为空列表 (合法, 上游 fail-open 记 warning)。
    """
    anchors = [j for j in review_texts if j in set(anchor_judges)]
    others = [j for j in review_texts if j not in set(anchor_judges)]
    entries: list[dict] = []
    for judge in anchors + others:
        text = review_texts[judge] or ""
        m = _REVISION_HEADING_RE.search(text) or _ACTION_HEADING_RE.search(text)
        if not m:
            continue
        for item in parse_suggestion_items(_section_after(text, m)):
            entries.append({"judge": judge, "text": item})
    for i, e in enumerate(entries, 1):
        e["id"] = f"{brand}-s{i}"
        e["priority"] = i <= 3
    return {
        "brand": brand,
        "scene": scene,
        "version": version,
        "created_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "suggestions": entries,
    }
