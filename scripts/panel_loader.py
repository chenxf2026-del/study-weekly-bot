#!/usr/bin/env python3
"""
panel_loader.py — panel extends 继承 + scoring_mode 解析
(PRD: docs/internal/prd-multi-scene-v1.md · v1.1 · M1 框架层 R2)

让一个新 panel 从"写一整套配置"收敛为"描述与默认 panel 有何不同":

  extends: panels/default.yaml      # 继承父 panel, 未覆盖字段自动沿用
  judges_drop: [org-strategy]       # 移除评委 (按 slug)
  judges_add: [...]                 # 追加评委
  judges_override: [...]            # 完全替换 judges 列表 (与 drop/add 互斥)
  scoring_weights: {falsifiability: 1.5}   # overlay 5 镜头权重
  scoring_lenses_override: [...]    # 完全替换打分镜头 (竞赛 / OP2 100分制)
  scoring_mode: sum_max_score       # 评分模式 (默认 weighted_average)
  anchor_judge: null                # 无锚点 (竞赛模式)

设计纪律:
  - 仅支持 1 层继承 (panel 不允许再 extends 一个 extends 的 panel)
  - 合并后产出"解析后 panel" (resolved dict), 下游 (run_pipeline / worker) 直接用
  - panels/default.yaml 不含 extends 时走原样返回, 向后兼容

CLI:
  python3 scripts/panel_loader.py resolve scenes/op2-company/panel.yaml
  python3 scripts/panel_loader.py score-demo scenes/op2-company/panel.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

VAULT_ROOT = Path(__file__).parent.parent.resolve()

VALID_SCORING_MODES = {"weighted_average", "sum_max_score"}

# 继承时这些字段不直接拷贝, 由专门的合并逻辑处理
_MERGE_DIRECTIVE_KEYS = {
    "extends", "judges_drop", "judges_add", "judges_override",
    "scoring_weights", "scoring_lenses_override",
}


class PanelError(ValueError):
    """panel.yaml 校验 / 继承失败。"""


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PanelError(f"panel 文件不存在: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PanelError(f"{path}: panel.yaml 顶层必须是 mapping")
    return data


def _resolve_extends_path(extends: str, *, child_path: Path,
                          root: Path) -> Path:
    """extends 字段 → 父 panel 路径。

    支持:
      extends: panels/default.yaml      (相对 vault root)
      extends: default                  (panels/ 下的 name)
      extends: ../foo/panel.yaml        (相对 child 目录)
    """
    # 绝对/含分隔符: 先按 vault root 试, 再按 child 目录试
    if "/" in extends or extends.endswith(".yaml"):
        cand = root / extends
        if cand.is_file():
            return cand
        cand2 = (child_path.parent / extends).resolve()
        if cand2.is_file():
            return cand2
        raise PanelError(f"extends 父 panel 找不到: {extends}")
    # 纯 name → panels/<name>.yaml
    cand = root / "panels" / f"{extends}.yaml"
    if cand.is_file():
        return cand
    raise PanelError(f"extends 父 panel 找不到: panels/{extends}.yaml")


def _apply_judge_directives(parent_judges: list[dict], child: dict[str, Any]) -> list[dict]:
    """对父 panel 的 judges 应用 drop / add / override 指令。"""
    # override 完全替换 (与 drop/add 互斥, override 优先)
    if "judges_override" in child:
        override = child["judges_override"]
        if not isinstance(override, list):
            raise PanelError("judges_override 必须是 list")
        return [dict(j) for j in override]

    judges = [dict(j) for j in parent_judges]

    drop = set(child.get("judges_drop") or [])
    if drop:
        judges = [j for j in judges if j.get("slug") not in drop]

    add = child.get("judges_add") or []
    if add:
        if not isinstance(add, list):
            raise PanelError("judges_add 必须是 list")
        existing = {j.get("slug") for j in judges}
        for j in add:
            if not isinstance(j, dict) or not j.get("slug"):
                raise PanelError(f"judges_add 每项须含 slug: {j!r}")
            if j["slug"] in existing:
                # 同 slug 覆盖 (用 add 的定义替换)
                judges = [jj for jj in judges if jj.get("slug") != j["slug"]]
            judges.append(dict(j))

    return judges


def _merge_scoring_lenses(parent: dict, child: dict[str, Any]) -> Any:
    """scoring_lenses_override 完全替换; 否则沿用父。"""
    if "scoring_lenses_override" in child:
        lenses = child["scoring_lenses_override"]
        if not isinstance(lenses, list) or not lenses:
            raise PanelError("scoring_lenses_override 必须是非空 list")
        return lenses
    return parent.get("scoring_lenses", [])


def resolve_panel(path: Path | str, *, root: Path | None = None,
                  _depth: int = 0) -> dict[str, Any]:
    """加载并解析一个 panel.yaml, 把 extends 继承展开成完整 panel dict。

    返回的 dict 是"解析后 panel": 含最终 judges / scoring_lenses / scoring_mode /
    scoring_weights / anchor_judge, 下游可直接消费。
    """
    root = root or VAULT_ROOT
    path = Path(path)
    if _depth > 1:
        raise PanelError("panel extends 仅支持 1 层继承 (父 panel 不能再 extends)")

    child = _read_yaml(path)
    extends = child.get("extends")

    if not extends:
        # 无继承: 原样返回 (向后兼容 panels/default.yaml)
        resolved = dict(child)
    else:
        parent_path = _resolve_extends_path(extends, child_path=path, root=root)
        parent = resolve_panel(parent_path, root=root, _depth=_depth + 1)

        resolved = dict(parent)
        # 1. 标量/普通字段: child 覆盖 parent (跳过合并指令字段)
        for k, v in child.items():
            if k in _MERGE_DIRECTIVE_KEYS:
                continue
            resolved[k] = v
        # 2. judges: drop/add/override
        resolved["judges"] = _apply_judge_directives(parent.get("judges", []), child)
        # 3. scoring_lenses: override or inherit
        resolved["scoring_lenses"] = _merge_scoring_lenses(parent, child)
        # 4. scoring_weights: overlay (parent ∪ child, child 优先)
        weights = dict(parent.get("scoring_weights") or {})
        weights.update(child.get("scoring_weights") or {})
        if weights:
            resolved["scoring_weights"] = weights

    # ─── 归一化 + 校验 ───
    resolved.setdefault("scoring_mode", "weighted_average")
    if resolved["scoring_mode"] not in VALID_SCORING_MODES:
        raise PanelError(
            f"{path}: 非法 scoring_mode={resolved['scoring_mode']!r}, "
            f"合法值: {sorted(VALID_SCORING_MODES)}")

    _validate_resolved(resolved, source=str(path))
    return resolved


def _validate_resolved(panel: dict[str, Any], *, source: str) -> None:
    """对解析后 panel 做一致性校验。"""
    if not panel.get("judges"):
        raise PanelError(f"{source}: 解析后 panel 无 judges")

    mode = panel["scoring_mode"]
    lenses = panel.get("scoring_lenses") or []

    if mode == "sum_max_score":
        # 镜头须是 dict 列表且 max_score 为正整数。
        # 注意: 不强制总分 == 100 — OP2 通用维度源文档实际合计 110 (源文档本身的
        # 标注与分值不一致, 见 PRD §4.1 脚注)。等级判定用百分比 (score/total) 归一,
        # 因此总分非 100 仍可用; grade_for_score() 按 total 自动归一。
        for lens in lenses:
            if not isinstance(lens, dict) or "max_score" not in lens:
                raise PanelError(
                    f"{source}: scoring_mode=sum_max_score 时每个镜头须含 max_score: {lens!r}")
            ms = lens["max_score"]
            if not isinstance(ms, int) or ms <= 0:
                raise PanelError(
                    f"{source}: max_score 须为正整数, 镜头 {lens.get('slug')!r} = {ms!r}")


def total_max_score(panel: dict[str, Any]) -> int:
    """sum_max_score 模式下镜头满分之和 (通常 100)。"""
    return sum(int(l.get("max_score", 0)) for l in panel.get("scoring_lenses", [])
               if isinstance(l, dict))


def grade_for_score(panel: dict[str, Any], score: float) -> str:
    """按 panel.score_threshold 把总分映射成等级标签。

    约定 (PRD §4.1): < rewrite → 重写; rewrite..revise → 修改; ≥ revise → 进人工评审。
    默认门槛 rewrite=60 / revise=80 (按"百分制"语义)。

    归一化: score 与门槛先换算成百分比 (score / total_max * 100) 再比较, 以兼容
    总分非 100 的 panel (如 OP2 通用维度合计 110)。total_max 不可得时按原值比较。
    """
    thr = panel.get("score_threshold") or {}
    rewrite = float(thr.get("rewrite", 60))
    revise = float(thr.get("revise", 80))

    total = total_max_score(panel)
    pct = (score / total * 100) if total > 0 else score

    if pct < rewrite:
        return "重写"
    if pct < revise:
        return "修改"
    return "人工评审"


def has_anchor(panel: dict[str, Any]) -> bool:
    """panel 是否含锚点评委 (anchor_judge 非 null)。"""
    return panel.get("anchor_judge") is not None


# ─────────────────────────── CLI ───────────────────────────

def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="panel extends 继承解析")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_res = sub.add_parser("resolve", help="解析 panel (展开 extends) 并打印")
    p_res.add_argument("path", help="panel.yaml 路径")

    args = ap.parse_args(argv)

    if args.cmd == "resolve":
        try:
            resolved = resolve_panel(args.path)
        except PanelError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            return 1
        judges = [j.get("slug") for j in resolved.get("judges", [])]
        print(f"name:         {resolved.get('name')}")
        print(f"scoring_mode: {resolved.get('scoring_mode')}")
        print(f"anchor_judge: {resolved.get('anchor_judge', '(默认)')}")
        print(f"judges ({len(judges)}): {judges}")
        if resolved.get("scoring_mode") == "sum_max_score":
            print(f"total_max:    {total_max_score(resolved)}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
