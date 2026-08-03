#!/usr/bin/env python3
"""
feishu_ws_client.py — 飞书长连接事件接收 (v1.2 PR3 · 多 bot 并发)

v1.2 新增多场景多 bot 支持:
  - scenes/ 下每个配置了 feishu 的 scene 对应一个飞书应用 (app_id/secret 读 env)
  - 每个 bot 维护一条长连接, **每 bot 一个子进程** (multiprocessing, 非线程)
  - 事件到达时按 scene 路由: feishu_events.handle(body, scene_slug=<name>)
  - 无 scene 配置 (或 env 未设) 时自动回退到 LARK_APP_ID/LARK_APP_SECRET 单 bot 模式

⚠ 为何用进程而非线程 (2026-06-29 双 bot 联调暴露):
  lark-oapi 的 ws.Client.start() 用的是**模块级全局 event loop** (非 per-client),
  loop.run_until_complete(_select()) 会把该全局 loop 永久跑起来。两个 client 在
  同一进程的不同线程里 start() 时, 第二个撞上"This event loop is already running"
  而崩溃 (只 1 个 bot 真连上)。每 bot 独立子进程 → 各有独立全局 loop → 互不干扰。

向后兼容: lark_event_to_body / on_message_receive / build_client 行为不变。

用法:
  python3 scripts/feishu_ws_client.py          # 常驻 (systemd)
依赖: lark-oapi · 飞书后台: 事件订阅选「使用长连接接收事件」+ im.message.receive_v1
"""

from __future__ import annotations

import concurrent.futures
import os
import sys
from pathlib import Path
from typing import Any, Optional

VAULT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(VAULT_ROOT / "scripts"))

import feishu_client
import feishu_events


# ─── 异步 fast-lane (M4 #126) — 回调不阻塞 SDK event loop ─────────
#
# lark-oapi 的事件回调是**同步**跑在该 bot 进程的模块级全局 event loop 上的。
# 若回调里直接同步跑 handle() (含 persona 评议 ~78s 的 LLM 调用), 会把 loop 心跳
# 饿死 → 飞书服务端主动关连接 → 掉线重连 (2026-07-03 上线日志现形)。
#
# 修法: 回调只做「读 SDK 对象 → 转 body dict」(快, 必须在回调线程), 然后把 handle()
# 丢进本进程的线程池, 回调立即返回。worker 线程内重设 thread-local bot 凭证 (send_card
# 用) 再跑 handle。每 bot 一个进程 → 一个池, 池内所有 job 同一个 app_id, 无跨 bot 串号。
# max_workers 有上限, 天然限并发 (灰度低流量足够; 同用户并发消息可能乱序, 可接受)。
_HANDLER_POOL_WORKERS = int(os.environ.get("FEISHU_WS_HANDLER_WORKERS", "4"))
_handler_pool: Any = None   # lazy (测试可注入 fake / inline)


def _get_handler_pool() -> Any:
    global _handler_pool
    if _handler_pool is None:
        _handler_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=_HANDLER_POOL_WORKERS, thread_name_prefix="ws-handle")
    return _handler_pool


def _submit_handle(job: Any) -> None:
    """把事件处理 job 丢后台线程池 → WS 回调秒回, 不阻塞 SDK loop。
    测试可 monkeypatch 本函数为 inline (`lambda job: job()`) 保持同步断言。"""
    _get_handler_pool().submit(job)


def _g(obj: Any, *names: str) -> Any:
    """逐层 getattr, 任一层 None 即返回 None。"""
    for n in names:
        if obj is None:
            return None
        obj = getattr(obj, n, None)
    return obj


def lark_event_to_body(data: Any) -> dict:
    """把 lark-oapi P2ImMessageReceiveV1 typed 事件适配成 webhook body dict。

    feishu_events.handle 读 header.event_type/event_id + event.sender.sender_id.open_id
    + event.sender.sender_type + event.message.{message_type,content,message_id,chat_id,chat_type,
    mentions}。mentions 用于群里判断是否 @机器人 (PR-O 群命令)。
    """
    return {
        "header": {
            "event_id": _g(data, "header", "event_id") or "",
            "event_type": _g(data, "header", "event_type") or "im.message.receive_v1",
            "token": _g(data, "header", "token") or "",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": _g(data, "event", "sender", "sender_id", "open_id") or ""},
                "sender_type": _g(data, "event", "sender", "sender_type") or "",
            },
            "message": {
                "message_id": _g(data, "event", "message", "message_id") or "",
                "message_type": _g(data, "event", "message", "message_type") or "",
                "content": _g(data, "event", "message", "content") or "{}",
                "chat_id": _g(data, "event", "message", "chat_id") or "",
                "chat_type": _g(data, "event", "message", "chat_type") or "",
                # 群里 @机器人 才处理命令 (PR-O); typed event 的 mentions 是对象列表, 带过去即可
                "mentions": _g(data, "event", "message", "mentions") or [],
                # 引用回复的父消息 id (群里「引用某文件 + @机器人」评议用, M4 follow-up)
                "parent_id": _g(data, "event", "message", "parent_id") or "",
            },
        },
    }


