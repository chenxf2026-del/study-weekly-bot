"""boss_core.prompts — Phase 4/5 system prompt 装配 (M0.2 · T2, 从 run_pipeline_local 抽库)。

打分段渲染 + 评委/Lead system prompt。原逻辑读运行时**可变**模块全局 (SCORING_SPEC /
SCORING_LENSES / ANCHOR_JUDGES, 由 refresh_scoring_spec 等改)。按 §6 R-a **不搬可变全局** ——
改为**显式收** scoring_spec / scoring_lenses / anchor_judges 参数, boss_core 保持无全局态。
run_pipeline_local 顶部定义**同名薄 wrapper**, 把当前 (可变) 全局注入进来, 保对外签名与
快照输出逐字不变 (test_op2_scoring / test_anchor_judges_panel 等)。

_phase_4_system_prompt 还依赖 anchor confidence cap 解析 (原 _anchor_confidence_cap, 走
skill_lint I/O); 同样按 DI 注入 (confidence_cap_fn), 让本模块既无全局态也无 I/O。

M2 boss-service 可直接调这些核函数, 传入 per-tenant / BYOM 的 scoring_spec。
"""

from __future__ import annotations

from typing import Callable, Optional


def _scores_spec_block(
    scoring_spec: dict,
    scoring_lenses: list,
    is_anchor: bool = False,
) -> tuple[str, str, str]:
    """按 scoring_spec 渲染 Phase 4 prompt 的打分相关三段:
       (scores_yaml, rubric_guidance, hard_constraint)。

    weighted_average: 5 镜头 1-10 (历史行为, rubric_guidance 空)。
    sum_max_score: panel 自定义维度 + 各维 max_score + 评分标准 (description)。

    is_anchor=True (锚点评委): 即便 sum_max 模式也保留 5 镜头心证 —— 锚点用自己的
    心智模型独立打分, 不读维度 doctrine (CLAUDE.md §4.5.3)。维度评委才走自定义 rubric。"""
    if is_anchor or scoring_spec.get("mode") != "sum_max_score":
        scores_yaml = "\n".join(f"  {lens}: <int 1-10>" for lens in scoring_lenses)
        return scores_yaml, "", "- 5 镜头分数都是 1-10 整数, 不写 N/A"

    lenses = scoring_spec["lenses"]
    scores_yaml = "\n".join(f"  {l['slug']}: <int 0-{l['max_score']}>" for l in lenses)
    rubric_lines = [
        f"# 打分维度 (本 panel 专属 · 满分相加 = {scoring_spec['total_max']})",
        "",
        "按下列维度各打一个整数分 (非 5 镜头 1-10), 严格依据每维评分标准:",
        "",
    ]
    for l in lenses:
        rubric_lines.append(f"- `{l['slug']}` · {l['display_name_cn']} (0-{l['max_score']} 分)")
        if l["description"]:
            for ln in l["description"].splitlines():
                ln = ln.strip()
                if ln:
                    rubric_lines.append(f"    {ln}")
    rubric_guidance = "\n".join(rubric_lines)
    slugs = ", ".join(f"{l['slug']}≤{l['max_score']}" for l in lenses)
    constraint = (
        f"- 打分维度是上述 {len(lenses)} 项 (非 5 镜头), 各为整数且不超过满分 ({slugs}), 不写 N/A"
    )
    return scores_yaml, rubric_guidance, constraint


def _placeholder_scores_block(
    scoring_spec: dict,
    scoring_lenses: list,
    fraction: float,
    is_anchor: bool = False,
) -> str:
    """dry-run / failed 占位 review 的 scores 块, 按 scoring_spec 渲染。

    fraction: 0.0 = 最低分 (failed), 0.5 = 中间分 (dry-run)。
    weighted / 锚点: 1-10 区间 (failed→1, dry→5); sum_max 维度评委: 0..max_score。
    is_anchor=True: 锚点保留 5 镜头 (与 _scores_spec_block 一致)。"""
    if is_anchor or scoring_spec.get("mode") != "sum_max_score":
        # 历史值: dry-run (0.5) → 5, failed (0.0) → 1
        val = 1 if fraction <= 0.0 else round(1 + fraction * 8)
        return "\n".join(f"  {lens}: {val}" for lens in scoring_lenses)
    return "\n".join(
        f"  {l['slug']}: {0 if fraction <= 0.0 else round(l['max_score'] * fraction)}"
        for l in scoring_spec["lenses"]
    )


