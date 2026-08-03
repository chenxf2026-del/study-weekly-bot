#!/usr/bin/env python3
"""
llm_switch.py — 一条命令切换主评审流水线的 LLM provider / 端点 / 模型。

读 config/llm_profiles.yaml 的 profile, 改写 .env 的 BOSS_LLM_* 5 行 (其余行不动),
密钥从 profile 指向的 key 变量 (如 OPENAI_API_KEY) 现读现写, 私有端点用 ${VAR} 从 .env 取。

  python3 scripts/llm_switch.py                 # 交互式菜单 (傻瓜化, 无参数即进)
                                                #   选 profile 后, openai 兼容端点会拉
                                                #   /v1/models 列出该网关可用模型让你选号
  python3 scripts/llm_switch.py menu            # 同上, 显式进菜单
  python3 scripts/llm_switch.py models [name]   # 列某 profile 端点上的可用模型
  python3 scripts/llm_switch.py list            # 列 profile + 当前激活
  python3 scripts/llm_switch.py show            # 当前 BOSS_LLM_* (key 打码)
  python3 scripts/llm_switch.py status          # 现网各通道生效模型 + 解析逻辑 + per-scene/failover (只读)
  python3 scripts/llm_switch.py failover-test    # 测 failover 链: 解析 + 逐个探活备选 + 报切换顺序 (只读)
  python3 scripts/llm_switch.py failover-test --models   # ↑ 再逐个链节点列端点配置 + 可用模型
  python3 scripts/llm_switch.py use <name>      # 切换 (改写 .env)

切完重启生效:  sudo systemctl restart boss-review-worker
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

VAULT_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_ENV = VAULT_ROOT / ".env"
DEFAULT_PROFILES = VAULT_ROOT / "config" / "llm_profiles.yaml"

# llm_switch 改写的字段 (其余 .env 行一律保留)
MANAGED_KEYS = ("BOSS_LLM_PROVIDER", "BOSS_LLM_BASE_URL",
                "BOSS_LLM_MODEL_FAST", "BOSS_LLM_MODEL_DEEP", "BOSS_LLM_API_KEY")
ACTIVE_MARKER = "# llm_switch_active:"


class SwitchError(RuntimeError):
    pass


def load_profiles(path: Path = DEFAULT_PROFILES) -> dict[str, dict]:
    import yaml
    if not path.exists():
        raise SwitchError(f"profile 文件不存在: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profiles = data.get("profiles") or {}
    if not isinstance(profiles, dict) or not profiles:
        raise SwitchError(f"{path} 无 profiles 段或为空")
    return profiles


def read_env_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def env_value(lines: list[str], name: str) -> Optional[str]:
    """取 .env 中 `name=` 的值 (最后一条生效, 与 shell `set -a; .` 一致)。"""
    val = None
    for line in lines:
        if line.startswith(f"{name}="):
            val = line.split("=", 1)[1]
    return val


def expand(value: str, lines: list[str]) -> str:
    """${VAR} → 从 .env 读 VAR 的值; 否则原样返回。"""
    m = re.fullmatch(r"\$\{(\w+)\}", value.strip())
    if not m:
        return value
    var = m.group(1)
    resolved = env_value(lines, var)
    if resolved is None:
        raise SwitchError(f"base_url 引用 ${{{var}}}, 但 .env 里未配 {var}=")
    return resolved


def active_profile(lines: list[str]) -> Optional[str]:
    for line in lines:
        if line.startswith(ACTIVE_MARKER):
            return line[len(ACTIVE_MARKER):].strip()
    return None


def _mask(secret: Optional[str]) -> str:
    if not secret:
        return "(未设)"
    s = secret.strip()
    return f"{s[:4]}…{s[-4:]}（{len(s)} 位）" if len(s) > 10 else "****"


def _clean_model_name(s: str) -> str:
    """剥模型名首尾的反引号 / 引号 / 空白。

    2026-07-02 踩坑: 从飞书卡片带 markdown 反引号复制模型名发 `/model`, 结果 .env 写成
    ``BOSS_LLM_MODEL_DEEP=`wangsu/...` ``, 发给网关的模型名含字面反引号 → 精确匹配失败 401
    (key_model_access_denied, 尽管该模型在允许列表)。这里统一剥掉成对首尾的 ` ' " (可多层)。
    """
    s = (s or "").strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in "`'\"":
        s = s[1:-1].strip()
    return s