# ─── 单 bot 模式 (向后兼容 v1.1) ──────────────────────────────

def on_message_receive(data: Any) -> None:
    """单 bot 模式回调 — 适配事件 → 丢线程池跑 feishu_events.handle (与 webhook 同逻辑)。
    读 SDK 对象转 body 在回调线程 (快); handle 丢后台线程 → 回调秒回不阻塞 loop。
    worker 内不抛 (抛了会断连): 任何异常吞掉打日志。"""
    body = lark_event_to_body(data)

    def _job() -> None:
        try:
            result = feishu_events.handle(body)
            print(f"[feishu_ws] handled: {result}", flush=True)
        except Exception as e:
            print(f"[feishu_ws] ⚠ handle 异常 (吞掉防断连): {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)

    _submit_handle(_job)


def build_client():
    """单 bot: 从 LARK_APP_ID/SECRET env 构造 (v1.1 行为, 向后兼容)。"""
    app_id = os.environ.get("LARK_APP_ID", "")
    app_secret = os.environ.get("LARK_APP_SECRET", "")
    if not app_id or not app_secret:
        raise RuntimeError("LARK_APP_ID / LARK_APP_SECRET 未配置 — feishu_ws fail-close")
    return _build_ws_client(app_id, app_secret, on_message_receive)


# ─── 多 bot 支持 (v1.2 PR3) ───────────────────────────────────

def _build_ws_client(app_id: str, app_secret: str, callback: Any) -> Any:
    """构造一个 lark ws.Client (延迟 import lark_oapi 便于无依赖单测)。"""
    import lark_oapi as lark
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(callback)
        .build()
    )
    return lark.ws.Client(app_id, app_secret, event_handler=handler,
                          log_level=lark.LogLevel.INFO)


