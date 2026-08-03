#!/usr/bin/env python3
"""
run_pipeline_local.py — 路径 B 落地脚手架 (W1 Day 1-3, dev-plan v2.7)

不依赖 Hermes / sage-wiki, 用可配置 LLM provider + Skill 文件直接驱动锚点判断流水线。
设计 note: docs/run-pipeline-local-design.md

Phase 0 路由 → Phase 1 PRD/Context → Phase 2 N 维并行 → Phase 3 合成 →
Phase 4 评委独立打分 → Phase 5 合议 + 版本 → Verify (smoke_e2e + redact + lint)

Day 1 AM: Phase 0 路由完整 + Phase 1 PRD/Context 骨架 + grep fallback
Day 1 PM: Phase 2 并行 sub-agents + Phase 3 合成
Day 2:    Phase 4 评委独立打分 (asyncio + data-isolated) + adversarial_view + fail-soft
Day 3 (当前): Phase 5 合议 (Python 算 panel_summary + Opus 写 prose) + versions 不可变 +
              case.json 12 字段 + _wiki/log.md 追加 + verify (smoke_e2e + redact + lint)

§ 隔离方式: 设计 note §6.3 推荐 subprocess, 实际选 asyncio.to_thread + 数据层隔离
   (每位评委 prompt 仅含自己 SKILL.md + synthesis + context, 永不含其他评委 review)。
   CLAUDE.md §4.5 硬约束是 "不读其他评委 reviews", 数据隔离已满足; subprocess 物理
   隔离开销 (2-3s 启动 × N) 在 V0 期不值得。

CLI:
  python3 run_pipeline_local.py "议题描述" --brand <slug> [--dry-run] [--auto-confirm]

Q1-Q4 当前默认 (设计 note §9.2):
  Q1 GATE 1 → --auto-confirm flag (默认 False, 加 flag 即跳过 stdin Y/N)
  Q2 真议题 brand → 由用户 CLI 传 --brand
  Q3 评委失败 → 直接占位 (Day 2 实现)
  Q4 verify 失败 → 保留产物 (Day 3 实现)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Optional, Any

# ── M0.1 boss_core 抽库 · 原位 re-export 保签名 (所有现有 import/测试零改动) ──
# 纯逻辑已下沉 boss_core/; 这里 re-export 让 `rp.<name>` 照旧可用。
from boss_core.errors import PipelineError  # noqa: E402,F401
from boss_core.logger import Logger  # noqa: E402,F401
from boss_core.docio import (  # noqa: E402,F401
    _read_doc_text, _cap_doc_text, _extract_section_body,
)
from boss_core.constants import (  # noqa: E402,F401
    _DECISION_HEADINGS, _CONSENSUS_HEADINGS, _EVIDENCE_HEADINGS,
    _COUNTER_HEADINGS, _ATTRIBUTION_HEADINGS,
)
from boss_core.prose import (  # noqa: E402,F401
    _strip_brief_noise, _extract_conclusion_prose, _extract_reasoning_prose,
    _extract_evidence_prose, _extract_counter_prose,
)
from boss_core.wiki_query import (  # noqa: E402,F401
    WikiEntityHit, WIKI_DIR, _extract_keywords, query_wiki_entities,
)
# M0.2 · T2 prompt 装配下沉 boss_core.prompts; 纯逻辑收显式 scoring_spec 参数 (§6 R-a),
# 本文件顶部同名薄 wrapper 注入当前可变全局 (SCORING_SPEC/SCORING_LENSES/ANCHOR_JUDGES) +
# _anchor_confidence_cap 回调, 保对外签名与快照输出不变。
from boss_core import prompts as _prompts  # noqa: E402,F401
# M0.3 · T3 打分聚合下沉 boss_core.scoring; 固定阈值常量随之下沉 (re-export 见下),
# 可变全局 + _parse_review_frontmatter 由本文件薄 wrapper 注入 (§6 R-a)。
from boss_core import scoring as _scoring  # noqa: E402,F401
from boss_core.scoring import (  # noqa: E402,F401
    ANCHOR_DELTA_THRESHOLD, META_TOPIC_TYPES_FOR_DUAL_SCALE,
    ANCHOR_DUAL_SCALE_DELTA_THRESHOLD, _format_panel_summary_dual_scale_yaml,
)
# M1.3 · KB 访问统一走 KBProvider (boss_core.kb); CLI 满档实例见下方 _hosted_kb()。
from boss_core.kb import HostedKB  # noqa: E402
# M2.0a · panel 路径 + 评委 doctrine 加载下沉 boss_core/kb/vault_paths.py (服务化前置);
# 本文件下方留同名薄 wrapper 注入本模块 (测试可 monkeypatch 的) 路径全局。
# PHASE_3_SYSTEM_PROMPT 下沉 boss_core/prompts.py。
from boss_core.kb import vault_paths as _vault_paths  # noqa: E402
from boss_core.prompts import PHASE_3_SYSTEM_PROMPT  # noqa: E402,F401
# M2.0b · review frontmatter 解析 + anchor confidence cap 下沉 (re-export 保签名;
# rpl 内部调用点运行时查本模块 globals, 测试 monkeypatch 语义不变);
# scoring spec / anchor judges 推导体纯化进 boss_core, 本文件 refresh_* wrapper 保全局刷新语义。
from boss_core.reviews import (  # noqa: E402,F401
    _parse_review_frontmatter, _parse_review_frontmatter_fallback,
)
from boss_core.anchor_research import (  # noqa: E402,F401
    ANCHOR_RESEARCH_CAPS, _anchor_confidence_cap,
)

# ──────────────────────────────────────────────────────────────────────
# 常量与路径
# ──────────────────────────────────────────────────────────────────────

VAULT_ROOT = Path(__file__).parent.parent.resolve()
SCRIPTS_DIR = VAULT_ROOT / "scripts"
REPORTS_DIR = VAULT_ROOT / "reports"
CASES_DIR = VAULT_ROOT / "cases"
# multi-anchor B 档后, 锚点专属 raw/ 在 anchors/<slug>/raw/ (见 ADR-002)
# clippings/ 仍是 锚点无关的通用来源
RAW_DIRS_FOR_WIKI_FALLBACK = [
    VAULT_ROOT / "raw" / "clippings",
    VAULT_ROOT / "anchors" / "tian" / "raw" / "interviews",
]
SKILLS_DIR = VAULT_ROOT / "skills"
ANCHORS_DIR = VAULT_ROOT / "anchors"           # multi-anchor B 档 · per-anchor perspective skills
PANELS_DIR = VAULT_ROOT / "panels"


# _resolve_panel_path 逻辑已下沉 boss_core/kb/vault_paths.py (M2.0a)。薄 wrapper 注入
# 本模块 VAULT_ROOT / PANELS_DIR (运行时读, 测试 monkeypatch 语义不变)。

def _resolve_panel_path(panel_name: str) -> Path:
    return _vault_paths._resolve_panel_path(
        panel_name, vault_root=VAULT_ROOT, panels_dir=PANELS_DIR)


# provider env 可覆盖 (非 anthropic 端点须配套换模型名, 如智谱 glm-4.5)。
# 模型名默认 (DEFAULT_MODEL_FAST/DEEP) 见下方 import 之后 —— 兜底走 model_defaults 单一真相源。
DEFAULT_LLM_PROVIDER = os.environ.get("BOSS_LLM_PROVIDER", "anthropic")

# Phase 0 塞进 parse prompt 的文档正文字符上限 (2026-07-01 生产: 2.5MB docx 提取的
# 巨量正文原样怼进 prompt → 超大 POST body, 反复重试累积流量把网关 WAF 顶爆 / 亦可能超
# token / 超时)。超限智能截断 (保头留尾, 汇报稿结论常在末尾)。~6万中文字符 ≈ 3万 token,
# 对一份方案评审的信息量足够; env 可调。
REVIEW_DOC_MAX_CHARS = int(os.environ.get("REVIEW_DOC_MAX_CHARS", "60000"))

CASE_ID_RE = re.compile(r"^C-(\d{4})-(\d{4})$")
BRAND_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")

# 议题类型 → panels/default.yaml 的 auto_judge_selection.rules key
# Phase 1 PRD 起草时让 LLM 选, 这里仅是 fallback
DEFAULT_TOPIC_TYPE = "unknown"

# 让 conftest 风格的 sys.path 调整生效, 这样可以 import 同目录脚本
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import report_builder
import llm_client
import model_defaults

# 单一源在 report_builder._yaml_quote_inline; 这里 re-export 让 pipeline 内
# 旧调用点 + 现有测试 (rp._yaml_quote_inline) 仍按本模块解析, 无需改 import.
_yaml_quote_inline = report_builder._yaml_quote_inline

# 模型名默认: 主来源 BOSS_LLM_MODEL_* (llm_switch use / scene.llm 覆盖写入), 缺则落
# model_defaults 单一真相源 (P2 · 兜底 = ANTHROPIC_MODEL_* → 硬编码字面量)。
DEFAULT_MODEL_FAST = os.environ.get("BOSS_LLM_MODEL_FAST") or model_defaults.model_fast()
DEFAULT_MODEL_DEEP = os.environ.get("BOSS_LLM_MODEL_DEEP") or model_defaults.model_deep()


# ──────────────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────────────

@dataclass
class PipelineConfig:
    """CLI 参数 + 解析后的运行配置"""
    topic: str                       # 议题描述 (FRESH 必填; EVOLUTION 可空; REVIEW 默认从 doc 推, 可 --topic override)
    brand_slug: str
    panel: str = "default"
    model_fast: str = DEFAULT_MODEL_FAST
    model_deep: str = DEFAULT_MODEL_DEEP
    llm_provider: str = DEFAULT_LLM_PROVIDER
    llm_base_url: Optional[str] = None
    llm_api_key_env: Optional[str] = None
    dry_run: bool = False
    auto_confirm: bool = False       # Q1: True = 跳过 GATE 1 stdin Y/N
    evolution: bool = False
    diff_only: list[str] = field(default_factory=list)  # EVOLUTION 重跑维度
    no_diff_plan: bool = False       # v0.6 R7: 禁用 Phase 1E auto diff plan (强制全维度重跑)
    resume_from: Optional[str] = None  # phase-0..5
    no_redact: bool = False
    verbose: bool = False
    # REVIEW mode (ADR-007)
    review_doc_path: Optional[str] = None    # REVIEW: 被评议方案 doc 路径
    review_verify: bool = False              # REVIEW: --verify opt-in Phase 2 claim verification
    review_into: bool = False                # REVIEW: 允许覆盖已存在 brand
    # Phase 6 export (P1.3 · CLAUDE.md §11 "等价于 /boss")
    export_set: Optional[str] = None         # "a" | "b" | "c" | "all" | None (= 不导出, 默认)
    export_redact: str = "light"             # Set B 脱敏强度: "light" | "strict" | "meta-only"
    export_skip_pdf: bool = False            # 仅生 md/html, 跳 Chrome PDF
    export_only: bool = False                # 跳 Phase 0-5, 仅 Phase 6


@dataclass
class PipelineState:
    """每次跑保存到 cases/<id>/_pipeline_run.json 的状态 (审计 + 断点续跑)"""
    case_id: str
    brand_slug: str
    mode: str                        # FRESH | EVOLUTION | REVIEW (ADR-007)
    started_at: str                  # ISO timestamp
    phases_completed: list[str] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)   # phase -> [paths]
    token_usage: dict = field(default_factory=dict)  # phase -> {input, output, cost_usd}
    errors: list[str] = field(default_factory=list)

    def save(self, vault_root: Optional[Path] = None) -> Path:
        # 不在签名里用 VAULT_ROOT 作默认值 (定义时绑定),
        # 否则测试 monkeypatch.setattr(rp, "VAULT_ROOT", ...) 不生效。
        root = vault_root if vault_root is not None else VAULT_ROOT
        path = root / "cases" / self.case_id / "_pipeline_run.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        return path


# ──────────────────────────────────────────────────────────────────────
# 日志 (轻量, 不引外部 logging 库)
# ──────────────────────────────────────────────────────────────────────

# Logger 已下沉 boss_core/logger.py (M0.1d), 文件顶部 re-export。


# ──────────────────────────────────────────────────────────────────────
# Phase 0 — Router
# ──────────────────────────────────────────────────────────────────────

def phase_0_router(cfg: PipelineConfig, log: Logger) -> tuple[str, str]:
    """
    检查 reports/<brand>/report.md 是否存在, 决定 FRESH vs EVOLUTION vs REVIEW (ADR-007)。
    同时分配 / 复用 case_id。

    Returns:
        (mode, case_id) — mode ∈ {"FRESH", "EVOLUTION", "REVIEW"}
    """
    report_path = REPORTS_DIR / cfg.brand_slug / "report.md"

    # REVIEW mode (ADR-007)
    if cfg.review_doc_path:
        if report_path.exists() and not cfg.review_into:
            raise PipelineError(
                f"REVIEW 拒绝覆盖已存在 brand={cfg.brand_slug} ({report_path}). "
                f"加 --review-into 显式覆盖, 或换 --brand <other-slug>."
            )
        mode = "REVIEW"
        case_id = _allocate_new_case_id()
        log.step("Phase 0", f"REVIEW mode · doc={cfg.review_doc_path} · 新分配 case_id={case_id}")
        return mode, case_id

    # 已有逻辑: FRESH vs EVOLUTION
    if cfg.evolution or report_path.exists():
        if not report_path.exists():
            raise PipelineError(
                f"--evolution 要求 {report_path} 存在, 但没找到。"
                f"首次跑此 brand 应走 FRESH (去掉 --evolution)。"
            )
        mode = "EVOLUTION"
        # EVOLUTION: 复用 brand 关联的最近一个 case_id
        case_id = _find_latest_case_for_brand(cfg.brand_slug, log)
        if case_id is None:
            raise PipelineError(
                f"EVOLUTION 模式找不到 brand={cfg.brand_slug} 的历史 case。"
                f"扫描 {CASES_DIR} 无匹配 — 数据状态不一致, 人工排查。"
            )
        log.step("Phase 0", f"EVOLUTION mode · 复用 case_id={case_id}")
    else:
        mode = "FRESH"
        case_id = _allocate_new_case_id()
        log.step("Phase 0", f"FRESH mode · 新分配 case_id={case_id}")

    return mode, case_id


# ──────────────────────────────────────────────────────────────────────
# REVIEW mode (ADR-007): doc parser
# ──────────────────────────────────────────────────────────────────────

@dataclass
class ReviewDocParse:
    """Phase 0 REVIEW doc parser 输出"""
    topic: str                       # doc 隐含/明示议题
    topic_type: str                  # 8 种之一 (strategic/customer/...)
    claims: list[str]                # doc 提出的核心 claim (3-5 句)
    decisions: list[dict]            # action list: {action, owner?, deadline?}
    variables: list[dict]            # 关键变量 (将进 case.json.variables, source=from-doc)
    time_constraints: list[str]      # 时间/截止 约束
    key_assumptions: list[str]       # doc 的核心假设
    doc_title: str                   # doc 标题 (用作 brand 派生 + report.md §A 头)
    doc_summary: str                 # ≤ 800 字 doc 摘要 (REVIEW report §A)
    project_name: Optional[str] = None  # 竞赛: 参赛项目名 (排名榜列; 默认回退 doc_title)
    team: Optional[str] = None          # 竞赛: 参赛组名 (排名榜列; 缺则 "—")


# 模型填可选字段时常把"无"写成字符串 "null"/"none"/"无"/"-" 而非省略 — 这些都归一成 None
_NULLISH_STRINGS = {"", "null", "none", "nil", "n/a", "na", "-", "—", "无", "未知", "未提供", "暂无"}


def _clean_optional_str(v: Any) -> Optional[str]:
    """把模型返回的 null 类字符串 / 非字符串归一成 None; 否则返回 strip 后的字符串。"""
    if not isinstance(v, str):
        return None
    s = v.strip()
    return None if s.lower() in _NULLISH_STRINGS else s


def _normalize_decisions(raw_decisions: Any) -> list[dict]:
    """把模型返回的 decisions 归一成 list[dict] (每项至少含 action 字符串)。

    2026-06-30 op2 生产暴露: 指令遵循弱的端点 (gpt-5.x 等) 常把 decisions 返回成
    **字符串列表** (而非 {action, owner?, deadline?} 字典列表), 下游 d.get('action')
    直接 AttributeError: 'str' object has no attribute 'get' 崩掉整条流水线。
    统一兜底: 字符串 → {action: <str>}; dict 缺 action → 取首个非空字符串值兜底;
    空白项丢弃; 其它类型 → str()。
    """
    out: list[dict] = []
    for d in (raw_decisions or []):
        if isinstance(d, dict):
            if _clean_optional_str(d.get("action")):
                out.append(d)
            else:
                fallback = next(
                    (v.strip() for v in d.values() if isinstance(v, str) and v.strip()), "")
                out.append({**d, "action": fallback or "TBD"})
        elif isinstance(d, str):
            if d.strip():
                out.append({"action": d.strip()})
        elif d is not None:
            out.append({"action": str(d)})
    return out


_REVIEW_DOC_PARSER_SYSTEM = """你是 boss 流水线的 Phase 0 doc parser. 用户给你一份**已含方案的文档**, 你的任务是提取它,
**不是评议它, 也不是研究新东西**.

输出严格 JSON 格式:
{
  "topic": "<doc 隐含或明示的议题, 1-2 句>",
  "topic_type": "<9 选 1: strategic / customer / organizational / product / brand / financial / cross_domain / meta_framework / unknown>",
  "claims": ["<doc 的核心论点 3-5 条>"],
  "decisions": [
    {"action": "<doc 提议的具体动作>", "owner": "<谁来做, 可省>", "deadline": "<时间, 可省>"}
  ],
  "variables": [
    {"name": "<doc 提到的关键变量>", "current_value": "<当前值>", "flip_threshold": {"value": "<翻转阈值>", "direction": "above|below"}, "weight": 0.5, "data_source": "<doc 引用源>", "source": "from-doc"}
  ],
  "time_constraints": ["<时间窗口 / 截止 / 节奏 约束>"],
  "key_assumptions": ["<doc 没明说但暗含的核心假设>"],
  "doc_title": "<doc 一级标题>",
  "doc_summary": "<≤ 800 字摘要, 保留 doc 原话作 claim, 不要 paraphrase 改变意思>",
  "project_name": "<可选: 若为竞赛产品说明书, 填参赛项目名; 无则省略或 null>",
  "team": "<可选: 若 doc 含参赛组名/团队名, 填; 无则省略或 null>"
}

**严格纪律**:
- 只提取 doc 已有内容, 不要生成新 claim / 新 variables
- variables 至少 3 个, decisions 至少 1 个 (若 doc 没明显方案, 抛 ERROR)
- doc_summary 不超过 800 字, 不复述 doc 全文
- project_name / team 仅竞赛产品说明书有, 非竞赛 doc 省略即可 (它们是可选字段)
- 若 doc 不像方案 (没 claim 没 decision), 输出 {"error": "doc 不像方案, 建议用 FRESH 模式"}
"""


# _read_doc_text / _cap_doc_text 已下沉 boss_core/docio.py (M0.1b), 文件顶部 re-export。


def _loads_lenient(raw: str) -> dict:
    """宽松解析 LLM 的 JSON 输出: 去 markdown ```json``` 壳 + 取最外层 {...} 跨度
    (兼容 GLM 等加前后缀文字的端点)。仍是合法 JSON 才返回; 截断/真错抛 JSONDecodeError。"""
    s = raw.strip()
    if s.startswith("```"):
        s = s[3:]
        if s[:4].lower() == "json":
            s = s[4:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b > a:
        s = s[a:b + 1]
    import json as _j
    return _j.loads(s)


def _parse_review_doc(doc_path: str, cfg: PipelineConfig, log: Logger) -> ReviewDocParse:
    """REVIEW Phase 0: LLM 解析 doc → 结构化 claims/decisions/variables.

    Args:
        doc_path: doc 路径 (vault 相对)
        cfg: PipelineConfig (含 model_fast)
        log: Logger

    Returns:
        ReviewDocParse

    Raises:
        PipelineError: doc 不存在 / 不像方案 / LLM 调用失败
    """
    path = _resolve_review_doc_path(doc_path)
    if not path.exists():
        raise PipelineError(f"REVIEW doc 不存在: {doc_path}")

    content = _read_doc_text(path)
    if len(content.strip()) < 100:
        raise PipelineError(f"REVIEW doc 内容过短 (< 100 字符, 或文档无可提取文字): {doc_path}")

    raw_chars = len(content)
    content, truncated = _cap_doc_text(content, REVIEW_DOC_MAX_CHARS)
    if truncated:
        log.warn(f"REVIEW doc 过长: {raw_chars} chars → 截断到 {len(content)} "
                 f"(上限 REVIEW_DOC_MAX_CHARS={REVIEW_DOC_MAX_CHARS}); 避免超大 POST 触发网关 WAF / 超 token")

    log.step("Phase 0", f"REVIEW doc parser · 读 {doc_path} ({raw_chars} chars"
             f"{f' → 截断 {len(content)}' if truncated else ''})")

    # dry-run: 返 placeholder, 不 LLM call
    if cfg.dry_run:
        log.info("  · dry-run · 返 placeholder ReviewDocParse")
        return ReviewDocParse(
            topic="<dry-run-extracted-topic>",
            topic_type="strategic",
            claims=["<dry-run claim 1>", "<dry-run claim 2>", "<dry-run claim 3>"],
            decisions=[{"action": "<dry-run decision>", "owner": "TBD", "deadline": "TBD"}],
            variables=[
                {"name": f"<dry-run-var-{i}>",
                 "current_value": "TBD",
                 "flip_threshold": {"value": "TBD", "direction": "above"},
                 "weight": 0.5,
                 "data_source": "doc",
                 "source": "from-doc"}
                for i in range(1, 4)
            ],
            time_constraints=["<dry-run-time-constraint>"],
            key_assumptions=["<dry-run-assumption>"],
            doc_title=path.stem,
            doc_summary=f"<dry-run summary of {path.name}>",
        )

    # 真 LLM call
    import json as _json

    user_prompt = f"""## 待 review 的方案 doc

文件: {doc_path}
长度: {len(content)} chars

----- doc 内容开始 -----
{content}
----- doc 内容结束 -----

