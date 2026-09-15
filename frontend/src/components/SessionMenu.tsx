import { useEffect, useState } from 'react'
import { authState } from '../lib/auth'
import { api } from '../lib/api'
import { fmtTime } from '../lib/format'

/** 顶栏右侧的账号菜单：改口令、看会话、退出。 */
export function SessionMenu() {
  const [open, setOpen] = useState(false)
  const [rows, setRows] = useState<{ last_seen: number; label: string }[]>([])
  const [cur, setCur] = useState('')
  const [next, setNext] = useState('')
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    api.sessions().then((r) => setRows(r.rows)).catch(() => setRows([]))
  }, [open])

  const logout = async () => {
    try { await api.logout() } finally { authState.set('out') }
  }

  const change = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true); setMsg(null)
    try {
      const r = await api.changePassword(cur, next)
      setCur(''); setNext('')
      setMsg({ kind: 'ok', text: r.note })
      api.sessions().then((s) => setRows(s.rows)).catch(() => {})
    } catch (ex) {
      setMsg({ kind: 'err', text: (ex as Error).message })
    } finally { setBusy(false) }
  }

  return (
    <div className="acct-menu">
      <button className="ghost sm" onClick={() => setOpen(!open)} title="账号">
        {open ? '✕' : '⚙'}
      </button>
      {open && (
        <div className="acct-pop">
          <div className="acct-pop-head">账号</div>
          <form onSubmit={change} className="col" style={{ gap: 8 }}>
            <input type="password" placeholder="当前口令" autoComplete="current-password"
                   value={cur} onChange={(e) => setCur(e.target.value)} />
            <input type="password" placeholder="新口令（至少 8 位）" autoComplete="new-password"
                   value={next} onChange={(e) => setNext(e.target.value)} />
            <button className="primary sm" type="submit"
                    disabled={busy || !cur || next.length < 8}>
              {busy ? '提交中…' : '修改口令'}
            </button>
          </form>
          {msg && <div className={`msg ${msg.kind}`}>{msg.text}</div>}

          <div className="acct-pop-head" style={{ marginTop: 14 }}>
            登录中的设备 {rows.length}
          </div>
          <div className="acct-sessions">
            {rows.map((r, i) => (
              <div key={i} className="acct-session">
                <span className="muted">{fmtTime(r.last_seen * 1000)}</span>
                <span className="acct-ua" title={r.label}>{r.label || '未知设备'}</span>
              </div>
            ))}
          </div>
          <div className="toolbar" style={{ marginTop: 10 }}>
            <button className="sm" onClick={logout}>退出登录</button>
            <button className="sm danger" onClick={async () => {
              await api.revokeAllSessions(); authState.set('out')
            }}>在所有设备退出</button>
          </div>
        </div>
      )}
    </div>
  )
}
