"""test_scene_loader.py — 多场景评委体系 scene 加载/校验 (PRD v1.1 · R1)"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import scene_loader as sl


# ─────────────────────── fixtures ───────────────────────

def _write_scene(root: Path, slug: str, data: dict) -> Path:
    d = root / "scenes" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "scene.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return d


VALID_SCENE = {
    "name": "op2-company",
    "display_name": "公司级 OP2 评审",
    "panel": "op2-company",
    "description": "公司级评审",
    "feishu": {
        "app_id_env": "LARK_APP_ID_OP2_COMPANY",
        "app_secret_env": "LARK_APP_SECRET_OP2_COMPANY",
        "bot_name": "OP2 公司级评审助手",
    },
    "access": {"whitelist_env": "OP2_COMPANY_WHITELIST", "quota_per_user_daily": 5},
    "report": {"brand_prefix": "op2-company", "anchor_judge": "tian"},
    "scene_type": "review",
}


# ─────────────────────── 合法载入 ───────────────────────

class TestLoadScene:
    def test_valid_scene_loads(self, tmp_path):
        _write_scene(tmp_path, "op2-company", VALID_SCENE)
        cfg = sl.load_scene("op2-company", root=tmp_path)
        assert cfg.name == "op2-company"
        assert cfg.display_name == "公司级 OP2 评审"
        assert cfg.panel == "op2-company"
        assert cfg.scene_type == "review"
        assert cfg.feishu.app_id_env == "LARK_APP_ID_OP2_COMPANY"
        assert cfg.access.quota_per_user_daily == 5
        assert cfg.report.anchor_judge == "tian"
        assert cfg.path == tmp_path / "scenes" / "op2-company"

    def test_defaults_applied(self, tmp_path):
        minimal = {"name": "minimal", "display_name": "最小", "panel": "default"}
        _write_scene(tmp_path, "minimal", minimal)
        cfg = sl.load_scene("minimal", root=tmp_path)
        assert cfg.feishu is None                      # 无 feishu 块 → 仅 CLI
        assert cfg.access.quota_per_user_daily == 3    # 默认配额
        assert cfg.report.anchor_judge == "tian"       # 默认锚点
        assert cfg.report.brand_prefix == "minimal"    # 缺省 = name
        assert cfg.scene_type == "review"

    def test_missing_scene_raises(self, tmp_path):
        with pytest.raises(sl.SceneError, match="不存在"):
            sl.load_scene("nope", root=tmp_path)


# ─────────────────────── 缺字段 / 非法值 ───────────────────────

class TestValidation:
    @pytest.mark.parametrize("drop", ["name", "display_name", "panel"])
    def test_missing_required_field(self, tmp_path, drop):
        data = {k: v for k, v in VALID_SCENE.items() if k != drop}
        _write_scene(tmp_path, "broken", data)
        with pytest.raises(sl.SceneError, match="缺必填字段"):
            sl.validate_scene(tmp_path / "scenes" / "broken")

    def test_bad_scene_type(self, tmp_path):
        data = dict(VALID_SCENE, scene_type="nonsense")
        _write_scene(tmp_path, "bad", data)
        with pytest.raises(sl.SceneError, match="非法 scene_type"):
            sl.validate_scene(tmp_path / "scenes" / "bad")

    def test_feishu_block_requires_both_envs(self, tmp_path):
        data = dict(VALID_SCENE)
        data["feishu"] = {"app_id_env": "X"}   # 缺 app_secret_env
        _write_scene(tmp_path, "halfbot", data)
        with pytest.raises(sl.SceneError, match="app_id_env / app_secret_env 必填"):
            sl.validate_scene(tmp_path / "scenes" / "halfbot")

    def test_competition_must_have_null_anchor(self, tmp_path):
        data = dict(VALID_SCENE, scene_type="competition")
        data["report"] = {"anchor_judge": "tian"}    # 竞赛却带锚点
        _write_scene(tmp_path, "ws", data)
        with pytest.raises(sl.SceneError, match="anchor_judge: null"):
            sl.validate_scene(tmp_path / "scenes" / "ws")

    def test_competition_null_anchor_ok(self, tmp_path):
        data = dict(VALID_SCENE, scene_type="competition")
        data["report"] = {"anchor_judge": None}
        _write_scene(tmp_path, "ws", data)
        cfg = sl.validate_scene(tmp_path / "scenes" / "ws")
        assert cfg.report.anchor_judge is None


# ─────────────────────── list_scenes ───────────────────────

class TestListScenes:
    def test_empty_when_no_scenes_dir(self, tmp_path):
        assert sl.list_scenes(root=tmp_path) == []

    def test_lists_valid_skips_invalid_and_template(self, tmp_path):
        _write_scene(tmp_path, "op2-company", VALID_SCENE)
        _write_scene(tmp_path, "op2-bg", dict(VALID_SCENE, name="op2-bg",
                                              display_name="BG", panel="op2-bg"))
        _write_scene(tmp_path, "_template", dict(VALID_SCENE, name="_template"))  # 跳过
        _write_scene(tmp_path, "broken", {"name": "broken"})                      # 解析失败跳过
        names = {s.name for s in sl.list_scenes(root=tmp_path)}
        assert names == {"op2-company", "op2-bg"}


# ─────────────────────── find_scene_by_app_id ───────────────────────

class TestFindByAppId:
    def test_route_by_app_id(self, tmp_path):
        _write_scene(tmp_path, "op2-company", VALID_SCENE)
        bg = dict(VALID_SCENE, name="op2-bg", display_name="BG", panel="op2-bg")
        bg["feishu"] = {"app_id_env": "LARK_APP_ID_OP2_BG",
                        "app_secret_env": "LARK_APP_SECRET_OP2_BG"}
        _write_scene(tmp_path, "op2-bg", bg)

        env = {
            "LARK_APP_ID_OP2_COMPANY": "cli_company",
            "LARK_APP_ID_OP2_BG": "cli_bg",
        }
        assert sl.find_scene_by_app_id("cli_bg", env, root=tmp_path).name == "op2-bg"
        assert sl.find_scene_by_app_id("cli_company", env, root=tmp_path).name == "op2-company"
        assert sl.find_scene_by_app_id("cli_unknown", env, root=tmp_path) is None

    def test_unset_env_not_matched(self, tmp_path):
        _write_scene(tmp_path, "op2-company", VALID_SCENE)
        # env 里没有该变量 → 不匹配 (空值不应误命中)
        assert sl.find_scene_by_app_id("", {}, root=tmp_path) is None


# ─────────────────────── 真实 example 模板自检 ───────────────────────

def test_repo_example_parses():
    """config/scene.yaml.example 自身应能被解析 (防模板漂移)。"""
    example = sl.VAULT_ROOT / "config" / "scene.yaml.example"
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    cfg = sl._parse_scene_dict(data, source=str(example))
    assert cfg.name == "op2-company"
    assert cfg.scene_type == "review"


class TestSceneLLMOverride:
    """per-scene 模型覆盖 (缺省=全局统一切换)。"""

    def test_no_llm_block_defaults_none(self, tmp_path):
        _write_scene(tmp_path, "s", VALID_SCENE)
        assert sl.load_scene("s", root=tmp_path).llm is None      # 缺省 = 全局

    def test_llm_profile_parsed(self, tmp_path):
        data = dict(VALID_SCENE, llm={"profile": "glm"})
        _write_scene(tmp_path, "s2", data)
        cfg = sl.load_scene("s2", root=tmp_path)
        assert cfg.llm.profile == "glm"
        assert cfg.llm.model_fast is None and cfg.llm.model_deep is None

    def test_llm_model_overrides_parsed(self, tmp_path):
        data = dict(VALID_SCENE, llm={"profile": "aigw", "model_deep": "wangsu/x"})
        _write_scene(tmp_path, "s3", data)
        cfg = sl.load_scene("s3", root=tmp_path)
        assert cfg.llm.profile == "aigw" and cfg.llm.model_deep == "wangsu/x"

    def test_llm_block_without_profile_raises(self, tmp_path):
        data = dict(VALID_SCENE, llm={"model_deep": "x"})     # 缺 profile
        _write_scene(tmp_path, "s4", data)
        with pytest.raises(sl.SceneError):
            sl.load_scene("s4", root=tmp_path)