def apply_profile(name: str, *, env_path: Path = DEFAULT_ENV,
                  profiles_path: Path = DEFAULT_PROFILES,
                  model_fast: Optional[str] = None,
                  model_deep: Optional[str] = None) -> dict[str, str]:
    """把 profile <name> 写进 .env 的 BOSS_LLM_* 块。返回落地后的配置 (key 已打码)。

    model_fast / model_deep: CLI 覆盖 (端点上任意模型名), 优先于 profile 默认。
    """
    profiles = load_profiles(profiles_path)
    if name not in profiles:
        raise SwitchError(f"未知 profile: {name!r}; 可选: {', '.join(sorted(profiles))}")
    p = profiles[name] or {}
    lines = read_env_lines(env_path)

    provider = str(p.get("provider") or "").strip()
    if not provider:
        raise SwitchError(f"profile {name} 缺 provider")
    base_url = expand(str(p.get("base_url") or ""), lines).strip()
    # 模型: CLI 覆盖 > profile (profile 也支持 ${VAR} 从 .env 取)。
    # _clean_model_name 剥首尾反引号/引号, 防从卡片带 markdown 反引号复制污染 (→ 网关 401)。
    model_fast = _clean_model_name(model_fast or expand(str(p.get("model_fast") or ""), lines))
    model_deep = _clean_model_name(model_deep or expand(str(p.get("model_deep") or ""), lines))

    # key: profile 指向的 key 变量, 从 .env 现读 (两个 openai-compatible 端点必须显式落 key)
    key_env = str(p.get("api_key_env") or "").strip()
    key_val = env_value(lines, key_env) if key_env else None
    needs_key = provider not in {"codex-cli", "kimi-cli"}
    if needs_key and key_env and not key_val:
        raise SwitchError(
            f"profile {name} 的 api_key_env={key_env}, 但 .env 里未配 {key_env}=<key>; "
            f"请先在 .env 加 `{key_env}=...` 再切换")

    # 删旧 BOSS_LLM_* 行 + 旧激活标记, 其余保留
    kept = [ln for ln in lines
            if not any(ln.startswith(f"{k}=") for k in MANAGED_KEYS)
            and not ln.startswith(ACTIVE_MARKER)]
    while kept and kept[-1].strip() == "":
        kept.pop()

    block = [f"BOSS_LLM_PROVIDER={provider}"]
    if base_url:
        block.append(f"BOSS_LLM_BASE_URL={base_url}")
    if model_fast:
        block.append(f"BOSS_LLM_MODEL_FAST={model_fast}")
    if model_deep:
        block.append(f"BOSS_LLM_MODEL_DEEP={model_deep}")
    if needs_key and key_val:
        block.append(f"BOSS_LLM_API_KEY={key_val}")
    block.append(f"{ACTIVE_MARKER} {name}")

    new_text = "\n".join(kept + [""] + block) + "\n"
    env_path.write_text(new_text, encoding="utf-8")

    # 管理台审计埋点 (M0): 谁在何时把系统切到哪个网关/模型。fail-open, 不含 key。
    try:
        import telemetry
        telemetry.record_event("model_switch", profile=name, provider=provider,
                               model_fast=model_fast or "", model_deep=model_deep or "")
    except Exception:  # noqa: BLE001
        pass

    return {
        "profile": name,
        "provider": provider,
        "base_url": base_url or "(provider 默认)",
        "model_fast": model_fast or "(默认)",
        "model_deep": model_deep or "(默认)",
        "api_key": _mask(key_val) + (f"  ← {key_env}" if key_env else ""),
    }


def render_menu(profiles: dict[str, dict], active: Optional[str],
                active_model: Optional[str] = None) -> str:
    """编号菜单文本 (纯函数, 可测)。

    第一层选的是**网关 / provider** (一个 profile = 一个端点); 选完会列该网关的可用模型。
    active_model: 当前 .env 实跑的模型 (BOSS_LLM_MODEL_DEEP), 显示在「当前」网关行,
    区别于 profile 里写的默认模型。"""
    bar = "─" * 56
    lines = ["", "  切换 LLM 模型 ·  ① 选网关 (序号) → ② 列出该网关的模型再选",
             "  (每行是一个网关 / 端点; 第二列是该网关的默认模型)", bar]
    for i, (name, p) in enumerate(profiles.items(), 1):
        is_active = name == active
        mark = "  ← 当前网关" if is_active else ""
        lines.append(f"  [{i}] {name:12s} {str(p.get('provider','?')):18s} "
                     f"默认 {str(p.get('model_deep','?')):14s}{mark}")
        bu = p.get("base_url") or "(provider 默认)"
        if is_active and active_model:
            lines.append(f"      端点: {bu}   · 当前实跑: {active_model}")
        else:
            lines.append(f"      端点: {bu}")
    lines.append("  [q] 退出 (不改动)")
    lines.append(bar)
    lines.append("  ▶ 输网关序号 (如 2) 进入该网关的模型列表; 直接 q 退出")
    return "\n".join(lines)


