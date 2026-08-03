"""剥离冒烟 —— 这个仓从 boss-vault 抽出来后, **还是不是一个能跑的完整机器人**。

单测覆盖的是「功能对不对」; 本文件覆盖的是「剥离这件事有没有剥漏 / 剥错」:

  1. 依赖闭包完整   —— 每个入口都能 import, 没有留下指向未带过来的模块的 import
  2. 场景唯一       —— 只有 study-weekly 一个场景 (别的场景的配置不该跟过来)
  3. panel 可解析   —— extends 链走得通, 评委与镜头是预期的那套
  4. 机密边界       —— 上游的他方真名 / 客户名 / 尽调材料一个都没跟过来 (★ 授权边界)
  5. 凭据零入库     —— 没有任何真实密钥被提交
  6. 资产齐全       —— 运行时真正会读的文件都在

第 4/5 条是**授权边界的可执行版本**: 本仓获授权带走的是「框架 + 评委 doctrine +
学习小组成员数据」, 其余一律不带。它们红了就说明剥离漏了东西, 不要放宽断言 —— 去删内容。
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"

# 常驻/定时跑起来的入口 —— 任何一个 import 不了, 部署当场就废
ENTRYPOINTS = [
    "feishu_ws_client.py",      # 长连接收事件
    "review_worker.py",         # 队列消费 + 跑流水线
    "gen_study_weekly_summary.py",  # 周汇总 (timer)
    "feishu_events.py",         # 事件路由
    "feishu_notify.py",         # 回推 + 出站闸
    "study_weekly_output.py",   # 六段式渲染
    "run_pipeline_local.py",    # 评审流水线本体
    "redact_check.py",
    "scene_loader.py",
    "panel_loader.py",
]


class TestClosure:
    """1 · 依赖闭包 —— 剥离最容易出的错就是漏带某个被 import 的模块。"""

    @pytest.mark.parametrize("entry", ENTRYPOINTS)
    def test_entrypoint_imports(self, entry):
        mod = entry[:-3]
        r = subprocess.run([sys.executable, "-c", f"import {mod}"],
                           cwd=SCRIPTS, capture_output=True, text=True)
        assert r.returncode == 0, f"{entry} import 失败:\n{r.stderr}"

    def test_no_import_of_missing_local_module(self):
        """顶层 import 的本地模块必须都在仓里 —— 抓「删了文件但没删 import」。"""
        local = {p.stem for p in SCRIPTS.glob("*.py")}
        local |= {f"boss_core.{p.stem}" for p in (SCRIPTS / "boss_core").glob("*.py")}
        local.add("boss_core")
        missing = []
        for py in SCRIPTS.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for node in ast.walk(tree):
                # 只查顶层 import (函数内惰性 import 允许指向可选依赖)
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                if getattr(node, "col_offset", 0) != 0:
                    continue
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""])
                for n in names:
                    head = n.split(".")[0]
                    if (SCRIPTS / f"{head}.py").exists() or head in ("boss_core",):
                        if n not in local and head not in local:
                            missing.append(f"{py.relative_to(ROOT)}: {n}")
        assert not missing, "顶层 import 指向仓里不存在的模块:\n" + "\n".join(missing)


class TestSceneIsolation:
    """2 · 只带了 study-weekly 一个场景。"""

    def test_exactly_one_scene(self):
        scenes = [d for d in (ROOT / "scenes").iterdir() if d.is_dir()]
        assert [d.name for d in scenes] == ["study-weekly-reflect"], \
            "本仓只该有 study-weekly-reflect 一个场景"

    def test_scene_loads(self):
        sys.path.insert(0, str(SCRIPTS))
        import scene_loader as sl
        scene = sl.load_scene("study-weekly-reflect")
        assert scene.name == "study-weekly-reflect"
        assert scene.scene_type == "review"      # 不是 persona (本仓无分身场景)

    def test_title_keyword_is_configured(self):
        """标题闸是本机器人的核心行为, 配丢了会变成「什么文档都评」。"""
        cfg = yaml.safe_load((ROOT / "scenes" / "study-weekly-reflect"
                              / "scene.yaml").read_text(encoding="utf-8"))
        assert cfg["access"]["auto_review_title_keyword"] == "周报"


class TestPanelResolves:
    """3 · panel extends 链走得通, 评委编组是预期的。"""

    def test_panel_chain(self):
        sys.path.insert(0, str(SCRIPTS))
        import panel_loader
        p = panel_loader.resolve_panel("scenes/study-weekly-reflect/panel.yaml")
        assert [j["slug"] for j in p["judges"]] == ["v8-coach"], "单教练评委"
        assert p["scoring_mode"] == "sum_max_score"
        assert p["output_format"] == "study_weekly_v8"
        lenses = p["scoring_lenses"]
        assert len(lenses) == 5, "v7.22 是 5 维"
        assert sum(l["max_score"] for l in lenses) == 100, "5 维基础分合计 100"

    def test_extends_target_exists(self):
        """panel.yaml extends 的是 panels/default.yaml —— 它必须跟过来了。"""
        raw = yaml.safe_load((ROOT / "scenes" / "study-weekly-reflect"
                              / "panel.yaml").read_text(encoding="utf-8"))
        if "extends" in raw:
            assert (ROOT / raw["extends"]).is_file(), \
                f"panel extends {raw['extends']} 但该文件没带过来"


class TestConfidentialityBoundary:
    """4 · ★ 授权边界的可执行版本。

    本仓获授权带走: 评分框架 + 评委 doctrine + 学习小组成员真实姓名。
    **不含**: 上游锚点真名 / 上游团队真名 / 第三方客户名 / 任何并购尽调材料。

    这条红了 = 剥离漏了东西。**去删内容, 不要放宽断言。**
    """

    # 上游锚点身份 / 上游团队 / 第三方客户 / 上游专有场景代号
    FOREIGN = re.compile(
        r"田溯宁|田[总總伯先生哥]|老田|田老板|tian-suning|宽带资本|"
        r"亚信|AsiaInfo|智坊|zhifang|laotian|问策\s*AI|干部大会"
    )
    # 第三方并购尽调材料的指纹 (与周报诊断毫无关系, 出现即说明夹带了别的文档)
    MNA = re.compile(r"尽调|代持|竞业|减持|禁售|对赌|折价|限售")

    # 本文件自己写着这些正则的字面量, 扫自己必然自命中 —— 排除。
    SELF = "tests/unit/test_smoke_strip.py"

    @staticmethod
    def _tracked_text_files(exclude_self: bool = True):
        """遍历会被提交的文本文件。

        优先用 `git ls-files` (只看真正入库的); 仓还没 git init 时回退到 rglob,
        并跳过点目录 (.venv / .pytest_cache / .git 这些不是仓内容)。
        """
        r = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            names = r.stdout.split()
        else:
            names = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                     if p.is_file() and not any(part.startswith(".")
                                                for part in p.relative_to(ROOT).parts[:-1])]
        for rel in names:
            if exclude_self and rel == TestConfidentialityBoundary.SELF:
                continue
            p = ROOT / rel
            if not p.is_file() or p.suffix in (".png", ".jpg", ".pdf", ".ico", ".woff2"):
                continue
            try:
                yield rel, p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

    def test_no_foreign_identities(self):
        hits = [f"{rel}: {m.group()}" for rel, txt in self._tracked_text_files()
                if (m := self.FOREIGN.search(txt))]
        assert not hits, ("上游/第三方身份泄漏 (不在本仓授权范围内):\n  "
                          + "\n  ".join(hits))

    # 「说明本仓不带什么」是这几个文件的**职责**, 它们必然要写下 '尽调' 二字。
    # 只对 MNA (夹带材料) 免检; FOREIGN (他方身份) 对它们照查不误 ——
    # 任何文件都没有正当理由写上游锚点真名, 元文档也不例外。
    BOUNDARY_DOCS = {"CHANGELOG.md", "CLAUDE.md", "README.md", "UPSTREAM.lock",
                     "docs/deploy.md"}

    def test_no_ma_dossier_material(self):
        """按**密度**判, 不按单次出现判。

        真的夹带一份尽调文档长的是另一个样: 多个术语反复出现 (被排除的那份原始草稿
        是 尽调×18 / 估值×7 / 减持×3 / 代持×2 …)。故门槛设为
        **≥2 个不同术语** 或 **同一术语 ≥3 次**, 并放过 BOUNDARY_DOCS。
        """
        offenders = []
        for rel, txt in self._tracked_text_files():
            if rel in self.BOUNDARY_DOCS:
                continue
            found = self.MNA.findall(txt)
            if not found:
                continue
            distinct = set(found)
            if len(distinct) >= 2 or len(found) >= 3:
                tally = ", ".join(f"{t}×{found.count(t)}" for t in sorted(distinct))
                offenders.append(f"{rel}: {tally}")
        assert not offenders, ("夹带了并购/尽调材料 —— 与周报诊断无关, 属越权:\n  "
                               + "\n  ".join(offenders))

    def test_boundary_docs_still_checked_for_identities(self):
        """免检名单只对 MNA 生效 —— 别让它悄悄变成身份检查的后门。"""
        for rel in self.BOUNDARY_DOCS:
            p = ROOT / rel
            if p.is_file():
                assert not self.FOREIGN.search(p.read_text(encoding="utf-8")), \
                    f"{rel} 含他方身份 —— 免检名单不覆盖这一条"

    def test_upstream_lock_records_exclusions(self):
        """故意不带的文件要**写下来**, 否则下次同步会被无声地拉回来。"""
        lock = json.loads((ROOT / "UPSTREAM.lock").read_text(encoding="utf-8"))
        removed = lock.get("removed_from_upstream") or {}
        assert removed, "UPSTREAM.lock 必须记录哪些上游文件是故意不带的"
        for path, reason in removed.items():
            assert not (ROOT / path).exists(), f"{path} 标了不带, 却还在仓里"
            assert reason.strip(), f"{path} 的排除理由不能为空"


class TestNoSecrets:
    """5 · 凭据零入库 —— .env 只有 .example, 且 .example 里全是空值。"""

    def test_no_real_env_file(self):
        r = subprocess.run(["git", "ls-files", ".env"], cwd=ROOT,
                           capture_output=True, text=True)
        assert not r.stdout.strip(), ".env 绝不能入库"

    def test_env_example_has_no_values(self):
        secret_key = re.compile(r"^(LARK_APP_(ID|SECRET)\w*|BOSS_LLM_API_KEY)=(.+)$", re.M)
        txt = (ROOT / ".env.example").read_text(encoding="utf-8")
        filled = [m.group(0) for m in secret_key.finditer(txt)]
        assert not filled, f".env.example 里有填了值的凭据: {filled}"

    def test_no_credential_shaped_literals(self):
        """飞书 app id / secret 的字面形态 (cli_xxx / 32 位十六进制) 不该出现在任何文件。"""
        cli_id = re.compile(r"\bcli_[a-z0-9]{12,}\b")
        # 恰好 32 位的字母数字串 —— 飞书 app secret 的形态。前后不能再接同类字符,
        # 否则会把 sha256 (64 位) 切成两半误报。
        secret_like = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{32}(?![A-Za-z0-9])")
        hits = []
        for rel, txt in TestConfidentialityBoundary._tracked_text_files(exclude_self=False):
            if rel == "UPSTREAM.lock":           # 里面全是 sha256, 不是凭据
                continue
            if cli_id.search(txt):
                hits.append(f"{rel}: 疑似飞书 app_id")
            if m := secret_like.search(txt):
                hits.append(f"{rel}: 疑似 32 位密钥 {m.group()[:8]}…")
        assert not hits, "疑似凭据入库:\n  " + "\n  ".join(hits)


class TestRuntimeAssets:
    """6 · 运行时真正会读的文件都在 (少一个就是「跑起来才发现」)。"""

    @pytest.mark.parametrize("rel", [
        "scenes/study-weekly-reflect/scene.yaml",
        "scenes/study-weekly-reflect/panel.yaml",
        "scenes/study-weekly-reflect/judges/v8-coach/SKILL.md",
        "panels/default.yaml",
        "config/study_weekly_roster.yaml",
        "config/llm_profiles.yaml",
        "config/review_service.yaml.example",
        "config/scene.yaml.example",
        "config/redact_local.yaml.example",
        "schemas/case-schema.json",
        "requirements.txt",
        "UPSTREAM.lock",
        "ops/systemd/study-weekly-ws.service",
        "ops/systemd/study-weekly-worker.service",
        "ops/systemd/study-weekly-summary.service",
        "ops/systemd/study-weekly-summary.timer",
        "docs/deploy.md",
    ])
    def test_asset_present(self, rel):
        assert (ROOT / rel).is_file(), f"缺运行时资产: {rel}"

    def test_judge_doctrine_is_loadable(self):
        """评委 doctrine 是唯一喂给 LLM 的框架正文 —— 空了机器人会给出无意义评分。"""
        skill = (ROOT / "scenes/study-weekly-reflect/judges/v8-coach/SKILL.md")
        txt = skill.read_text(encoding="utf-8")
        assert len(txt) > 2000, "SKILL.md 过短, 疑似没带全"
        assert "v7.22" in txt, "doctrine 版本号缺失"

    def test_vendored_integrity(self):
        r = subprocess.run([sys.executable, "scripts/check_vendored.py"],
                           cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, f"vendored 引擎校验未过:\n{r.stdout}{r.stderr}"
