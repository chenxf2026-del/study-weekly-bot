#!/usr/bin/env python3
"""
framework_compare.py — 90d 盲测自动比对 (v0.9 C2 · orchestrator SKILL Step 1.5 第 7/8 点 M2 落地)

比对 `cases/<id>/framework_<version>_prediction.md` (chmod 444 锁定预判, Phase 1.5 产)
vs 实际决策方向, 输出 §E 命中判定 (match) + §C 渲染块。

actual direction 来源优先级 (fail-safe, 全不可得 → match=None 待人工):
  1. CLI --actual-direction (人工权威输入)
  2. case.json decision.direction
  3. case.json framework_actual.direction
  4. reports/<brand>/report.md 正文 direction 正则 (最后兜底, 弱)

用法:
  python3 scripts/framework_compare.py --case-id C-2026-NNNN                 # 只看比对结果
  python3 scripts/framework_compare.py --case-id C-2026-NNNN --write         # 写 90d cp actual_signal
  python3 scripts/framework_compare.py --case-id C-2026-NNNN --actual-direction bet --write

集成点:
  - attribution_check.run_checkpoint: 90d cp + 有 lock → 自动比对 (代替 stub fetch)
  - run_pipeline_local Phase 5: 有 lock → report.md 追加 §C 段
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

VAULT_ROOT = Path(__file__).parent.parent.resolve()
CASES_DIR = VAULT_ROOT / "cases"
REPORTS_DIR = VAULT_ROOT / "reports"

VALID_DIRECTIONS = ("bet", "wait", "follow")
_DIRECTION_RE = re.compile(
    r"\*\*direction\*\*:\s*(bet|wait|follow|[\"“]?中段[^\n]*)", re.IGNORECASE)


def find_prediction_lock(case_dir: Path) -> Optional[Path]:
    """找 framework_<version>_prediction.md (v0.1 / v0.2 任一; 多个取版本号最大)。"""
    if not case_dir.is_dir():
        return None
    locks = sorted(case_dir.glob("framework_v*_prediction.md"))
    return locks[-1] if locks else None


def parse_prediction_lock(path: Path) -> Optional[dict]:
    """解析 lock 文件 → {version, direction, confidence, locked_at, file}。
    格式异常返回 None (盲测数据宁缺毋错)。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # v0.11 C3: 单一源 strict 解析 (空 frontmatter 的 lock 同样视为格式异常 → None,
    # 比原实现略严 — lock 格式固定, 该 edge 实际不可达)
    from _export_helpers import parse_frontmatter_strict
    fm = parse_frontmatter_strict(text)
    if not isinstance(fm, dict):
        return None
    m_dir = _DIRECTION_RE.search(text)
    direction = (m_dir.group(1).strip().lower() if m_dir else None)
    if direction not in VALID_DIRECTIONS:
        # "中段, 进 panel 仲裁" 等非三态 → 盲测口径按无预判处理
        direction = direction if direction in VALID_DIRECTIONS else None
    m_conf = re.search(r"\*\*confidence\*\*:\s*([0-9.]+)", text)
    if direction is None:
        return None
    return {
        "version": fm.get("framework_version", "v0.1"),
        "direction": direction,
        "confidence": float(m_conf.group(1)) if m_conf else None,
        "locked_at": str(fm.get("locked_at", "")),
        "file": str(path),
    }


def extract_actual_direction(case: dict, report_text: Optional[str] = None) -> Optional[str]:
    """按优先级提取实际决策方向 (不含 CLI override — 那在调用方)。"""
    for getter in (
        lambda: (case.get("decision") or {}).get("direction"),
        lambda: (case.get("framework_actual") or {}).get("direction"),
    ):
        v = getter()
        if isinstance(v, str) and v.lower() in VALID_DIRECTIONS:
            return v.lower()
    if report_text:
        # 兜底: report 正文找 "实际方向/actual direction/direction" 标注 (弱信号, 仅 markdown 字段行)
        m = re.search(r"(?:actual[_ ]?direction|实际方向|direction)\*{0,2}[:：]\s*\*{0,2}(bet|wait|follow)\b",
                      report_text, re.IGNORECASE)
        if m:
            return m.group(1).lower()
    return None


def compare_for_case(
    case: dict,
    case_dir: Path,
    reports_dir: Path = REPORTS_DIR,
    actual_override: Optional[str] = None,
) -> Optional[dict]:
    """主入口。返回 {version, prediction, pred_confidence, actual, match, lock_file} 或 None (无 lock)。
    match: True/False; actual 不可得时 actual=None, match=None (待人工)。"""
    lock_path = find_prediction_lock(case_dir)
    if lock_path is None:
        return None
    pred = parse_prediction_lock(lock_path)
    if pred is None:
        return None

    actual = actual_override.lower() if actual_override else None
    if actual is not None and actual not in VALID_DIRECTIONS:
        raise ValueError(f"actual_direction 必须是 {VALID_DIRECTIONS}, got {actual!r}")
    if actual is None:
        report_text = None
        report_path = reports_dir / case.get("brand_slug", "") / "report.md"
        if report_path.exists():
            report_text = report_path.read_text(encoding="utf-8", errors="replace")
        actual = extract_actual_direction(case, report_text)

    return {
        "version": pred["version"],
        "prediction": pred["direction"],
        "pred_confidence": pred["confidence"],
        "locked_at": pred["locked_at"],
        "lock_file": pred["file"],
        "actual": actual,
        "match": (pred["direction"] == actual) if actual else None,
    }