def resolve_menu_choice(choice: str, names: list[str]) -> Optional[str]:
    """菜单输入 → profile 名。'q'/'quit'/空 → None (退出); 非法 → SwitchError。
    支持序号 (1-based) 或直接输 profile 名。"""
    c = choice.strip()
    if c.lower() in ("q", "quit", ""):
        return None
    if c.isdigit():
        idx = int(c)
        if 1 <= idx <= len(names):
            return names[idx - 1]
        raise SwitchError(f"序号超出范围 (应在 1-{len(names)})")
    if c in names:
        return c
    raise SwitchError(f"无效输入: {choice!r} (输序号或 profile 名, q 退出)")


def fetch_gateway_models(base_url: str, api_key: Optional[str], *, timeout: float = 8.0,
                         raise_on_error: bool = False) -> list[str]:
    """拉 OpenAI 兼容端点的 GET {base_url}/models, 返回模型 id 列表 (排序去重)。

    raise_on_error=False (默认): 失败 (网络/鉴权/端点不支持) → [] (菜单兜底成自由输入, 不阻断切换)。
    raise_on_error=True: 失败抛 SwitchError (带 HTTP 状态码) —— 供**探活**区分"鉴权失败 (401/403)"
      与"端点真返回空列表", 否则 401 被吞成 [] 会让 doctor/failover-test 把死端点误报成 '✓ 0 模型可用'。"""
    import json
    import urllib.request
    if not base_url:
        return []
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        if raise_on_error:
            code = getattr(e, "code", None)   # urllib.error.HTTPError 带 .code
            raise SwitchError(f"拉 {url} 失败: "
                              + (f"HTTP {code}" if code else f"{type(e).__name__}: {str(e)[:80]}"))
        return []
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    ids = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
    return sorted(set(str(i) for i in ids))


def render_model_menu(models: list[str], profile_name: str) -> str:
    """端点模型编号子菜单 (纯函数, 可测)。"""
    lines = [f"  {profile_name} 端点上的可用模型 ({len(models)} 个):"]
    for i, m in enumerate(models, 1):
        lines.append(f"    [{i}] {m}")
    lines.append("    [Enter] 用 profile 默认 · 或直接输任意模型名")
    return "\n".join(lines)


# 合法模型名字符集: 字母数字 + . _ : / - (如 wangsu/gpt-5.5, claude-opus-4-8, gpt-4o-mini)。
# 不含空格/shell 元字符 → 据此拒掉"误把命令粘进模型输入"(如粘了 `sudo systemctl restart ...`),
# 防被当模型名写进 .env (2026-07-02 交互粘贴踩坑)。
_MODEL_NAME_RE = re.compile(r"[A-Za-z0-9._:/\-]+")


def resolve_model_choice(choice: str, models: list[str]) -> str:
    """模型子菜单输入 → 模型名。空 → ''(用 profile 默认); 序号 → models[i]; 其它 → 原样
    (端点上任意模型名, 自由输入)。序号越界 / 非法字符 (含空格) → SwitchError。"""
    c = choice.strip()
    if c == "":
        return ""
    if c.isdigit():
        idx = int(c)
        if 1 <= idx <= len(models):
            return models[idx - 1]
        raise SwitchError(f"序号超出范围 (应在 1-{len(models)})")
    c = _clean_model_name(c)   # 先剥首尾反引号/引号 (卡片复制常带), 再校验
    if not _MODEL_NAME_RE.fullmatch(c):
        raise SwitchError("看起来不像模型名 (含空格或非法字符, 可能误粘了命令); 请输列表序号或合法模型名")
    return c


def _resolve_profile_endpoint(profile: dict, lines: list[str]) -> tuple[str, Optional[str]]:
    """从 profile + .env 解析 (base_url, api_key) — 用于拉端点模型列表。"""
    try:
        base_url = expand(str(profile.get("base_url") or ""), lines).strip()
    except SwitchError:
        base_url = ""
    key_env = str(profile.get("api_key_env") or "").strip()
    api_key = env_value(lines, key_env) if key_env else None
    return base_url, api_key


def _choose_model_interactive(profile_name: str, profile: dict, lines: list[str]) -> str:
    """选模型: openai 兼容端点能拉到 /models 时给编号子菜单, 否则自由输入。返回模型名或 ''。"""
    base_url, api_key = _resolve_profile_endpoint(profile, lines)
    models: list[str] = []
    if base_url and api_key:
        print(f"  正在从 {base_url} 拉可用模型 …")
        models = fetch_gateway_models(base_url, api_key)
    if models:
        print(render_model_menu(models, profile_name))
        for _ in range(3):
            try:
                raw = input("  选模型 (序号 / 直接输模型名 / 留空=profile 默认): ").strip()
            except EOFError:
                return ""
            try:
                return resolve_model_choice(raw, models)
            except SwitchError as e:
                print(f"  ✗ {e}")
        return ""
    # 兜底: 拉不到列表 (anthropic / 端点不支持 /models / 网络失败) → 自由输入
    if base_url:
        print(f"  (没拉到 {base_url} 的模型列表 — 手动输入模型名即可)")
    for _ in range(3):
        try:
            raw = input(f"  模型名 (留空=用 {profile_name} 默认; 或输端点上任意模型名): ").strip()
        except EOFError:
            return ""
        if raw == "":
            return ""
        if _MODEL_NAME_RE.fullmatch(raw):
            return raw
        print("  ✗ 看起来不像模型名 (含空格或非法字符, 可能误粘了命令); 请重输或留空")
    return ""


