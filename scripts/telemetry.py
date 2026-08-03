#!/usr/bin/env python3
"""telemetry.py — 运维管理台遥测底座 (PRD docs/internal/prd-admin-dashboard.md §6, M0)

轻量 SQLite 事件存储, 给管理台当数据地基。ws / worker / pipeline 在关键点 record 一行,
管理台聚合读。**fail-open**: 写入永不抛 (遥测挂了绝不影响飞书回复 / 评审主流程)。
**只写元数据/指标, 不写消息正文/报告正文** (隐私: confidential 正文不进遥测库)。

两张表:
  events(ts, kind, scene, job_id, bot, payload) — 生命周期/活动事件
  llm_usage(ts, scene, job_id, phase, model, tokens_in, tokens_out, cost_usd) — LLM 用量
  heartbeats(scene, bot, ts, status)            — bot 在线心跳 (upsert, 判在线看新鲜度)

kind ∈ {bot_connect, bot_heartbeat, msg_received, job_enqueued, job_start, phase_start,
        phase_done, judge_scored, judge_failed, llm_call, job_done, job_failed,
        model_switch, persona_reply, group_toggle}

用法:
  python3 scripts/telemetry.py summary                 # 事件/用量总览
  python3 scripts/telemetry.py usage --since-days 7     # 近 7 天用量 (按场景)
  python3 scripts/telemetry.py events --limit 30        # 最近事件
  BOSS_TELEMETRY_DB=/path/boss.db 覆盖库位置 (测试用)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

VAULT_ROOT = Path(__file__).parent.parent.resolve()
DB_FILE = VAULT_ROOT / "cases" / ".telemetry" / "boss.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        REAL    NOT NULL,
  kind      TEXT    NOT NULL,
  scene     TEXT,
  job_id    TEXT,
  bot       TEXT,
  payload   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts    ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind  ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_job   ON events(job_id);
CREATE INDEX IF NOT EXISTS idx_events_scene ON events(scene);

CREATE TABLE IF NOT EXISTS llm_usage (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         REAL    NOT NULL,
  scene      TEXT,
  job_id     TEXT,
  phase      TEXT,
  model      TEXT,
  tokens_in  INTEGER DEFAULT 0,
  tokens_out INTEGER DEFAULT 0,
  cost_usd   REAL    DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts    ON llm_usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_scene ON llm_usage(scene);

CREATE TABLE IF NOT EXISTS heartbeats (
  scene   TEXT PRIMARY KEY,
  bot     TEXT,
  ts      REAL NOT NULL,
  status  TEXT
);
"""

# 心跳超过这么久没更新 = 判离线 (管理台用)
HEARTBEAT_STALE_SEC = 180


def _db_path(path: Optional[Path] = None) -> Path:
    if path:
        return Path(path)
    return Path(os.environ.get("BOSS_TELEMETRY_DB") or DB_FILE)


@contextmanager
def _connect(path: Optional[Path] = None, *, write: bool = False) -> Iterator[sqlite3.Connection]:
    """短生命周期连接。WAL + busy_timeout 让多进程 (ws 子进程 / worker) 并发读 + 单写不打架。"""
    p = _db_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        yield conn
        if write:
            conn.commit()
    finally:
        conn.close()


# ─────────────────────────── 写入 (fail-open) ───────────────────────────

def record_event(kind: str, *, scene: Optional[str] = None, job_id: Optional[str] = None,
                 bot: Optional[str] = None, path: Optional[Path] = None, **payload: Any) -> None:
    """记一条事件。fail-open: 任何异常 (含 DB 锁) 吞掉, 绝不影响主流程。
    payload 只放元数据/指标, **不要放消息正文/报告正文** (隐私)。"""
    try:
        with _connect(path, write=True) as conn:
            conn.execute(
                "INSERT INTO events(ts, kind, scene, job_id, bot, payload) VALUES(?,?,?,?,?,?)",
                (round(time.time(), 3), kind, scene, job_id, bot,
                 json.dumps(payload, ensure_ascii=False) if payload else None))
    except Exception:  # noqa: BLE001 — 遥测 fail-open
        pass


