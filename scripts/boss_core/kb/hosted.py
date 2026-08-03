"""boss_core.kb.hosted — HostedKB: 包住现网行为的第一个 KBProvider (M1.2)。

三方法**逐字委派**到现有实现, 唯一新增是 query 的 tier 过滤 (K-a) + 可选出口脱敏
骨架 (K-b)。CLI (M1.3 接线) 用 `tier="tian_only"` + `redact_egress=False` → 零裁剪、
存原文, 与直调现有函数逐字一致; 服务侧 (M2/M3) 按调用方角色降档 + 开出口闸。

依赖纪律 (K-c): 同包 boss_core.wiki_query 直接 import; panel_loader / redact_check
是独立顶层模块 → 函数内 lazy import; rpl-resident 的 _load_judge_skill /
_resolve_panel_path / wiki_query_fallback 走构造器 DI 注入 — 本模块绝不 import
run_pipeline_local (no_reverse_import 护栏)。

与 M1 方案 §4 的一处实现修正: 方案原文把 grep 兜底「拼进」query 返回, 但实测两路
类型不同 (WikiEntityHit 带 sensitivity, grep 的 WikiHit 不带) 且 CLI context.md 按
两路分开渲染 — 拼接会破坏 KBHit 契约与 K-d 逐字快照。故 query() (Protocol 面) 只
返回 entity hits; grep 兜底作为 HostedKB **专属**方法 query_fallback_raw() (不进
Protocol — M5 LocalKB bundle 本无 raw/ 可 grep), 接线时两路照旧分开。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from boss_core.errors import PipelineError
from boss_core.kb.types import KBHit, PanelSpec, Tier, _TIER_ORDER
from boss_core.wiki_query import _extract_keywords, query_wiki_entities

# 未知 sensitivity 按 confidential 兜底 (fail-close 方向: 未标注的当高敏管)
_UNKNOWN_SENSITIVITY_RANK = _TIER_ORDER["confidential"]
# 未知 tier 按 public 兜底 (fail-close 方向: 未知调用方给最小可见面)
_UNKNOWN_TIER_RANK = _TIER_ORDER["public"]


class HostedKB:
    """托管 KB: 只读 vault (_wiki/ + doctrine + panels), CLI 与 M2 服务共用。

    Args:
        load_doctrine_fn: `(judge, panel_name) -> str` — rpl._load_judge_skill (DI)
        resolve_panel_path_fn: `(panel_name) -> Path` — rpl._resolve_panel_path (DI)
        wiki_fallback_fn: `(keywords) -> list` — rpl.wiki_query_fallback (DI, 可选)
        log: Logger (可选, 透传给 query_wiki_entities 的 dbg)
        redact_egress: True = 对外返回过 redact 出口闸 (SR2)。CLI 必须 False (K-b:
            内部存原文, 靠 tier 分级管控; 脱敏只发生在公网出口)。
        redact_log_fn: `(hits) -> None` — 出口闸命中时的记录回调 (服务侧注入
            redact_check.log_blocked 落 blocked-publish.log; 默认 None 不落盘)。
    """

    def __init__(
        self,
        *,
        load_doctrine_fn: Callable[[str, Optional[str]], str],
        resolve_panel_path_fn: Callable[[str], Any],
        wiki_fallback_fn: Optional[Callable[..., list]] = None,
        log=None,
        redact_egress: bool = False,
        redact_log_fn: Optional[Callable[[list], None]] = None,
    ) -> None:
        self._load_doctrine_fn = load_doctrine_fn
        self._resolve_panel_path_fn = resolve_panel_path_fn
        self._wiki_fallback_fn = wiki_fallback_fn
        self._log = log
        self._redact_egress = redact_egress
        self._redact_log_fn = redact_log_fn

    # ── KBProvider 契约面 ────────────────────────────────────────────────

    def query(self, q: str, *, tier: Tier = "tian_only",
              topic_raw: Optional[str] = None,
              keywords: Optional[list[str]] = None) -> list[KBHit]:
        """检索 _wiki entities/people/concepts, 按 tier 过滤。

        keywords 显式传入时跳过内部抽取 (REVIEW 模式的 keywords 是 topic+doc_title
        两段拼接, 接线时由调用方拼好传入, 保逐字不变)。topic_raw 原样透传 (含 None) —
        不 fallback 到 q, 否则 REVIEW 路径会多出整段匹配、改变命中集。
        """
        kws = list(keywords) if keywords is not None else _extract_keywords(q)
        hits = query_wiki_entities(kws, log=self._log, topic_raw=topic_raw)
        tier_rank = _TIER_ORDER.get(tier, _UNKNOWN_TIER_RANK)
        hits = [
            h for h in hits
            if _TIER_ORDER.get(h.sensitivity, _UNKNOWN_SENSITIVITY_RANK) <= tier_rank
        ]
        if self._redact_egress:
            hits = [h for h in hits if not self._egress_blocked(
                f"{h.canonical}\n{h.profile}", f"<kb:entity:{h.slug}>")]
        return hits

    def load_doctrine(self, judge: str, *, scene: Optional[str] = None) -> str:
        """评委 SKILL.md doctrine (委派 rpl._load_judge_skill; scene = panel_name)。"""
        text = self._load_doctrine_fn(judge, scene)
        if self._redact_egress and self._egress_blocked(text, f"<kb:doctrine:{judge}>"):
            raise PipelineError(
                f"doctrine `{judge}` 命中出口脱敏闸, 已拦截 (SR2 fail-close); "
                f"详见 blocked-publish.log")
        return text

    def resolve_panel(self, scene: str) -> PanelSpec:
        """panel 编组 (extends 已展开) — 委派 panel_loader.resolve_panel。"""
        import panel_loader
        return panel_loader.resolve_panel(self._resolve_panel_path_fn(scene))

    # ── HostedKB 专属 (不进 Protocol) ────────────────────────────────────

    def query_fallback_raw(self, keywords: list[str], **kwargs) -> list:
        """grep raw/ 兜底 (sage-wiki 未编译时的 B 方案) — vault 专属退化路径。

        返回 WikiHit (无 sensitivity 字段) — 不做 tier 过滤, 只供满档 CLI 用;
        服务侧 (降档调用方) 不暴露此方法。未注入 fallback fn 时返回空列表。
        """
        if self._wiki_fallback_fn is None:
            return []
        return self._wiki_fallback_fn(keywords, **kwargs)

    # ── 内部 ────────────────────────────────────────────────────────────

    def _egress_blocked(self, text: str, path_label: str) -> bool:
        """SR2 出口闸: 命中即拦 + 可选记录 (redact_log_fn)。"""
        import redact_check
        blocked, rhits = redact_check.check_text(text, path=path_label)
        if blocked and self._redact_log_fn is not None:
            self._redact_log_fn(rhits)
        return blocked