def _restart_worker() -> None:
    import subprocess
    cmd = ["sudo", "systemctl", "restart", "boss-review-worker"]
    print(f"$ {' '.join(cmd)}")
    try:
        rc = subprocess.run(cmd).returncode
    except FileNotFoundError:
        print("✗ 找不到 systemctl (非 VM?) — 请在 VM 上手动重启"); return
    print("✅ worker 已重启" if rc == 0 else f"✗ 重启返回 {rc} — 手动跑上面的命令")


def _cmd_menu(args) -> int:
    profiles = load_profiles(Path(args.profiles))
    names = list(profiles)
    env_path = Path(args.env_file)
    env_lines = read_env_lines(env_path)
    active = active_profile(env_lines)
    active_model = env_value(env_lines, "BOSS_LLM_MODEL_DEEP")
    print(render_menu(profiles, active, active_model))

    try:
        raw = input("  选网关 (序号 → 看该网关的模型; q 退出): ")
    except EOFError:
        return 0
    try:
        name = resolve_menu_choice(raw, names)
    except SwitchError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    if name is None:
        print("已退出, 未改动。")
        return 0

    model = _choose_model_interactive(name, profiles[name], read_env_lines(env_path))

    try:
        info = apply_profile(name, env_path=env_path, profiles_path=Path(args.profiles),
                             model_fast=model or None, model_deep=model or None)
    except SwitchError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2

    print(f"\n✅ 已切到 profile = {info['profile']}")
    for k in ("provider", "base_url", "model_fast", "model_deep", "api_key"):
        print(f"  {k:12s} {info[k]}")

    print()
    try:
        ans = input("  现在重启 boss-review-worker 生效? [y/N]: ").strip().lower()
    except EOFError:
        ans = "n"
    if ans == "y":
        _restart_worker()
    else:
        print("  稍后手动重启:  sudo systemctl restart boss-review-worker")
    return 0


def _cmd_models(args) -> int:
    """列某 profile 端点上的可用模型 (openai 兼容端点的 /v1/models)。"""
    profiles = load_profiles(Path(args.profiles))
    lines = read_env_lines(Path(args.env_file))
    name = args.name or active_profile(lines)
    if not name:
        print("✗ 未指定 profile 且 .env 无激活标记; 用: llm models <profile>", file=sys.stderr)
        return 2
    if name not in profiles:
        raise SwitchError(f"未知 profile: {name!r}; 可选: {', '.join(sorted(profiles))}")
    base_url, api_key = _resolve_profile_endpoint(profiles[name], lines)
    if not base_url:
        print(f"profile {name} 无 base_url (可能是 anthropic 默认端点) — 无法列模型")
        return 0
    print(f"从 {base_url} 拉取 {name} 端点的模型 …")
    models = fetch_gateway_models(base_url, api_key)
    if not models:
        print("(没拉到 — 端点不支持 /models / 鉴权失败 / 网络不通; 仍可手动指定模型名)")
        return 0
    print(render_model_menu(models, name))
    return 0


def _cmd_list(args) -> int:
    profiles = load_profiles(Path(args.profiles))
    cur = active_profile(read_env_lines(Path(args.env_file)))
    print(f"LLM profiles ({args.profiles}):\n")
    for name, p in profiles.items():
        mark = " ← 当前" if name == cur else ""
        bu = p.get("base_url") or "(provider 默认)"
        print(f"  {name:12s} {p.get('provider','?'):20s} {p.get('model_deep','?'):14s} {bu}{mark}")
    if cur is None:
        print("\n(当前 .env 无 llm_switch 激活标记 — 可能是手工配置的)")
    return 0


def _cmd_show(args) -> int:
    lines = read_env_lines(Path(args.env_file))
    cur = active_profile(lines)
    print(f"当前 LLM 配置 ({args.env_file}):  profile = {cur or '(手工/未标记)'}\n")
    for k in MANAGED_KEYS:
        v = env_value(lines, k)
        shown = _mask(v) if k == "BOSS_LLM_API_KEY" else (v if v is not None else "(未设)")
        print(f"  {k:22s} {shown}")
    return 0


