#!/usr/bin/env python3
"""review_batch.py — 会议总结评审「多演讲人攒批」缓冲 (仅 output_format==meeting_summary 场景)。

飞书一条消息一个文件, 多演讲人材料必须在应用层「攒」: 收到文件先缓冲, 收到触发词
(评审/开始/汇总…) 再把该会话攒的所有文件合并成一份 doc → 交回 feishu_events enqueue 一个 job。

- 批次按 (scene_slug, conv_key) 隔离; conv_key = 群 chat_id / 单聊 sender open_id (即 reply_id)。
- 落盘: cases/.review-inbox/_batches/<hash>/ (files + _manifest.json)。cases/ 在 sage-wiki ignore, 不入 KB。
- 过期: 批次首文件超 TTL (默认 6h) 视为上一场会残留, 下次 add 前自动清空 (防跨会串料)。
- 配额: 攒批不建 job → 不耗配额 (check_access 只读扫队列); 只有 finalize enqueue 才耗 1 份。

纯 IO + 数据装配, 无飞书依赖 → 可单测。合并读文本复用 run_pipeline_local._read_doc_text。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VAULT_ROOT = Path(__file__).resolve().parent.parent
if str(VAULT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(VAULT_ROOT / "scripts"))

BATCH_TTL_SEC = int(os.environ.get("MEETING_BATCH_TTL_SEC", str(6 * 3600)))
_MANIFEST = "_manifest.json"

# 触发「开始汇总评审」的词 (归一化后精确匹配; 不含 '/' 前缀命令, 与 admin command 不冲突)。
TRIGGER_WORDS = {
    "评审", "开始", "开始评审", "开评", "汇总", "生成", "生成总结",
    "总结", "评一下", "开始生成", "开始汇总", "go", "start", "review",
}

# 「取消/清空」当前会话攒批的词 (与触发词同款精确匹配)。想重开一批时用, 不必等 6h 过期。
CANCEL_WORDS = {
    "取消", "清空", "清除", "清空材料", "取消评审", "取消攒批", "重来", "重新开始",
    "reset", "clear", "cancel",
}

# 会议材料 (合并后) 字数上限: 多演讲人大会材料量大, 默认放宽到 12 万 (Opus 200k 上下文吃得下);
# 可用 env 调 (WAF/token 收紧时下调)。review_batch 合并 + meeting_summary_pipeline 读取共用同一口径。
MEETING_MATERIAL_MAX_CHARS = int(os.environ.get("MEETING_MATERIAL_MAX_CHARS", "120000"))


def _normalize(text: str) -> str:
    return re.sub(r"[\s，。,.!！、:：;；]+", "", (text or "")).strip().lower()


def is_trigger(text: str) -> bool:
    """归一化文本是否是「开始汇总」触发词 (去空白/标点/@残留后精确匹配, 防长句误触)。"""
    return _normalize(text) in TRIGGER_WORDS


def is_cancel(text: str) -> bool:
    """归一化文本是否是「取消/清空攒批」词 (精确匹配, 防长句误触)。"""
    return _normalize(text) in CANCEL_WORDS


@dataclass
class BatchState:
    key_dir: Path
    files: list[dict]           # [{name, path, ts}] 顺序即提交顺序 (deck 材料序)

    @property
    def count(self) -> int:
        return len(self.files)

    @property
    def names(self) -> list[str]:
        return [f["name"] for f in self.files]


# ─────────────────────────── 内部 ───────────────────────────

def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _batches_root(root: Optional[Path]) -> Path:
    return (root or VAULT_ROOT) / "cases" / ".review-inbox" / "_batches"


def _key_dir(scene_slug: str, conv_key: str, root: Optional[Path]) -> Path:
    h = hashlib.sha1(f"{scene_slug}::{conv_key}".encode("utf-8")).hexdigest()[:16]
    return _batches_root(root) / h


def _read_manifest(key_dir: Path) -> list[dict]:
    mf = key_dir / _MANIFEST
    if not mf.is_file():
        return []
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 — 损坏 manifest 当空批, 不炸
        return []


def _write_manifest(key_dir: Path, files: list[dict]) -> None:
    key_dir.mkdir(parents=True, exist_ok=True)
    (key_dir / _MANIFEST).write_text(json.dumps(files, ensure_ascii=False, indent=2), encoding="utf-8")


def _expired(files: list[dict], now: datetime) -> bool:
    if not files:
        return False
    try:
        first = datetime.fromisoformat(files[0]["ts"])
    except Exception:  # noqa: BLE001
        return False
    return (now - first).total_seconds() > BATCH_TTL_SEC


# ─────────────────────────── 公开 API ───────────────────────────

def clear(scene_slug: str, conv_key: str, *, root: Optional[Path] = None) -> None:
    """丢弃该会话的批次 (文件 + manifest)。"""
    kd = _key_dir(scene_slug, conv_key, root)
    if kd.exists():
        shutil.rmtree(kd, ignore_errors=True)


def peek(scene_slug: str, conv_key: str, *, root: Optional[Path] = None,
         now: Optional[datetime] = None) -> Optional[BatchState]:
    """取当前批次 (过期自动清空并返回 None); 空批返回 None。"""
    kd = _key_dir(scene_slug, conv_key, root)
    files = _read_manifest(kd)
    if not files:
        return None
    if _expired(files, _now(now)):
        clear(scene_slug, conv_key, root=root)
        return None
    return BatchState(kd, files)


def add(scene_slug: str, conv_key: str, downloaded_path: Path, file_name: str, *,
        root: Optional[Path] = None, now: Optional[datetime] = None) -> BatchState:
    """把已下载的文件纳入批次 (过期批次先清空)。移动文件进批次目录并追加 manifest。返回更新后的 BatchState。"""
    now = _now(now)
    kd = _key_dir(scene_slug, conv_key, root)
    files = _read_manifest(kd)
    if _expired(files, now):
        clear(scene_slug, conv_key, root=root)
        files = []
    kd.mkdir(parents=True, exist_ok=True)
    idx = len(files) + 1
    dest = kd / f"{idx:02d}-{Path(file_name).name}"
    try:
        shutil.move(str(downloaded_path), str(dest))
    except Exception:  # noqa: BLE001 — 跨设备/占用 → 退化为拷贝
        shutil.copyfile(str(downloaded_path), str(dest))
    files.append({"name": file_name, "path": str(dest), "ts": now.isoformat()})
    _write_manifest(kd, files)
    return BatchState(kd, files)


def combine_and_clear(scene_slug: str, conv_key: str, *, root: Optional[Path] = None,
                      now: Optional[datetime] = None,
                      max_chars: Optional[int] = None) -> Optional[tuple[Path, str, list[str]]]:
    """合并该会话攒的所有文件成一份 md → 返回 (combined_path, scope, names)。空批返回 None。

    - 每份加「## 【材料 i / <演讲人=文件名 stem>】」小节头, 供评委按演讲人归因。
    - scope = 各文件名 stem 顿号连接, 供 deck 封面「资料范围」(闭合此前 scope 永远空白的洞)。
    - 合并后清空批次 (combined md 落在 _batches/ 下, 不随批次目录删除)。
    - max_chars: 合并总量上限, 缺省用 MEETING_MATERIAL_MAX_CHARS (默认 12 万, 多演讲人大会用)。
    """
    if max_chars is None:
        max_chars = MEETING_MATERIAL_MAX_CHARS
    st = peek(scene_slug, conv_key, root=root, now=now)
    if not st or st.count == 0:
        return None
    import run_pipeline_local as rp
    stems, names, sections = [], [], []
    for i, f in enumerate(st.files, 1):
        name = f.get("name", f"材料{i}")
        stem = Path(name).stem
        names.append(name)
        stems.append(stem)
        try:
            text = rp._read_doc_text(Path(f["path"]))
        except Exception as e:  # noqa: BLE001 — 单份读失败不拖垮整批
            text = f"（材料读取失败: {type(e).__name__}: {e}）"
        sections.append(f"## 【材料 {i} / {stem}】\n\n{(text or '').strip()}")
    combined = (f"# 会议材料合集（{st.count} 份 · 多演讲人）\n\n"
                + "\n\n---\n\n".join(sections))
    if hasattr(rp, "_cap_doc_text"):
        combined, _ = rp._cap_doc_text(combined, max_chars)
    else:
        combined = combined[:max_chars]
    out = _batches_root(root) / f"combined-{st.key_dir.name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(combined, encoding="utf-8")
    scope = "、".join(stems)
    clear(scene_slug, conv_key, root=root)
    return out, scope, names
