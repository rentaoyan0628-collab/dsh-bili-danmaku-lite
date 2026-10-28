# dsh-bili-live

**DeepSeek Harness (DSH) Bilibili Live Plugin / B站直播弹幕 AI 插件**：实时读取 Bilibili 直播间弹幕、在用户授权后发送弹幕，并提供可选的 AI 自动陪聊。无需 QQBridge 或 QQ 机器人。

**English:** An independent DeepSeek Harness plugin for Bilibili live chat (danmaku): read live room events, send messages with user-controlled permissions, and optionally use AI to decide when to reply. No QQ bot required. Version 0.2 handles text events only, **not** audio or video.

**关键词 / Keywords:** DSH plugin · DeepSeek Harness · Bilibili Live · B站直播 · 弹幕 danmaku · AI auto chat · live room assistant

给 [DeepSeek Harness](https://github.com/deepseek-ai)（DSH）用的 **B站直播间插件**：看弹幕、发弹幕、挂着听、可选自动陪聊。

> **v0.4.0 安全修复版**：只做弹幕与配置，**不含听觉或视觉功能**（不拉流、不转写、不看画面）。本版修复认证服务不可用时的放行问题，并取消首次解释器配置例外。
> 所有「进入某房间」「发弹幕」的动作都在**人**手里 —— 模型只能申请，放行在设置页里点。

## 它能做什么

| 能力 | 谁触发 | 说明 |
| --- | --- | --- |
| 查房间信息 / 最近弹幕 | 模型 | 只读。是否在播、标题、分区、在线人数 |
| 监听实时弹幕若干秒 | 模型 | 只读。弹幕 / 礼物 / SC / 进场 |
| **发弹幕** | 模型（要 `confirm=true`）或设置页 | 默认关；同一房间 60 秒 1 条；每次写日志 |
| **申请进入某房间** | 模型 | 只写一条待批申请，**放行必须人来点** |
| **挂着听** | 模型或设置页 | 后台 worker 收弹幕写 hint，够得着时 POST 到你的 webhook |
| **自动陪聊** | 设置页开关 | 定时读增量弹幕 → 调一次模型判断要不要接话 → 写进队列由 worker 发 |

## 它和「刷B站」插件、以及网上那些直播弹幕机器人，差别在哪

这套东西不是「又一个直播间挂机脚本」。它是在 DSH 里跑的插件，边界画得很死：

| 维度 | 这个插件 | 刷B站那类插件 | 常见的直播弹幕机器人 |
| --- | --- | --- | --- |
| 只管一件事 | 直播间：看弹幕 / 发弹幕 / 挂着听 | 推荐流浏览、挑视频、发评论、写报告 | 通常是「关键词命中就回复」 |
| 进哪个房间 | **默认谁都进不去**，放行名单为空就全拒；模型只能写申请，放行必须人在设置页点 | 不涉及 | 配好 cookie 就全站可进，或写死房间号 |
| 什么时候说话 | 默认**发弹幕总开关关着**；开了也要 `confirm=true`、每房间 60 秒 1 条 | 自动评论可关 | 命中关键词立刻发，多是无人复核 |
| 自动陪聊 | 先「判断要不要接」，模型回 `SKIP` 就沉默；默认 4 分钟不重复、每小时 8 句、单句 40 字内 | 无 | 「收到弹幕就生成一句回复」的占多数 |
| 记录 | 每次发送（含**草稿/被拦/失败**）都写 `logs/danmaku.jsonl`，事后能查「打算发什么、最后发没发」 | 有报告 | 多半只 log 错误 |
| 依赖 | 一个 Python venv + DSH 自己 | DSH | 往往要 Node/Go 服务、Redis、数据库、自建面板 |
| 人格与记忆 | 不自己存对话历史，人格交给宿主（DSH 的 prompt / 记忆），插件只负责「进房、看、说」 | 宿主负责 | 多数内置自己的 prompt 和会话库 |
| 音视频 | **不做**（v0.2 明确不拉流、不转写、不看画面） | 不含 | 有些项目会录 15 秒音频做 ASR 或抽帧做视觉，代价是带宽、显存和隐私 |
| 凭据 | B站 cookie 只落本机运行目录，不进仓库、不进发布包 | 同 | 有的项目要求填在配置文件里并提交 |

一句话：**别人做的是「让机器人尽量多说话」，这里做的是「让人始终有权决定它能不能进、能不能说」**。

## 安装

从 GitHub 源码安装到 desktop profile：

```bash
dsh plugin --profile desktop add github:rentaoyan0628-collab/dsh-bili-live
```

npm 安装方式（请先确认 [npm 包页面](https://www.npmjs.com/package/dsh-bili-live) 已有发布版本；发布者为 `n0y0t`）：

```bash
dsh plugin --profile desktop add dsh-bili-live
```

安装插件后仍需完成下面的 Python 依赖和 B站凭证配置。v0.2.0 尚未完成独立插件端到端运行验证，适合愿意反馈问题的用户使用。

1. 把整个目录放进 DSH 的插件位置（或 `npm i` 到 profile 里），然后在 `dsh.profile.bundles` 里列出 `dsh-bili-live`。
   插件自带的 `cordis.patch.yml` 只插入一条默认配置（默认全关）。

2. 装 Python 依赖（**装进插件自己的 venv**，别装进系统 Python）：

   ```bash
   python -m venv "$DSH_HOME/bili-live/.venv"
   "$DSH_HOME/bili-live/.venv/Scripts/python.exe" -m pip install -r requirements.txt   # Windows
   # "$DSH_HOME/bili-live/.venv/bin/python" -m pip install -r requirements.txt          # macOS / Linux
   ```

   插件会按这个顺序找解释器：设置页里填的「Python 路径」→ 家目录下的 `.venv` → `PATH` 里的 `python`。

3. 放 B站 登录凭证：`$DSH_HOME/bili-live/bili_credential.json`
   ```json
   { "sessdata": "...", "bili_jct": "...", "buvid3": "...", "dedeuserid": "..." }
   ```
   只读功能（info / feed）不登录也能用一部分；**发弹幕必须有凭证**。
   这个文件落在本机运行目录，不进仓库、不进发布包。

4. 打开 DSH 设置页 →「B站直播」→ 「重新自检」看到 **Python 就绪 / 已有 B站 凭证** 才算装好。

## 怎么用

1. **先放行**：设置页「放行名单」→ 填房间号 / UP主昵称 / UID → 「放行」。
   名单为空 = 哪个房间都进不去，这是刻意的。
2. **再挂机**（可选）：「挂着听」里填房间，webhook 可留空（留空就只写 hint，不打扰任何人）。
3. **要它开口**：把「允许发弹幕」打开。之后模型调 `bili_live_send`（带 `confirm=true`）或你在设置页手动发都能用。
4. **自动陪聊**（可选）：选 provider / 模型、写人设、定节奏，然后勾「启用」。
   它大多数时候会选择沉默（模型回 `SKIP`），只在真有意思时才说一句。

## 给模型的工具

| 工具 | 作用 |
| --- | --- |
| `bili_live_info` | 房间信息 + 最近弹幕（只读） |
| `bili_live_feed` | 监听 N 秒实时弹幕（只读） |
| `bili_live_send` | 发弹幕（默认草稿，`confirm=true` 才真发；总开关关着会被拒） |
| `bili_live_request` | 申请进入某房间（只写待批，**不**自行放行） |
| `bili_live_watch_start` / `watch_status` / `watch_stop` | 挂机 worker 的开关与状态 |
| `bili_live_log` | 最近的发送日志（含草稿/被拦/失败） |
| `bili_live_watching` | 我此刻在看的房间（放行名单 + 开播状态 + 最近说过什么） |
| `bili_live_doctor` | 环境自检（Python / 依赖 / 凭证路径） |

## 配置

配置在 `$DSH_HOME/bili-live/config.json`，设置页就是它的前端。

```json
{
  "pythonPath": "",
  "botName": "大肥鱼",
  "sendEnabled": false,
  "autoChat": {
    "enabled": false,
    "provider": "", "model": "",
    "persona": "你是直播间里的一个普通观众，说话短、口语、不客套。",
    "tickSec": 20, "windowSec": 150,
    "minLines": 2, "minGapSec": 240, "perHour": 8, "maxChars": 60
  }
}
```

- `sendEnabled`：发弹幕总开关。默认 `false`（先看后发）。
- `botName`：它在房里的名字，用来判断「有人点名」。
- `autoChat.minGapSec` / `perHour` / `maxChars`：两次开口的间隔、每小时上限、单句字数上限。

## 安全：设置页那几个接口怎么守的（v0.3.0）

插件在宿主里注册了 `/dsh-bili-live/*` 一组本地 HTTP 接口，给设置页用。这些接口能改配置、能动放行名单、能发弹幕，所以不是"能连上本机端口就算数"：

1. **先过 DSH 的信任栅栏** `connection.requestRejection` —— 宿主级鉴权（认 cookie/token），隧道或反代把 Host 改写成回环也挡得住；
2. **再过本机同源** —— Host 必须是回环地址，Origin/Referer 必须同端口（挡掉浏览器里另一个本地页面借你的登录态驱动这些写接口）；
3. **认证服务不可用直接拒绝访问**：没有 connection 时全部插件 HTTP 接口返回 503；认证调用报错或拒绝时也不放行。本机同源不是身份认证的替代品。

另外 **网页端不许把插件指到任意可执行程序**：仅接受固定 venv 解释器或本机配置/环境变量已经明确指定的解释器。必须为存在的绝对文件路径，比较真实路径，固定 venv 不允许符号链接逃出目录。取消首次配置例外，不按文件名放行。其它路径返回 403，新增信任路径须由管理员直接修改本机配置。

发弹幕的房间授权不在这层放宽：真正的闸在 `python/live_mcp.py` 的 `bili_live_send` —— 房间不在 `live-allow.json` 里一律拒绝。

## 数据都在哪

`$DSH_HOME/bili-live/`（可用环境变量 `DSH_BILI_LIVE_HOME` 改）：

```
config.json              插件配置
bili_credential.json     B站 凭证（本机，不进仓库）
live-allow.json          放行名单（只有人能改）
live-requests.jsonl      待批申请（模型写的）
live-chat/<房间>/        弹幕流、hint、queue、worker.log、watch.json
logs/danmaku.jsonl       每次发送（含草稿/被拦/失败）
logs/plugin.log          插件自己的日志
```

## 限制（v0.2 有意不做）

- **不看画面、不听声音**：不拉流、不做语音转写、不做视觉理解。
- **不自动进房**：名单必须人来放行；没放行的房间连只读的 info/feed 都会被拒。
- **弹幕发出去收不回**：60 秒冷却 + 每小时上限只能减少手滑，不能撤销。
- **不做开机自启 / 计划任务**：worker 由插件进程拉起，插件停它就停。
- 弹幕内容会交给你在设置页选的那个模型供应商 —— 本地缓存不等于全部在本地处理，详见 [docs/PRIVACY.md](docs/PRIVACY.md)。

## 排障

| 症状 | 先看 |
| --- | --- |
| 设置页一直「Python 没就绪」 | 设置页「重新自检」里的 tried 列表；缺依赖就按上面 pip 命令装，或手填解释器路径 |
| 工具报「找不到能用的 Python」 | 同上；`.venv` 位置或 `pythonPath` 写错了 |
| 发弹幕被拒 | 「允许发弹幕」没开；或房间不在放行名单；或 60 秒冷却没到 |
| 挂机起来了但没反应 | `live-chat/<房间>/worker.log`，以及房间是否在播 |
| 自动陪聊不开口 | 模型选择、`sendEnabled`、`minGapSec`；插件日志里有「模型决定 SKIP」是正常行为 |

## 许可与来源

MIT。Python 侧基于 [bilibili-api-python](https://github.com/Nemo2011/bilibili-api) 写的直播弹幕/发送逻辑，
MCP 用官方 [mcp](https://github.com/modelcontextprotocol/python-sdk) 的 FastMCP；
插件外壳照着 DSH 的插件约定写的。发布前请照 [docs/RELEASE-CHECKLIST.md](docs/RELEASE-CHECKLIST.md) 走一遍。