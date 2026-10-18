# hermes-cron-recipes

一套跑在 [Hermes Agent](https://hermes-agent.nousresearch.com/docs) 上的**定时任务配方集**：10 个可直接复用的 cron job，含 5 个开箱即用的采集脚本 + 5 个需要按自己环境改造的 agent 任务模板。

所有个人标识（飞书 space/node token、open_id、chat_id、绝对路径、网关地址）已抽出为环境变量，仓库本身不含任何账号信息。

---

## 前置条件（三样，缺一样脚本类 job 都跑不起来）

| 依赖 | 用途 | 检查命令 |
|---|---|---|
| **Hermes Agent** | 调度器本体 | `hermes --version` |
| **OpenAI 兼容网关** | 脚本里那三列中文摘要靠它生成 | `curl $LLM_BASE_URL/models -H "Authorization: Bearer $LLM_API_KEY"` |
| **lark-cli + 飞书自建应用** | 写飞书文档 / 知识库 | `lark-cli whoami` |

> 没有网关也能跑：给脚本加 `--no-llm`，只是「给谁用 / 同类对比 / 中文用途」三列会留空。

---

## 快速开始

```bash
git clone <this-repo> && cd hermes-cron-recipes

# 1) 配置
cp config/.env.example .env
$EDITOR .env                      # 填 FEISHU_* 和 LLM_*

# 2) 看它会干什么（不落地）
set -a; . ./.env; set +a
python3 install.py --dry-run

# 3) 真装：拷脚本到 ~/.hermes/scripts/ + 建 job
python3 install.py
```

只想要能跑的采集器：

```bash
python3 install.py --only portable
```

---

## 10 个 Job 一览

### 通用（`--only portable`，装完即可跑）

| 名称 | 时间 | 形态 | 干什么 |
|---|---|---|---|
| `github-trending-weekly` | 周一 10:00 | 纯脚本 | GitHub 周榜 → 六列表格 → 飞书 wiki 子文档 |
| `github-trending-monthly` | 每月 1 日 09:00 | 纯脚本 | 同上，月榜 |
| `clawhub-trending-daily` | 每日 09:30 | 纯脚本 | ClawHub 24h 热门技能 Top 20 |
| `skillhub360-trending-daily` | 每日 09:50 | 纯脚本 | 360 SkillHub 下载榜 Top 20 |
| `topic-radar-daily` | 每日 09:00 | agent + 脚本 | 4 个 AI 信源快照抓取，agent 负责筛选入库 |

**共同机制**：抓页面 → 解析 → 本地模型批量生成三列中文（带指数退避重试）→ 拼 Markdown → `drive +import` → `wiki +move` → 回读校验。

### 个人化（`--only personalized`，必须先改造）

| 名称 | 时间 | 个人化点 |
|---|---|---|
| `feishu-daily-report` | 每日 09:00 | 依赖知识库里固定的「日报」父节点 + 你自己的日报模板结构 |
| `dida365-todo-reminder` | 每日 10:00 / 15:00 | 依赖滴答清单 MCP server |
| `youtube-daily-collect` | 每日 22:00 | 依赖 yt-dlp + 本地知识库（gbrain）目录结构 |
| `tuitui-message-digest` | 每日 17:00 | 依赖企业内网 IM（tuitui-cli）授权 |
| `errorbook-mining` | 每日 03:00 | 依赖私有 skill `feishu-workflow-errorbook`（本仓库不含） |

改造要点见 [`docs/personalized.md`](docs/personalized.md)。

---

## 目录结构

```
jobs/     10 个 job 模板（schedule / prompt / skills / script，id 已脱敏）
scripts/  8 个脚本（4 py + 4 sh wrapper），常量全部走环境变量
config/   .env.example
install.py  拷脚本 + 调 hermes cron create（支持 --dry-run / --only）
docs/     portable.md（通用部署） / personalized.md（改造指引）
```

---

## 已知坑（都踩过，已写进脚本或文档）

1. **网关会抖**：本地网关实测会出现瞬时 `HTTP 408 stream disconnected before completion`，且**成功失败与模型无关**。Hermes 的 agent 循环对 408/5xx 自带重试，所以 agent 类 job 能扛；**纯脚本 job 没有重试**，一次抖动就整轮失败。
   → 本仓库的采集脚本已内置指数退避重试（`LLM_MAX_ATTEMPTS` / `LLM_BACKOFF_SECONDS`，默认 4 次 / 3s 起）。
2. **thinking 类模型的回放 400**：某些模型（DeepSeek V4 系）要求把 `content[].thinking` 原样回传，经网关中转后易报
   `HTTP 400: The content[].thinking in the thinking mode must be passed back to the API`。
   → 解决办法是把该 job pin 到**非 thinking 模型**：`hermes cron edit <id> --model <非thinking模型> --provider <provider> --pin`。
3. **飞书 user token 会失效**：`lark-cli whoami` 显示 `tokenStatus: missing` 时，所有 `drive +import` / `wiki +move` 全挂。
   → 恢复：`lark-cli auth login --domain wiki,docs,drive`（Device Flow，需要本人在浏览器确认）。**cron 里不要跑阻塞式登录**。
4. **`no_agent` 的 script 字段不接受参数**：要传 `--limit 20` 这类参数，必须用 `.sh` wrapper 包一层——`scripts/*.sh` 就是这么来的。
5. **`hermes cron create` 的 prompt 是位置参数**：长 prompt 用 `install.py` 走 argv 传，别手敲进 shell。

---

## 安全

- `.env` 在 `.gitignore` 里，**不要提交**。
- 仓库内已无任何 token / open_id / chat_id / 绝对路径（`grep -rn "zhengshuaijie\|ou_\|oc_" .` 应为空）。
- `jobs/*.job.json` 里的 `${FEISHU_SPACE_ID}` 之类占位符由 `install.py` 在安装时展开；未设置时**保留原文并告警**，不会静默写入空值。

---

## License

脚本部分 MIT。`jobs/` 里的 prompt 是个人实践沉淀，同样按 MIT 使用，但请按自己环境重写其中的节点结构与账号假设。
