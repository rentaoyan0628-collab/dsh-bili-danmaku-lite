/**
 * dsh-bili-live —— host 半端（B站直播间：看弹幕 / 发弹幕 / 挂着听 / 自动陪聊）
 * ============================================================================
 * 为什么要它：原先这套东西长在 qq-bridge 里 —— 换台机器、或者不想装 QQ 桥接的人
 * 就只能看着 skill 干瞪眼。这里把「能力」抽出来做成独立插件（v0.2 弹幕；v0.4 加可选「听声音」转写）：
 *
 *   1. 插件自己用 spawn 起那份 Python MCP（python/live_mcp.py），把它的能力注册成
 *      DSH 工具（info / feed / send / request / watch_* / log / watching）。
 *   2. 放行名单是人的闸：模型只能 bili_live_request 申请，放行只在设置页里点。
 *   3. 「挂着听」= 后台 worker（python/live_chat_worker.py）收弹幕、写 hint；
 *      够得着时 POST 到用户自己的 webhook（没配就只写 hint，不打扰任何人）。
 *   4. 自动陪聊（可选）：host 这半读增量弹幕 → 调一次模型判断要不要接话 →
 *      把要发的那句写进 queue.jsonl，由 worker 按节流发出去。
 *
 * 家里的东西（$DSH_HOME/bili-live/，可用 DSH_BILI_LIVE_HOME 改）：
 *   config.json            插件配置（设置页读写）
 *   bili_credential.json   B站 登录凭证（扫码/手填，落在本机，不进仓库）
 *   live-allow.json        放行名单（只有人能改）
 *   live-requests.jsonl    待批申请（模型写的）
 *   live-chat/<房间>/      每个房间的弹幕流、hint、queue、watch.json
 *   logs/                  danmaku.jsonl（发了什么）+ plugin.log（插件自己的）
 * ============================================================================
 */
import { loadHostDeps } from './lib/hostdeps.mjs'

// defineTool 从宿主树里取（拿不到就用本地等价实现）—— 裸 import 会让整条插件不激活
const hostDeps = await loadHostDeps()
const defineTool = hostDeps.defineTool

import { spawn } from 'node:child_process'
import { appendFileSync, existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, statSync, writeFileSync } from 'node:fs'
import { basename, dirname, isAbsolute, join, resolve, sep } from 'node:path'
import { homedir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'
import { McpClient } from './lib/mcp.mjs'
import { MAX_ALLOW_ENTRIES, formatAllowEntry, isAllowed as webhookAllowed, normalizeAllowlist, parseTarget } from './lib/webhook.mjs'

export const name = 'bili-live'
// tools / webServer 是必需的；llm 只在「自动陪聊」时才用，所以不写进 inject，
// 没有它的宿主照样装得上，只是自动陪聊在设置页里提示「这台宿主没有可用的模型服务」。
export const inject = ['webServer', 'tools']

const HERE = dirname(fileURLToPath(import.meta.url))
const PY_DIR = join(HERE, 'python')
const MCP_SCRIPT = join(PY_DIR, 'live_mcp.py')
const WORKER_SCRIPT = join(PY_DIR, 'live_chat_worker.py')

const DSH_HOME = process.env.DSH_HOME || join(homedir(), '.dsh')
const HOME = process.env.DSH_BILI_LIVE_HOME || join(DSH_HOME, 'bili-live')
const WATCH_ROOT = join(HOME, 'live-chat')
const LOG_DIR = join(HOME, 'logs')
const CONFIG_FILE = join(HOME, 'config.json')
const ALLOW_FILE = join(HOME, 'live-allow.json')
const REQUEST_FILE = join(HOME, 'live-requests.jsonl')
const CRED_FILE = join(HOME, 'bili_credential.json')
const DANMAKU_LOG = join(LOG_DIR, 'danmaku.jsonl')
const PLUGIN_LOG = join(LOG_DIR, 'plugin.log')
const STATE_FILE = join(HOME, 'state', 'plugin-state.json')

const DEFAULTS = {
  pythonPath: '',
  botName: '大肥鱼',
  // 发弹幕的总开关：关着的时候模型和设置页都发不出去（默认关，先看后发）
  sendEnabled: false,
  // 挂机「叫醒」的 webhook 白名单：只允许 POST 到这里列出的地址。
  // 模型不能自己往里加 —— 只有本机配置文件 / 设置页（人在操作）能改。
  webhookAllowlist: [],
  // 自动陪聊（默认关；开了也要 sendEnabled）
  autoChat: {
    enabled: false,
    provider: '',
    model: '',
    persona: '你是直播间里的一个普通观众，说话短、口语、不客套。',
    tickSec: 20,
    windowSec: 150,
    minLines: 2,
    minGapSec: 240,
    perHour: 8,
    maxChars: 60,
  },
  // 听声音（v0.4.0）：只转写，不发弹幕。device=auto 时先试 GPU，失败自动退 CPU。
  audio: { model: 'small', device: 'auto', window: 15, overlap: 2, keep: 300, asrPython: '' },
}

/* ────────────────────────── 小工具 ────────────────────────── */

function log(msg) {
  const line = '[' + new Date().toISOString() + '] ' + String(msg) + '\n'
  try {
    mkdirSync(LOG_DIR, { recursive: true })
    appendFileSync(PLUGIN_LOG, line, 'utf8')
  } catch { /* 日志写不进去不能影响功能 */ }
}

function readJson(path, fallback) {
  try { return JSON.parse(readFileSync(path, 'utf8')) } catch { return fallback }
}

function writeJson(path, value) {
  try {
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, JSON.stringify(value, null, 2), 'utf8')
    return true
  } catch (e) { log('写 ' + path + ' 失败：' + (e?.message ?? e)); return false }
}

function readJsonl(path, limit = 200) {
  try {
    const lines = readFileSync(path, 'utf8').split('\n').filter((x) => x.trim())
    return lines.slice(-limit).map((x) => { try { return JSON.parse(x) } catch { return null } }).filter(Boolean)
  } catch { return [] }
}

function tailFile(path, n = 30) {
  try {
    return readFileSync(path, 'utf8').split('\n').filter((x) => x.trim()).slice(-n)
  } catch { return [] }
}

function pidAlive(pid) {
  const p = Number(pid) || 0
  if (!p) return false
  try { process.kill(p, 0); return true } catch { return false }
}

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v))

/* ────────────────────────── 配置 ────────────────────────── */

let cfg = loadCfg()

