"""boss_core.anchor_research — anchor research 状态 → confidence 上限 (M2.0b, 从 run_pipeline_local 纯搬移)。

v0.6 R1.3 · anchor research 聚合状态 (skill_lint.anchor_research_state, 独立模块) 决定
anchor 评委 confidence 上限。M2 boss_prepare 装配 phase-4 prompt 时作 cap_fn 注入;
CLI 经 rpl re-export 照旧 (rpl 内部调用点运行时查 rpl globals, 测试对
rpl._anchor_confidence_cap 的 monkeypatch 语义不变)。

- placeholder: 占位模板 (§6.5 anti-fabrication 现行 0.4)
- agent_derived: 已合成未经锚点签收 (verified_by_anchor: false)
- verified: 锚点逐文件签收, 解除上限
"""

from __future__ import annotations

ANCHOR_RESEARCH_CAPS: dict[str, float | None] = {
    "placeholder": 0.4, "agent_derived": 0.6, "verified": None,
}


def _anchor_confidence_cap(anchor_slug: str) -> tuple[float | None, str]:
    """读 anchor research 聚合状态 (skill_lint.anchor_research_state), 返回 (cap, state)。
    任何异常按最保守 placeholder 处理 (fail-safe)。"""
    try:
        import skill_lint
        state, _ = skill_lint.anchor_research_state(anchor_slug)
    except Exception:
        state = "placeholder"
    return ANCHOR_RESEARCH_CAPS.get(state, 0.4), state