_PHASE_5_SYSTEM_PROMPT_WEIGHTED = """\
你是锚点判断流水线 Phase 5 的 Lead。Phase 2-4 已产 synthesis + N 份独立评委 review。
你的任务: 写 report.md 的 body prose, 给锚点读。

约束:
- 不写 frontmatter (Python 已算好 panel_summary, 你只写 body)
- 不重新打分 (5 镜头分数已固化在评委 review)
- 重点交付: "## Phase 5 — Lead Merge" 段, 4 个子段:
  · 一句话结论 (anchor_delta 高低 + 杠杆 + 建议方向)
  · 评委分歧高亮 (单镜头维度评委间差 > 3 的)
  · 共识 / 矛盾
  · 30/90/365 attribution 建议 (供 项目主理 / 锚点 finalize)
- 不要给 panel_summary 数字 (已在 frontmatter), 用 ↑↓→ 描述 anchor_delta 方向

输出格式 (markdown 段, 不含 ---frontmatter---):

```
## Phase 5 — Lead Merge

### 结论
<2-3 句>

### 评委分歧高亮
- <judge_A 在 lens_X 给 9, judge_B 给 4, 分歧来自 ...>

### 共识 / 矛盾
共识: ...
矛盾: ...

### Attribution 建议 (30/90/365)
- 30d: <metric, data_source>
- 90d: ...
- 365d: ...
```
"""

# sum_max_score (op2-company / 竞赛): 维度评委按自定义维度满分制打分, 锚点 (若有)
# 用 5 镜头心证 (独立尺子, 不与维度总分跨尺度相减)。Lead 不得用 anchor_delta / 5 镜头措辞。
_PHASE_5_SYSTEM_PROMPT_SUM_MAX = """\
你是评审流水线 Phase 5 的 Lead。Phase 3-4 已产 synthesis + N 份独立评委 review。
你的任务: 写 report.md 的 body prose。

本场景用**自定义维度满分制**: 维度评委按各自满分的维度打分, 满分相加成总分;
若有锚点评委, 它用自己的独立心证打分 (与维度总分**不同尺子, 不要相减**)。

约束:
- 不写 frontmatter (Python 已算好 panel_summary, 你只写 body)
- 不重新打分 (分数已固化在评委 review 与 frontmatter)
- **只按本场景的自定义维度行文** —— 不要套用通用判断流水线的固定打分镜头名或跨维度差值方向等措辞。
- 重点交付: "## Phase 5 — Lead Merge" 段, 4 个子段:
  · 一句话结论 (总分高低 + 关键短板维度 + 建议方向; 有等级则提等级)
  · 评委分歧高亮 (某维度上评委间分差大的)
  · 共识 / 矛盾
  · 30/90/365 attribution 建议 (供 项目主理 / 锚点 finalize)

输出格式 (markdown 段, 不含 ---frontmatter---):

```
## Phase 5 — Lead Merge

### 结论
<2-3 句, 围绕维度总分 + 短板维度 + 建议>

### 评委分歧高亮
- <judge_A 在 维度X 给高分, judge_B 给低分, 分歧来自 ...>

### 共识 / 矛盾
共识: ...
矛盾: ...

### Attribution 建议 (30/90/365)
- 30d: <metric, data_source>
- 90d: ...
- 365d: ...
```
"""