def render_actual_signal(result: dict) -> str:
    """90d cp actual_signal 文本 (orchestrator SKILL Step 1.5 第 8 点)。"""
    match_str = {True: "match=true", False: "match=false", None: "match=pending(待人工 --actual-direction)"}
    return (f"framework_{result['version']} blind-test: prediction={result['prediction']} "
            f"actual={result['actual'] or '?'} {match_str[result['match']]}")


def render_section_c(result: dict, case_id: str) -> str:
    """report.md §C 段 (orchestrator SKILL Step 1.5 第 7 点格式)。"""
    actual = result["actual"] or "待定 (90d 比对时 --actual-direction 提供)"
    hit = {True: "✅", False: "❌", None: "⏳"}[result["match"]]
    conf = result["pred_confidence"]
    lock_name = Path(result["lock_file"]).name
    return (
        f"\n## §C · Framework {result['version']} Prediction vs Actual\n\n"
        f"| 项目 | 锁定预判 (Phase 1.5) | 实际决策 (Phase 5) | 命中 |\n"
        f"|---|---|---|:---:|\n"
        f"| direction | {result['prediction']} | {actual} | {hit} |\n"
        f"| confidence | {conf if conf is not None else '—'} | (无需对齐) | |\n\n"
        f"预判文件: [{lock_name}](../../cases/{case_id}/{lock_name}) (chmod 444)\n"
    )


def render_section_c_for_case(case_id: str, cases_dir: Path = CASES_DIR,
                              reports_dir: Path = REPORTS_DIR,
                              case: Optional[dict] = None) -> Optional[str]:
    """Phase 5 集成入口: 有 lock 渲 §C, 无 lock 返回 None (零侵入)。"""
    case_dir = cases_dir / case_id
    if case is None:
        case_json = case_dir / "case.json"
        case = {}
        if case_json.exists():
            try:
                case = json.loads(case_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
    result = compare_for_case(case, case_dir, reports_dir)
    return render_section_c(result, case_id) if result else None


def main() -> int:
    ap = argparse.ArgumentParser(description="90d 盲测 framework prediction vs actual 比对 (v0.9 C2)")
    ap.add_argument("--case-id", required=True)
    ap.add_argument("--actual-direction", choices=VALID_DIRECTIONS,
                    help="人工提供实际方向 (优先级最高)")
    ap.add_argument("--write", action="store_true",
                    help="把比对结果写入 90d checkpoint actual_signal (demo case 豁免)")
    args = ap.parse_args()

    case_dir = CASES_DIR / args.case_id
    case_json = case_dir / "case.json"
    if not case_json.exists():
        print(f"❌ {case_json} 不存在", file=sys.stderr)
        return 2
    case = json.loads(case_json.read_text(encoding="utf-8"))

    result = compare_for_case(case, case_dir, actual_override=args.actual_direction)
    if result is None:
        print(f"本 case 无 framework_*_prediction.md lock (非盲测议题) — 无事可做")
        return 0

    print(f"version:    {result['version']}")
    print(f"prediction: {result['prediction']} (conf={result['pred_confidence']}, locked_at={result['locked_at']})")
    print(f"actual:     {result['actual'] or '未能提取 — 用 --actual-direction 提供'}")
    print(f"match:      {result['match']}")
    print(f"actual_signal: {render_actual_signal(result)}")

    if args.write:
        if "_demo_note" in case:
            print("demo case — 跳过写盘")
            return 0
        cps = case.get("attribution", {}).get("checkpoints", [])
        cp90 = next((c for c in cps if c.get("horizon_days") == 90), None)
        if cp90 is None:
            print("❌ 无 90d checkpoint", file=sys.stderr)
            return 2
        cp90["actual_signal"] = render_actual_signal(result)
        cp90["framework_compare"] = {
            "version": result["version"], "prediction": result["prediction"],
            "actual": result["actual"], "match": result["match"],
        }
        if result["match"] is not None:
            cp90["status"] = "confirmed" if result["match"] else "falsified"
        # 原子写: case.json 是 source-of-truth, 直接 write_text 若中途被打断会截断源文件.
        # 先写同目录临时文件再 os.replace (同一文件系统内原子 rename), 保证要么旧要么新.
        payload = json.dumps(case, ensure_ascii=False, indent=2)
        fd, tmp_path = tempfile.mkstemp(dir=str(case_json.parent), prefix=".case.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(case_json))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        print(f"✅ 已写 90d cp (status={cp90.get('status')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