def _cmd_doctor(args) -> int:
    """体检所有 profile 端点 (只读)。退出码非零 = 有端点探测失败 (缺 key/CLI 型的跳过项不计), 便于挂巡检。"""
    import llm_failover
    results = llm_failover.doctor(profiles_path=Path(args.profiles), env_path=Path(args.env_file))
    print(f"端点体检 (llm doctor) — {len(results)} 个 profile:\n")
    n_fail = 0
    for h in results:
        if h.skipped:
            mark = "⊘"
        elif h.ok:
            mark = "✓"
        else:
            mark = "✗"
            n_fail += 1
        print(f"  {mark} {h.profile:14s} {h.detail}")
    print(f"\n{failover_status_or_hint(Path(args.profiles), Path(args.env_file))}")
    if n_fail:
        print(f"\n⚠ {n_fail} 个端点探测失败 (跳过项不计); 检查网关/密钥。", file=sys.stderr)
    return 1 if n_fail else 0


def failover_status_or_hint(profiles_path: Path, env_path: Optional[Path] = None) -> str:
    import llm_failover
    # env_path → 从 .env 文件读 failover 开关 (CLI 场景 os.environ 常没 source, 避免误显示成关)
    return llm_failover.failover_status_line(profiles_path=profiles_path, env_path=env_path)


def _cmd_use(args) -> int:
    mf = args.model or args.model_fast
    md = args.model or args.model_deep
    info = apply_profile(args.name, env_path=Path(args.env_file),
                         profiles_path=Path(args.profiles),
                         model_fast=mf, model_deep=md)
    print(f"✅ 已切到 profile = {info['profile']}\n")
    for k in ("provider", "base_url", "model_fast", "model_deep", "api_key"):
        print(f"  {k:12s} {info[k]}")
    print("\n重启生效:  sudo systemctl restart boss-review-worker")
    print("验证一发:  python3 scripts/llm_switch.py show")
    return 0