请按 system prompt 要求输出 JSON.
"""

    if not cfg.no_redact:
        _check_redact_or_raise(
            _REVIEW_DOC_PARSER_SYSTEM + "\n\n" + user_prompt,
            log,
            label="phase-0-review-doc-parser-prompt",
        )

    # 兼容端点偶发: ① JSON 截断/加壳 (max_tokens 不足/推理模型吃 token);
    # ② schema 跑偏 — 模型"评审"了文档而非"解析"成固定字段 (gpt-5.x 等指令遵循弱的常见)。
    # max_tokens 8000 + 宽松提取 {...} + 至多 3 次重试 (JSON 错 / schema 缺字段 均重试,
    # 缺字段时附更强硬的 reprompt 把模型拉回"解析而非评审")。
    required_fields = ["topic", "topic_type", "claims", "decisions", "variables",
                       "time_constraints", "key_assumptions", "doc_title", "doc_summary"]
    data = None
    last_err: Optional[Exception] = None
    raw = ""
    for attempt in (1, 2, 3):
        user = user_prompt
        if attempt > 1:
            user = user_prompt + (
                f"\n\n⚠️ 上次输出不合规 ({last_err})。你必须**只**输出一个 JSON, 字段严格为: "
                f"{', '.join(required_fields)}。这是把方案文档**解析**成结构化字段的任务, "
                f"**不是**评审文档 — 不要输出 overall_score / critical_issues / suggestions "
                f"等评审字段, 不要 code fence 之外的文字。"
            )
        response = _call_llm(
            cfg, model=cfg.model_fast, max_tokens=8000,
            system=_REVIEW_DOC_PARSER_SYSTEM, user=user, phase="phase_0",
        )
        raw = response.text.strip()
        if not cfg.no_redact:
            _check_redact_or_raise(raw, log, label="phase-0-review-doc-parser-response")
        try:
            cand = _loads_lenient(raw)
        except _json.JSONDecodeError as e:
            last_err = e
            log.warn(f"doc parser JSON 解析失败 (尝试 {attempt}/3): {e}")
            continue
        if "error" in cand:          # 模型判定 doc 不像方案 — 不重试
            data = cand
            break
        missing = [f for f in required_fields if f not in cand]
        if missing:
            last_err = ValueError(
                f"缺字段 {missing} (模型实际返回 {list(cand.keys())[:6]}… — 疑似把解析当成了评审)")
            log.warn(f"doc parser schema 不符 (尝试 {attempt}/3): {last_err}")
            continue
        data = cand
        break
    if data is None:
        log.err(f"原始响应 (末 600 字): ...{raw[-600:]}")
        raise PipelineError(f"REVIEW doc parser 输出非合规 (重试 3 次仍失败: {last_err})")

    # doc 不像方案
    if "error" in data:
        raise PipelineError(
            f"REVIEW doc parser 判定: {data['error']}. "
            f"建议: 走 FRESH 模式, 把 doc 内容作议题字符串."
        )
    # required_fields 已在循环内保证

    # decisions 归一成 list[dict] (端点常返回字符串列表 → 下游 d.get 崩); 归一后再做 len 守卫
    data["decisions"] = _normalize_decisions(data.get("decisions"))

    if len(data["claims"]) < 1:
        raise PipelineError("REVIEW doc 缺核心 claim (< 1), 不像方案")
    if len(data["decisions"]) < 1:
        raise PipelineError("REVIEW doc 缺具体 decision (< 1), 不像方案")
    if len(data["variables"]) < 3:
        raise PipelineError(f"REVIEW doc 提取 variables < 3 (实际 {len(data['variables'])}), 信息不足")

    # 可选字段归一: 模型常把"无"填成字符串 "null"/"none"/空白 (而非省略), 这些都当 None,
    # 让下游回退 (project_name → doc_title; team → "—"), 否则排名榜会显示字面 "null"。
    parsed = ReviewDocParse(
        topic=data["topic"],
        topic_type=data["topic_type"],
        claims=data["claims"],
        decisions=data["decisions"],
        variables=data["variables"],
        time_constraints=data["time_constraints"],
        key_assumptions=data["key_assumptions"],
        doc_title=data["doc_title"],
        doc_summary=data["doc_summary"],
        project_name=_clean_optional_str(data.get("project_name")),
        team=_clean_optional_str(data.get("team")),
    )

    log.info(f"  · doc_title={parsed.doc_title}")
    log.info(f"  · topic_type={parsed.topic_type}")
    log.info(f"  · claims={len(parsed.claims)} · decisions={len(parsed.decisions)} · variables={len(parsed.variables)}")
    return parsed


def _allocate_new_case_id() -> str:
    """扫 cases/ 找下一个可用编号 C-YYYY-NNNN (year 用当前年)"""
    year = datetime.now().year
    if not CASES_DIR.exists():
        return f"C-{year}-0001"
    existing = []
    for p in CASES_DIR.iterdir():
        if not p.is_dir():
            continue
        m = CASE_ID_RE.match(p.name)
        if m and int(m.group(1)) == year:
            existing.append(int(m.group(2)))
    next_n = (max(existing) + 1) if existing else 1
    return f"C-{year}-{next_n:04d}"


def _find_latest_case_for_brand(brand_slug: str, log: Logger) -> Optional[str]:
    """EVOLUTION: 扫 cases/ 找 brand_slug 匹配 + created_at 最新的 case_id"""
    if not CASES_DIR.exists():
        return None
    candidates: list[tuple[str, str]] = []  # (created_at, case_id)
    for p in CASES_DIR.iterdir():
        if not p.is_dir() or not CASE_ID_RE.match(p.name):
            continue
        case_json = p / "case.json"
        if not case_json.exists():
            continue
        try:
            data = json.loads(case_json.read_text(encoding="utf-8"))
            if data.get("brand_slug") == brand_slug:
                candidates.append((data.get("created_at", ""), p.name))
        except (json.JSONDecodeError, OSError) as e:
            log.warn(f"case.json 读取失败 {case_json}: {e}")
    if not candidates:
        return None
    candidates.sort(reverse=True)  # 最新在前
    return candidates[0][1]


def _resolve_review_doc_path(doc_path: str) -> Path:
    """Resolve a REVIEW input doc and ensure it stays inside the vault.

    REVIEW mode sends the doc body to an external LLM. Keeping the input path
    inside the vault makes that data boundary explicit and auditable.
    """
    root = VAULT_ROOT.resolve()
    p = Path(doc_path)
    resolved = (root / p).resolve() if not p.is_absolute() else p.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise PipelineError(f"REVIEW doc 必须位于 vault 内: {doc_path}")
    return resolved


# ──────────────────────────────────────────────────────────────────────
# Wiki 退化方案 — grep raw/ (B 方案核心)
# ──────────────────────────────────────────────────────────────────────

@dataclass
class WikiHit:
    """grep 命中的一个片段"""
    source_path: str                 # 相对 vault 根
    keyword: str
    line_no: int
    snippet: str                     # 含前后 context 的多行片段


# WikiEntityHit / WIKI_DIR / query_wiki_entities 已下沉 boss_core/wiki_query.py (M0.1d),
# 文件顶部 re-export。


def wiki_query_fallback(
    keywords: list[str],
    max_hits_per_keyword: int = 3,
    max_total: int = 10,
    log: Optional[Logger] = None,
) -> list[WikiHit]:
    """
    B 方案: 用 grep 替代 sage-wiki query。
    扫 raw/clippings/ + anchors/<slug>/raw/interviews/
    返回最多 max_total 个 WikiHit (按关键字密度排序)。

    Phase 1 强制要求: 0 命中 → 上层应返回 context: insufficient, 拒绝继续。
    """
    if log is None:
        log = Logger()

    valid_dirs = [d for d in RAW_DIRS_FOR_WIKI_FALLBACK if d.exists()]
    if not valid_dirs:
        log.warn(f"wiki_query_fallback: 无任何 raw/ 目录可扫")
        return []

    all_hits: list[WikiHit] = []
    for kw in keywords:
        if not kw.strip():
            continue
        # grep -rn -A 3 -B 1 <kw> raw/...
        # -F 把关键字当字面字符串 (避免正则元字符)
        # 排除 _meta/ (filter manifest 等元数据) / .bak.* (备份) / .review/ (待人审)
        # 只扫 .md 文件 (跳过 binary / pdf / 图片)
        try:
            result = subprocess.run(
                ["grep", "-rnF", "-A", "3", "-B", "1",
                 "--include=*.md",
                 "--exclude-dir=_meta",
                 "--exclude-dir=.review",
                 "--exclude=*.bak",
                 "--exclude=*.bak.*",
                 "--", kw, *map(str, valid_dirs)],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            log.warn(f"grep 超时 (kw={kw!r}, 30s)")
            continue
        except FileNotFoundError:
            log.err("grep 命令找不到 — 请确认系统 PATH")
            return []

        # grep 没命中 → exit 1, stdout 空 (正常); 真错误 → exit 2
        if result.returncode == 2:
            log.warn(f"grep 异常 kw={kw!r}: {result.stderr[:200]}")
            continue

        hits = _parse_grep_output(result.stdout, kw, max_hits_per_keyword)
        all_hits.extend(hits)
        log.dbg(f"kw={kw!r} → {len(hits)} hits")

    # 简单去重: 同一 (source, line_no) 合并
    seen = set()
    deduped: list[WikiHit] = []
    for h in all_hits:
        key = (h.source_path, h.line_no)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(h)

    return deduped[:max_total]


def _hosted_kb(log: Optional[Logger] = None) -> HostedKB:
    """CLI 满档 KBProvider 实例 (M1.3 接线点统一入口)。

    K-a: 不降档 → query 默认 tier="tian_only" 零裁剪; K-b: redact_egress=False
    存原文 (内部私有边界, 脱敏只在公网出口 — CLAUDE.md §9.3)。rpl-resident 依赖
    (doctrine / panel 路径 / grep 兜底) 在此注入 (K-c, boss_core 不反向 import)。
    """
    return HostedKB(
        load_doctrine_fn=_load_judge_skill,
        resolve_panel_path_fn=_resolve_panel_path,
        wiki_fallback_fn=wiki_query_fallback,
        log=log,
    )


def _parse_grep_output(stdout: str, keyword: str, max_hits: int) -> list[WikiHit]:
    """
    grep -A/-B 输出格式:
        <file>-<line_no>-<context_line>
        <file>:<line_no>:<match_line>
        <file>-<line_no>-<context_line>
        --
        <next match...>
    用 -- 分隔 chunk。每个 chunk 取首个 ':' 行作为主匹配。
    """
    hits: list[WikiHit] = []
    chunks = stdout.split("\n--\n")
    for chunk in chunks:
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        # 找主匹配行 (含 ':<line>:<content>')
        main_path = None
        main_line = None
        snippet_lines: list[str] = []
        for ln in lines:
            # 路径行用 : 或 - 作分隔。-A/-B 内的 context 行用 '-' 分隔。
            # 鲁棒做法: split 取前两段 + 剩余
            # 优先匹配 ':' (实际匹配行)
            m = re.match(r"^(.+?):(\d+):(.*)$", ln)
            if m and main_path is None:
                main_path = m.group(1)
                main_line = int(m.group(2))
                snippet_lines.append(m.group(3))
                continue
            # context 行 (-A/-B)
            m2 = re.match(r"^(.+?)-(\d+)-(.*)$", ln)
            if m2:
                snippet_lines.append(m2.group(3))
        if main_path and main_line is not None:
            # 路径相对化
            try:
                rel = str(Path(main_path).resolve().relative_to(VAULT_ROOT))
            except (ValueError, OSError):
                rel = main_path
            hits.append(WikiHit(
                source_path=rel,
                keyword=keyword,
                line_no=main_line,
                snippet="\n".join(snippet_lines).strip()[:1000],
            ))
            if len(hits) >= max_hits:
                break
    return hits


# ──────────────────────────────────────────────────────────────────────
# Phase 1 — PRD + Context (LLM provider)
# ──────────────────────────────────────────────────────────────────────

PHASE_1_SYSTEM_PROMPT_HEAD = """\
你是锚点判断流水线的 Phase 1 协调员。基于用户给出的议题描述, 完成两件事:

1. 起草一份 PRD (产品需求文档), 用 markdown 输出, 必含字段:
   - topic: 议题原文 (一句话)
   - topic_type: 选 strategic | product | organizational | customer | financial | brand | cross_domain | meta_framework | unknown
   - trigger_event: { named_event, occurred_at, source_url? }
   - stakeholders: [...]
   - time_window: { deadline, external_anchor }
   - constraints: ≥ 3 条, 含 legal/equity/resource 类
   - investigation_dimensions: 4-7 条调研维度, 给 Phase 2 sub-agent 派发用

2. 写一份 Context 段, 引用 Wiki 背景。当前 B 方案: Wiki 不可用, 给你的是 grep
   命中片段 (WikiHit 列表)。你必须:
   - 优先引用片段 (markdown link: [[anchors/<slug>/raw/xxx.md]] @ line N)
   - 0 命中时直接返回 `context: insufficient`, 不要凭空发挥
   - 不要把 raw 内容大段拷贝, 只摘核心 3-5 句

输出格式: 一段 YAML frontmatter (PRD 字段) + 一段 Markdown body (Context)。
"""


def phase_1_prd_context(
    cfg: PipelineConfig,
    case_id: str,
    log: Logger,
    forced_topic_type: Optional[str] = None,
    skip_write_dry_run: bool = False,
) -> dict[str, Any]:
    """
    Phase 1: 起草 PRD + 拉 Context (grep fallback)。
    Dry-run: 不调 LLM, 仅打印计划 + 跑 grep 看候选 hits。

    Returns:
        {
          "prd_path": str (relative),
          "context_path": str,
          "wiki_hits": int,
          "topic_type": str,
          "tokens_in": int,
          "tokens_out": int,
          "cost_usd": float,
        }
    """
    log.step("Phase 1", f"起草 PRD + Context (topic={cfg.topic[:60]!r}, brand={cfg.brand_slug})")

    case_dir = CASES_DIR / case_id
    case_dir.mkdir(parents=True, exist_ok=True)

    # 1. 从议题里抽关键字 (启发式: 取所有 ≥ 2 字的中文 + 英文 token)
    keywords = _extract_keywords(cfg.topic)
    log.dbg(f"keywords: {keywords}")

    # 2a. P1 #3 修 (dev-plan v2.14): 优先查 _wiki/<type>/, fallback 到 grep
    # M1.3: KB 访问统一走 KBProvider (满档 CLI 实例, 行为与直调逐字一致)
    kb = _hosted_kb(log)
    entity_hits = kb.query(cfg.topic, keywords=keywords, topic_raw=cfg.topic)
    if entity_hits:
        log.step("Phase 1", f"_wiki/ 拉到 {len(entity_hits)} 个 entity hits "
                            f"({sum(1 for h in entity_hits if h.type == 'people')} people / "
                            f"{sum(1 for h in entity_hits if h.type == 'entities')} entities / "
                            f"{sum(1 for h in entity_hits if h.type == 'concepts')} concepts)")
    else:
        log.step("Phase 1", "_wiki/ 0 hits (未编译或无匹配) — 全靠 grep fallback")

    # 2b. grep fallback (raw 原文片段)
    hits = kb.query_fallback_raw(keywords, log=log)
    log.step("Phase 1", f"grep 拉到 {len(hits)} 个 raw hits")

    # 3. 写 context.md (entity + grep 双源)
    # P1 #5 修 (dev-plan v2.12): EVOLUTION + dry-run 不破坏 v1 rolling 文件
    context_path = case_dir / "context.md"
    prd_path = case_dir / "prd.md"
    if not skip_write_dry_run:
        _write_context_md(context_path, cfg, hits, entity_hits=entity_hits)
    else:
        log.info(f"  · skip write context.md (EVOLUTION+dry-run, 保护 v1 rolling)")

    if cfg.dry_run:
        log.info("DRY-RUN: 不调 LLM. 模拟产出 prd.md (占位) 并估算 token 预算")
        if not skip_write_dry_run:
            prd_path.write_text(_dry_run_prd_placeholder(cfg, hits), encoding="utf-8")
        else:
            log.info(f"  · skip write prd.md (EVOLUTION+dry-run, 保护 v1 rolling)")
        return {
            "prd_path": str(prd_path.relative_to(VAULT_ROOT)),
            "context_path": str(context_path.relative_to(VAULT_ROOT)),
            "wiki_hits": len(hits),
            "topic_type": forced_topic_type or "(dry-run · LLM 未调)",
            "tokens_in": _estimate_input_tokens_phase_1(cfg, hits),
            "tokens_out": 3000,  # 估算 output
            "cost_usd": 0.0,
        }

    # 4. 真 LLM 调用 · 0 命中保护 (P1 #3 修后, entity 或 grep 任一非空即可)
    if len(hits) == 0 and len(entity_hits) == 0:
        log.warn(
            "wiki + grep 双源 0 命中 — Phase 1 强制规则: 不让 LLM 凭空发挥。"
            "建议补 raw/clippings/ 素材后重跑, 或先跑 scripts/build_wiki.py 编译 _wiki/, "
            "或人工起草 PRD 然后 --resume-from phase-2。"
        )
        raise PipelineError("context: insufficient — wiki + grep 双源 0 命中, 拒绝继续")

    log.step("Phase 1", f"调 {cfg.model_fast} 起草 PRD ...")
    response = _call_anthropic_phase_1(cfg, hits, log)

    prd_path.write_text(response["text"], encoding="utf-8")

    # 解析 topic_type (frontmatter 第一行); EVOLUTION 模式锁 v1 topic_type
    topic_type = _extract_topic_type(response["text"]) or DEFAULT_TOPIC_TYPE
    if forced_topic_type:
        if topic_type != forced_topic_type:
            log.warn(f"Phase 1 LLM 推断 topic_type={topic_type}, 但 EVOLUTION 锁定 v1 类型={forced_topic_type}. 用锁定值.")
        topic_type = forced_topic_type

    log.step("Phase 1", f"PRD 落盘 {prd_path.relative_to(VAULT_ROOT)} · topic_type={topic_type}")

    return {
        "prd_path": str(prd_path.relative_to(VAULT_ROOT)),
        "context_path": str(context_path.relative_to(VAULT_ROOT)),
        "wiki_hits": len(hits),
        "topic_type": topic_type,
        "tokens_in": response["tokens_in"],
        "tokens_out": response["tokens_out"],
        "cost_usd": response["cost_usd"],
    }


def phase_1_review_from_doc(
    cfg: PipelineConfig,
    case_id: str,
    parsed_doc: 'ReviewDocParse',
    log: Logger,
    skip_write_dry_run: bool = False,
) -> dict:
    """REVIEW mode (ADR-007) Phase 1: 把 parsed doc 渲染成 PRD-shape, 跳 LLM 调用.

    与 phase_1_prd_context() 的区别:
    - 不调 LLM 起草 PRD (doc 本身就是 PRD)
    - 仍做 Wiki Background query (entity/people 引用)
    - prd.md 渲染 parsed_doc.doc_summary + claims + decisions
    - context.md 渲染 Wiki Background + raw refs (同 FRESH)

    Returns: dict 与 phase_1_prd_context() 兼容 (prd_path/context_path/topic_type/...).
    """
    log.step("Phase 1 (REVIEW)", f"渲染 parsed doc → prd.md (跳 LLM, doc 本身是 PRD)")

    case_dir = CASES_DIR / case_id
    case_dir.mkdir(parents=True, exist_ok=True)

    # 跑 Wiki query · 拿 entity 引用 (复用 FRESH 逻辑)
    keywords = _extract_keywords(cfg.topic or parsed_doc.topic)
    keywords.extend(_extract_keywords(parsed_doc.doc_title))  # doc 标题也作 keyword 源
    keywords = list(dict.fromkeys(keywords))[:20]  # 去重 + cap

    # M1.3: KB 访问统一走 KBProvider; REVIEW 路径 keywords 显式传 (拼接场景),
    # topic_raw 不传 (保持历史命中集不变)
    kb = _hosted_kb(log)

    # entity hits
    entity_hits = kb.query(cfg.topic or parsed_doc.topic, keywords=keywords)
    log.info(f"  · Wiki entity hits: {len(entity_hits)}")

    # raw hits
    raw_hits = kb.query_fallback_raw(keywords, log=log)
    log.info(f"  · grep raw hits: {len(raw_hits)}")

    # 写 prd.md (REVIEW shape)
    # doc_title 是 LLM 抽取的自由文本, 可能含 ':' / 引号 / 换行 → 经 _yaml_quote_inline
    # 转义, 换行先折叠成空格 (single-quoted 标量不容多行), 防 frontmatter 破损
    _safe_doc_title = _yaml_quote_inline(" ".join((parsed_doc.doc_title or "").split()))
    prd_lines = [
        "---",
        f"brand_slug: {cfg.brand_slug}",
        f"case_id: {case_id}",
        f"mode: REVIEW",
        f"review_doc_path: {cfg.review_doc_path}",
        f"topic_type: {parsed_doc.topic_type}",
        f"doc_title: {_safe_doc_title}",
        f"created_at: {_now_iso()}",
        "---",
        "",
        f"# REVIEW · {parsed_doc.doc_title}",
        "",
        f"## 议题 (从 doc 推)",
        "",
        parsed_doc.topic,
        "",
        f"## §A · 原方案摘要 (Phase 0 doc parser)",
        "",
        parsed_doc.doc_summary,
        "",
        f"### 核心 claims",
        "",
    ]
    for c in parsed_doc.claims:
        prd_lines.append(f"- {c}")
    prd_lines.append("")
    prd_lines.append("### Decisions (doc 提议)")
    prd_lines.append("")
    for d in parsed_doc.decisions:
        line = f"- **action**: {d.get('action', 'TBD')}"
        if d.get('owner'):
            line += f" · **owner**: {d['owner']}"
        if d.get('deadline'):
            line += f" · **deadline**: {d['deadline']}"
        prd_lines.append(line)
    prd_lines.append("")
    prd_lines.append("### 时间约束")
    prd_lines.append("")
    for t in parsed_doc.time_constraints:
        prd_lines.append(f"- {t}")
    prd_lines.append("")
    prd_lines.append("### 核心假设 (doc 暗含)")
    prd_lines.append("")
    for a in parsed_doc.key_assumptions:
        prd_lines.append(f"- {a}")
    prd_lines.append("")
    prd_text = "\n".join(prd_lines)

    prd_path = case_dir / "prd.md"
    if not skip_write_dry_run:
        prd_path.write_text(prd_text, encoding='utf-8')
        log.info(f"  · 写 {prd_path}")
    else:
        log.info(f"  · skip write prd.md (dry-run protect)")

    # 写 context.md (Wiki Background + raw refs · 与 FRESH 同结构)
    context_path = case_dir / "context.md"
    if not skip_write_dry_run:
        _write_context_md(context_path, cfg, raw_hits, entity_hits=entity_hits)
        log.info(f"  · 写 {context_path}")
    else:
        log.info(f"  · skip write context.md (dry-run protect)")

    return {
        "prd_path": str(prd_path),
        "context_path": str(context_path),
        "topic_type": parsed_doc.topic_type,
        "wiki_hits": len(entity_hits) + len(raw_hits),
        "tokens_in": 0,                  # REVIEW Phase 1 不调 LLM
        "tokens_out": 0,
        "cost_usd": 0.0,
        "review_mode": True,             # flag for downstream phases
    }


# _extract_keywords 已下沉 boss_core/wiki_query.py (M0.1d), 文件顶部 re-export。


def _write_context_md(path: Path, cfg: PipelineConfig,
                       hits: list[WikiHit],
                       entity_hits: Optional[list[WikiEntityHit]] = None) -> None:
    """
    落盘 context.md.

    P1 #3 修 (dev-plan v2.14): 两源拼接 — _wiki/<type>/ entity 引用 (优先) + grep 片段 (兜底).
    若 _wiki/ 空, 仍输出 grep 段维持向后兼容.
    """
    entity_hits = entity_hits or []
    mode = "wiki+grep" if entity_hits else "grep_fallback"
    lines = [
        "---",
        f"case_id: (待 Phase 1 分配)",
        f"brand_slug: {cfg.brand_slug}",
        f"phase: 1",
        f"generated_at: '{_now_iso()}'",
        f"wiki_query_mode: {mode}",
        f"wiki_entity_hits: {len(entity_hits)}",
        f"wiki_grep_hits: {len(hits)}",
        "---",
        "",
        f"# Context · {cfg.brand_slug}",
        "",
        f"> 议题: {cfg.topic}",
        "",
    ]

    # ── 优先段: _wiki/ entity 引用 (P1 #3 修) ──
    if entity_hits:
        lines.append("## Background from Wiki (entity 引用)")
        lines.append("")
        lines.append("> 来自 `_wiki/<type>/<slug>.md` (由 `scripts/build_wiki.py` 编译). 含 canonical name / role / profile / mention 数 / 反向引用.")
        lines.append("")
        # 按 type 分组
        for type_dir, label in (("people", "人物"), ("entities", "实体"), ("concepts", "概念")):
            type_hits = [h for h in entity_hits if h.type == type_dir]
            if not type_hits:
                continue
            lines.append(f"### {label} ({len(type_hits)})")
            lines.append("")
            for h in type_hits:
                meta_bits = []
                if h.role:
                    meta_bits.append(f"role: {h.role}")
                elif h.entity_type:
                    meta_bits.append(f"type: {h.entity_type}")
                meta_bits.append(f"mentions: {h.mention_count}")
                meta_bits.append(f"sensitivity: `{h.sensitivity}`")
                if h.related_judgements_count:
                    meta_bits.append(f"related judgements: {h.related_judgements_count}")
                meta_line = " · ".join(meta_bits)
                lines.append(f"- **[{h.canonical}](../../_wiki/{h.type}/{h.slug}.md)** (matched `{h.matched_keyword}`) — {meta_line}")
                if h.profile:
                    for pl in h.profile.splitlines():
                        if pl.strip():
                            lines.append(f"  - {pl.strip()}")
            lines.append("")
        lines.append("---")
        lines.append("")

    # ── 兜底段: grep 片段 ──
    lines.append("## Background from raw (grep fallback)")
    lines.append("")
    if not hits:
        lines.append("> grep 0 命中。" + ("Entity 引用已覆盖 background, 流水线可继续。" if entity_hits else "Wiki + grep 双源 0 命中, Phase 1 上层应拒绝继续。"))
    else:
        for h in hits:
            lines.append(f"### [{h.source_path}](../../{h.source_path}) @ line {h.line_no} (kw={h.keyword!r})")
            lines.append("")
            lines.append("```")
            lines.append(h.snippet)
            lines.append("```")
            lines.append("")

    # ── 战略 OS M3: 该场景在册指标注入 (评「目标是否量化」有真数可对; fail-open) ──
    try:
        from boss_core.loop.metrics import metrics_context_block
        _mb = metrics_context_block(VAULT_ROOT / "strategy",
                                    _scene_slug_from_panel(cfg.panel))
        if _mb:
            lines.append("")
            lines.append(_mb)
    except Exception:  # noqa: BLE001 — 指标注入失败绝不阻断 Phase 1
        pass

    lines.append("---")
    lines.append("")
    lines.append("*注: 本 Context 由 grep_fallback 生成 (sage-wiki 未部署时的退化方案)。*")
    path.write_text("\n".join(lines), encoding="utf-8")


def _dry_run_prd_placeholder(cfg: PipelineConfig, hits: list[WikiHit]) -> str:
    """dry-run 模式: 产一份说明性 prd.md, 不调 LLM"""
    return f"""---
DRY_RUN: true
topic: {_yaml_quote_inline(cfg.topic)}
brand_slug: {cfg.brand_slug}
generated_at: '{_now_iso()}'
note: |
  本文件为 --dry-run 模式占位, 未调 LLM。
  真跑时 (去掉 --dry-run) 将由 {cfg.model_fast} 起草完整 PRD。
---

# PRD (dry-run placeholder)

议题: {cfg.topic}

预计 grep 拉到 {len(hits)} 个 wiki hits, LLM 会基于这些 hits 起草 PRD。

预算估算: ~{_estimate_input_tokens_phase_1(cfg, hits)} input tokens / 3000 output tokens / ~$0.07 USD.

