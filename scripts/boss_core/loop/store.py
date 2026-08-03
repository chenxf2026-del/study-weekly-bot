"""boss_core.loop.store — 回路对象的文件存取 (文件即真相 · PRD D1)。

写 fail-close (校验不过 / 会破坏 append-only → 抛 ValueError), 读 fail-open
(坏文件跳过, 目录缺失 = 空)。全部显式传根路径, 无隐藏全局 — 可单测。

布局 (PRD §4):
  strategy/decisions/<YYYY-MM>/<suggestion_id>--<ts>.json   append-only
  strategy/actions/<action_id>.json                          upsert (checkins 只追加)
  strategy/metrics/<id>.yaml                                 人工维护
  reports/<brand>/suggestions.json                           Phase 5 写
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from pathlib import Path
from typing import Optional

from boss_core.loop.models import ACTION_STATUSES, validate_action, validate_decision

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_id(s: str) -> str:
    return _SAFE_ID_RE.sub("_", str(s))[:120] or "x"


def _atomic_write(path: Path, text: str) -> None:
    """临时文件 + rename 原子落盘 (中途被打断不产生半截文件)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ── suggestions (reports/<brand>/suggestions.json) ──

def suggestions_path(brand_dir: Path) -> Path:
    return Path(brand_dir) / "suggestions.json"


def write_suggestions(brand_dir: Path, payload: dict) -> Path:
    p = suggestions_path(brand_dir)
    _atomic_write(p, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return p


def read_suggestions(brand_dir: Path) -> Optional[dict]:
    p = suggestions_path(brand_dir)
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else None
    except Exception:  # noqa: BLE001 — fail-open
        return None


# ── decisions (append-only) ──

def append_decision(strategy_root: Path, rec: dict) -> Path:
    """写一条决策记录。校验 fail-close; 同 (suggestion_id, ts) 已存在 → 拒绝
    (append-only: 修正决策 = 新 ts 追加一条, 永不改写历史)。"""
    errs = validate_decision(rec)
    if errs:
        raise ValueError("决策记录非法: " + "; ".join(errs))
    ts = int(rec["ts"])
    month = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m")
    p = (Path(strategy_root) / "decisions" / month
         / f"{_safe_id(rec['suggestion_id'])}--{ts}.json")
    if p.exists():
        raise ValueError(f"决策记录已存在, append-only 拒绝覆盖: {p.name}")
    _atomic_write(p, json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    return p


def read_decisions(strategy_root: Path) -> list[dict]:
    out: list[dict] = []
    d = Path(strategy_root) / "decisions"
    try:
        if d.is_dir():
            for p in sorted(d.rglob("*.json")):
                try:
                    v = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(v, dict):
                        out.append(v)
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        pass
    return out


# ── actions (upsert; checkins 只追加) ──

def _action_path(strategy_root: Path, action_id: str) -> Path:
    return Path(strategy_root) / "actions" / f"{_safe_id(action_id)}.json"


def upsert_action(strategy_root: Path, rec: dict) -> Path:
    """新建或更新行动。更新时 checkins 只允许追加 (前缀必须与已存一致), 防改写历史。"""
    errs = validate_action(rec)
    if errs:
        raise ValueError("行动记录非法: " + "; ".join(errs))
    p = _action_path(strategy_root, rec["id"])
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            old = {}
        old_ci = old.get("checkins") or []
        new_ci = rec.get("checkins") or []
        if new_ci[: len(old_ci)] != old_ci:
            raise ValueError(f"action {rec['id']}: checkins 只允许追加, 拒绝改写历史")
    _atomic_write(p, json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    return p


def append_checkin(strategy_root: Path, action_id: str, checkin: dict) -> Path:
    """追加一条 check-in 并同步顶层 status (跟催回收的标准入口)。"""
    if checkin.get("status") not in ACTION_STATUSES:
        raise ValueError(f"checkin.status 非法 (须 ∈ {ACTION_STATUSES})")
    p = _action_path(strategy_root, action_id)
    rec = json.loads(p.read_text(encoding="utf-8"))   # 不存在 → 自然抛 (fail-close)
    rec.setdefault("checkins", []).append(checkin)
    rec["status"] = checkin["status"]
    _atomic_write(p, json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    return p


def read_actions(strategy_root: Path) -> list[dict]:
    out: list[dict] = []
    d = Path(strategy_root) / "actions"
    try:
        if d.is_dir():
            for p in sorted(d.glob("*.json")):
                try:
                    v = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(v, dict):
                        out.append(v)
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        pass
    return out


# ── metrics (人工 YAML, 只读) ──

def read_metrics(strategy_root: Path) -> list[dict]:
    out: list[dict] = []
    d = Path(strategy_root) / "metrics"
    try:
        import yaml
        if d.is_dir():
            for p in sorted(d.glob("*.yaml")):
                try:
                    v = yaml.safe_load(p.read_text(encoding="utf-8"))
                    if isinstance(v, dict):
                        v.setdefault("id", p.stem)
                        out.append(v)
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        pass
    return out
