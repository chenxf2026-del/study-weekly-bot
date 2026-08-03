"""boss_core — boss 判断引擎纯逻辑库 (boss-as-a-Service M0 抽库)。

从 run_pipeline_local 逐步搬入的**纯逻辑** (prompt 装配 / 打分聚合 / 无状态工具),
供 run_pipeline_local (re-export shim) 与未来 boss-service (MCP/HTTP) 共用同一引擎核。

纪律 (§6 R-b): boss_core **只依赖 stdlib + 已独立模块**, 绝不 import run_pipeline_local
(方向单一 rpl → boss_core, 防循环 import)。
"""

from __future__ import annotations

from boss_core.errors import PipelineError

__all__ = ["PipelineError"]
