# dsh-bili-danmaku-lite

> **名字变了**：这个包原来叫 `dsh-bili-live`，现在叫 `dsh-bili-danmaku-lite`。旧包不再发新版，装了旧包的人把 `dsh plugin` 那条命令换成新名字即可；插件在宿主里的运行时 id 仍是 `bili-live`，配置目录（`$DSH_HOME/bili-live`）、放行名单、发弹幕日志都沿用，不会丢。
>
> **「lite」是说运行开销小，不是功能被砍**：看弹幕、发弹幕、挂机听、可选自动陪聊、可选语音转写都在。轻在「不常驻重进程、不默认下载模型、闲时几乎不占 CPU」——重活只有你打开对应开关时才会起来。

**DeepSeek Harness (DSH) Bilibili Live Plugin / B站直播弹幕 AI 插件**：实时读取 Bilibili 直播间弹幕、在用户授权后发送弹幕，并提供可选的 AI 自动陪聊。无需 QQBridge 或 QQ 机器人。

**English:** An independent DeepSeek Harness plugin for Bilibili live chat (danmaku): read live room events, send messages with user-controlled permissions, and optionally use AI to decide when to reply. No QQ bot required. v0.2 is text only; **v0.4 adds optional audio transcription** (speech-to-text of the stream) — still **no video / no vision**.

**关键词 / Keywords:** DSH plugin · DeepSeek Harness · Bilibili Live · B站直播 · 弹幕 danmaku · AI auto chat · live room assistant

