"""boss_core.logger — 引擎共享 Logger (M0.1d, 从 run_pipeline_local 纯搬移)。

极简 stdout/stderr Logger (原 run_pipeline_local:190 下沉), 无状态、只依赖 stdlib。
boss_core 各纯逻辑模块 (wiki_query / 未来 prompts/scoring) 与 run_pipeline_local
共用同一个 Logger 类; run_pipeline_local 顶部 re-export 保 `rp.Logger` 签名不变
(约 20 个测试用 rp.Logger() 实例化)。
"""

from __future__ import annotations

import sys


class Logger:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def info(self, msg: str) -> None:
        print(f"[INFO] {msg}", flush=True)

    def warn(self, msg: str) -> None:
        print(f"[WARN] {msg}", file=sys.stderr, flush=True)

    def err(self, msg: str) -> None:
        print(f"[ERR ] {msg}", file=sys.stderr, flush=True)

    def step(self, phase: str, msg: str) -> None:
        print(f"[{phase}] {msg}", flush=True)

    def dbg(self, msg: str) -> None:
        if self.verbose:
            print(f"[DBG ] {msg}", flush=True)
