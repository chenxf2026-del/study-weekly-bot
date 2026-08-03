"""
_fetchers.py — attribution V2 真 fetcher (v0.7 R11 · fetcher PRD §3 中期 B 路线)

数据流: attribution_check.fetch_signal_stub(data_source="websearch")
  → fetch_signal_websearch() → HTTP search API → 拼接检索摘要作为 actual_signal
  → compare_signals(--llm-compare 时语义比对; 否则字符串 fallback)

provider 探测链 (env key, 有哪个用哪个):
  1. TAVILY_API_KEY  → Tavily /search (有 answer 合成, 首选)
  2. BRAVE_API_KEY   → Brave Web Search API
  3. 都没有          → 返回 (None, None, 降级说明) — 行为退回 V1 hybrid
                       (instructions.md 人工调研指令), 与 v0.6 完全一致

设计纪律:
  - confidence 固定 0.5: 检索拼接是弱证据, 不是人工核实 —
    compare_signals 字符串 fallback 在 conf < 0.7 时最多判 partial, 永不 falsified;
    语义判断交给 --llm-compare (V2 GLM/Haiku)。
  - fail-safe: 任何异常 (网络/限流/解析) 都返回 None 降级, 不让 cron 挂掉。
  - 出站内容仅 falsification_metric (case 内已脱敏的可观测信号描述);
    调用方 (attribution_check) 在 confidential case 上使用前应自行确认 metric 不含敏感词。
"""

from __future__ import annotations

import os
from typing import Optional

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

# v0.8 (N8 backlog): 网络类异常重试 3 次, 指数退避 (Tavily/Brave 常见 429/瞬断)。
# 重试范围: 传输层异常 (含超时) + 429/5xx; 其余 4xx 配置类错误不重试 (key 错重试无意义)。
# 耗尽后 reraise, 由 fetch_signal_websearch 外层 except 降级 V1 hybrid — fail-safe 语义不变。


def _is_retryable_exc(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):  # 含 TimeoutException 子类
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


_search_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=8),
    retry=retry_if_exception(_is_retryable_exc),
    reraise=True,
)

SEARCH_TIMEOUT_SEC = 15.0
MAX_SIGNAL_CHARS = 1500
WEBSEARCH_CONFIDENCE = 0.5  # 弱证据 — 见模块 docstring 设计纪律


def websearch_provider() -> Optional[str]:
    """返回可用 provider 名 (tavily / brave), 都无 key 时 None。"""
    if os.environ.get("TAVILY_API_KEY"):
        return "tavily"
    if os.environ.get("BRAVE_API_KEY"):
        return "brave"
    return None


@_search_retry
def _tavily_search(query: str) -> str:
    resp = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": os.environ["TAVILY_API_KEY"],
            "query": query,
            "max_results": 5,
            "include_answer": True,
        },
        timeout=SEARCH_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    data = resp.json()
    parts: list[str] = []
    if data.get("answer"):
        parts.append(f"[answer] {data['answer']}")
    for r in data.get("results", [])[:5]:
        title = r.get("title", "")
        content = (r.get("content") or "")[:200]
        url = r.get("url", "")
        parts.append(f"- {title}: {content} ({url})")
    return "\n".join(parts)


@_search_retry
def _brave_search(query: str) -> str:
    resp = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": 5},
        headers={
            "X-Subscription-Token": os.environ["BRAVE_API_KEY"],
            "Accept": "application/json",
        },
        timeout=SEARCH_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    data = resp.json()
    parts: list[str] = []
    for r in data.get("web", {}).get("results", [])[:5]:
        title = r.get("title", "")
        desc = (r.get("description") or "")[:200]
        url = r.get("url", "")
        parts.append(f"- {title}: {desc} ({url})")
    return "\n".join(parts)


def web_search(query: str) -> Optional[str]:
    """通用 web 搜索原语 (Phase 2 调研复用 · v1.1 A4/C3)。
    返回结果文本 (answer + 前 5 条); 无 key / 网络失败 / 空结果 → None (调用方降级到无 web)。"""
    provider = websearch_provider()
    if provider is None:
        return None
    try:
        text = _tavily_search(query) if provider == "tavily" else _brave_search(query)
    except Exception:
        return None
    return text.strip() or None


