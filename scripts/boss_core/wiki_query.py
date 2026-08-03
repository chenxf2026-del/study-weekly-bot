"""boss_core.wiki_query — _wiki/ entity 查询 + 议题关键词抽取 (M0.1d, 从 run_pipeline_local 纯搬移)。

Phase 1 Context 阶段的两个无状态工具:
- `_extract_keywords`: 从议题描述抽 grep 关键字 (纯 re + 本地 STOPWORDS)。
- `query_wiki_entities`: 扫 _wiki/{people,entities,concepts}/*.md, 匹配 keyword/topic → WikiEntityHit。

只依赖 stdlib + 已独立模块 (_export_helpers, lazy yaml) + boss_core.logger; 绝不 import
run_pipeline_local (方向单一, §6 R-b)。run_pipeline_local 顶部 re-export
`_extract_keywords` / `query_wiki_entities` / `WikiEntityHit` / `WIKI_DIR`, 生产 import 方
(persona_chat) 与测试零改动。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from boss_core.logger import Logger

# 仓库根 (boss_core → scripts → repo root); 与 run_pipeline_local.VAULT_ROOT 同解析结果。
_VAULT_ROOT = Path(__file__).resolve().parents[2]
WIKI_DIR = _VAULT_ROOT / "_wiki"


@dataclass
class WikiEntityHit:
    """命中的 _wiki/<type>/<slug>.md (P1 #3 修法, build_wiki.py 编译产出)"""
    slug: str
    canonical: str
    type: str                         # 'people' / 'entities' / 'concepts'
    matched_keyword: str
    role: Optional[str]               # people only
    entity_type: Optional[str]        # entities/concepts: type 字段
    sensitivity: str
    mention_count: int
    profile: str                      # profile_seed (从 frontmatter 后段抽)
    related_judgements_count: int = 0


def _extract_keywords(topic: str) -> list[str]:
    """
    从议题描述里抽 grep 关键字。启发式:
    - 切中文连续段 (≥ 2 字) — 简单按非汉字字符切
    - 切英文 token (≥ 3 字符)
    - 去除停用词
    """
    STOPWORDS = {
        "的", "是", "在", "和", "与", "及", "或", "了", "我", "你", "他",
        "这个", "那个", "什么", "怎么", "为什么", "如果", "因为", "所以",
        "the", "and", "for", "with", "that", "this", "are", "was", "will",
    }
    # 中文段
    cn_parts = re.findall(r"[一-鿿]{2,}", topic)
    # 英文 token
    en_parts = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", topic)
    raw = cn_parts + en_parts
    seen = set()
    result = []
    for w in raw:
        if w in STOPWORDS or w.lower() in STOPWORDS:
            continue
        if w in seen:
            continue
        seen.add(w)
        result.append(w)
    return result[:10]


def query_wiki_entities(
    keywords: list[str],
    max_total: int = 12,
    log: Optional[Logger] = None,
    topic_raw: Optional[str] = None,
) -> list[WikiEntityHit]:
    """
    P1 #3 修 (dev-plan v2.14): 优先用 _wiki/<type>/<slug>.md 替代 grep fallback.

    扫 _wiki/people|entities|concepts/*.md, 对每个 entity 检查 canonical / aliases
    是否匹配 keywords 或 topic 整段 (后者补救 _extract_keywords 把 "AI 优先" 拆开的边界).

    若 _wiki/ 不存在或全空 → 返回空列表 (上层 fallback 到 grep).
    """
    import yaml
    if log is None:
        log = Logger()

    if not WIKI_DIR.exists():
        return []

    hits: list[WikiEntityHit] = []
    for type_dir in ("people", "entities", "concepts"):
        d = WIKI_DIR / type_dir
        if not d.exists():
            continue
        for f in d.glob("*.md"):
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            # v0.11 C3: 单一源 strict 解析 (顺修 latent: 空 frontmatter 时
            # 原 safe_load 返 None → 下行 .get 会 AttributeError; strict 返 None 直接跳)
            import _export_helpers
            fm = _export_helpers.parse_frontmatter_strict(text)
            if fm is None or not isinstance(fm, dict):
                continue
            canonical = fm.get("canonical", f.stem)
            aliases = fm.get("aliases", []) or []
            slug = fm.get("slug", f.stem)
            sensitivity = fm.get("sensitivity", "confidential")
            mention_count = fm.get("mention_count", 0)

            # 找出第一个匹配 keyword (或 topic_raw 整段, 补救分词边界)
            matched_kw: Optional[str] = None
            search_targets = list(keywords) + ([topic_raw] if topic_raw else [])
            for kw in search_targets:
                if not kw or not kw.strip():
                    continue
                for term in [canonical] + list(aliases):
                    if not term or len(term.strip()) < 2:
                        continue
                    # 双向子串: keyword 含 alias, 或 alias 含 keyword
                    if kw in term or term in kw:
                        matched_kw = term
                        break
                if matched_kw:
                    break
            if not matched_kw:
                continue

            # 抽 profile (取 ## Profile 后段, 限 3 行)
            profile_m = re.search(r"##\s*Profile\s*\n+(.+?)\n\n", text, re.DOTALL)
            profile = profile_m.group(1).strip() if profile_m else ""
            profile = "\n".join(profile.splitlines()[:3])

            # role / entity_type from body
            role_m = re.search(r"\*\*role\*\*:\s*(.+)", text)
            etype_m = re.search(r"\*\*type\*\*:\s*(.+)", text)
            role = role_m.group(1).strip() if role_m else None
            entity_type = etype_m.group(1).strip() if etype_m else None

            # Related Judgements 行数
            rj_block_m = re.search(r"##\s*Related Judgements.*?(?=##|\Z)", text, re.DOTALL)
            rj_count = len(re.findall(r"^- \d{4}-\d{2}-\d{2}", rj_block_m.group(0), re.MULTILINE)) if rj_block_m else 0

            hits.append(WikiEntityHit(
                slug=slug,
                canonical=canonical,
                type=type_dir,
                matched_keyword=matched_kw,
                role=role,
                entity_type=entity_type,
                sensitivity=sensitivity,
                mention_count=mention_count,
                profile=profile,
                related_judgements_count=rj_count,
            ))

    # 排序: mention_count 降序
    hits.sort(key=lambda h: -h.mention_count)
    log.dbg(f"query_wiki_entities: {len(keywords)} keywords → {len(hits)} entity hits (truncated to {max_total})")
    return hits[:max_total]
