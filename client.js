/**
 * dsh-bili-live —— 设置页（浏览器那半）
 * 在 设置 → 插件 →「B站直播」里出现。所有数据都从 /dsh-bili-live/* 拿。
 */
window.__ModuleLoader__.load({
  id: 'dsh-bili-live',
  factory: (require) => {
    const React = require('react')
    const h = React.createElement
    const { useState, useEffect, useCallback } = React
    const CSS = ".bl-wrap{display:flex;flex-direction:column;gap:14px;font-size:13px;color:var(--fg,#e8ecf4)}.bl-card{border:1px solid rgba(148,163,184,.28);border-radius:12px;padding:14px 16px;background:rgba(148,163,184,.05)}.bl-h{font-weight:600;font-size:14px;margin:0 0 10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}.bl-mut{color:#94a3b8;font-size:12px;line-height:1.6}.bl-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:8px 0}.bl-row>label{min-width:96px;color:#cbd5e1}.bl-in,.bl-sel{background:rgba(15,23,42,.5);border:1px solid rgba(148,163,184,.35);color:inherit;border-radius:8px;padding:6px 9px;font-size:13px;min-width:120px}.bl-in.bl-w{min-width:260px}.bl-btn{border:1px solid rgba(148,163,184,.4);background:rgba(148,163,184,.12);color:inherit;border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer}.bl-btn:hover{background:rgba(148,163,184,.22)}.bl-btn.bl-primary{background:#3b82f6;border-color:#3b82f6;color:#fff}.bl-btn.bl-danger{background:rgba(239,68,68,.18);border-color:rgba(239,68,68,.5);color:#fca5a5}.bl-btn:disabled{opacity:.5;cursor:not-allowed}.bl-tag{font-size:11px;padding:2px 7px;border-radius:999px;border:1px solid rgba(148,163,184,.4)}.bl-ok{border-color:rgba(34,197,94,.5);color:#86efac}.bl-bad{border-color:rgba(239,68,68,.5);color:#fca5a5}.bl-list{display:flex;flex-direction:column;gap:6px}.bl-item{display:flex;gap:10px;align-items:center;justify-content:space-between;border:1px solid rgba(148,163,184,.2);border-radius:8px;padding:6px 10px}.bl-pre{font-family:ui-monospace,monospace;font-size:11.5px;line-height:1.5;white-space:pre-wrap;word-break:break-all;max-height:150px;overflow:auto;color:#94a3b8;margin:0}.bl-sep{height:1px;background:rgba(148,163,184,.2);margin:12px 0}"

    const api = async (path, body) => {
      const opt = body === undefined
        ? { headers: { accept: 'application/json' } }
        : { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }
      const r = await fetch('/dsh-bili-live/' + path, opt)
      let data = null
      try { data = await r.json() } catch { data = { ok: false, error: 'HTTP ' + r.status } }
      return data || { ok: false, error: 'HTTP ' + r.status }
    }

    const Field = ({ label, children, hint }) => h('div', { className: 'bl-row' },
      h('label', null, label),
      children,
      hint ? h('span', { className: 'bl-mut' }, hint) : null,
    )

    const Tag = ({ ok, children }) => h('span', { className: 'bl-tag ' + (ok ? 'bl-ok' : 'bl-bad') }, children)

    function Panel() {
      const [st, setSt] = useState(null)
      const [models, setModels] = useState({ providers: [], available: false })
      const [busy, setBusy] = useState('')
      const [msg, setMsg] = useState('')
      const [draft, setDraft] = useState({ room_id: '', text: '' })
      const [watchForm, setWatchForm] = useState({ room_id: '', webhook: '' })
      const [reqRoom, setReqRoom] = useState('')
      const [cfg, setCfg] = useState(null)

      const load = useCallback(async () => {
        setBusy('load')
        const s = await api('state')
        if (s.ok) { setSt(s); setCfg(s.config) }
        else setMsg(s.error || '读取失败')
        setBusy('')
      }, [])

      useEffect(() => { void load() }, [load])
      useEffect(() => { void api('models').then((m) => { if (m.ok) setModels(m) }) }, [])

      const saveCfg = async (patch) => {
        setBusy('save')
        const r = await api('config', patch)
        if (r.ok) { setCfg(r.config); setMsg('已保存') } else setMsg(r.error || '保存失败')
        setBusy('')
      }

      const saveAuto = async (patch) => {
        setBusy('autosave')
        const r = await api('autochat', patch)
        if (r.ok) {
          setCfg((c) => ({ ...(c || {}), autoChat: r.autoChat }))
          setSt((s) => (s ? { ...s, auto: { ...(s.auto || {}), ...r.autoChat } } : s))
          setMsg('自动陪聊已保存')
        } else setMsg(r.error || '保存失败')
        setBusy('')
      }

      const act = async (path, body, label) => {
        setBusy(label)
        const r = await api(path, body)
        setMsg(r && r.ok === false ? (r.error || r.message || '失败') : (label + '完成'))
        await load()
        setBusy('')
        return r
      }

      if (!st) {
        return h('div', { className: 'bl-wrap' }, h('div', { className: 'bl-mut' }, busy ? '正在读取…' : (msg || '没有数据')))
      }

      const A = (cfg && cfg.autoChat) || {}
      const allow = st.allow || { enforce: true, rooms: {} }
      const rooms = Object.entries(allow.rooms || {})
      const reqs = st.requests || []
      const watching = st.watching || []
      const send = st.send || { total: 0, by_result: {}, recent: [] }
      const pyOk = Boolean(st.python && st.python.ok)

      const modelOptions = []
      for (const p of (models.providers || [])) {
        for (const m of (p.models || [])) modelOptions.push({ provider: p.id, model: m.id, label: p.name + ' / ' + m.name })
      }

      return h('div', { className: 'bl-wrap' },

        // ── 环境
        h('div', { className: 'bl-card' },
          h('div', { className: 'bl-h' }, 'B站直播 · 环境',
            pyOk ? h(Tag, { ok: true }, 'Python 就绪') : h(Tag, { ok: false }, 'Python 没就绪'),
            st.has_credential ? h(Tag, { ok: true }, '已有 B站 凭证') : h(Tag, { ok: false }, '还没有 B站 凭证'),
          ),
          h('div', { className: 'bl-mut' }, '家目录：' + st.home + '（凭证、放行名单、弹幕日志都在这里，不进仓库）'),
          h(Field, { label: 'Python 路径' },
            h('input', {
              className: 'bl-in bl-w',
              value: cfg.pythonPath || '',
              placeholder: '留空=自动找（$DSH_HOME/bili-live/.venv → PATH）',
              onChange: (e) => setCfg({ ...cfg, pythonPath: e.target.value }),
            }),
            h('button', { className: 'bl-btn', disabled: busy !== '', onClick: () => saveCfg({ pythonPath: cfg.pythonPath }) }, '保存'),
            h('button', { className: 'bl-btn', disabled: busy !== '', onClick: () => act('doctor', {}, '环境自检') }, '重新自检'),
          ),
          h('div', { className: 'bl-mut' },
            '当前解释器：' + ((st.python && st.python.path) || '未找到') + (st.python && st.python.version ? ('　Python ' + st.python.version) : '')
            + '　｜　缺依赖时按 README 里那条 pip 命令装，或者把 .venv 放到家目录下。'),
        ),

        // ── 总开关
        h('div', { className: 'bl-card' },
          h('div', { className: 'bl-h' }, '发弹幕的总开关'),
          h(Field, { label: '允许发弹幕' },
            h('input', {
              type: 'checkbox',
              checked: Boolean(cfg.sendEnabled),
              onChange: (e) => saveCfg({ sendEnabled: e.target.checked }),
            }),
            h('span', { className: 'bl-mut' }, cfg.sendEnabled ? '开着：模型和这个页面都能发' : '关着：谁都发不出去（先看后发）'),
          ),
          h(Field, { label: '它在房里的名字' },
            h('input', {
              className: 'bl-in',
              value: cfg.botName || '',
              onChange: (e) => setCfg({ ...cfg, botName: e.target.value }),
              onBlur: () => saveCfg({ botName: cfg.botName }),
            }),
            h('span', { className: 'bl-mut' }, '用来判断「有人点名」；换人格时改这里'),
          ),
          h('div', { className: 'bl-mut' }, '同一房间 60 秒只能发 1 条（历史是 60 秒冷却，硬编码在 Python 里）。发出去删不掉 —— 每次都会写进 logs/danmaku.jsonl。'),
        ),

        // ── 放行名单
        h('div', { className: 'bl-card' },
          h('div', { className: 'bl-h' }, '放行名单 · 只有人能加'),
          h('div', { className: 'bl-mut' }, '模型只能「申请」，放行在这里点。名单为空 = 哪个房间都进不去。'),
          h('div', { className: 'bl-list' },
            rooms.length === 0
              ? h('div', { className: 'bl-mut' }, '（名单是空的）')
              : rooms.map(([rid, info]) => h('div', { className: 'bl-item', key: rid },
                h('span', null, '房间 ' + rid + (info && info.anchor ? ('　' + info.anchor) : '')),
                h('button', {
                  className: 'bl-btn bl-danger',
                  disabled: busy !== '',
                  onClick: () => act('allow', { room_id: rid, action: 'remove' }, '收回放行'),
                }, '收回'),
              )),
          ),
          h('div', { className: 'bl-sep' }),
          h('div', { className: 'bl-h' }, '待批申请（' + reqs.length + '）'),
          reqs.length === 0
            ? h('div', { className: 'bl-mut' }, '暂时没有待批的申请')
            : reqs.map((r, i) => h('div', { className: 'bl-item', key: (r.room_id || '') + i },
              h('span', null, '房间 ' + (r.room_id || '?') + (r.anchor ? ('　' + r.anchor) : '') + (r.reason ? ('　理由：' + r.reason) : '')),
              h('span', null,
                h('button', { className: 'bl-btn bl-primary', disabled: busy !== '', onClick: () => act('request', { room_id: r.room_id, action: 'approve' }, '放行') }, '放行'),
                ' ',
                h('button', { className: 'bl-btn', disabled: busy !== '', onClick: () => act('request', { room_id: r.room_id, action: 'dismiss' }, '忽略') }, '忽略'),
              ),
            )),
          h('div', { className: 'bl-sep' }),
          h(Field, { label: '手动放行' },
            h('input', {
              className: 'bl-in',
              placeholder: '房间号 / UP主昵称 / UID',
              value: reqRoom,
              onChange: (e) => setReqRoom(e.target.value),
            }),
            h('button', {
              className: 'bl-btn',
              disabled: busy !== '' || !reqRoom.trim(),
              onClick: async () => { await act('request', { room_id: reqRoom.trim(), action: 'approve' }, '放行'); setReqRoom('') },
            }, '放行'),
          ),
        ),

        // ── 挂着听
        h('div', { className: 'bl-card' },
          h('div', { className: 'bl-h' }, '挂着听（' + watching.length + ' 个房间）'),
          h('div', { className: 'bl-mut' }, 'worker 在后台收弹幕、写 hint；够得着时 POST 到你的 webhook（没配就只写 hint，不打扰任何人）。收工点「停」。'),
          h('div', { className: 'bl-list' },
            watching.length === 0
              ? h('div', { className: 'bl-mut' }, '（没有在挂的房间）')
              : watching.map((w) => h('div', { className: 'bl-item', key: String(w.room_id) },
                h('span', null,
                  '房间 ' + w.room_id + (w.anchor ? ('　' + w.anchor) : '')
                  + '　' + (w.alive ? 'worker 在跑' : 'worker 已经没了')
                  + '　弹幕 ' + w.danmaku_count + ' 条　发出 ' + w.sent_count + ' 条　叫醒 ' + w.wake_count + ' 次',
                ),
                h('button', {
                  className: 'bl-btn bl-danger',
                  disabled: busy !== '',
                  onClick: () => act('watch', { action: 'stop', room_id: String(w.room_id) }, '停'),
                }, '停'),
              )),
          ),
          h('div', { className: 'bl-sep' }),
          h(Field, { label: '房间' },
            h('input', { className: 'bl-in', placeholder: '房间号 / 昵称 / UID', value: watchForm.room_id, onChange: (e) => setWatchForm({ ...watchForm, room_id: e.target.value }) }),
            h('input', { className: 'bl-in bl-w', placeholder: 'webhook 地址（可留空）', value: watchForm.webhook, onChange: (e) => setWatchForm({ ...watchForm, webhook: e.target.value }) }),
            h('button', {
              className: 'bl-btn bl-primary',
              disabled: busy !== '' || !watchForm.room_id.trim(),
              onClick: () => act('watch', { action: 'start', room_id: watchForm.room_id.trim(), webhook: watchForm.webhook.trim() }, '开始挂机'),
            }, '开始挂机'),
          ),
        ),

        // ── 手动发一条
        h('div', { className: 'bl-card' },
          h('div', { className: 'bl-h' }, '手动发一条'),
          h('div', { className: 'bl-mut' }, '今日已发 ' + send.total + ' 条'
            + (Object.keys(send.by_result || {}).length ? ('　（' + Object.entries(send.by_result).map(([k, v]) => k + ' ' + v).join('，') + '）') : '')),
          h(Field, { label: '房间' },
            h('input', { className: 'bl-in', placeholder: '房间号 / 昵称 / UID', value: draft.room_id, onChange: (e) => setDraft({ ...draft, room_id: e.target.value }) }),
          ),
          h(Field, { label: '内容' },
            h('input', { className: 'bl-in bl-w', placeholder: '不超过 40 字', value: draft.text, onChange: (e) => setDraft({ ...draft, text: e.target.value }) }),
            h('button', {
              className: 'bl-btn bl-primary',
              disabled: busy !== '' || !draft.room_id.trim() || !draft.text.trim(),
              onClick: async () => { await act('send', { room_id: draft.room_id.trim(), text: draft.text.trim() }, '发送'); setDraft({ ...draft, text: '' }) },
            }, '发送'),
          ),
          send.recent && send.recent.length
            ? h('pre', { className: 'bl-pre' }, send.recent.map((x) => (x.ts || '') + '  ' + (x.result || '?') + '  ' + (x.text || '')).join('\n'))
            : null,
        ),

        // ── 自动陪聊
        h('div', { className: 'bl-card' },
          h('div', { className: 'bl-h' }, '自动陪聊（可选）',
            A.enabled ? h(Tag, { ok: true }, '开着') : h(Tag, { ok: false }, '关着'),
            models.available ? null : h(Tag, { ok: false }, '这台宿主没有可用模型'),
          ),
          h('div', { className: 'bl-mut' }, '每 ' + (A.tickSec || 20) + ' 秒看一眼增量弹幕 → 调一次模型判断要不要接话 → 写进 queue，由 worker 按节流发。大多数时候它会选择沉默。'),
          h(Field, { label: '启用' },
            h('input', { type: 'checkbox', checked: Boolean(A.enabled), onChange: (e) => saveAuto({ enabled: e.target.checked }) }),
            h('span', { className: 'bl-mut' }, '还要「允许发弹幕」也开着才会真的发'),
          ),
          h(Field, { label: '模型' },
            h('select', {
              className: 'bl-sel',
              value: (A.provider || '') + '|' + (A.model || ''),
              onChange: (e) => {
                const v = e.target.value
                if (!v) return saveAuto({ provider: '', model: '' })
                const parts = v.split('|')
                saveAuto({ provider: parts[0], model: parts[1] })
              },
            },
              h('option', { value: '' }, '（没选）'),
              modelOptions.map((m) => h('option', { key: m.provider + '|' + m.model, value: m.provider + '|' + m.model }, m.label)),
            ),
          ),
          h(Field, { label: '人设' },
            h('input', {
              className: 'bl-in bl-w',
              value: A.persona || '',
              onChange: (e) => setCfg((c) => ({ ...(c || {}), autoChat: { ...((c && c.autoChat) || {}), persona: e.target.value } })),
              onBlur: () => saveAuto({ persona: (cfg.autoChat || {}).persona }),
            }),
          ),
          h(Field, { label: '节奏' },
            h('input', { className: 'bl-in', style: { minWidth: 70 }, type: 'number', value: A.tickSec || 20, onChange: (e) => setCfg((c) => ({ ...(c || {}), autoChat: { ...((c && c.autoChat) || {}), tickSec: Number(e.target.value) } })) }),
            h('span', { className: 'bl-mut' }, '秒看一次　'),
            h('input', { className: 'bl-in', style: { minWidth: 70 }, type: 'number', value: A.minGapSec || 240, onChange: (e) => setCfg((c) => ({ ...(c || {}), autoChat: { ...((c && c.autoChat) || {}), minGapSec: Number(e.target.value) } })) }),
            h('span', { className: 'bl-mut' }, '秒内不重复开口　每小时最多'),
            h('input', { className: 'bl-in', style: { minWidth: 60 }, type: 'number', value: A.perHour || 8, onChange: (e) => setCfg((c) => ({ ...(c || {}), autoChat: { ...((c && c.autoChat) || {}), perHour: Number(e.target.value) } })) }),
            h('span', { className: 'bl-mut' }, '句　最长'),
            h('input', { className: 'bl-in', style: { minWidth: 60 }, type: 'number', value: A.maxChars || 60, onChange: (e) => setCfg((c) => ({ ...(c || {}), autoChat: { ...((c && c.autoChat) || {}), maxChars: Number(e.target.value) } })) }),
            h('span', { className: 'bl-mut' }, '字'),
          ),
          h('div', { className: 'bl-row' },
            h('button', {
              className: 'bl-btn',
              disabled: busy !== '',
              onClick: () => saveAuto({
                tickSec: Number((cfg.autoChat || {}).tickSec) || 20,
                minGapSec: Number((cfg.autoChat || {}).minGapSec) || 240,
                perHour: Number((cfg.autoChat || {}).perHour) || 8,
                maxChars: Number((cfg.autoChat || {}).maxChars) || 60,
              }),
            }, '保存节奏'),
            h('button', { className: 'bl-btn', disabled: busy !== '', onClick: () => act('autochat', { runNow: true }, '试一次') }, '立刻试一次'),
            st.auto && st.auto.lastError ? h('span', { className: 'bl-mut' }, '上次出错：' + st.auto.lastError) : null,
          ),
        ),

        // ── 日志
        h('div', { className: 'bl-card' },
          h('div', { className: 'bl-h' }, '插件日志（最近 25 行）'),
          h('pre', { className: 'bl-pre' }, (st.log_tail || []).join('\n') || '（还没有日志）'),
        ),

        msg ? h('div', { className: 'bl-mut' }, msg) : null,
      )
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        const style = document.createElement('style')
        style.textContent = CSS
        document.head.appendChild(style)
        ctx.effect(() => () => style.remove(), 'dsh-bili-live-style')
        const slots = (typeof ctx.get === 'function' ? ctx.get('slots') : undefined) || ctx.slots
        if (!slots) return
        slots.inject('settings.section', () => slots.register({
          name: 'settings.section',
          id: 'bili-live',
          order: 45,
          label: 'B站直播',
        }, () => h(Panel)))
      },
    }
  },
})
