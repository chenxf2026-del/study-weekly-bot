"""boss_core.kb.vault_paths — vault 路径解析 + 评委 doctrine 加载 (M2.0a, 从 run_pipeline_local 下沉)。

panel 路径解析 / panel.yaml 的 slug→skill_path·display_name 映射 / 评委 SKILL.md 加载。
M1 里这些是 HostedKB 的 DI 注入项 (rpl-resident); M2 服务 (boss_service) 禁 import rpl,
故下沉至此 — 服务可自建 HostedKB 实例直接引用。

按 R-a 同款范式: 核心函数**显式收路径参数** (默认用本模块常量), run_pipeline_local 留
同名薄 wrapper 注入自己的 (测试可 monkeypatch 的) VAULT_ROOT/PANELS_DIR/ANCHORS_DIR/
SKILLS_DIR 全局 — 现有测试对 rpl 全局的 patch 语义零改动。缓存按**解析后路径**键
(原按 panel_name 键; 生产语义等价, 对 patch 路径的测试更正确)。

只依赖 stdlib + lazy yaml + boss_core.errors (K-c / §6 R-b: 绝不 import run_pipeline_local)。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from boss_core.errors import PipelineError

# 仓库根 (boss_core/kb → scripts → repo root); 与 run_pipeline_local.VAULT_ROOT 同解析结果。
VAULT_ROOT = Path(__file__).resolve().parents[3]
PANELS_DIR = VAULT_ROOT / "panels"
ANCHORS_DIR = VAULT_ROOT / "anchors"           # multi-anchor B 档 · per-anchor perspective skills
SKILLS_DIR = VAULT_ROOT / "skills"


def _resolve_panel_path(panel_name: str, *, vault_root: Optional[Path] = None,
                        panels_dir: Optional[Path] = None) -> Path:
    """panel_name 解析规则:
    - 以 '.yaml' 结尾 → 视为路径 (绝对路径直接用; 相对路径相对 vault_root)。
      场景 panel 用此形式: 'scenes/op2-company/panel.yaml'。
    - 否则 → <panels_dir>/<name>.yaml (向后兼容旧行为)。
    """
    vr = vault_root if vault_root is not None else VAULT_ROOT
    pd = panels_dir if panels_dir is not None else PANELS_DIR
    if panel_name.endswith(".yaml"):
        p = Path(panel_name)
        return p if p.is_absolute() else vr / p
    return pd / f"{panel_name}.yaml"


@lru_cache(maxsize=32)
def _skill_map_by_path(panel_path_str: str) -> dict[str, str]:
    """panel.yaml → slug → skill_path (vault 相对路径)。按路径键缓存。"""
    try:
        import yaml
        data = yaml.safe_load(Path(panel_path_str).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    out: dict[str, str] = {}
    for key in ("judges", "judges_override", "judges_add"):
        for entry in (data.get(key) or []):
            if isinstance(entry, dict) and entry.get("slug") and entry.get("skill_path"):
                out[entry["slug"]] = entry["skill_path"]
    return out


@lru_cache(maxsize=32)
def _display_map_by_path(panel_path_str: str) -> dict[str, str]:
    """panel.yaml → slug → display_name_cn。按路径键缓存。"""
    try:
        import yaml
        data = yaml.safe_load(Path(panel_path_str).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    out: dict[str, str] = {}
    for key in ("judges", "judges_override", "judges_add"):
        for entry in (data.get(key) or []):
            if isinstance(entry, dict) and entry.get("slug") and entry.get("display_name_cn"):
                out[entry["slug"]] = str(entry["display_name_cn"])
    return out


def _panel_judge_skill_map(panel_name: str, *, panel_path: Optional[Path] = None) -> dict[str, str]:
    """从 panel.yaml 的 judges / judges_override / judges_add 收集 slug → skill_path
    (vault 相对路径)。scene panel 的评委 (scenes/shared-judges/, scenes/<scene>/judges/)
    用显式 skill_path 指定, 不在默认 anchors/ · skills/ 约定路径下。无 skill_path 的条目跳过。"""
    p = panel_path if panel_path is not None else _resolve_panel_path(panel_name)
    return _skill_map_by_path(str(p))


def _panel_judge_display_map(panel_name: str, *, panel_path: Optional[Path] = None) -> dict[str, str]:
    """slug → display_name_cn (含中英文姓名, 如 'Palantir CTO 评委·Shyam Sankar 视角' /
    '梁胜评委（AI 技术战略）')。从 panel.yaml judges/judges_override/judges_add 收集。"""
    p = panel_path if panel_path is not None else _resolve_panel_path(panel_name)
    return _display_map_by_path(str(p))


def _judge_label(judge: str, panel_name: str | None, *,
                 display_map_fn: Optional[Callable[[str], dict[str, str]]] = None) -> str:
    """报告里评委的显示名 = panel display_name_cn (含中英文姓名); 无映射时回退 slug。"""
    if panel_name:
        fn = display_map_fn if display_map_fn is not None else _panel_judge_display_map
        name = fn(panel_name).get(judge)
        if name:
            return name
    return judge


def _load_judge_skill(judge: str, panel_name: str | None = None, *,
                      vault_root: Optional[Path] = None,
                      anchors_dir: Optional[Path] = None,
                      skills_dir: Optional[Path] = None,
                      skill_map_fn: Optional[Callable[[str], dict[str, str]]] = None) -> str:
    """读 perspective skill 作 system prompt.

    解析顺序:
    1. scene panel 显式 skill_path (judges_override/judges_add) — 多场景评委在
       scenes/shared-judges/ 等, 不在默认约定路径下
    2. anchor judge → anchors/<judge>/perspective/SKILL.md (ADR-002 multi-anchor B 档)
    3. dim judge → skills/<judge>-perspective/SKILL.md
    """
    vr = vault_root if vault_root is not None else VAULT_ROOT
    ad = anchors_dir if anchors_dir is not None else ANCHORS_DIR
    sd = skills_dir if skills_dir is not None else SKILLS_DIR
    # 1. scene panel 显式 skill_path
    if panel_name:
        fn = skill_map_fn if skill_map_fn is not None else _panel_judge_skill_map
        sp = fn(panel_name).get(judge)
        if sp:
            p = Path(sp) if Path(sp).is_absolute() else (vr / sp)
            if p.exists():
                return p.read_text(encoding="utf-8")

    # 2. anchors/<judge>/perspective/ (anchor judge path)
    anchor_path = ad / judge / "perspective" / "SKILL.md"
    if anchor_path.exists():
        return anchor_path.read_text(encoding="utf-8")

    # 3. skills/<judge>-perspective/ (dim judge path)
    dim_path = sd / f"{judge}-perspective" / "SKILL.md"
    if not dim_path.exists():
        raise PipelineError(
            f"SKILL.md 不存在: panel({panel_name}) 未指定 skill_path, "
            f"且不在 {anchor_path} 也不在 {dim_path}")
    return dim_path.read_text(encoding="utf-8")


# anchor 评委兜底 (panel 缺失 / 解析失败 / 无 anchor 条目时)
ANCHOR_JUDGES_FALLBACK = frozenset({"tian"})


def derive_anchor_judges(panel_path: Path, *, fallback=ANCHOR_JUDGES_FALLBACK) -> set[str]:
    """从 panel.yaml judges[].judge_category == 'anchor' 推导 anchor 评委集合。
    **纯函数** (M2.0b 从 refresh_anchor_judges 推导体抽出; rpl wrapper 保留刷新
    模块全局 ANCHOR_JUDGES 的语义)。解析失败 / 无 anchor → set(fallback)。"""
    import yaml
    anchors: set[str] = set()
    try:
        data = yaml.safe_load(panel_path.read_text(encoding="utf-8"))
        for j in (data or {}).get("judges", []):
            if isinstance(j, dict) and j.get("judge_category") == "anchor" and j.get("slug"):
                anchors.add(j["slug"])
    except Exception:
        pass
    if not anchors:
        anchors = set(fallback)
    return anchors
