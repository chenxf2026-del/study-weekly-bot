# 部署指南 · study-weekly-bot

从零把机器人跑起来。**预计 40–60 分钟**，其中等飞书审批可能另计。

需要准备：

- 一台能访问公网的 Linux 机器（1C2G 起步够用；不需要公网 IP，也**不需要**开放任何入站端口）
- 飞书**管理员权限**（建应用、批 scope）
- 一个 LLM API key

---

## 目录

1. [建飞书应用](#1-建飞书应用)
2. [配权限 scope](#2-配权限-scope)
3. [开长连接事件订阅](#3-开长连接事件订阅)
4. [发布应用](#4-发布应用)
5. [装代码](#5-装代码)
6. [填配置](#6-填配置)
7. [验证凭据](#7-验证凭据)
8. [装 systemd](#8-装-systemd)
9. [拉机器人进群](#9-拉机器人进群)
10. [验收](#10-验收)
11. [排障](#排障)

---

## 1. 建飞书应用

[飞书开放平台](https://open.feishu.cn/app) → **创建企业自建应用**。

名字建议直接叫用户会在群里看到的名字，比如「周报教练」。头像随意。

建完在「凭证与基础信息」页拿到两个值，等下要填：

| 字段 | 形态 |
|---|---|
| App ID | `cli_` 开头 |
| App Secret | 32 位字母数字 |

> ⚠️ **App Secret 只在这里能完整看到**。它等价于机器人账号的密码 —— 不要贴进聊天、文档、issue。本仓任何文件都不该出现它（有单测在守：`test_no_credential_shaped_literals`）。

---

## 2. 配权限 scope

「权限管理」里加下面 6 个。**少一个都会以奇怪的方式失败**，所以逐个核对：

| scope | 少了会怎样 |
|---|---|
| `im:message` | 收不到任何消息 |
| `im:message:send_as_bot` | 收得到、回不了；日志有 job 完成但群里没动静 |
| `im:resource` | 下载不了群里发的 Word/PDF（报 permission denied） |
| `im:message.group_msg` | **群里只有 @ 机器人才收得到消息** —— 免 @ 自动评审直接失效 |
| `docx:document:readonly` | 读不了飞书云文档（本机器人主用法就是发云文档链接） |
| `drive:drive:readonly` | 云文档拿得到标题、拿不到正文 |

> ### `im:message.group_msg` 需要单独说明
>
> 这条是**敏感权限**，飞书会要求单独申请、由管理员审批，可能等几小时到一天。
>
> 它的含义是「机器人能读到群里的**所有**消息，不限于 @ 它的」。本机器人**必须**要它 —— 「群里发一份周报云文档就自动评审、不用 @」这个核心体验完全建立在它之上。
>
> 与此同时也要如实告诉小组成员：**机器人能看到群里的消息**。它只处理标题含「周报」的文档，其余一律丢弃、不入队、不留存（见 `scripts/feishu_events.py` 的 `_title_gate_blocked`），但知情权是知情权。

---

## 3. 开长连接事件订阅

「事件与回调」→ 订阅方式选 **长连接**（不是 Webhook）。

选长连接的好处：机器人**主动出站**连飞书，所以

- 不需要公网 IP、不需要域名、不需要证书
- 不需要在防火墙开任何入站端口
- 内网机器也能跑

然后添加事件：**接收消息 v2.0**（`im.message.receive_v1`）。只要这一个。

---

## 4. 发布应用

「版本管理与发布」→ 创建版本 → 提交发布。

**可用范围**要包含所有会用它的人。范围外的人发消息机器人收不到，且不会有任何报错提示 —— 排查时很容易懵。

---

## 5. 装代码

```bash
git clone <本仓地址> ~/study-weekly-bot
cd ~/study-weekly-bot

python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

make test          # 应该全绿；不绿先别往下走
```

> Python **3.11+**。3.10 及以下跑不了（代码里用了 `X | None` 的运行时求值形态）。

---

## 6. 填配置

三个文件，都 gitignored：

### 6.1 `.env` —— 凭据

```bash
cp .env.example .env
chmod 600 .env
```

然后**用 `printf` 追加**，不要手编留空行：

```bash
printf 'LARK_APP_ID_STUDY_WEEKLY=%s\n'     'cli_你的appid'   >> .env
printf 'LARK_APP_SECRET_STUDY_WEEKLY=%s\n' '你的appsecret'   >> .env
printf 'BOSS_LLM_API_KEY=%s\n'             '你的key'         >> .env
```

> ### ⚠️ 这里有个能耗掉你一下午的坑
>
> `.env` 被 systemd 以 `set -a` 语义读取，**同名变量后定义覆盖先定义**。
>
> 如果你 `cp` 来的模板里有一行空的 `LARK_APP_SECRET_STUDY_WEEKLY=`，而你把真值追加在**后面**，那没问题；但如果空行在后面，空值会覆盖真值，表现是飞书返回 **10014 app secret invalid** —— 而你去 `grep` 会看到密钥明明在文件里。
>
> 所以每次改完都数一遍：
>
> ```bash
> grep -c '^LARK_APP_SECRET_STUDY_WEEKLY=' .env   # 必须是 1
> grep -c '^LARK_APP_ID_STUDY_WEEKLY='     .env   # 必须是 1
> ```

还要确认 `.env` 里有：

```
TZ=Asia/Shanghai
```

不设时区，「本周」的归属会算错档 —— 周汇总会把周一的周报算进上一周。

### 6.2 `config/review_service.yaml` —— 服务兜底配置

```bash
cp config/review_service.yaml.example config/review_service.yaml
```

默认值直接能用。文件里注释说明了每一项。

### 6.3 `config/redact_local.yaml` —— 本地敏感词（可选，建议配）

```bash
cp config/redact_local.yaml.example config/redact_local.yaml
```

填你们**自己**要保护的真名 / 客户名 / 机构名。出站的卡片和附件命中即不发原文。

不配也能跑：结构性规则（精确财务数字、`lark://` 深链、case_id）本来就在代码里，一直生效。

### 6.4 花名册

`config/study_weekly_roster.yaml` **已经带了真实成员名单**（这是本仓获授权携带的数据）。

新增成员时按现有格式加一条即可。`open_id` 字段可以留空 —— 留空时按姓名匹配，配上了更精确（open_id 可以在飞书管理后台查，或从收到的消息日志里取）。

---

## 7. 验证凭据

装 systemd 之前先手工确认凭据是通的，省得后面在服务日志里猜：

```bash
set -a && . ./.env && set +a
curl -s -X POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal \
  -H 'Content-Type: application/json' \
  -d "{\"app_id\":\"$LARK_APP_ID_STUDY_WEEKLY\",\"app_secret\":\"$LARK_APP_SECRET_STUDY_WEEKLY\"}"
```

期望 `{"code":0,...,"tenant_access_token":"t-..."}`。

| 返回 | 意思 |
|---|---|
| `code: 0` | 好了，往下走 |
| `code: 10014` | secret 不对 —— 回 6.1 数那个 `grep -c` |
| `code: 10013` | app_id 不对 |

再验 LLM：

```bash
.venv/bin/python scripts/llm_switch.py    # 打印当前解析到的模型与来源
```

---

## 8. 装 systemd

三个 unit + 一个 timer：

| unit | 职责 |
|---|---|
| `study-weekly-ws` | 长连接收飞书事件 |
| `study-weekly-worker` | 消费队列、跑流水线、**回推报告** |
| `study-weekly-summary.timer` | 每周一 20:00 出上周汇总 |

```bash
cd ~/study-weekly-bot
for u in ops/systemd/*; do sudo cp "$u" /etc/systemd/system/; done
sudo sed -i "s|__ROOT__|$HOME/study-weekly-bot|g; s|__USER__|$USER|g" \
     /etc/systemd/system/study-weekly-*.service \
     /etc/systemd/system/study-weekly-*.timer

sudo systemctl daemon-reload
sudo systemctl enable --now study-weekly-ws study-weekly-worker study-weekly-summary.timer
```

确认起来了：

```bash
systemctl status study-weekly-ws study-weekly-worker --no-pager
systemctl list-timers study-weekly-summary --no-pager
```

> ### ⚠️ 换/加飞书凭据后，**worker 也要重启**
>
> 报告是 **worker** 回推的，它需要在自己的进程环境里拿到新凭据。
>
> 只重启 `ws` 会得到一个特别难查的现象：**评审跑成功了、日志一切正常、群里就是收不到报告**，且不报错。
>
> ```bash
> sudo systemctl restart study-weekly-ws study-weekly-worker   # 两个一起
> ```

---

## 9. 拉机器人进群

把机器人加进学习小组群。

群里发文件**不需要** @ 它（这正是 `im:message.group_msg` 换来的）。

---

## 10. 验收

按顺序走一遍，每步都该看到明确现象：

| # | 做什么 | 期望 |
|---|---|---|
| 1 | 群里发一份标题含「周报」的飞书云文档 | 几秒内回「📋 评审已受理」受理卡（含排队位） |
| 2 | 等约 5 分钟 | 回一份六段式诊断报告 |
| 3 | 群里发一份标题**不含**「周报」的文档 | **完全没反应** —— 这是对的，不是坏了 |
| 4 | 单聊发一份标题不含「周报」的文档 | 回一张说明卡，告诉对方规则 |
| 5 | @ 机器人发一个 Word 周报 | 同 1、2 |

第 3 步的静默是设计决定，不是 bug：群里成员会分享各类文档，每份都弹一张「未进入评审」的卡极其吵。单聊则相反 —— 一对一没反应会让人以为机器人挂了，所以单聊要给提示。

想让单聊也闭嘴：`scenes/study-weekly-reflect/scene.yaml` 里设 `access.title_gate_notify: false`。

---

## 排障

### 判活

机器人是**出站**长连接，**没有监听端口** —— `ss -ltnp` 查不到东西是正常的，别据此判断它挂了。

```bash
systemctl show study-weekly-ws -p MainPID     # 0 = 在崩溃重启循环
journalctl -u study-weekly-ws -n 50 --no-pager
journalctl -u study-weekly-worker -n 50 --no-pager
```

### 常见现象

| 现象 | 多半是 |
|---|---|
| 群里发周报**毫无反应** | ① `im:message.group_msg` 没批下来 ② 标题真的不含「周报」 ③ 发的人不在应用可用范围内 |
| 收到受理卡、**等不到报告** | worker 挂了或没拿到凭据 → 看 worker 日志；改过 `.env` 就重启 worker |
| 报 `10014 app secret invalid` | `.env` 里 secret 被空行覆盖 → `grep -c` 数一遍（见 6.1） |
| 云文档报权限错误 | 缺 `docx:document:readonly` 或 `drive:drive:readonly`；也可能该文档没授权给应用 |
| 日志里 `wiki_query_fallback` / `context.md` 为空 | **正常**。本机器人不接知识库，评委只看被评的那份周报 |
| 报告里分数明显不对 | 看 `scripts/study_weekly_output.py` 的三张注册表与 `scenes/.../judges/v8-coach/SKILL.md` 是否一致（改框架必须两边一起改） |
| 周汇总没出 | `systemctl list-timers`；机器当时关机的话 `Persistent=true` 会在开机后补跑 |

### 配额

默认每人每日 30 单（`scene.yaml` 的 `quota_per_user_daily`）。

注意**失败的、被拦的 job 也占额度** —— 反复调试同一份文档会把额度用光，表现为「突然不受理了」。

### 日志里想看某一单

```bash
journalctl -u study-weekly-worker --since today | grep <job_id>
```

`job_id` 在受理卡上。

---

## 改评分框架

框架是**两处**定义的，必须同步改：

1. `scripts/study_weekly_output.py` —— 三张注册表（维度、分值、扣分项），**机械层**，决定总分怎么算、报告怎么渲染
2. `scenes/study-weekly-reflect/judges/v8-coach/SKILL.md` —— **喂给 LLM** 的 doctrine，决定评委怎么打分

只改一处会得到「LLM 按新框架打分、渲染层按旧框架算总分」这类静默错位。

改完跑：

```bash
make test
```

`tests/unit/test_study_weekly_output.py` 会校验注册表自洽（分值合计、等级边界、扣分上限）。

---

## 升级引擎

`scripts/` 下大部分文件是从上游 `boss-vault` 整份拷来的，**不要本地改**（理由见 [README](../README.md#-scripts-是-vendored-引擎不要改)）。

```bash
make check-vendored              # 校验没被本地改过
bash scripts/sync_from_upstream.sh   # 从上游同步
```

> ### ⚠️ 同步后必须重新删节
>
> 有 13 个文件的内容是「上游原文 **减去**机密删节」—— 剥离本仓时移除了上游写死的他方真名与客户名。
>
> 从上游整文件同步会把那些名字**带回来**，正是本仓当初剥离要避免的事。
>
> ```bash
> python3 scripts/check_vendored.py --list   # 列出哪些文件带删节、删了什么
> ```
>
> 同步完跑 `make smoke`，`TestConfidentialityBoundary` 会当场抓出漏网的。

---

## 数据与边界

| 项 | 说明 |
|---|---|
| 仓库可见性 | **必须保持 private**。含真实成员姓名 |
| 运行产物 | `cases/` `reports/` `writing/` 含成员周报原文与评分，已在 `.gitignore` 里 —— **不要 `git add -f`** |
| 凭据 | 只在部署机的 `.env`（`chmod 600`），永不入库 |
| 周报原文 | 只在本机；评审时会送给 LLM（这是它的工作方式，建议提前告知成员） |

### 🔴 红线：评分不作绩效依据

这是**自省诊断**工具。分数与等级**一律不作任何绩效、考核、排名依据**。

一旦用于考核，所有人会立刻开始优化分数，而不是优化周报 —— 工具当场失去全部价值。