function normalizeCfg(raw) {
  const r = (raw && typeof raw === 'object') ? raw : {}
  const ac = (r.autoChat && typeof r.autoChat === 'object') ? r.autoChat : {}
  const d = DEFAULTS.autoChat
  const au = (r.audio && typeof r.audio === 'object') ? r.audio : {}
  const da = DEFAULTS.audio
  return {
    pythonPath: String(r.pythonPath || '').trim(),
    botName: String(r.botName || DEFAULTS.botName).trim().slice(0, 20) || DEFAULTS.botName,
    sendEnabled: Boolean(r.sendEnabled),
    webhookAllowlist: normalizeAllowlist(Array.isArray(r.webhookAllowlist) ? r.webhookAllowlist : [])
      .map(formatAllowEntry),
    autoChat: {
      enabled: Boolean(ac.enabled),
      provider: String(ac.provider || '').trim().slice(0, 60),
      model: String(ac.model || '').trim().slice(0, 80),
      persona: String(ac.persona || d.persona).trim().slice(0, 400) || d.persona,
      tickSec: clamp(Math.round(Number(ac.tickSec) || d.tickSec), 10, 600),
      windowSec: clamp(Math.round(Number(ac.windowSec) || d.windowSec), 30, 900),
      minLines: clamp(Math.round(Number(ac.minLines) || d.minLines), 1, 20),
      minGapSec: clamp(Math.round(Number(ac.minGapSec) || d.minGapSec), 60, 7200),
      perHour: clamp(Math.round(Number(ac.perHour) || d.perHour), 1, 60),
      maxChars: clamp(Math.round(Number(ac.maxChars) || d.maxChars), 10, 40),
    },
    audio: {
      model: String(au.model || da.model).trim().slice(0, 20) || da.model,
      device: ['auto', 'cpu', 'cuda'].includes(String(au.device)) ? String(au.device) : da.device,
      window: clamp(Math.round(Number(au.window) || da.window), 5, 60),
      overlap: clamp(Number(au.overlap) ?? da.overlap, 0, 10),
      keep: clamp(Math.round(Number(au.keep) || da.keep), 60, 1800),
      // 听觉 worker 用哪个解释器（要装 faster-whisper 的那个 venv）。空=跟插件自己的 Python 一样。
      asrPython: String(au.asrPython || '').trim().slice(0, 300),
    },
  }
}

function loadCfg() {
  return normalizeCfg(readJson(CONFIG_FILE, {}))
}

function persistCfg(patch) {
  const next = normalizeCfg({
    ...cfg,
    ...(patch || {}),
    autoChat: { ...cfg.autoChat, ...((patch && patch.autoChat) || {}) },
    audio: { ...cfg.audio, ...((patch && patch.audio) || {}) },
  })
  cfg = next
  writeJson(CONFIG_FILE, next)
  return next
}

/* ────────────────────────── Python 环境 ────────────────────────── */

function pythonCandidates() {
  const out = []
  const push = (p) => { const s = String(p || '').trim(); if (s && !out.includes(s)) out.push(s) }
  push(cfg.pythonPath)
  push(process.env.DSH_BILI_LIVE_PYTHON)
  push(join(HOME, '.venv', 'Scripts', 'python.exe'))
  push(join(HOME, '.venv', 'bin', 'python'))
  push('python')
  push('python3')
  return out
}

/** Web requests may select only fixed venv interpreters or explicitly trusted local configuration.
 * No first-run exception, basename-only approval, arbitrary subtree, or PATH aliases.
 */
function interpreterPathAllowed(next, trusted = []) {
  const p = String(next || '').trim()
  if (!p) return { ok: true }
  const deny = { ok: false, error: '网页端只允许插件固定 venv 解释器或已由本机配置明确指定的解释器；其它路径请由管理员直接修改本机配置文件。' }
  if (!isAbsolute(p)) return deny
  try {
    if (!statSync(p).isFile()) return deny
    const canonical = (v) => {
      const value = realpathSync(v)
      return process.platform === 'win32' ? value.toLowerCase() : value
    }
    const target = canonical(p)
    // An existing local config/environment is trusted input, never discovered from a web request.
    for (const candidate of trusted) {
      if (candidate && isAbsolute(candidate)) {
        try { if (target === canonical(candidate) && statSync(candidate).isFile()) return { ok: true } } catch { /* unavailable */ }
      }
    }
    const venv = realpathSync(join(HOME, '.venv'))
    const root = process.platform === 'win32' ? venv.toLowerCase() : venv
    for (const fixed of [join(HOME, '.venv', 'Scripts', 'python.exe'), join(HOME, '.venv', 'bin', 'python'), join(HOME, '.venv', 'bin', 'python3')]) {
      try {
        const actual = canonical(fixed)
        if (actual.startsWith(root + sep) && actual === target) return { ok: true }
      } catch { /* absent interpreter */ }
    }
  } catch { /* missing file or invalid path: fail closed */ }
  return deny
}

function pythonPathAllowed(next) {
  return interpreterPathAllowed(next, [cfg.pythonPath, process.env.DSH_BILI_LIVE_PYTHON])
}

function asrPythonAllowed(next) {
  return interpreterPathAllowed(next, [cfg.audio.asrPython, cfg.pythonPath, process.env.DSH_BILI_LIVE_ASR_PYTHON, process.env.DSH_BILI_LIVE_PYTHON])
}

function pythonEnv() {
  return {
    ...process.env,
    DSH_BILI_LIVE_HOME: HOME,
    DSH_BILI_LIVE_WORKER: WORKER_SCRIPT,
    DSH_BILI_LIVE_AUDIO: join(PY_DIR, 'live_audio.py'),
    DSH_BILI_LIVE_AUDIO_PROBE: join(PY_DIR, 'asr_probe.py'),
    // 空串会被 python 侧当成没设，回落到插件自己的解释器
    DSH_BILI_LIVE_ASR_PYTHON: cfg.audio.asrPython || '',
    DSH_BILI_LIVE_NAME: cfg.botName,
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONUNBUFFERED: '1',
  }
}

/** 跑一小段 python（自检用），返回 { ok, code, out, err } */
function runPython(exe, args, timeoutMs = 20000) {
  return new Promise((resolve) => {
    let done = false
    let out = ''
    let err = ''
    let child
    const finish = (r) => { if (!done) { done = true; resolve(r) } }
    try {
      child = spawn(exe, args, { cwd: HOME, env: pythonEnv(), windowsHide: true })
    } catch (e) {
      resolve({ ok: false, code: -1, out: '', err: String(e?.message ?? e) })
      return
    }
    const timer = setTimeout(() => {
      try { child.kill() } catch { /* 已经没了 */ }
      finish({ ok: false, code: -1, out, err: err + '\n[超时 ' + timeoutMs + 'ms]' })
    }, timeoutMs)
    child.stdout.on('data', (c) => { out = (out + c).slice(-8000) })
    child.stderr.on('data', (c) => { err = (err + c).slice(-8000) })
    child.on('error', (e) => { clearTimeout(timer); finish({ ok: false, code: -1, out, err: err + '\n' + String(e?.message ?? e) }) })
    child.on('exit', (code) => { clearTimeout(timer); finish({ ok: code === 0, code, out, err }) })
  })
}

const DOCTOR_SNIPPET = 'import sys,json;\n'
  + 'mods={}\n'
  + 'for m in ("bilibili_api","mcp"):\n'
  + '    try:\n'
  + '        __import__(m); mods[m]=True\n'
  + '    except Exception as e:\n'
  + '        mods[m]=str(e)\n'
  + 'print(json.dumps({"python":sys.version.split()[0],"mods":mods},ensure_ascii=False))'

