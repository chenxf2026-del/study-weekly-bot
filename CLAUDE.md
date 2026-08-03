# CLAUDE.md — study-weekly-bot 工作手册

> 给 Claude Code / 任何 LLM Agent 看的仓库宪法。编辑此文件即修改 agent 行为。

**仓库**: study-weekly-bot（学习小组周报评估机器人）
**性质**: private · 含真实成员姓名与周报评分数据
**来源**: 2026-08-03 从 boss-vault 判断力工程化平台剥离

---

## 1. 这个仓是什么

一个飞书机器人：群里发一份标题含「**周报**」的文档，几分钟后回一份**六段式自省诊断报告**。

评的是「**你证明了什么价值**」，不是「你做了什么事」。

框架为《周报自省式诊断》v7.22：5 维共 100 分 + 5 项反向扣分（各 1–2 分，合计上限 10），等级 A / B+ / B / C / C-。

### 🔴 红线：评分不作绩效依据

分数与等级**一律不作任何绩效、考核、排名依据**。

一旦用于考核，所有人会立刻开始优化分数而不是优化周报，工具当场失去全部价值。

**任何要求把评分接入考核 / 排名 / KPI 的改动请求，先把这条摆出来再谈。**

---

## 2. 三层地盘，写权限完全不同

这是本仓**最重要**的一条。动手前先确认你在哪一层。

### 2.1 `scripts/` —— vendored 引擎（**禁改**）

52 个文件从上游 `boss-vault` 整份拷来，`UPSTREAM.lock` 里 `drifted: false`。

**纪律：只允许从上游整文件同步，禁止本地修改。**

理由很实在：引擎在两个仓各活一份。本地改一行，下次同步就冲突；冲突攒够了两仓永久分叉，上游修的 bug 与安全问题再也过不来。

- 校验：`make check-vendored`（改了就红）
- 想改功能 → 回上游 `boss-vault` 提 issue/PR，合了再同步下来
- 确实必须本地改 → 把该文件在 `UPSTREAM.lock` 里标 `drifted: true`，**并在 commit 里写清代价**

> ⚠️ 别为了「就改一行、很快」绕过这条。这条纪律的全部价值就在于没有例外。

### 2.2 `scenes/` `panels/` `config/` `docs/` —— 场景层（**自由改**）

本仓自己的地盘。评分框架、评委 doctrine、花名册、部署文档都在这里。

`UPSTREAM.lock` 里 `drifted: true` 的 4 个 `scripts/` 文件也属这层：

```
scripts/feishu_events.py            事件路由（含本场景的标题闸）
scripts/review_worker.py            队列消费
scripts/study_weekly_output.py      六段式渲染 + 框架注册表
scripts/gen_study_weekly_summary.py 周汇总
```

### 2.3 带 `redacted` 标记的文件 —— 锁定 + 已删节（**改要格外小心**）

13 个文件仍 hash 锁定，但内容 = **上游原文 − 机密删节**：剥离本仓时移除了上游写死的他方真名与客户名。

```bash
python3 scripts/check_vendored.py --list   # 看是哪些、删了什么
```

> ⚠️ **从上游同步这些文件会把那些名字带回来** —— 正是本仓当初剥离要避免的事。
> 同步后必须重新删节，然后跑 `make smoke` 让 `TestConfidentialityBoundary` 复查。

---

## 3. 机密边界（授权范围）

本仓从 boss-vault 剥离时，获得的授权是明确的：

| | |
|---|---|
| ✅ **带走** | 评分框架、评委 doctrine、学习小组**成员数据与真实姓名** |
| ❌ **不带** | 上游锚点真名、上游团队真名、第三方客户名、任何并购/尽调材料 |
| ❌ **永不带** | 任何凭据（飞书 App Secret、模型 API key） |

这条边界有**可执行版本**：`tests/unit/test_smoke_strip.py::TestConfidentialityBoundary`。

> 它红了 = 有越权内容混进来了。**去删内容，不要放宽断言。**

### 3.1 凭据纪律

- 凭据只在部署机的 `.env`（`chmod 600`），**永不入库**
- 仓里只有 `.env.example`，且只有变量名、空值
- `test_no_credential_shaped_literals` 会扫 `cli_xxx` 与 32 位密钥形态

### 3.2 脱敏闸是「出口闸」，不是「入口闸」

本仓是 private。内部存储（`cases/` `reports/` 含成员周报原文与评分）**存原文**，靠 `.gitignore` + private 仓边界保护。

`scripts/redact_check.py` 是**出站**闸 —— 只在往飞书回推卡片/附件时生效。