给 [DeepSeek Harness](https://github.com/deepseek-ai)（DSH）用的 **B站直播间插件**：看弹幕、发弹幕、挂着听、可选自动陪聊。

> **v0.4.2 范围**：弹幕 + 配置 + **可选「听声音」**（把直播里的说话转成文字，给自动陪聊当上下文），外加 v0.4.0 / v0.4.1 两轮安全修复（认证服务不可用时拒绝对敏感接口放行、解释器只认固定 venv 或本机已配的那个、挂机 webhook 只允许用户预先放行的地址）。
> **不看画面**：不抽帧、不做视觉理解。
> 所有「进入某房间」「发弹幕」的动作都在**人**手里 —— 模型只能申请，放行在设置页里点。

## 它能做什么

| 能力 | 谁触发 | 说明 |
| --- | --- | --- |
| 查房间信息 / 最近弹幕 | 模型 | 只读。是否在播、标题、分区、在线人数 |
| 监听实时弹幕若干秒 | 模型 | 只读。弹幕 / 礼物 / SC / 进场 |
| **发弹幕** | 模型（要 `confirm=true`）或设置页 | 默认关；同一房间 60 秒 1 条；每次写日志 |
| **申请进入某房间** | 模型 | 只写一条待批申请，**放行必须人来点** |
| **挂着听** | 模型或设置页 | 后台 worker 收弹幕写 hint，够得着时 POST 到你事先放行在 **webhook 白名单**里的地址 |
| **自动陪聊** | 设置页开关 | 定时读增量弹幕（有转录时也读转录）→ 调一次模型判断要不要接话 → 写进队列由 worker 发 |
| **听声音 / 语音转写**（v0.4） | 模型或设置页 | 后台拉流 → 分段 → 语音识别成带时间戳的文字；**只转文字，不发弹幕**；默认只留最近 5 分钟 |

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
| 音视频 | 只做**耳朵**（v0.4：可选转写，默认关；不抽帧、不看画面）；转写文本只留最近几分钟 | 不含 | 有些项目会录 15 秒音频做 ASR 或抽帧做视觉，代价是带宽、显存和隐私 |
| 凭据 | B站 cookie 只落本机运行目录，不进仓库、不进发布包 | 同 | 有的项目要求填在配置文件里并提交 |

一句话：**别人做的是「让机器人尽量多说话」，这里做的是「让人始终有权决定它能不能进、能不能说」**。

## 安装

从 GitHub 源码安装到 desktop profile：

```bash
dsh plugin --profile desktop add github:rentaoyan0628-collab/dsh-bili-danmaku-lite
```

npm 安装方式（包页：[dsh-bili-danmaku-lite](https://www.npmjs.com/package/dsh-bili-danmaku-lite)，发布者 `n0y0t`）：

```bash
dsh plugin --profile desktop add dsh-bili-danmaku-lite
```

> 旧名字 `dsh-bili-live` 在 npm 上已被标记为废弃（deprecated），指向这个新包；别同时装两个，运行时 id 相同会冲突。

安装插件后仍需完成下面的 Python 依赖和 B站凭证配置。**v0.4.2 尚未做端到端直播实测**（代码层过了语法检查与单元测试），适合愿意反馈问题的用户使用。

1. 把整个目录放进 DSH 的插件位置（或 `npm i` 到 profile 里），然后在 `dsh.profile.bundles` 里列出 `dsh-bili-danmaku-lite`。
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
2. **再挂机**（可选）：「挂着听」里填房间。webhook 那一栏是**下拉选的**，选项来自设置页的「挂机叫醒 · webhook 白名单」（留空就只写 hint，不打扰任何人）。
   - 模型不能自己往里加地址：调 `bili_live_watch_start` 时给不在白名单里的 webhook，会被直接拒（`status: webhook_not_allowed`），worker 都不会被拉起来。
   - 白名单只认 http(s)、不收带用户名/密码的地址；写 `https://bot.example.com` 放行那台机器的任意路径，写 `https://bot.example.com/wake` 就只放行那一条。
   - worker POST 时**不跟随跳转**，响应最多读 4 KB，落进状态日志的部分还会再截到 500 字。
3. **要它开口**：把「允许发弹幕」打开。之后模型调 `bili_live_send`（带 `confirm=true`）或你在设置页手动发都能用。
4. **自动陪聊**（可选）：选 provider / 模型、写人设、定节奏，然后勾「启用」。
   它大多数时候会选择沉默（模型回 `SKIP`），只在真有意思时才说一句。

## 听声音（v0.4.0，可选）

把直播里的**说话**转成文字，给自动陪聊当上下文。只做耳朵：**不抽帧、不看画面、不存音频**（PCM 只在管道里过一遍，不落盘）。

**前提**

1. `ffmpeg` 在 PATH 里；不在就设 `DSH_BILI_LIVE_FFMPEG` 指向可执行文件。
2. 装转写引擎（选一个）：
   - **faster-whisper**（CPU）：`pip install faster-whisper` 到插件用的那个 venv；装在别的 venv 就用 `DSH_BILI_LIVE_ASR_PYTHON` 指过去。
   - **whisper.cpp**（能吃 AMD 显卡的 Vulkan 版）：装好后设 `DSH_BILI_LIVE_WHISPERCPP` 指向 `whisper-cli.exe`，再放一个模型文件（`ggml-*.bin`），设 `DSH_BILI_LIVE_WHISPERCPP_MODEL`。
3. 转写不必用插件自己的 Python：配置里的 `audio.asrPython`（设置页「转写解释器」那个输入框）可以指向另一个已经装好 faster-whisper 的解释器。网页端只接受插件固定 venv 或本机配置/环境变量已明确指定的解释器，不再按整个 `$DSH_HOME` 目录放行；新增其它环境需由管理员直接修改本机配置文件。

**GPU**

- faster-whisper 走 CTranslate2，只认 **CUDA**。AMD 卡开 `cuda` 一定失败 —— 插件会把**原始报错原样显示**在设置页，然后自动退回 CPU，不会假装成功。AMD 卡上常见原文是 `RuntimeError: CUDA failed with error CUDA driver version is insufficient for CUDA runtime version`（其实是根本没有可用的 CUDA 设备）。
- 想在 AMD 卡上用，只有 whisper.cpp 的 Vulkan 版（本仓库不代编译，需要自己装 VS Build Tools + Vulkan SDK）。
- 设置页有「转写自检」按钮，就是真去点一次 CUDA 拿报错；慢，但能确认到底行不行。

**怎么开**

设置页「听声音」→ 填已放行的房间 → 选模型（tiny/base/small/medium）和设备（GPU 优先 / 只用 CPU / 强要 CUDA）→「开始听」。
模型也可以直接调工具：

| 工具 | 作用 |
| --- | --- |
| `bili_live_audio_start` | 开听觉（房间必须已放行） |
| `bili_live_audio_stop` | 关掉（留空=全部房间） |
| `bili_live_audio_status` | 谁在听、哪个引擎/设备、GPU 有没有失败、最近听到什么 |
| `bili_live_transcript` | 读最近几条转录（带时间戳） |
| `bili_live_asr_probe` | 体检：能不能用 GPU、whisper.cpp / DirectML 在不在 |

**代价**

- CPU 转写吃资源：`small` 模型在普通台式机上大约是「十几秒一段、延迟十几到几十秒」。想更快就降模型或加大 `window`。
- 转写一定有错字，自动陪聊的提示里已经写明「这是语音转写、可能有错，别当画面描述」。
- 文本会随自动陪聊的 prompt 交给你选的那个模型供应商。不想要就只开转写、不开自动陪聊。
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
| `bili_live_audio_start` / `audio_stop` / `audio_status` | 听声音（语音转写）的开关与状态 |
| `bili_live_transcript` | 读最近的语音转写（带时间戳） |
| `bili_live_asr_probe` | 转写体检：GPU 能不能用、whisper.cpp / DirectML 在不在 |

## 配置

配置在 `$DSH_HOME/bili-live/config.json`，设置页就是它的前端。

```json
{
  "pythonPath": "",
  "botName": "大肥鱼",
  "sendEnabled": false,
  "webhookAllowlist": [],
  "autoChat": {
    "enabled": false,
    "provider": "", "model": "",
    "persona": "你是直播间里的一个普通观众，说话短、口语、不客套。",
    "tickSec": 20, "windowSec": 150,
    "minLines": 2, "minGapSec": 240, "perHour": 8, "maxChars": 60
  },
  "audio": { "model": "small", "device": "auto", "window": 15, "overlap": 2, "keep": 300 }
}
```

- `sendEnabled`：发弹幕总开关。默认 `false`（先看后发）。
- `botName`：它在房里的名字，用来判断「有人点名」。
- `webhookAllowlist`：挂机叫醒允许 POST 的地址（v0.4.1）。空数组 = 谁都不叫醒；只有人能从这里加，模型加的会被拒。
- `autoChat.minGapSec` / `perHour` / `maxChars`：两次开口的间隔、每小时上限、单句字数上限。
- `audio.device`：`auto`（先试 GPU，失败退 CPU）/ `cpu` / `cuda`；`window` 每段秒数；`overlap` 相邻段重叠；`keep` 转录文本保留秒数。

## 安全：设置页那几个接口怎么守的（v0.3.0 起，v0.4.1 补 webhook）

插件在宿主里注册了 `/dsh-bili-live/*` 一组本地 HTTP 接口，给设置页用。这些接口能改配置、能动放行名单、能发弹幕，所以不是"能连上本机端口就算数"：

1. **先过 DSH 的信任栅栏** `connection.requestRejection` —— 宿主级鉴权（认 cookie/token），隧道或反代把 Host 改写成回环也挡得住；
2. **再过本机同源** —— Host 必须是回环地址，Origin/Referer 必须同端口（挡掉浏览器里另一个本地页面借你的登录态驱动这些写接口）；
3. **认证不可用时拒绝访问**：拿不到宿主认证服务时，所有插件 HTTP 接口返回 503，不再降级为“本机同源即放行”；认证调用抛错或返回拒绝结果时拒绝访问。只有认证明确通过后才检查本机同源。

另外 **网页端不许把插件指到任意可执行程序**：`pythonPath` 与 `audio.asrPython` 仅接受插件固定 venv 解释器，或本机配置文件/环境变量已明确指定的解释器。必须为存在的绝对文件路径；比较真实路径，固定 venv 内的路径不得通过符号链接逃出 venv。取消首次配置例外，不按文件名或任意目录前缀放行；其它路径返回 403，要新增信任路径请由管理员直接改本机配置。普通 `/config` 与听觉启动接口均执行相同校验。

发弹幕的房间授权不在这层放宽：真正的闸在 `python/live_mcp.py` 的 `bili_live_send` —— 房间不在 `live-allow.json` 里一律拒绝。

**webhook 也是同一套思路（v0.4.1）**：挂机 worker 会真的去 POST 一个地址、还把响应写进状态日志，所以目标不能由模型随口指定。

1. 目标必须落在 `config.json` 的 `webhookAllowlist` 里（设置页的「挂机叫醒 · webhook 白名单」是人填的，模型的写配置入口没有浏览器登录态）；
2. JS 侧（`lib/webhook.mjs`）在工具入口、`/dsh-bili-live/watch`、`/dsh-bili-live/config` 三处各核一次，不通过就 `webhook_not_allowed` / 403，worker 不会被拉起；
3. Python 侧在 `bili_live_watch_start` 和 worker 的 `_wake` 里再各核一次（worker 不信一次性的入口检查）；
4. 响应最多读 4 KB、落日志再截到 500 字，**不跟随 3xx 跳转**（`_NoRedirect`），超时 10 秒。

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

## 限制（有意不做）

- **不看画面**：不抽帧、不做视觉理解。
- **听声音是可选的**（v0.4）：要自己装 ffmpeg + 一个转写引擎；不装也能正常用弹幕那一套。
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