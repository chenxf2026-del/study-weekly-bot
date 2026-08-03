"""
共享 pytest fixtures. pytest 自动发现本文件中的 fixture。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# vault 根
VAULT_ROOT = Path(__file__).parent.parent.resolve()
SCRIPTS_DIR = VAULT_ROOT / "scripts"

# 让 tests/ 能 import scripts/*.py
sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def _isolate_telemetry(tmp_path, monkeypatch):
    """遥测 DB 指到 tmp, 防测试经 review_queue 等 fail-open 埋点写真实 cases/.telemetry/。
    telemetry 测试自身显式传 path= 覆盖 env, 不受影响。"""
    monkeypatch.setenv("BOSS_TELEMETRY_DB", str(tmp_path / "telemetry-test.db"))


@pytest.fixture
def vault_root() -> Path:
    """vault 根路径"""
    return VAULT_ROOT


@pytest.fixture
def fixtures_dir() -> Path:
    """tests/fixtures/ 路径"""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    """临时 vault, 含基本目录结构"""
    for d in ("skills", "cases", "reports", "anchors/tian/raw/interviews",
              "_wiki", "scripts", "schemas"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def sample_skill_md() -> str:
    """合法的 SKILL.md 样例 (与 tests/fixtures/sample_skill.md 同步)"""
    return """---
name: sample-skill
description: |
  示例 Skill, 用于测试 skill_lint.py 的基本通过案例。
  本 Skill 不实现真实业务逻辑, 仅作 frontmatter 校验 fixture。
allowed-tools:
  - Read
  - Write
boss:
  schema_version: "1.0"
  skill_class: test-fixture
  sensitivity: public
---

# sample-skill

This is a fixture for testing.
"""


@pytest.fixture
def sample_case_json() -> dict:
    """合法的 case.json 样例"""
    return {
        "case_id": "C-2026-9999",
        "brand_slug": "test-fixture",
        "version": 1,
        "created_at": "2026-05-24T00:00:00+08:00",
        "owner": "项目主理",
        "sensitivity": "internal",
        "context": {
            "trigger_event": {
                "named_event": "示例触发事件 - 用于测试",
                "time": "2026-05-24",
                "source": "tests/fixtures/sample_case.json",
            },
            "stakeholders": ["项目主理", "SuperIntern"],
        },
        "thesis": {
            "central_claim": "这是测试用 thesis",
            "rationale": "fixture",
        },
        "evidence": [],
        "counter_arguments": [],
        "attribution": {
            "checkpoints": [
                {"horizon_days": 30, "metric": "test-metric", "threshold": "n/a"},
                {"horizon_days": 90, "metric": "test-metric", "threshold": "n/a"},
                {"horizon_days": 365, "metric": "test-metric", "threshold": "n/a"},
            ]
        },
        "handover": {
            "decision_owner": "test",
            "decision_deadline": "2026-12-31",
        },
    }