def _phase_4_v8_system_prompt(
    judge: str, judge_skill_md: str, category: str,
    scores_yaml: str, rubric_section: str, score_constraint: str,
) -> str:
    """study_weekly_v8 场景专属 Phase 4 输出契约 (六段式自省诊断)。

    与通用块的差异: frontmatter 带全套 v8 payload (score_reasons/deductions/position_value/
    core_label/suggestions/rewrite_example, study_weekly_output.load_review_payload 消费),
    不强塞 adversarial_view / 通用三段体。六段式报告由渲染层 (render_personal_report) 从
    frontmatter 机械生成 —— 评委只需吐 frontmatter, 不自己排版、不自己算总分/等级。"""
    return f"""{judge_skill_md}

──────────────────────────────────────────────────────────────
# Phase 4 输出强约束 · v10 自省诊断 (run_pipeline_local 加, 不可违反)
──────────────────────────────────────────────────────────────

你是周报自省诊断评委 `{judge}` (雅总《周报自省式诊断》v8, SKILL.md 已在上方)。
任务: 对这份**周报**独立诊断, 产出**机器可读 frontmatter**。六段式报告由渲染层从
frontmatter 机械生成 —— 你不用自己排版六段, **也不要自己算总分/等级** (渲染层从
Σscores − Σdeductions 机械算, 你算了也会被忽略)。
{rubric_section}
输出格式 (markdown):

```
---
judge: {judge}
judge_display_name: <人格化称呼, 如 周报教练·价值证明官>
judge_category: {category}
brand_slug: <slug>
case_id: <ID>
version: 1
reviewed_at: <ISO timestamp>
framework_version: v10
scores:
{scores_yaml}
score_reasons:            # 每维一句话理由 (进 ② 表"理由"列), 尽量引周报原文短语
  anchor_problem: "<≤40字>"
  value_proof: "<≤40字>"
  decision_risk: "<≤40字>"
  time_loop: "<≤40字>"
  growth: "<≤40字>"
deductions:               # ③ 触发的反向扣分 (v7.22: 5 项一维一项 d1_value/d2_progress/d3_decision/d4_time/d5_cognition, 各 1-2 分, 合计上限 10); 无则写 []
  - slug: <d1_value|d2_progress|d3_decision|d4_time|d5_cognition 之一>
    points: <触发即扣, 轻微 1 / 明显 2, 整数 (每项上限 2)>
    reason: "<≤40字, 引原文>"
position_value: "<① 岗位价值判断, 1-2 句, 单行>"
core_label: "<核心标签 ≤20 字>"
suggestions:              # ⑤ 改进建议 2-3 条, 对应扣分最多的问题给具体动作
  - "<建议 1>"
  - "<建议 2>"
rewrite_example: "<⑥ 把一处低分内容改写为 v8 标准表达, 单行>"
confidence: <float 0-1>
---

<一段话总评 (正文, 可选; 不进个人六段式报告)>
```

硬约束:
- **输出的第一个字符必须是 `---`** — frontmatter 前**不要写任何前言/思考/开场白**
  (如"现在我来诊断…"、"先读周报…"); 分析正文放到结尾的 `---` 之后
{score_constraint}
- **deductions 的 slug 必须是 SKILL.md §二 的 5 项之一** (一维一项, 每维最多 1 条),
  **points 落在 [1, 2]** (v7.22: 每项 1-2 分, 触发即扣, 合计上限 10); 该维无问题就不列该项;
  没触发任何扣分就写 `deductions: []` (不编造扣分, 也不漏真实问题)
- **总分与等级不要自己写** — 渲染层从 Σscores − Σdeductions 机械算 (等级 A/B+/B/C/C-)
- frontmatter **必须是合法 YAML** (这条最容易踩坑, 严格遵守):
  · position_value / rewrite_example / 各 reason / suggestions 每一项, 只要值里含**冒号「:」「：」、
    引号、方括号、逗号或以数字开头**, 就**必须整体用双引号 `"..."` 包裹** (中文冒号也算);
  · 值本身若含双引号, 改用中文引号「」或去掉; 全部**单行**, 内部不换行;
  · 例: `reason: "卡点: 需主管7/25前拍板"` (对) / `reason: 卡点: 需...` (错, 裸冒号会被判成 key)
- 只依据周报文本 + 花名册岗位; anti-fabrication (SKILL.md §六); 不引用其他评委 review
- 自省不施压: C-/C 级如实给, 但每条建议必须具体可做
"""


def _phase_4_system_prompt(
    judge: str,
    judge_skill_md: str,
    scoring_spec: dict,
    scoring_lenses: list,
    anchor_judges,
    confidence_cap_fn: Callable[[str], tuple],
    output_format: Optional[str] = None,
) -> str:
    """
    评委 system prompt = SKILL.md + 强约束输出格式块。
    格式约束写在 system prompt 而非 user msg, 这样 prompt cache 命中率更高。

    output_format=study_weekly_v8: 走 v8 六段式专属契约 (frontmatter 带全套 v8 payload,
    不强塞 adversarial_view); 其余 (None / 通用) 走原通用块, 行为零变化。
    """
    category = "anchor" if judge in anchor_judges else "dimension"
    adversarial_clause = "" if category == "anchor" else """\
- `adversarial_view`: 三字段 dict (维度评委必填, smoke_e2e.verify 强制):
    `if_thesis_wrong`: 一句话, 本维度判断错了哪一环最脆弱
    `contrary_signal_observed`: 近期反向信号 (带 source link)
    `base_rate_warning`: 同类决策历史 base rate
"""
    # v0.6 R1.3: anchor research 未签收时注入 confidence 上限 (超出会被写盘后 clamp)
    cap_clause = ""
    if category == "anchor":
        cap, state = confidence_cap_fn(judge)
        if cap is not None:
            cap_clause = (
                f"- confidence ≤ {cap} — 你的 research doctrine 当前状态为 `{state}` "
                f"(未经锚点本人签收), 心证不得给高 confidence (PRD R1.3; 超出会被 clamp 并标注)\n"
            )

    scores_yaml, rubric_guidance, score_constraint = _scores_spec_block(
        scoring_spec, scoring_lenses, is_anchor=(category == "anchor"))
    rubric_section = f"\n{rubric_guidance}\n" if rubric_guidance else ""

    if output_format == "study_weekly_v8":
        return _phase_4_v8_system_prompt(
            judge, judge_skill_md, category, scores_yaml, rubric_section, score_constraint)

    return f"""{judge_skill_md}

──────────────────────────────────────────────────────────────
# Phase 4 输出强约束 (run_pipeline_local 加, 不可违反)
──────────────────────────────────────────────────────────────

你是 Phase 4 的评委 `{judge}` (category={category}). 你的 SKILL.md 已在上方。
现在收到 Phase 3 的 synthesis.md + Phase 1 的 context.md, 任务: 独立打分。
{rubric_section}
输出格式 (markdown):

```
---
judge: {judge}
judge_display_name: <人格化称呼>
judge_category: {category}
brand_slug: <slug>
case_id: <ID>
version: 1
reviewed_at: <ISO timestamp>
scores:
{scores_yaml}
confidence: <float 0-1>
{adversarial_clause}wiki_entities_referenced:
  - <_wiki/path/file.md>
---

## 一句话
<人格化金句, 体现你的视角>

## 关键缺口
<本议题在你视角下的最大缺口, 2-3 句>

## 行动建议
<{('1-3 条具体下一步' if category == 'dimension' else '可选, anchor 给 ≤ 1 条')}>
```

硬约束:
{score_constraint}
- {('adversarial_view 三字段都必须有非空值 (smoke_e2e.verify 会阻断 commit)' if category == 'dimension' else 'anchor 评委不写 adversarial_view 字段')}
{cap_clause}- 不要引用其他评委的 review (你读不到, 也不要假装读到)
- **先用你自己的镜头审, 不要被 synthesis 主叙事牵着走**: synthesis 会有一条最显眼的结论/张力, 但那是"大家都看得到"的。你被选进 panel 是因为你 SKILL.md 里那套**独特的 doctrine 与行业抓手** —— 先问"从我这套视角看, 这份方案有哪些**主叙事没覆盖到**的角度/缺口/风险?"(具体到你 doctrine 里的抓手, 不要泛泛), 把它写进「关键缺口」与金句; **回应主叙事只占次要篇幅**。金句必须是"只有你这个视角才说得出"的话, 而非任何评委都能说的通用结论。
- 不要给最终 panel 合议 — 那是 Phase 5 Lead 的活
"""