def _cmd_status(args) -> int:
    """一屏看清**现网**模型配置 + 各通道解析逻辑 (只读; 读 .env 文件 + profiles + scenes)。

    与 show 的区别: show 只列 BOSS_LLM_* 原始变量; status 把它们**解析成每个通道
    (评审/分身日常/分身大会/语义比对) 实际生效的模型 + 命中的来源变量**, 并打印解析
    优先级逻辑与 per-scene 覆盖、failover 状态。全部读 .env 文件 (同 show/models),
    反映服务重启后会用的值。"""
    profiles = load_profiles(Path(args.profiles))
    lines = read_env_lines(Path(args.env_file))
    cur = active_profile(lines)

    def pick(*names: str) -> tuple[Optional[str], Optional[str]]:
        """按 names 顺序取 .env 首个非空值 (剥反引号/空白)。返回 (值, 命中变量名) 或 (None, None)。"""
        for n in names:
            v = env_value(lines, n)
            if v is not None:
                cv = _clean_model_name(v)
                if cv:
                    return cv, n
        return None, None

    # tier 兜底 (与 model_defaults 同源: ANTHROPIC_MODEL_* → 硬编码字面量)。读 .env 文件保持一致。
    try:
        import model_defaults as md
        fb_fast = _clean_model_name(env_value(lines, "ANTHROPIC_MODEL_SONNET") or md._FALLBACK_FAST)
        fb_deep = _clean_model_name(env_value(lines, "ANTHROPIC_MODEL_OPUS") or md._FALLBACK_DEEP)
        fb_haiku = _clean_model_name(env_value(lines, "ANTHROPIC_MODEL_HAIKU") or md._FALLBACK_HAIKU)
    except Exception:  # noqa: BLE001 — model_defaults 缺失也不炸, 兜底显示未知
        fb_fast = fb_deep = fb_haiku = "?"

    o: list[str] = [f"现网 LLM 模型配置  (读 {args.env_file} + {args.profiles} + scenes/)\n"]

    # ── 激活网关 ──
    prof = profiles.get(cur) if cur else None
    base_url, api_key = _resolve_profile_endpoint(prof, lines) if prof else ("", None)
    o.append("■ 激活网关 (profile · llm_switch use 决定 provider/端点/key)")
    if cur:
        o.append(f"    {cur}   provider={(prof or {}).get('provider','?')}   {base_url or '(provider 默认端点)'}")
        o.append(f"    key   {_mask(api_key) if api_key else '(未设!)'}")
    else:
        o.append("    (无激活标记 — .env 手工配置, 或未经 llm_switch use)")
    o.append("")

    # ── 各通道生效模型 ──
    rv_deep, s = pick("BOSS_LLM_MODEL_DEEP")
    rv_deep, rvd_src = (rv_deep or fb_deep), (s or "兜底 ANTHROPIC_MODEL_OPUS→字面量")
    rv_fast, s = pick("BOSS_LLM_MODEL_FAST")
    rv_fast, rvf_src = (rv_fast or fb_fast), (s or "兜底 ANTHROPIC_MODEL_SONNET→字面量")
    pd, s = pick("BOSS_LLM_MODEL_PERSONA", "BOSS_LLM_MODEL_DEEP", "BOSS_LLM_MODEL_FAST")
    pd, pd_src = (pd or fb_deep), (s or "兜底")
    pdm, s = pick("BOSS_LLM_MODEL_PERSONA_DEMO", "BOSS_LLM_MODEL_PERSONA",
                  "BOSS_LLM_MODEL_DEEP", "BOSS_LLM_MODEL_FAST")
    pdm, pdm_src = (pdm or fb_deep), (s or "兜底")
    hk, s = pick("ANTHROPIC_MODEL_HAIKU")
    hk, hk_src = (hk or fb_haiku), (s or "字面量兜底")

    o.append("■ 各通道生效模型  (模型名 ← 命中来源)")
    o.append(f"    评审/竞赛/会议 · deep (Phase4-5 打分/合议)   {rv_deep:36s} ← {rvd_src}")
    o.append(f"    评审/竞赛/会议 · fast (Phase1-3 起草/合成)   {rv_fast:36s} ← {rvf_src}")
    o.append(f"    分身 · 日常                                 {pd:36s} ← {pd_src}")
    o.append(f"    分身 · 大会                                 {pdm:36s} ← {pdm_src}")
    o.append(f"    语义比对/过滤 (attribution)                 {hk:36s} ← {hk_src}")
    o.append("")

    # ── per-scene 覆盖 ──
    o.append("■ per-scene 覆盖 (scenes/<slug>/scene.yaml 的 llm: · 仅评审流水线读)")
    ovs: list[str] = []
    try:
        import scene_loader as sl
        for sc in sl.list_scenes():
            ov = getattr(sc, "llm", None)
            if ov:
                m = ov.model_deep or ov.model_fast or "(用 profile 默认)"
                ovs.append(f"    {sc.name:26s} profile={ov.profile}  model={m}")
    except Exception as e:  # noqa: BLE001
        ovs.append(f"    (scene 读取失败: {type(e).__name__}: {e})")
    o.extend(ovs or ["    (无 — 所有评审/竞赛/会议场景走全局激活网关)"])
    o.append("    ⚠ 分身 (persona) 不读 scene.llm; 分身模型走上面的 PERSONA / PERSONA_DEMO 变量")
    o.append("")

    # ── failover ──
    fo_raw = (env_value(lines, "BOSS_LLM_FAILOVER") or "").strip()
    fo_on = fo_raw.lower() in {"1", "true", "yes", "on"}
    try:
        import llm_failover
        chain = llm_failover.load_failover_chain(Path(args.profiles))
    except Exception:  # noqa: BLE001
        chain = []
    o.append("■ 故障切换 (failover · 端点级故障自动切备选)")
    o.append(f"    开关  {'开' if fo_on else '关'}  (BOSS_LLM_FAILOVER={fo_raw or '未设'})")
    o.append(f"    链    {' → '.join(chain) if chain else '(未配 failover.chain)'}")
    o.append("")

    # ── 解析逻辑 ──
    o.append("■ 解析优先级 (逻辑 · 左优先, 缺则右)")
    o.append("    评审/竞赛/会议   BOSS_LLM_MODEL_DEEP|FAST (llm_switch use / scene.llm 写入)")
    o.append("                    → 兜底 model_defaults (ANTHROPIC_MODEL_OPUS|SONNET → 字面量)")
    o.append("    分身·日常        BOSS_LLM_MODEL_PERSONA → DEEP → FAST → 兜底")
    o.append("    分身·大会(demo)  BOSS_LLM_MODEL_PERSONA_DEMO → PERSONA → DEEP → FAST → 兜底")
    o.append("    语义比对/过滤    ANTHROPIC_MODEL_HAIKU → 字面量兜底 (attribution 另: GLM_MODEL 优先)")
    o.append("    provider/端点    激活 profile 决定; failover 开时端点级故障 (429/5xx/超时/WAF/401) 自动切链")
    o.append("    ★ 换模型只换模型名, provider/base_url/key 跟随激活 profile → 新模型须在当前网关有 channel")
    o.append("      (查网关模型: python3 scripts/llm_switch.py models <profile>)")

    print("\n".join(o))
    return 0


