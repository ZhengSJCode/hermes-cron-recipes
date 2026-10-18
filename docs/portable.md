# 通用 Job 部署说明（5 个）

这 5 个装完就能跑，只要 `config/.env` 填好。

```bash
python3 install.py --only portable
```

---

## 1. GitHub 热门周榜 / 月榜

| | |
|---|---|
| job | `github-trending-weekly`（周一 10:00）、`github-trending-monthly`（每月 1 日 09:00） |
| 脚本 | `github_trending_weekly.sh` / `github_trending_monthly.sh` → 同一份 `github_trending_collect.py` |
| 数据源 | `https://github.com/trending?since=weekly|monthly` |

**流程**
1. 抓 trending 页（UA 伪装，超时 60s）
2. 解析 repo / desc / lang / 周期新增 star
3. 页面 HTML 拿 topics；API 补 license 和总 star（失败留空，不中断）
4. 本地模型批量生成三列：给谁用 / 同类仓库对比 / 中文用途
5. 拼 `| # | 应用 | 给谁用 | 同类仓库对比 | 用途 | 标签 |`
6. `lark-cli drive +import` 拿 `data.token` → `wiki +move` 挂到父节点

**必填环境变量**

```bash
FEISHU_SPACE_ID=          # 知识库 space id
FEISHU_GITHUB_PARENT_TOKEN=   # 榜单挂在哪个节点下
LLM_BASE_URL / LLM_MODEL
```

**手动跑一次**

```bash
export FEISHU_SPACE_ID=... FEISHU_GITHUB_PARENT_TOKEN=...
python3 ~/.hermes/scripts/github_trending_collect.py --period weekly --limit 15
# 只看采集、不写飞书、不调模型：
python3 ~/.hermes/scripts/github_trending_collect.py --period weekly --limit 3 --no-llm
```

**注意**：脚本最后一步要写飞书。`lark-cli whoami` 必须是 `user` 身份可用，否则会以
`need_user_authorization` 失败，且**前 5 步的采集结果会白跑**。

---

## 2. ClawHub 热门技能榜（每日 09:30）

- 抓 `sort=24h` Top 20，补 topics/categories
- 三列（给谁用 / 同类对比 / 中文用途）由本地模型生成
- 其余与 GitHub 榜同一条入库链路

环境变量：`FEISHU_SPACE_ID`、`FEISHU_PARENT_TOKEN`（ClawHub 自己的父节点）。

---

## 3. 360 SkillHub 热门技能榜（每日 09:50）

- 抓 `sort=downloads` Top 20，**官方中文简介直接可用**，模型只补两列
- 与 ClawHub 的差别：少一列、少一次模型输出 token

环境变量：`FEISHU_SPACE_ID`、`FEISHU_PARENT_TOKEN`。

---

## 4. AI 选题雷达（每日 09:00）

**形态不同**：`no_agent=false` + `script`，脚本只负责取快照，判断哪些是「新内容」由 agent 做。

```bash
# 单独跑采集（只打印结构化快照，不写任何地方）
python3 ~/.hermes/scripts/topic_radar_daily.py
```

job 的 prompt 会让 agent：
1. 读脚本输出的 4 个信源快照
2. 按自己的选题标准筛
3. 走 `drive +import` → `wiki +move` 入库

**要改的地方**：信源列表在脚本里；选题标准在 `jobs/topic-radar-daily.job.json` 的 prompt 里。
它挂的两个 skill（`feishu-wiki-maintenance`、`web-to-feishu-wiki`）属外部 skill，需自行安装，
不装也能跑，只是 agent 少了 wiki 结构约定。

---

## 5. 通用排障

| 现象 | 查什么 |
|---|---|
| 完全没输出 | `~/.hermes/cron/executions.db` 里该 job 最近行的 `status` / `error` |
| 报 `Script exited with code 1` | `~/.hermes/cron/output/<job_id>/<ts>.md` 的 stdout 段（脚本自带分步日志 `[1/5]…`） |
| 三列是空的 | stdout 里 `[llm] 调用失败`——网关抖动或模型渠道下线，看 `LLM_BASE_URL` |
| 采集到了但飞书没文档 | `lark-cli whoami` → `tokenStatus` |
| 报告没到但 job 显示 ok | 该 job 的 `deliver` 目标不对（`--deliver origin` 指创建它的那个会话） |

一键体检：

```bash
hermes cron list
sqlite3 ~/.hermes/cron/executions.db \
  "select status,datetime(claimed_at,'unixepoch','localtime'),substr(error,1,120) \
   from executions where job_id='<id>' order by claimed_at desc limit 5;"
```
