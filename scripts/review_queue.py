#!/usr/bin/env python3
"""
review_queue.py — review-service 文件 job 队列 (v1.0 A2.1 · PRD §3)

vault 范式: job = JSON 文件, 状态 = 所在目录, 流转 = 原子 rename (同文件系统)。
无 Redis / 无数据库; VM 重启后队列状态天然保留 (PRD §8 验收第 3 条)。

布局:
  cases/.review-jobs/
    ├── pending/   RJ-<ts>-<rand>.json   ← enqueue 落此; 文件名按时间排序 = FIFO
    ├── running/                          ← worker claim (rename) 后
    ├── done/                             ← complete (含 result 字段)
    └── failed/                           ← fail (含 error 字段, 保留排障)

并发模型: 单 worker 串行消费 (PRD: LLM 成本与并发控制); claim 用 rename 原子性
保证即使误起多 worker 也不会重复消费同一 job (rename 失败 = 别人抢到, 跳过)。
崩溃恢复: worker 启动时 recover_stale() 把超龄 running (上次崩溃遗留) 退回 pending。
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

VAULT_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_QUEUE_ROOT = VAULT_ROOT / "cases" / ".review-jobs"
STATES = ("pending", "running", "done", "failed")
STALE_RUNNING_MINUTES = 90  # REVIEW ~18min; 90min 仍 running = worker 崩溃遗留


def _resolve_root(queue_root: Optional[Path]) -> Path:
    """默认参数在定义时绑定会让 DEFAULT_QUEUE_ROOT 不可 monkeypatch (v1.1 测试泄漏教训) —
    统一调用时解析模块属性。"""
    return queue_root if queue_root is not None else DEFAULT_QUEUE_ROOT


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_dirs(queue_root: Path) -> None:
    for s in STATES:
        (queue_root / s).mkdir(parents=True, exist_ok=True)


def _job_path(queue_root: Path, state: str, job_id: str) -> Path:
    return queue_root / state / f"{job_id}.json"


def _read_job(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_job(path: Path, job: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(path)  # 原子落盘, 读者永远不见半截 JSON


def new_job_id() -> str:
    """时间排序 + 防撞: RJ-YYYYMMDD-HHMMSS<μs>-xxxx (文件名序 = FIFO 序)。
    微秒位保证同秒多次提交仍严格有序 (v1.1 测试暴露: 仅秒级前缀时同秒平局顺序随机)。"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
    return f"RJ-{ts}-{secrets.token_hex(2)}"


def _emit(kind: str, job: dict, **extra: Any) -> None:
    """给管理台遥测记一条 job 生命周期事件 (fail-open, 遥测挂了不影响队列)。"""
    try:
        import telemetry
        telemetry.record_event(kind, scene=job.get("scene_slug"), job_id=job.get("job_id"),
                               brand=job.get("brand_slug"), submitter=job.get("submitter"), **extra)
    except Exception:  # noqa: BLE001
        pass


def enqueue(payload: dict[str, Any], queue_root: Optional[Path] = None) -> str:
    """落 pending。payload 至少含: submitter / doc (path 或 url) / brand_slug。"""
    queue_root = _resolve_root(queue_root)
    _ensure_dirs(queue_root)
    job_id = new_job_id()
    job = {
        "job_id": job_id,
        "status": "pending",
        "submitted_at": _now_iso(),
        "retries": 0,
        **payload,
    }
    _write_job(_job_path(queue_root, "pending", job_id), job)
    _emit("job_enqueued", job, doc_name=payload.get("doc_name") or payload.get("doc"))
    return job_id


def claim_next(queue_root: Optional[Path] = None) -> Optional[dict]:
    """取最老 pending → running (原子 rename)。无任务返回 None。
    多进程安全: rename 失败 (被别人抢走) 就尝试下一个。"""
    queue_root = _resolve_root(queue_root)
    _ensure_dirs(queue_root)
    for path in sorted((queue_root / "pending").glob("RJ-*.json")):
        target = queue_root / "running" / path.name
        try:
            path.rename(target)
        except OSError:
            continue  # 已被其他 worker 抢走
        job = _read_job(target)
        if job is None:
            target.rename(queue_root / "failed" / path.name)  # 坏 JSON 直接隔离
            continue
        job["status"] = "running"
        job["claimed_at"] = _now_iso()
        _write_job(target, job)
        _emit("job_start", job)
        return job
    return None