def _fetch_models_summary(provider: str, base_url: str, key_val: Optional[str]) -> str:
    """拉某链节点端点的可用模型一行摘要。openai 兼容端点走 /v1/models; 其余给原因说明。只读。"""
    if provider in ("anthropic", "anthropic-compatible"):
        return "(anthropic 端点不提供 /v1/models 列表)"
    if provider in ("codex-cli", "kimi-cli"):
        return "(CLI 型 provider, 无端点模型列表)"
    if not base_url:
        return "(无 base_url, 跳过)"
    if not key_val:
        return "(缺 key, 跳过拉取)"
    try:
        models = fetch_gateway_models(base_url, key_val, raise_on_error=True)
    except SwitchError as e:            # 鉴权/HTTP 错误 → 显式报, 别吞成"返回空"
        return f"⚠ {e}"
    except Exception as e:  # noqa: BLE001 — 其余异常不炸整条链测试
        return f"(拉取失败: {type(e).__name__})"
    if not models:
        return "(端点 /v1/models 返回空 — 可达但列不出模型, 无法确认目标模型可用)"
    return f"({len(models)}) " + " · ".join(models)


def _chain_detail_lines(*, profiles_path: Path, lines: list[str],
                        preferred: Optional[str], plan, fetch_models: bool) -> list[str]:
    """--models: 逐个 failover.chain 节点打印实际端点配置 (provider/端点/key/模型档) + 可用模型。

    遍历**整条链** (含首选与被跳过的节点), 端点配置从 profile + .env 解析 (即便该节点会被
    failover 跳过也照样显示, 便于诊断"为什么这个备选没用上")。"""
    import llm_failover as lf
    chain = lf.load_failover_chain(profiles_path)
    if not chain:
        return ["\n  (config/llm_profiles.yaml 未配 failover.chain)"]
    profiles = load_profiles(profiles_path)
    cand_names = {c.profile for c in plan.candidates}
    skip = dict(plan.skipped)
    out = ["\n  链节点端点配置" + (" + 可用模型" if fetch_models else "") + "  (按 failover.chain 顺序):"]
    for i, name in enumerate(chain, 1):
        p = profiles.get(name) or {}
        provider = str(p.get("provider") or "?")
        role = ("首选" if name == preferred else
                "备选" if name in cand_names else
                f"跳过·{skip.get(name, '未知')}")
        try:
            base_url = expand(str(p.get("base_url") or ""), lines).strip()
            base_err = ""
        except SwitchError as e:
            base_url, base_err = "", str(e)
        key_env = str(p.get("api_key_env") or "").strip()
        key_val = env_value(lines, key_env) if key_env else None
        mf = _clean_model_name(expand(str(p.get("model_fast") or ""), lines)) if p.get("model_fast") else ""
        md = _clean_model_name(expand(str(p.get("model_deep") or ""), lines)) if p.get("model_deep") else ""

        out.append(f"\n  {i}. {name}  [{role}]")
        out.append(f"       provider   {provider}")
        out.append(f"       endpoint   {base_url or ('⚠ ' + base_err if base_err else '(provider 默认端点)')}")
        out.append(f"       key        {key_env or '(无 api_key_env)'}  "
                   f"{'✓ ' + _mask(key_val) if key_val else '✗ 未配'}")
        out.append(f"       模型档     fast={mf or '(profile 默认)'}   deep={md or '(profile 默认)'}")
        if fetch_models:
            out.append(f"       可用模型   {_fetch_models_summary(provider, base_url, key_val)}")
    return out