def record_llm_usage(*, scene: Optional[str], job_id: Optional[str], phase: Optional[str],
                     model: Optional[str], tokens_in: int = 0, tokens_out: int = 0,
                     cost_usd: float = 0.0, path: Optional[Path] = None) -> None:
    """记一次 LLM 调用用量。fail-open。"""
    try:
        with _connect(path, write=True) as conn:
            conn.execute(
                "INSERT INTO llm_usage(ts, scene, job_id, phase, model, tokens_in, tokens_out, cost_usd) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (round(time.time(), 3), scene, job_id, phase, model,
                 int(tokens_in or 0), int(tokens_out or 0), float(cost_usd or 0.0)))
    except Exception:  # noqa: BLE001
        pass


def heartbeat(scene: str, *, bot: Optional[str] = None, status: str = "online",
              path: Optional[Path] = None) -> None:
    """bot 心跳 (upsert)。ws 子进程周期调用; 管理台看 ts 新鲜度判在线。fail-open。"""
    try:
        with _connect(path, write=True) as conn:
            conn.execute(
                "INSERT INTO heartbeats(scene, bot, ts, status) VALUES(?,?,?,?) "
                "ON CONFLICT(scene) DO UPDATE SET bot=excluded.bot, ts=excluded.ts, status=excluded.status",
                (scene, bot, round(time.time(), 3), status))
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────── 读取 (管理台用) ───────────────────────────