去掉 --dry-run 即真跑。
"""


def _extract_topic_type(prd_text: str) -> Optional[str]:
    """从 PRD frontmatter 拉 topic_type 字段"""
    m = re.search(r"^topic_type:\s*([a-z_]+)\s*$", prd_text, re.MULTILINE)
    return m.group(1) if m else None


def _estimate_input_tokens_phase_1(cfg: PipelineConfig, hits: list[WikiHit]) -> int:
    """token 估算 (粗算: 1 char ≈ 0.5 token 中文, 1 token 英文)"""
    system_chars = len(PHASE_1_SYSTEM_PROMPT_HEAD)
    topic_chars = len(cfg.topic)
    hits_chars = sum(len(h.snippet) for h in hits)
    return int((system_chars + topic_chars + hits_chars) * 0.5) + 500  # +500 buffer


# ──────────────────────────────────────────────────────────────────────
# LLM SDK 封装
# ──────────────────────────────────────────────────────────────────────

# 管理台成本估算价目 (USD / 1M token, [input, output])。按模型名子串匹配 (兼容
# wangsu/anthropic.claude-opus-4-8 之类前缀), 命中不了回退 Sonnet 价 (与流水线既有默认一致)。
# 仅供管理台粗看花费量级, 非结算依据; 价目变了改这里即可。
_MODEL_PRICE_PER_MTOK: list[tuple[str, float, float]] = [
    ("opus", 15.0, 75.0),
    ("sonnet", 3.0, 15.0),
    ("haiku", 0.80, 4.0),
    ("gpt-4o-mini", 0.15, 0.60),
    ("gpt-4o", 2.50, 10.0),
    ("gpt-5", 1.25, 10.0),
    ("glm", 0.60, 2.20),
]
_DEFAULT_PRICE_PER_MTOK = (3.0, 15.0)   # 回退 = Sonnet


def _estimate_cost(model: Optional[str], tokens_in: int, tokens_out: int) -> float:
    """按模型名粗估一次调用花费 (USD)。未知模型回退 Sonnet 价。"""
    name = (model or "").lower()
    price_in, price_out = _DEFAULT_PRICE_PER_MTOK
    for key, pin, pout in _MODEL_PRICE_PER_MTOK:
        if key in name:
            price_in, price_out = pin, pout
            break
    return (tokens_in or 0) / 1_000_000 * price_in + (tokens_out or 0) / 1_000_000 * price_out


def _emit_llm_usage(phase: str, result: "llm_client.LLMResult") -> None:
    """把一次 LLM 调用的 token/花费落进管理台遥测 (按 job/phase 聚合)。fail-open。
    job_id / scene 由 worker 经 env BOSS_JOB_ID / BOSS_SCENE_SLUG 注入; CLI 手跑时为空。"""
    try:
        import telemetry
        telemetry.record_llm_usage(
            scene=os.environ.get("BOSS_SCENE_SLUG") or None,
            job_id=os.environ.get("BOSS_JOB_ID") or None,
            phase=phase, model=result.model,
            tokens_in=result.input_tokens, tokens_out=result.output_tokens,
            cost_usd=_estimate_cost(result.model, result.input_tokens, result.output_tokens))
    except Exception:  # noqa: BLE001
        pass


# study-weekly 自省评委钉低温 (降跨次分数漂移): 同一份周报跑出 83 B+/76 B 的漂移,
# 金标准单测只锁机械层锁不住 AI 打分。此为**硬钉** (override), **无视全局 BOSS_LLM_TEMPERATURE**
# —— 因为线上 .env 常设了非 0 的全局温度 (如 0.3), 若仅作"软默认"会被盖掉、形同虚设。
# 只作用于 study-weekly 评委, 不动 op2/cadre/boss 等其他场景的全局温度。
STUDY_WEEKLY_JUDGE_TEMPERATURE = 0.0


def _pipeline_temperature(default: Optional[float] = None) -> Optional[float]:
    """BOSS_LLM_TEMPERATURE 环境旋钮 (评审可复现: 低温降分数漂移, PRD §14)。
    优先级: 环境旋钮 (运维) > 传入 default (场景默认) > None (API 默认)。
    未设/非法 → 退 default (默认 None = 不传, 与接线前 bit 级等价)。评审场景建议 0-0.3。"""
    raw = os.environ.get("BOSS_LLM_TEMPERATURE", "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _resolve_temperature(override: Optional[float] = None) -> Optional[float]:
    """温度优先级: **硬钉 override (无视 env) > 环境 BOSS_LLM_TEMPERATURE > None (API 默认)**。
    study-weekly 评委传 override=0 强制可复现, 即便运维在 .env 设了全局非 0 温度也压不过它。"""
    if override is not None:
        return override
    return _pipeline_temperature()


# 每 phase 的 LLM 墙钟秒累加 (profile 提速用: 看哪个 phase 最吃时间)。run_pipeline 开头 reset;
# ⚠ phase-4 评委并行, 这里是**各评委累加**非墙钟 (仍能指出"评委阶段 LLM 工作量最大")。串行 phase (1/3/5) 即墙钟。
_PHASE_LLM_SECONDS: dict[str, float] = {}


def _call_llm(
    cfg: PipelineConfig,
    *,
    model: str,
    max_tokens: int,
    system: str,
    user: str,
    phase: str = "other",
    temperature_override: Optional[float] = None,
) -> llm_client.LLMResult:
    import time
    _t0 = time.perf_counter()
    try:
        result = llm_client.complete(
            provider=cfg.llm_provider,
            model=model,
            max_tokens=max_tokens,
            system=system,
            user=user,
            base_url=cfg.llm_base_url,
            api_key_env=cfg.llm_api_key_env,
            temperature=_resolve_temperature(temperature_override),
        )
    except llm_client.LLMClientError as exc:
        raise PipelineError(str(exc)) from exc
    _PHASE_LLM_SECONDS[phase] = _PHASE_LLM_SECONDS.get(phase, 0.0) + (time.perf_counter() - _t0)
    _emit_llm_usage(phase, result)
    return result

def _call_anthropic_phase_1(
    cfg: PipelineConfig, hits: list[WikiHit], log: Logger,
) -> dict[str, Any]:
    """调配置的 LLM provider 起草 PRD。"""

    # 用户消息: 议题 + grep hits
    hits_text = "\n\n".join([
        f"### Hit {i+1} · {h.source_path}:{h.line_no} (kw={h.keyword!r})\n\n{h.snippet}"
        for i, h in enumerate(hits)
    ])
    user_msg = (
        f"# 议题\n\n{cfg.topic}\n\n"
        f"# brand_slug\n\n{cfg.brand_slug}\n\n"
        f"# Wiki Hits (grep fallback, {len(hits)} 条)\n\n{hits_text}\n\n"
        f"请按 system prompt 格式产出 PRD frontmatter + Context body。"
    )

    if cfg.verbose:
        log.dbg(f"--- system prompt (头 200 字) ---\n{PHASE_1_SYSTEM_PROMPT_HEAD[:200]}...")
        log.dbg(f"--- user msg (头 400 字) ---\n{user_msg[:400]}...")

    # redact 检查 prompt
    if not cfg.no_redact:
        _check_redact_or_raise(PHASE_1_SYSTEM_PROMPT_HEAD + "\n\n" + user_msg, log)

    response = _call_llm(
        cfg,
        model=cfg.model_fast,
        max_tokens=4096,
        system=PHASE_1_SYSTEM_PROMPT_HEAD,
        user=user_msg,
        phase="phase_1",
    )
    text = response.text
    tokens_in = response.input_tokens
    tokens_out = response.output_tokens
    # Sonnet 4.6 定价 (设计 note §5): $3 / M input, $15 / M output
    cost = tokens_in / 1_000_000 * 3.0 + tokens_out / 1_000_000 * 15.0

    # redact 检查 response
    if not cfg.no_redact:
        _check_redact_or_raise(text, log, label="response")

    return {"text": text, "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost}


def _check_redact_or_raise(text: str, log: Logger, label: str = "prompt") -> None:
    """跑 redact_check.check_text(); 命中则 raise"""
    try:
        import redact_check
        blocked, hits = redact_check.check_text(text, f"<{label}>")
        if blocked:
            log.err(f"redact {label} 命中: {[h.pattern_id for h in hits]}")
            raise PipelineError(f"redact 阻断 ({label}): {len(hits)} 命中")
    except ImportError:
        log.warn("redact_check 不可 import — 跳过 (--no-redact 等价)")


# ──────────────────────────────────────────────────────────────────────
# Phase 2 — Parallel Research Sub-agents
# ──────────────────────────────────────────────────────────────────────

PHASE_2_CONCURRENCY = int(os.environ.get("PHASE_2_CONCURRENCY", "3"))  # 同时跑的 sub-agent 上限 (env 可降; 端点/网关频控严时设 1-2)
PHASE_2_TIMEOUT_SEC = 120

PHASE_2_SYSTEM_PROMPT = """\
你是锚点判断流水线 Phase 2 的研究 sub-agent。你被分配一个调研维度, 任务是基于
议题 + Wiki Context 产出该维度的"raw evidence"。

7 调研维度参考 (CLAUDE.md §5.4):
  1. 触发事件链 (events & timing) — 谁/何时/源头
  2. 关键变量与阈值 — 决策驱动变量, 当前值, 翻转阈值
  3. 法律/协议/股权约束 — 不可逆约束
  4. 客户/用户感知信号 — 真实需求 vs 自我投射
  5. 竞品/玩家动作 — 同业最近 30/90 天动作
  6. 内部资源/团队约束 — 组织能承的边界
  7. 时机/外部催化窗口 — 大趋势锚点

你的输出要求 (markdown):
- YAML frontmatter (案例归属 + sub_agent 自报 + generated_at)
- ≥ 3 段独立证据 ("## 证据 N · <小标题>"), 每段:
  * 一句话核心结论
  * 来源 (引 [[raw/...]] / URL / 推断标注)
  * relevance 1-5 (本证据对当前议题的相关度)
- 不要起 Phase 3 / 4 的活: 只列证据, 不合成, 不打分
- 不要凭空发挥: Context 没提到的事实标 "[推断]" + 1-2 句推断依据
"""


def phase_2_parallel_research(
    cfg: PipelineConfig,
    case_id: str,
    research_dims: list[str],        # 研究维度 slug 列表 (= panel judges 去掉 tian)
    context_md: str,                  # Phase 1 context.md 原文
    log: Logger,
    skip_write_dry_run: bool = False,  # P1 #5: EVOLUTION + dry-run 时跳写 rolling
) -> dict[str, Any]:
    """
    并行派发 sub-agents 调研各维度。failed_dims 不阻断其他, 写占位符。

    Returns:
        {
          "raw_evidence_paths": [str, ...],
          "failed_dims": [str, ...],
          "tokens_in": int, "tokens_out": int, "cost_usd": float,
        }
    """
    log.step("Phase 2", f"派发 {len(research_dims)} 维 sub-agents · 并发 ≤ {PHASE_2_CONCURRENCY}")

    case_dir = CASES_DIR / case_id
    raw_dir = case_dir / "raw_evidence"
    if not skip_write_dry_run:
        raw_dir.mkdir(parents=True, exist_ok=True)

    if cfg.dry_run:
        if skip_write_dry_run:
            log.info(f"DRY-RUN Phase 2 (EVOLUTION 保护): skip write {len(research_dims)} 个 dim_*.md")
        else:
            log.info(f"DRY-RUN Phase 2: 落 {len(research_dims)} 个占位 dim_*.md, 不调 LLM")
        paths = []
        for dim in research_dims:
            f = raw_dir / f"dim_{dim.replace('-', '_')}.md"
            if not skip_write_dry_run:
                f.write_text(_dry_run_raw_evidence_placeholder(cfg, case_id, dim), encoding="utf-8")
            paths.append(str(f.relative_to(VAULT_ROOT)))
        return {
            "raw_evidence_paths": paths,
            "failed_dims": [],
            "tokens_in": 6000 * len(research_dims),
            "tokens_out": 5000 * len(research_dims),
            "cost_usd": 0.09 * len(research_dims),
        }

    # 真跑 — asyncio.gather 并行
    results = asyncio.run(_phase_2_async(cfg, case_id, research_dims, context_md, raw_dir, log))

    paths = []
    failed = []
    total_in = 0
    total_out = 0
    total_cost = 0.0
    for dim, res in zip(research_dims, results):
        if isinstance(res, Exception):
            log.err(f"  · dim={dim} FAIL · {type(res).__name__}: {res}")
            failed.append(dim)
            # 占位文件让 verify 仍能找到 dim_*.md
            f = raw_dir / f"dim_{dim.replace('-', '_')}.md"
            f.write_text(_failed_dim_placeholder(cfg, case_id, dim, str(res)), encoding="utf-8")
            paths.append(str(f.relative_to(VAULT_ROOT)))
            continue
        # 成功
        f = raw_dir / f"dim_{dim.replace('-', '_')}.md"
        f.write_text(res["text"], encoding="utf-8")
        paths.append(str(f.relative_to(VAULT_ROOT)))
        total_in += res["tokens_in"]
        total_out += res["tokens_out"]
        total_cost += res["cost_usd"]
        log.step("Phase 2", f"  · dim={dim} ✓ {res['tokens_in']}+{res['tokens_out']} tok")

    if failed:
        log.warn(f"Phase 2: {len(failed)} 维失败 (写占位), {len(research_dims) - len(failed)} 维成功")
    else:
        log.step("Phase 2", f"全 {len(research_dims)} 维成功 · 总 ${total_cost:.3f}")

    return {
        "raw_evidence_paths": paths,
        "failed_dims": failed,
        "tokens_in": total_in,
        "tokens_out": total_out,
        "cost_usd": total_cost,
    }


async def _phase_2_async(
    cfg: PipelineConfig, case_id: str, dims: list[str], context_md: str,
    raw_dir: Path, log: Logger,
) -> list[Any]:
    """并发跑所有 dim sub-agents · 用 to_thread 把 sync SDK 调用挂到线程池"""
    sem = asyncio.Semaphore(PHASE_2_CONCURRENCY)

    async def run_one(dim: str):
        async with sem:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(_call_anthropic_phase_2, cfg, case_id, dim, context_md, log),
                    timeout=PHASE_2_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                raise PipelineError(f"dim={dim} 超时 ({PHASE_2_TIMEOUT_SEC}s)")
            except Exception as e:
                # 让 gather(return_exceptions=True) 收
                raise

    return await asyncio.gather(*[run_one(d) for d in dims], return_exceptions=True)


def _call_anthropic_phase_2(
    cfg: PipelineConfig, case_id: str, dim: str, context_md: str, log: Logger,
) -> dict[str, Any]:
    """单 dim sub-agent · 同步调用 (asyncio.to_thread 包外)"""
    user_msg = (
        f"# 议题\n\n{cfg.topic}\n\n"
        f"# brand_slug\n\n{cfg.brand_slug}\n\n"
        f"# case_id\n\n{case_id}\n\n"
        f"# 你的调研维度\n\n{dim}\n\n"
        f"# Phase 1 Context (含 Wiki 命中片段)\n\n{context_md}\n\n"
        f"请按 system prompt 格式产出 raw_evidence (frontmatter + ≥ 3 段证据)。"
        f"frontmatter 中 dimension 字段填 {dim!r}, sub_agent 填 {dim}@run_pipeline_local。"
    )

    if not cfg.no_redact:
        _check_redact_or_raise(user_msg, log, label=f"phase-2-{dim}-prompt")

    response = _call_llm(
        cfg,
        model=cfg.model_fast,
        max_tokens=4096,
        system=PHASE_2_SYSTEM_PROMPT,
        user=user_msg,
        phase="phase_2",
    )
    text = response.text
    tokens_in = response.input_tokens
    tokens_out = response.output_tokens
    cost = tokens_in / 1_000_000 * 3.0 + tokens_out / 1_000_000 * 15.0

    if not cfg.no_redact:
        _check_redact_or_raise(text, log, label=f"phase-2-{dim}-response")

    return {"text": text, "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost}


def _dry_run_raw_evidence_placeholder(cfg: PipelineConfig, case_id: str, dim: str) -> str:
    return f"""---
DRY_RUN: true
case_id: {case_id}
brand_slug: {cfg.brand_slug}
dimension: {dim}
phase: 2
sub_agent: dry-run@placeholder
generated_at: '{_now_iso()}'
---

# Raw Evidence · {dim} (dry-run placeholder)

> 本文件为 --dry-run 模式占位, 未调 LLM。真跑会由 {cfg.model_fast}
> 基于议题 + Phase 1 context.md 产出 ≥ 3 段独立证据。

## 证据 1 · (dry-run)
真跑时此段为 sub-agent 调研产出。

## 证据 2 · (dry-run)
真跑时此段为 sub-agent 调研产出。

## 证据 3 · (dry-run)
真跑时此段为 sub-agent 调研产出。
"""


def _failed_dim_placeholder(cfg: PipelineConfig, case_id: str, dim: str, err: str) -> str:
    """Phase 2 单 dim 失败的占位 · verify 仍能数到, 内容标记 status=failed"""
    safe_err = err.replace("\n", " ")[:200]
    return f"""---
case_id: {case_id}
brand_slug: {cfg.brand_slug}
dimension: {dim}
phase: 2
sub_agent: {dim}@run_pipeline_local
generated_at: '{_now_iso()}'
status: failed
error: {_yaml_quote_inline(safe_err)}
---

# Raw Evidence · {dim} (FAILED)

> ⚠️ 本维度 sub-agent 失败, 写占位。Phase 3 合成时应跳过此维度或标 [missing]。
> Phase 4 评委读到 status=failed 时应 falsifiability 镜头扣分。

错误: {safe_err}
"""


# ──────────────────────────────────────────────────────────────────────
# Phase 3 — Lead Synthesis
# ──────────────────────────────────────────────────────────────────────

# PHASE_3_SYSTEM_PROMPT 已下沉 boss_core/prompts.py (M2.0a), 文件顶部 re-export。


_REVIEW_QUERY_GEN_SYSTEM = (
    "你是 boss 流水线 REVIEW 模式的检索策略助手。给你一份方案文档的议题 + 核心 claims, "
    "为「用真实 web 证据印证/挑战这份方案」生成 4-6 条**具体、可检索**的搜索 query。\n"
    "覆盖: 行业趋势 / 竞品动作 / 关键数字与市场规模 / 风险与反方信号。query 要短、含专名或年份。\n"
    "输出**纯 JSON** (无解释、无围栏): {\"queries\": [\"...\", \"...\"]}"
)


def _review_search_queries(cfg: PipelineConfig, parsed_doc: 'ReviewDocParse', log: Logger) -> list[str]:
    """从 doc 议题 + claims 生成 4-6 条搜索 query (1 次 model_fast 调用); 失败 → 退化用议题本身。"""
    fallback = [parsed_doc.topic] if parsed_doc.topic else []
    if cfg.dry_run:
        return fallback
    try:
        claims = "\n".join(f"- {c}" for c in (parsed_doc.claims or [])[:8])
        resp = _call_llm(cfg, model=cfg.model_fast, max_tokens=400,
                         system=_REVIEW_QUERY_GEN_SYSTEM, phase="phase_2",
                         user=f"# 议题\n{parsed_doc.topic}\n\n# 核心 claims\n{claims}")
        data = _loads_lenient(resp.text)
        qs = [str(q).strip() for q in (data.get("queries") or []) if str(q).strip()]
        return qs[:6] or fallback
    except Exception as e:
        log.warn(f"  · query 生成失败, 退化用议题本身: {type(e).__name__}: {e}")
        return fallback


def phase_2_review_verify(cfg: PipelineConfig, case_id: str, parsed_doc: 'ReviewDocParse',
                          context_md: str, log: Logger) -> dict[str, Any]:
    """REVIEW Phase 2 (ADR-007 C3 · v1.1): 用真实 web 检索印证/挑战 doc claims, 喂给 Phase 3 synthesis。

    无 TAVILY_API_KEY / BRAVE_API_KEY → **优雅跳过** (返回空 web_evidence, 行为 = 旧 REVIEW, 不报错)。
    web_evidence 经 synthesis 进入各评委 (CLAUDE.md §4.5 评委读 synthesis.md)。
    """
    empty = {"web_evidence": "", "raw_evidence_paths": [], "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    try:
        from _fetchers import websearch_provider, web_search
    except Exception:
        return empty
    provider = websearch_provider()
    if provider is None:
        log.step("Phase 2 (REVIEW + --verify)",
                 "无 TAVILY_API_KEY / BRAVE_API_KEY — 跳过实时调研 (降级 = 旧 REVIEW)")
        return empty
    if cfg.dry_run:
        log.info("DRY-RUN Phase 2 (REVIEW verify): 不调 web")
        return empty

    queries = _review_search_queries(cfg, parsed_doc, log)
    log.step("Phase 2 (REVIEW + --verify)", f"web 检索 {len(queries)} query via {provider}")
    blocks: list[str] = []
    for q in queries:
        res = web_search(q)
        if res:
            blocks.append(f"### query: {q}\n\n{res}")
    if not blocks:
        log.info("  · web 检索 0 结果, 降级无 web")
        return empty

    web_evidence = "\n\n".join(blocks)
    if not cfg.no_redact:
        _check_redact_or_raise(web_evidence, log, label="phase-2-review-websearch")
    raw_dir = CASES_DIR / case_id / "raw_evidence"
    raw_dir.mkdir(parents=True, exist_ok=True)
    ev_path = raw_dir / "dim_websearch.md"
    ev_path.write_text(
        f"---\ncase_id: {case_id}\ndimension: websearch\nphase: 2\nprovider: {provider}\n"
        f"generated_at: '{_now_iso()}'\n---\n\n# REVIEW 实时 Web 调研 ({len(queries)} query)\n\n{web_evidence}\n",
        encoding="utf-8")
    log.info(f"  ✓ web 调研落盘 dim_websearch.md ({len(blocks)}/{len(queries)} query 有结果)")
    return {"web_evidence": web_evidence,
            "raw_evidence_paths": [str(ev_path.relative_to(VAULT_ROOT))],
            "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}


def phase_3_review_synthesis(
    cfg: PipelineConfig,
    case_id: str,
    parsed_doc: 'ReviewDocParse',
    context_md: str,
    log: Logger,
    skip_write_dry_run: bool = False,
    web_evidence: str = "",
) -> dict[str, Any]:
    """REVIEW Phase 3 (ADR-007): doc claims 分析 + identify gap.

    与 phase_3_synthesis 区别:
    - 输入是 parsed_doc (含 claims/decisions/variables/assumptions), 不是 raw_evidence
    - synthesis 内容是"对 doc 的分析", 不是"研究证据合成"
    - 输出仍是 synthesis.md, 走 LLM (除 dry-run)
    """
    log.step("Phase 3 (REVIEW)", f"分析 doc claims + 识别 gap · {len(parsed_doc.claims)} claims · {len(parsed_doc.decisions)} decisions")
    case_dir = CASES_DIR / case_id
    synth_path = case_dir / "synthesis.md"

    if cfg.dry_run:
        placeholder = (
            f"---\nmode: REVIEW\ncase_id: {case_id}\n---\n\n"
            f"# REVIEW synthesis · dry-run placeholder\n\n"
            f"## doc claims ({len(parsed_doc.claims)})\n"
            + "\n".join([f"- {c}" for c in parsed_doc.claims])
            + f"\n\n## identified gaps (dry-run placeholder)\n"
            f"- <gap-1>\n- <gap-2>\n- <gap-3>\n"
        )
        synth_path.write_text(placeholder, encoding='utf-8')
        log.info("DRY-RUN Phase 3 (REVIEW): 占位 synthesis.md")
        return {
            "synthesis_path": str(synth_path.relative_to(VAULT_ROOT)),
            "tokens_in": 8000, "tokens_out": 3000, "cost_usd": 0.04,
        }

    # 真 LLM call
    import json as _json
    claims_text = "\n".join([f"  - {c}" for c in parsed_doc.claims])
    decisions_text = "\n".join([f"  - {d.get('action', 'TBD')}" for d in parsed_doc.decisions])
    assumptions_text = "\n".join([f"  - {a}" for a in parsed_doc.key_assumptions])

    # C3: 实时 web 调研证据 (有则注入, 让 synthesis 用真实证据印证/挑战 doc claims)
    web_section = (f"\n## 实时 Web 调研 (Tavily/Brave — 用来印证/挑战 doc 的 claims/数字/趋势)\n{web_evidence}\n"
                   if web_evidence else "")
    web_task_note = ("\n> 注: 上方有「实时 Web 调研」时, identify gaps / 跨视角矛盾 / 共识 三段须优先引 web 证据"
                     " 印证或挑战 doc 的 claims、数字、趋势 (web 与 doc 冲突处重点标出)。" if web_evidence else "")

    user_msg = f"""# REVIEW · 分析方案 doc

议题 (从 doc 推): {parsed_doc.topic}
topic_type: {parsed_doc.topic_type}
doc: {cfg.review_doc_path}

## doc 核心 claims
{claims_text}

## doc 提议 decisions
{decisions_text}

## doc 暗含核心假设
{assumptions_text}

## doc 摘要
{parsed_doc.doc_summary}

## Wiki Background (供交叉)
{context_md}
{web_section}
## 任务
你不是要研究新议题, 是要**分析这个方案 doc**. 输出 synthesis.md (frontmatter + body), 含:{web_task_note}

### body 段:
1. **核心 claim 清单** (从 doc 原文提, 不要 paraphrase)
2. **杠杆变量** (5-7 个, 从 doc 提的 variables 出发, 标 `source: from-doc`)
3. **identify gaps** — doc 没回答的关键问题 (≥ 3 条)
4. **跨视角矛盾** — doc 各 section 之间的内部矛盾或未对齐 (若有)
5. **共识** — doc 哪些 claim 看起来稳 (≥ 3 条)
6. **锚点 5 镜头初评** (lever_clarity / counter_position / specificity / falsifiability / actionability 各打 1-10)

### frontmatter:
mode: REVIEW
case_id: {case_id}
review_doc_path: {cfg.review_doc_path}
created_at: {_now_iso()}
"""

    if not cfg.no_redact:
        _check_redact_or_raise(user_msg, log, label="phase-3-review-prompt")

    response = _call_llm(
        cfg,
        model=cfg.model_fast,
        max_tokens=6144,
        system="你是 boss 流水线 Phase 3 (REVIEW mode) — 分析一份方案 doc, 而不是研究 open question. 严格按用户要求输出 synthesis.md.",
        user=user_msg,
        phase="phase_3",
    )
    text = response.text
    if not skip_write_dry_run:
        synth_path.write_text(text, encoding='utf-8')
    log.step("Phase 3 (REVIEW)", f"synthesis 落盘 · in={response.input_tokens} out={response.output_tokens}")

    return {
        "synthesis_path": str(synth_path.relative_to(VAULT_ROOT)),
        "tokens_in": response.input_tokens,
        "tokens_out": response.output_tokens,
        "cost_usd": (response.input_tokens * 3 + response.output_tokens * 15) / 1_000_000,
    }


def phase_3_synthesis(
    cfg: PipelineConfig,
    case_id: str,
    raw_evidence_paths: list[str],   # phase 2 输出的相对路径
    context_md: str,                  # phase 1 的 context (作背景)
    failed_dims: list[str],
    log: Logger,
    skip_write_dry_run: bool = False,  # P1 #5: EVOLUTION + dry-run 保护
) -> dict[str, Any]:
    """合成 synthesis.md. 一次 LLM 调用, sonnet 4.6."""
    log.step("Phase 3", f"合成 synthesis · {len(raw_evidence_paths)} 维输入 ({len(failed_dims)} 失败)")

    case_dir = CASES_DIR / case_id
    synth_path = case_dir / "synthesis.md"

    if cfg.dry_run:
        if skip_write_dry_run:
            log.info("DRY-RUN Phase 3 (EVOLUTION 保护): skip write synthesis.md")
        else:
            log.info("DRY-RUN Phase 3: 落占位 synthesis.md")
            synth_path.write_text(_dry_run_synthesis_placeholder(cfg, case_id, raw_evidence_paths, failed_dims), encoding="utf-8")
        return {
            "synthesis_path": str(synth_path.relative_to(VAULT_ROOT)),
            "tokens_in": 25000, "tokens_out": 6000, "cost_usd": 0.16,
        }

    # 读所有 raw_evidence
    evidences_concat = []
    for rel_path in raw_evidence_paths:
        abs_path = VAULT_ROOT / rel_path
        if not abs_path.exists():
            log.warn(f"raw_evidence 文件缺失: {rel_path}")
            continue
        evidences_concat.append(f"\n\n=== {rel_path} ===\n\n{abs_path.read_text(encoding='utf-8')}")
    evidences_text = "".join(evidences_concat)

    response = _call_anthropic_phase_3(cfg, case_id, context_md, evidences_text, failed_dims, log)
    synth_path.write_text(response["text"], encoding="utf-8")

    log.step("Phase 3", f"synthesis 落盘 {synth_path.relative_to(VAULT_ROOT)} · ${response['cost_usd']:.3f}")

    return {
        "synthesis_path": str(synth_path.relative_to(VAULT_ROOT)),
        "tokens_in": response["tokens_in"],
        "tokens_out": response["tokens_out"],
        "cost_usd": response["cost_usd"],
    }


def _call_anthropic_phase_3(
    cfg: PipelineConfig, case_id: str, context_md: str, evidences_text: str,
    failed_dims: list[str], log: Logger,
) -> dict[str, Any]:
    failed_note = ""
    if failed_dims:
        failed_note = f"\n\n⚠️ Phase 2 失败维度: {failed_dims}. synthesis 应在 frontmatter failed_dims 标记, 并在'跨维度矛盾'段说明影响。"

    user_msg = (
        f"# 议题\n\n{cfg.topic}\n\n"
        f"# brand_slug · case_id\n\n{cfg.brand_slug} · {case_id}\n\n"
        f"# Phase 1 Context\n\n{context_md}\n\n"
        f"# Phase 2 Raw Evidence (各 dim 拼接)\n{evidences_text}\n\n"
        f"{failed_note}\n\n"
        f"请按 system prompt 格式产出 synthesis.md 全文 (frontmatter + body)。"
    )

    if not cfg.no_redact:
        _check_redact_or_raise(user_msg, log, label="phase-3-prompt")

    response = _call_llm(
        cfg,
        model=cfg.model_fast,
        max_tokens=6144,
        system=PHASE_3_SYSTEM_PROMPT,
        user=user_msg,
        phase="phase_3",
    )
    text = response.text
    tokens_in = response.input_tokens
    tokens_out = response.output_tokens
    cost = tokens_in / 1_000_000 * 3.0 + tokens_out / 1_000_000 * 15.0

    if not cfg.no_redact:
        _check_redact_or_raise(text, log, label="phase-3-response")

    return {"text": text, "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost}


def _dry_run_synthesis_placeholder(cfg: PipelineConfig, case_id: str,
                                    raw_evidence_paths: list[str],
                                    failed_dims: list[str]) -> str:
    return f"""---
