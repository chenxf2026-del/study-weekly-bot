#!/usr/bin/env python3
"""
scene_loader.py — 多场景评委体系 (Multi-Scene Panel) 场景加载器
(PRD: docs/internal/prd-multi-scene-v1.md · v1.1 · M1 框架层 R1)

Scene = panel 的上层容器 = 一个部署单元 = 一个飞书机器人入口。
共用同一 wiki 知识库 + 同一锚点心智模型, 但每个 scene 有独立的:
  评委编组 (panel) / 飞书 bot 绑定 / 白名单与配额 / 脱敏策略 / 报告品牌前缀。

目录约定:
  scenes/<slug>/scene.yaml   ← 场景配置 (本文件加载)
  scenes/<slug>/panel.yaml   ← 该场景 panel (panel_loader.py 加载, 可 extends default)
  scenes/<slug>/judges/      ← 场景专属虚拟评委 (可选)

CLI:
  python3 scripts/scene_loader.py list                       # 列出所有 scene
  python3 scripts/scene_loader.py validate scenes/op2-company  # 校验单个 scene
  python3 scripts/scene_loader.py show op2-company           # 打印解析后的 SceneConfig

向后兼容: scenes/ 目录不存在或为空时, list_scenes() 返回 []，
调用方 (feishu_ws_client) 回退到单 bot 模式。
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path
from typing import Any

import yaml

VAULT_ROOT = Path(__file__).parent.parent.resolve()
SCENES_DIR = VAULT_ROOT / "scenes"

# scene_type 合法值
# persona: 锚点数字分身 (对话式人格代理), 非评审/竞赛。本仓不含该场景, 仅保留类型常量。
VALID_SCENE_TYPES = {"review", "competition", "judgement", "persona"}

# scoring_mode 合法值 (panel 层校验时也用)
VALID_SCORING_MODES = {"weighted_average", "sum_max_score"}


class SceneError(ValueError):
    """scene.yaml 校验失败 (缺字段 / 非法值)。"""


@dataclasses.dataclass
class FeishuBinding:
    app_id_env: str
    app_secret_env: str
    bot_name: str = ""

    @property
    def configured(self) -> bool:
        """app_id_env 非空 = 此 scene 期望绑定一个 bot。"""
        return bool(self.app_id_env)


@dataclasses.dataclass
class SceneAccess:
    whitelist_env: str = ""
    quota_per_user_daily: int = 3
    require_group: bool = False
    trust_p2p: bool = False   # 单聊默认可信→T1 (persona): 前提是 bot 单聊可见性已被主理审批限制,
                              # "能单聊本 bot"即已过人工授权。群不受此影响 (仍靠 trusted_groups)。默认 False fail-close。
    allow_cloud_doc: bool = False  # 消息里的飞书云文档链接是否可直读评审 (feishu_docio, study-weekly M1)。
                                   # 默认 False fail-close; 需 app 开 docx/wiki readonly 权限的场景才开。
    group_open: bool = False       # 群内 @机器人 即放行白名单闸 (配额/日上限仍生效)。学习小组场景用:
                                   # "谁在群里谁就能用", 群成员即授权; 单聊仍走白名单。默认 False fail-close。
    auto_review_title_keyword: str = ""  # ★ 标题闸 (study-weekly: "周报")。空 = 关, 该场景无标题门槛。
                                   # ① 免 @ 触发: 群里分享标题含此词的飞书云文档 → 不用 @ 即自动评审
                                   #    (依赖 bot 能收群里非 @ 消息 im:message.group_msg)。
                                   # ② 全路径硬门槛: 云文档 / 直接发的 Word·PDF / 引用的文件, 标题不含此词一律不评。
    title_gate_notify: bool = True   # 被标题闸拦下时, **单聊**是否回一张「未进入评审」卡。
                                   # ⚠ **群里恒静默, 与本开关无关** (主理 2026-07-27 实测拍板):
                                   # 群里成员常分享各类文档, 每份弹拒绝卡极吵; 且 group_open 场景发文件
                                   # 不需要 @, 所谓"显式提交"实际覆盖群内全部文件。
                                   # 单聊默认提示: 一对一低流量, 对方明确冲机器人来的, 没反应会让人以为
                                   # 机器人坏了。设 false 可把单聊也静音。


@dataclasses.dataclass
class SceneReport:
    brand_prefix: str = ""
    anchor_judge: str | None = "tian"   # null/None = 无锚点 (Workshop 竞赛模式)
    redact_review: bool = False
    show_scores_publicly: bool = False
    trusted_groups: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class SceneLLM:
    """per-scene 模型覆盖 (可选)。缺省 = 沿用全局 .env BOSS_LLM_* (统一切换)。

    profile: 引用 config/llm_profiles.yaml 的网关名 (glm/gpt/claude/aigw…);
    model_fast/model_deep: 在该 profile 端点上覆盖默认模型 (可空 = 用 profile 默认)。"""
    profile: str
    model_fast: str | None = None
    model_deep: str | None = None


@dataclasses.dataclass
class SceneConfig:
    name: str
    display_name: str
    panel: str                          # scene 自带 panel slug (相对 scene 目录) 或 panels/ 下的 name
    slug: str                           # 目录名 (= name, 但显式保留)
    description: str = ""
    feishu: FeishuBinding | None = None
    access: SceneAccess = dataclasses.field(default_factory=SceneAccess)
    report: SceneReport = dataclasses.field(default_factory=SceneReport)
    scene_type: str = "review"
    llm: SceneLLM | None = None         # per-scene 模型覆盖 (缺省=全局统一)
    path: Path | None = None            # scene.yaml 所在目录 (运行期填)

    @property
    def panel_path(self) -> Path | None:
        """解析 panel 字段为实际 yaml 路径。

        优先 scenes/<slug>/panel.yaml (scene 自带), 回退 panels/<panel>.yaml。
        """
        if self.path is not None:
            local = self.path / "panel.yaml"
            if local.is_file():
                return local
        candidate = VAULT_ROOT / "panels" / f"{self.panel}.yaml"
        return candidate if candidate.is_file() else None


def _parse_scene_dict(data: dict[str, Any], *, source: str) -> SceneConfig:
    """把 scene.yaml 解析成 SceneConfig, 校验必填字段与合法值。"""
    if not isinstance(data, dict):
        raise SceneError(f"{source}: scene.yaml 顶层必须是 mapping, 实际 {type(data).__name__}")

    # ─── 必填字段 ───
    missing = [k for k in ("name", "display_name", "panel") if not data.get(k)]
    if missing:
        raise SceneError(f"{source}: scene.yaml 缺必填字段: {missing}")

    scene_type = data.get("scene_type", "review")
    if scene_type not in VALID_SCENE_TYPES:
        raise SceneError(
            f"{source}: 非法 scene_type={scene_type!r}, 合法值: {sorted(VALID_SCENE_TYPES)}")

    # ─── feishu 绑定 (可选; 缺则此 scene 不接 bot, 仅 CLI 可用) ───
    feishu = None
    fconf = data.get("feishu")
    if isinstance(fconf, dict):
        if not fconf.get("app_id_env") or not fconf.get("app_secret_env"):
            raise SceneError(
                f"{source}: feishu 块存在时 app_id_env / app_secret_env 必填 "
                f"(从环境变量读凭证, 不明文入 git)")
        feishu = FeishuBinding(
            app_id_env=fconf["app_id_env"],
            app_secret_env=fconf["app_secret_env"],
            bot_name=fconf.get("bot_name", ""),
        )

    # ─── access (有默认值) ───
    aconf = data.get("access") or {}
    access = SceneAccess(
        whitelist_env=aconf.get("whitelist_env", ""),
        quota_per_user_daily=int(aconf.get("quota_per_user_daily", 3)),
        require_group=bool(aconf.get("require_group", False)),
        trust_p2p=bool(aconf.get("trust_p2p", False)),
        allow_cloud_doc=bool(aconf.get("allow_cloud_doc", False)),
        group_open=bool(aconf.get("group_open", False)),
        auto_review_title_keyword=str(aconf.get("auto_review_title_keyword", "") or ""),
        title_gate_notify=bool(aconf.get("title_gate_notify", True)),
    )

    # ─── report (有默认值) ───
    rconf = data.get("report") or {}
    # anchor_judge: 显式 null → None (无锚点); 缺省 → "tian"
    anchor_judge = rconf["anchor_judge"] if "anchor_judge" in rconf else "tian"
    report = SceneReport(
        brand_prefix=rconf.get("brand_prefix", data["name"]),
        anchor_judge=anchor_judge,
        redact_review=bool(rconf.get("redact_review", False)),
        show_scores_publicly=bool(rconf.get("show_scores_publicly", False)),
        trusted_groups=list(rconf.get("trusted_groups", []) or []),
    )

    # competition 场景强约束: 无锚点
    if scene_type == "competition" and report.anchor_judge is not None:
        raise SceneError(
            f"{source}: scene_type=competition 必须 report.anchor_judge: null "
            f"(竞赛是方案横向比拼, 不是锚点个人决策)")

    # ─── llm 覆盖 (可选; 缺省=全局统一切换) ───
    llm = None
    lconf = data.get("llm")
    if isinstance(lconf, dict):
        prof = str(lconf.get("profile") or "").strip()
        if not prof:
            raise SceneError(f"{source}: llm 块存在时 profile 必填 (引用 config/llm_profiles.yaml 的网关名)")
        llm = SceneLLM(
            profile=prof,
            model_fast=(lconf.get("model_fast") or None),
            model_deep=(lconf.get("model_deep") or None),
        )

    return SceneConfig(
        name=data["name"],
        display_name=data["display_name"],
        panel=data["panel"],
        slug=data["name"],
        description=data.get("description", ""),
        feishu=feishu,
        access=access,
        report=report,
        scene_type=scene_type,
        llm=llm,
    )


def load_scene(slug: str, *, root: Path | None = None) -> SceneConfig:
    """按 slug 加载 scenes/<slug>/scene.yaml。"""
    base = (root or VAULT_ROOT) / "scenes" / slug
    scene_yaml = base / "scene.yaml"
    if not scene_yaml.is_file():
        raise SceneError(f"scene 不存在: {scene_yaml}")
    data = yaml.safe_load(scene_yaml.read_text(encoding="utf-8"))
    cfg = _parse_scene_dict(data, source=str(scene_yaml))
    cfg.path = base
    return cfg


def validate_scene(path: Path | str, *, root: Path | None = None) -> SceneConfig:
    """校验一个 scene 目录或 scene.yaml 文件, 返回解析结果 (不通过则抛 SceneError)。"""
    p = Path(path)
    scene_yaml = p / "scene.yaml" if p.is_dir() else p
    if not scene_yaml.is_file():
        raise SceneError(f"找不到 scene.yaml: {scene_yaml}")
    data = yaml.safe_load(scene_yaml.read_text(encoding="utf-8"))
    cfg = _parse_scene_dict(data, source=str(scene_yaml))
    cfg.path = scene_yaml.parent
    return cfg


def list_scenes(*, root: Path | None = None) -> list[SceneConfig]:
    """扫 scenes/*/scene.yaml, 返回所有可解析的 scene (跳过 _template / 隐藏 / 解析失败)。

    向后兼容: scenes/ 不存在时返回 []。
    """
    scenes_dir = (root or VAULT_ROOT) / "scenes"
    if not scenes_dir.is_dir():
        return []
    out: list[SceneConfig] = []
    for child in sorted(scenes_dir.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        scene_yaml = child / "scene.yaml"
        if not scene_yaml.is_file():
            continue
        try:
            cfg = validate_scene(child, root=root)
        except (SceneError, yaml.YAMLError, ValueError) as e:
            # 单个坏 scene (畸形 YAML / 非 int quota 等) 只跳过它, 不拖垮整个枚举
            # (feishu 路由 / overview 依赖此函数; docstring 承诺"跳过解析失败")
            print(f"[scene_loader] 跳过无法解析的 scene {child.name}: {type(e).__name__}: {e}",
                  file=sys.stderr)
            continue
        out.append(cfg)
    return out


def find_scene_by_app_id(app_id: str, env: dict[str, str], *,
                         root: Path | None = None) -> SceneConfig | None:
    """按运行期 app_id (从飞书事件取) 反查所属 scene。

    env: os.environ 的快照 (或测试注入的 dict)。逐 scene 解析其 feishu.app_id_env
    在 env 中的实际值, 与传入 app_id 比对。无匹配返回 None (调用方 drop 该事件)。
    """
    for cfg in list_scenes(root=root):
        if cfg.feishu and cfg.feishu.configured:
            actual = env.get(cfg.feishu.app_id_env, "")
            if actual and actual == app_id:
                return cfg
    return None


# ─────────────────────────── CLI ───────────────────────────

def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="多场景评委体系 scene 加载/校验")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出所有 scene")
    p_val = sub.add_parser("validate", help="校验一个 scene 目录/文件")
    p_val.add_argument("path", help="scenes/<slug> 目录或 scene.yaml 路径")
    p_show = sub.add_parser("show", help="打印解析后的 SceneConfig")
    p_show.add_argument("slug", help="scene slug")

    args = ap.parse_args(argv)

    if args.cmd == "list":
        scenes = list_scenes()
        if not scenes:
            print("(无 scene; scenes/ 不存在或为空 → 单 bot 回退模式)")
            return 0
        for s in scenes:
            bot = s.feishu.app_id_env if s.feishu else "—"
            print(f"  {s.name:24} type={s.scene_type:11} panel={s.panel:16} bot_env={bot}")
        return 0

    if args.cmd == "validate":
        try:
            cfg = validate_scene(args.path)
        except SceneError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            return 1
        print(f"OK: {cfg.name} (type={cfg.scene_type}, panel={cfg.panel}, "
              f"anchor={cfg.report.anchor_judge})")
        return 0

    if args.cmd == "show":
        try:
            cfg = load_scene(args.slug)
        except SceneError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            return 1
        d = dataclasses.asdict(cfg)
        d.pop("path", None)
        print(yaml.safe_dump(d, allow_unicode=True, sort_keys=False))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