def recent_events(limit: int = 50, *, kind: Optional[str] = None, scene: Optional[str] = None,
                  since_ts: Optional[float] = None, path: Optional[Path] = None) -> list[dict]:
    """最近事件 (倒序)。可按 kind/scene/时间过滤。payload 解析回 dict。"""
    where, params = [], []
    if kind:
        where.append("kind = ?"); params.append(kind)
    if scene:
        where.append("scene = ?"); params.append(scene)
    if since_ts is not None:
        where.append("ts >= ?"); params.append(since_ts)
    sql = "SELECT * FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(int(limit))
    try:
        with _connect(path) as conn:
            return [_row_event(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:  # noqa: BLE001
        return []


def job_events(job_id: str, path: Optional[Path] = None) -> list[dict]:
    """某 job 的事件时间线 (正序), 供任务详情页画 Phase 进度。"""
    try:
        with _connect(path) as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE job_id = ? ORDER BY ts ASC, id ASC", (job_id,)).fetchall()
            return [_row_event(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def usage_summary(*, group_by: str = "scene", since_ts: Optional[float] = None,
                  path: Optional[Path] = None) -> list[dict]:
    """LLM 用量聚合。group_by ∈ {scene, model, job_id, phase, day}。返回按 cost 降序。"""
    col = {"scene": "scene", "model": "model", "job_id": "job_id", "phase": "phase",
           "day": "date(ts, 'unixepoch', 'localtime')"}.get(group_by, "scene")
    where, params = [], []
    if since_ts is not None:
        where.append("ts >= ?"); params.append(since_ts)
    sql = (f"SELECT {col} AS grp, COUNT(*) AS calls, "
           "SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out, SUM(cost_usd) AS cost_usd "
           "FROM llm_usage")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY grp ORDER BY cost_usd DESC"
    try:
        with _connect(path) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:  # noqa: BLE001
        return []


def usage_by_job(job_id: str, *, path: Optional[Path] = None) -> list[dict]:
    """某 job 的 LLM 用量按 phase 拆 (任务详情页用)。按 cost 降序。"""
    sql = ("SELECT phase AS grp, COUNT(*) AS calls, "
           "SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out, SUM(cost_usd) AS cost_usd "
           "FROM llm_usage WHERE job_id = ? GROUP BY phase ORDER BY cost_usd DESC")
    try:
        with _connect(path) as conn:
            return [dict(r) for r in conn.execute(sql, (job_id,)).fetchall()]
    except Exception:  # noqa: BLE001
        return []


def usage_total(*, since_ts: Optional[float] = None, path: Optional[Path] = None) -> dict:
    """总用量 (卡片用)。"""
    where = "WHERE ts >= ?" if since_ts is not None else ""
    params = [since_ts] if since_ts is not None else []
    try:
        with _connect(path) as conn:
            r = conn.execute(
                f"SELECT COUNT(*) AS calls, COALESCE(SUM(tokens_in),0) AS tokens_in, "
                f"COALESCE(SUM(tokens_out),0) AS tokens_out, COALESCE(SUM(cost_usd),0) AS cost_usd "
                f"FROM llm_usage {where}", params).fetchone()
            return dict(r) if r else {}
    except Exception:  # noqa: BLE001
        return {}


def event_kind_counts(*, since_ts: Optional[float] = None, path: Optional[Path] = None) -> dict[str, int]:
    """按 kind 计数 (总览用)。"""
    where = "WHERE ts >= ?" if since_ts is not None else ""
    params = [since_ts] if since_ts is not None else []
    try:
        with _connect(path) as conn:
            rows = conn.execute(
                f"SELECT kind, COUNT(*) AS n FROM events {where} GROUP BY kind", params).fetchall()
            return {r["kind"]: r["n"] for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def heartbeats(*, stale_sec: int = HEARTBEAT_STALE_SEC, path: Optional[Path] = None) -> list[dict]:
    """所有 bot 心跳 + 在线判定 (now - ts < stale_sec → online)。"""
    now = time.time()
    try:
        with _connect(path) as conn:
            rows = conn.execute("SELECT * FROM heartbeats ORDER BY scene").fetchall()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        age = now - float(r["ts"])
        out.append({"scene": r["scene"], "bot": r["bot"], "ts": r["ts"],
                    "age_sec": round(age, 1), "online": age < stale_sec,
                    "status": r["status"]})
    return out


def _row_event(r: sqlite3.Row) -> dict:
    d = dict(r)
    if d.get("payload"):
        try:
            d["payload"] = json.loads(d["payload"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


# ─────────────────────────── CLI ───────────────────────────

def _since(days: Optional[float]) -> Optional[float]:
    return time.time() - days * 86400 if days else None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="遥测底座查询 (管理台数据源)")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("summary", "usage", "events", "heartbeats"):
        sp = sub.add_parser(name)
        sp.add_argument("--since-days", type=float, default=None)
        sp.add_argument("--limit", type=int, default=30)
        sp.add_argument("--group-by", default="scene")
    args = ap.parse_args(argv)
    since = _since(getattr(args, "since_days", None))

    if args.cmd == "usage":
        rows = usage_summary(group_by=args.group_by, since_ts=since)
        tot = usage_total(since_ts=since)
        print(f"总: {tot.get('calls',0)} 调用 · in {tot.get('tokens_in',0)} + out "
              f"{tot.get('tokens_out',0)} tok · ${tot.get('cost_usd',0):.4f}")
        for r in rows:
            print(f"  {str(r['grp']):<28} {r['calls']:>4} 调用 · "
                  f"{r['tokens_in']}+{r['tokens_out']} tok · ${r['cost_usd']:.4f}")
    elif args.cmd == "events":
        for e in recent_events(limit=args.limit, since_ts=since):
            print(f"  {e['ts']:.0f} {e['kind']:<14} scene={e.get('scene')} job={e.get('job_id')} "
                  f"{e.get('payload') or ''}")
    elif args.cmd == "heartbeats":
        for h in heartbeats():
            print(f"  {h['scene']:<24} {'🟢online' if h['online'] else '🔴offline'} "
                  f"(age {h['age_sec']}s)")
    else:  # summary
        since7 = _since(7)
        print("▸ 事件计数 (近 7 天)")
        for k, n in sorted(event_kind_counts(since_ts=since7).items()):
            print(f"    {k:<16} {n}")
        tot = usage_total(since_ts=since7)
        print(f"\n▸ LLM 用量 (近 7 天): {tot.get('calls',0)} 调用 · "
              f"{tot.get('tokens_in',0)}+{tot.get('tokens_out',0)} tok · ${tot.get('cost_usd',0):.4f}")
        print("\n▸ bot 心跳")
        for h in heartbeats():
            print(f"    {h['scene']:<24} {'🟢' if h['online'] else '🔴'} (age {h['age_sec']}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
