#!/usr/bin/env python3
"""
feishu_admin.py — 飞书机器人的管理命令 (admin-only): 文字命令切 LLM 模型。

为什么单独成模块: 让命令解析 / 执行与 feishu_events 路由解耦, 纯逻辑可单测 (不依赖飞书 SDK)。
切模型影响**全员评审**用的模型, 因此严格限 BOSS_ADMIN_WHITELIST 里的 open_id。
切完只改 .env (复用 llm_switch.apply_profile); worker 每单作业前热重载 .env, 不用重启。

支持的命令 (单聊发给机器人):
  /model              当前配置 + 可切的网关
  /model <网关>        切到该网关 (默认模型) — 网关名见 /model
  /model <网关> <模型> 切到该网关 + 指定模型
  /model <模型>        在当前网关上换模型 (模型名见 /models)
  /models [网关]       列该网关 (默认当前) 上的可用模型
  /help                帮助
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

VAULT_ROOT = Path(__file__).parent.parent.resolve()

# admin 白名单 env (逗号分隔 open_id); 空 = 无人有权 (fail-close)
ADMIN_ENV = "BOSS_ADMIN_WHITELIST"

# parse 出来的命令: (verb, args)。verb ∈ {model, models, mode, help}
# mode = persona bot 人格模式 (上游分身场景专有); 实际路由/鉴权在 feishu_events (需 scene_slug),
# 此处只识别命令词, handle_admin_command 不处理 mode (它只管 llm 切换)。
_VERB_ALIASES = {
    "model": "model", "m": "model", "llm": "model", "切换": "model", "模型": "model",
    "models": "models", "model-list": "models",
    "mode": "mode", "模式": "mode", "切模式": "mode", "人格": "mode",
    "help": "help", "?": "help", "帮助": "help",
}


def admin_open_ids() -> set[str]:
    raw = os.environ.get(ADMIN_ENV, "") or ""
    return {x.strip() for x in raw.split(",") if x.strip()}


def is_admin(open_id: str) -> bool:
    return bool(open_id) and open_id in admin_open_ids()


def parse_admin_command(text: str) -> Optional[tuple[str, list[str]]]:
    """文字消息 → (verb, args) 或 None (非管理命令, 交回常规流程)。纯函数, 可测。"""
    if not text:
        return None
    t = text.strip()
    if not t.startswith("/"):
        return None
    parts = t[1:].split()
    if not parts:
        return None
    verb = _VERB_ALIASES.get(parts[0].lower())
    if verb is None:
        return None
    return verb, parts[1:]


# ─── 卡片数据 (纯 dict, feishu_events 负责渲成飞书卡片) ───

def _card(title: str, lines: list[str], template: str = "blue") -> dict[str, Any]:
    return {"title": title, "lines": lines, "template": template}


def _help_lines(profiles: dict, active: Optional[str], active_model: Optional[str]) -> list[str]:
    """完整命令手册 (所有人可见; 切模型仍仅管理员)。网关列表从 profiles 动态生成。"""
    gateways = " / ".join(f"`{n}`" for n in profiles) or "(无)"
    return [
        "**📄 提交评审 (所有人)**",
        "直接发**文档文件** (PDF / Word .docx / Markdown / .txt) → 自动派评委评审,",
        "约 18-25 分钟后在本对话回推报告卡片。飞书云文档链接仅部分机器人支持直读"
        "(不支持时会回卡提示); 导出为文件发送在所有机器人一定可用。",
        "> 每人每日有配额上限; 满了次日恢复或联系管理员调整。",
        "",
        "**🧭 建议决策与进展 (所有人 · 战略 OS 回路)**",
        "评审卡片带「🎯 优先建议」区块, 对建议直接回复:",
        "· `决策 <建议ID> 采纳` — 采纳并自动进行动台账 (期限 T+30, T+7/T+30 跟催)",
        "· `决策 <建议ID> 不采纳 理由:一句话` — 判定还有 部分采纳/校准; 后三者必带理由",
        "· `进展 <行动ID> 完成/进行中/受阻/放弃 [说明]` — 更新行动进展",
        "> AI 是参谋, 判断在人; 修正 = 再发一条 (追加不改史)。",
        "",
        "**🔧 模型管理 (仅管理员)**",
        f"当前: 网关 `{active or '(未标记)'}` · 实跑模型 `{active_model or '(未设)'}`",
        "",
        "· `/model`",
        "   看当前网关 + 实跑模型 + 可切网关列表。",
        "· `/model <网关>`",
        "   切到该网关的**默认模型** (例: `/model gpt` → asiainfo 网关默认)。",
        "· `/model <网关> <模型>`",
        "   切网关 + 指定模型 (例: `/model gpt gpt-5.5`)。",
        "· `/model <模型>`",
        "   在**当前网关**上换模型 (例: `/model gpt-5.5`)。",
        "· `/models [网关]`",
        "   列该网关可用模型 (省略=当前; 例: `/models gpt` 列 asiainfo 全部)。",
        "· `/help`",
        "   本帮助 (所有人可发)。",
        "",
        f"**可选网关** (provider/端点): {gateways}",
        "  gpt = asiainfo 自建网关 · gpt-openai = OpenAI 官方 · glm = 智谱 · claude = Anthropic",
        "",
        "**⚠️ 注意事项**",
        "1. `/model <网关>` 只给网关 = **重置成该网关默认模型**, 会覆盖你之前手动设的模型!",
        "   想保留具体模型请用 `/model <网关> <模型>`。",
        "2. 切模型影响**全员评审**, 仅 `BOSS_ADMIN_WHITELIST` 内管理员可切; 命令仅**单聊**生效。",
        "3. 切换只改配置, **下一单**评审自动用新模型, **无需重启**; 正在跑的那单用旧模型跑完。",
        "4. 模型名须是该网关 `/models` 列出的 (区分大小写); 选了不存在的会评审失败。",
        "5. o 系列 (o1/o3/o4-mini) 参数不兼容, 别用; 用 gpt-4o / gpt-5.x / deepseek / glm 等。",
    ]


def _failover_status() -> str:
    """failover 开关/链/冷却 一行摘要 (fail-open, 取不到就空)。"""
    try:
        import llm_failover
        return llm_failover.failover_status_line()
    except Exception:  # noqa: BLE001
        return ""


def _status_lines(profiles: dict, active: Optional[str], active_model: Optional[str]) -> list[str]:
    lines = [
        f"**当前**: 网关 `{active or '(未标记)'}` · 实跑模型 `{active_model or '(未设)'}`",
    ]
    fo = _failover_status()
    if fo:
        lines.append(f"**{fo}**")
    lines += [
        "",
        "**可切网关** (发 `/model <网关>`):",
    ]
    for name, p in profiles.items():
        mark = "  ← 当前" if name == active else ""
        lines.append(f"- `{name}` · {p.get('provider','?')} · 默认 {p.get('model_deep','?')}{mark}")
    lines += [
        "",
        "**用法**: `/model <网关>` 切网关 · `/model <网关> <模型>` 指定模型 · "
        "`/models <网关>` 看该网关模型",
    ]
    return lines


def handle_admin_command(verb: str, args: list[str], *,
                         env_path: Optional[Path] = None,
                         profiles_path: Optional[Path] = None) -> dict[str, Any]:
    """执行管理命令, 返回卡片数据 {title, lines, template}。不抛异常 (失败也回卡片)。"""
    import llm_switch as ls
    env_path = env_path or (VAULT_ROOT / ".env")
    profiles_path = profiles_path or (VAULT_ROOT / "config" / "llm_profiles.yaml")

    try:
        profiles = ls.load_profiles(profiles_path)
    except ls.SwitchError as e:
        return _card("⚠ 配置错误", [str(e)], template="red")
    lines_env = ls.read_env_lines(env_path)
    active = ls.active_profile(lines_env)
    active_model = ls.env_value(lines_env, "BOSS_LLM_MODEL_DEEP")

    if verb == "help":
        return _card("📖 机器人使用帮助", _help_lines(profiles, active, active_model))

    if verb == "models":
        gw = args[0] if args else active
        if not gw or gw not in profiles:
            return _card("⚠ 未指定网关", [f"用 `/models <网关>`; 可选: {', '.join(profiles)}"], template="red")
        base_url, api_key = ls._resolve_profile_endpoint(profiles[gw], lines_env)
        if not base_url:
            return _card(f"网关 {gw}", [f"`{gw}` 无 base_url (anthropic 默认端点), 无法列模型"])
        models = ls.fetch_gateway_models(base_url, api_key)
        if not models:
            return _card(f"网关 {gw} 模型", ["(没拉到 — 端点不支持 /models 或网络问题; 仍可直接 `/model <网关> <模型名>`)"])
        return _card(f"📋 {gw} 端点可用模型 ({len(models)} 个)",
                     [f"- `{m}`" for m in models] + ["", f"切换: `/model {gw} <模型>`"])

    # verb == "model"
    if not args:
        return _card("🔧 当前模型配置", _status_lines(profiles, active, active_model))

    a0 = args[0]
    if a0.lower() in ("help", "?"):
        return handle_admin_command("help", [], env_path=env_path, profiles_path=profiles_path)

    # a0 是网关 → 切网关 (+ 可选模型 a1); 否则当作"当前网关上换模型"
    if a0 in profiles:
        gateway = a0
        model = args[1] if len(args) > 1 else None
    else:
        if not active or active not in profiles:
            return _card("⚠ 先选网关", [
                f"`{a0}` 不是网关名; 当前也没激活网关。",
                f"先 `/model <网关>` (可选: {', '.join(profiles)})", ], template="red")
        gateway = active
        model = a0

    try:
        info = ls.apply_profile(gateway, env_path=env_path, profiles_path=profiles_path,
                                model_fast=model, model_deep=model)
    except ls.SwitchError as e:
        return _card("✗ 切换失败", [str(e)], template="red")

    note = []
    if model is None:
        base_url, api_key = ls._resolve_profile_endpoint(profiles[gateway], lines_env)
        if base_url:
            avail = ls.fetch_gateway_models(base_url, api_key)
            if avail:
                note = ["", f"该网关可用模型 ({len(avail)} 个) 见 `/models {gateway}`; "
                            f"换具体模型发 `/model {gateway} <模型>`"]
    return _card("✅ 已切换模型", [
        f"网关: `{info['profile']}` · provider `{info['provider']}`",
        f"模型: fast `{info['model_fast']}` · deep `{info['model_deep']}`",
        f"端点: {info['base_url']}",
        "",
        "**下一单评审自动生效** (worker 热重载, 无需重启)。",
    ] + note)