DRY_RUN: true
case_id: {case_id}
brand_slug: {cfg.brand_slug}
phase: 3
generated_at: '{_now_iso()}'
research_dims_count: {len(raw_evidence_paths)}
failed_dims: {failed_dims}
---

# Synthesis · {cfg.brand_slug} (dry-run placeholder)

> 本文件为 --dry-run 模式占位, 未调 LLM。真跑会由 {cfg.model_fast} 合成。

## 执行摘要
真跑时此段为 Lead 合成的 3-5 句议题总览。

## 杠杆地图
真跑时此段列 3-5 个关键变量 (来自 raw_evidence)。

## 脆弱边缘
真跑时此段列最易证伪的假设。

## 跨维度矛盾
真跑时此段列 dim 间冲突。

## 引用索引
{chr(10).join(f'- {p}' for p in raw_evidence_paths)}
"""


# ──────────────────────────────────────────────────────────────────────
# Phase 4 — 评委独立打分 (data-isolated parallel, 设计 note §6.3)
# ──────────────────────────────────────────────────────────────────────

PHASE_4_CONCURRENCY = int(os.environ.get("PHASE_4_CONCURRENCY", "3"))  # 并发上限 (env 可降; 端点限流严时设 1-2)
PHASE_4_TIMEOUT_SEC = 180            # Opus 推理慢, 给 3 分钟
# 单评委超时/失败自动重试次数 (总尝试次数, 含首次)。2 = 首次挂了再重试 1 次, 偶发抖动自愈;
# 只有连着挂 PHASE_4_MAX_ATTEMPTS 次才写 FAILED 占位 (2026-07-04: capital-market 单次超时暴露)。
PHASE_4_MAX_ATTEMPTS = max(1, int(os.environ.get("PHASE_4_MAX_ATTEMPTS", "2")))

# CLAUDE.md §5 lens
SCORING_LENSES = [
    "reasoning_soundness",
    "evidence_thesis_coupling",
    "counter_position_treatment",
    "falsifiability",
    "real_world_resilience",
]

# ─── scoring spec · panel 驱动的打分模式 (sum_max_score 真打分支持) ───
# 默认 weighted_average + 5 镜头 1-10 (与历史完全一致)。op2-company / workshop-midyear
# 等 panel 用 scoring_mode: sum_max_score + scoring_lenses_override (自定义维度 + max_score),
# refresh_scoring_spec(panel) 在 Phase 0 后刷新此模块全局, Phase 4 prompt / Phase 5 聚合读它。
# 设计对齐 ANCHOR_JUDGES / refresh_anchor_judges (panel 缺失/解析失败 → 兜底 weighted)。
SCORING_SPEC_DEFAULT: dict[str, Any] = {
    "mode": "weighted_average",
    "lens_slugs": list(SCORING_LENSES),
    "lenses": [],            # sum_max 时: [{slug, display_name_cn, max_score, description}]
    "total_max": 0,
    "score_threshold": None,
    "has_anchor": True,
}
SCORING_SPEC: dict[str, Any] = dict(SCORING_SPEC_DEFAULT)

# 场景输出格式 (panel.yaml output_format) — 由 refresh_scoring_spec 从解析后 panel 捕获,
# 供 Phase 4 prompt 装配感知 (study_weekly_v8 → v8 六段式契约)。None = 通用打分报告 (零变化)。
OUTPUT_FORMAT: Optional[str] = None


def refresh_scoring_spec(panel_name: str, log: Logger | None = None) -> dict[str, Any]:
    """从 panel (panel_loader 解析后) 推导打分规格, 刷新模块全局 SCORING_SPEC。

    weighted_average (默认): 5 镜头 1-10, 行为与历史完全一致。
    sum_max_score: 用 panel scoring_lenses (各含 max_score) 作打分维度, 满分相加。
    解析失败 / panel 无 override → 兜底 weighted (零行为变化)。"""
    global SCORING_SPEC, OUTPUT_FORMAT
    spec = dict(SCORING_SPEC_DEFAULT)
    output_format = None
    try:
        # M1.3: panel 解析走 KBProvider; M2.0b: 推导体纯化进 boss_core.scoring (R-a:
        # 全局刷新语义留本 wrapper, 服务侧 per-run 直调 derive_scoring_spec)
        resolved = _hosted_kb(log).resolve_panel(panel_name)
        spec = _scoring.derive_scoring_spec(resolved, default_lens_slugs=SCORING_LENSES)
        if isinstance(resolved, dict):
            output_format = resolved.get("output_format")   # study-weekly M2: v8 六段式接线
    except Exception as e:
        if log is not None:
            log.warn(f"refresh_scoring_spec({panel_name}) 失败: {e} — 兜底 weighted 5 镜头")
    if log is not None and spec["mode"] != SCORING_SPEC.get("mode"):
        if spec["mode"] == "sum_max_score":
            log.step("Panel", f"scoring_mode=sum_max_score · {len(spec['lenses'])} 维度 / "
                              f"满分 {spec['total_max']} / anchor={'有' if spec['has_anchor'] else '无'}")
        else:
            log.step("Panel", "scoring_mode=weighted_average · 5 镜头 1-10")
    SCORING_SPEC = spec
    OUTPUT_FORMAT = output_format
    return spec


def _scene_slug_from_panel(panel_name: str) -> Optional[str]:
    """从 scene panel 路径 'scenes/<slug>/panel.yaml' 反推 scene_slug; 非 scene 路径返回 None。"""
    m = re.search(r"scenes/([^/]+)/panel\.ya?ml$", str(panel_name))
    return m.group(1) if m else None


def _is_competition_scoring() -> bool:
    # 逻辑已下沉 boss_core/scoring.py (M0.3); 薄 wrapper 注入当前 SCORING_SPEC。
    return _scoring._is_competition_scoring(SCORING_SPEC)

# anchor 评委 slug (不参与维度加权, 不写 adversarial_view)
# v0.8 (R13 前置) · anchor 评委集合 — panel 驱动, 不再硬编码
# refresh_anchor_judges(panel) 在 Phase 0 后刷新; {"tian"} 仅兜底
# (panel 缺失 / 解析失败 / 无 anchor 条目时, 行为与 v0.7 完全一致)
ANCHOR_JUDGES_FALLBACK = frozenset({"tian"})
ANCHOR_JUDGES: set[str] = set(ANCHOR_JUDGES_FALLBACK)


def refresh_anchor_judges(panel_name: str, log: Logger | None = None) -> set[str]:
    """从 panels/<name>.yaml judges[].judge_category == 'anchor' 推导 anchor 评委集合,
    刷新模块全局 ANCHOR_JUDGES (11 处 `judge in ANCHOR_JUDGES` 调用点零改动)。
    M2.0b: 推导体纯化进 boss_core.kb.vault_paths.derive_anchor_judges (服务侧 per-run 直调)。"""
    global ANCHOR_JUDGES
    anchors = _vault_paths.derive_anchor_judges(
        _resolve_panel_path(panel_name), fallback=ANCHOR_JUDGES_FALLBACK)
    if log is not None and anchors != ANCHOR_JUDGES:
        log.step("Panel", f"anchor 评委 (panel 驱动): {sorted(anchors)}")
    ANCHOR_JUDGES = anchors
    return anchors

# ANCHOR_RESEARCH_CAPS / _anchor_confidence_cap 已下沉 boss_core/anchor_research.py
# (M2.0b), 文件顶部 re-export (rpl 内部调用点运行时查本模块 globals, monkeypatch 语义不变)。


def _clamp_anchor_confidence(path: Path, judge: str, log: Logger) -> None:
    """R1.3 后置强制: anchor review 的 confidence 超 cap 时 clamp 并显式标注。
    不静默 — 加 confidence_capped_by 字段让 Phase 5 与审计可见。"""
    cap, state = _anchor_confidence_cap(judge)
    if cap is None:
        return
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^(confidence:\s*)([0-9.]+)\s*$", text, flags=re.MULTILINE)
    if not m:
        return
    try:
        val = float(m.group(2))
    except ValueError:
        return
    if val <= cap:
        return
    replacement = (
        f"{m.group(1)}{cap}\n"
        f"confidence_capped_by: \"anchor_research_state={state} · R1.3 · original {val}\""
    )
    path.write_text(text[:m.start()] + replacement + text[m.end():], encoding="utf-8")
    log.warn(f"  · judge={judge} confidence {val} → clamp {cap} (anchor research={state}, R1.3)")


def _emit_judge_failure_fc(cfg: PipelineConfig, case_id: str, judge: str,
                           error_str: str, log: Logger) -> None:
    """v0.7 R12: Phase 4 评委失败 → 自动写 process_failure Failure Card
    (run-pipeline-local-design §10 验收遗留项落地)。

    注: Phase 4 时 case.json 通常未写 (Phase 5 才落), 用最小 case dict 即可;
    FC 通过 case_id 关联, 项目主理事后在卡内补业务上下文。
    """
    import classify_failure
    fc_dir = VAULT_ROOT / "failure_cards"
    case_path = CASES_DIR / case_id / "case.json"
    case: dict[str, Any] = {"case_id": case_id, "brand_slug": cfg.brand_slug}
    if case_path.exists():
        try:
            case = json.loads(case_path.read_text(encoding="utf-8"))
        except Exception as e:
            # 不阻断 FC 生成, 但保留运维可见性 (PR #7 自审: 不静默吞错)
            log.warn(f"  · FC 生成: 读 {case_path.name} 失败, 用最小 case dict ({type(e).__name__}: {e})")
    classification = classify_failure.Classification(
        type="process_failure",
        confidence=0.9,
        reason=f"Phase 4 评委 {judge} 调用失败, 已写占位 review: {error_str[:200]}",
    )
    # v0.8: 走 classify_failure.write_failure_card 一体化入口 (取号+渲染+写盘)
    fc_path = classify_failure.write_failure_card(
        case, classification, case_path, fc_dir,
        detected_by=f"run_pipeline_local phase-4 judge={judge}",
    )
    log.warn(f"  · judge={judge} 失败 → 自动写 {fc_path.relative_to(VAULT_ROOT)} (process_failure, R12)")


def phase_4_judges(
    cfg: PipelineConfig,
    case_id: str,
    panel_judges: list[str],         # 含 anchor + dimension 评委 slug
    synthesis_md: str,
    context_md: str,
    log: Logger,
    skip_write_dry_run: bool = False,  # P1 #5: EVOLUTION + dry-run 保护
) -> dict[str, Any]:
    """
    每位评委 LOAD 自己的 SKILL.md, 读 synthesis (不读对方 reviews), 写 reviews/<judge>.md。

    数据层独立: 每位评委的 prompt 只含 (自己 SKILL.md + synthesis + context),
    永不含其他评委的 review (CLAUDE.md §4.5)。并发用 asyncio.to_thread.

    fail-soft (P5): 单 judge 失败 → status=failed 占位 review + state.errors;
    其他 judges 继续。
    """
    log.step("Phase 4", f"派发 {len(panel_judges)} 评委 · 并发 ≤ {PHASE_4_CONCURRENCY}")

    reviews_dir = REPORTS_DIR / cfg.brand_slug / "reviews"
    if not skip_write_dry_run:
        reviews_dir.mkdir(parents=True, exist_ok=True)

    if cfg.dry_run:
        if skip_write_dry_run:
            log.info(f"DRY-RUN Phase 4 (EVOLUTION 保护): skip write {len(panel_judges)} reviews")
        else:
            log.info(f"DRY-RUN Phase 4: 落 {len(panel_judges)} 个占位 review.md, 不调 LLM")
        paths = []
        for j in panel_judges:
            f = reviews_dir / f"{j}.md"
            if not skip_write_dry_run:
                f.write_text(_dry_run_review_placeholder(cfg, case_id, j), encoding="utf-8")
            paths.append(str(f.relative_to(VAULT_ROOT)))
        return {
            "review_paths": paths,
            "failed_judges": [],
            "tokens_in": 15000 * len(panel_judges),
            "tokens_out": 4000 * len(panel_judges),
            "cost_usd": 0.39 * len(panel_judges),
        }

    # 真跑 — asyncio.gather 并行
    results = asyncio.run(
        _phase_4_async(cfg, case_id, panel_judges, synthesis_md, context_md, log)
    )

    paths = []
    failed = []
    total_in = 0
    total_out = 0
    total_cost = 0.0
    for judge, res in zip(panel_judges, results):
        f = reviews_dir / f"{judge}.md"
        if isinstance(res, Exception):
            log.err(f"  · judge={judge} FAIL · {type(res).__name__}: {res}")
            failed.append(judge)
            f.write_text(
                _failed_review_placeholder(cfg, case_id, judge, str(res)),
                encoding="utf-8",
            )
            paths.append(str(f.relative_to(VAULT_ROOT)))
            # v0.7 R12: 评委失败自动写 process_failure FC (非致命, 生成失败仅 warn)
            try:
                _emit_judge_failure_fc(cfg, case_id, judge, str(res), log)
            except Exception as fc_err:
                log.warn(f"  · judge={judge} FC 自动生成失败 (非致命): {fc_err}")
            continue
        # 成功 — 落盘前剥最外层代码围栏 (gpt-4o 等偶把整份 review 包进 ```markdown,
        # 否则 frontmatter 解析失败 → 该评委被判废, 多评委 panel 上 ≥2 废即硬失败)
        f.write_text(_strip_review_fence(res["text"]), encoding="utf-8")
        # P1 #2 修 (dev-plan v2.10): yaml frontmatter 自动后处理 (LLM 输出长字符串易撞 yaml 语法)
        try:
            import fix_review_yaml
            status = fix_review_yaml.process_file(f)
            if status == "ok-fixed":
                log.info(f"  · judge={judge} review yaml 自动修复 (LLM 字符串非法)")
            elif status.startswith("fail"):
                log.warn(f"  · judge={judge} review yaml 仍 fail ({status}), Phase 5 / verify 会报")
        except ImportError:
            pass  # fix_review_yaml 可选
        # v0.6 R1.3: anchor review confidence 超 cap 时 clamp + 显式标注
        if judge in ANCHOR_JUDGES:
            try:
                _clamp_anchor_confidence(f, judge, log)
            except Exception as e:
                log.warn(f"  · judge={judge} confidence clamp 失败 (非致命): {e}")
        paths.append(str(f.relative_to(VAULT_ROOT)))
        total_in += res["tokens_in"]
        total_out += res["tokens_out"]
        total_cost += res["cost_usd"]
        log.step("Phase 4", f"  · judge={judge} ✓ {res['tokens_in']}+{res['tokens_out']} tok")

    if failed:
        log.warn(f"Phase 4: {len(failed)}/{len(panel_judges)} 评委失败 (写占位)")
    else:
        log.step("Phase 4", f"全 {len(panel_judges)} 评委成功 · 总 ${total_cost:.3f}")

    return {
        "review_paths": paths,
        "failed_judges": failed,
        "tokens_in": total_in,
        "tokens_out": total_out,
        "cost_usd": total_cost,
    }


async def _phase_4_async(
    cfg: PipelineConfig, case_id: str, judges: list[str],
    synthesis_md: str, context_md: str, log: Logger,
) -> list[Any]:
    """并发跑所有 judge · 用 to_thread 把 sync SDK 调用挂到线程池"""
    sem = asyncio.Semaphore(PHASE_4_CONCURRENCY)

    async def run_one(judge: str):
        async with sem:
            last_err: Exception | None = None
            for attempt in range(1, PHASE_4_MAX_ATTEMPTS + 1):
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(
                            _call_anthropic_phase_4, cfg, case_id, judge,
                            synthesis_md, context_md, log,
                        ),
                        timeout=PHASE_4_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    last_err = PipelineError(f"judge={judge} 超时 ({PHASE_4_TIMEOUT_SEC}s)")
                except Exception as e:  # noqa: BLE001 — 网关抖动/解析等偶发也重试, 非偶发第 2 次仍会挂
                    last_err = e
                if attempt < PHASE_4_MAX_ATTEMPTS:
                    log.warn(f"Phase 4: judge={judge} 第 {attempt}/{PHASE_4_MAX_ATTEMPTS} 次失败 "
                             f"({type(last_err).__name__}: {str(last_err)[:80]}), 重试")
            raise last_err  # 重试用尽 → 抛最后一次错, 上层 gather 收进 failed 写占位

    return await asyncio.gather(*[run_one(j) for j in judges], return_exceptions=True)


# _panel_judge_skill_map / _panel_judge_display_map / _judge_label / _load_judge_skill
# 逻辑已下沉 boss_core/kb/vault_paths.py (M2.0a)。薄 wrapper 注入本模块可 patch 的
# 路径全局与彼此 (缓存在 core 按解析后路径键, 生产语义等价)。

def _panel_judge_skill_map(panel_name: str) -> dict[str, str]:
    return _vault_paths._panel_judge_skill_map(
        panel_name, panel_path=_resolve_panel_path(panel_name))


def _panel_judge_display_map(panel_name: str) -> dict[str, str]:
    return _vault_paths._panel_judge_display_map(
        panel_name, panel_path=_resolve_panel_path(panel_name))


def _judge_label(judge: str, panel_name: str | None) -> str:
    return _vault_paths._judge_label(
        judge, panel_name, display_map_fn=_panel_judge_display_map)


def _load_judge_skill(judge: str, panel_name: str | None = None) -> str:
    return _vault_paths._load_judge_skill(
        judge, panel_name, vault_root=VAULT_ROOT, anchors_dir=ANCHORS_DIR,
        skills_dir=SKILLS_DIR, skill_map_fn=_panel_judge_skill_map)


# _scores_spec_block / _placeholder_scores_block 逻辑已下沉 boss_core/prompts.py (M0.2)。
# 下面是薄 wrapper: 注入当前 (可变) SCORING_SPEC / SCORING_LENSES 全局, 保对外签名不变。

def _scores_spec_block(is_anchor: bool = False) -> tuple[str, str, str]:
    return _prompts._scores_spec_block(SCORING_SPEC, SCORING_LENSES, is_anchor=is_anchor)


def _placeholder_scores_block(fraction: float, is_anchor: bool = False) -> str:
    return _prompts._placeholder_scores_block(
        SCORING_SPEC, SCORING_LENSES, fraction, is_anchor=is_anchor)


# _phase_4_system_prompt 逻辑已下沉 boss_core/prompts.py (M0.2)。薄 wrapper 注入当前
# SCORING_SPEC/SCORING_LENSES/ANCHOR_JUDGES 全局 + _anchor_confidence_cap (skill_lint I/O) 回调。

def _phase_4_system_prompt(judge: str, judge_skill_md: str) -> str:
    return _prompts._phase_4_system_prompt(
        judge, judge_skill_md, SCORING_SPEC, SCORING_LENSES,
        ANCHOR_JUDGES, _anchor_confidence_cap, output_format=OUTPUT_FORMAT)


def _call_anthropic_phase_4(
    cfg: PipelineConfig, case_id: str, judge: str,
    synthesis_md: str, context_md: str, log: Logger,
) -> dict[str, Any]:
    """单评委 sync 调用 (asyncio.to_thread 包外)"""
    # M1.3: doctrine 加载走 KBProvider (委派 _load_judge_skill, 逐字一致)
    judge_skill = _hosted_kb(log).load_doctrine(judge, scene=cfg.panel)
    system_prompt = _phase_4_system_prompt(judge, judge_skill)

    # REVIEW mode (ADR-007): 在 user_msg 头部加 REVIEW context, 让评委知道这是评议而非研究。
    # v8 自省诊断 (study_weekly_v8): 被评议 doc = 一份周报, 走 v8 六段式契约 (system prompt 末尾),
    # 不套通用「5 镜头 + adversarial_view + 修订建议段」(否则与 v8 契约冲突, 评委收到矛盾指令)。
    review_context = ""
    if cfg.review_doc_path and OUTPUT_FORMAT == "study_weekly_v8":
        review_context = (
            f"# ★ 周报自省诊断 (v8)\n\n"
            f"被评议 doc = 一份**周报** (`{cfg.review_doc_path}`)。请按上方 SKILL.md 的 v8 框架 +\n"
            f"system prompt 末尾的 **frontmatter 契约** 诊断: 5 维基础分 + 触发的反向扣分 (deductions) +\n"
            f"岗位价值判断 (position_value) + 改进建议 (suggestions) + 重写示例 (rewrite_example)。\n"
            f"六段式报告由渲染层从 frontmatter 机械生成, 你只需吐 frontmatter (可选一段正文总评)。\n\n"
        )
    elif cfg.review_doc_path:
        review_context = (
            f"# ★ REVIEW mode (ADR-007)\n\n"
            f"这不是研究 open question, 而是**评议一份已含方案的 doc**.\n"
            f"被评议 doc: `{cfg.review_doc_path}`\n\n"
            f"你的任务调整:\n"
            f"1. 从你的 doctrine 角度看, 这份方案的**优点 / 致命弱点 / 反方观点**是什么\n"
            f"2. 5 镜头打分 (与 FRESH 一样)\n"
            f"3. **★ 修订建议**: 在 review body 末尾加 `## 修订建议 (REVIEW)` 段, 写 3 条 '如果是我会改的'\n"
            f"4. adversarial_view 三字段不变 (不适用 anchor judge)\n\n"
        )

    user_msg = (
        f"{review_context}"
        f"# 议题\n\n{cfg.topic}\n\n"
        f"# brand_slug · case_id\n\n{cfg.brand_slug} · {case_id}\n\n"
        f"# Phase 1 Context\n\n{context_md}\n\n"
        f"# Phase 3 Synthesis\n\n{synthesis_md}\n\n"
        f"请按 system prompt 末尾的输出格式写 review (frontmatter + body)。"
    )

    if not cfg.no_redact:
        _check_redact_or_raise(user_msg, log, label=f"phase-4-{judge}-prompt")

    response = _call_llm(
        cfg,
        model=cfg.model_deep,
        max_tokens=4096,
        system=system_prompt,
        user=user_msg,
        phase="phase_4",
        # study-weekly 自省评委硬钉低温, 压跨次分数漂移 (张路 W30 曾 83 B+/76 B 漂移)。
        # override 无视全局 BOSS_LLM_TEMPERATURE (线上常设 0.3, 软默认会被盖掉)。
        # 其他场景传 None → 走全局 env / API 默认, 行为不变。
        temperature_override=(
            STUDY_WEEKLY_JUDGE_TEMPERATURE if OUTPUT_FORMAT == "study_weekly_v8" else None
        ),
    )
    text = response.text
    tokens_in = response.input_tokens
    tokens_out = response.output_tokens
    # Opus 4.7 价格 (USD/M tokens): $15 in / $75 out
    cost = tokens_in / 1_000_000 * 15.0 + tokens_out / 1_000_000 * 75.0

    if not cfg.no_redact:
        _check_redact_or_raise(text, log, label=f"phase-4-{judge}-response")

    # study_weekly_v8 单评委: 整单成败系于此 review 的 frontmatter 能否解析 + 过区间校验。
    # LLM 偶发吐畸形 YAML (长中文串带冒号/引号) 或越界扣分 → fix_review_yaml 修不动 →
    # Phase 5 / verify exit 5, 成员收到"评审失败"。在此就地校验, 不过则 raise, 触发 run_one
    # 的既有 PHASE_4_MAX_ATTEMPTS 重试重新生成 (LLM 非确定性, 重试多半吐出合法 YAML)。
    if OUTPUT_FORMAT == "study_weekly_v8":
        _assert_study_weekly_review_ok(text, judge, log)

    return {"text": text, "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost}


def _assert_study_weekly_review_ok(text: str, judge: str, log: Logger) -> None:
    """study_weekly_v8 review 就地校验: frontmatter 可解析 + 通过区间校验; 不过则 raise
    (→ Phase 4 重试)。fix_review_yaml 先规整 (剥围栏/提 frontmatter/引号修复) 再判。"""
    try:
        import fix_review_yaml
        import study_weekly_output as _swo
    except ImportError:
        return  # 依赖缺失就不拦 (退回原行为)
    fixed, _mod, status = fix_review_yaml.fix_review_frontmatter(text)
    if status.startswith("fail"):
        raise PipelineError(f"study_weekly_v8 review frontmatter 无法解析 ({status}) — 触发重试")
    problems = _swo.validate_review_text(fixed)
    if problems:
        raise PipelineError(
            f"study_weekly_v8 review 区间校验不过 ({'; '.join(problems[:3])}) — 触发重试")
    log.step("Phase 4", f"  · judge={judge} v8 review 就地校验通过")


def _dry_run_review_placeholder(cfg: PipelineConfig, case_id: str, judge: str) -> str:
    """dry-run 模式的占位 review · 格式与真跑产出一致, 让 smoke_e2e.verify 仍能过"""
    category = "anchor" if judge in ANCHOR_JUDGES else "dimension"
    scores_block = _placeholder_scores_block(fraction=0.5, is_anchor=(category == "anchor"))
    if category == "dimension":
        adversarial_block = """adversarial_view:
  if_thesis_wrong: 'dry-run placeholder · 真跑由 {judge} 评委填写'
  contrary_signal_observed: 'dry-run placeholder · 真跑由 {judge} 评委填写'
  base_rate_warning: 'dry-run placeholder · 真跑由 {judge} 评委填写'
