#!/usr/bin/env python3
"""model_defaults.py — 默认模型兜底的单一真相源 (P2)。

散落各调用点的硬编码模型名 (claude-sonnet-4-6 / claude-opus-4-8 / claude-haiku-4-5-…)
收敛到这里一处。升级模型代 (如 opus-4-7 → opus-4-8) 只改本文件 (或在 .env 设
ANTHROPIC_MODEL_*), 不用全库捞散落字面量。

三个 tier (与 .env.example 的 ANTHROPIC_MODEL_* 对齐, 亦是运维不改代码就能换模型的开关):

  fast  = 起草 / 调研 / 合成 (Phase 1-3)                    ← ANTHROPIC_MODEL_SONNET
  deep  = 评委打分 / 合议 / 分身 / 会议总结 (Phase 4-5 等)    ← ANTHROPIC_MODEL_OPUS
  haiku = 便宜语义比对 / 过滤兜底 (attribution)                ← ANTHROPIC_MODEL_HAIKU

解析优先级 (每个 tier): 对应 ANTHROPIC_MODEL_* 环境变量 → 下面的硬编码兜底。

⚠ 这些是**兜底**, 不是主来源。各调用点仍先读自己的主来源:
  - 主评审 / 会议 / 分身: BOSS_LLM_MODEL_FAST/DEEP (由 llm_switch use <profile> 或
    scene.llm 覆盖写入); 切到非 anthropic 端点 (如 glm) 时这些已被写成 glm-* 模型名,
    本模块兜底不参与, 故这里恒为 anthropic 系名不会污染别家网关。
  - attribution: GLM 优先 (GLM_MODEL), 都不可用才落 anthropic haiku。
"""
from __future__ import annotations

import os

# 硬编码最终兜底 (对应 ANTHROPIC_MODEL_* 都没配时用)。升级模型代只改这三行。
_FALLBACK_FAST = "claude-sonnet-4-6"
_FALLBACK_DEEP = "claude-opus-4-8"    # T2: deep 档默认升 Opus 4.8 (与 aigw 网关对齐)
_FALLBACK_HAIKU = "claude-haiku-4-5-20251001"


def _pick(env_name: str, fallback: str) -> str:
    """读 env_name (剥空白/反引号) → 缺则 fallback。反引号剥除同 llm_switch (避免网关 401)。"""
    return (os.environ.get(env_name) or "").strip().strip("`").strip() or fallback


def model_fast() -> str:
    """Phase 1-3 (起草/调研/合成) 的默认模型兜底。"""
    return _pick("ANTHROPIC_MODEL_SONNET", _FALLBACK_FAST)


def model_deep() -> str:
    """Phase 4-5 / 分身 / 会议总结 的默认模型兜底。"""
    return _pick("ANTHROPIC_MODEL_OPUS", _FALLBACK_DEEP)


def model_haiku() -> str:
    """便宜语义比对 / 过滤兜底 的默认模型。"""
    return _pick("ANTHROPIC_MODEL_HAIKU", _FALLBACK_HAIKU)