def fetch_signal_websearch(
    falsification_metric: str,
    expected_signal: str,
    metric_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[float], str]:
    """websearch fetcher 入口。返回 (actual_signal | None, confidence | None, note)。

    None = 降级 V1 hybrid (无 key / 空结果 / 任何异常), 调用方行为与 v0.6 一致。

    expected_signal 当前**有意不用**: query 只用 falsification_metric (可观测信号描述),
    把预期答案塞进检索词会造成确认偏差 (搜出来的都像预期)。比对语义交给
    compare_signals / --llm-compare。保留参数是为了与 fetch_signal_stub 的
    fetcher 接口形状一致 (marsdata/feishu 接入时同签名)。

    metric_id: websearch 不用 (marsdata 专用的指标定位 ID); 接口对齐保留。
    """
    provider = websearch_provider()
    if provider is None:
        return None, None, ("websearch fetcher: 无 TAVILY_API_KEY / BRAVE_API_KEY — "
                            "降级 V1 hybrid (instructions.md 人工调研)")

    query = falsification_metric.strip()[:300]
    if not query:
        return None, None, "websearch fetcher: falsification_metric 为空, 无法构造 query"

    try:
        text = _tavily_search(query) if provider == "tavily" else _brave_search(query)
    except Exception as e:
        return None, None, (f"websearch fetch 失败 ({provider}, {type(e).__name__}: {e}) — "
                            f"降级 V1 hybrid")

    if not text.strip():
        return None, None, f"websearch ({provider}) 0 结果 — 降级 V1 hybrid"

    signal = text.strip()[:MAX_SIGNAL_CHARS]
    note = f"websearch via {provider} · query={query[:80]!r} · 弱证据 conf={WEBSEARCH_CONFIDENCE} (建议 --llm-compare)"
    return signal, WEBSEARCH_CONFIDENCE, note


# ─── marsdata fetcher (接线脚手架 · REST 契约待定) ──────────────────
#
# 状态 (fetcher PRD §3 / prd-v1-fetcher-2026-06-09.md): marsdata REST API **契约未知**
# ("API 状态不明"), 真接入是 V3 (Hermes 上云走 marsdata-mcp, D 路线)。
#
# 本函数是**接线脚手架**, 不伪造数据: 把 env (MARSDATA_API_KEY / MARSDATA_BASE_URL) 与
# checkpoint 的 metric_id 都接好, 在"已知/未知边界"显式降级 (返回 None → V1 hybrid)。
# 当 marsdata REST 契约确定时, 只需在 _marsdata_query() 里填 HTTP 请求/响应解析, 其余不动。
#
# ⚠️ 纪律: 在 REST 契约确定前, 本函数**永不返回非 None 的 actual_signal** — 宁可降级走人工,
#    也不猜端点/参数编造数值 (编造财务信号 → 误判证伪 → 错误 Failure Card, §7 联动后果严重)。

MARSDATA_CONFIDENCE = 0.8  # 结构化数值源 (契约确定后用); 强于 websearch 拼接的 0.5


def marsdata_configured() -> bool:
    """有 API key 才算配置 (base_url 有默认)。"""
    return bool(os.environ.get("MARSDATA_API_KEY"))


def _marsdata_query(metric_id: str, base_url: str, api_key: str) -> Optional[str]:
    """marsdata REST 查询 — **契约待定 (V3)**。

    确定 marsdata REST 端点/参数/响应形状后在此实现:
      resp = httpx.get(f"{base_url}/<endpoint>", params={"metric_id": metric_id, ...},
                       headers={"Authorization": f"Bearer {api_key}"}, timeout=...)
      → 解析出最新数值 → 返回可读 actual_signal 文本 (如 "Q2 营收 12.3 亿, 同比 +8%")。

    当前: 契约未知, 返回 None (绝不编造数值)。
    """
    return None


def fetch_signal_marsdata(
    falsification_metric: str,
    expected_signal: str,
    metric_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[float], str]:
    """marsdata fetcher 入口。返回 (actual_signal | None, confidence | None, note)。

    降级链 (任一不满足 → None, 走 V1 hybrid 人工调研指令):
      1. 无 MARSDATA_API_KEY                  → None
      2. checkpoint 无 metric_id              → None (提示填 cp["metric_id"])
      3. REST 契约待定 (_marsdata_query None) → None (当前恒走此分支)
    """
    if not marsdata_configured():
        return None, None, ("marsdata fetcher: 无 MARSDATA_API_KEY — 降级 V1 hybrid (instructions.md 人工调研)")

    mid = (metric_id or "").strip()
    if not mid:
        return None, None, ("marsdata fetcher: checkpoint 缺 metric_id — "
                            "在 cp[\"metric_id\"] 填 marsdata 指标 ID 后重试; 暂降级 V1 hybrid")

    base_url = os.environ.get("MARSDATA_BASE_URL", "https://api.marsdata.ai")
    api_key = os.environ["MARSDATA_API_KEY"]
    try:
        signal = _marsdata_query(mid, base_url, api_key)
    except Exception as e:  # fail-safe: 任何异常都降级, 不让 cron 挂
        return None, None, (f"marsdata fetch 失败 (metric_id={mid}, {type(e).__name__}: {e}) — 降级 V1 hybrid")

    if not signal:
        return None, None, (f"marsdata fetcher: REST 契约待定 (V3, metric_id={mid}) — "
                            f"暂降级 V1 hybrid; 契约确定后实现 _marsdata_query 即闭环")

    return signal.strip()[:MAX_SIGNAL_CHARS], MARSDATA_CONFIDENCE, (
        f"marsdata via REST · metric_id={mid} · conf={MARSDATA_CONFIDENCE}")