""".replace("{judge}", judge)
    else:
        adversarial_block = ""

    return f"""---
DRY_RUN: true
judge: {judge}
judge_display_name: {judge} (dry-run placeholder)
judge_category: {category}
brand_slug: {cfg.brand_slug}
case_id: {case_id}
version: 1
reviewed_at: '{_now_iso()}'
scores:
{scores_block}
confidence: 0.5
{adversarial_block}wiki_entities_referenced: []
---

# Review · {judge} (dry-run placeholder)

> 本文件为 --dry-run 模式占位, 未调 LLM。真跑会由 {cfg.model_deep}
> 基于 SKILL.md + synthesis 产 review。

## 一句话
dry-run placeholder — 真跑时此处是 {judge} 视角的人格化金句。

## 关键缺口
dry-run placeholder

{'## 行动建议' + chr(10) + 'dry-run placeholder' if category == 'dimension' else ''}
"""


def _failed_review_placeholder(cfg: PipelineConfig, case_id: str, judge: str, err: str) -> str:
    """Phase 4 单评委失败的占位 · smoke_e2e.verify 仍数到, 标 status=failed"""
    category = "anchor" if judge in ANCHOR_JUDGES else "dimension"
    safe_err = err.replace("\n", " ")[:200]
    scores_block = _placeholder_scores_block(fraction=0.0, is_anchor=(category == "anchor"))
    # 失败时仍写 adversarial_view 三字段 (空内容但 key 在), 让 verify 不挂;
    # 但用显眼字符串 + status=failed 让 Phase 5 / 人工评审能识别。
    if category == "dimension":
        adversarial_block = """adversarial_view:
  if_thesis_wrong: '[FAILED · sub-agent 未跑通]'
  contrary_signal_observed: '[FAILED · sub-agent 未跑通]'
  base_rate_warning: '[FAILED · sub-agent 未跑通]'
"""
    else:
        adversarial_block = ""

    return f"""---
judge: {judge}
judge_display_name: {judge} (FAILED)
judge_category: {category}
brand_slug: {cfg.brand_slug}
case_id: {case_id}
version: 1
reviewed_at: '{_now_iso()}'
status: failed
error: {_yaml_quote_inline(safe_err)}
scores:
{scores_block}
confidence: 0.0
{adversarial_block}wiki_entities_referenced: []
---

# Review · {judge} (FAILED)

> ⚠️ 本评委 sub-agent 失败, 写占位。Phase 5 合议应跳过此评委或标 [missing]。
> falsifiability 镜头给 1 分, 其他镜头同样, 让 anchor_delta 信号偏向 "缺数据"。

错误: {safe_err}
"""


# ──────────────────────────────────────────────────────────────────────
# Phase 5 — Lead 合议 (panel_summary + report.md + versions + case.json + log)
# ──────────────────────────────────────────────────────────────────────

# ANCHOR_DELTA_THRESHOLD / META_TOPIC_TYPES_FOR_DUAL_SCALE / ANCHOR_DUAL_SCALE_DELTA_THRESHOLD
# 已随打分聚合下沉 boss_core/scoring.py (M0.3), 文件顶部 re-export。

# _PHASE_5_SYSTEM_PROMPT_WEIGHTED / _SUM_MAX 两个 prompt 常量 + _phase_5_system_prompt
# 逻辑已下沉 boss_core/prompts.py (M0.2)。薄 wrapper 注入当前 SCORING_SPEC / ANCHOR_JUDGES。

def _phase_5_system_prompt(panel_judges: Optional[list[str]] = None) -> str:
    return _prompts._phase_5_system_prompt(SCORING_SPEC, ANCHOR_JUDGES, panel_judges)


def phase_5_merge(
    cfg: PipelineConfig,
    case_id: str,
    panel_judges: list[str],
    synthesis_md: str,
    context_md: str,
    log: Logger,
    skip_write_dry_run: bool = False,  # P1 #5: EVOLUTION + dry-run 保护
) -> dict[str, Any]:
    """
    Lead 合议: 算 panel_summary, 写 report.md (滚动覆盖) + versions/v{n}_<date>.md (不可变)
    + panel.yaml (若不存在) + case.json (12 字段 schema 合规) + _wiki/log.md 追加。

    Returns:
        {report_path, version_path, case_json_path, panel_yaml_path, version,
         panel_summary, tokens_in, tokens_out, cost_usd}
    """
    log.step("Phase 5", f"Lead 合议 · panel={panel_judges}")

    brand_dir = REPORTS_DIR / cfg.brand_slug
    reviews_dir = brand_dir / "reviews"
    versions_dir = brand_dir / "versions"
    if not skip_write_dry_run:
        versions_dir.mkdir(parents=True, exist_ok=True)

    # P2.4: 读 prd.md / case.json 的 topic_type, 传给 _compute_panel_summary
    # 元层议题 (meta_framework / cross_domain) 触发 anchor confidence dual scale 渲染
    topic_type = _load_v_prev_topic_type(case_id, log) or DEFAULT_TOPIC_TYPE
    panel_summary = _compute_panel_summary(reviews_dir, panel_judges, log, topic_type=topic_type)
    version_n = _allocate_next_version(versions_dir)
    if panel_summary.get("scoring_mode") == "sum_max_score":
        anchor_str = (f" · anchor={panel_summary['anchor_total']:.1f}"
                      if panel_summary.get("anchor_total") is not None else "")
        grade_str = f" · 等级={panel_summary['grade']}" if panel_summary.get("grade") else ""
        log.step("Phase 5", f"version=v{version_n} · "
                 f"维度总分={panel_summary['dimension_total_mean']:.1f}/{panel_summary['total_max']}"
                 f"{anchor_str}{grade_str}")
    else:
        extra = ""
        if "anchor_dual_scale_delta" in panel_summary:
            extra = (f" · meta={panel_summary['anchor_tian_meta_mean']:.2f}"
                     f" sp={panel_summary['anchor_tian_single_point_mean']:.2f}"
                     f" Δ_dual={panel_summary['anchor_dual_scale_delta']:+.2f}")
        log.step("Phase 5", f"version=v{version_n} · "
                 f"dim_mean={panel_summary['dimension_weighted_mean']:.2f} "
                 f"anchor={panel_summary['anchor_tian_mean']:.2f} "
                 f"Δ={panel_summary['anchor_delta']:+.2f} "
                 f"{'⚠ HIGH' if panel_summary['delta_high'] else 'OK'}"
                 f"{extra}")

    # LLM call (跳过 dry-run)
    if OUTPUT_FORMAT == "study_weekly_v8":
        # study-weekly 轻量化: report.md 只作审计留痕, 交付物是 report-v8.md 六段式;
        # 跳过 Opus 合议 prose (省一次 deep call, 打分全在 Phase 4 不受影响)。
        log.info("study_weekly_v8 轻量化: Phase 5 跳过 Opus 合议 prose (交付物 = report-v8.md)")
        body_prose = _study_weekly_lightweight_body(panel_summary)
        tokens_in = tokens_out = 0
        cost = 0.0
    elif cfg.dry_run:
        log.info("DRY-RUN Phase 5: 用占位 prose, 不调 LLM")
        body_prose = _dry_run_phase_5_body_prose(panel_judges, panel_summary)
        tokens_in = 30000
        tokens_out = 8000
        cost = 1.05
    else:
        # 合议: 剥代码围栏 + 退化乱码检测重跑 (模型在长合成上偶发吐 token 汤)
        llm, body_prose = _phase_5_body_with_retry(
            cfg, panel_judges, synthesis_md, context_md, reviews_dir, log)
        tokens_in = llm["tokens_in"]
        tokens_out = llm["tokens_out"]
        cost = llm["cost_usd"]

    # P1 #5 修: EVOLUTION + dry-run 跳所有 rolling 写盘
    report_path = brand_dir / "report.md"
    version_path = versions_dir / f"v{version_n}_{_today()}.md"
    panel_yaml_path = brand_dir / "panel.yaml"
    case_json_path = CASES_DIR / case_id / "case.json"

    # REVIEW mode (ADR-007): 准备 review_mode_data 给 _assemble_report_md 用三段式 §A/§B/§C
    review_mode_data = None
    if cfg.review_doc_path:
        # 从 cases/<id>/_parsed_doc.json 读 parsed_doc
        parsed_doc_path = CASES_DIR / case_id / "_parsed_doc.json"
        if parsed_doc_path.exists():
            import json as _json
            parsed_doc_data = _json.loads(parsed_doc_path.read_text(encoding='utf-8'))
            # 聚合 revision_suggestions (inv-3 §C)
            revision_block = _compile_revision_suggestions(reviews_dir, panel_judges, log,
                                                           panel_name=cfg.panel)
            review_mode_data = {
                'doc_title': parsed_doc_data.get('doc_title', '<unknown>'),
                'doc_path': cfg.review_doc_path,
                'doc_summary': parsed_doc_data.get('doc_summary', ''),
                'claims': parsed_doc_data.get('claims', []),
                'decisions': parsed_doc_data.get('decisions', []),
                'revision_suggestions_block': revision_block,
            }
            # 竞赛场景 (sum_max + 无 anchor): 写 build_workshop_ranking 的输入契约
            # (scene_slug / project_name / team + competition_summary)。
            if _is_competition_scoring():
                review_mode_data['competition_meta'] = {
                    'scene_slug': _scene_slug_from_panel(cfg.panel),
                    'project_name': (_clean_optional_str(parsed_doc_data.get('project_name'))
                                     or _clean_optional_str(parsed_doc_data.get('doc_title'))),
                    'team': _clean_optional_str(parsed_doc_data.get('team')),
                    'competition_summary': {
                        'total_score': panel_summary.get('dimension_total_mean'),
                        'dimension_scores': panel_summary.get('lens_means', {}),
                    },
                }
            # 也写独立 revision-suggestions.md 文件
            if not skip_write_dry_run:
                rs_path = brand_dir / "revision-suggestions.md"
                rs_path.write_text(
                    f"# 修订建议清单 · {cfg.brand_slug} · v{version_n}\n\n"
                    f"> 5 评委 REVIEW 后 (ADR-007 §C) · 共识 (≥3 评委同意) / 分歧 / 致命脆弱\n\n"
                    f"{revision_block}\n",
                    encoding='utf-8'
                )
                log.info(f"  · 写 {rs_path.relative_to(VAULT_ROOT)}")
                # 战略 OS M0 (prd-strategy-os §5.1): 建议结构化落 suggestions.json,
                # 给回路 (M1 决策捕获 / M2 台账) 提供带 ID 的锚。fail-open — 绝不阻断评审。
                try:
                    from boss_core import loop as _loop
                    _review_texts = {
                        j: (reviews_dir / f"{j}.md").read_text(encoding="utf-8")
                        for j in panel_judges if (reviews_dir / f"{j}.md").exists()}
                    _sug = _loop.extract_suggestions(
                        _review_texts, brand=cfg.brand_slug,
                        scene=_scene_slug_from_panel(cfg.panel) or "",
                        version=version_n, anchor_judges=ANCHOR_JUDGES)
                    _loop.write_suggestions(brand_dir, _sug)
                    log.info(f"  · 写 suggestions.json (战略 OS 回路 · {len(_sug['suggestions'])} 条)")
                except Exception as _sug_e:  # noqa: BLE001
                    log.warning(f"  · suggestions.json 写入失败 (fail-open): {_sug_e}")

    if skip_write_dry_run:
        log.info(f"  · EVOLUTION+dry-run 保护: skip write report.md / versions/v{version_n} / panel.yaml / case.json / _wiki/log / report-brief")
    else:
        if version_path.exists():
            # 同一天重跑或并发撞版本 — 在写任何 rolling 文件前拒绝,
            # 防止 report.md 与 frozen snapshot 版本不一致。
            raise PipelineError(f"versions/{version_path.name} 已存在, 拒绝覆盖 (设计 §7 不可变)")

        # 报告生成元数据 (调用的模型 + 端点), 写进 frontmatter 便于跨报告质量比较
        generation_meta = {
            "provider": cfg.llm_provider,
            "model_fast": cfg.model_fast,
            "model_deep": cfg.model_deep,
            "base_url": cfg.llm_base_url,
            # profile 提速: 各 phase LLM 墙钟秒进 frontmatter (worker 捕获子进程日志 → journald 看不到, 持久化到报告可见)
            "phase_llm_seconds": {k: round(v, 1) for k, v in sorted(_PHASE_LLM_SECONDS.items())},
        }
        # R8: 本 job 若发生过跨端点 failover, 打标 + 记实际用过的 model (混模型打分归因)
        try:
            import llm_failover
            _fo = llm_failover.session_summary()
            if _fo.get("failover"):
                generation_meta["failover"] = True
                generation_meta["models_used"] = _fo.get("models_used") or []
        except Exception:  # noqa: BLE001
            pass
        # 先组装所有关键内容, 再落盘。这样组装失败不会污染 rolling report。
        report_md_text = report_builder.assemble_report_md(
            cfg.brand_slug, cfg.topic, cfg.panel, case_id, panel_judges, version_n,
            panel_summary, body_prose, review_mode_data=review_mode_data,
            generation=generation_meta,
        )
        version_md_text = report_builder.assemble_version_snapshot(
            cfg.brand_slug, cfg.topic, case_id, version_n, panel_summary, body_prose,
            generation=generation_meta,
        )
        panel_yaml_text = _assemble_panel_yaml(cfg, case_id, panel_judges)
        # attribution checkpoint: 用 LLM 基于判断正文起草具体可证伪信号 (draft, 待审), 失败回退占位。
        # study_weekly_v8 例外: 一份周报没有 30/90/365 证伪点, 跳过 deep 起草直接用占位 (省一次 deep call)。
        cp_dates = _attribution_checkpoint_dates()
        drafted_cps = (None if OUTPUT_FORMAT == "study_weekly_v8"
                       else _draft_checkpoints(cfg, cp_dates, body_prose or "", log))
        # EVOLUTION 复用同一 case_id → case.json 已在盘 (FRESH/REVIEW 用全新 case_id, 不存在)。
        # 存在即合并: 保留 attribution 归因结果 / variables / decision / trigger_event 等
        # 手工与 daemon 累积字段, 只重写身份与 panel/version 元数据 (修早先整体覆盖丢归因)。
        existing_case = None
        if case_json_path.exists():
            try:
                existing_case = json.loads(case_json_path.read_text(encoding="utf-8"))
                log.step("Phase 5", "  · EVOLUTION merge: 保留既有 attribution/variables/decision/trigger_event")
            except (json.JSONDecodeError, OSError) as e:
                log.warn(f"  ⚠ 既有 case.json 解析失败, 回退全量重建 (可能丢归因): {e}")
                existing_case = None
        case_data = _build_case_json(cfg, case_id, panel_judges, context_md,
                                     checkpoints=drafted_cps, checkpoint_dates=cp_dates,
                                     existing_case=existing_case)

        # v0.9 C2 (Step 1.5 第 7 点 M2): framework lock 存在时, report + 冻结快照追加 §C
        # Framework vs Actual 段; 无 lock 零侵入 (render 返回 None)
        try:
            import framework_compare
            sec_c = framework_compare.render_section_c_for_case(
                case_id, CASES_DIR, REPORTS_DIR, case=case_data)
            if sec_c:
                report_md_text += sec_c
                version_md_text += sec_c
                log.step("Phase 5", "  · §C Framework vs Actual 已渲染 (盲测议题)")
        except Exception as e:
            log.warn(f"  ⚠ §C framework 渲染失败 (非致命): {e}")

        # 交付前来源脱敏: 评委按 anti-fabrication 引用了内部路径 / 原始素材名 / synthesis 引用 /
        # 锚点真名, 这些不进交付报告 (report.md / 富 HTML / PDF); 原始 reviews/*.md 保留供审计。
        # best-effort: 脱敏异常不阻断投递 (原文落盘)。
        try:
            import desensitize
            report_md_text = desensitize.desensitize_sources(report_md_text)
            version_md_text = desensitize.desensitize_sources(version_md_text)
        except Exception as e:
            log.warn(f"  ⚠ 报告来源脱敏失败 (非致命, 原文投递): {e}")

        # 写 report.md (滚动覆盖) · REVIEW 模式走三段式
        report_path.write_text(report_md_text, encoding="utf-8")

        # 写 versions/v{n}_<date>.md (不可变) · chmod 444 与 Skill 层契约对齐 (R3, v0.6)
        version_path.write_text(version_md_text, encoding="utf-8")
        os.chmod(version_path, 0o444)

        # 写 panel.yaml (若不存在或重跑覆盖 OK)
        panel_yaml_path.write_text(panel_yaml_text, encoding="utf-8")

        # 写 case.json (12 字段 schema 合规)
        case_dir = CASES_DIR / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        case_json_path.write_text(json.dumps(case_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # L1 · 写 report-brief.md (1-page 摘要层 · 2026-05-30 用户反馈"过于复杂"对策)
        # 全用已结构化字段 (panel_summary / reviews frontmatter / case.json checkpoints) 拼,
        # 不调 LLM, 无 token 成本, 100% 可靠.
        brief_path = brand_dir / "report-brief.md"
        try:
            brief_text = report_builder.assemble_report_brief(
                cfg.brand_slug, cfg.topic, cfg.panel, case_id, version_n,
                REPORTS_DIR, CASES_DIR,
            )
            brief_path.write_text(brief_text, encoding="utf-8")
            log.info(f"  · 写 {brief_path.relative_to(VAULT_ROOT)} (摘要层, 详情见 report.md)")
        except Exception as e:
            log.warn(f"  ⚠ report-brief 写失败 (非致命, report.md 已写): {e}")

        # 追加 _wiki/log.md (CLAUDE.md §2.2 可追加不可改)
        _append_wiki_log(cfg.brand_slug, version_n, log)

    log.step("Phase 5", f"✓ report.md (v{version_n}) + case.json + log 追加" if not skip_write_dry_run else f"✓ Phase 5 logic 完成 (无写盘, EVOLUTION 保护)")

    return {
        "report_path": str(report_path.relative_to(VAULT_ROOT)),
        "version_path": str(version_path.relative_to(VAULT_ROOT)),
        "case_json_path": str(case_json_path.relative_to(VAULT_ROOT)),
        "panel_yaml_path": str(panel_yaml_path.relative_to(VAULT_ROOT)),
        "version": version_n,
        "panel_summary": panel_summary,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost,
    }


# "修订建议" 标题宽容匹配: 行首 (≤3 空格) + (1-4 个 # 或 **) + ≤10 个非换行字 + "修订建议"。
# 覆盖 `## 修订建议 (REVIEW)` / `### 修订建议` / `**修订建议**` / `## 我的修订建议` / `## ★修订建议`。
_REVISION_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}(?:#{1,4}[ \t]*|\*\*[ \t]*)[^\n#*]{0,10}修订建议")
# 回退: 部分 Phase 2 人格评委 (梁胜/邹叙 等) SKILL 格式用 `## 行动建议` / `## 改进建议` 而非
# "修订建议", 内容同样是对方案的可落地改法 (语义等价)。主匹配失败时回退到它, 避免 §C 误标"未提供"。
_ACTION_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}(?:#{1,4}[ \t]*|\*\*[ \t]*)[^\n#*]{0,10}(?:行动建议|改进建议)")
# 下一个 markdown 标题 (截断 body 用)
_NEXT_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,4}[ \t]")


def _compile_revision_suggestions(reviews_dir: Path, panel_judges: list[str], log: Logger,
                                  panel_name: str | None = None) -> str:
    """REVIEW (ADR-007 §C): 聚合 5 评委 reviews 里的修订建议.

    每个 review body 末尾应有 `## 修订建议 (REVIEW)` 段, 含 3 条 bullet.
    本函数:
    - 提取每个评委的修订建议 list
    - 聚合: 共识 (≥3 评委 同意) / 分歧 / 致命脆弱 (anchor 标的)
    - 输出 markdown block

    简化版 v1: 不做语义聚类, 只按 judge 罗列 + 标 anchor 视角.
    后续 V1.1 可加 LLM-based clustering 找共识.
    """
    blocks = []
    blocks.append("### 各评委修订建议\n")

    for judge in panel_judges:
        review_path = reviews_dir / f"{judge}.md"
        if not review_path.exists():
            continue
        text = review_path.read_text(encoding='utf-8')
        # 宽容匹配"修订建议"标题 (容忍 # 数量/粗体/前后缀), 主匹配失败回退到"行动建议/改进建议"
        # —— 精确 "## 修订建议" 曾漏掉标题变体 + 人格评委的 "## 行动建议" (2026-07-01: 梁胜/邹叙 §C 空)
        m = _REVISION_HEADING_RE.search(text) or _ACTION_HEADING_RE.search(text)
        label = _judge_label(judge, panel_name)
        if not m:
            blocks.append(f"#### {label}\n\n_(未提供修订建议段)_\n")
            continue
        # body = 标题行之后 → 下一个 markdown 标题 (或文末)
        nl = text.find("\n", m.start())
        rest = text[nl + 1:] if nl >= 0 else ""
        nxt = _NEXT_HEADING_RE.search(rest)
        body = (rest[:nxt.start()] if nxt else rest).strip()
        body = _strip_orphan_fences(body)   # 评委偶把整段 review 包进 ```markdown, 剥残留围栏

        is_anchor = judge in ANCHOR_JUDGES
        marker = " ⭐ (anchor 视角)" if is_anchor else ""
        blocks.append(f"#### {label}{marker}\n\n{body}\n")

    blocks.append("\n### 聚合提示\n")
    blocks.append("> v1 简化: 仅按 judge 罗列. V1.1 可加 LLM-based 共识识别 (≥3 评委同意 → 共识 / anchor 标 → 致命脆弱).\n")

    return "\n".join(blocks)


def _grade_for_total(total_mean: float) -> Optional[str]:
    # 逻辑已下沉 boss_core/scoring.py (M0.3); 薄 wrapper 注入当前 SCORING_SPEC。
    return _scoring._grade_for_total(SCORING_SPEC, total_mean)


def _compute_panel_summary_sum_max(reviews_dir: Path, panel_judges: list[str],
                                   log: Logger) -> dict[str, Any]:
    # 逻辑已下沉 boss_core/scoring.py (M0.3); 薄 wrapper 注入当前 SCORING_SPEC /
    # SCORING_LENSES / ANCHOR_JUDGES + _parse_review_frontmatter 回调。
    return _scoring._compute_panel_summary_sum_max(
        reviews_dir, panel_judges, log,
        scoring_spec=SCORING_SPEC, scoring_lenses=SCORING_LENSES,
        anchor_judges=ANCHOR_JUDGES, parse_fm=_parse_review_frontmatter)


def _compute_panel_summary(reviews_dir: Path, panel_judges: list[str], log: Logger,
                            topic_type: str = "unknown") -> dict[str, Any]:
    # 逻辑已下沉 boss_core/scoring.py (M0.3); 薄 wrapper 注入当前 SCORING_SPEC /
    # SCORING_LENSES / ANCHOR_JUDGES + _parse_review_frontmatter 回调 (§6 R-a)。
    return _scoring._compute_panel_summary(
        reviews_dir, panel_judges, log, topic_type,
        scoring_spec=SCORING_SPEC, scoring_lenses=SCORING_LENSES,
        anchor_judges=ANCHOR_JUDGES, parse_fm=_parse_review_frontmatter)


# _parse_review_frontmatter / _parse_review_frontmatter_fallback 已下沉
# boss_core/reviews.py (M2.0b), 文件顶部 re-export。


def _allocate_next_version(versions_dir: Path) -> int:
    """扫 versions/v*.md, 找最大 n 返回 n+1. 没有则 1."""
    if not versions_dir.exists():
        return 1
    nums = []
    for p in versions_dir.glob("v*.md"):
        m = re.match(r"^v(\d+)_\d{4}-\d{2}-\d{2}\.md$", p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


# _format_panel_summary_dual_scale_yaml 已下沉 boss_core/scoring.py (M0.3, 纯函数),
# 文件顶部 re-export。


def _assemble_report_md(cfg: PipelineConfig, case_id: str, panel_judges: list[str],
                        version: int, panel_summary: dict, body_prose: str,
                        review_mode_data: Optional[dict] = None) -> str:
    """report.md.

    FRESH/EVOLUTION: frontmatter + Phase 1 Context + Body + reviews link
    REVIEW (ADR-007 inv-3): 三段式 §A 摘要 + §B 评委 review + §C 修订建议
    """
    judges_yaml = "\n".join(f"  - {j}" for j in panel_judges)
    # 评委明细: 保留 slug wiki 链接 (机器可寻址) + 附中英文显示名 (人可读)
    review_links = "\n".join(
        f"- [[reviews/{j}]] · {_judge_label(j, cfg.panel)}" if _judge_label(j, cfg.panel) != j
        else f"- [[reviews/{j}]]"
        for j in panel_judges
    )
    panel_labels = ", ".join(_judge_label(j, cfg.panel) for j in panel_judges)
    dual_scale_yaml = _format_panel_summary_dual_scale_yaml(panel_summary)

    # REVIEW mode (ADR-007): 三段式 §A/§B/§C
    if review_mode_data:
        doc_title = review_mode_data.get('doc_title', '<unknown>')
        doc_path = review_mode_data.get('doc_path', '<unknown>')
        doc_summary = review_mode_data.get('doc_summary', '')
        claims = review_mode_data.get('claims', [])
        decisions = review_mode_data.get('decisions', [])
        revision_suggestions_block = review_mode_data.get('revision_suggestions_block', '(待 _compile_revision_suggestions 填充)')
        claims_md = "\n".join([f"- {c}" for c in claims])
        decisions_md = "\n".join([
            f"- **action**: {d.get('action', 'TBD')}" + (f" · owner: {d['owner']}" if d.get('owner') else "") + (f" · deadline: {d['deadline']}" if d.get('deadline') else "")
            for d in decisions
        ])
        return f"""---
