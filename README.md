# study-weekly-bot · 学习小组周报评估机器人

> 飞书机器人：群里发一份标题含「**周报**」的文档，几分钟后回一份**六段式自省诊断报告**。

---

## ⚠️ 机密声明（先读）

**本仓库为 private，含真实成员姓名与周报评分数据，不得公开、不得转发给未授权人员。**

- 仓库可见性必须保持 **private**
- `config/study_weekly_roster.yaml`（花名册）、`scenes/.../references/`（含金标准案例）含真实人名
- 运行产物 `cases/` `reports/` `writing/` 含成员周报原文与评分，**已在 `.gitignore` 中排除，不要 `git add -f`**
- **凭据（飞书 App Secret、模型 API key）永不入库** —— 只在部署机的 `.env`（`chmod 600`）

### 🔴 红线：评分不作绩效依据

这是**自省诊断**工具，帮成员看清「周报是价值证明而非工作记录」。
分数与等级**一律不作任何绩效、考核、排名依据**。一旦用于考核，所有人会立刻开始优化分数而不是优化周报。

---

## 这是什么

| | |
|---|---|
| **触发** | 群里发标题含「周报」的飞书云文档 → **免 @ 自动评审**；或 @ 机器人发 Word/PDF |
| **不触发** | 标题不含「周报」的任何文档 —— 群里**静默跳过**，单聊回一张说明卡 |
| **产出** | 六段式个人诊断报告（5 维基础分 + 反向扣分 + 改进建议 + 重写示例） |
| **耗时** | 单份约 5 分钟 |
| **框架** | 《周报自省式诊断》v7.22 —— 5 维共 100 分，5 项反向扣分各 1–2 分、合计上限 10；等级 A / B+ / B / C / C- |

一句话：**评的是「你证明了什么价值」，不是「你做了什么事」。**

---

## 快速上手

完整步骤见 **[`docs/deploy.md`](docs/deploy.md)**。最短路径：

```bash
git clone <本仓> ~/study-weekly-bot && cd ~/study-weekly-bot
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt

cp .env.example .env && chmod 600 .env        # 填飞书凭据 + LLM key
cp config/review_service.yaml.example config/review_service.yaml
cp config/redact_local.yaml.example config/redact_local.yaml   # 可选: 本地敏感词

make test                                      # 单测应全绿
```

然后按 `docs/deploy.md` 建飞书应用、配 scope、装 systemd。

---

## 仓库结构

```
scenes/study-weekly-reflect/    ← 场景本体：评分框架 + 评委 doctrine（本仓核心资产）
  judges/v8-coach/SKILL.md        唯一在运行时喂给 LLM 的 doctrine
scripts/                        ← 判断引擎（vendored，见下）
config/                         ← 花名册 / 服务配置 / LLM profile
ops/systemd/                    ← 三个 unit（ws / worker / 周汇总 timer）
tests/                          ← 单测
```

### ⚠️ `scripts/` 是 vendored 引擎，**不要改**

`scripts/` 里 56 个受管文件中的 52 个是从上游 `boss-vault` **整份拷来的**，不是本仓原创。

**纪律：只允许从上游整文件同步，禁止本地修改。**

理由很实在：引擎在两个仓各活一份，本地改一行，下次同步就冲突；冲突攒够了两仓永久分叉，上游修的 bug 和安全问题再也过不来。

- 校验：`make check-vendored`（CI 也跑，改了就红）
- 想改功能 → 回上游 `boss-vault` 提 issue/PR，合了再同步下来
- 同步：`bash scripts/sync_from_upstream.sh`

**可自由修改的只有 4 个**（`UPSTREAM.lock` 里标了 `drifted: true`）：

```
scripts/feishu_events.py            事件路由（已含本场景的标题闸逻辑）
scripts/review_worker.py            队列消费
scripts/study_weekly_output.py      六段式报告渲染 + 框架注册表
scripts/gen_study_weekly_summary.py 周汇总
```

改评分框架 → 改 `study_weekly_output.py` 的三张注册表 + `scenes/` 下的 doctrine，**两边要一致**。

### ⚠️ 有 13 个文件带「机密删节」，同步后要重新删节

它们仍 hash 锁定，但内容 = **上游原文 − 删节**：剥离本仓时移除了上游写死的他方真名与客户名（那些名字在本部署里保护不了任何东西，只会泄露它们自己）。

```bash
python3 scripts/check_vendored.py --list   # 看是哪些、各删了什么
```

从上游整文件同步会把那些名字**带回来**。同步完务必：

```bash
make smoke     # TestConfidentialityBoundary 会当场抓出漏网的
```

上游另有 4 个文件**整份不带**（分身内核 + 一份夹带第三方尽调材料的框架早期草稿），理由记在 `UPSTREAM.lock` 的 `removed_from_upstream` 里。

---

## 常用命令

```bash
make test            # 单测
make check-vendored  # 校验引擎未被本地改动
make smoke           # 冒烟（import + 场景唯一 + panel 解析 + 机械层）
```

---

## 已知行为（不是 bug）

- 日志出现 `wiki_query_fallback` / `context.md` 为空 —— **预期**。本机器人不接知识库，评委只看被评周报。
- 群里发非周报文档**完全没反应** —— 预期，见上面的触发规则。
- `ss -ltnp` 查不到监听端口 —— 预期，飞书长连接是**出站**的。判活用 `systemctl show study-weekly-ws -p MainPID`。

---

*源自 boss-vault 判断力工程化平台 · 剥离于 2026-08-03 · private · 内部受控*
