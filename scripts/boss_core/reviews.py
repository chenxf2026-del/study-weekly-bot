"""boss_core.reviews — 评委 review frontmatter 解析 (M2.0b, 从 run_pipeline_local 纯搬移)。

Phase 5 聚合与 M2 boss_submit_reviews 契约校验共用的解析器。strict yaml 失败时退
regex fallback (anchor review 常含 unquoted 中文冒号)。只依赖 stdlib + lazy yaml +
lazy _export_helpers (独立模块); 绝不 import run_pipeline_local (§6 R-b)。
run_pipeline_local 顶部 re-export 保签名零改动。
"""

from __future__ import annotations

import re
from typing import Any, Optional


def _parse_review_frontmatter(text: str) -> Optional[dict]:
    """与 smoke_e2e._parse_frontmatter 同构 (避免循环 import).

    Graceful degrade: anchor review (e.g. C-2026-0006 tian.md) 常含 reflective
    text 用 unquoted Chinese 含 ":", yaml strict parse 易失败. 失败时退到
    regex fallback, 只抽 pipeline 实际消费的字段 (scores / confidence /
    adversarial_view), 让 panel_summary / brief 仍能正常工作.

    v0.11 C3: frontmatter 抽取走 _export_helpers 正则单一源; fallback 语义
    (仅 yaml 非法时触发, 缺块直接 None) 保持不变, 故不能用 parse_frontmatter_strict
    (它无法区分"缺块"与"非法").
    """
    import yaml
    from _export_helpers import extract_frontmatter_text
    fm_text = extract_frontmatter_text(text)
    if fm_text is None:
        return None
    try:
        return yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return _parse_review_frontmatter_fallback(fm_text)


def _parse_review_frontmatter_fallback(fm_text: str) -> Optional[dict]:
    """Regex fallback · 抽 scores: 块 + confidence: 标量 + adversarial_view: 块.

    用于 strict yaml parse 失败时. 不还原全部字段, 只够 panel_summary +
    report-brief 用. 失败 (无 scores 块) 时返回 None.
    """
    out: dict[str, Any] = {}

    # scores: 块 — 多行 indented mapping. 找 "scores:" 起 ~ 下一个 unindented 行
    sm_start = re.search(r"^scores:\s*$", fm_text, re.MULTILINE)
    if sm_start:
        block_start = sm_start.end()
        # 下一个非 indented (非空且首字符非空格/tab) 行结束块
        rest = fm_text[block_start:]
        block_lines = []
        for line in rest.split("\n"):
            if not line:
                block_lines.append(line)
                continue
            if line[0] in (" ", "\t"):
                block_lines.append(line)
            else:
                break
        scores: dict[str, float] = {}
        for line in block_lines:
            sm2 = re.match(r"^[ \t]+([\w_]+):\s*(-?\d+(?:\.\d+)?)\s*$", line)
            if sm2:
                try:
                    v = float(sm2.group(2))
                    scores[sm2.group(1)] = int(v) if v.is_integer() else v
                except ValueError:
                    pass
        if scores:
            out["scores"] = scores

    # confidence: 单行标量
    cm = re.search(r"^confidence:\s*(-?\d+(?:\.\d+)?)\s*$", fm_text, re.MULTILINE)
    if cm:
        try:
            out["confidence"] = float(cm.group(1))
        except ValueError:
            pass

    # confidence_meta_layer / confidence_single_point_layer (P2.4 dual scale)
    for key in ("confidence_meta_layer", "confidence_single_point_layer"):
        km = re.search(rf"^{key}:\s*(-?\d+(?:\.\d+)?)\s*$", fm_text, re.MULTILINE)
        if km:
            try:
                out[key] = float(km.group(1))
            except ValueError:
                pass

    # adversarial_view: 块 — 取 if_thesis_wrong 字段 (block scalar 形式)
    avm = re.search(
        r"^adversarial_view:\s*\n((?:[ \t]+.+\n?)+)",
        fm_text, re.MULTILINE,
    )
    if avm:
        av_text = avm.group(1)
        # if_thesis_wrong 字段 (block scalar |- 或 |+ 或单行)
        itw_m = re.search(
            r"^[ \t]+if_thesis_wrong:\s*\|[+-]?\s*\n((?:[ \t]{4,}.+\n?)+)|"
            r"^[ \t]+if_thesis_wrong:\s*(.+)$",
            av_text, re.MULTILINE,
        )
        if itw_m:
            wrong = (itw_m.group(1) or itw_m.group(2) or "").strip()
            if wrong:
                out["adversarial_view"] = {"if_thesis_wrong": wrong}

    return out if out else None