brand_slug: {cfg.brand_slug}
case_id: {case_id}
version: {version}
mode: REVIEW
review_doc_path: {doc_path}
created_at: '{_now_iso()}'
panel: {cfg.panel}
judges:
{judges_yaml}
panel_summary:
  dimension_weighted_mean: {panel_summary['dimension_weighted_mean']}
  anchor_tian_mean: {panel_summary['anchor_tian_mean']}
  anchor_delta: {panel_summary['anchor_delta']}
  delta_high: {str(panel_summary['delta_high']).lower()}
  delta_explanation: {_yaml_quote_inline(panel_summary['delta_explanation'])}{dual_scale_yaml}
sensitivity: confidential
---

# REVIEW · {doc_title} · v{version}

> ADR-007 REVIEW mode · 评议方案 doc, 不是研究 open question
> 被评议: `{doc_path}` · 议题 (从 doc 推): {cfg.topic}

---

## §A · 原方案摘要 (Phase 0 doc parser)

{doc_summary}

### 核心 claims (从 doc 提)
{claims_md}

### Decisions (doc 提议)
{decisions_md}

---

## §B · 5 评委评议

> panel = {panel_labels}
> dim_weighted_mean = {panel_summary['dimension_weighted_mean']} · anchor = {panel_summary['anchor_tian_mean']} · anchor_delta = {panel_summary['anchor_delta']}

{body_prose}

### 评委明细
{review_links}

---

## §C · 修订建议清单

> ADR-007 REVIEW 独有 deliverable · 5 评委 "如果是我会改的 3 点" 聚合

{revision_suggestions_block}

---

## 30/90/365 attribution

> 跟踪 §C 修订建议是否落实 (REVIEW 推荐 vs 实际执行差距)
> 详见 `cases/{case_id}/case.json` attribution 段
"""

    # FRESH/EVOLUTION 标准模板 (不变)
    return f"""---
brand_slug: {cfg.brand_slug}
case_id: {case_id}
version: {version}
created_at: '{_now_iso()}'
panel: {cfg.panel}
judges:
{judges_yaml}
panel_summary:
  dimension_weighted_mean: {panel_summary['dimension_weighted_mean']}
  anchor_tian_mean: {panel_summary['anchor_tian_mean']}
  anchor_delta: {panel_summary['anchor_delta']}
  delta_high: {str(panel_summary['delta_high']).lower()}
  delta_explanation: {_yaml_quote_inline(panel_summary['delta_explanation'])}{dual_scale_yaml}
sensitivity: confidential
---

# Report · {cfg.brand_slug} · v{version}

## Phase 1 — Context

议题: {cfg.topic or '(EVOLUTION 重判, 见上版)'}

### Background from Wiki

> CLAUDE.md §3.1 单向引用. 链接为快照引用, Wiki 内容由 sage-wiki 自动更新。
> 本判断书冻结于触发日, 如 Wiki 后续修订, 以版本快照为准。

- 公司档案: [[_wiki/entities/{cfg.brand_slug}]]
- 关键人物: [[_wiki/people/anchor]] (待 sage-wiki 编译真 entity 名)

{body_prose}

## 评委 reviews

{review_links}
"""


def _assemble_version_snapshot(cfg: PipelineConfig, case_id: str, version: int,
                               panel_summary: dict, body_prose: str) -> str:
    """冻结快照. 与 report.md 体一致, 但 frontmatter 强调 immutable."""
    dual_scale_yaml = _format_panel_summary_dual_scale_yaml(panel_summary)
    # Body 段渲染 dual_scale (镜像 frontmatter, 给人类读)
    dual_scale_body = ""
    if "anchor_dual_scale_delta" in panel_summary:
        dual_scale_body = (
            f"\n  anchor_tian_meta_mean: {panel_summary['anchor_tian_meta_mean']}"
            f"\n  anchor_tian_single_point_mean: {panel_summary['anchor_tian_single_point_mean']}"
            f"\n  anchor_dual_scale_delta: {panel_summary['anchor_dual_scale_delta']}"
        )
    return f"""---
brand_slug: {cfg.brand_slug}
case_id: {case_id}
version: {version}
frozen_at: '{_now_iso()}'
immutable: true
panel_summary:
  dimension_weighted_mean: {panel_summary['dimension_weighted_mean']}
  anchor_tian_mean: {panel_summary['anchor_tian_mean']}
  anchor_delta: {panel_summary['anchor_delta']}{dual_scale_yaml}
---

# Frozen Snapshot · {cfg.brand_slug} · v{version}

⚠️ 此快照永久不可变 (CLAUDE.md §2.2). EVOLUTION 模式应写 v{version + 1}, 不修改本版。

议题: {cfg.topic or '(EVOLUTION 重判)'}

panel_summary:
  dimension_weighted_mean: {panel_summary['dimension_weighted_mean']}
  anchor_tian_mean: {panel_summary['anchor_tian_mean']}
  anchor_delta: {panel_summary['anchor_delta']}
  delta_high: {panel_summary['delta_high']}{dual_scale_body}