def _make_scene_callback(scene_name: str, app_id: str, app_secret: str):
    """创建绑定到特定 scene 的事件回调。
    读 SDK 对象转 body 在回调线程 (快), handle 丢线程池 → 回调秒回不阻塞 loop。
    worker 线程内**重设** thread-local bot 凭证 (feishu_client.download/send_card 用) —
    凭证是 thread-local, 主回调线程设的不会传到池线程, 必须在 job 内重设。"""
    def _callback(data: Any) -> None:
        body = lark_event_to_body(data)

        def _job() -> None:
            try:
                feishu_client.set_bot_creds(app_id, app_secret)
                result = feishu_events.handle(body, scene_slug=scene_name)
                print(f"[feishu_ws:{scene_name}] handled: {result}", flush=True)
            except Exception as e:
                print(f"[feishu_ws:{scene_name}] ⚠ 异常 (吞掉防断连): {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)

        _submit_handle(_job)
    return _callback


def collect_bot_specs() -> list[tuple[str, str, str]]:
    """读取所有 scenes, 返回 (scene_name, app_id, app_secret) — 仅纳入 env 已配置的 scene。"""
    try:
        import scene_loader as sl
        scenes = sl.list_scenes()
    except Exception:
        return []
    specs: list[tuple[str, str, str]] = []
    for scene in scenes:
        if not (scene.feishu and scene.feishu.configured):
            continue
        app_id = os.environ.get(scene.feishu.app_id_env, "")
        app_secret = os.environ.get(scene.feishu.app_secret_env, "")
        if app_id and app_secret:
            specs.append((scene.name, app_id, app_secret))
        else:
            print(f"[feishu_ws] skip scene {scene.name!r}: "
                  f"env {scene.feishu.app_id_env}/{scene.feishu.app_secret_env} unset",
                  flush=True)
    return specs


def build_clients() -> list[tuple[Any, str]]:
    """构造所有 bot clients。
    有 scene + env 配置时: 每个 scene 一个 client (多 bot)。
    否则: 回退到单 bot (LARK_APP_ID/SECRET env)。
    返回 [(client, label), ...]。"""
    specs = collect_bot_specs()
    if specs:
        clients: list[tuple[Any, str]] = []
        for name, app_id, app_secret in specs:
            try:
                cb = _make_scene_callback(name, app_id, app_secret)
                cli = _build_ws_client(app_id, app_secret, cb)
                clients.append((cli, name))
            except Exception as e:
                print(f"[feishu_ws] ⚠ build client {name!r} 失败: {e}", file=sys.stderr)
        if clients:
            return clients
    # fallback: 单 bot
    try:
        return [(build_client(), "default")]
    except RuntimeError:
        return []


HEARTBEAT_INTERVAL_SEC = 60


def _scene_bot_name(scene_name: str) -> Optional[str]:
    """best-effort 取 scene 的飞书显示名 (心跳带上, 管理台好看)。取不到返回 None。"""
    try:
        import scene_loader as sl
        s = sl.load_scene(scene_name)
        return s.feishu.bot_name if s.feishu else None
    except Exception:  # noqa: BLE001
        return None


def _heartbeat_once(scene_name: str, bot_name: Optional[str] = None) -> None:
    """打一次心跳 → 管理台遥测 (M0, PRD §6)。fail-open: 遥测挂了不影响长连接。"""
    try:
        import telemetry
        telemetry.heartbeat(scene_name, bot=bot_name, status="online")
    except Exception:  # noqa: BLE001
        pass


def _start_heartbeat(scene_name: str, bot_name: Optional[str] = None,
                     interval: int = HEARTBEAT_INTERVAL_SEC) -> None:
    """后台 daemon 线程周期打心跳。子进程内起, 随进程退出而止。"""
    import threading
    import time as _t

    def _beat() -> None:
        while True:
            _heartbeat_once(scene_name, bot_name)
            _t.sleep(interval)

    threading.Thread(target=_beat, daemon=True, name=f"hb-{scene_name}").start()


def _run_bot(client: Any, label: str) -> None:
    """启动一个已构造的 bot 长连接 (阻塞, 内部自动重连)。单 bot 回退路径用。"""
    _start_heartbeat(label, _scene_bot_name(label))
    print(f"[feishu_ws:{label}] 长连接启动 — server 主动连飞书, 无需公网入站", flush=True)
    client.start()


def _run_bot_process(name: str, app_id: str, app_secret: str) -> None:
    """子进程入口: 在独立进程内构造并启动一个 bot 长连接。

    在子进程内才构造 client + callback, 这样每个进程拥有独立的 lark 模块级全局
    event loop (见文件头说明), 规避多 client 同进程的 loop 冲突。阻塞直到断开。
    """
    cb = _make_scene_callback(name, app_id, app_secret)
    cli = _build_ws_client(app_id, app_secret, cb)
    _start_heartbeat(name, _scene_bot_name(name))   # 后台心跳 → 管理台判在线 (M0)
    print(f"[feishu_ws:{name}] 长连接启动 (pid={os.getpid()}) — "
          f"server 主动连飞书, 无需公网入站", flush=True)
    cli.start()


# 多 bot 进程工厂 seam (测试可注入同步 fake; None → multiprocessing.Process)
_PROCESS_FACTORY: Any = None


def main() -> int:
    specs = collect_bot_specs()

    # ── fallback: 无 scene 配置 → 单 bot (LARK_APP_ID/SECRET) ──
    if not specs:
        try:
            cli = build_client()
        except RuntimeError:
            print("[feishu_ws] 无 bot 配置 (LARK_APP_ID/SECRET 未设) — fail-close",
                  file=sys.stderr)
            return 3
        print("[feishu_ws] 启动 1 个 bot 长连接: ['default']", flush=True)
        _run_bot(cli, "default")   # 阻塞直到断开
        return 0

    labels = [name for name, _, _ in specs]
    print(f"[feishu_ws] 启动 {len(specs)} 个 bot 长连接: {labels}", flush=True)

    # ── 单 bot: 主进程内直跑 (无多进程开销, 与旧行为一致) ──
    if len(specs) == 1:
        name, app_id, app_secret = specs[0]
        _run_bot_process(name, app_id, app_secret)   # 阻塞直到断开
        return 0

    # ── 多 bot: 每 bot 一个子进程 (独立 event loop) ──
    import multiprocessing as mp
    import time
    factory = _PROCESS_FACTORY or mp.Process
    procs = []
    for name, app_id, app_secret in specs:
        p = factory(target=_run_bot_process, args=(name, app_id, app_secret),
                    name=f"ws-{name}")
        p.daemon = False
        p.start()
        procs.append(p)

    # 长连接子进程正常情况下永不退出; 任一退出即视为异常 → 终止其余, 返回非 0
    # 让 systemd (Restart=always) 整体重启所有 bot, 保证一致状态。
    try:
        while all(p.is_alive() for p in procs):
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    dead = [p.name for p in procs if not p.is_alive()]
    for p in procs:
        if p.is_alive():
            p.terminate()
    print(f"[feishu_ws] ⚠ bot 子进程退出 {dead} — 整体退出让 systemd 重启",
          file=sys.stderr, flush=True)
    return 4


if __name__ == "__main__":
    sys.exit(main())
