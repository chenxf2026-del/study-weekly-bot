#!/usr/bin/env bash
# sync_from_upstream.sh — 从上游 boss-vault 单向同步 vendored 引擎
#
# 纪律 (见 README「vendored 引擎」一节):
#   引擎在两仓各活一份。本地改一行 → 下次同步冲突 → 攒够了两仓永久分叉,
#   上游修的 bug 与安全问题再也过不来。所以: **只整文件覆盖, 不 merge**。
#
# 用法:
#   UPSTREAM=/path/to/boss-vault bash scripts/sync_from_upstream.sh
#   UPSTREAM=git@github.com:zhanglunet/boss-vault.git bash scripts/sync_from_upstream.sh
#
# 流程: 取上游 → 整文件覆盖锁定清单 → 跑测试 → 更新 UPSTREAM.lock
# 建议节奏: 季度一次; 上游发安全/严重 bug 修复时即时。

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

UPSTREAM="${UPSTREAM:-}"
[ -n "$UPSTREAM" ] || { echo "✗ 需指定 UPSTREAM=<上游仓路径或 URL>" >&2; exit 2; }

TMP=""
if [ ! -d "$UPSTREAM/.git" ]; then
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  echo "→ 克隆上游到临时目录 …"
  git clone --depth 1 "$UPSTREAM" "$TMP/up" >/dev/null 2>&1
  UPSTREAM="$TMP/up"
fi

UP_COMMIT="$(git -C "$UPSTREAM" rev-parse HEAD)"
echo "→ 上游 commit: ${UP_COMMIT:0:12}"

# 只覆盖 drifted=false 的文件
python3 - "$UPSTREAM" "$UP_COMMIT" <<'PY'
import hashlib, json, shutil, sys, pathlib
up = pathlib.Path(sys.argv[1]); commit = sys.argv[2]
ROOT = pathlib.Path.cwd()
lock = json.loads((ROOT/"UPSTREAM.lock").read_text(encoding="utf-8"))
changed, skipped, missing = [], [], []
for rel, meta in lock["files"].items():
    if meta["drifted"]:
        skipped.append(rel); continue
    src = up / rel
    if not src.exists():
        missing.append(rel); continue
    dst = ROOT / rel
    new = hashlib.sha256(src.read_bytes()).hexdigest()
    if not dst.exists() or new != meta["sha256"]:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        changed.append(rel)
    meta["sha256"] = new
lock["upstream_commit"] = commit
import datetime  # noqa: E402  (同步时间由调用方系统时钟决定)
lock["synced_at"] = datetime.date.today().isoformat()
(ROOT/"UPSTREAM.lock").write_text(json.dumps(lock, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
print(f"  覆盖 {len(changed)} 个 · 跳过场景层 {len(skipped)} 个" + (f" · ⚠ 上游已无 {len(missing)} 个" if missing else ""))
for f in changed: print(f"    ~ {f}")
for f in missing: print(f"    ! 上游已删除, 请人工确认: {f}")
PY

echo "→ 跑测试 …"
.venv/bin/python -m pytest tests/unit -q || { echo "✗ 同步后测试不过 —— 请人工检查再提交"; exit 1; }
.venv/bin/python scripts/check_vendored.py

echo "✓ 同步完成。请 review 后提交: git add -A && git commit -m 'chore: sync engine from upstream ${UP_COMMIT:0:12}'"
