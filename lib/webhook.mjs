/**
 * webhook 目标校验（纯函数，无依赖，测试直接 import）。
 *
 * 背景：bili_live_watch_start 的 webhook 会由 worker 真的去 POST，响应还会落进
 * 状态日志。如果地址可以随手填，模型（或被注入的上下文）就能让本机 worker 去访问
 * 任意地址 —— 典型的 SSRF 口子。所以规则是：
 *
 *   · 只认 http(s)，不允许带用户名/密码；
 *   · 只认用户预先写进 config.json 的 webhookAllowlist（设置页里也是人在填）；
 *   · 白名单条目写成 origin（如 http://127.0.0.1:8787）= 放行这个源的任意路径；
 *     带路径（如 http://127.0.0.1:8787/wake）= 只放行这个路径（query 不参与比较）。
 *
 * 这里只做「地址是否被允许」的判断；响应大小/超时/重定向由调用方
 * （python/live_chat_worker.py）另外限制。
 */

export const MAX_URL_LEN = 2048
export const MAX_ALLOW_ENTRIES = 20

/**
 * 把地址解析成 { origin, path }；不合法返回 null。
 * origin 例：https://example.com:8443　path 例：/wake/hook
 */
export function parseTarget(raw) {
  const text = String(raw ?? '').trim()
  if (!text || text.length > MAX_URL_LEN) return null
  let url
  try { url = new URL(text) } catch { return null }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
  if (url.username || url.password) return null
  const host = String(url.hostname || '').toLowerCase()
  if (!host) return null
  // 反斜杠、空白、@ 在旧解析器里会被当作分隔符，宁可拒绝
  if (/[\s@\\]/.test(host)) return null
  const scheme = url.protocol.slice(0, -1)
  const defaultPort = scheme === 'https' ? '443' : '80'
  const port = url.port && url.port !== defaultPort ? ':' + url.port : ''
  const path = url.pathname || '/'
  if (!path.startsWith('/')) return null
  return { origin: scheme + '://' + host + port, path, url: scheme + '://' + host + port + path }
}

/** 白名单条目规范化（去重、丢掉不合法的、最多 MAX_ALLOW_ENTRIES 条）。 */
export function normalizeAllowlist(list) {
  const out = []
  if (!Array.isArray(list)) return out
  for (const item of list.slice(0, MAX_ALLOW_ENTRIES)) {
    const target = parseTarget(item)
    if (!target) continue
    if (!out.some((x) => x.origin === target.origin && x.path === target.path)) out.push(target)
  }
  return out
}

/** 序列化成 config.json 里存的样子：origin（放行整个源）或 origin+path。 */
export function formatAllowEntry(target) {
  if (!target) return ''
  return target.path === '/' ? target.origin : target.origin + target.path
}

/**
 * webhook 是否被允许。
 * 返回 { ok: true, origin, path } 或 { ok: false, error }
 */
export function isAllowed(raw, allowlist) {
  const target = parseTarget(raw)
  if (!target) return { ok: false, error: 'webhook 必须是合法的 http(s) 地址，且不能带用户名/密码' }
  const allow = normalizeAllowlist(allowlist)
  if (allow.length === 0) {
    return { ok: false, error: 'config.json 的 webhookAllowlist 是空的：webhook 只能 POST 到用户预先配好的地址（请用户在设置页里加）' }
  }
  for (const entry of allow) {
    if (entry.origin !== target.origin) continue
    if (entry.path === '/' || entry.path === target.path) return { ok: true, origin: target.origin, path: target.path }
  }
  return { ok: false, error: target.origin + target.path + ' 不在 webhookAllowlist 里（请用户在设置页里加）' }
}