def _cmd_failover_test(args) -> int:
    """测试 failover 备选链: 解析当前首选之后的备选 + 逐个 **live 探活** (只读), 报告切换顺序。

    --models: 额外逐个链节点 (含首选/跳过项) 打印实际端点配置 (provider/端点/key/模型档) + 拉可用模型。

    回答"首选网关 (aigw) 挂了会切到哪、备选到底通不通":
      1. 读 .env 的 failover 开关 + 激活 profile (首选)。
      2. resolve_chain 解析链: 列可执行备选 (按序) + 跳过项 (原因: 同首选/冷却/缺 key/CLI 型…)。
      3. 对每个备选跑 default_probe (openai 列 /v1/models, anthropic 发 1-token ping) — 只读探连通+鉴权。
      4. 判定: 首选故障时会切到"第一个探活 ✓"的备选。
    退出码非零 = 无可用备选 / 全部探活失败 (链形同虚设), 便于挂巡检。"""
    import llm_failover as lf
    profiles_path = Path(args.profiles)
    env_path = Path(args.env_file)
    lines = read_env_lines(env_path)
    preferred = active_profile(lines)
    fo_raw = (env_value(lines, "BOSS_LLM_FAILOVER") or "").strip()
    fo_on = fo_raw.lower() in {"1", "true", "yes", "on"}

    print("failover 链测试 (只读 · 逐个探活每个备选端点)\n")
    print(f"  开关   {'开' if fo_on else '关 ⚠ (BOSS_LLM_FAILOVER 未开 → 链已配但运行期不会自动切)'}")
    print(f"  首选   {preferred or '(无激活标记; 全链都会被当作备选试)'}")

    plan = lf.resolve_chain(preferred=preferred, profiles_path=profiles_path, env_path=env_path)
    if plan.skipped:
        print("\n  跳过的链节点:")
        for name, reason in plan.skipped:
            print(f"    ⊘ {name:12s} {reason}")
    rc = 0
    if not plan.candidates:
        print("\n  ✗ 无可用备选 —— 首选挂了没得切。检查各 profile 的 key/base_url "
              "(见 config/llm_profiles.yaml failover.chain + .env)。", file=sys.stderr)
        rc = 1
    else:
        print(f"\n  备选执行顺序 (共 {len(plan.candidates)} 个 · 逐个 live 探活):")
        first_ok: Optional[str] = None
        n_ok = 0
        for i, cand in enumerate(plan.candidates, 1):
            t0 = time.monotonic()
            try:
                ok, detail = lf.default_probe(cand)
            except Exception as e:  # noqa: BLE001 — 探活异常也算失败, 不中断整条链测试
                ok, detail = False, f"{type(e).__name__}: {str(e)[:80]}"
            dt = time.monotonic() - t0
            if ok:
                n_ok += 1
                if first_ok is None:
                    first_ok = cand.profile
            print(f"    {i}. {'✓' if ok else '✗'} {cand.profile:12s} {cand.provider:18s} "
                  f"{cand.model_deep:32s} {dt:4.1f}s  {detail}")
        print()
        if first_ok:
            print(f"  ✅ 首选 ({preferred or '?'}) 端点级故障 (429/5xx/超时/WAF/401) 时 → 自动切到"
                  f"第一个可用备选: {first_ok}  ({n_ok}/{len(plan.candidates)} 个备选探活通过)")
            if not fo_on:
                print("  ⚠ 但 BOSS_LLM_FAILOVER 现在是关的 → 运行期不会真的切。"
                      "开: .env 设 BOSS_LLM_FAILOVER=1 + 重启 boss-review-worker / boss-feishu-ws。")
        else:
            print("  ✗ 所有备选都探活失败 —— 链形同虚设。检查备选网关的 key/连通性。", file=sys.stderr)
            rc = 1

    # --models: 逐个链节点的实际端点配置 + 可用模型 (含首选/跳过项, 便于核对与诊断)
    if getattr(args, "models", False):
        for ln in _chain_detail_lines(profiles_path=profiles_path, lines=lines,
                                      preferred=preferred, plan=plan, fetch_models=True):
            print(ln)

    return rc


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="llm_switch.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", default=str(DEFAULT_ENV), help="目标 .env (默认 vault/.env)")
    ap.add_argument("--profiles", default=str(DEFAULT_PROFILES), help="profile 文件")
    sub = ap.add_subparsers(dest="cmd")   # 不强制 — 无子命令默认进交互菜单
    sub.add_parser("menu", help="交互式菜单 (傻瓜化切换; 无参数也默认进此)")
    sub.add_parser("list", help="列 profile + 当前激活")
    pm = sub.add_parser("models", help="列某 profile 端点上的可用模型 (/v1/models)")
    pm.add_argument("name", nargs="?", help="profile 名 (省略=当前激活)")
    sub.add_parser("show", help="看当前 BOSS_LLM_* (key 打码)")
    sub.add_parser("status", help="一屏看清现网各通道生效模型 + 解析逻辑 + per-scene/failover (只读)")
    sub.add_parser("doctor", help="体检所有 profile 端点 (连通/鉴权/模型可用, 只读)")
    pf = sub.add_parser("failover-test", help="测试 failover 备选链: 解析链 + 逐个 live 探活 + 报告切换顺序 (只读)")
    pf.add_argument("--models", action="store_true",
                    help="额外逐个链节点列实际端点配置 (provider/端点/key/模型档) + 拉可用模型 (/v1/models)")
    pu = sub.add_parser("use", help="切换到指定 profile")
    pu.add_argument("name", help="profile 名 (见 list)")
    pu.add_argument("--model", help="同时覆盖 fast+deep 模型 (端点上任意模型名, 如 gpt-5.5)")
    pu.add_argument("--model-fast", help="覆盖 Phase 1-3 模型 (起草/调研/合成)")
    pu.add_argument("--model-deep", help="覆盖 Phase 4-5 模型 (评委打分/合议)")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.cmd or "menu"   # 无子命令 → 交互菜单
    try:
        return {"menu": _cmd_menu, "list": _cmd_list, "models": _cmd_models,
                "show": _cmd_show, "status": _cmd_status, "doctor": _cmd_doctor,
                "failover-test": _cmd_failover_test, "use": _cmd_use}[cmd](args)
    except SwitchError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
