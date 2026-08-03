"""boss_core.kb — 判断引擎的 KB 抽象 (boss-as-a-Service M1)。

统一「sage-wiki query + Read 文件 + panel 解析」为 KBProvider 单一接口。
纪律同 boss_core (§6 R-b / M1 K-c): 只依赖 stdlib + 已独立模块
(panel_loader / scene_loader / redact_check), 绝不 import run_pipeline_local;
rpl-resident 依赖走构造器 DI 注入。
"""

from __future__ import annotations

from boss_core.kb.base import KBProvider
from boss_core.kb.hosted import HostedKB
from boss_core.kb.types import KBHit, PanelSpec, Tier, _TIER_ORDER

__all__ = ["KBProvider", "HostedKB", "KBHit", "PanelSpec", "Tier", "_TIER_ORDER"]
