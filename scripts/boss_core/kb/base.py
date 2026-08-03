"""boss_core.kb.base — KBProvider 统一接口 (M1.1 · Protocol, 无实现)。

判断引擎访问知识库的**唯一入口面** (M1 方案 §3.2, 上游 PRD §5.4):
- M1.2 `HostedKB`: 包住现网行为 (wiki query + grep 兜底 + doctrine + panel), CLI 走它零变化。
- M2 `boss_prepare` 服务化: 以本 Protocol 为唯一 KB 依赖。
- M5 `LocalKB` (BYO bundle): 同 Protocol 第二实现 + 租户隔离 (SR3)。

契约面由 tests/unit/test_kb_provider_contract.py 锁死 (M1.0), 删改方法即红。
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from boss_core.kb.types import KBHit, PanelSpec, Tier


@runtime_checkable
class KBProvider(Protocol):
    """KB 访问三能力: 检索 / 评委 doctrine / panel 编组。

    K-a 纪律: `tier` 默认 "tian_only" (最高可见档) — CLI 不传即满权限零裁剪;
    服务侧 (M2/M3) 按调用方角色显式降档。
    """

    def query(self, q: str, *, tier: Tier = "tian_only",
              topic_raw: Optional[str] = None) -> list[KBHit]:
        """按议题文本检索 entities/people/concepts, 命中按 tier 过滤后返回。"""
        ...

    def load_doctrine(self, judge: str, *, scene: Optional[str] = None) -> str:
        """加载评委 SKILL.md doctrine 文本 (scene panel 显式 skill_path 优先)。"""
        ...

    def resolve_panel(self, scene: str) -> PanelSpec:
        """解析 panel 编组 (extends / judges / scoring_lenses 已展开)。"""
        ...
