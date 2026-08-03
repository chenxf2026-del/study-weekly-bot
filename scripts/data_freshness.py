#!/usr/bin/env python3
"""
data_freshness.py — wiki 快照新鲜度 (v1.0 S0.5 · ADR-011 §7)

读 data repo 的 `wiki/.snapshot` 戳 (data_sync.sh 写入), 供:
  - review_worker (A3): 评审卡片标注 "背景知识截至 <ts>"
  - CLI 巡检:  python3 scripts/data_freshness.py [--wiki-dir PATH]

.snapshot 格式 (data_sync.sh 产):
  2026-06-12T09:30:00+0800
  main_commit: abc1234
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VAULT_ROOT = Path(__file__).parent.parent.resolve()
# VM 布局: data repo 与 vault 平级; 本地布局: _wiki 即权威 (无快照戳 = 即时)
DEFAULT_WIKI_DIRS = (
    VAULT_ROOT.parent / "boss-vault-data" / "wiki",   # VM / 远程 (消费快照)
    VAULT_ROOT / "_wiki",                              # 本地 (生产地, 即时)
)

STALE_WARN_HOURS = 48  # 超过即在卡片字段加 ⚠️ (本地编译周期 + pull 周期的宽松上界)


def read_snapshot_ts(wiki_dir: Path) -> Optional[datetime]:
    """读 .snapshot 首行 ISO 时间戳; 无文件/解析失败 → None。"""
    snap = wiki_dir / ".snapshot"
    if not snap.is_file():
        return None
    try:
        first = snap.read_text(encoding="utf-8").splitlines()[0].strip()
        return datetime.fromisoformat(first)
    except (ValueError, IndexError, OSError):
        return None


def find_wiki_dir(explicit: Optional[Path] = None) -> Optional[Path]:
    """探测 wiki 目录: 显式 > data repo 快照 > 本地 _wiki。"""
    candidates = [explicit] if explicit else list(DEFAULT_WIKI_DIRS)
    for c in candidates:
        if c and c.is_dir():
            return c
    return None


def freshness_label(wiki_dir: Optional[Path] = None, now: Optional[datetime] = None) -> str:
    """卡片用一行字段。三态:
    - 有快照戳: "背景知识截至 YYYY-MM-DD HH:MM" (+超龄 ⚠️)
    - 本地 _wiki 无戳: "背景知识: 本地实时"
    - 全无: "背景知识: 不可用 (wiki 未同步)"
    """
    d = find_wiki_dir(wiki_dir)
    if d is None:
        return "背景知识: 不可用 (wiki 未同步 — 检查 data repo clone / vm_pull.timer)"
    ts = read_snapshot_ts(d)
    if ts is None:
        # 本地生产地: _wiki 由 sage-wiki 直写, 无快照戳 = 即时
        return "背景知识: 本地实时"
    now = now or datetime.now(timezone.utc)
    age_h = (now - ts.astimezone(timezone.utc)).total_seconds() / 3600
    label = f"背景知识截至 {ts.strftime('%Y-%m-%d %H:%M')}"
    if age_h > STALE_WARN_HOURS:
        label += f" ⚠️ (已 {age_h / 24:.1f} 天未更新 — 检查本地 data_sync cron)"
    return label


def main() -> int:
    ap = argparse.ArgumentParser(description="wiki 快照新鲜度 (ADR-011 §7)")
    ap.add_argument("--wiki-dir", type=Path, default=None)
    args = ap.parse_args()
    print(freshness_label(args.wiki_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
