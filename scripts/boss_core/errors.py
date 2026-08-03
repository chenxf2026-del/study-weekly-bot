"""boss_core.errors — 引擎共享异常基座 (M0.1a)。

从 `run_pipeline_local` 下沉 (原 run_pipeline_local:4468), 让 boss_core 各模块与
run_pipeline_local 共用**同一个** PipelineError 类, 避免 boss_core 反向 import
run_pipeline_local 造成循环 (§6 R-b)。run_pipeline_local 顶部 re-export 保签名不变。
"""

from __future__ import annotations


class PipelineError(RuntimeError):
    pass
