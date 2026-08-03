"""boss_core.loop.metrics — 指标注册表 → 评审 context 注入块 (M3 · prd-strategy-os §5.4)。

让评委评「目标是否量化可考核」时**有真数可对**: Phase 1 写 context.md 时, 把该场景
挂钩的在册指标 (目标值 / owner / 最新快照) 渲成 markdown 段注入 —— 评委与 Phase 3
合成都能读到 (context 是它们的共同输入)。

敏感度边界: **tian_only 指标不注入** (context.md 落在 cases/, 私仓全员可审计;
tian_only 的可见面是 锚点+主理, 不经此扩散) — 只提示"另有 N 条未注入"。
注册表由主理/owner 人工维护 YAML (D3: 快照人工/半自动, 不做系统集成)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from boss_core.loop.store import read_metrics

# 校验用: metric YAML 的字段契约 (loop_metrics_check.py 与文档共同引用)
METRIC_REQUIRED = ("name", "owner", "target")
METRIC_SENSITIVITIES = ("confidential", "tian_only")


def validate_metric(rec: dict) -> list[str]:
    """单条 metric 校验 → 问题列表 (空 = 合法)。"""
    errs = [f"缺字段 {k}" for k in METRIC_REQUIRED if not rec.get(k)]
    sens = rec.get("sensitivity", "confidential")
    if sens not in METRIC_SENSITIVITIES:
        errs.append(f"sensitivity 非法: {sens} (须 ∈ {METRIC_SENSITIVITIES})")
    scenes = rec.get("scenes")
    if scenes is not None and not (isinstance(scenes, list)
                                   and all(isinstance(s, str) for s in scenes)):
        errs.append("scenes 须为字符串列表 (挂钩场景 slug)")
    snaps = rec.get("snapshots")
    if snaps is not None:
        if not isinstance(snaps, list):
            errs.append("snapshots 须为列表")
        else:
            for i, s in enumerate(snaps):
                if not isinstance(s, dict) or not s.get("ts"):
                    errs.append(f"snapshots[{i}] 须为含 ts 的 dict")
    return errs


def metrics_for_scene(strategy_root: Path, scene: Optional[str]) -> tuple[list[dict], int]:
    """→ (可注入的该场景指标, 被敏感度挡下的条数)。

    只注入 `scenes` 显式包含该场景的指标 (无 scenes 字段 = 不挂任何场景, 不注入 —
    宁可少注不误注); tian_only 一律不注入, 单独计数。
    """
    if not scene:
        return [], 0
    inject: list[dict] = []
    blocked = 0
    for m in read_metrics(strategy_root):
        scenes = m.get("scenes") or []
        if scene not in scenes:
            continue
        if m.get("sensitivity") == "tian_only":
            blocked += 1
            continue
        inject.append(m)
    return inject, blocked


def metrics_context_block(strategy_root: Path, scene: Optional[str]) -> str:
    """该场景的指标注册表 → context.md 注入段。无可注入指标 → 空串 (不加空段)。fail-open。"""
    try:
        items, blocked = metrics_for_scene(strategy_root, scene)
    except Exception:  # noqa: BLE001
        return ""
    if not items and not blocked:
        return ""
    lines = [
        "## 指标注册表 (战略 OS · 在册目标对照)",
        "",
        "> 本业务线在册目标与最新快照 (主理/owner 人工维护, 非实时)。评「目标是否量化",
        "> 可考核」时请对照: 方案目标是否与在册指标对齐、量化、有 owner、有里程碑。",
        "",
    ]
    for m in items:
        snaps = m.get("snapshots") or []
        last = snaps[-1] if snaps and isinstance(snaps[-1], dict) else {}
        snap_txt = (f"{last.get('ts', '')} 快照: {last.get('value', '—')}"
                    if last else "暂无快照")
        lines.append(f"- **{m.get('name')}** (owner: {m.get('owner')})")
        lines.append(f"  目标: {m.get('target')} · {snap_txt}")
    if blocked:
        lines.append("")
        lines.append(f"> 另有 {blocked} 条 tian_only 指标未注入 (敏感度边界, 见注册表本体)。")
    lines.append("")
    return "\n".join(lines)