def _phase_5_system_prompt(
    scoring_spec: dict,
    anchor_judges,
    panel_judges: Optional[list[str]] = None,
) -> str:
    """Phase 5 Lead system prompt, 按 scoring_spec 模式选择措辞。

    sum_max_score 时避免 5 镜头 / anchor_delta 语言 (那是 weighted 模式的概念)。
    单评委 panel (≤1 维度评委, 如竞赛 workshop-ai-evaluator) 追加约束: 没有组间分歧,
    不要编造多个评委 (B 瑕疵修复)。"""
    base = (_PHASE_5_SYSTEM_PROMPT_SUM_MAX
            if scoring_spec.get("mode") == "sum_max_score"
            else _PHASE_5_SYSTEM_PROMPT_WEIGHTED)
    if panel_judges is not None:
        n_dim = sum(1 for j in panel_judges if j not in anchor_judges)
        if n_dim <= 1:
            base += ("\n\n⚠️ 本场景只有 1 位评委 — \"评委分歧高亮\"子段没有组间分歧可言。"
                     "该子段请写\"单评委评分, 无组间分歧\", 或改为该评委对各维度强弱的判断; "
                     "**绝不要编造\"其他评委\"\"部分评委\"或虚构分歧**。")
    return base


# Phase 3 合成 (Lead) system prompt — M2.0a 从 run_pipeline_local:1467 纯搬移。
# 档 A 服务化 (boss_prepare) 的 synthesis_task.system 用它; CLI 经 rpl re-export 照旧。
PHASE_3_SYSTEM_PROMPT = """\
你是锚点判断流水线 Phase 3 的 Lead。Phase 2 已派发 N 个 sub-agent 各自调研了
一个维度, 输出 raw_evidence/dim_*.md。你的任务: 把这些证据合成 synthesis.md,
给 Phase 4 的评委读。

synthesis.md 必含字段 (设计 note §4.4 + smoke_e2e fixture 对齐):

```
---
case_id: <ID>
brand_slug: <slug>
phase: 3
generated_at: <ISO>
research_dims: [dim1, dim2, ...]
failed_dims: [dimX, ...]    # 若 Phase 2 有失败
---

# Synthesis · <brand_slug>

## 执行摘要
3-5 句, 给评委的"一口气总览"。包含: 议题 / 核心张力 / 时间窗。

## 杠杆地图
列 3-5 个关键变量 (来自 raw_evidence): 名字 / 当前值 / flip 阈值 / weight。

## 脆弱边缘
最易证伪的 1-2 个假设。明确写"如果 X 发生, 本判断全盘垮"。

## 跨维度矛盾
若不同 dim 的证据互相打架, 在这列出。例: industry-trend 看好 Q3 时机,
但 internal 资源说团队 Q3 已超载。

## 引用索引
列每段证据的来源 dim_*.md + line range。
```

不要打分, 不要做评委的活。只合成。
"""
