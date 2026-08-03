"""boss_core.kb.types — KB 抽象的数据类型 (M1.1 · 纯类型, 无行为)。

对齐 M1 方案 §3.1 (docs/internal/boss-as-a-service-m1-kbprovider-plan.md):
- Tier: 四档敏感度 (CLAUDE.md §9.1), _TIER_ORDER 给 K-a tier 过滤骨架排序。
- KBHit: query 返回项的**结构契约** (Protocol)。HostedKB 直接返回现有
  WikiEntityHit (已带 sensitivity 字段) 保 CLI 逐字不变; M5 的 LocalKB 返回同构对象。
- PanelSpec: resolve_panel 返回 = panel_loader.resolve_panel 产出的 dict
  (typed alias, 不新造结构, 消费方零特判)。
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Protocol, runtime_checkable

Tier = Literal["public", "internal", "confidential", "tian_only"]

# K-a: tier 全序 public < internal < confidential < tian_only。
# query(tier=X) 只放行 _TIER_ORDER[hit.sensitivity] <= _TIER_ORDER[X];
# 未知 sensitivity 按最高档 confidential 兜底处理 (fail-close 方向), 见 hosted.py。
_TIER_ORDER: dict[str, int] = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "tian_only": 3,
}


@runtime_checkable
class KBHit(Protocol):
    """query 命中项的结构契约 — 与 boss_core.wiki_query.WikiEntityHit 同构。"""
    slug: str
    canonical: str
    type: str                         # 'people' / 'entities' / 'concepts'
    matched_keyword: str
    role: Optional[str]
    entity_type: Optional[str]
    sensitivity: str                  # tier 过滤 (K-a) 读这个字段
    mention_count: int
    profile: str
    related_judgements_count: int


# panel_loader.resolve_panel 的产出 dict (name/judges/scoring_lenses/...)。
# M1 不 formalize 成 dataclass — 现有消费方 (rpl / report_builder) 都吃 dict。
PanelSpec = dict[str, Any]
