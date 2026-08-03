#!/usr/bin/env python3
"""
feishu_client.py — 飞书开放平台 API 薄封装 (v1.1 A3 · review-service 用)

只封装 review-service 需要的四个动作: tenant token / 下载消息文件 / 发卡片 / 发文件。
fail-close: LARK_APP_ID / LARK_APP_SECRET 未配置 → FeishuNotConfigured (调用方决定降级)。
网络重试与 _fetchers 同纪律 (传输层/429/5xx 三次指数退避)。

测试全部 mock 本模块函数 — 不真出网。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

BASE = "https://open.feishu.cn/open-apis"
TIMEOUT = 15.0


class FeishuNotConfigured(RuntimeError):
    """LARK_APP_ID/SECRET 缺失 — 调用方按 fail-close 处理。"""


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


_retry = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=8),
               retry=retry_if_exception(_is_retryable), reraise=True)

# 多 bot 支持 (PR3): per-thread 凭证 (feishu_ws_client 每 bot 线程设一次) + per-app_id token 缓存
_local = threading.local()
_token_cache: dict[str, dict[str, Any]] = {}   # keyed by app_id
_token_lock = threading.Lock()


def set_bot_creds(app_id: str, secret: str) -> None:
    """为当前线程设置 bot 凭证 (feishu_ws_client 在每个 bot 线程内调用)。"""
    _local.app_id = app_id
    _local.secret = secret


def _creds() -> tuple[str, str]:
    """读取凭证: 优先 thread-local (多 bot 模式), 回退 env (单 bot / 向后兼容)。"""
    app_id = getattr(_local, "app_id", None) or os.environ.get("LARK_APP_ID", "")
    secret = getattr(_local, "secret", None) or os.environ.get("LARK_APP_SECRET", "")
    if not app_id or not secret:
        raise FeishuNotConfigured("LARK_APP_ID / LARK_APP_SECRET 未配置")
    return app_id, secret


@_retry
def get_tenant_token() -> str:
    app_id, secret = _creds()
    with _token_lock:
        entry = _token_cache.get(app_id, {})
        if entry.get("token") and time.time() < entry.get("expire_at", 0.0):
            return entry["token"]
    resp = httpx.post(f"{BASE}/auth/v3/tenant_access_token/internal",
                      json={"app_id": app_id, "app_secret": secret}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"tenant_access_token 失败: {data.get('code')} {data.get('msg')}")
    token = data["tenant_access_token"]
    expire_at = time.time() + int(data.get("expire", 7200)) - 300
    with _token_lock:
        _token_cache[app_id] = {"token": token, "expire_at": expire_at}
    return token


def _auth_headers(access_token: str | None = None) -> dict[str, str]:
    """鉴权头。默认用 bot tenant_access_token; 传 access_token 时用它 (user_access_token
    走群历史/私有文档回填 — tenant 读不到进群前群历史与成员私有文档, 见 pull_chat_backfill)。"""
    return {"Authorization": f"Bearer {access_token or get_tenant_token()}"}


@_retry
def list_chats(*, access_token: str | None = None, page_size: int = 100,
               max_pages: int = 50) -> list[dict[str, Any]]:
    """列出当前身份 (bot 或 user) 所在的群, 自动翻页。返回 [{chat_id, name, ...}]。
    飞书 GET /im/v1/chats。用于回填前查群 chat_id (bot 进群后即在列表里)。"""
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"page_size": max(1, min(page_size, 100))}
        if page_token:
            params["page_token"] = page_token
        resp = httpx.get(f"{BASE}/im/v1/chats", params=params,
                         headers=_auth_headers(access_token), timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"list_chats 失败: {data.get('code')} {data.get('msg')}")
        d = data.get("data") or {}
        items.extend(d.get("items") or [])
        if not d.get("has_more"):
            break
        page_token = d.get("page_token")
        if not page_token:
            break
    return items


@_retry
def list_chat_messages(
    chat_id: str, *, access_token: str | None = None, page_size: int = 50,
    start_time: int | None = None, end_time: int | None = None, max_pages: int = 200,
) -> list[dict[str, Any]]:
    """列一个群 (container=chat) 的消息, 自动翻页 (按创建时间升序)。

    飞书 GET /im/v1/messages?container_id_type=chat&container_id=<chat_id>。
    ⚠ tenant token 读不到「机器人进群前」的历史 + 成员私有文档; 群历史回填须传
    user_access_token (调用方在群内, 见 scripts/pull_chat_backfill.py)。
    start_time/end_time: unix 秒 (含); max_pages: 翻页硬上限 (防失控)。
    返回 message dict 列表 (每条含 message_id / msg_type / body.content(JSON 串) / create_time / sender)。"""
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    for _ in range(max_pages):
        params: dict[str, Any] = {
            "container_id_type": "chat", "container_id": chat_id,
            "sort_type": "ByCreateTimeAsc", "page_size": max(1, min(page_size, 50)),
        }
        if start_time is not None:
            params["start_time"] = str(start_time)
        if end_time is not None:
            params["end_time"] = str(end_time)
        if page_token:
            params["page_token"] = page_token
        resp = httpx.get(f"{BASE}/im/v1/messages", params=params,
                         headers=_auth_headers(access_token), timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"list_chat_messages 失败: {data.get('code')} {data.get('msg')}")
        d = data.get("data") or {}
        items.extend(d.get("items") or [])
        if not d.get("has_more"):
            break
        page_token = d.get("page_token")
        if not page_token:
            break
    return items


@_retry
def download_message_file(message_id: str, file_key: str, dest: Path) -> Path:
    """下载消息附件 (用户发给机器人的文档) 到 dest。"""
    url = f"{BASE}/im/v1/messages/{message_id}/resources/{file_key}"
    with httpx.stream("GET", url, params={"type": "file"},
                      headers=_auth_headers(), timeout=60.0) as resp:
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)
    return dest


@_retry
def get_message(message_id: str) -> dict[str, Any]:
    """取一条消息 (用于群里「引用回复某文件」时, 解析被引用的父消息拿 file_key)。

    返回规整后的 dict: {message_id, message_type, content(JSON 字符串)} — 与长连接
    事件的 message 字段对齐, 便于复用下游文件解析。取不到返回 {}。
    """
    url = f"{BASE}/im/v1/messages/{message_id}"
    resp = httpx.get(url, headers=_auth_headers(), timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"get_message 失败: {data.get('code')} {data.get('msg')}")
    items = (data.get("data") or {}).get("items") or []
    if not items:
        return {}
    it = items[0]
    body = it.get("body") or {}
    return {
        "message_id": it.get("message_id", message_id),
        "message_type": it.get("msg_type", ""),
        "content": body.get("content", "{}"),
    }


# open_id → 姓名 / chat_id → 群名 的进程内缓存 (keyed by id; 值 = 名字 or None 负缓存)。
# 缓存跨调用复用, 避免同一 id 每次归档都打通讯录 API。多 bot 各线程共用 (名字与 app 无关)。
_name_cache: dict[str, Optional[str]] = {}
_name_lock = threading.Lock()


def get_user_name(open_id: str) -> Optional[str]:
    """open_id → 姓名 (飞书通讯录)。best-effort **fail-open**: 缺 scope / 非同租户 / 任何错误 → None。
    进程内缓存 (含负缓存, 拿不到也记 None 不再重试, 防每轮归档反复打 API)。"""
    oid = (open_id or "").strip()
    if not oid or not oid.startswith(("ou_", "on_")):
        return None
    with _name_lock:
        if oid in _name_cache:
            return _name_cache[oid]
    name: Optional[str] = None
    try:
        resp = httpx.get(f"{BASE}/contact/v3/users/{oid}",
                         params={"user_id_type": "open_id"},
                         headers=_auth_headers(), timeout=TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 0:
                name = ((data.get("data") or {}).get("user") or {}).get("name") or None
    except Exception:  # noqa: BLE001 — 名字解析绝不炸调用方 (归档 best-effort)
        name = None
    with _name_lock:
        _name_cache[oid] = name
    return name


def get_chat_name(chat_id: str) -> Optional[str]:
    """chat_id → 群名 (飞书群信息)。best-effort **fail-open**: 缺 scope / bot 非群成员 / 任何错误 → None。
    进程内缓存 (含负缓存)。群名可能变, 但归档每轮刷新取最新, 管理台读末轮即可。"""
    cid = (chat_id or "").strip()
    if not cid or not cid.startswith("oc_"):
        return None
    with _name_lock:
        if cid in _name_cache:
            return _name_cache[cid]
    name: Optional[str] = None
    try:
        resp = httpx.get(f"{BASE}/im/v1/chats/{cid}",
                         headers=_auth_headers(), timeout=TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 0:
                name = (data.get("data") or {}).get("name") or None
    except Exception:  # noqa: BLE001
        name = None
    with _name_lock:
        _name_cache[cid] = name
    return name


@_retry
def send_card(receive_id: str, card: dict[str, Any], receive_id_type: str = "open_id") -> Optional[str]:
    """发互动卡片。receive_id_type=open_id 发给个人 (1:1), =chat_id 发到群 (A4 群评审)。
    返回飞书 **message_id** (供后续 update_card 逐步更新; 取不到返回 None)。旧调用方忽略返回值不受影响。

    群投递的脱敏纪律由调用方 (feishu_notify) 决定 — 群 = 非单一收件人, 强制过出站闸。"""
    resp = httpx.post(
        f"{BASE}/im/v1/messages", params={"receive_id_type": receive_id_type},
        headers=_auth_headers(),
        json={"receive_id": receive_id, "msg_type": "interactive",
              "content": json.dumps(card, ensure_ascii=False)},
        timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"send_card 失败: {data.get('code')} {data.get('msg')}")
    return (data.get("data") or {}).get("message_id")


def update_card(message_id: str, card: dict[str, Any]) -> None:
    """原地更新一张已发的互动卡片 (供流式逐步渲染)。飞书: PATCH /im/v1/messages/{message_id}。
    只能更新**本 bot 自己发的**互动卡片。失败抛 RuntimeError (调用方 best-effort catch)。"""
    resp = httpx.patch(
        f"{BASE}/im/v1/messages/{message_id}",
        headers=_auth_headers(),
        json={"content": json.dumps(card, ensure_ascii=False)},
        timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"update_card 失败: {data.get('code')} {data.get('msg')}")


@_retry
def send_file(receive_id: str, path: Path, receive_id_type: str = "open_id",
              display_name: Optional[str] = None) -> None:
    """上传并发送文件 (评审完整报告)。receive_id_type=open_id 个人 / =chat_id 群。

    display_name: 飞书里展示的文件名 (省略=用磁盘名 path.name)。用于把每组报告的通用
    磁盘名 report.pdf 换成按提交文档标题派生的可区分名 (调用方须先过 redact 文件名闸)。"""
    name = display_name or path.name
    with path.open("rb") as f:
        up = httpx.post(f"{BASE}/im/v1/files", headers=_auth_headers(),
                        data={"file_type": "stream", "file_name": name},
                        files={"file": (name, f)}, timeout=60.0)
    up.raise_for_status()
    data = up.json()
    if data.get("code") != 0:
        raise RuntimeError(f"upload 失败: {data.get('code')} {data.get('msg')}")
    file_key = data["data"]["file_key"]
    resp = httpx.post(
        f"{BASE}/im/v1/messages", params={"receive_id_type": receive_id_type},
        headers=_auth_headers(),
        json={"receive_id": receive_id, "msg_type": "file",
              "content": json.dumps({"file_key": file_key})},
        timeout=TIMEOUT)
    resp.raise_for_status()
    if resp.json().get("code") != 0:
        raise RuntimeError(f"send_file 失败: {resp.json().get('msg')}")
