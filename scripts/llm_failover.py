#!/usr/bin/env python3
"""llm_failover.py — 跨 profile 自动故障切换核心模块 (PRD prd-llm-failover · M1)

三件事 (纯逻辑, M1 不接线 — llm_client 到 M2 才 import 本模块):

1. ``CooldownRegistry`` — 进程级线程安全冷却注册表。profile 硬失败后冷却
   (默认 300s; 鉴权失败 30min, 密钥失效不会自愈), 冷却中被 resolve_chain 跳过,
   避免 Phase 4 并发 5+ 评委反复撞死端点。内存态, 进程重启即清 (SR-3)。
2. ``resolve_chain()`` — 从 config/llm_profiles.yaml 的 ``failover.chain`` 产出
   备选执行计划: 去首选重复 / 去链内重复 / 去冷却中 / 去不合格 (未知 profile /
   缺 provider / CLI 型 provider / 缺 key)。跳过项带原因返回, 供诊断与日志。
3. ``profile_call_args()`` — 把 profile 展开成 ``llm_client.complete()`` 可直接用的
   调用参数 (provider / base_url / api_key / 该 profile **自己的** model_fast|deep)。
   各网关模型名不同, failover 绝不把当前模型名带去下一个端点 (PRD §5.1)。

纪律 (PRD D1/SR-3): 本模块**绝不写 .env** — 自动切换是 per-call 内存态,
手工切换 (llm_switch/apply_profile) 仍是唯一"首选"真相源。

复用 llm_switch 的 profile/env 解析 (load_profiles / env_value / expand /
_clean_model_name), 不重复造轮子。

开关 (M2 接线时由 llm_client 读):
  BOSS_LLM_FAILOVER=1                       # 默认 0 = 关 (SR-1 bit 级等价)
  BOSS_LLM_FAILOVER_COOLDOWN_SEC=300        # 端点级失败冷却
  BOSS_LLM_FAILOVER_AUTH_COOLDOWN_SEC=1800  # 鉴权失败长冷却 (D2)
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import llm_switch
from llm_switch import (
    DEFAULT_ENV,
    DEFAULT_PROFILES,
    SwitchError,
    _clean_model_name,
    env_value,
    expand,
    read_env_lines,
)

# CLI 型 provider 不入链 (OQ2 定案: 行为差异大, 本期只支持 API 型)
_CLI_PROVIDERS = {"codex-cli", "kimi-cli"}

DEFAULT_COOLDOWN_SEC = 300.0
DEFAULT_AUTH_COOLDOWN_SEC = 1800.0


def failover_enabled() -> bool:
    """BOSS_LLM_FAILOVER 开关 (默认关, SR-1)。"""
    return (os.environ.get("BOSS_LLM_FAILOVER") or "").strip().lower() in {"1", "true", "yes", "on"}


def cooldown_sec() -> float:
    return float(os.environ.get("BOSS_LLM_FAILOVER_COOLDOWN_SEC") or DEFAULT_COOLDOWN_SEC)


def auth_cooldown_sec() -> float:
    return float(os.environ.get("BOSS_LLM_FAILOVER_AUTH_COOLDOWN_SEC") or DEFAULT_AUTH_COOLDOWN_SEC)


# ─── 冷却注册表 ─────────────────────────────────────────────────────

class CooldownRegistry:
    """profile → 冷却截止时刻 (monotonic)。线程安全; 进程内存态, 重启即清 (SR-3)。

    时钟可注入 (now 参数) 便于测试; 生产走 time.monotonic。
    """

    def __init__(self) -> None:
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def mark(self, profile: str, seconds: float, *, now: Optional[float] = None) -> None:
        """记冷却。并发多线程同时失败: 取更晚的截止 (只延不缩), 天然幂等无惊群。"""
        t = time.monotonic() if now is None else now
        until = t + max(0.0, seconds)
        with self._lock:
            if until > self._until.get(profile, 0.0):
                self._until[profile] = until

    def is_cooling(self, profile: str, *, now: Optional[float] = None) -> bool:
        t = time.monotonic() if now is None else now
        with self._lock:
            return t < self._until.get(profile, 0.0)

    def remaining(self, profile: str, *, now: Optional[float] = None) -> float:
        t = time.monotonic() if now is None else now
        with self._lock:
            return max(0.0, self._until.get(profile, 0.0) - t)

    def cooling_profiles(self, *, now: Optional[float] = None) -> list[str]:
        """冷却中的 profile 名单 (供 /model 状态行 · doctor 展示)。"""
        t = time.monotonic() if now is None else now
        with self._lock:
            return sorted(p for p, u in self._until.items() if t < u)

    def clear(self) -> None:
        with self._lock:
            self._until.clear()


# 进程级单例 (M2 接线用; 测试各自 new 不共享)
COOLDOWNS = CooldownRegistry()


# ─── per-job 会话累加 (R8 报告 frontmatter 打标) ────────────────────
# run_pipeline_local 每 job 一个进程, 故进程级单例即 per-job。job 起手 reset_session(),
# 每次成功调用 note_model(实际 model), 发生 failover 时 note_failover()。
# 报告 frontmatter 据此打标 (发生过 failover → models_used + failover: true)。

class _Session:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._models: list[str] = []      # 保序去重 (首用在前)
        self._failover = False

    def reset(self) -> None:
        with self._lock:
            self._models = []
            self._failover = False

    def note_model(self, model: Optional[str]) -> None:
        if not model:
            return
        with self._lock:
            if model not in self._models:
                self._models.append(model)

    def note_failover(self) -> None:
        with self._lock:
            self._failover = True

    def summary(self) -> dict:
        with self._lock:
            return {"failover": self._failover, "models_used": list(self._models)}


SESSION = _Session()


def reset_session() -> None:
    SESSION.reset()


def note_model(model: Optional[str]) -> None:
    SESSION.note_model(model)


def note_failover() -> None:
    SESSION.note_failover()


def session_summary() -> dict:
    """{failover: bool, models_used: [实际用过的 model 保序去重]}。供报告 frontmatter 打标。"""
    return SESSION.summary()


# ─── profile → 调用参数 ────────────────────────────────────────────

@dataclass(frozen=True)
class ProfileCallArgs:
    """llm_client.complete() 可直接用的一组调用参数 (per-call 内存态, 不落 .env)。"""
    profile: str
    provider: str
    base_url: Optional[str]
    api_key: Optional[str]        # None = 该 provider 不需要 key (本期链内不会出现)
    model_fast: str
    model_deep: str


class IneligibleProfile(SwitchError):
    """profile 不具备入链资格 (缺 key / CLI 型 / 配置坏)。reason 供跳过日志。"""

    def __init__(self, profile: str, reason: str):
        super().__init__(f"profile {profile} 不入链: {reason}")
        self.profile = profile
        self.reason = reason


def profile_call_args(name: str, *, profiles: dict[str, dict],
                      env_lines: list[str]) -> ProfileCallArgs:
    """展开 profile 为调用参数。不合格抛 IneligibleProfile (resolve_chain 捕获降为跳过)。

    与 llm_switch.apply_profile 同一套解析 (expand ${VAR} / 剥反引号 / key 现读),
    区别只在: 这里**不写 .env**, 产出内存态参数 (PRD D1)。
    """
    p = profiles.get(name)
    if p is None:
        raise IneligibleProfile(name, "未知 profile")
    p = p or {}

    provider = str(p.get("provider") or "").strip()
    if not provider:
        raise IneligibleProfile(name, "缺 provider")
    if provider in _CLI_PROVIDERS:
        raise IneligibleProfile(name, f"CLI 型 provider ({provider}) 本期不入链")

    base_url = expand(str(p.get("base_url") or ""), env_lines).strip() or None
    model_fast = _clean_model_name(expand(str(p.get("model_fast") or ""), env_lines))
    model_deep = _clean_model_name(expand(str(p.get("model_deep") or ""), env_lines))
    if not (model_fast or model_deep):
        raise IneligibleProfile(name, "缺 model_fast/model_deep")
    # 只配了一个时互为兜底 (与手工切换后单模型运行的行为一致)
    model_fast = model_fast or model_deep
    model_deep = model_deep or model_fast

    key_env = str(p.get("api_key_env") or "").strip()
    api_key = env_value(env_lines, key_env) if key_env else None
    if not api_key:
        raise IneligibleProfile(name, f"缺 key (.env 未配 {key_env or 'api_key_env'})")

    return ProfileCallArgs(profile=name, provider=provider, base_url=base_url,
                           api_key=api_key, model_fast=model_fast, model_deep=model_deep)


# ─── 备选链解析 ────────────────────────────────────────────────────

def load_failover_chain(path: Path = DEFAULT_PROFILES) -> list[str]:
    """读 llm_profiles.yaml 顶层 failover.chain。缺段/文件缺失 → [] (fail-safe, SR-5)。"""
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, OSError, yaml.YAMLError):
        # 声称 fail-safe [] 却只 catch FileNotFoundError → 畸形 YAML 会打崩 failover 路径
        return []
    chain = ((data.get("failover") or {}).get("chain")) or []
    if not isinstance(chain, list):
        return []
    return [str(x).strip() for x in chain if str(x).strip()]


@dataclass
class ChainPlan:
    """resolve_chain 的产出: 可执行备选 + 跳过明细 (供日志/诊断/doctor)。"""
    candidates: list[ProfileCallArgs] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (profile, 原因)


def resolve_chain(*, preferred: Optional[str],
                  profiles_path: Path = DEFAULT_PROFILES,
                  env_path: Path = DEFAULT_ENV,
                  registry: Optional[CooldownRegistry] = None,
                  now: Optional[float] = None) -> ChainPlan:
    """产出首选之后的备选执行计划。

    跳过规则 (均降为 skipped 记原因, 绝不抛 — SR-5 链配置坏不连坐首选):
      与首选同名 / 链内重复 / 冷却中 / 未知 profile / 缺 provider / CLI 型 / 缺 key。
    """
    reg = registry if registry is not None else COOLDOWNS
    plan = ChainPlan()
    chain = load_failover_chain(profiles_path)
    if not chain:
        return plan

    try:
        profiles = llm_switch.load_profiles(profiles_path)
    except SwitchError as exc:
        plan.skipped.append(("*", f"profiles 加载失败: {exc}"))
        return plan
    env_lines = read_env_lines(env_path)

    seen: set[str] = set()
    for name in chain:
        if preferred and name == preferred:
            plan.skipped.append((name, "与首选相同"))
            continue
        if name in seen:
            plan.skipped.append((name, "链内重复"))
            continue
        seen.add(name)
        if reg.is_cooling(name, now=now):
            plan.skipped.append((name, f"冷却中 (剩 {reg.remaining(name, now=now):.0f}s)"))
            continue
        try:
            plan.candidates.append(profile_call_args(name, profiles=profiles, env_lines=env_lines))
        except IneligibleProfile as exc:
            plan.skipped.append((name, exc.reason))
        except SwitchError as exc:            # expand ${VAR} 缺变量等配置坏
            plan.skipped.append((name, str(exc)))
    return plan


# ─── 健康巡检 (llm doctor) ──────────────────────────────────────────

@dataclass
class ProfileHealth:
    profile: str
    ok: bool
    detail: str
    skipped: bool = False     # True = 配置性跳过 (缺 key / CLI 型), 非"探测失败"


def default_probe(args: "ProfileCallArgs") -> tuple[bool, str]:
    """默认探针: openai 兼容端点列 /v1/models (最省、测连通+鉴权); anthropic 系发 1-token ping。
    只读, 不改任何状态。异常 → (False, 精简原因)。"""
    try:
        if args.provider == "openai-compatible":
            import llm_switch
            # raise_on_error=True: 鉴权失败 (401/403) 抛出 → 判为探活失败, 不再被吞成 '0 模型可用'
            # (否则用错 key 的死端点会误报成 ✓, 让 failover 链虚假通过)。
            models = llm_switch.fetch_gateway_models(args.base_url or "", args.api_key,
                                                     raise_on_error=True)
            if not models:
                return True, "0 模型可用 ⚠ 端点未列出模型, 无法确认目标模型是否真可用"
            return True, f"{len(models)} 模型可用"
        import llm_client
        llm_client._complete_endpoint(
            provider=args.provider, api_key=args.api_key or "", base_url=args.base_url,
            model=args.model_deep, system="", user="ping", max_tokens=1)
        return True, "ping ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:100]}"


def doctor(*, profiles_path: Path = DEFAULT_PROFILES, env_path: Path = DEFAULT_ENV,
           probe=default_probe) -> list[ProfileHealth]:
    """遍历所有 profile 做只读体检 (连通/鉴权/模型可用)。CLI 型 / 缺 key → 标 skipped。
    probe 可注入 (测试用 mock)。"""
    profiles = llm_switch.load_profiles(profiles_path)
    env_lines = read_env_lines(env_path)
    out: list[ProfileHealth] = []
    for name in sorted(profiles):
        try:
            args = profile_call_args(name, profiles=profiles, env_lines=env_lines)
        except IneligibleProfile as exc:
            out.append(ProfileHealth(name, ok=False, detail=exc.reason, skipped=True))
            continue
        ok, detail = probe(args)
        out.append(ProfileHealth(name, ok=ok, detail=detail))
    return out


def _failover_enabled_from_env(env_path: Path) -> bool:
    """从 .env **文件**读 BOSS_LLM_FAILOVER 开关 (不读 os.environ)。

    供 CLI / 展示用: 交互 shell 里没 source .env 时 os.environ 无此值, 读文件才反映
    "服务重启后会用的" 真实状态 (与 llm status / failover-test 同口径)。服务进程内
    os.environ 已被 systemd 从 .env 注入, 二者一致。"""
    val = (env_value(read_env_lines(env_path), "BOSS_LLM_FAILOVER") or "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def failover_status_line(*, profiles_path: Path = DEFAULT_PROFILES,
                         registry: Optional[CooldownRegistry] = None,
                         env_path: Optional[Path] = None) -> str:
    """给 /model · doctor 用的一行 failover 状态摘要 (开关 · 链 · 冷却中)。

    env_path 给定 → 从 .env **文件**读开关 (CLI 展示: 避免"写了 .env 但当前 shell 没
    source" 时误显示成关); 缺省 → 读 os.environ (服务进程内 systemd 已注入, 正确)。"""
    reg = registry if registry is not None else COOLDOWNS
    enabled = _failover_enabled_from_env(env_path) if env_path is not None else failover_enabled()
    if not enabled:
        return "Failover: 关 (BOSS_LLM_FAILOVER=0)"
    chain = load_failover_chain(profiles_path)
    chain_str = " → ".join(chain) if chain else "(未配 failover.chain)"
    cooling = reg.cooling_profiles()
    cool_str = ", ".join(cooling) if cooling else "无"
    return f"Failover: 开 · 链 {chain_str} · 冷却中: {cool_str}"
