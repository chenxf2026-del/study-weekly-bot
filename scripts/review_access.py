#!/usr/bin/env python3
"""
review_access.py — review-service 白名单 + 配额 (v1.1 A2.4 · PRD §6 决策点 2/3)

fail-close: config/review_service.yaml (真实, gitignored) 不存在 → 拒绝所有提交。
配额用文件队列本身计数 (扫四目录当日同 submitter 的 job), 无额外状态。

供 boss_server (A2.3) 与飞书事件处理 (A3) 共用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

VAULT_ROOT = Path(__file__).parent.parent.resolve()
CONFIG_FILE = VAULT_ROOT / "config" / "review_service.yaml"

import review_queue


@dataclass
class AccessResult:
    allowed: bool
    http_status: int       # 200 / 403 / 429 / 503
    reason: str


def load_config(config_file: Path = CONFIG_FILE) -> Optional[dict]:
    """读真实 config; 不存在/解析失败 → None (调用方 fail-close)。.example 不作降级。"""
    if not config_file.is_file():
        return None
    try:
        import yaml
        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _jobs_today_by(submitter: str, queue_root: Path) -> int:
    """扫四目录数当日该 submitter 的 job (submitted_at UTC 同日)。"""
    today = datetime.now(timezone.utc).date().isoformat()
    n = 0
    for state in review_queue.STATES:
        d = queue_root / state
        if not d.is_dir():
            continue
        for f in d.glob("RJ-*.json"):
            job = review_queue._read_job(f)
            if (job and job.get("submitter") == submitter
                    and str(job.get("submitted_at", "")).startswith(today)):
                n += 1
    return n


def _jobs_today_in_group(group_id: str, queue_root: Path) -> int:
    """扫四目录数当日回推到该群 (notify_id_type=chat_id 且 notify_to=group_id) 的 job。
    群放开后按群限总量, 防单群被刷爆 (per-user 配额之上再加一层群护栏)。"""
    today = datetime.now(timezone.utc).date().isoformat()
    n = 0
    for state in review_queue.STATES:
        d = queue_root / state
        if not d.is_dir():
            continue
        for f in d.glob("RJ-*.json"):
            job = review_queue._read_job(f)
            if (job and job.get("notify_id_type") == "chat_id"
                    and job.get("notify_to") == group_id
                    and str(job.get("submitted_at", "")).startswith(today)):
                n += 1
    return n


def check_access(submitter: str,
                 queue_root: Optional[Path] = None,
                 config: Optional[dict] = None,
                 config_file: Path = CONFIG_FILE,
                 bypass_whitelist: bool = False,
                 group_id: Optional[str] = None,
                 group_daily_cap: Optional[int] = None) -> AccessResult:
    """提交前闸: 503 (无 config, fail-close) / 403 (不在白名单) / 429 (超配额/超群限) / 200。

    bypass_whitelist: 跳过白名单 (群里放开时用, 见 feishu_events._review_group_open);
    **配额仍然生效** —— 放开的是"谁能用", 不是"无限用", 每人每日仍受 quota 限制防刷。
    group_id + group_daily_cap: 群放开时再加一层"按群日上限"护栏, 防单群被刷爆 (一群多人时,
    per-user 配额挡不住整群刷量)。仅当两者都给才生效。
    """
    queue_root = queue_root if queue_root is not None else review_queue.DEFAULT_QUEUE_ROOT
    cfg = config if config is not None else load_config(config_file)
    if cfg is None:
        return AccessResult(False, 503,
                            "review-service 未配置 (config/review_service.yaml 缺失) — fail-close 拒绝提交")
    whitelist = cfg.get("whitelist") or []
    if not bypass_whitelist and submitter not in whitelist:
        return AccessResult(False, 403,
                            "您不在 review-service 灰度名单中 — 如需使用请联系项目主理")
    # 群刷量护栏 (群放开时): 单群当日总量超上限 → 拦
    if group_id and group_daily_cap:
        gused = _jobs_today_in_group(group_id, queue_root)
        if gused >= int(group_daily_cap):
            return AccessResult(False, 429,
                                f"本群今日评审已达上限 ({gused}/{group_daily_cap}) — 明天再试或联系项目主理调整")
    daily = int((cfg.get("quota") or {}).get("per_user_daily", 2))
    used = _jobs_today_by(submitter, queue_root)
    if used >= daily:
        return AccessResult(False, 429,
                            f"今日配额已用完 ({used}/{daily}) — 明天再试或联系项目主理调整")
    return AccessResult(True, 200, f"ok (今日 {used}/{daily})")
