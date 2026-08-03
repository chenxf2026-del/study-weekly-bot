"""Provider-neutral LLM calls for the boss local pipeline.

Supported providers:
- anthropic: Anthropic Messages API, defaulting to ANTHROPIC_API_KEY.
- anthropic-compatible: Anthropic SDK with a custom base_url, e.g. Zhipu Claude-compatible API.
- openai-compatible: OpenAI Chat Completions API with a custom base_url, e.g. Kimi or Zhipu OpenAI-compatible API.
- codex-cli: Spawn `codex exec` as an ephemeral read-only subprocess and use its last message as the response.
- kimi-cli: Spawn `kimi -p` non-interactively and parse the first stream-json line.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


DEFAULT_PROVIDER = "anthropic"
SUPPORTED_PROVIDERS = {"anthropic", "anthropic-compatible", "openai-compatible", "codex-cli", "kimi-cli"}

# 429 限流 / 5xx / 超时 退避重试 (Phase 4 并发派多评委时, GLM 等端点易触发 429)
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "5"))
LLM_RETRY_BASE_SEC = float(os.environ.get("LLM_RETRY_BASE_SEC", "2.0"))
LLM_RETRY_MAX_SEC = float(os.environ.get("LLM_RETRY_MAX_SEC", "30.0"))


def _request_timeout_sec() -> float:
    """单次 SDK 请求超时 (秒)。默认 240 — 远高于网关合法生成 (~60s, §15) 又能掐断真挂死;
    没有它 anthropic/openai SDK 默认 ~600s/次, 挂死请求会长占 persona fast-lane 线程池
    格子、把该 bot 饿死 (#126 §1)。超时是可重试错误 → 每次尝试受此约束, 总时长有界。
    每次读环境, 便于运维/测试运行时调 (BOSS_LLM_TIMEOUT_SEC)。"""
    try:
        return float(os.environ.get("BOSS_LLM_TIMEOUT_SEC", "240"))
    except ValueError:
        return 240.0

# 进程级"调用间最小间隔"节流 (2026-06-30 op2 生产: 网关前置 WAF 按"每分钟请求数"频控,
# 一整场评审的密集调用跑到中段被拦)。把相邻两次 LLM 请求摊开到至少隔 N 秒, 压低瞬时速率,
# 避开 WAF 阈值。默认 0 = 关闭 (零行为变化); 运维按网关频控严程度调大 (如 1.5)。
LLM_MIN_INTERVAL_SEC = float(os.environ.get("LLM_MIN_INTERVAL_SEC", "0"))
_throttle_lock = threading.Lock()
_last_call_monotonic = 0.0


def _throttle_before_call() -> None:
    """节流闸: 保证相邻两次真实 LLM 调用至少隔 LLM_MIN_INTERVAL_SEC 秒。

    线程安全 (Phase 2/4 用 asyncio.to_thread 多线程并发调用)。持锁期间 sleep,
    把并发请求自然串成"每 N 秒一发", 这正是避开每分钟频控所需。默认 0 立即返回。
    """
    if LLM_MIN_INTERVAL_SEC <= 0:
        return
    global _last_call_monotonic
    with _throttle_lock:
        now = time.monotonic()
        wait = _last_call_monotonic + LLM_MIN_INTERVAL_SEC - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_call_monotonic = now

# 网关前置 WAF / 安全防护拦截特征 (2026-06-30 op2 生产: 网关被自家 WAF 限流,
# 回 HTTP 200 + 一张 HTML 挑战页而非模型 JSON, SDK 解析失败把整页 HTML 抛进异常)。
# 这些 token 在正常的战略评审输出里几乎不可能出现, 命中即判定为 WAF 拦截 (而非误伤模型正文)。
_WAF_BLOCK_MARKERS = (
    "damddos",            # 抗 D / WAF 服务域名特征
    "waf-daq",
    "wafhost",
    "wafid",
    "工程师在快马加鞭",     # 拦截页固定文案
    "网络开小差",
)


def _looks_like_waf_block(text: str) -> bool:
    """文本 (应答正文 或 异常消息) 疑似 WAF/安全防护拦截页, 而非模型正常输出。

    只认 WAF 专属强特征 (域名 / 固定文案), 不认裸 `<html>`/`<script>` ——
    后者在讨论 web 安全的评审正文里可能合法出现, 避免误伤。
    """
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in _WAF_BLOCK_MARKERS)


# WAF 页面里可交给网关运维精确定位被拦请求的取证线索:
#   - wafId: 拦截页给每次拦截分配的 ID, 运维可据此在 WAF 日志里查到源 IP / 命中规则
#   - 页面提及的 IPv4: 有的拦截页会回显"您的访问 IP", 直接就是网关看到的真实源 IP
# 元素形态多变 (id="wafId">值< / value="值" / "wafId":"值"), 多模式尽力匹配, 匹配不到就算了。
_WAFID_RES = (
    re.compile(r"""id=["']wafId["'][^>]*>\s*([A-Za-z0-9._\-]{4,})""", re.I),
    re.compile(r"""id=["']wafId["'][^>]*value=["']([A-Za-z0-9._\-]{4,})["']""", re.I),
    re.compile(r"""["']?wafId["']?\s*[:=]\s*["']([A-Za-z0-9._\-]{4,})["']""", re.I),
)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _waf_forensics(text: str) -> str:
    """从 WAF 拦截页 / 异常文本里抽取给运维定位用的线索 (wafId + 页面回显 IP)。

    返回可直接拼进诊断的一句串 (无线索则空串)。纯字符串处理, 不做 IO, 匹配不到零副作用。
    """
    if not text:
        return ""
    hints: list[str] = []
    for rx in _WAFID_RES:
        m = rx.search(text)
        if m:
            hints.append(f"wafId={m.group(1)}")
            break
    ips = [ip for ip in _IPV4_RE.findall(text)
           if not ip.startswith(("127.", "0.")) and all(int(o) < 256 for o in ip.split("."))]
    if ips:
        # 去重保序, 最多 3 个, 避免把页面里无关 IP (如 CDN) 全塞进来
        seen: list[str] = []
        for ip in ips:
            if ip not in seen:
                seen.append(ip)
        hints.append("页面IP=" + ",".join(seen[:3]))
    return " ".join(hints)


def _is_retryable_error(exc: Exception) -> bool:
    """判断 LLM 调用异常是否可重试: 429 限流 / 5xx 服务端 / 超时 / 连接 / overloaded / WAF 拦截。"""
    # 0. WAF/防护拦截 (网关回 HTML 挑战页) — 多为临时限流, 可重试
    #    a) 我们主动检出的 200+HTML body (WAFBlockError)  b) SDK 把 HTML 抛进异常消息
    if isinstance(exc, WAFBlockError) or _looks_like_waf_block(str(exc)):
        return True
    # 1. status_code (openai/anthropic APIStatusError, httpx.HTTPStatusError)
    sc = getattr(exc, "status_code", None)
    if sc is None:
        sc = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(sc, int) and (sc == 429 or 500 <= sc < 600):
        return True
    # 2. 异常类名 (RateLimitError / APITimeoutError / APIConnectionError / InternalServerError)
    name = type(exc).__name__.lower()
    if any(k in name for k in ("ratelimit", "timeout", "apiconnection", "overloaded", "serviceunavailable")):
        return True
    # 3. 消息兜底 (provider 不规范时)
    msg = str(exc).lower()
    return any(k in msg for k in ("429", "rate limit", "too many requests", "overloaded"))


def _is_auth_error(exc: Exception) -> bool:
    """鉴权失败 (401/403 / key 无效)。failover 视为端点级 (密钥失效不自愈, D2 长冷却)。"""
    sc = getattr(exc, "status_code", None)
    if sc is None:
        sc = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(sc, int) and sc in (401, 403):
        return True
    name = type(exc).__name__.lower()
    if any(k in name for k in ("authentication", "permissiondenied")):
        return True
    msg = str(exc).lower()
    return any(k in msg for k in (
        "401", "403", "authentication", "invalid api key", "invalid_api_key",
        "api key", "unauthorized", "key_model_access_denied", "permission denied"))


def _is_endpoint_level(exc: Exception) -> bool:
    """端点/网关/鉴权级故障 (换端点可能有救) → 触发 failover。
    与之相对的是"请求内容问题" (400/404/模型名非法 等), 换端点无用, 不切。"""
    return _is_retryable_error(exc) or _is_auth_error(exc)


class LLMClientError(RuntimeError):
    """Raised when the configured LLM backend cannot be called."""


class WAFBlockError(LLMClientError):
    """应答疑似被网关前置 WAF/安全防护拦截 (HTML 挑战页而非模型 JSON)。可重试。

    body 保留原始拦截页全文 (未截断), 供 _waf_forensics 抽取 wafId / 回显 IP 等
    定位线索; 我们主动检出 200+HTML 时能拿到全文, 比 SDK 抛异常时截断的消息更完整。
    """

    def __init__(self, message: str, body: str = ""):
        super().__init__(message)
        self.body = body


@dataclass(frozen=True)
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    provider: str
    model: str


def resolve_provider(value: Optional[str] = None) -> str:
    provider = (value or os.environ.get("BOSS_LLM_PROVIDER") or DEFAULT_PROVIDER).strip()
    if provider not in SUPPORTED_PROVIDERS:
        raise LLMClientError(
            f"不支持的 BOSS_LLM_PROVIDER={provider!r}; "
            f"可选: {', '.join(sorted(SUPPORTED_PROVIDERS))}"
        )
    return provider


def resolve_api_key(provider: str, api_key_env: Optional[str] = None) -> str:
    if provider in {"codex-cli", "kimi-cli"}:
        return ""

    env_names = []
    if api_key_env:
        env_names.append(api_key_env)
    env_names.append("BOSS_LLM_API_KEY")
    if provider in {"anthropic", "anthropic-compatible"}:
        env_names.extend(["ANTHROPIC_API_KEY", "ZAI_API_KEY", "ZHIPU_API_KEY"])
    elif provider == "openai-compatible":
        env_names.extend(["OPENAI_API_KEY", "KIMI_API_KEY", "MOONSHOT_API_KEY", "ZAI_API_KEY", "ZHIPU_API_KEY"])

    for name in env_names:
        value = os.environ.get(name)
        if value:
            return value

    raise LLMClientError(
        "LLM API key 未设. 请设置 BOSS_LLM_API_KEY, 或使用 --llm-api-key-env 指定已有环境变量。"
    )


def resolve_base_url(provider: str, base_url: Optional[str] = None) -> Optional[str]:
    if provider in {"codex-cli", "kimi-cli"}:
        return None

    if base_url:
        return base_url
    value = os.environ.get("BOSS_LLM_BASE_URL")
    if value:
        return value
    if provider in {"anthropic", "anthropic-compatible"}:
        return os.environ.get("ANTHROPIC_BASE_URL")
    if provider == "openai-compatible":
        return os.environ.get("OPENAI_BASE_URL")
    return None


def _complete_endpoint(
    *,
    provider: str,
    api_key: str,
    base_url: Optional[str],
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: Optional[float] = None,
) -> LLMResult:
    """单端点调用 (含既有退避重试环)。provider/api_key/base_url 均**已解析**。

    failover (跨端点) 在 complete() 一层; 本函数只负责"一个端点尽力打通"。
    temperature=None → 不传 (API 默认); 用于评审可复现 (低温降分数漂移)。CLI 通道不支持, 忽略。
    """
    def _dispatch() -> LLMResult:
        if provider in {"anthropic", "anthropic-compatible"}:
            return _complete_anthropic(
                provider=provider, api_key=api_key, base_url=base_url,
                model=model, system=system, user=user, max_tokens=max_tokens,
                temperature=temperature,
            )
        if provider == "openai-compatible":
            return _complete_openai_compatible(
                api_key=api_key, base_url=base_url,
                model=model, system=system, user=user, max_tokens=max_tokens,
                temperature=temperature,
            )
        if provider == "codex-cli":
            return _complete_codex_cli(model=model, system=system, user=user, max_tokens=max_tokens)
        if provider == "kimi-cli":
            return _complete_kimi_cli(model=model, system=system, user=user, max_tokens=max_tokens)
        raise AssertionError(f"unreachable provider: {provider}")

    # 429 / 5xx / 超时 / WAF 拦截 指数退避重试 (Phase 4 并发派多评委时易触发 GLM 限流 / 网关 WAF 风控)
    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            _throttle_before_call()   # 每次真实调用前过节流闸 (摊平瞬时速率, 避开 WAF 频控)
            return _dispatch()
        except Exception as exc:  # noqa: BLE001 — 仅可重试错误重试, 其余原样抛出
            if attempt >= LLM_MAX_RETRIES or not _is_retryable_error(exc):
                # WAF 拦截耗尽重试: 抛干净的一句话诊断 (不灌整页 HTML), 抽 wafId / 回显 IP 供运维定位。
                # 抛 WAFBlockError (LLMClientError 子类) 而非裸 LLMClientError, 好让 failover 层
                # 用 isinstance 判定为端点级 → 触发切换 (外部 `except LLMClientError` 仍照常捕获)。
                if isinstance(exc, WAFBlockError) or _looks_like_waf_block(str(exc)):
                    forensics = _waf_forensics(getattr(exc, "body", "") or str(exc))
                    hint = f" 定位线索: [{forensics}]。" if forensics else ""
                    raise WAFBlockError(
                        "网关被 WAF/安全防护拦截 (返回 HTML 挑战页, 非模型问题)。"
                        "多为临时限流或出口 IP 触发风控; 稍后重试, 或给网关侧加白 VM 出口 IP, "
                        "或用 /model 切到不在该 WAF 后的网关。" + hint,
                        body=getattr(exc, "body", "") or str(exc),
                    ) from exc
                raise
            delay = min(LLM_RETRY_BASE_SEC * (2 ** attempt), LLM_RETRY_MAX_SEC) + random.uniform(0, 1)
            time.sleep(delay)
    raise AssertionError("unreachable: retry loop exhausted")


def _model_slot(model: str) -> str:
    """判断传入 model 是 fast 还是 deep 槽 (failover 到备选端点时取同槽模型名)。默认 deep。"""
    if model and model == (os.environ.get("BOSS_LLM_MODEL_FAST") or "").strip():
        return "fast"
    return "deep"


def _active_profile_name() -> Optional[str]:
    """当前激活 profile 名 (.env 里 llm_switch 写的标记); 取不到返回 None。failover 用它去链里排重。"""
    try:
        import llm_switch
        return llm_switch.active_profile(llm_switch.read_env_lines(llm_switch.DEFAULT_ENV))
    except Exception:  # noqa: BLE001 — 取不到就当无名首选
        return None


def _match_profile_name(*, provider: str, base_url: Optional[str]) -> Optional[str]:
    """无 active 标记时, 按 (provider, base_url) 反查首选对应的 profile 名。

    没有它 failover 只能把冷却记在占位名 "(preferred)" 上, 且 resolve_chain(preferred=None)
    无法把刚失败的首选从备选链里排除 → 同一个坏端点被立即重试。反查到真名后, 冷却记对键、
    备选链也能正确排除它。查不到 (真无名 / 配置异常) 返回 None, 退回占位行为。"""
    try:
        import llm_switch
        profiles = llm_switch.load_profiles()
        env_lines = llm_switch.read_env_lines(llm_switch.DEFAULT_ENV)
    except Exception:  # noqa: BLE001
        return None
    want = (base_url or "").strip().rstrip("/")
    for name, p in (profiles or {}).items():
        p = p or {}
        if str(p.get("provider") or "").strip() != provider:
            continue
        try:
            pb = llm_switch.expand(str(p.get("base_url") or ""), env_lines).strip().rstrip("/")
        except Exception:  # noqa: BLE001 — 单个 profile 配置坏不连坐整体反查
            continue
        if pb == want:
            return name
    return None


def _record_failover_event(*, from_name: Optional[str], to_name: str, reason: str) -> None:
    """记一条 failover 遥测事件 (fail-open, 绝不影响调用)。"""
    try:
        import telemetry
        telemetry.record_event(
            "llm_failover",
            scene=os.environ.get("BOSS_SCENE_SLUG") or None,
            job_id=os.environ.get("BOSS_JOB_ID") or None,
            **{"from": from_name or "(preferred)", "to": to_name, "reason": reason},
        )
    except Exception:  # noqa: BLE001
        pass


def _complete_with_failover(
    *,
    preferred_provider: str,
    preferred_key: str,
    preferred_base_url: Optional[str],
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: Optional[float] = None,
) -> LLMResult:
    """首选端点打不通 (端点级错误) 时, 按 failover 链自动试备选。

    纪律 (PRD): 只对端点级错误切换 (输出/请求内容问题原样抛); per-call 内存态,
    绝不写 .env; 每个备选用它自己的模型名; 硬失败记冷却 (鉴权长冷却)。
    """
    import llm_failover as lf

    # 优先用 .env 的 active 标记; 无标记时按 (provider, base_url) 反查真名,
    # 保证冷却记对键 + 备选链能排除刚失败的首选 (否则同一坏端点会被立即重试)。
    preferred_name = _active_profile_name() or _match_profile_name(
        provider=preferred_provider, base_url=preferred_base_url)
    try:
        res = _complete_endpoint(
            provider=preferred_provider, api_key=preferred_key, base_url=preferred_base_url,
            model=model, system=system, user=user, max_tokens=max_tokens, temperature=temperature,
        )
        lf.note_model(res.model)          # R8: 记实际用过的 model (报告 frontmatter 打标)
        return res
    except Exception as exc:  # noqa: BLE001
        if not _is_endpoint_level(exc):
            raise   # 请求内容问题 (400/非法模型名等), 换端点无用
        cooldown = lf.auth_cooldown_sec() if _is_auth_error(exc) else lf.cooldown_sec()
        lf.COOLDOWNS.mark(preferred_name or "(preferred)", cooldown)
        plan = lf.resolve_chain(preferred=preferred_name)
        if not plan.candidates:
            raise   # 无可用备选 → 原样抛首选错误

        slot = _model_slot(model)
        last_exc: Exception = exc
        for cand in plan.candidates:
            cand_model = cand.model_fast if slot == "fast" else cand.model_deep
            try:
                res = _complete_endpoint(
                    provider=cand.provider, api_key=cand.api_key or "", base_url=cand.base_url,
                    model=cand_model, system=system, user=user, max_tokens=max_tokens,
                    temperature=temperature,
                )
                lf.note_failover()             # R8: 本 job 发生过 failover → 报告打标
                lf.note_model(res.model)
                _record_failover_event(
                    from_name=preferred_name, to_name=cand.profile,
                    reason=f"{type(last_exc).__name__}: {str(last_exc)[:120]}")
                return res
            except Exception as e2:  # noqa: BLE001
                if not _is_endpoint_level(e2):
                    raise
                cd = lf.auth_cooldown_sec() if _is_auth_error(e2) else lf.cooldown_sec()
                lf.COOLDOWNS.mark(cand.profile, cd)
                last_exc = e2
        skipped = "; ".join(f"{p}: {r}" for p, r in plan.skipped) or "无"
        raise LLMClientError(
            f"首选端点 + {len(plan.candidates)} 个备选端点均失败 (failover 链耗尽)。"
            f"末次错误: {last_exc}。跳过的链节点: [{skipped}]"
        ) from last_exc


def complete(
    *,
    provider: Optional[str],
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    base_url: Optional[str] = None,
    api_key_env: Optional[str] = None,
    temperature: Optional[float] = None,
) -> LLMResult:
    resolved_provider = resolve_provider(provider)
    api_key = resolve_api_key(resolved_provider, api_key_env)
    resolved_base_url = resolve_base_url(resolved_provider, base_url)

    # 默认关 (SR-1): failover 未开时, 路径与接线前 bit 级等价 (仅多一次 env 读)。
    try:
        import llm_failover
        failover_on = llm_failover.failover_enabled()
    except Exception:  # noqa: BLE001 — 模块缺失/异常 → 退回单端点, 绝不因 failover 拖垮调用
        failover_on = False

    if not failover_on:
        return _complete_endpoint(
            provider=resolved_provider, api_key=api_key, base_url=resolved_base_url,
            model=model, system=system, user=user, max_tokens=max_tokens, temperature=temperature,
        )
    return _complete_with_failover(
        preferred_provider=resolved_provider, preferred_key=api_key,
        preferred_base_url=resolved_base_url, model=model,
        system=system, user=user, max_tokens=max_tokens, temperature=temperature,
    )


def _complete_anthropic(
    *,
    provider: str,
    api_key: str,
    base_url: Optional[str],
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: Optional[float] = None,
) -> LLMResult:
    try:
        import anthropic
    except ImportError as exc:
        raise LLMClientError("anthropic SDK 未装. pip install anthropic") from exc

    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = anthropic.Anthropic(**kwargs)
    create_kwargs: dict[str, Any] = dict(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
        timeout=_request_timeout_sec(),      # per-request 超时: 挂死请求不再永占 worker (#126 §1)
    )
    if temperature is not None:              # 未设则不传 → 与接线前 bit 级等价 (API 默认)
        create_kwargs["temperature"] = temperature
    response = client.messages.create(**create_kwargs)
    # 安全取正文: content 为空列表时 content[0] 会 IndexError 崩栈; 空/纯空白同 openai
    # 通道降级为清晰 LLMClientError (stop_reason=max_tokens 时正文可能全被思考吃掉)。
    blocks = getattr(response, "content", None) or []
    text = blocks[0].text if (blocks and hasattr(blocks[0], "text")) else ""
    if not text.strip():
        sr = getattr(response, "stop_reason", None) or "?"
        raise LLMClientError(
            f"anthropic 应答内容为空 (stop_reason={sr}), 端点未产出可用补全")
    if _looks_like_waf_block(text):
        raise WAFBlockError("应答正文疑似 WAF 拦截页 (anthropic 通道)", body=text)
    return LLMResult(
        text=text,
        input_tokens=int(getattr(response.usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(response.usage, "output_tokens", 0) or 0),
        provider=provider,
        model=model,
    )


def _complete_openai_compatible(
    *,
    api_key: str,
    base_url: Optional[str],
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: Optional[float] = None,
) -> LLMResult:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise LLMClientError("openai SDK 未装. pip install 'openai>=1.0'") from exc

    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    create_kwargs: dict[str, Any] = dict(
        model=model, max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        timeout=_request_timeout_sec(),      # per-request 超时: 挂死请求不再永占 worker (#126 §1)
    )
    if temperature is not None:              # 未设则不传 → 与接线前 bit 级等价 (API 默认)
        create_kwargs["temperature"] = temperature
    response = client.chat.completions.create(**create_kwargs)
    # 空 choices 守卫: 网关偶发回 200 但 choices=[] (被拦/截断/端点异常), 直接
    # choices[0] 会 IndexError 打崩调用栈; 降级为清晰的 LLMClientError 交上层处理。
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise LLMClientError("openai-compatible 应答 choices 为空 (端点未返回任何补全)")
    text = choices[0].message.content or ""
    # 空内容守卫: 网关偶发回 200 + choice 但 content 为空/纯空白 (上游超时被吞成空 /
    # 内容过滤 / max_tokens 被思考 token 耗尽 → finish_reason=length 却无正文)。
    # 静默返回空文本会一路变成"发了卡但正文为空"的鬼卡 (2026-07-10 分身文档评议踩到);
    # 降级为清晰 LLMClientError 交上层 → persona 回「没接上话, 再发一次」而非空卡, 且入日志。
    if not text.strip():
        fr = getattr(choices[0], "finish_reason", None) or "?"
        raise LLMClientError(
            f"openai-compatible 应答内容为空 (finish_reason={fr}; 多为上游超时/内容过滤/"
            "max_tokens 被思考耗尽), 端点未产出可用补全")
    if _looks_like_waf_block(text):
        raise WAFBlockError("应答正文疑似 WAF 拦截页 (openai-compatible 通道)", body=text)
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    return LLMResult(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider="openai-compatible",
        model=model,
    )


# ─── 流式 (供飞书卡片逐步渲染: TTFT 快, 答案实时长出来) ────────────────

def complete_stream(
    *,
    provider: Optional[str],
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    base_url: Optional[str] = None,
    api_key_env: Optional[str] = None,
    on_delta: Optional[Callable[[str], None]] = None,
) -> LLMResult:
    """流式版 complete: 边生成边通过 `on_delta(累计文本)` 回调 (逐步渲染用)。

    仅 openai-compatible / anthropic 走**真流式** (aigw = openai-compatible); 其余 provider
    (codex-cli / kimi-cli 等) 优雅退化为非流式 (末尾 on_delta 一次)。返回最终 LLMResult。
    **不走 failover** (流式聚焦单端点); 任何异常抛给调用方 —— persona 侧 catch 后回退非流式。"""
    resolved_provider = resolve_provider(provider)
    api_key = resolve_api_key(resolved_provider, api_key_env)
    resolved_base_url = resolve_base_url(resolved_provider, base_url)
    if resolved_provider in ("openai-compatible", "openai"):
        return _stream_openai_compatible(
            api_key=api_key, base_url=resolved_base_url, model=model,
            system=system, user=user, max_tokens=max_tokens, on_delta=on_delta)
    if resolved_provider == "anthropic":
        return _stream_anthropic(
            provider=resolved_provider, api_key=api_key, base_url=resolved_base_url,
            model=model, system=system, user=user, max_tokens=max_tokens, on_delta=on_delta)
    # 无流式能力的 provider → 非流式兜底, 末尾回调一次
    res = _complete_endpoint(
        provider=resolved_provider, api_key=api_key, base_url=resolved_base_url,
        model=model, system=system, user=user, max_tokens=max_tokens)
    if on_delta:
        on_delta(res.text)
    return res


def _stream_openai_compatible(
    *, api_key: str, base_url: Optional[str], model: str, system: str, user: str,
    max_tokens: int, on_delta: Optional[Callable[[str], None]],
) -> LLMResult:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise LLMClientError("openai SDK 未装. pip install 'openai>=1.0'") from exc
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    stream = client.chat.completions.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        stream=True, stream_options={"include_usage": True},
        timeout=_request_timeout_sec())      # 流式也加超时: 卡住的流不再永占线程
    parts: list[str] = []
    in_tok = out_tok = 0
    for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if choices:
            delta = getattr(choices[0].delta, "content", None) or ""
            if delta:
                parts.append(delta)
                if on_delta:
                    on_delta("".join(parts))
        usage = getattr(chunk, "usage", None)
        if usage:
            in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
            out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
    text = "".join(parts)
    if _looks_like_waf_block(text):
        raise WAFBlockError("应答正文疑似 WAF 拦截页 (openai-compatible 流式)", body=text)
    return LLMResult(text=text, input_tokens=in_tok, output_tokens=out_tok,
                     provider="openai-compatible", model=model)


def _stream_anthropic(
    *, provider: str, api_key: str, base_url: Optional[str], model: str, system: str,
    user: str, max_tokens: int, on_delta: Optional[Callable[[str], None]],
) -> LLMResult:
    try:
        import anthropic
    except ImportError as exc:
        raise LLMClientError("anthropic SDK 未装. pip install anthropic") from exc
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = anthropic.Anthropic(**kwargs)
    parts: list[str] = []
    with client.messages.stream(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
        timeout=_request_timeout_sec(),      # 流式也加超时: 卡住的流不再永占线程
    ) as stream:
        for delta in stream.text_stream:
            if delta:
                parts.append(delta)
                if on_delta:
                    on_delta("".join(parts))
        final = stream.get_final_message()
    text = "".join(parts)
    if _looks_like_waf_block(text):
        raise WAFBlockError("应答正文疑似 WAF 拦截页 (anthropic 流式)", body=text)
    usage = getattr(final, "usage", None)
    return LLMResult(text=text,
                     input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                     output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                     provider=provider, model=model)


def _complete_codex_cli(
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
) -> LLMResult:
    cmd = os.environ.get("BOSS_CODEX_CMD", "codex")
    timeout_sec = int(os.environ.get("BOSS_CODEX_TIMEOUT_SEC", "900"))
    sandbox = os.environ.get("BOSS_CODEX_SANDBOX", "read-only")
    cwd = os.environ.get("BOSS_CODEX_CWD") or str(Path(__file__).resolve().parent.parent)
    profile = os.environ.get("BOSS_CODEX_PROFILE")
    explicit_model = os.environ.get("BOSS_CODEX_MODEL")
    pass_pipeline_model = os.environ.get("BOSS_CODEX_PASS_PIPELINE_MODEL") == "1"
    chosen_model = explicit_model or (model if pass_pipeline_model else "")

    prompt = (
        "# System instructions\n\n"
        f"{system}\n\n"
        "# User request\n\n"
        f"{user}\n\n"
        "# Output contract\n\n"
        "Return only the requested artifact text. Do not edit files. Do not include extra commentary."
    )

    with tempfile.TemporaryDirectory(prefix="boss-codex-cli-") as tmp:
        output_path = Path(tmp) / "last-message.txt"
        args = [
            cmd,
            "exec",
            "--ephemeral",
            "--sandbox",
            sandbox,
            "--cd",
            cwd,
            "--output-last-message",
            str(output_path),
        ]
        if profile:
            args.extend(["--profile", profile])
        if chosen_model:
            args.extend(["--model", chosen_model])
        args.append("-")

        try:
            completed = subprocess.run(
                args,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout_sec,
                check=False,
            )
        except FileNotFoundError as exc:
            raise LLMClientError(f"codex CLI 未找到: {cmd!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMClientError(f"codex exec 超时 ({timeout_sec}s)") from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip()[-2000:]
            stdout = completed.stdout.strip()[-1000:]
            detail = stderr or stdout or f"exit={completed.returncode}"
            raise LLMClientError(f"codex exec 失败: {detail}")

        text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
        if not text:
            text = completed.stdout.strip()
        if not text:
            raise LLMClientError("codex exec 未返回 last message")

    approx_input = max(1, len(prompt) // 4)
    approx_output = max(1, len(text) // 4)
    return LLMResult(
        text=text,
        input_tokens=approx_input,
        output_tokens=approx_output,
        provider="codex-cli",
        model=chosen_model or "codex-config-default",
    )


def _complete_kimi_cli(
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
) -> LLMResult:
    """Spawn the local Kimi CLI (`kimi -p`) non-interactively and parse stream-json."""
    cmd = os.environ.get("BOSS_KIMI_CMD", "/Users/john/.kimi-code/bin/kimi")
    timeout_sec = int(os.environ.get("BOSS_KIMI_TIMEOUT_SEC", "900"))
    explicit_model = os.environ.get("BOSS_KIMI_MODEL")
    chosen_model = explicit_model or model

    prompt = (
        f"{system}\n\n"
        f"{user}\n\n"
        "Output contract: Return only the requested artifact text. "
        "Do not call any tools. Do not edit files. Do not add commentary outside the artifact."
    )

    args = [
        cmd,
        "-p", prompt,
        "--output-format", "stream-json",
    ]
    if chosen_model:
        args.extend(["-m", chosen_model])

    try:
        completed = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
    except FileNotFoundError as exc:
        raise LLMClientError(f"Kimi CLI 未找到: {cmd!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LLMClientError(f"Kimi CLI 超时 ({timeout_sec}s)") from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip()[-2000:] or completed.stdout.strip()[-1000:] or f"exit={completed.returncode}"
        raise LLMClientError(f"Kimi CLI 失败: {detail}")

    text = ""
    for line in completed.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("role") == "assistant" and "content" in obj:
            text = obj["content"]
            break

    if not text:
        raise LLMClientError("Kimi CLI 未返回 assistant content")

    approx_input = max(1, len(prompt) // 4)
    approx_output = max(1, len(text) // 4)
    return LLMResult(
        text=text,
        input_tokens=approx_input,
        output_tokens=approx_output,
        provider="kimi-cli",
        model=chosen_model or "kimi-default",
    )
