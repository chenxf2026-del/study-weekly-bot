#!/usr/bin/env python3
"""check_vendored.py — vendored 引擎完整性校验 (fail-close)。

本仓的 `scripts/` 绝大部分是从上游 boss-vault **整份拷来的引擎**, 不是本仓原创。
这些文件的纪律是: **只允许从上游整文件同步, 禁止本地修改**。

为什么这么严:
    引擎在两个仓各活一份。只要本地改了一行, 下次从上游同步就会冲突; 冲突攒够了
    就再也同步不动, 两仓永久分叉 —— 上游修的 bug 与安全问题再也过不来。
    想改功能 → 回上游提 issue / PR, 上游改完再同步下来。

例外: `UPSTREAM.lock` 里 `drifted: true` 的文件是**场景层**, 本仓自己的地盘,
可自由演进, 不做 hash 校验。

★ 第三态 `redacted`: 仍 hash 锁定, 但内容 = 上游原文 **减去机密删节** (剥离本仓时移
   除了上游写死的他方真名 / 客户名)。**从上游同步这些文件后必须重新删节**, 否则会把
   那些名字带回本仓 —— 这正是本仓当初剥离要避免的事。

用法:
    python3 scripts/check_vendored.py          # 有改动即 exit 1
    python3 scripts/check_vendored.py --list   # 只列当前锁定清单 (含删节说明)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "UPSTREAM.lock"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校验 vendored 引擎未被本地修改")
    ap.add_argument("--list", action="store_true", help="只列锁定清单, 不校验")
    args = ap.parse_args(argv)

    if not LOCK.exists():
        print(f"✗ 缺 {LOCK.name} — 无法校验 vendored 完整性")
        return 1
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    files = lock["files"]

    if args.list:
        locked = [f for f, v in files.items() if not v["drifted"]]
        drift = [f for f, v in files.items() if v["drifted"]]
        redacted = {f: v["redacted"] for f, v in files.items() if v.get("redacted")}
        print(f"锁定 (禁改) {len(locked)} 个 · 可漂移 (场景层) {len(drift)} 个 · 含删节 {len(redacted)} 个")
        print(f"上游 commit: {lock['upstream_commit'][:12]}  同步于 {lock['synced_at']}")
        for f in drift:
            print(f"  [可改] {f}")
        if redacted:
            print("\n  ★ 以下文件 = 上游原文 - 机密删节, 同步后必须重新删节:")
            for f, why in redacted.items():
                print(f"    {f}\n        {why}")
        for f, why in (lock.get("removed_from_upstream") or {}).items():
            print(f"  [不带] {f}\n        {why}")
        return 0

    modified, missing = [], []
    for rel, meta in files.items():
        if meta["drifted"]:
            continue
        p = ROOT / rel
        if not p.exists():
            missing.append(rel)
            continue
        if hashlib.sha256(p.read_bytes()).hexdigest() != meta["sha256"]:
            modified.append(rel)

    if not modified and not missing:
        n = sum(1 for v in files.values() if not v["drifted"])
        n_red = sum(1 for v in files.values() if v.get("redacted"))
        print(f"✓ vendored 引擎完整 — {n} 个文件与锁定基线一致 "
              f"(上游 {lock['upstream_commit'][:12]}, 其中 {n_red} 个含机密删节)")
        return 0

    if missing:
        print(f"✗ vendored 文件缺失 ({len(missing)}):")
        for f in missing:
            print(f"    {f}")
    if modified:
        print(f"✗ vendored 文件被本地修改 ({len(modified)}):")
        for f in modified:
            print(f"    {f}")
        print()
        print("  vendored 引擎不允许本地改动 —— 这样才同步得动上游。")
        print("  想改功能: 回上游 boss-vault 提 issue/PR, 合了再 sync 下来。")
        print("  确实是有意改动 (且已想清楚代价): 更新 UPSTREAM.lock 里对应的 sha256,")
        print("  或把该文件标 drifted:true 移出锁定面。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