/** 环境自检：找一个能用、且装了 bilibili_api + mcp 的 python */
async function doctor() {
  const tried = []
  for (const exe of pythonCandidates()) {
    const r = await runPython(exe, ['-c', DOCTOR_SNIPPET], 20000)
    let info = null
    try { info = JSON.parse((r.out || '').trim().split('\n').pop() || 'null') } catch { info = null }
    const mods = (info && info.mods) || {}
    const missing = Object.keys(mods).filter((k) => mods[k] !== true)
    const item = {
      python: exe,
      ok: Boolean(r.ok && info && missing.length === 0),
      version: (info && info.python) || null,
      missing,
      stderr: (r.err || '').trim().slice(-400) || undefined,
    }
    tried.push(item)
    if (item.ok) return { ok: true, python: exe, version: item.version, tried }
  }
  return { ok: false, python: null, tried }
}

/* ────────────────────────── MCP ────────────────────────── */

let mcp = null
let mcpKey = ''
let mcpExe = ''

async function ensureMcp() {
  const key = cfg.pythonPath + '|' + MCP_SCRIPT
  if (mcp && mcp.running && mcpKey === key) return mcp
  if (mcp) { try { mcp.stop() } catch { /* 已经没了 */ } mcp = null }
  if (!existsSync(MCP_SCRIPT)) throw new Error('插件包里没有 python/live_mcp.py（应当是 ' + MCP_SCRIPT + '）')
  let exe = cfg.pythonPath || mcpExe
  if (!exe) {
    const d = await doctor()
    if (!d.ok) {
      throw new Error('找不到能用的 Python：需要装了 bilibili_api 和 mcp 的解释器。'
        + '去设置页「环境自检」看哪一步缺，或把解释器路径填进「Python 路径」。')
    }
    exe = d.python
    mcpExe = exe
  }
  mcpKey = key
  mcp = new McpClient({
    command: exe,
    args: [MCP_SCRIPT],
    cwd: HOME,
    env: pythonEnv(),
    log: (m) => log('MCP: ' + m),
  })
  return mcp
}

/** 拿 llm 服务；没注入 / 宿主没这个服务时返回 null（自动陪聊会给出可读的提示） */
function getLlm() {
  try { return (ctx && typeof ctx.get === 'function') ? (ctx.get('llm') || null) : null } catch { return null }
}

async function call(toolName, args, opts) {
  const client = await ensureMcp()
  return client.call(toolName, args, opts)
}

/* ────────────────────────── 放行名单 / 待批申请 ────────────────────────── */

function loadAllow() {
  const data = readJson(ALLOW_FILE, null)
  if (data && typeof data === 'object') return { enforce: data.enforce !== false, rooms: data.rooms || {} }
  return { enforce: true, rooms: {} }
}

function loadRequests(limit = 50) {
  return readJsonl(REQUEST_FILE, 500).slice(-limit)
}

/* ────────────────────────── 挂机状态 ────────────────────────── */

