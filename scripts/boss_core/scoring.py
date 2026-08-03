"""boss_core.scoring — Phase 5 确定性打分聚合 (M0.3 · T3, 从 run_pipeline_local 抽库)。

读 reviews_dir 下各评委 review frontmatter 的 scores, 确定性聚合 (sum_max / 5 镜头均 /
anchor_delta), 无网络 / 无 LLM。按 §6 R-a **不搬可变全局**: scoring_spec / scoring_lenses /
anchor_judges 显式收参; review frontmatter 解析器 (_parse_review_frontmatter, 走
_export_helpers) 按 DI 注入 (parse_fm 回调), 让本模块无全局态、无对 rpl 的反向依赖。

固定阈值常量 (ANCHOR_DELTA_THRESHOLD 等, 非运行时可变) 随本模块一起下沉, run_pipeline_local
顶部 re-export。同名薄 wrapper 在 rpl 注入当前可变全局, 保对外签名与聚合输出逐字不变
(test_op2_scoring / test_found_bugs)。M2 boss-service 可传 per-tenant scoring_spec 直调。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

# 聚合阈值 (固定常量, 非运行时可变; 原 run_pipeline_local:2356-2361 下沉)
ANCHOR_DELTA_THRESHOLD = 2.0   # panels/default.yaml merge_rules.anchor_delta_threshold
META_TOPIC_TYPES_FOR_DUAL_SCALE = {"meta_framework", "cross_domain"}
ANCHOR_DUAL_SCALE_DELTA_THRESHOLD = 0.15   # > 0.15 标 anchor 元层 vs 单点层心证显著分化


def _is_competition_scoring(scoring_spec: dict) -> bool:
    """scoring_spec 是否竞赛口径 (sum_max + 无 anchor) — 触发 competition_summary 写盘。"""
    return scoring_spec.get("mode") == "sum_max_score" and not scoring_spec.get("has_anchor")


def _grade_for_total(scoring_spec: dict, total_mean: float) -> Optional[str]:
    """按 scoring_spec.score_threshold + total_max 把维度总分均值映射成等级。

    competition (无 score_threshold) → None (仅排名, 无门槛)。
    review (op2-company 等): pct = total/total_max*100, < rewrite → 重写 / < revise → 修改 / 否则 人工评审。"""
    thr = scoring_spec.get("score_threshold")
    total_max = scoring_spec.get("total_max", 0)
    if not thr or total_max <= 0:
        return None
    rewrite = float(thr.get("rewrite", 60))
    revise = float(thr.get("revise", 80))
    pct = total_mean / total_max * 100
    if pct < rewrite:
        return "重写"
    if pct < revise:
        return "修改"
    return "人工评审"


def _compute_panel_summary_sum_max(
    reviews_dir: Path,
    panel_judges: list[str],
    log,
    *,
    scoring_spec: dict,
    scoring_lenses: list,
    anchor_judges,
    parse_fm: Callable[[str], Optional[dict]],
) -> dict[str, Any]:
    """sum_max_score 聚合: 各评委按 panel 自定义维度打分, 满分相加。

    返回 (带 scoring_mode='sum_max_score' 标记, report_builder 据此分支渲染):
      - total_max: 维度满分之和 (op2=100, workshop=10)
      - dimension_total_mean: 维度评委总分均值 (含 failed 占位的 0 分)
      - lens_means: {维度 slug: 维度评委均分}
      - anchor_5lens_mean: 锚点 5 镜头心证均分 (1-10; 锚点用自己的心智模型,
        不读维度 doctrine — CLAUDE.md §4.5.3); 无锚点 (竞赛) 为 None
      - grade: 等级 (review 走门槛; competition None)
      - grade_explanation: 一句话说明 (不评判)

    注意: 锚点与维度评委用**不同尺子** (5 镜头 1-10 vs 维度满分制), 因此不算
    anchor_delta — 锚点心证单列作独立参照, 不与维度总分跨尺度相减。
    """
    lens_slugs = scoring_spec["lens_slugs"]
    total_max = scoring_spec["total_max"]

    dim_totals: list[float] = []
    lens_accum: dict[str, list[float]] = {s: [] for s in lens_slugs}
    anchor_5lens_mean: Optional[float] = None
    # 无任何可用分数的**维度**评委 (缺文件 / frontmatter 解析失败 / scores 全空):
    # 从均值里排除 (技术失败不该按 0 拖分), 但显式记录 + WARN, 让缺口在报告里留痕,
    # 不再像以前那样静默丢一位评委、悄悄拉低/改变等级 (C-2026-0074 zouxu 教训)。
    no_score_judges: list[str] = []

    def _mark_no_score(judge: str, reason: str) -> None:
        if judge not in anchor_judges and judge not in no_score_judges:
            no_score_judges.append(judge)
        log.warn(f"评委 {judge} 未计入维度总分 ({reason})")

    for judge in panel_judges:
        review_path = reviews_dir / f"{judge}.md"
        if not review_path.exists():
            _mark_no_score(judge, f"review 缺失 {review_path.name}, Phase 4 应已写占位")
            continue
        fm = parse_fm(review_path.read_text(encoding="utf-8"))
        if fm is None:
            _mark_no_score(judge, f"{review_path.name} frontmatter 解析失败 (fix_review_yaml 未能修复)")
            continue
        # 技术失败占位 (Phase 4 sub-agent 超时/重试耗尽): 排除, 不按占位 0 分拖低维度总分/grade。
        # 占位写的是 <slug>: 0 全镜头, numeric 非空绕过下方"无数值"守卫, 故须显式按 status 排除。
        if fm.get("status") == "failed":
            _mark_no_score(judge, f"{review_path.name} status=failed (Phase 4 占位, 非真实打分)")
            continue
        scores = fm.get("scores") or {}
        if judge in anchor_judges:
            # 锚点打 5 镜头 (1-10) — 取其 5 镜头均分作独立心证参照
            five = [scores.get(k) for k in scoring_lenses if isinstance(scores.get(k), (int, float))]
            if five:
                anchor_5lens_mean = round(sum(five) / len(five), 2)
            else:
                log.warn(f"{review_path.name} (anchor) 无 5 镜头分数, anchor_5lens_mean 留空")
        else:
            # 维度评委: 缺维度按 0 计 (模型漏个别维度偏保守, 不虚高总分)。
            # 但**整份无任何数值分** = 技术失败 (非"打了 0 分"), 从均值里排除 + 记录, 不按 0 拖分。
            vals = {s: scores.get(s) for s in lens_slugs}
            numeric = [float(v) for v in vals.values() if isinstance(v, (int, float))]
            if not numeric:
                _mark_no_score(judge, f"{review_path.name} scores 为空/无数值")
                continue
            judge_total = sum(numeric)
            dim_totals.append(judge_total)
            for s in lens_slugs:
                v = vals[s]
                if isinstance(v, (int, float)):
                    lens_accum[s].append(float(v))

    dim_total_mean = round(sum(dim_totals) / len(dim_totals), 2) if dim_totals else 0.0
    lens_means = {s: round(sum(v) / len(v), 2) for s, v in lens_accum.items() if v}
    grade = _grade_for_total(scoring_spec, dim_total_mean)

    if not scoring_spec.get("has_anchor"):
        grade_explanation = (
            f"竞赛总分 {dim_total_mean}/{total_max} (满分相加, 仅用于排名, 无门槛分级)"
        )
    else:
        grade_explanation = (
            f"维度评委总分均值 {dim_total_mean}/{total_max}"
            + (f" · 等级「{grade}」" if grade else "")
            + (f" · 锚点心证 {anchor_5lens_mean}/10 (5 镜头, 独立尺子)"
               if anchor_5lens_mean is not None else "")
            + (f" · ⚠ {len(no_score_judges)} 位评委未出分未计入" if no_score_judges else "")
        )

    if no_score_judges:
        log.warn(f"⚠ 维度总分仅按 {len(dim_totals)} 位评委算, 未出分: {', '.join(no_score_judges)}")

    out = {
        "scoring_mode": "sum_max_score",
        "total_max": total_max,
        "dimension_total_mean": dim_total_mean,
        "lens_means": lens_means,
        "anchor_5lens_mean": anchor_5lens_mean,
        "grade": grade,
        "grade_explanation": grade_explanation,
    }
    if no_score_judges:
        out["judges_no_score"] = no_score_judges   # 显式留痕: 哪些维度评委未出分
    return out


def _compute_panel_summary(
    reviews_dir: Path,
    panel_judges: list[str],
    log,
    topic_type: str = "unknown",
    *,
    scoring_spec: dict,
    scoring_lenses: list,
    anchor_judges,
    parse_fm: Callable[[str], Optional[dict]],
) -> dict[str, Any]:
    """
    读各 review frontmatter 的 scores, 计算:
      - dimension_weighted_mean: 维度评委 5 镜头均分 (含 status=failed 占位的 1 分)
      - anchor_tian_mean: tian.md 5 镜头均分
      - anchor_delta: dim_weighted_mean - anchor_tian_mean (正数 = 维度乐观)
      - delta_high: |delta| > ANCHOR_DELTA_THRESHOLD
      - delta_explanation: 一句话描述方向 (不评判)

    P2.4 (Case 6 §confidence_cap_protest): 元层议题 (meta_framework / cross_domain)
    额外读 anchor frontmatter 的 confidence_meta_layer + confidence_single_point_layer,
    输出 anchor_dual_scale_delta. 单点议题 backward compat, 不输出新字段.
    详见 schemas/framework-v0.2-spec.yaml §anchor_confidence_dual_scale.

    sum_max_score 模式 (op2-company / workshop-midyear 等): 改走 _compute_panel_summary_sum_max,
    用 panel 自定义维度满分相加, 出总分 + 等级 (review) / 仅总分 (competition 无 anchor)。
    """
    if scoring_spec.get("mode") == "sum_max_score":
        return _compute_panel_summary_sum_max(
            reviews_dir, panel_judges, log,
            scoring_spec=scoring_spec, scoring_lenses=scoring_lenses,
            anchor_judges=anchor_judges, parse_fm=parse_fm)

    import yaml

    dim_means: list[float] = []
    anchor_mean: float = 0.0
    anchor_found = False
    anchor_fm: Optional[dict] = None  # P2.4: 保留 anchor frontmatter 给 dual_scale 用

    # 与 sum_max 路径一致: 无有效分/技术失败的维度评委记录下来, 从加权均分排除 + 在报告留痕,
    # 而非静默 continue 后悄悄用幸存者重算 dimension_weighted_mean / anchor_delta。
    no_score_judges: list[str] = []

    def _mark_no_score(judge: str, reason: str) -> None:
        if judge not in anchor_judges and judge not in no_score_judges:
            no_score_judges.append(judge)
        log.warn(f"评委 {judge} 未计入维度加权均分 ({reason})")

    for judge in panel_judges:
        review_path = reviews_dir / f"{judge}.md"
        if not review_path.exists():
            _mark_no_score(judge, f"review 缺失 {review_path.name}, Phase 4 应已写占位")
            continue
        text = review_path.read_text(encoding="utf-8")
        fm = parse_fm(text)
        if fm is None:
            _mark_no_score(judge, f"{review_path.name} frontmatter 解析失败")
            continue
        # 技术失败占位排除 (weighted 占位写全镜头 1 分, 否则 review_mean=1.0 拖低加权均分/污染 anchor)
        if fm.get("status") == "failed":
            _mark_no_score(judge, f"{review_path.name} status=failed (Phase 4 占位, 非真实打分)")
            continue
        scores = fm.get("scores") or {}
        if not scores:
            _mark_no_score(judge, f"{review_path.name} 无 scores 字段")
            continue
        # 5 镜头均分; 缺镜头跳过
        lens_vals = [scores.get(k) for k in scoring_lenses if isinstance(scores.get(k), (int, float))]
        if not lens_vals:
            _mark_no_score(judge, f"{review_path.name} 无数值镜头分")
            continue
        review_mean = sum(lens_vals) / len(lens_vals)
        if judge in anchor_judges:
            anchor_mean = review_mean
            anchor_found = True
            anchor_fm = fm
        else:
            dim_means.append(review_mean)

    dim_weighted = (sum(dim_means) / len(dim_means)) if dim_means else 0.0
    delta = dim_weighted - anchor_mean if anchor_found else 0.0
    delta_high = abs(delta) > ANCHOR_DELTA_THRESHOLD

    # NOTE: 字符串避开 "锚点" / "<anchor-real-name>" / "anchor raw" 等被 redact_check.py 拦截的真名,
    # 用 "anchor" 等抽象口径. 真名只允许出现在 raw/ 与 SKILL.md 的方法论描述。
    # (P1 缺口 #1 修, dev-plan v2.10)
    if not anchor_found:
        explanation = "anchor 视角 review 未找到, anchor_delta 设 0 (信号无效)"
    elif delta_high:
        direction = "维度比 anchor 乐观" if delta > 0 else "anchor 比维度乐观"
        explanation = f"⚠ |Δ|={abs(delta):.2f} > {ANCHOR_DELTA_THRESHOLD}, {direction} — 飞书卡片应高亮"
    else:
        explanation = f"|Δ|={abs(delta):.2f} ≤ {ANCHOR_DELTA_THRESHOLD}, 维度与 anchor 判断方向一致"

    if no_score_judges:
        explanation += f" · ⚠ {len(no_score_judges)} 位评委未出分未计入"

    result: dict[str, Any] = {
        "scoring_mode": "weighted_average",
        "dimension_weighted_mean": round(dim_weighted, 2),
        "anchor_tian_mean": round(anchor_mean, 2),
        "anchor_delta": round(delta, 2),
        "delta_high": delta_high,
        "delta_explanation": explanation,
    }
    if no_score_judges:
        result["judges_no_score"] = no_score_judges

    # P2.4 · anchor confidence dual scale (元层议题专属)
    # 应用范围 schemas/framework-v0.2-spec.yaml §anchor_confidence_dual_scale.applies_to_topic_types
    if topic_type in META_TOPIC_TYPES_FOR_DUAL_SCALE and anchor_found and anchor_fm is not None:
        meta_val = anchor_fm.get("confidence_meta_layer")
        sp_val = anchor_fm.get("confidence_single_point_layer")
        meta_ok = isinstance(meta_val, (int, float)) and 0 <= meta_val <= 1
        sp_ok = isinstance(sp_val, (int, float)) and 0 <= sp_val <= 1
        if meta_ok and sp_ok:
            dual_delta = round(float(meta_val) - float(sp_val), 2)
            result["anchor_tian_meta_mean"] = round(float(meta_val), 2)
            result["anchor_tian_single_point_mean"] = round(float(sp_val), 2)
            result["anchor_dual_scale_delta"] = dual_delta
            # 提示 dual_scale 信号方向 (不评判)
            if abs(dual_delta) > ANCHOR_DUAL_SCALE_DELTA_THRESHOLD:
                result["anchor_dual_scale_explanation"] = (
                    f"⚠ |Δ_dual|={abs(dual_delta):.2f} > {ANCHOR_DUAL_SCALE_DELTA_THRESHOLD}, "
                    f"anchor 元层 vs 单点层心证显著分化 — 元框架使用场景成立"
                )
            else:
                result["anchor_dual_scale_explanation"] = (
                    f"|Δ_dual|={abs(dual_delta):.2f} ≤ {ANCHOR_DUAL_SCALE_DELTA_THRESHOLD}, "
                    f"元层 vs 单点层心证方向一致"
                )
        else:
            log.warn(
                f"topic_type={topic_type} (元层议题) 但 anchor frontmatter 缺 dual_scale 字段 "
                f"(confidence_meta_layer / confidence_single_point_layer). "
                f"backward compat: 仅渲染单一 anchor_tian_mean. "
                f"详见 schemas/framework-v0.2-spec.yaml §anchor_confidence_dual_scale."
            )

    return result


def _format_panel_summary_dual_scale_yaml(panel_summary: dict, indent: str = "  ") -> str:
    """P2.4: 元层议题 (meta_framework / cross_domain) 在 panel_summary 段渲染
    anchor confidence 双 scale 字段. 单点议题返回空串 (backward compat).

    schemas/framework-v0.2-spec.yaml §anchor_confidence_dual_scale.panel_summary_render.
    """
    if "anchor_dual_scale_delta" not in panel_summary:
        return ""
    lines = [
        f"{indent}anchor_tian_meta_mean: {panel_summary['anchor_tian_meta_mean']}",
        f"{indent}anchor_tian_single_point_mean: {panel_summary['anchor_tian_single_point_mean']}",
        f"{indent}anchor_dual_scale_delta: {panel_summary['anchor_dual_scale_delta']}",
    ]
    if "anchor_dual_scale_explanation" in panel_summary:
        # YAML 单引号 escape: 内部 ' → ''
        exp = str(panel_summary["anchor_dual_scale_explanation"]).replace("'", "''")
        lines.append(f"{indent}anchor_dual_scale_explanation: '{exp}'")
    return "\n" + "\n".join(lines)


def derive_scoring_spec(panel_resolved: dict, *, default_lens_slugs: list) -> dict[str, Any]:
    """panel (panel_loader 解析后 dict) → scoring spec dict。**纯函数, 不碰任何全局**
    (M2.0b 从 refresh_scoring_spec 推导体抽出; rpl wrapper 保留刷新模块全局语义, R-a)。

    weighted_average (默认): 5 镜头 1-10, 行为与历史完全一致。
    sum_max_score: 用 panel scoring_lenses (各含 max_score) 作打分维度, 满分相加。
    panel 无 override / lenses 无效 → 兜底 weighted。M2 服务侧 per-run 直调本函数。
    """
    spec: dict[str, Any] = {
        "mode": "weighted_average",
        "lens_slugs": list(default_lens_slugs),
        "lenses": [],
        "total_max": 0,
        "score_threshold": None,
        "has_anchor": True,
    }
    mode = panel_resolved.get("scoring_mode", "weighted_average")
    if mode == "sum_max_score":
        lenses_raw = panel_resolved.get("scoring_lenses") or []
        lenses = [
            {
                "slug": l["slug"],
                "display_name_cn": l.get("display_name_cn", l["slug"]),
                "max_score": int(l["max_score"]),
                "description": str(l.get("description", "")).strip(),
            }
            for l in lenses_raw
            if isinstance(l, dict) and l.get("slug") and l.get("max_score")
        ]
        if lenses:
            spec = {
                "mode": "sum_max_score",
                "lens_slugs": [l["slug"] for l in lenses],
                "lenses": lenses,
                "total_max": sum(l["max_score"] for l in lenses),
                "score_threshold": panel_resolved.get("score_threshold"),
                "has_anchor": panel_resolved.get("anchor_judge") is not None,
            }
    return spec


# 5 镜头 slug (CLAUDE.md §5.1 固定打分项)。服务侧 (boss_service, 禁 import rpl) 的
# derive_scoring_spec / 聚合调用用此常量; 与 run_pipeline_local.SCORING_LENSES 的
# 一致性由 test_run_pipeline_local_public_surface 的同步断言锁死 (M2.2)。
DEFAULT_LENS_SLUGS: list[str] = [
    "reasoning_soundness",
    "evidence_thesis_coupling",
    "counter_position_treatment",
    "falsifiability",
    "real_world_resilience",
]