{body_prose}
"""


def _assemble_report_brief(cfg: PipelineConfig, case_id: str, panel_judges: list[str],
                           version: int, panel_summary: dict, reviews_dir: Path,
                           log: Logger) -> str:
    """v3 摘要层 (2026-05-30 用户反馈 #2: "太多无关信息, 不需要太多评分或过程,
    直接用逻辑严谨的中文说明结论, 以及推论过程和论据").

    v3 砍掉 dashboard (scores 表 / panel_summary / dual_scale / adversarial 列表),
    只留 5 段 prose:
      1. 议题
      2. 结论 (grep ### 结论 / ### 主 Decision)
      3. 推论过程 (grep ### 共识 / ### 6 dim 收敛点 + 一段 prose 引到反方)
      4. 论据 (grep ### 引用 evidence / ### 6 评委核心金句)
      5. 反方与脆弱 (grep ### 矛盾 / ### 锚点视角的反方启示 简化首段)
      6. 30/90/365 验证 (一句话 each)

    panel_summary / 评委 scores 全留在 report.md, brief 完全不出数字.
    panel_summary 参数保留兼容 phase_5_merge 调用, 内部不消费.
    """
    _ = panel_summary  # v3: brief 不出 panel_summary 数字, 仅保留参数兼容

    report_path = reviews_dir.parent / "report.md"

    # ─── 5 段 grep 提取 ───
    conclusion = _extract_conclusion_prose(report_path)
    reasoning = _extract_reasoning_prose(report_path)
    evidence = _extract_evidence_prose(report_path)
    counter = _extract_counter_prose(report_path)
    attribution = _extract_attribution_prose(case_id, log, report_path)

    return f"""---
brand_slug: {cfg.brand_slug}
case_id: {case_id}
version: {version}
created_at: '{_now_iso()}'
brief_of: reports/{cfg.brand_slug}/report.md
panel: {cfg.panel}
sensitivity: confidential
---

# 判断书摘要 · {cfg.brand_slug} · v{version}

> 议题 + 结论 + 推论过程 + 论据 + 反方 + 验证锚点. 评分 / scores / 合议数字在 [report.md](report.md).

## 议题

{cfg.topic or '(EVOLUTION 重判 · 见上版)'}

## 结论

{conclusion}

## 推论过程

{reasoning}

## 论据

{evidence}

## 反方与脆弱

{counter}

## 30/90/365 验证锚点

{attribution}

---

完整推理 / 评委 reviews / 数字 dashboard 见 [report.md](report.md) ({_estimate_lines(report_path)} 行).
"""


# ──────────────────────────────────────────────────────────────────────────
# v3 brief section extractors · 直出 prose, 不出数字 / 评分
# ──────────────────────────────────────────────────────────────────────────

# brief heading 常量 + _strip_brief_noise / _extract_{conclusion,reasoning,evidence,counter}_prose
# 已下沉 boss_core/constants.py + boss_core/prose.py (M0.1c), 文件顶部 re-export。
# _extract_section_body 已下沉 boss_core/docio.py (M0.1b)。
# _extract_attribution_prose 仍留此处 (需 CASES_DIR / Logger / json), 用 re-export 的
# _ATTRIBUTION_HEADINGS / _strip_brief_noise / _extract_section_body。


def _extract_attribution_prose(case_id: str, log: Logger, report_path: Optional[Path] = None) -> str:
    """提 30/90/365 验证锚点. 优先 case.json checkpoints (结构化), 否则 fallback
    grep report.md `### Attribution Checkpoints` 段 (prior 5 案场景)."""
    case_json_path = CASES_DIR / case_id / "case.json"
    if case_json_path.exists():
        try:
            cdata = json.loads(case_json_path.read_text(encoding="utf-8"))
            cps = ((cdata.get("attribution") or {}).get("checkpoints") or [])
            lines: list[str] = []
            for c in cps:
                if not isinstance(c, dict):
                    continue
                h = c.get("horizon_days", "?")
                when = c.get("check_at", "?")
                metric = (c.get("falsification_metric") or "").strip()
                metric_short = metric.split("\n")[0][:200]
                if len(metric) > 200:
                    metric_short += "…"
                lines.append(f"- **{h} 天 ({when})**: {metric_short}")
            if lines:
                return "\n".join(lines)
        except (json.JSONDecodeError, OSError) as e:
            log.warn(f"report-brief: case.json 读失败 {e}")
    # Fallback: grep report.md attribution heading
    if report_path is not None and report_path.exists():
        body = _extract_section_body(report_path, _ATTRIBUTION_HEADINGS)
        if body:
            cleaned = _strip_brief_noise(body)
            if len(cleaned) > 1200:
                cleaned = cleaned[:1200] + "…"
            return cleaned
    return "_本案 case.json 与 report.md 均未含 attribution checkpoint, 详见 [report.md](report.md)._"


def _estimate_lines(p: Path) -> str:
    """文件行数 (估计用, 失败返回 '?')."""
    try:
        return str(len(p.read_text(encoding="utf-8").splitlines()))
    except (OSError, UnicodeDecodeError):
        return "?"


def _assemble_panel_yaml(cfg: PipelineConfig, case_id: str, panel_judges: list[str]) -> str:
    """brand 绑定的 panel snapshot · 比 panels/default.yaml 精简, 仅列本次评委"""
    judges_yaml = "\n".join(f"  - {j}" for j in panel_judges)
    return f"""# panel.yaml · {cfg.brand_slug} (run_pipeline_local auto-write)
name: {cfg.panel}
display_name: brand-bound panel snapshot
brand_slug: {cfg.brand_slug}
case_id: {case_id}
judges:
{judges_yaml}
auto_selected_by: run_pipeline_local@phase_5
"""


def _attribution_checkpoint_dates() -> dict[int, str]:
    """30/90/365 到期日 (东八区当天起算)。占位与 LLM 起草共用一处, 避免漂移。"""
    today_date = datetime.now(timezone(timedelta(hours=8))).date()
    return {h: (today_date + timedelta(days=h)).isoformat() for h in (30, 90, 365)}


_CHECKPOINT_DRAFT_SYSTEM = (
    "你是判断力工程化流水线的 attribution 设计助手。给你一份判断/评审正文, 为 30/90/365 天"
    "各起草 1 条**具体、可被另一个人独立观测**的证伪点。\n"
    "纪律 (anti-fabrication): 只能基于正文已明确提到的论点/预测/事件/主体来设计信号, **不得编造**"
    "正文未提及的事实、数字或主体。信号要让另一个人在到期日能机械判定 confirmed/partial/falsified。\n"
    "输出**纯 JSON** (无解释、无围栏): "
    '{"checkpoints":[{"horizon_days":30,"falsification_metric":"<可观测信号>","expected_signal":"<预期具体读数>"},'
    '{"horizon_days":90,...},{"horizon_days":365,...}]}'
)


def _draft_checkpoints(cfg: PipelineConfig, checkpoint_dates: dict[int, str],
                       context_text: str, log: Logger) -> Optional[list[dict]]:
    """LLM 起草 30/90/365 具体可证伪 checkpoint, 替代空占位 (draft=true 标记待项目主理审)。
    任何失败 (dry-run / 上下文太短 / LLM 错 / 解析错 / 不完整 / 仍含'占位') → None, 调用方回退占位。
    case.json 是 confidential 内部数据 (非出站), 故不过 redact 闸 (出站由 feishu_notify 把关)。"""
    if cfg.dry_run or not context_text or len(context_text.strip()) < 200:
        return None
    try:
        resp = _call_llm(cfg, model=cfg.model_deep, max_tokens=1400,
                         system=_CHECKPOINT_DRAFT_SYSTEM, phase="phase_5",
                         user=f"# 判断/评审正文\n\n{context_text[:6000]}")
        data = _loads_lenient(resp.text.strip())
    except Exception as e:
        log.warn(f"  · checkpoint LLM 起草失败, 回退占位: {type(e).__name__}: {e}")
        return None
    by_h: dict[int, dict] = {}
    for c in (data.get("checkpoints") or []):
        if isinstance(c, dict):
            try:
                by_h[int(c.get("horizon_days", 0))] = c
            except (ValueError, TypeError):
                pass
    out: list[dict] = []
    for h in (30, 90, 365):
        c = by_h.get(h) or {}
        fm = str(c.get("falsification_metric") or "").strip()
        es = str(c.get("expected_signal") or "").strip()
        if not fm or not es or "占位" in fm or "占位" in es:
            return None   # 不完整/仍占位 → 整体回退 (不混合占位与起草)
        out.append({
            "horizon_days": h, "check_at": checkpoint_dates[h],
            "falsification_metric": fm, "expected_signal": es,
            "data_source": "manual", "status": "pending", "draft": True,
        })
    log.info("  · attribution checkpoint LLM 已起草 3 条 (draft, 待项目主理审收)")
    return out


def _build_case_json(cfg: PipelineConfig, case_id: str, panel_judges: list[str],
                     context_md: str, checkpoints: Optional[list[dict]] = None,
                     checkpoint_dates: Optional[dict[int, str]] = None,
                     existing_case: Optional[dict] = None) -> dict[str, Any]:
    """
    构造 12 字段 schema 合规 case.json. V0 期: 用占位值, 项目主理 在 T15 手填 真实
    trigger_event / variables / evidences / theses / decision / checkpoints。
    checkpoints 非空时用 LLM 起草版 (draft, 仍待审), 否则落空占位。

    existing_case 非空 (EVOLUTION 复用 case_id) 时**合并**而非重建: 以既有 case 为底,
    只覆盖身份与 panel/version 元数据, 保留 attribution 归因结果 / variables / decision /
    trigger_event 等手工与 daemon 累积字段 (修早先整体覆盖 → 归因结果被 placeholder 冲掉)。
    """
    checkpoint_dates = checkpoint_dates or _attribution_checkpoint_dates()
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    # ADR-007 inv-1: mode 字段 / review_doc_path
    mode = "REVIEW" if cfg.review_doc_path else ("EVOLUTION" if cfg.evolution else "FRESH")
    fresh = {
        "case_id": case_id,
        "brand_slug": cfg.brand_slug,
        "skill_used": "run_pipeline_local",
        "skill_version": "0.4-adr-007",
        "mode": mode,
        "review_doc_path": cfg.review_doc_path,
        "created_at": _now_iso(),
        "panel": cfg.panel,
        "judges": panel_judges,
        "sensitivity": "confidential",
        "context": {
            "topic": cfg.topic or "(EVOLUTION 重判)",
            "trigger_event": {
                "named_event": "[占位 · 项目主理 T15 手填具体事件名]",
                "occurred_at": _now_iso(),
                "source_url": "lark://placeholder/anchor-fills",
            },
            "time_window": {
                "deadline": "2026-12-31",
                "external_anchor": "[占位 · 外部催化窗口, 项目主理 填具名时点]",
            },
            "constraints": [
                {"type": "legal", "description": "[占位 · 法律约束]"},
                {"type": "equity", "description": "[占位 · 股权约束]"},
                {"type": "resource", "description": "[占位 · 资源约束]"},
            ],
            "background_from_wiki": [
                f"_wiki/entities/{cfg.brand_slug}.md",
            ],
        },
        "variables": [
            {
                "name": "[占位变量 A]", "category": "macro",
                "current_value": 0.5,
                "flip_threshold": {"value": 0.7, "direction": "above"},
                "weight": 0.4, "data_source": "run_pipeline_local placeholder",
            },
            {
                "name": "[占位变量 B]", "category": "internal",
                "current_value": "ok",
                "flip_threshold": {"value": "degraded", "direction": "eq"},
                "weight": 0.3, "data_source": "run_pipeline_local placeholder",
            },
            {
                "name": "[占位变量 C]", "category": "other",
                "current_value": 10,
                "flip_threshold": {"value": 5, "direction": "below"},
                "weight": 0.3, "data_source": "run_pipeline_local placeholder",
            },
        ],
        "reasoning_trace": {
            "steps": [
                "step 1 [占位]: Phase 2 各 dim sub-agent 调研",
                "step 2 [占位]: Phase 3 Lead 合成 synthesis",
                "step 3 [占位]: Phase 4 评委独立打分",
                "step 4 [占位]: Phase 5 算 anchor_delta + 合议",
            ],
            "jumps": [
                {
                    "type": "weight_jump",
                    "description": "[占位 · 跳步说明, 项目主理 在评审时标注]",
                    "source": "run_pipeline_local@phase_5",
                    "confidence": 0.5,
                }
            ],
        },
        "evidences": [
            {
                "statement": f"[占位证据 {i}]",
                "source_url": f"https://placeholder.example/{i}",
                "source_type": "document",
                "verified_at": today,
            }
            for i in (1, 2, 3)
        ],
        "theses": [
            {
                "statement": "[占位主张 · 由 Phase 3 synthesis 升级]",
                "counter_statement": "[占位反方独立命题]",
                "evidence_refs": [0, 1, 2],
            }
        ],
        "decision": {
            "actions": [
                {
                    "description": "[占位动作 · 项目主理 在 T15 手填]",
                    "owner": "[人名占位]",
                    "deadline": "2026-09-30",
                    "machine_verifiable_done": "[占位 · 90 天后另一人能机械判断的描述]",
                }
            ],
            "owner": "[占位整体决策 owner]",
            "deadline": "2026-09-30",
        },
        "attribution": {
            "checkpoints": checkpoints if checkpoints else [
                {
                    "horizon_days": 30, "check_at": checkpoint_dates[30],
                    "falsification_metric": "[占位 · 30d 信号, 项目主理 在 T15 改成可独立观测]",
                    "expected_signal": "[占位]",
                    "data_source": "manual", "status": "pending",
                },
                {
                    "horizon_days": 90, "check_at": checkpoint_dates[90],
                    "falsification_metric": "[占位 · 90d 指标]",
                    "expected_signal": "[占位]",
                    "data_source": "manual", "status": "pending",
                },
                {
                    "horizon_days": 365, "check_at": checkpoint_dates[365],
                    "falsification_metric": "[占位 · 1y 指标]",
                    "expected_signal": "[占位]",
                    "data_source": "manual", "status": "pending",
                },
            ]
        },
    }

    if existing_case is None:
        return fresh  # FRESH / REVIEW: 全新 case_id, 无既有内容, 保持原行为

    # EVOLUTION 合并: 以既有 case 为底 (保留 context/variables/reasoning_trace/evidences/
    # theses/decision/attribution.checkpoints 的归因结果 + failure_cards + parent_case_id 等),
    # 只覆盖本次重新生成的身份与 panel/version 元数据。
    merged = dict(existing_case)
    merged.update({
        "judges": panel_judges,
        "panel": cfg.panel,
        "skill_used": fresh["skill_used"],
        "skill_version": fresh["skill_version"],
        "mode": fresh["mode"],
        "review_doc_path": fresh["review_doc_path"],
        "sensitivity": existing_case.get("sensitivity", fresh["sensitivity"]),
    })
    # EVOLUTION 血统: schema 的 parent_case_id 就为此设计, 既有为空则回填上一版 case_id
    if not merged.get("parent_case_id"):
        merged["parent_case_id"] = existing_case.get("case_id")
    return merged


def _append_wiki_log(brand_slug: str, version: int, log: Logger) -> None:
    """
    追加一行到 _wiki/log.md (CLAUDE.md §2.2 可追加不可改).
    格式: ## [YYYY-MM-DD] judgement | <brand> v{n}
    若 _wiki/log.md 不存在, 创建之并加最小 header.

    BOSS_VM_READER=1 (ADR-011 §7 VM 只读消费者): 跳过 — _wiki/log.md 在主 repo
    是 tracked 文件, VM 侧改动会让 vm_pull --ff-only 在下次上游更新时永久失败。
    canonical log 由写者侧维护; VM 的任务记录在 cases/.review-jobs/done/ JSON。
    """
    if os.environ.get("BOSS_VM_READER") == "1":
        log.info("BOSS_VM_READER=1 — 跳过 _wiki/log.md 追加 (VM 只读消费者, 任务记录见 review job JSON)")
        return
    wiki_log = VAULT_ROOT / "_wiki" / "log.md"
    wiki_log.parent.mkdir(parents=True, exist_ok=True)
    line = f"\n## [{_today()}] judgement | {brand_slug} v{version}\n"
    if not wiki_log.exists():
        wiki_log.write_text(
            "# Wiki Log\n\n> sage-wiki / run_pipeline_local 共同追加的操作日志 (CLAUDE.md §2.2).\n"
            + line,
            encoding="utf-8",
        )
        log.info(f"_wiki/log.md 不存在 — 已创建并追加首条")
    else:
        with wiki_log.open("a", encoding="utf-8") as f:
            f.write(line)
        log.dbg(f"_wiki/log.md 追加: {line.strip()}")


def _strip_md_fence(text: str) -> str:
    """剥掉 LLM 偶尔把整段输出包进的最外层 ```/```markdown 代码围栏。

    仅当整段恰好被**一对**围栏包裹 (首行 ```lang + 末行 ```, 中间无其他围栏) 时剥,
    内部合法代码块 (多对围栏) 一律不动。
    """
    if not text:
        return text
    lines = text.strip().splitlines()
    fence_idx = [i for i, l in enumerate(lines) if l.strip().startswith("```")]
    if len(fence_idx) == 2 and fence_idx[0] == 0 and fence_idx[1] == len(lines) - 1:
        first = lines[0].strip()
        if first == "```" or re.fullmatch(r"```[\w-]*", first):
            return "\n".join(lines[1:-1]).strip()
    return text


def _strip_review_fence(text: str) -> str:
    """评委 review 落盘前剥最外层代码围栏。

    gpt-4o 等模型偶把整份 review (frontmatter + body) 包进 ```markdown … ```,
    导致 frontmatter 解析器找不到开头的 `---` → fail-no-frontmatter → 该评委被
    smoke_e2e.verify 判废 (多评委 panel 上 ≥2 个废即硬失败)。

    两层: ① _strip_md_fence 处理整段恰好一对围栏的常见情形; ② 兜底 — 若仍以围栏行
    开头且下一行是 frontmatter 起始 (--- 或 judge:), 单独剥这条 leading 围栏 (+ 末尾
    孤立围栏), 覆盖 body 内含合法代码块致围栏 > 2 条、_strip_md_fence 不动的情形。"""
    s = _strip_md_fence(text)
    lines = s.lstrip("\n").splitlines()
    if lines and re.fullmatch(r"```[\w-]*", lines[0].strip()):
        nxt = lines[1].strip() if len(lines) > 1 else ""
        if nxt == "---" or nxt.startswith("judge:"):
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            return "\n".join(lines).strip()
    return s


def _strip_orphan_fences(text: str) -> str:
    """剥孤立 (不成对) 的代码围栏行。

    评委偶把整段 review 包进 ```markdown; 截取 §C 修订建议段 (review 末段) 时, 整段被
    包裹的会先被 `_strip_md_fence` 处理掉, 否则只残留单边 (多为末尾) 孤立 ``` —
    围栏行总数为奇数时剥掉那一条 (优先末尾)。成对的合法代码块不动。
    """
    s = _strip_md_fence(text)
    lines = s.splitlines()
    fence_idx = [i for i, l in enumerate(lines) if l.strip().startswith("```")]
    if len(fence_idx) % 2 == 1:
        drop = fence_idx[-1] if lines[fence_idx[-1]].strip() == "```" else fence_idx[0]
        del lines[drop]
    return "\n".join(lines).strip()


def _count_glued_script(text: str) -> int:
    """统计中英文紧贴粘连 (无空格) 的次数 — Phase 5 合议退化为乱码的强信号。

    正常中文报告里英文术语用空格分隔 ('falsifiability 维度'); 模型在长合成上偶发退化时,
    letter-run 与汉字直接粘连成 token 汤 ('其他团队模块expenses status评估部门pilot')。
    """
    if not text:
        return 0
    return len(re.findall(r"[一-鿿][A-Za-z]{3,}|[A-Za-z]{3,}[一-鿿]", text))


def _looks_garbled(text: str, threshold: int = 4) -> bool:
    """Phase 5 合议输出是否疑似退化为乱码 (中英文碎片乱拼粘连超阈值)。"""
    return _count_glued_script(text) >= threshold


def _phase_5_body_with_retry(cfg: PipelineConfig, panel_judges: list[str],
                             synthesis_md: str, context_md: str,
                             reviews_dir: Path, log: Logger) -> tuple[dict, str]:
    """调 Phase 5 合议; 输出疑似退化为乱码 (与模型选型无关的偶发) 则重跑,
    取乱码信号最低的一版。最多 2 次。返回 (llm_result_dict, body_prose)。"""
    best_llm: Optional[dict] = None
    best_prose = ""
    best_garble: Optional[int] = None
    for attempt in (1, 2):
        llm = _call_anthropic_phase_5(cfg, panel_judges, synthesis_md, context_md, reviews_dir, log)
        cand = _strip_md_fence(llm["text"])
        g = _count_glued_script(cand)
        if best_garble is None or g < best_garble:
            best_llm, best_prose, best_garble = llm, cand, g
        if not _looks_garbled(cand):
            break
        log.warn(f"Phase 5 合议疑似退化为乱码 (中英粘连信号={g}), 重跑 (尝试 {attempt}/2)")
    return best_llm, best_prose


def _dry_run_phase_5_body_prose(panel_judges: list[str], panel_summary: dict) -> str:
    """dry-run 模式的占位 Phase 5 body · 仍含 Phase 5 — Lead Merge 标题与四子段"""
    if panel_summary.get("scoring_mode") == "sum_max_score":
        concl = (f"dry-run 模式: 维度总分 {panel_summary['dimension_total_mean']}/"
                 f"{panel_summary['total_max']}, {panel_summary['grade_explanation']}.")
    else:
        direction = "↑" if panel_summary["anchor_delta"] > 0 else ("↓" if panel_summary["anchor_delta"] < 0 else "→")
        concl = (f"dry-run 模式: anchor_delta = {panel_summary['anchor_delta']:+.2f} {direction}, "
                 f"{panel_summary['delta_explanation']}.")
    return f"""## Phase 5 — Lead Merge

> dry-run placeholder — 真跑由 Opus 4.7 合议 synthesis + reviews 生成 prose。

### 结论

{concl}
真跑时此处由 Lead 综合 {len(panel_judges)} 位评委 review 写 2-3 句结论。

### 评委分歧高亮

dry-run 模式: 真跑时由 Lead 扫描各评委分数, 标出维度间分歧大的对子。

### 共识 / 矛盾

dry-run placeholder.

### Attribution 建议 (30/90/365)

- 30d: [Lead 提议, 项目核心 finalize]
- 90d: [Lead 提议]
- 365d: [Lead 提议]
"""


def _study_weekly_lightweight_body(panel_summary: dict) -> str:
    """study_weekly_v8 轻量化 Phase 5 body (零 LLM): 成员交付物是 report-v8.md 六段式
    (Phase 4 frontmatter 机械渲染), report.md 只作审计留痕, 不需 Opus 合议 prose。
    用 panel_summary 机械小结, 省一次 deep call; 打分仍全在 Phase 4, 不受影响。"""
    total = panel_summary.get("dimension_total_mean")
    mx = panel_summary.get("total_max", 100)
    grade = panel_summary.get("grade", "")
    expl = panel_summary.get("grade_explanation", "")
    head = (f"总分 {total:.0f}/{mx} · 等级 {grade}"
            if isinstance(total, (int, float)) else "见下方六段式诊断")
    return (
        "## Phase 5 — Lead Merge (study-weekly 轻量化)\n\n"
        "> 学习小组周报自省: 交付物为 **report-v8.md** 六段式诊断 (由 v8-coach 评委 frontmatter\n"
        "> 机械渲染)。本 report.md 仅作审计留痕, 故 Phase 5 不调 Opus 合议 prose (省一次 deep call,\n"
        "> 打分全在 Phase 4 不受影响)。\n\n"
        f"### 结论\n\n{head}。{expl}\n\n"
        "### 明细\n\n完整六段式 (岗位价值 / 5 维基础分 / 反向扣分 / 总分等级 / 改进建议 / 重写示例)\n"
        "见 `report-v8.md`。自省式诊断, 评分与等级不作为绩效依据。\n"
    )


def _call_anthropic_phase_5(
    cfg: PipelineConfig, panel_judges: list[str], synthesis_md: str,
    context_md: str, reviews_dir: Path, log: Logger,
) -> dict[str, Any]:
    """Lead 合议: Opus 一次调用, 读 synthesis + 全部 reviews, 输出 body prose."""
    # 拼所有 review (Lead 是唯一能跨读的角色)
    review_blobs = []
    for j in panel_judges:
        p = reviews_dir / f"{j}.md"
        if not p.exists():
            review_blobs.append(f"\n=== reviews/{j}.md (MISSING) ===\n")
            continue
        review_blobs.append(f"\n=== reviews/{j}.md ===\n\n{p.read_text(encoding='utf-8')}")
    reviews_text = "".join(review_blobs)

    user_msg = (
        f"# 议题\n\n{cfg.topic}\n\n"
        f"# brand_slug\n\n{cfg.brand_slug}\n\n"
        f"# Phase 1 Context\n\n{context_md}\n\n"
        f"# Phase 3 Synthesis\n\n{synthesis_md}\n\n"
        f"# Phase 4 全部评委 reviews\n\n{reviews_text}\n\n"
        f"请按 system prompt 末尾的格式写 Phase 5 body prose (不含 frontmatter)。"
    )

    if not cfg.no_redact:
        _check_redact_or_raise(user_msg, log, label="phase-5-prompt")

    response = _call_llm(
        cfg,
        model=cfg.model_deep,
        max_tokens=8192,
        system=_phase_5_system_prompt(panel_judges),
        user=user_msg,
        phase="phase_5",
    )
    text = response.text
    tokens_in = response.input_tokens
    tokens_out = response.output_tokens
    cost = tokens_in / 1_000_000 * 15.0 + tokens_out / 1_000_000 * 75.0

    if not cfg.no_redact:
        _check_redact_or_raise(text, log, label="phase-5-response")

    return {"text": text, "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost}


# ──────────────────────────────────────────────────────────────────────
# Verify — smoke_e2e.verify + redact + skill_lint
# ──────────────────────────────────────────────────────────────────────

def phase_verify(cfg: PipelineConfig, case_id: str, log: Logger) -> dict[str, Any]:
    """
    最后一关 (设计 note §3): 三件套自检.
      1. smoke_e2e.verify(VAULT_ROOT, brand_slug) — 7 类契约 (case schema /
         dim adversarial_view / panel_summary / versions filename / panel.yaml /
         raw_evidence ≥ 3 / synthesis exists)
      2. redact_check on 全产物 — 不能出真名 / 财务数字 / lark://
      3. skill_lint — Skill / panels / schemas 一致性

    任一失败 → exit 5, 不删产物 (Q4=a, 设计 §7).
    """
    log.step("Verify", "smoke_e2e.verify + redact_check + skill_lint")

    # P1 #2 修 (dev-plan v2.10): verify 前先跑 yaml 后处理兜底
    # 即使 Phase 4 真跑路径没接到 fix_review_yaml, 这里也能修
    _verify_pre_step_fix_yaml(cfg, log)

    if cfg.dry_run:
        log.info("DRY-RUN Verify: 跑 smoke_e2e.verify (dry-run 产物应过) + skip redact/lint")
        return _run_smoke_verify(cfg, log)

    # 1. smoke_e2e.verify
    v = _run_smoke_verify(cfg, log)
    # 2. redact_check 扫产物 (跳过 --no-redact)
    if not cfg.no_redact:
        r = _run_redact_scan(cfg, log)
        v["checks_passed"] += r["passed"]
        v["checks_failed"] += r["failed"]
    # 3. skill_lint (项目级一致性检查, 不针对单 case): 失败**只告警不阻断本次 verify** —
    #    它扫全项目 skills/panels/cases, 别的 case 的占位/旧 SKILL 不该卡本次 (尤其评审
    #    服务的一次性产出)。commit 时 git pre-commit 钩子仍强制 skill_lint, 安全网不丢。
    l = _run_skill_lint(log)
    v["checks_passed"] += l["passed"]
    if l["failed"]:
        log.warn("  · skill_lint 未过 (项目级 lint, 不阻断本次 case verify; commit 由 pre-commit 强制)")

    return v


def _verify_pre_step_fix_yaml(cfg: PipelineConfig, log: Logger) -> None:
    """Verify 前预处理: reports/<brand>/reviews/*.md 跑 fix_review_yaml 自动修复.
    若 review 文件是 Claude Code as Lead / Hermes / 外部写, 可能没过 Phase 4 fix 兜底."""
    try:
        import fix_review_yaml
    except ImportError:
        return
    reviews_dir = REPORTS_DIR / cfg.brand_slug / "reviews"
    if not reviews_dir.exists():
        return
    for review in reviews_dir.glob("*.md"):
        status = fix_review_yaml.process_file(review)
        if status == "ok-fixed":
            log.info(f"  · {review.name} yaml 后处理修复 (verify 兜底)")
        elif status.startswith("fail"):
            log.warn(f"  · {review.name} yaml 仍 fail ({status}), smoke_e2e.verify 会报")


def _run_smoke_verify(cfg: PipelineConfig, log: Logger) -> dict[str, Any]:
    """调 smoke_e2e.verify(VAULT_ROOT, brand_slug) — case_id hardcoded 在 smoke 里,
    但 verify 用 brand 路径找 reports, case_id 走 cases/<smoke 默认 C-2026-9999>。
    本地 runner 用 真 case_id, 所以需要临时 patch SMOKE_CASE_ID."""
    try:
        import smoke_e2e
    except ImportError as e:
        raise PipelineError(f"smoke_e2e 导入失败: {e}")

    # smoke_e2e.verify 内部用全局 CASE_ID = "C-2026-9999", 这里临时切到本 case
    # (read-only 验证, 不影响 smoke 自身用例)
    saved = smoke_e2e.CASE_ID
    smoke_e2e.CASE_ID = (next(iter(_local_case_ids(cfg))) if False else _current_case_id_for_brand(cfg))
    try:
        result = smoke_e2e.verify(VAULT_ROOT, brand_slug=cfg.brand_slug)
    finally:
        smoke_e2e.CASE_ID = saved

    for c in result["checks"]:
        log.dbg(f"  ✓ {c}")
    for e in result["errors"]:
        log.err(f"  ✗ {e}")
    for w in result["warnings"]:
        log.warn(f"  ⚠ {w}")

    return {
        "checks_passed": len(result["checks"]),
        "checks_failed": len(result["errors"]),
        "errors": result["errors"],
    }


def _current_case_id_for_brand(cfg: PipelineConfig) -> str:
    """Verify 时需要 case_id 来定位 cases/<id>/. 从 reports/<brand>/report.md 读 case_id."""
    report_path = REPORTS_DIR / cfg.brand_slug / "report.md"
    if not report_path.exists():
        # fallback: 找最新 case_id 关联 brand
        cid = _find_latest_case_for_brand(cfg.brand_slug, Logger())
        if cid is None:
            raise PipelineError(f"verify: 找不到 brand={cfg.brand_slug} 的 case_id")
        return cid
    text = report_path.read_text(encoding="utf-8")
    m = re.search(r"^case_id:\s*(C-\d{4}-\d{4})", text, re.MULTILINE)
    if not m:
        raise PipelineError(f"verify: report.md 无 case_id frontmatter")
    return m.group(1)


def _local_case_ids(cfg: PipelineConfig) -> list[str]:
    """(unused stub for clarity, 保留以便后续 batch 验证)"""
    return []


def _run_redact_scan(cfg: PipelineConfig, log: Logger) -> dict[str, int]:
    """对 reports/<brand>/ + cases/<id>/ 全部 markdown / json 跑 redact_check."""
    try:
        import redact_check
    except ImportError as e:
        log.warn(f"redact_check 导入失败 — 跳过: {e}")
        return {"passed": 0, "failed": 0}

    targets: list[Path] = []
    brand_dir = REPORTS_DIR / cfg.brand_slug
    case_id = _current_case_id_for_brand(cfg)
    case_dir = CASES_DIR / case_id
    for d in (brand_dir, case_dir):
        if d.exists():
            for p in d.rglob("*"):
                if p.is_file() and p.suffix in (".md", ".json", ".yaml", ".yml"):
                    targets.append(p)

    passed = 0
    failed = 0
    for f in targets:
        try:
            text = f.read_text(encoding="utf-8")
            blocked, hits = redact_check.check_text(text, str(f.relative_to(VAULT_ROOT)))
        except Exception as e:
            log.warn(f"redact_check 扫 {f.name} 异常: {e}")
            continue
        if blocked:
            failed += 1
            log.err(f"  ✗ redact: {f.relative_to(VAULT_ROOT)} 命中 {len(hits)} 条")
        else:
            passed += 1
            log.dbg(f"  ✓ redact: {f.relative_to(VAULT_ROOT)}")
    log.info(f"redact 扫 {len(targets)} 文件 · {passed} pass / {failed} fail")
    return {"passed": passed, "failed": failed}


def _run_skill_lint(log: Logger) -> dict[str, int]:
    """跑 scripts/skill_lint.py (项目级一致性检查, 不针对单 case)"""
    lint_py = SCRIPTS_DIR / "skill_lint.py"
    if not lint_py.exists():
        log.warn("scripts/skill_lint.py 不存在 — 跳过")
        return {"passed": 0, "failed": 0}
    try:
        result = subprocess.run(
            [sys.executable, str(lint_py)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            log.dbg("  ✓ skill_lint PASS")
            return {"passed": 1, "failed": 0}
        log.err(f"  ✗ skill_lint FAIL (exit {result.returncode})")
        for line in result.stdout.splitlines()[-10:]:
            log.err(f"      {line}")
        return {"passed": 0, "failed": 1}
    except subprocess.TimeoutExpired:
        log.warn("skill_lint 超时 — 计 1 fail")
        return {"passed": 0, "failed": 1}
    except Exception as e:
        log.warn(f"skill_lint 异常 — 计 1 fail: {e}")
        return {"passed": 0, "failed": 1}


# ──────────────────────────────────────────────────────────────────────
# 主入口 / 编排
# ──────────────────────────────────────────────────────────────────────
# PipelineError 已下沉 boss_core/errors.py (M0.1a), 在文件顶部 re-export。


def _now_iso() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _validate_config(cfg: PipelineConfig) -> None:
    if not cfg.brand_slug or not BRAND_SLUG_RE.match(cfg.brand_slug):
        raise PipelineError(
            f"--brand 不合法: {cfg.brand_slug!r}. 要求 [a-z0-9-] · 3-64 字符 · 首尾非 '-'"
        )
    if not cfg.evolution and not cfg.topic and not cfg.resume_from and not cfg.review_doc_path:
        raise PipelineError("FRESH 模式必须提供议题描述 (positional 参数). REVIEW 用 --review <doc>.")


def _gate_1_confirm(cfg: PipelineConfig, phase_1_out: dict[str, Any], log: Logger) -> bool:
    """
    Q1: GATE 1 confirm.
    - 当前默认 auto_confirm=False, 阻塞等 stdin Y/N
    - --auto-confirm 跳过 (V0 dev mode 推荐)
    """
    if cfg.auto_confirm:
        log.info("GATE 1 · --auto-confirm 跳过人工确认")
        return True

    print("\n────────────────────────────────────────────────────────────────")
    print(f"  GATE 1 · PRD 起草完成. 是否继续到 Phase 2 并行调研?")
    print(f"  PRD: {phase_1_out['prd_path']}")
    print(f"  Context: {phase_1_out['context_path']}")
    print(f"  Wiki hits: {phase_1_out['wiki_hits']}")
    print(f"  Topic type: {phase_1_out['topic_type']}")
    print(f"  Tokens used: {phase_1_out['tokens_in']} in / {phase_1_out['tokens_out']} out")
    print("────────────────────────────────────────────────────────────────")
    try:
        ans = input("继续? [y/N] ").strip().lower()
    except EOFError:
        log.warn("stdin 关闭 (非交互环境). 默认拒绝 — 用 --auto-confirm 跳过 GATE")
        return False
    return ans in ("y", "yes")


def run_pipeline(cfg: PipelineConfig, log: Logger) -> int:
    """主编排逻辑. 返回 exit code."""
    _validate_config(cfg)
    _PHASE_LLM_SECONDS.clear()   # profile: 本 job 起手清零 per-phase LLM 计时 (进程=job)

    # R8: 本 job 起手清 failover 会话累加 (进程=job); 报告 frontmatter 据此打标发生过的切换
    try:
        import llm_failover
        llm_failover.reset_session()
    except Exception:  # noqa: BLE001 — failover 模块缺失不影响主流程
        pass

    if cfg.dry_run:
        log.info("=== DRY-RUN 模式 · 0 token 消耗 · 仅打印计划与估算 ===")

    # Phase 0
    mode, case_id = phase_0_router(cfg, log)

    state = PipelineState(
        case_id=case_id,
        brand_slug=cfg.brand_slug,
        mode=mode,
        started_at=_now_iso(),
        config=asdict(cfg),
    )
    state.phases_completed.append("phase-0")

    # REVIEW mode (ADR-007): parse doc after router
    parsed_doc: Optional[ReviewDocParse] = None
    if mode == "REVIEW":
        parsed_doc = _parse_review_doc(cfg.review_doc_path, cfg, log)
        # doc 推的 topic override cfg.topic (if not user-specified)
        if not cfg.topic:
            cfg.topic = parsed_doc.topic
            log.info(f"  · REVIEW topic from doc: {cfg.topic[:80]}")
        # 持久化 parsed_doc 给 Phase 1-5 复用 (即使 dry-run, 因 Phase 5 需要)
        parsed_doc_path = CASES_DIR / case_id / "_parsed_doc.json"
        parsed_doc_path.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        parsed_doc_path.write_text(
            _json.dumps(asdict(parsed_doc), ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
        log.info(f"  · parsed doc saved: {parsed_doc_path}")

    # Resume support (skip phases already done)
    skip_until = cfg.resume_from
    def should_skip(phase: str) -> bool:
        if skip_until is None:
            return False
        # phase-0..5: 字符串比较够用
        return phase < skip_until

    # P1 #5 修 (dev-plan v2.12): EVOLUTION + dry-run 保护 — 所有 Phase 不写 rolling
    evo_dry_run_safe = (mode == "EVOLUTION" and cfg.dry_run)
    if evo_dry_run_safe:
        log.info("EVOLUTION + --dry-run: 所有 Phase 跳写盘 (保护 v1 rolling 文件), 仅运行 logic + log")

    # EVOLUTION 模式: 锁 v_prev topic_type (P1 #5 修)
    forced_topic_type = None
    if mode == "EVOLUTION":
        forced_topic_type = _load_v_prev_topic_type(case_id, log)
        if forced_topic_type:
            log.step("Panel", f"EVOLUTION: 锁 topic_type={forced_topic_type} (来自 v1 case.json + panel reverse-lookup)")

    # Phase 1 · FRESH/EVOLUTION: phase_1_prd_context · REVIEW: phase_1_review_from_doc (ADR-007)
    if not should_skip("phase-1"):
        if mode == "REVIEW":
            p1 = phase_1_review_from_doc(cfg, case_id, parsed_doc, log,
                                          skip_write_dry_run=evo_dry_run_safe)
        else:
            p1 = phase_1_prd_context(cfg, case_id, log,
                                      forced_topic_type=forced_topic_type,
                                      skip_write_dry_run=evo_dry_run_safe)
        state.artifacts["phase-1"] = [p1["prd_path"], p1["context_path"]]
        state.token_usage["phase-1"] = {
            "in": p1["tokens_in"], "out": p1["tokens_out"], "cost_usd": p1["cost_usd"],
        }
        state.phases_completed.append("phase-1")
        state.save()

        # GATE 1
        if not _gate_1_confirm(cfg, p1, log):
            log.info("GATE 1 拒绝 — 流水线在 Phase 1 后停止. 产物保留.")
            state.errors.append("gate_1_rejected")
            state.save()
            return 0
    else:
        log.info(f"resume-from={skip_until} · 跳过 Phase 1")
        p1 = {"topic_type": forced_topic_type or DEFAULT_TOPIC_TYPE}  # 占位

    # 选 panel · Phase 2/3/4 都用到
    refresh_anchor_judges(cfg.panel, log)  # v0.8: anchor 评委集合 panel 驱动 (R13 前置)
    refresh_scoring_spec(cfg.panel, log)   # 打分模式 panel 驱动 (sum_max_score 真打分)
    panel_judges = _select_panel_judges(p1.get("topic_type", DEFAULT_TOPIC_TYPE), cfg.panel, log)
    research_dims = [j for j in panel_judges if j != "tian"]
    # P1 #4 (dev-plan v2.10): EVOLUTION + --diff-only 过滤; FRESH 忽略 diff_only
    dims_to_rerun, judges_to_rerun = _apply_diff_only_filter(
        cfg, mode, panel_judges, research_dims, log,
    )

    # v0.6 R7 · Phase 1E auto diff plan: EVOLUTION 未传 --diff-only 时 LLM 自动选维度
    if (mode == "EVOLUTION" and not cfg.diff_only and not cfg.no_diff_plan
            and dims_to_rerun == research_dims and not should_skip("phase-2")):
        auto_dims, plan_usage = phase_1e_diff_plan(cfg, case_id, research_dims, log)
        state.token_usage["phase-1e"] = plan_usage
        state.save()
        if auto_dims:
            dims_to_rerun = [d for d in research_dims if d in auto_dims]
            judges_to_rerun = [j for j in panel_judges
                               if (j in dims_to_rerun) or (j in ANCHOR_JUDGES)]

    if dims_to_rerun != research_dims:
        log.step("Panel", f"full panel = {panel_judges} · full dims = {research_dims}")
        log.step("Panel", f"选择性重跑: rerun judges = {judges_to_rerun} · rerun dims = {dims_to_rerun}")
    else:
        log.step("Panel", f"judges = {panel_judges} · research dims = {research_dims}")

    # Phase 2 (FRESH/EVOLUTION 跑调研, REVIEW 默认跳; REVIEW + --verify → 真 web 调研 C3)
    p2_failed: list[str] = []
    review_web_evidence = ""
    if not should_skip("phase-2"):
        if mode == "REVIEW" and not cfg.review_verify:
            log.step("Phase 2 (REVIEW)", "跳过 — REVIEW 默认不调研 (加 --verify 开 Phase 2 claim verification)")
            state.artifacts["phase-2"] = []
            state.token_usage["phase-2"] = {"in": 0, "out": 0, "cost_usd": 0.0}
            state.phases_completed.append("phase-2")
            state.save()
        elif mode == "REVIEW" and cfg.review_verify:
            context_md = _load_context_md(case_id)
            p2v = phase_2_review_verify(cfg, case_id, parsed_doc, context_md, log)
            review_web_evidence = p2v["web_evidence"]
            state.artifacts["phase-2"] = p2v["raw_evidence_paths"]
            state.token_usage["phase-2"] = {"in": p2v["tokens_in"], "out": p2v["tokens_out"],
                                            "cost_usd": p2v["cost_usd"]}
            state.phases_completed.append("phase-2")
            state.save()
        else:
            context_md = _load_context_md(case_id)
            p2 = phase_2_parallel_research(cfg, case_id, dims_to_rerun, context_md, log,
                                            skip_write_dry_run=evo_dry_run_safe)
            state.artifacts["phase-2"] = p2["raw_evidence_paths"]
            state.token_usage["phase-2"] = {"in": p2["tokens_in"], "out": p2["tokens_out"], "cost_usd": p2["cost_usd"]}
            if p2["failed_dims"]:
                state.errors.append(f"phase-2-failed-dims: {p2['failed_dims']}")
            p2_failed = p2["failed_dims"]
            state.phases_completed.append("phase-2")
            state.save()

    # Phase 3 · FRESH/EVOLUTION: phase_3_synthesis · REVIEW: phase_3_review_synthesis (ADR-007)
    if not should_skip("phase-3"):
        context_md = _load_context_md(case_id)
        if mode == "REVIEW":
            p3 = phase_3_review_synthesis(cfg, case_id, parsed_doc, context_md, log,
                                           skip_write_dry_run=evo_dry_run_safe,
                                           web_evidence=review_web_evidence)
        else:
            raw_paths = state.artifacts.get("phase-2") or [
                f"cases/{case_id}/raw_evidence/dim_{d.replace('-', '_')}.md" for d in research_dims
            ]
            # v0.6 R7: EVOLUTION 部分重跑时 synthesis 仍要读全维度证据
            # (未变维度沿用上版 raw_evidence 文件, 同一 case dir 复用)
            if mode == "EVOLUTION" and dims_to_rerun != research_dims:
                all_paths = [
                    f"cases/{case_id}/raw_evidence/dim_{d.replace('-', '_')}.md"
                    for d in research_dims
                ]
                existing = [p for p in all_paths if (VAULT_ROOT / p).exists()]
                if existing:
                    raw_paths = existing
            p3 = phase_3_synthesis(cfg, case_id, raw_paths, context_md, p2_failed, log,
                                    skip_write_dry_run=evo_dry_run_safe)
        state.artifacts["phase-3"] = [p3["synthesis_path"]]
        state.token_usage["phase-3"] = {"in": p3["tokens_in"], "out": p3["tokens_out"], "cost_usd": p3["cost_usd"]}
        state.phases_completed.append("phase-3")
        state.save()

    # Phase 4 (只派 judges_to_rerun; 沿用 prev review 的不动)
    if not should_skip("phase-4"):
        context_md = _load_context_md(case_id)
        synthesis_md = _load_synthesis_md(case_id)
        p4 = phase_4_judges(cfg, case_id, judges_to_rerun, synthesis_md, context_md, log,
                             skip_write_dry_run=evo_dry_run_safe)
        state.artifacts["phase-4"] = p4["review_paths"]
        state.token_usage["phase-4"] = {"in": p4["tokens_in"], "out": p4["tokens_out"], "cost_usd": p4["cost_usd"]}
        if p4["failed_judges"]:
            state.errors.append(f"phase-4-failed-judges: {p4['failed_judges']}")
        state.phases_completed.append("phase-4")
        state.save()

    # Phase 5
    if not should_skip("phase-5"):
        context_md = _load_context_md(case_id)
        synthesis_md = _load_synthesis_md(case_id)
        p5 = phase_5_merge(cfg, case_id, panel_judges, synthesis_md, context_md, log,
                            skip_write_dry_run=evo_dry_run_safe)
        state.artifacts["phase-5"] = [
            p5["report_path"], p5["version_path"],
            p5["case_json_path"], p5["panel_yaml_path"],
        ]
        state.token_usage["phase-5"] = {"in": p5["tokens_in"], "out": p5["tokens_out"], "cost_usd": p5["cost_usd"]}
        state.phases_completed.append("phase-5")
        state.save()

    # Verify (Q4: 失败保留产物, exit 5)
    pv = phase_verify(cfg, case_id, log)
    state.phases_completed.append("verify")
    if pv["checks_failed"] > 0:
        state.errors.append(f"verify-failed: {pv['checks_failed']} checks fail, see logs")
        state.save()
        total_cost = sum(v.get("cost_usd", 0) for v in state.token_usage.values())
        log.err(f"=== Pipeline FAILED Verify · {case_id} · 产物保留 · 总成本 ~${total_cost:.2f} ===")
        return 5
    state.save()

    # ⏱ per-phase LLM 计时 (profile 提速: 看哪个 phase 最吃时间)
    if _PHASE_LLM_SECONDS:
        parts = " · ".join(f"{k}={v:.0f}s" for k, v in sorted(_PHASE_LLM_SECONDS.items()))
        log.step("⏱ per-phase LLM", f"{parts}  (phase-4 为并行评委累加, 串行 phase 即墙钟)")

    # Phase 6 export (P1.3 · 任一 --export* flag 触发)
    if cfg.export_set:
        export_rc = phase_6_export(cfg, log)
        state.phases_completed.append("phase-6")
        if export_rc != 0:
            state.errors.append(f"phase-6-export-failed: exit={export_rc}")
            state.save()
            log.err(f"=== Pipeline FAILED Phase 6 export · exit={export_rc} · 前序产物保留 ===")
            return 6
        state.save()

    # Summary
    total_cost = sum(v.get("cost_usd", 0) for v in state.token_usage.values())
    log.info(f"=== Pipeline DONE · {case_id} · {mode} mode · 总成本 ~${total_cost:.2f} USD ===")
    if cfg.dry_run:
        log.info("=== DRY-RUN: 0 token 实际消耗. 真跑请去掉 --dry-run ===")

    return 0


def phase_6_export(cfg: PipelineConfig, log: Logger) -> int:
    """Phase 6 export (P1.3). 复用 export_case_report.py + export_phase6.py subprocess.

    Set A: 一键 subprocess 调 export_case_report.py (Python end-to-end, 无 LLM)
    Set B/C: prepare-only · 渲染 prompt 到 /tmp/ + 打印 sub-agent dispatch 指南.

    CLI 不做 sub-agent 自动 dispatch 是 design choice — CLI 已有 provider-based LLM calls
    (line 1071+ Phase 2 sub-agent), 但 Set B/C 端到端 (含 redact_check fail-close +
    Chrome PDF) 走 Claude Code Agent tool 自然得多. CLI 给出 prepare 输出 + 指南即可,
    用户/项目主理 手动 dispatch 后跑 finalize-b/c.

    返回 exit code (0=成功 / 非 0=Set A 失败).
    """
    set_flag = cfg.export_set
    brand = cfg.brand_slug
    log.step("Phase 6", f"export-set={set_flag} · brand={brand} · "
                          f"redact={cfg.export_redact} · skip-pdf={cfg.export_skip_pdf}")

    exit_code = 0

    # Set A — Python end-to-end, 无 LLM
    if set_flag in ("a", "all"):
        args = ["python3", str(VAULT_ROOT / "scripts" / "export_case_report.py"), brand]
        if cfg.export_skip_pdf:
            args.append("--skip-pdf")
        result = subprocess.run(args, cwd=VAULT_ROOT)
        if result.returncode != 0:
            log.warn(f"  ⚠ Set A export 失败 (exit={result.returncode})")
            exit_code = result.returncode
        else:
            artifacts = "{md,html}" if cfg.export_skip_pdf else "{md,html,pdf}"
            log.info(f"  ✓ Set A: writing/reports-export/{brand}_v<n>.{artifacts}")

    # Set B — prepare-only (LLM sub-agent dispatch 走 Claude Code 或外部 finalize)
    if set_flag in ("b", "all"):
        log.info(f"  · Set B Light public prepare:")
        args = ["python3", str(VAULT_ROOT / "scripts" / "export_phase6.py"),
                "prepare-b", brand, "--redact-level", cfg.export_redact]
        result = subprocess.run(args, cwd=VAULT_ROOT)
        if result.returncode != 0:
            log.warn(f"  ⚠ Set B prepare 失败 (exit={result.returncode})")
            exit_code = exit_code or result.returncode
        else:
            log.info(f"  ✓ Set B prepare 完成")
        log.info(f"    → sub-agent dispatch 后跑: python3 scripts/export_phase6.py finalize-b {brand} "
                  f"--short-slug <thematic>{' --skip-pdf' if cfg.export_skip_pdf else ''}")

    # Set C — prepare-only
    if set_flag in ("c", "all"):
        log.info(f"  · Set C v2 internal prepare:")
        args = ["python3", str(VAULT_ROOT / "scripts" / "export_phase6.py"),
                "prepare-c", brand]
        result = subprocess.run(args, cwd=VAULT_ROOT)
        if result.returncode != 0:
            log.warn(f"  ⚠ Set C prepare 失败 (exit={result.returncode})")
            exit_code = exit_code or result.returncode
        else:
            log.info(f"  ✓ Set C prepare 完成")
        log.info(f"    → sub-agent dispatch 后跑: python3 scripts/export_phase6.py finalize-c {brand} "
                  f"--short-slug <thematic>{' --skip-pdf' if cfg.export_skip_pdf else ''}")

    return exit_code


def _load_context_md(case_id: str) -> str:
    """读 cases/<id>/context.md (Phase 1 产出). 不存在则空字符串."""
    p = CASES_DIR / case_id / "context.md"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def _load_synthesis_md(case_id: str) -> str:
    """读 cases/<id>/synthesis.md (Phase 3 产出). 不存在则空字符串 (--resume-from phase-4 时 OK)."""
    p = CASES_DIR / case_id / "synthesis.md"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def _load_v_prev_topic_type(case_id: str, log: Logger) -> Optional[str]:
    """
    P1 #5 修 (dev-plan v2.12): EVOLUTION 模式锁 v1 topic_type, 防止 Phase 1 重新分类.

    优先级 (直接读, 不反查 panel — customer/product 类 dimensions 集合相同, reverse-lookup ambiguous):
    1. cases/<case_id>/prd.md frontmatter 的 `topic_type:` 字段
    2. cases/<case_id>/case.json → 从 panel + judges 反查 panels/default.yaml.rules (兜底, 有 ambiguity)
    3. 都失败 → None (走 LLM 推断)

    Returns: topic_type slug ('strategic'/'customer'/...) or None
    """
    # 1. 优先从 prd.md frontmatter 读 (直接, 无 ambiguity)
    prd_path = CASES_DIR / case_id / "prd.md"
    if prd_path.exists():
        try:
            prd_text = prd_path.read_text(encoding="utf-8")
            m = re.search(r"^topic_type:\s*([a-z_]+)\s*$", prd_text, re.MULTILINE)
            if m:
                tt = m.group(1)
                log.dbg(f"_load_v_prev_topic_type: case={case_id} prd.md → topic_type={tt}")
                return tt
        except OSError as e:
            log.warn(f"_load_v_prev_topic_type: prd.md 读失败 {e}")

    # 2. 兜底: case.json + panel rule reverse-lookup (注意 customer/product dimensions 相同 ambiguity)
    case_json_path = CASES_DIR / case_id / "case.json"
    if not case_json_path.exists():
        return None
    try:
        data = json.loads(case_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warn(f"_load_v_prev_topic_type: case.json 读取失败 {e}")
        return None
    judges_v_prev = sorted(j for j in data.get("judges", []) if j != "tian")
    if not judges_v_prev:
        return None

    import yaml
    panel_yaml = PANELS_DIR / f"{data.get('panel', 'default')}.yaml"
    if not panel_yaml.exists():
        return None
    try:
        panel_cfg = yaml.safe_load(panel_yaml.read_text(encoding="utf-8"))
        rules = ((panel_cfg.get("auto_judge_selection") or {}).get("rules") or {})
        candidates = []
        for tt, rule in rules.items():
            if sorted(rule.get("dimensions") or []) == judges_v_prev:
                candidates.append(tt)
        if len(candidates) == 1:
            log.dbg(f"_load_v_prev_topic_type: case={case_id} case.json reverse-lookup → topic_type={candidates[0]}")
            return candidates[0]
        elif len(candidates) > 1:
            log.warn(f"_load_v_prev_topic_type: case={case_id} reverse-lookup 多匹配 {candidates}, 用 prd.md 而非反查 (返 None)")
            return None
    except yaml.YAMLError as e:
        log.warn(f"_load_v_prev_topic_type: panel yaml 解析失败 {e}")
    return None


def _apply_diff_only_filter(
    cfg: PipelineConfig, mode: str,
    panel_judges: list[str], research_dims: list[str],
    log: Logger,
) -> tuple[list[str], list[str]]:
    """
    P1 #4 (dev-plan v2.10): EVOLUTION + --diff-only 时, 限制 Phase 2/4 只跑指定维度.

    规则:
    - FRESH 模式 + diff_only 非空 → 警告 + 忽略 (FRESH 必须全跑)
    - EVOLUTION + diff_only 空 → 全跑 (= 行为同未传 diff_only)
    - EVOLUTION + diff_only 非空:
      · "tian" / anchor 类 in diff_only → 警告 + 丢弃 (anchor 永远跑, 不可作 diff)
      · diff_only 含不在 panel 的维度 → 警告 + 丢弃
      · 最终 dims_to_rerun = diff_only ∩ research_dims
      · judges_to_rerun = dims_to_rerun + anchor 评委 (anchor 永远跑)
      · 若过滤后 dims_to_rerun 为空 → PipelineError (至少要重跑 1 维)

    Returns:
        (dims_to_rerun, judges_to_rerun)
    """
    diff_only = list(cfg.diff_only)
    if not diff_only:
        # 未传 --diff-only: 全跑
        return research_dims, panel_judges

    # 有 --diff-only 但 FRESH 模式: 警告, 全跑
    if mode == "FRESH":
        log.warn(f"--diff-only={diff_only} 仅 EVOLUTION 模式生效 · FRESH 必全跑 · 已忽略")
        return research_dims, panel_judges

    # EVOLUTION + 有 diff_only: 过滤
    # 1) 丢 anchor 类
    anchor_in_diff = [j for j in diff_only if j in ANCHOR_JUDGES]
    if anchor_in_diff:
        log.warn(f"--diff-only 含 anchor 评委 {anchor_in_diff}, 已丢 (anchor 永远跑, 不可作 diff)")
        diff_only = [j for j in diff_only if j not in ANCHOR_JUDGES]

    # 2) 丢不在 panel 的维度
    invalid = [j for j in diff_only if j not in research_dims]
    if invalid:
        log.warn(f"--diff-only 含不在 panel 的维度 {invalid}, 已丢. 有效 panel dims = {research_dims}")
        diff_only = [j for j in diff_only if j in research_dims]

    if not diff_only:
        raise PipelineError(
            "--diff-only 过滤后为空 (全部 invalid 或 anchor). EVOLUTION 至少要重跑 1 维."
        )

    # 3) 构造 dims_to_rerun + judges_to_rerun (含 anchor)
    dims_to_rerun = [d for d in research_dims if d in diff_only]   # 保持 panel 顺序
    judges_to_rerun = [j for j in panel_judges if (j in dims_to_rerun) or (j in ANCHOR_JUDGES)]

    sustained_dims = [d for d in research_dims if d not in dims_to_rerun]
    if sustained_dims:
        log.info(f"--diff-only: {len(sustained_dims)} 维沿用上版 ({sustained_dims}) · 仅重跑 {len(dims_to_rerun)} 维")

    return dims_to_rerun, judges_to_rerun


# ─────────────────────────────────────────────────────────────────────
# v0.6 R7 · Phase 1E auto diff plan (CLAUDE.md §4.7)
#
# EVOLUTION 未传 --diff-only 时, LLM 对比上版判断书 + 本次触发, 自动选出
# 变化维度 (通常 1-3 个); 信号不足 / 解析失败 / dry-run → 降级全维度重跑 (fail-safe)。
# 强制全跑: --no-diff-plan。anchor 评委不参与选择 (永远重跑)。
# ─────────────────────────────────────────────────────────────────────

PHASE_1E_SYSTEM_PROMPT = """\
你是 EVOLUTION 模式的 Phase 1E diff planner (CLAUDE.md §4.7)。
任务: 对比上一版判断书与本次重判触发, 列出"自上版以来可能变了什么",
选出需要重跑调研与重打分的维度 (通常 1-3 个)。

只输出一个 JSON 对象, 不要解释、不要 code fence 之外的文字:
{"changed": ["<dim-slug>", ...], "reason": "<一句话: 什么变了>"}

规则:
- changed 只能从给定 dims 列表里选
- 触发信息不足以定位变化维度时, 返回全部 dims (保守全跑)
- 不要选 anchor 评委 (tian) — 它永远重跑, 不在 dims 列表里
"""


def _parse_diff_plan_json(text: str, research_dims: list[str]) -> Optional[list[str]]:
    """解析 Phase 1E LLM 输出。返回合法维度子集; 无法收窄 (空/全选/解析失败) → None。"""
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    changed = data.get("changed")
    if not isinstance(changed, list):
        return None
    valid = [d for d in research_dims if d in changed]
    if not valid or set(valid) == set(research_dims):
        return None  # 空 = 信号不足; 全选 = 等价全跑 — 都按降级处理
    return valid


def phase_1e_diff_plan(
    cfg: PipelineConfig, case_id: str, research_dims: list[str], log: Logger,
) -> tuple[Optional[list[str]], dict]:
    """LLM 自动 diff plan。返回 (dims 子集 | None, token usage)。None = 全维度重跑。"""
    zero = {"in": 0, "out": 0, "cost_usd": 0.0}
    report_path = REPORTS_DIR / cfg.brand_slug / "report.md"
    if not report_path.exists():
        return None, zero
    if cfg.dry_run:
        log.info("Phase 1E (dry-run): 跳过 LLM diff plan · 全维度重跑")
        return None, zero

    prior_report = report_path.read_text(encoding="utf-8", errors="replace")[:12000]
    user_msg = (
        f"# 上一版判断书 (截断 12k 字符)\n\n{prior_report}\n\n"
        f"# 本次重判触发\n\n{cfg.topic or '(未提供 — 例行 refresh, 倾向保守全跑)'}\n\n"
        f"# 可选 dims\n\n{json.dumps(research_dims, ensure_ascii=False)}\n"
    )
    if not cfg.no_redact:
        _check_redact_or_raise(user_msg, log, label="phase-1e-diff-plan-prompt")

    try:
        res = _call_llm(cfg, model=cfg.model_fast, max_tokens=400,
                        system=PHASE_1E_SYSTEM_PROMPT, user=user_msg, phase="phase_1e")
    except Exception as e:
        log.warn(f"Phase 1E diff plan LLM 失败 ({type(e).__name__}: {e}) · 降级全维度重跑")
        return None, zero

    # _call_llm 返回 llm_client.LLMResult (frozen dataclass), 用属性访问而非 dict 下标
    # (字段是 input_tokens/output_tokens, 无 cost — cost 由 _estimate_cost 算, 同 _emit_llm_usage)
    usage = {"in": res.input_tokens, "out": res.output_tokens,
             "cost_usd": _estimate_cost(res.model, res.input_tokens, res.output_tokens)}
    dims = _parse_diff_plan_json(res.text, research_dims)
    if dims is None:
        log.info("Phase 1E: diff plan 未能收窄维度 (信号不足/全选) · 全维度重跑")
        return None, usage
    m = re.search(r'"reason"\s*:\s*"([^"]+)"', res.text)
    log.step("Phase 1E", f"auto diff plan: {dims} · reason: {m.group(1) if m else '(见输出)'}")
    return dims, usage


def _select_panel_judges(topic_type: str, panel_name: str, log: Logger) -> list[str]:
    """从 panels/<panel>.yaml 的 auto_judge_selection 规则选出本议题的评委 slug 列表"""
    panel_path = _resolve_panel_path(panel_name)
    if not panel_path.exists():
        log.warn(f"panel 文件不存在: {panel_path} — 用默认 fallback")
        return ["tian", "industry-trend", "strategic-vision", "financial-strategy", "org-strategy"]

    try:
        import yaml
        panel_data = yaml.safe_load(panel_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warn(f"panel 解析失败 {panel_path}: {e} — 用 fallback")
        return ["tian", "industry-trend", "strategic-vision", "financial-strategy", "org-strategy"]

    selection = panel_data.get("auto_judge_selection")

    # 无 auto_judge_selection 的 panel (competition workshop / op2-tsg/dig/scg 等固定评委):
    # 评委由 judges_override/judges_add 显式定义, 用 panel_loader 解析后的 judges 列表,
    # 不能兜底成 ["tian"] (competition 无 anchor; 否则会跑错评委)。
    if not selection:
        try:
            # M1.3: panel 解析走 KBProvider (resolve_panel(panel_name) 内部即
            # panel_loader.resolve_panel(_resolve_panel_path(panel_name)) = 原 panel_path 路径)
            resolved = _hosted_kb(log).resolve_panel(panel_name)
            slugs = [j["slug"] for j in (resolved.get("judges") or []) if j.get("slug")]
            if slugs:
                return slugs
        except Exception as e:
            log.warn(f"panel_loader 解析 {panel_path} 失败: {e} — 用 fallback")
        return ["tian", "industry-trend", "strategic-vision", "financial-strategy", "org-strategy"]

    rules = selection.get("rules", {})
    # 核心常驻评委: default panel 用 default_include; scene panel (theme schema) 用 always_active
    base = list(selection.get("default_include") or selection.get("always_active") or ["tian"])

    if isinstance(rules, list):
        # scene panel theme schema (e.g. op2-company): rules 是
        #   [{theme, match_topic_types: [...], add_judges: [...]}],
        # 命中 topic_type 的所有 theme 的 add_judges 取并集, 追加到核心常驻评委。
        selected = list(base)
        for r in rules:
            if not isinstance(r, dict):
                continue
            if topic_type in (r.get("match_topic_types") or []):
                selected += list(r.get("add_judges") or [])
    else:
        # default panel schema: rules 按 topic_type 索引, value.dimensions 给整组维度
        rule = rules.get(topic_type) or rules.get("unknown") or {}
        selected = list(base) + list(rule.get("dimensions", []))

    # 去重保序 (评委 slug 唯一; base 与 add/dim 可能重叠)
    seen: set[str] = set()
    out: list[str] = []
    for j in selected:
        if j not in seen:
            seen.add(j)
            out.append(j)
    return out


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_pipeline_local.py",
        description="路径 B 落地脚手架: 可配置 LLM provider + Skill 直接驱动判断流水线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("topic", nargs="?", help="议题描述 (FRESH 必填; EVOLUTION+resume / REVIEW 可空)")
    p.add_argument("--brand", required=False, default=None,
                   help="brand_slug · reports/<brand>/ 路径 · FRESH/EVOLUTION required · REVIEW 默认 doc 文件名派生")
    p.add_argument("--panel", default="default", help="panels/<name>.yaml (默认 default)")
    p.add_argument("--model-fast", default=DEFAULT_MODEL_FAST, help=f"Phase 1-3 模型 (默认 {DEFAULT_MODEL_FAST})")
    p.add_argument("--model-deep", default=DEFAULT_MODEL_DEEP, help=f"Phase 4-5 模型 (默认 {DEFAULT_MODEL_DEEP})")
    p.add_argument("--llm-provider", default=DEFAULT_LLM_PROVIDER,
                   choices=sorted(llm_client.SUPPORTED_PROVIDERS),
                   help="LLM 后端: anthropic / anthropic-compatible / openai-compatible (默认读 BOSS_LLM_PROVIDER 或 anthropic)")
    p.add_argument("--llm-base-url", default=os.environ.get("BOSS_LLM_BASE_URL"),
                   help="兼容 API base_url (也可用 BOSS_LLM_BASE_URL / ANTHROPIC_BASE_URL / OPENAI_BASE_URL)")
    p.add_argument("--llm-api-key-env", default=None,
                   help="指定读取哪个环境变量作为 API key, 例如 KIMI_API_KEY / ZAI_API_KEY")
    p.add_argument("--dry-run", action="store_true", help="不调 LLM, 打印计划 + token 估算")
    p.add_argument("--auto-confirm", action="store_true", help="Q1: 跳过 GATE 1 stdin 确认 (V0 dev 推荐)")

    # 模式互斥 (ADR-007)
    mode_group = p.add_mutually_exclusive_group()
    mode_group.add_argument("--evolution", action="store_true",
                            help="强制 EVOLUTION 模式 (要求 report.md 已存在)")
    mode_group.add_argument("--review", dest="review_doc", default=None,
                            help="REVIEW 模式 (ADR-007): 评议现成方案 doc (.md/.txt)")

    p.add_argument("--diff-only", default="", help="EVOLUTION: 只重跑这些维度评委 (逗号分隔)")
    p.add_argument("--no-diff-plan", action="store_true",
                   help="EVOLUTION: 禁用 Phase 1E auto diff plan, 强制全维度重跑 (v0.6 R7)")
    p.add_argument("--verify", action="store_true",
                   help="REVIEW: 开 Phase 2 wiki 交叉验证 doc 里关键 claim (默认关)")
    p.add_argument("--review-into", action="store_true",
                   help="REVIEW: 允许写入已存在的 brand (默认拒绝覆盖)")
    p.add_argument("--resume-from", choices=["phase-1", "phase-2", "phase-3", "phase-4", "phase-5"],
                   help="从指定 phase 续跑, 跳过前序")
    p.add_argument("--no-redact", action="store_true", help="(debug) 跳过 redact_check 闸")
    p.add_argument("--verbose", "-v", action="store_true", help="打印 prompt / response 等调试信息")

    # Phase 6 export flags (P1.3 · 与 /boss SKILL.md §Export flags 对齐)
    export_group = p.add_mutually_exclusive_group()
    export_group.add_argument("--export", dest="export_short", action="store_true",
                               help="Phase 6 Set A confidential 完整合并 (等价 --export-set a)")
    export_group.add_argument("--export-full", action="store_true",
                               help="Phase 6 全 3 套 Set A + B + C (等价 --export-set all)")
    export_group.add_argument("--export-set", choices=["a", "b", "c", "all"], default=None,
                               help="Phase 6 显式选 set (a=confidential / b=public 脱敏 / c=internal 可视化 / all)")
    export_group.add_argument("--no-export", action="store_true",
                               help="(默认) 不导出. explicit 关闭, 与 --export* 互斥")
    p.add_argument("--export-redact", choices=["light", "strict", "meta-only"], default="light",
                   help="Set B 脱敏强度 (默认 light · 与 /boss SKILL.md §6.0 一致)")
    p.add_argument("--export-skip-pdf", action="store_true",
                   help="仅 md/html, 跳 Chrome PDF (用于快速 preview)")
    p.add_argument("--export-only", action="store_true",
                   help="跳过 Phase 0-5, 仅跑 Phase 6 (要求 brand 已存在 reports/<brand>/report.md)")
    return p


def _derive_brand_from_doc(doc_path: str) -> str:
    """REVIEW: 从 doc 文件名派生 brand_slug.

    例: docs/strategy-2026Q4.md → strategy-2026q4
        www/adr/ADR-005-hybrid-deployment.md → adr-005-hybrid-deployment
    """
    import re
    stem = Path(doc_path).stem  # 去扩展名
    # slugify: 转小写, 非字母数字短横线 全替 -, 折叠重复 -
    slug = re.sub(r'[^a-z0-9-]+', '-', stem.lower())
    slug = re.sub(r'-+', '-', slug).strip('-')
    # 中文/全非 ASCII 文件名 slugify 后会得到空串 (如 "方案.md" → "")
    # 导致下游 brand_slug 为空、REVIEW run 中断。退化为 stem 的稳定哈希短码:
    # 同一 doc → 同一 slug (可复现), 且保证非空、长度充足。
    if len(slug) < 3:
        import hashlib
        digest = hashlib.sha1(stem.encode('utf-8')).hexdigest()[:8]
        slug = f"review-{digest}"
    return slug


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Resolve export_set from flags (P1.3)
    export_set_resolved: Optional[str] = None
    if args.export_short:
        export_set_resolved = "a"
    elif args.export_full:
        export_set_resolved = "all"
    elif args.export_set:
        export_set_resolved = args.export_set
    # --no-export 显式 None (默认行为)

    # --export-only 校验
    if args.export_only:
        if not export_set_resolved:
            parser.error("--export-only 需配 --export / --export-set / --export-full")
        if not args.brand:
            parser.error("--export-only 需配 --brand")
        report_path = REPORTS_DIR / args.brand / "report.md"
        if not report_path.exists():
            parser.error(f"--export-only 要求 {report_path} 已存在 (跳 Phase 0-5, 仅 Phase 6)")

    # REVIEW mode (ADR-007): 派生 brand + 校验互斥
    review_doc_path = args.review_doc
    if review_doc_path:
        # REVIEW: brand 默认从 doc 文件名派生
        if not args.brand:
            args.brand = _derive_brand_from_doc(review_doc_path)
        # REVIEW + --diff-only 互斥
        if args.diff_only:
            parser.error("--diff-only 仅 EVOLUTION 模式可用, 与 --review 冲突")
        # REVIEW doc 必须存在
        try:
            resolved_review_doc = _resolve_review_doc_path(review_doc_path)
        except PipelineError as e:
            parser.error(str(e))
        if not resolved_review_doc.exists():
            parser.error(f"--review doc 不存在: {review_doc_path}")
    elif not args.export_only:
        # FRESH/EVOLUTION: brand required (除非 --export-only 已自校验)
        if not args.brand:
            parser.error("--brand required (FRESH/EVOLUTION 模式)")
        # FRESH/EVOLUTION: --verify 不适用
        if args.verify:
            parser.error("--verify 仅 REVIEW 模式可用 (ADR-007)")
        if args.review_into:
            parser.error("--review-into 仅 REVIEW 模式可用")

    cfg = PipelineConfig(
        topic=args.topic or "",
        brand_slug=args.brand,
        panel=args.panel,
        model_fast=args.model_fast,
        model_deep=args.model_deep,
        llm_provider=args.llm_provider,
        llm_base_url=args.llm_base_url,
        llm_api_key_env=args.llm_api_key_env,
        dry_run=args.dry_run,
        auto_confirm=args.auto_confirm,
        evolution=args.evolution,
        diff_only=[d.strip() for d in args.diff_only.split(",") if d.strip()],
        no_diff_plan=args.no_diff_plan,
        resume_from=args.resume_from,
        no_redact=args.no_redact,
        verbose=args.verbose,
        review_doc_path=review_doc_path,
        review_verify=args.verify,
        review_into=args.review_into,
        export_set=export_set_resolved,
        export_redact=args.export_redact,
        export_skip_pdf=args.export_skip_pdf,
        export_only=args.export_only,
    )
    log = Logger(verbose=args.verbose)

    # --export-only 路径: 跳 Phase 0-5, 直接 Phase 6
    if args.export_only:
        log.info(f"=== --export-only · 跳 Phase 0-5 · 仅 Phase 6 export ({export_set_resolved}) ===")
        return phase_6_export(cfg, log)

    try:
        return run_pipeline(cfg, log)
    except PipelineError as e:
        log.err(str(e))
        return 4
    except KeyboardInterrupt:
        log.err("中断 (Ctrl-C). 产物保留在 cases/ 与 reports/ 下.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
