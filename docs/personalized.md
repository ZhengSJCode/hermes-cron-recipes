# 个人化 Job 改造指引（5 个）

这 5 个 job 的 **prompt 里写死了原作者的知识库结构、私有 skill、内网服务**。
直接装会失败——不是脚本 bug，而是它们的假设只对原作者成立。

本文件逐个说明：**哪些能复用、哪些必须重写、改哪里**。

---

## 1. `feishu-daily-report`（每日 09:00）

**能复用**：整条「取数据 → 组 Markdown → import → move → 回读校验」的链路，以及零节的日报骨架。

**必须重写**

| 位置 | 原假设 | 你要改成 |
|---|---|---|
| `FEISHU_PARENT_TOKEN` | 固定父节点下挂当日文档 | 你自己知识库里「日报」所在节点 |
| prompt 第 3 步 | 读**昨日**那篇日报抽未闭环项 | 按你的结构改；若节点是日期命名，**用 title 精确相等匹配**，别用前缀 |
| prompt 第 1 步 | 滴答清单 MCP + `lark-cli calendar +agenda` | 换你的待办/日历来源 |
| prompt 第 5 步 | `--target-space-id` 写死 | 换 `FEISHU_SPACE_ID` |

**踩过的坑**

- 日历 scope 结构性缺失时（app 的 user scope 清单里根本没有 `calendar:*`），重登也没用，该区块永久降级 → prompt 应明确「该区块降级写一句，不要中断整个任务」。
- 日报是个人日记，**不要挂进三层结构**（raw / 编译页 / index），单独一个父节点。
- 交付纪律：最终回复只回一行 `日报已生成：<标题> → <URL>`，**不要贴正文**。

---

## 2. `dida365-todo-reminder`（每日 10:00 / 15:00）

**能复用**：prompt 里那套查询策略（今天 + 近 7 天积压去重）。

**必须改**

- 需要 MCP server `dida365` 已连上（`hermes mcp test dida365`）。
- prompt 指名 `mcp__dida365__list_undone_tasks_by_time_query`。**如果 cron 会话里没有这些 `mcp__*` 工具**，不是服务坏了，而是该 server 本次没注册；改走直连 HTTP JSON-RPC（token 在 `~/.hermes/mcp-tokens/dida365.json`），并在汇报里如实写明「本轮经直连查询」。
- **禁用 `filter_tasks`**（噪音太大），这是原作者踩坑后的结论。

---

## 3. `youtube-daily-collect`（每日 22:00）

**能复用**：prompt 里的质量门禁与「分批 + 逐条落盘」结构。

**必须重写**

- 依赖 `yt-dlp`（本机下 YouTube 需 `--no-check-certificate`）。
- 依赖个人知识库目录约定（原文用 gbrain：`~/.gbrain/brain.pglite` + 页面仓）。
- 采集范围是作者自定的（个人成长 + 科技两类，宁少勿滥）→ 换你的口味。
- **批量纪律**：逐个执行，禁批量循环；每个视频完成即落盘，别攒到最后。

---

## 4. `tuitui-message-digest`（每日 17:00）

**能复用**：日报的汇报结构（会话分组 / 未读统计 / 重点摘要）。

**必须改**

- 依赖企业内网 IM CLI（`tuitui-cli`）+ **企业授权**，公网拿不到。
- prompt 里引用的采集脚本路径属原作者 skill 目录，换成你自己的。
- 公司内网才有意义，**对外分享时建议直接删掉这个 job**。

---

## 5. `errorbook-mining`（每日 03:00）

**状态：不可直接运行** —— 依赖私有 skill `feishu-workflow-errorbook`，本仓库不含，公网也没有。

**能复用的思路**

> 每日凌晨扫「当天产生的经验/报错/trace」→ 归类 → 按准入策略晋升到长期知识库 → 更新索引。

要落地就照这个骨架写自己的 skill，并在 prompt 里保留两条硬约束：
1. 严格按 skill 的「固定坐标」执行，不要自创结构；
2. 晋升要有准入判据（重复次数、被引用次数），否则知识库会被噪音淹没。

---

## 通用改造检查表

装完 personalized 的 job 后逐项确认：

- [ ] `hermes cron list` 里每个 job 的 `skills` 在你本机都装得上（`install.py` 会列出缺失清单）
- [ ] prompt 里出现的每个 `${FEISHU_*_TOKEN}` 都已替换成真实节点，且该节点确实存在
- [ ] prompt 里出现的每条命令路径在本机存在（`ls <path>`）
- [ ] `lark-cli whoami` 是 user 身份可用
- [ ] 第一次跑用 `hermes cron run <job_id>` 手工触发，且**先确认产出不会与正式档期撞名**
- [ ] 跑完读 `~/.hermes/cron/output/<job_id>/<ts>.md` 的 stdout，确认每一步真的执行了

---

## 一句话判据

> **`status=completed` + `last_status=ok` + 报告自称「校验通过」，三者都不构成内容正确的证据。**

凡是「定时产出文档」的 job，验收时必须**回读产物正文**：
- 逐字稿页 → 数正文汉字数（坏页接近 0）
- 翻译稿 → 目标语言字符占比
- 表格 → 必含列数 / 关键字段非空率

存在性检查（条数对不对、链接在不在）永远查不出「内容被替换」这类故障。