def _transition(job_id: str, to_state: str, extra: dict,
                queue_root: Optional[Path] = None) -> dict:
    queue_root = _resolve_root(queue_root)
    src = _job_path(queue_root, "running", job_id)
    job = _read_job(src)
    if job is None:
        raise FileNotFoundError(f"running 中无此 job: {job_id}")
    job["status"] = to_state
    job["finished_at"] = _now_iso()
    job.update(extra)
    dst = _job_path(queue_root, to_state, job_id)
    _write_job(dst, job)
    src.unlink()
    return job


def complete(job_id: str, result: dict[str, Any],
             queue_root: Optional[Path] = None) -> dict:
    """running → done, 记产物路径等 result 字段。"""
    job = _transition(job_id, "done", {"result": result}, queue_root)
    _emit("job_done", job)
    return job


def fail(job_id: str, error: str, queue_root: Optional[Path] = None) -> dict:
    """running → failed, 保留 error 排障。"""
    job = _transition(job_id, "failed", {"error": error[:2000]}, queue_root)
    _emit("job_failed", job, error=(error or "")[:200])
    return job


def position(job_id: str, queue_root: Optional[Path] = None) -> Optional[int]:
    """pending 中的排队位置 (1-based); 不在 pending 返回 None。"""
    queue_root = _resolve_root(queue_root)
    pending = sorted(p.name for p in (queue_root / "pending").glob("RJ-*.json"))
    name = f"{job_id}.json"
    return pending.index(name) + 1 if name in pending else None


def get_job(job_id: str, queue_root: Optional[Path] = None) -> Optional[dict]:
    """跨四目录找 job (状态查询 endpoint 用)。"""
    queue_root = _resolve_root(queue_root)
    for state in STATES:
        p = _job_path(queue_root, state, job_id)
        if p.exists():
            job = _read_job(p)
            if job is not None:
                if state == "pending":
                    job["queue_position"] = position(job_id, queue_root)
                return job
    return None


def recover_stale(queue_root: Optional[Path] = None,
                  stale_minutes: int = STALE_RUNNING_MINUTES) -> list[str]:
    """worker 启动时调: 超龄 running (上次崩溃遗留) 退回 pending, retries+1。
    retries ≥ 2 的直接 fail (防 poison job 无限循环)。返回处理过的 job_id 列表。"""
    queue_root = _resolve_root(queue_root)
    _ensure_dirs(queue_root)
    recovered: list[str] = []
    now = datetime.now(timezone.utc)
    for path in sorted((queue_root / "running").glob("RJ-*.json")):
        job = _read_job(path)
        if job is None:
            path.rename(queue_root / "failed" / path.name)
            continue
        claimed = job.get("claimed_at")
        try:
            age_min = (now - datetime.fromisoformat(claimed)).total_seconds() / 60 if claimed else 1e9
        except ValueError:
            age_min = 1e9
        if age_min < stale_minutes:
            continue
        job_id = job["job_id"]
        if job.get("retries", 0) >= 2:
            job["status"] = "failed"
            job["error"] = f"重试 {job['retries']} 次后仍超龄 (poison job 隔离)"
            job["finished_at"] = _now_iso()
            _write_job(queue_root / "failed" / path.name, job)
            path.unlink()
        else:
            job["status"] = "pending"
            job["retries"] = job.get("retries", 0) + 1
            job.pop("claimed_at", None)
            _write_job(queue_root / "pending" / path.name, job)
            path.unlink()
        recovered.append(job_id)
    return recovered


def stats(queue_root: Optional[Path] = None) -> dict[str, int]:
    queue_root = _resolve_root(queue_root)
    _ensure_dirs(queue_root)
    return {s: len(list((queue_root / s).glob("RJ-*.json"))) for s in STATES}


def list_jobs(limit: int = 50, queue_root: Optional[Path] = None) -> list[dict]:
    """列出四个状态目录里的 job, 最新在前 (管理台任务列表用)。
    状态以所在目录为准 (覆盖 job 内可能过时的 status 字段)。"""
    queue_root = _resolve_root(queue_root)
    _ensure_dirs(queue_root)
    jobs: list[dict] = []
    for state in STATES:
        for p in (queue_root / state).glob("RJ-*.json"):
            job = _read_job(p)
            if job is not None:
                job["status"] = state
                jobs.append(job)
    # 最新在前: 优先 submitted_at, 退回 job_id (含 RJ-YYYYMMDD-HHMMSS 时间前缀)
    jobs.sort(key=lambda j: (j.get("submitted_at") or "", j.get("job_id") or ""), reverse=True)
    return jobs[: max(0, limit)]