function watchDirs() {
  try {
    return readdirSync(WATCH_ROOT, { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => ({ room: d.name, dir: join(WATCH_ROOT, d.name) }))
  } catch { return [] }
}

function watchSnapshot(room, dir) {
  const meta = readJson(join(dir, 'watch.json'), {})
  const hint = readJson(join(dir, 'hint.json'), {})
  const pid = Number(meta.pid) || 0
  const count = (f) => {
    try { return readFileSync(join(dir, f), 'utf8').split('\n').filter((x) => x.trim()).length } catch { return 0 }
  }
  return {
    room_id: meta.room || Number(room) || room,
    anchor: meta.anchor || null,
    pid,
    alive: pidAlive(pid),
    started_at: meta.started_at || null,
    webhook: meta.webhook || null,
    danmaku_count: count('danmaku.jsonl'),
    sent_count: count('sent.jsonl'),
    wake_count: count('wake.jsonl'),
    last_hint: Object.keys(hint || {}).length ? hint : null,
    last_log: tailFile(join(dir, 'worker.log'), 3),
  }
}

function watchingList() {
  return watchDirs().map(({ room, dir }) => watchSnapshot(room, dir))
}

/* ────────────────────────── 统计 ────────────────────────── */

function sendStats() {
  const today = new Date().toISOString().slice(0, 10)
  const items = readJsonl(DANMAKU_LOG, 2000)
  const mine = items.filter((x) => String(x.ts || '').slice(0, 10) === today)
  const by = {}
  for (const x of mine) by[x.result || 'unknown'] = (by[x.result || 'unknown'] || 0) + 1
  return { today, total: mine.length, by_result: by, recent: items.slice(-8).reverse() }
}

function pluginState() {
  return readJson(STATE_FILE, { auto: {}, cursor: {} })
}

function savePluginState(next) {
  writeJson(STATE_FILE, next)
}

/* ────────────────────────── 自动陪聊 ────────────────────────── */

const autoState = {
  busy: false,
  lastTickAt: 0,
  lastError: '',
  cursor: {},
}

// 每个房间的上次接话时间 / 这一小时的次数：落盘，重启不重置
;(function bootAutoState() {
  const st = pluginState()
  autoState.cursor = st.cursor || {}
  for (const [room, v] of Object.entries(st.auto || {})) {
    autoState['r' + room] = { lastReplyAt: Number(v.lastReplyAt) || 0, times: Array.isArray(v.times) ? v.times : [] }
  }
})()

function autoRoomState(room) {
  const k = 'r' + room
  if (!autoState[k]) autoState[k] = { lastReplyAt: 0, times: [] }
  return autoState[k]
}

function persistAutoState() {
  const st = pluginState()
  st.cursor = autoState.cursor
  st.auto = {}
  for (const [k, v] of Object.entries(autoState)) {
    if (k.charAt(0) === 'r' && typeof v === 'object' && v) st.auto[k.slice(1)] = { lastReplyAt: v.lastReplyAt, times: v.times }
  }
  savePluginState(st)
}

/** 读某个房间自上次以来新到的弹幕（按字节游标，日志被截断就从头读） */
function readFreshDanmaku(room, dir) {
  const file = join(dir, 'danmaku.jsonl')
  let size = 0
  try { size = statSync(file).size } catch { return [] }
  let pos = Number(autoState.cursor[room]) || 0
  if (pos > size) pos = 0
  if (pos === size) return []
  let buf = ''
  try {
    const bytes = readFileSync(file)
    buf = bytes.subarray(pos).toString('utf8')
  } catch { return [] }
  autoState.cursor[room] = size
  const out = []
  for (const line of buf.split('\n')) {
    const t = line.trim()
    if (!t) continue
    try { out.push(JSON.parse(t)) } catch { /* 半行，下一次再读 */ }
  }
  return out
}

function freshWithin(recs, windowSec) {
  const now = Date.now()
  const out = []
  for (const r of recs) {
    const ts = String(r.ts || '')
    // worker 写的是本地时间 "%Y-%m-%d %H:%M:%S"
    const d = new Date(ts.replace(' ', 'T'))
    const t = Number.isNaN(d.getTime()) ? now : d.getTime()
    if (now - t <= windowSec * 1000) out.push({ ...r, _t: t })
  }
  return out
}

/* ────────────────────── 听声音（v0.4.0，只转写不发弹幕）────────────────────── */

/** 某个房间的听觉状态：只读 worker 落的 audio.json / audio.jsonl，不起任何进程 */
function audioSnapshot(room) {
  const dir = join(WATCH_ROOT, String(room))
  const meta = readJson(join(dir, 'audio.json'), {})
  const keys = Object.keys(meta || {}).length
  const pid = Number(meta.pid) || 0
  const recent = tailFile(join(dir, 'audio.jsonl'), 5)
    .map((line) => { try { return JSON.parse(line) } catch { return null } })
  return {
    room_id: String(meta.room || room),
    pid,
    alive: pidAlive(pid),
    state: String(meta.state || (keys ? 'stopped' : 'idle')),
    engine: String(meta.engine || ''),
    device: String(meta.device || ''),
    model: String(meta.model || ''),
    window: Number(meta.window) || 0,
    started_at: meta.started_at || '',
    last_ok_at: meta.last_ok_at || '',
    device_error: String(meta.device_error || ''),
    last_error: String(meta.last_error || ''),
    segments: Number(meta.segments) || 0,
    chars: Number(meta.chars) || 0,
    recent: recent.filter(Boolean),
  }
}

function audioList() {
  try {
    if (!existsSync(WATCH_ROOT)) return []
    return readdirSync(WATCH_ROOT, { withFileTypes: true })
      .filter((e) => e.isDirectory() && existsSync(join(WATCH_ROOT, e.name, 'audio.json')))
      .map((e) => audioSnapshot(e.name))
  } catch { return [] }
}

/** 最近这段直播里都在说什么（语音转写，可能带错字）。给自动陪聊当上下文 */
function freshTranscript(room, windowSec, limit = 10) {
  const dir = join(WATCH_ROOT, String(room))
  const lines = tailFile(join(dir, 'audio.jsonl'), 60)
  const recs = []
  for (const line of lines) {
    try { recs.push(JSON.parse(line)) } catch { /* 半行 */ }
  }
  return freshWithin(recs, windowSec).slice(-limit)
}

const AUTO_SYS = [
  '你在一个 B站 直播间的弹幕里，是个普通观众，不是主播、不是助理。',
  '你要决定：这一刻要不要接一句话；要接就说一句短的（不超过 {max} 个字），不接就只回 SKIP。',
  '规矩：',
  '1) 大多数时候应该 SKIP。别人互相聊、复读、刷屏、广告、纯表情、跟主播互动 —— 都不用你插嘴。',
  '2) 只有真的有你插一句会更有意思、或者有人在问一个能一句话答的问题时才开口。',
  '3) 不复读、不总结、不当客服、不说「作为AI」、不要表情包、不要 @ 谁。',
  '4) 同一件事别连着说；上一条你自己刚说过就先沉默。',
  '5) 消息里可能附了「直播里正在说的」——那是语音转写，有错字、会漏句。可以顺着它的话接，但别当成画面描述，别编你没听到的内容。',
  '只输出一行：要么是那句弹幕，要么正好是 SKIP。',
].join('\n')

async function askLlm(llm, sys, user) {
  const A = cfg.autoChat
  if (!llm || typeof llm.stream !== 'function') return { ok: false, error: '这台宿主没有可用的模型服务（没有 llm）' }
  if (!A.provider || !A.model) return { ok: false, error: '还没选自动陪聊用的模型（设置页里选 provider/model）' }
  const messages = [{
    role: 'user',
    id: randomUUID(),
    content: [{ type: 'text', text: user }],
    source: { kind: 'plugin', plugin: 'dsh-bili-danmaku-lite' },
  }]
  let out = ''
  try {
    for await (const chunk of llm.stream({
      provider: A.provider,
      model: A.model,
      system: sys,
      messages,
      maxTokens: 200,
      temperature: 0.9,
    })) {
      if (!chunk) continue
      if (chunk.type === 'text-delta') out += chunk.text
      if (chunk.type === 'finish' && chunk.reason && chunk.reason.kind === 'error') {
        const fail = chunk.reason.failure || {}
        return { ok: false, error: String(fail.message || 'model error') }
      }
    }
  } catch (e) {
    return { ok: false, error: String(e?.message ?? e) }
  }
  return { ok: true, text: out.trim() }
}

function sanitizeReply(text, maxChars) {
  let t = String(text || '').trim()
  if (!t) return ''
  const firstLine = t.split('\n').map((x) => x.trim()).filter(Boolean)[0] || ''
  t = firstLine.replace(/^["'「『]|["'」』]$/g, '').trim()
  if (/^SKIP$/i.test(t)) return ''
  if (t.length > maxChars) t = t.slice(0, maxChars)
  return t
}

async function autoTick(llm) {
  const A = cfg.autoChat
  if (!A.enabled || !cfg.sendEnabled) return
  const alive = watchingList().filter((w) => w.alive)
  if (!alive.length) return
  for (const w of alive) {
    const room = String(w.room_id)
    const dir = join(WATCH_ROOT, room)
    const recs = freshWithin(readFreshDanmaku(room, dir), A.windowSec)
    // v0.4.0：只有画面里在说话、没人发弹幕时也可以接一句
    const heard = freshTranscript(room, A.windowSec, 10)
    if (!recs.length && !heard.length) continue
    const atMe = recs.filter((r) => r.at_me)
    if (!atMe.length && recs.length < A.minLines && !heard.length) continue
    const st = autoRoomState(room)
    const now = Date.now()
    if (now - st.lastReplyAt < A.minGapSec * 1000) continue
    st.times = st.times.filter((t) => now - t < 3600 * 1000)
    if (st.times.length >= A.perHour) continue
    const lines = recs.slice(-12).map((r) => (r.uname ? r.uname + '：' : '') + String(r.text || '').slice(0, 100))
    const heardLines = heard.map((r) => '（直播里）' + String(r.text || '').slice(0, 120))
    const sys = AUTO_SYS.split('{max}').join(String(A.maxChars))
    const user = (A.persona ? '你在这间房的人设：' + A.persona + '\n\n' : '')
      + '最近这一小段弹幕（时间从旧到新）：\n' + (lines.length ? lines.join('\n') : '（这段时间没人发弹幕）')
      + (heardLines.length ? '\n\n直播里正在说的（语音转写，可能有错字）：\n' + heardLines.join('\n') : '')
      + '\n\n要不要接一句？只回那一句，或者 SKIP。'
    const r = await askLlm(llm, sys, user)
    if (!r.ok) { autoState.lastError = r.error; log('自动陪聊跳过：' + r.error); continue }
    const say = sanitizeReply(r.text, A.maxChars)
    if (!say) { log('自动陪聊：模型决定 SKIP（房间 ' + room + '）'); continue }
    // 交给 worker 发：节流 / 放行校验都在 worker 那一侧
    try {
      appendFileSync(join(dir, 'queue.jsonl'), JSON.stringify({ id: randomUUID(), text: say }) + '\n', 'utf8')
    } catch (e) {
      autoState.lastError = '写 queue 失败：' + String(e?.message ?? e)
      log(autoState.lastError)
      continue
    }
    st.lastReplyAt = now
    st.times.push(now)
    persistAutoState()
    log('自动陪聊：往 ' + room + ' 排了一句「' + say + '」')
  }
}

/* ────────────────────────── 工具 ────────────────────────── */

const textOutput = {
  schema: { type: 'object', properties: {}, additionalProperties: true },
  render: (_args, value) => [{
    type: 'text',
    text: typeof value === 'string' ? value : JSON.stringify(value, null, 2),
  }],
}

const safe = (v) => {
  if (v === undefined || v === null || typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return v
  try { return JSON.parse(JSON.stringify(v)) } catch { return String(v) }
}

const jsonish = (v) => {
  if (typeof v === 'string') { try { return JSON.parse(v) } catch { return { ok: true, raw: v } } }
  return v ?? { ok: true }
}

function registerTools() {
  const tool = (spec) => ctx.tools.register(defineTool({
    ...spec,
    output: spec.output || textOutput,
    async execute(args, exec) { return safe(await spec.execute(args || {}, exec)) },
  }))

  tool({
    name: 'bili_live_info',
    description: '查直播间信息（是否在播、标题、分区、在线人数）和最近弹幕历史。只读，不会发送任何内容。',
    parameters: {
      room_id: { type: 'string', description: '直播间号 / UP主昵称 / UID；留空用默认房间' },
      history_size: { type: 'number', description: '返回最近几条弹幕（1-20）' },
    },
    async execute(args) {
      return jsonish(await call('bili_live_info', {
        room_id: String(args.room_id ?? ''),
        history_size: clamp(Number(args.history_size) || 10, 1, 20),
      }))
    },
  })

  tool({
    name: 'bili_live_feed',
    description: '监听直播间实时消息若干秒（弹幕/礼物/SC/进场），只读。',
    parameters: {
      room_id: { type: 'string', description: '直播间号 / UP主昵称 / UID' },
      seconds: { type: 'number', description: '监听秒数，5-120，默认 20' },
      max_items: { type: 'number', description: '最多返回多少条弹幕，默认 40' },
    },
    async execute(args) {
      return jsonish(await call('bili_live_feed', {
        room_id: String(args.room_id ?? ''),
        seconds: clamp(Number(args.seconds) || 20, 5, 120),
        max_items: clamp(Number(args.max_items) || 40, 1, 200),
      }, { timeoutMs: 180000 }))
    },
  })

  tool({
    name: 'bili_live_send',
    description: '往放行过的直播间发一条弹幕。默认只出草稿；必须 confirm=true 才真发。'
      + '同一房间 60 秒内只能发 1 条，发出去删不掉。',
    parameters: {
      room_id: { type: 'string', description: '直播间号 / UP主昵称 / UID' },
      text: { type: 'string', description: '弹幕内容，1-40 字' },
      confirm: { type: 'boolean', description: '真要发就传 true' },
    },
    async execute(args) {
      if (!cfg.sendEnabled) {
        return { ok: false, error: '发弹幕的总开关是关的。要发先去 DSH 设置页 →「B站直播」→ 打开「允许发弹幕」。' }
      }
      return jsonish(await call('bili_live_send', {
        room_id: String(args.room_id ?? ''),
        text: String(args.text ?? ''),
        confirm: args.confirm === true,
      }))
    },
  })

  tool({
    name: 'bili_live_request',
    description: '向人申请进入一个还没放行的直播间（放行由人在设置页里点，模型自己开不了门）。'
      + '先问人一句，得到同意再调这个；它只写一条待批申请。',
    parameters: {
      room_id: { type: 'string', description: '直播间号 / UP主昵称 / UID' },
      reason: { type: 'string', description: '为什么想进（只记日志）' },
      until_offline: { type: 'boolean', description: 'True=只待这一场（默认）' },
    },
    async execute(args) {
      return jsonish(await call('bili_live_request', {
        room_id: String(args.room_id ?? ''),
        reason: String(args.reason ?? ''),
        until_offline: args.until_offline !== false,
      }))
    },
  })

  tool({
    name: 'bili_live_watch_start',
    description: '在放行过的直播间「挂着听」：起一个后台 worker 收弹幕，够得着时 POST 到你的 webhook。'
      + '房间没开播会被拦下。webhook 只能是用户在配置里预先放行的地址（webhookAllowlist），'
      + '模型不能自己指定新地址；留空=只写 hint、不叫醒任何人。',
    parameters: {
      room_id: { type: 'string', description: '直播间号 / UP主昵称 / UID' },
      webhook: { type: 'string', description: '够得着时 POST 到这个地址；必须在 webhookAllowlist 里；留空=只写 hint' },
      wake_cooldown: { type: 'number', description: '两次叫醒最少间隔秒数（默认 90）' },
      judge_sec: { type: 'number', description: '多久看一次增量（默认 60）' },
      max_wakes_per_hour: { type: 'number', description: '每小时最多叫醒几次（默认 30）' },
    },
    async execute(args) {
      const webhook = String(args.webhook ?? '').trim()
      // ⭐ 0.4.1：模型给的 webhook 必须命中用户预先配的白名单，否则连 worker 都不拉。
      if (webhook) {
        const gate = webhookAllowed(webhook, cfg.webhookAllowlist)
        if (!gate.ok) return jsonish({ ok: false, status: 'webhook_not_allowed', error: gate.error, webhookAllowlist: cfg.webhookAllowlist })
      }
      return jsonish(await call('bili_live_watch_start', {
        room_id: String(args.room_id ?? ''),
        webhook,
        wake_cooldown: Number(args.wake_cooldown) || 90,
        judge_sec: Number(args.judge_sec) || 60,
        max_wakes_per_hour: Number(args.max_wakes_per_hour) || 30,
      }))
    },
  })

  tool({
    name: 'bili_live_watch_status',
    description: '看挂机状态：哪个房间挂着、worker 还活着吗、收了多少条弹幕、发了几条、叫醒过几次、最近 hint。只读。',
    parameters: { room_id: { type: 'string', description: '留空=看全部' } },
    async execute(args) {
      return jsonish(await call('bili_live_watch_status', { room_id: String(args.room_id ?? '') }))
    },
  })

  tool({
    name: 'bili_live_watch_stop',
    description: '让挂机 worker 收工（写 stop.flag，等它自己退，40 秒不退就强杀）。',
    parameters: { room_id: { type: 'string', description: '留空=停全部' } },
    async execute(args) {
      return jsonish(await call('bili_live_watch_stop', { room_id: String(args.room_id ?? '') }))
    },
  })

  tool({
    name: 'bili_live_log',
    description: '读最近的弹幕发送日志（含草稿/被拦/失败），只读。',
    parameters: {
      num: { type: 'number', description: '最近几条，默认 20' },
      room_id: { type: 'string', description: '只看某个房间；留空=全部' },
    },
    async execute(args) {
      return jsonish(await call('bili_live_log', {
        num: clamp(Number(args.num) || 20, 1, 100),
        room_id: String(args.room_id ?? ''),
      }))
    },
  })

  tool({
    name: 'bili_live_watching',
    description: '我此刻在看的直播间有哪些（放行名单 + 每个房间现在开没开播 + 最近说过什么）。'
      + '被问「你在看什么直播」时先调这个。',
    parameters: {},
    async execute() {
      return jsonish(await call('bili_live_watching', {}))
    },
  })

  tool({
    name: 'bili_live_audio_start',
    description: '给放行过的直播间装上「耳朵」：后台把直播声音转成带时间戳的文字（只转写，不发弹幕）。'
      + 'device=auto 先试 GPU，AMD 卡会失败并自动退回 CPU，原始报错会一起返回。',
    parameters: {
      room_id: { type: 'string', description: '直播间号 / UP主昵称 / UID' },
      model: { type: 'string', description: '识别模型 tiny/base/small/medium，默认 small' },
      device: { type: 'string', description: 'auto（默认）/ cpu / cuda' },
      window: { type: 'number', description: '每段音频秒数，默认 15' },
      overlap: { type: 'number', description: '相邻段重叠秒数，默认 2' },
      keep: { type: 'number', description: '转录文本保留秒数，默认 300' },
    },
    async execute(args) {
      const a = cfg.audio
      return jsonish(await call('bili_live_audio_start', {
        room_id: String(args.room_id ?? ''),
        model: String(args.model ?? a.model),
        device: String(args.device ?? a.device),
        window: clamp(Number(args.window) || a.window, 5, 60),
        overlap: clamp(Number(args.overlap) ?? a.overlap, 0, 10),
        keep: clamp(Number(args.keep) || a.keep, 60, 1800),
      }, { timeoutMs: 60000 }))
    },
  })

  tool({
    name: 'bili_live_audio_stop',
    description: '关掉语音转写（留空=全部房间）。只停耳朵，不影响弹幕监听。',
    parameters: { room_id: { type: 'string', description: '留空=全部' } },
    async execute(args) {
      return jsonish(await call('bili_live_audio_stop', { room_id: String(args.room_id ?? '') }, { timeoutMs: 60000 }))
    },
  })

  tool({
    name: 'bili_live_audio_status',
    description: '看语音转写状态：谁在听、用的哪个引擎/设备、GPU 有没有失败、最近听到什么。只读。',
    parameters: { room_id: { type: 'string', description: '留空=看全部' } },
    async execute(args) {
      const rid = String(args.room_id ?? '')
      if (!rid.trim()) return { ok: true, items: audioList() }
      const hit = audioList().find((x) => x.room_id === rid)
      return hit ? { ok: true, audio: hit } : { ok: false, error: '这个房间没在听：' + rid }
    },
  })

  tool({
    name: 'bili_live_transcript',
    description: '读直播间最近的语音转写（带时间戳，只保留最近几分钟）。只读。',
    parameters: {
      room_id: { type: 'string', description: '直播间号 / UP主昵称 / UID' },
      last: { type: 'number', description: '最近几条，默认 20，最多 200' },
    },
    async execute(args) {
      return jsonish(await call('bili_live_transcript', {
        room_id: String(args.room_id ?? ''),
        last: clamp(Number(args.last) || 20, 1, 200),
      }))
    },
  })

  tool({
    name: 'bili_live_asr_probe',
    description: '体检这台机器能不能用 GPU 转写：真点一次 CUDA 拿原始报错，再看 whisper.cpp / DirectML。只读。',
    parameters: {},
    async execute() {
      return jsonish(await call('bili_live_asr_probe', {}, { timeoutMs: 200000 }))
    },
  })

  tool({
    name: 'bili_live_doctor',
    description: '环境自检：找 Python、查 bilibili_api / mcp 装没装、看凭证和放行名单在哪。只读，不改任何东西。',
    parameters: {},
    async execute() {
      const d = await doctor()
      return {
        ok: d.ok,
        home: HOME,
        mcp_script: MCP_SCRIPT,
        has_credential: existsSync(CRED_FILE),
        allow_file: ALLOW_FILE,
        python: d.python,
        version: d.version,
        tried: d.tried,
        hint: d.ok ? '环境就绪' : '缺依赖：把 README 里那条 pip 命令跑一遍',
      }
    },
  })
}

/* ────────────────────────── 设置页要的 HTTP 接口 ────────────────────────── */

function sendJson(res, status, obj) {
  const body = JSON.stringify(obj)
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
  res.end(body)
}

function readBody(req) {
  return new Promise((resolve) => {
    let raw = ''
    req.on('data', (c) => { raw = (raw + c).slice(-200000) })
    req.on('end', () => { try { resolve(raw ? JSON.parse(raw) : {}) } catch { resolve({}) } })
    req.on('error', () => resolve({}))
  })
}

function markRequestDone(roomId) {
  const items = readJsonl(REQUEST_FILE, 500).map((r) => (
    String(r.room_id) === String(roomId) ? { ...r, status: 'done' } : r
  ))
  try {
    writeFileSync(REQUEST_FILE, items.map((r) => JSON.stringify(r)).join('\n') + (items.length ? '\n' : ''), 'utf8')
  } catch (e) { log('回写待批清单失败：' + (e?.message ?? e)) }
}

/* ── 来源校验（0.3.0）────────────────────────────────────────────
 * 这些接口能改配置、能动放行名单、能发弹幕，所以不能"谁能连上 127.0.0.1 就算谁"：
 *   ① 先过 DSH 的信任栅栏 connection.requestRejection —— 那是宿主级鉴权（认 cookie/token），
 *      即使有人用隧道/反代把 Host 改写成回环也挡得住；
 *   ② 再过一层"本机同源"兜底：Host 必须是回环，Origin/Referer 必须同端口
 *      （挡浏览器里另一个本地页面借用户身份驱动这些写接口）；认证服务不可用时直接拒绝，
 *      不允许降级成本机同源放行。
 * 写法与 dsh-bili-scroll 线上那套一致（它的 issue #88 踩过同一个坑）。
 */
function makeRouteGuard(ctx) {
  let fenceWarned = false

  const getConnection = () => {
    const tries = [
      ['ctx.get("connection")', () => (typeof ctx.get === 'function' ? ctx.get('connection') : null)],
      ['Reflect.get(ctx, "connection")', () => Reflect.get(ctx, 'connection')],
      ['ctx.root.connection', () => (ctx.root && typeof ctx.root === 'object' ? ctx.root.connection : null)],
    ]
    for (const [how, fn] of tries) {
      try {
        const c = fn()
        if (c && typeof c.requestRejection === 'function') {
          log('来源校验：用 ' + how + ' 拿到了宿主的信任栅栏 ✓')
          return c
        }
      } catch { /* 换下一个 */ }
    }
    if (!fenceWarned) { fenceWarned = true; log('宿主认证服务不可用，全部插件 HTTP 接口拒绝访问') }
    return null
  }

  const fenceReject = async (req, res) => {
    const conn = getConnection()
    if (!conn) {
      sendJson(res, 503, { ok: false, error: '宿主认证服务不可用；拒绝访问，请恢复认证服务后再试。' })
      return true
    }
    try {
      const rejection = await conn.requestRejection({ headers: req.headers })
      if (rejection === undefined) return false
      res.statusCode = typeof rejection === 'number' ? rejection : 403
      try { res.end() } catch { /* 已经关了 */ }
      return true
    } catch (err) {
      log('信任栅栏调用出错，按不通过处理：' + String(err?.message ?? err))
      res.statusCode = 403
      try { res.end() } catch { /* 已经关了 */ }
      return true
    }
  }

  const sameOriginOk = (req) => {
    try {
      const host = String(req.headers.host || '')
      if (!host) return false // Host 缺失不能作为本机或可信身份依据
      if (!/^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/i.test(host)) return false
      const pick = String(req.headers.origin || req.headers.referer || '')
      if (!pick) return true
      try {
        const u = new URL(pick)
        const hostAuthority = host.toLowerCase()
        if (String(u.host || '').toLowerCase() === hostAuthority) return true
        const defaultPort = u.protocol === 'https:' ? '443' : '80'
        return (u.hostname + ':' + (u.port || defaultPort)).toLowerCase() === hostAuthority
      } catch { return false }
    } catch { return false }
  }

  /** true = 放行；false = 已经回过响应了，别再写 */
  return async function guard(req, res) {
    if (await fenceReject(req, res)) return false
    if (!sameOriginOk(req)) {
      try { sendJson(res, 403, { ok: false, error: '只接受本机同源请求：Host 必须是回环地址，Origin/Referer 必须是同一个端口' }) } catch { /* 已经关了 */ }
      return false
    }
    return true
  }
}

function registerRoutes(ctx) {
  const guard = makeRouteGuard(ctx)
  const route = (path, handler) => {
    try {
      ctx.effect(() => ctx.webServer.register({
        kind: 'exact',
        path,
        handler: async (req, res) => {
          if (!await guard(req, res)) return
          try {
            await handler(req, res)
          } catch (e) {
            // 每个 handler 内部都自己 catch 了；这里是最后一道，且绝不写第二次响应
            if (!res.headersSent) sendJson(res, 500, { ok: false, error: String(e?.message ?? e) })
            log('路由 ' + path + ' 抛错：' + String(e?.message ?? e))
          }
        },
      }))
    } catch (e) { log('注册路由失败 ' + path + '：' + (e?.message ?? e)) }
  }

  route('/dsh-bili-live/state', async (req, res) => {
    try {
      const allow = loadAllow()
      const d = await doctor()
      return sendJson(res, 200, {
        ok: true,
        home: HOME,
        python: { ok: d.ok, path: d.python, version: d.version, tried: d.tried },
        config: cfg,
        has_credential: existsSync(CRED_FILE),
        allow: { enforce: allow.enforce, rooms: allow.rooms },
        requests: loadRequests(30).filter((r) => r.status !== 'done'),
        watching: watchingList(),
        send: sendStats(),
        auto: { ...cfg.autoChat, lastError: autoState.lastError, busy: autoState.busy },
        audio: audioList(),
        audioCfg: cfg.audio,
        log_tail: tailFile(PLUGIN_LOG, 25),
      })
    } catch (e) {
      return sendJson(res, 500, { ok: false, error: String(e?.message ?? e) })
    }
  })

  // 转写体检：真去点一次 CUDA，把原始报错原样返回给设置页（慢，最多 3 分钟）
  route('/dsh-bili-live/asr', async (req, res) => {
    try {
      const r = jsonish(await call('bili_live_asr_probe', {}, { timeoutMs: 200000 }))
      return sendJson(res, 200, { ok: true, probe: r })
    } catch (e) {
      return sendJson(res, 500, { ok: false, error: String(e?.message ?? e) })
    }
  })

  route('/dsh-bili-live/audio', async (req, res) => {
    try {
      if (req.method === 'POST') {
        const body = await readBody(req)
        const action = String(body.action || '')
        const room = String(body.room_id || '')
        if (action === 'start') {
          if (!room.trim()) return sendJson(res, 400, { ok: false, error: '要给房间号' })
          if (body.asrPython !== undefined) {
            const pass = asrPythonAllowed(body.asrPython)
            if (!pass.ok) return sendJson(res, 403, { ok: false, error: pass.error, config: cfg.audio })
          }
          if (body.model || body.device || body.asrPython !== undefined) {
            persistCfg({
              audio: {
                ...(body.model ? { model: body.model } : {}),
                ...(body.device ? { device: body.device } : {}),
                ...(body.asrPython !== undefined ? { asrPython: body.asrPython } : {}),
              },
            })
          }
          const r = jsonish(await call('bili_live_audio_start', {
            room_id: room,
            model: cfg.audio.model,
            device: cfg.audio.device,
            window: cfg.audio.window,
            overlap: cfg.audio.overlap,
            keep: cfg.audio.keep,
          }, { timeoutMs: 60000 }))
          log('设置页：开听觉 ' + room + ' → ' + String(r?.status || r?.ok))
          return sendJson(res, r?.ok ? 200 : 400, { ...r, items: audioList() })
        }
        if (action === 'stop') {
          const r = jsonish(await call('bili_live_audio_stop', { room_id: room }, { timeoutMs: 60000 }))
          log('设置页：关听觉 ' + (room || '全部'))
          return sendJson(res, 200, { ...r, items: audioList() })
        }
        return sendJson(res, 400, { ok: false, error: 'action 只能是 start / stop' })
      }
      return sendJson(res, 200, { ok: true, items: audioList(), config: cfg.audio })
    } catch (e) {
      return sendJson(res, 500, { ok: false, error: String(e?.message ?? e) })
    }
  })

  route('/dsh-bili-live/config', async (req, res) => {
    try {
      if (req.method === 'POST') {
        const patch = await readBody(req)
        // ⭐ 0.3.0：网页端不许把插件指到任意可执行程序上（维护者 polang233 的复核意见）
        if (patch && Object.prototype.hasOwnProperty.call(patch, 'pythonPath')) {
          const gate = pythonPathAllowed(patch.pythonPath)
          if (!gate.ok) return sendJson(res, 403, { ok: false, error: gate.error, config: cfg })
        }
        if (patch?.audio && Object.prototype.hasOwnProperty.call(patch.audio, 'asrPython')) {
          const gate = asrPythonAllowed(patch.audio.asrPython)
          if (!gate.ok) return sendJson(res, 403, { ok: false, error: gate.error })
        }
        // 0.4.1：webhook 白名单只由人来加（这个页面是人在点，模型没有浏览器登录态）。
        if (patch && Object.prototype.hasOwnProperty.call(patch, 'webhookAllowlist')) {
          const list = Array.isArray(patch.webhookAllowlist) ? patch.webhookAllowlist : []
          const bad = list.filter((x) => !parseTarget(x))
          if (bad.length) return sendJson(res, 400, { ok: false, error: '这些地址不是合法的 http(s) 地址：' + bad.join('、'), config: cfg })
          if (list.length > MAX_ALLOW_ENTRIES) return sendJson(res, 400, { ok: false, error: '白名单最多 ' + MAX_ALLOW_ENTRIES + ' 条', config: cfg })
        }
        persistCfg(patch)
        log('设置已改：' + JSON.stringify(patch).slice(0, 200))
        // 改了 python 路径就把旧的 MCP 关掉，下一条消息会按新解释器重建
        if (patch && Object.prototype.hasOwnProperty.call(patch, 'pythonPath')) {
          try { if (mcp) mcp.stop() } catch { /* 已经没了 */ }
          mcp = null; mcpKey = ''; mcpExe = ''
        }
      }
      return sendJson(res, 200, { ok: true, config: cfg })
    } catch (e) {
      return sendJson(res, 500, { ok: false, error: String(e?.message ?? e) })
    }
  })

  route('/dsh-bili-live/models', async (req, res) => {
    const providers = []
    try {
      const llm = getLlm()
      const list = (llm && typeof llm.listProviders === 'function') ? llm.listProviders() : []
      for (const p of (list || [])) {
        const item = { id: p.id, name: p.name || p.id, models: [] }
        try {
          if (llm && typeof llm.listModels === 'function') {
            const ms = await llm.listModels(p.id)
            for (const m of (ms || [])) item.models.push({ id: m.id, name: m.name || m.id })
          }
        } catch { /* 某个 provider 拉不出来就算了 */ }
        providers.push(item)
      }
    } catch (e) { log('列模型失败：' + (e?.message ?? e)) }
    return sendJson(res, 200, { ok: true, providers, available: providers.length > 0 })
  })

  route('/dsh-bili-live/doctor', async (req, res) => {
    const d = await doctor()
    return sendJson(res, 200, {
      ok: d.ok,
      python: d.python,
      version: d.version,
      tried: d.tried,
      has_credential: existsSync(CRED_FILE),
      hint: d.ok ? '环境就绪' : '按 README 那条 pip 命令装依赖，再把路径填进「Python 路径」（或放到 $DSH_HOME/bili-live/.venv）',
    })
  })

  route('/dsh-bili-live/allow', async (req, res) => {
    try {
      if (req.method !== 'POST') {
        const allow = loadAllow()
        return sendJson(res, 200, { ok: true, enforce: allow.enforce, rooms: allow.rooms })
      }
      const body = await readBody(req)
      const action = String(body.action || 'add')
      const ridRaw = String(body.room_id || '').trim()
      if (!ridRaw) return sendJson(res, 400, { ok: false, error: '缺 room_id' })
      const r = jsonish(await call('bili_live_allow', { room_id: ridRaw, action, until_offline: body.until_offline !== false }))
      return sendJson(res, r.ok === false ? 400 : 200, r)
    } catch (e) {
      return sendJson(res, 500, { ok: false, error: String(e?.message ?? e) })
    }
  })

  route('/dsh-bili-live/request', async (req, res) => {
    try {
      if (req.method !== 'POST') return sendJson(res, 200, { ok: true, requests: loadRequests(30) })
      const body = await readBody(req)
      const roomId = String(body.room_id || '').trim()
      const action = String(body.action || 'approve')
      if (!roomId) return sendJson(res, 400, { ok: false, error: '缺 room_id' })
      if (action === 'approve') {
        const r = jsonish(await call('bili_live_allow', { room_id: roomId, action: 'add', until_offline: body.until_offline !== false }))
        if (r.ok !== false) markRequestDone(roomId)
        return sendJson(res, r.ok === false ? 400 : 200, r)
      }
      markRequestDone(roomId)
      return sendJson(res, 200, { ok: true, action: 'dismiss', room_id: roomId })
    } catch (e) {
      return sendJson(res, 500, { ok: false, error: String(e?.message ?? e) })
    }
  })

  route('/dsh-bili-live/send', async (req, res) => {
    try {
      if (req.method !== 'POST') return sendJson(res, 405, { ok: false, error: '用 POST' })
      const body = await readBody(req)
      if (!cfg.sendEnabled) return sendJson(res, 400, { ok: false, error: '「允许发弹幕」是关的，先打开它' })
      // 房间授权不在这一层判：真正的闸在 live_mcp.py 的 bili_live_send —— 房间不在
      // live-allow.json 里一律拒绝。这里只是入口，不放宽任何权限。
      const r = jsonish(await call('bili_live_send', {
        room_id: String(body.room_id || ''),
        text: String(body.text || ''),
        confirm: true,
      }))
      return sendJson(res, r.ok === false ? 400 : 200, r)
    } catch (e) {
      return sendJson(res, 500, { ok: false, error: String(e?.message ?? e) })
    }
  })

  route('/dsh-bili-live/watch', async (req, res) => {
    try {
      if (req.method !== 'POST') return sendJson(res, 405, { ok: false, error: '用 POST' })
      const body = await readBody(req)
      const action = String(body.action || 'start')
      const roomId = String(body.room_id || '').trim()
      const webhook = String(body.webhook || '').trim()
      // 0.4.1：和工具侧同一条规则 —— 只认用户预先放行的地址（这里的页面本来就是人在点）。
      if (action !== 'stop' && webhook) {
        const gate = webhookAllowed(webhook, cfg.webhookAllowlist)
        if (!gate.ok) return sendJson(res, 403, { ok: false, error: gate.error, config: cfg })
      }
      const r = action === 'stop'
        ? jsonish(await call('bili_live_watch_stop', { room_id: roomId }))
        : jsonish(await call('bili_live_watch_start', {
          room_id: roomId,
          webhook,
          wake_cooldown: Number(body.wake_cooldown) || 90,
          judge_sec: Number(body.judge_sec) || 60,
          max_wakes_per_hour: Number(body.max_wakes_per_hour) || 30,
        }))
      return sendJson(res, r.ok === false ? 400 : 200, r)
    } catch (e) {
      return sendJson(res, 500, { ok: false, error: String(e?.message ?? e) })
    }
  })

  route('/dsh-bili-live/autochat', async (req, res) => {
    try {
      if (req.method !== 'POST') return sendJson(res, 405, { ok: false, error: '用 POST' })
      const body = await readBody(req)
      const runNow = body && body.runNow === true
      const patch = { ...(body || {}) }
      delete patch.runNow
      if (Object.keys(patch).length) persistCfg({ autoChat: patch })
      if (runNow) void autoTick(getLlm())
      return sendJson(res, 200, { ok: true, autoChat: cfg.autoChat })
    } catch (e) {
      return sendJson(res, 500, { ok: false, error: String(e?.message ?? e) })
    }
  })
}

/* ────────────────────────── 入口 ────────────────────────── */

export let ctx = null

export function apply(pluginCtx) {
  ctx = pluginCtx
  try {
    mkdirSync(HOME, { recursive: true })
    mkdirSync(LOG_DIR, { recursive: true })
    mkdirSync(WATCH_ROOT, { recursive: true })
    mkdirSync(join(HOME, 'state'), { recursive: true })
    if (!existsSync(CONFIG_FILE)) writeJson(CONFIG_FILE, DEFAULTS)
    if (!existsSync(ALLOW_FILE)) writeJson(ALLOW_FILE, { enforce: true, rooms: {} })
  } catch (e) {
    log('初始化目录失败：' + (e?.message ?? e))
  }
  log('插件启动：home=' + HOME + ' python=' + (cfg.pythonPath || '(自动找)'))
  for (const n of (hostDeps.notes || [])) log('宿主依赖：' + n)

  registerTools()
  registerRoutes(ctx)

  // 自动陪聊：一个很轻的定时器（默认 20 秒看一眼）。没开就立刻 return，不占资源。
  const timer = setInterval(() => {
    if (autoState.busy) return
    autoState.busy = true
    autoState.lastTickAt = Date.now()
    Promise.resolve()
      .then(() => autoTick(getLlm()))
      .catch((e) => { autoState.lastError = String(e?.message ?? e); log('自动陪聊出错：' + autoState.lastError) })
      .finally(() => { autoState.busy = false })
  }, clamp(cfg.autoChat.tickSec, 10, 600) * 1000)
  try { if (typeof timer.unref === 'function') timer.unref() } catch { /* 有的宿主没有 */ }
  try { ctx.effect(() => () => { clearInterval(timer) }) } catch { /* 没有 effect 也不会炸 */ }

  // 进程退出时把 MCP 一起收掉，别留孤儿进程
  try {
    const bye = () => { try { if (mcp) mcp.stop() } catch { /* 已经没了 */ } }
    process.once('exit', bye)
    process.once('SIGINT', bye)
    process.once('SIGTERM', bye)
  } catch { /* 某些宿主不给 process */ }
}