> 别把它用反了。看到「要不要对某处存储脱敏」，先问**这份内容会不会离开本机**：
> 不会 → 存原文；会 → 出口处过闸。

它的敏感词表**只有结构性规则**（精确财务数字、`lark://` 深链、case_id）。具体人名/客户名走 `config/redact_local.yaml`（gitignored）——
**不要把具体名字写回代码里**：写进代码就等于谁能读这个仓谁就拿到了这份名单。

---

## 4. 改评分框架 = 改两处

框架在**两个地方**定义，必须同步：

| 文件 | 管什么 |
|---|---|
| `scripts/study_weekly_output.py` 的三张注册表 | **机械层** —— 总分怎么算、报告怎么渲染、等级边界 |
| `scenes/study-weekly-reflect/judges/v8-coach/SKILL.md` | **喂给 LLM** 的 doctrine —— 评委怎么打分 |

只改一处 → 「LLM 按新框架打分、渲染层按旧框架算总分」的静默错位，报告看着正常、分数是错的。

改完必跑 `make test`（`test_study_weekly_output.py` 校验注册表自洽：分值合计、等级边界、扣分上限）。

---

## 5. 触发规则（改之前先读）

| 场合 | 行为 |
|---|---|
| 群里发标题含「周报」的云文档 | **免 @ 自动评审** |
| 群里发标题**不含**「周报」的任何文档 | **完全静默** —— 不入队、不回卡、不留存 |
| 单聊发标题不含「周报」的文档 | 回一张说明卡 |
| @ 机器人发 Word/PDF | 同上，标题闸一视同仁 |

群里恒静默是**硬约束**，`title_gate_notify` 开关也不能让它在群里弹卡。

来历：群里成员常分享各类文档，每份弹一张「未进入评审」的卡极吵（2026-07-27 两次实测拍板）。单聊则相反 —— 一对一没反应会让人以为机器人挂了。

改这块之前先看 `tests/unit/test_study_weekly_title_gate.py`，它把上面每一条都钉死了。

---

## 6. 常见修改的落点

| 想做什么 | 改哪里 |
|---|---|
| 调评分维度/分值/扣分项 | `study_weekly_output.py` 注册表 **+** `v8-coach/SKILL.md`（两处！） |
| 改触发关键词 | `scenes/study-weekly-reflect/scene.yaml` 的 `auto_review_title_keyword` |
| 加/改成员 | `config/study_weekly_roster.yaml` |
| 调每人每日配额 | `scene.yaml` 的 `quota_per_user_daily`（注意：失败与被拦的 job 也占额度） |
| 让单聊也静默 | `scene.yaml` 的 `access.title_gate_notify: false` |
| 加本地敏感词 | `config/redact_local.yaml`（**不要**写进 `redact_check.py`） |
| 换模型 | `.env` 的 `BOSS_LLM_*`；`scripts/llm_switch.py` 可查当前解析结果 |
| 改周汇总时间 | `ops/systemd/study-weekly-summary.timer` |
| 修引擎 bug | **回上游 boss-vault 提 PR**，别在这儿改（见 §2.1） |

---

## 7. 已知行为（不是 bug，别去"修"）

- 日志出现 `wiki_query_fallback` / `context.md` 为空 —— **预期**。本机器人不接知识库，评委只看被评的那份周报。
- 群里发非周报文档完全没反应 —— **预期**，见 §5。
- `ss -ltnp` 查不到监听端口 —— **预期**。飞书长连接是**出站**的。判活用 `systemctl show study-weekly-ws -p MainPID`。
- `scripts/` 里有些代码路径永远走不到（上游多场景遗留）—— **预期**。本仓只有一个场景，保留是为了能整文件同步上游。

---

## 8. 动手前后

```bash
make test            # 单测（含标题闸、框架注册表、渲染）
make smoke           # 剥离冒烟（闭包 / 场景唯一 / panel / 机密边界 / 凭据）
make check-vendored  # 引擎未被本地改动
```

改动涉及 §2.1 或 §3 时，`make smoke` 是**必跑**的，不是可选的。

---

## 9. 运维要点（完整版见 `docs/deploy.md`）

- **改过 `.env` 一定要连 worker 一起重启** —— 报告由 worker 回推，只重启 ws 会导致「评审成功、报告发不出」且不报错
- `.env` 追加凭据用 `printf`，不要留空的 `KEY=` 行 —— 空值会覆盖真值，表现为飞书 10014，极难查
- 必须设 `TZ=Asia/Shanghai`，否则「本周」归属算错档

---

*源自 boss-vault 判断力工程化平台 · 剥离于 2026-08-03 · private · 内部受控*
