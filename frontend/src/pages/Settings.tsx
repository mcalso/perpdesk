import { useCallback, useEffect, useState } from 'react'
import { activeAccount } from '../lib/account'
import { authState } from '../lib/auth'
import { api, type AccountRow } from '../lib/api'
import { fmtTime } from '../lib/format'

/** 浏览器当前是不是走的 HTTPS —— 决定能不能在网页里录入凭据 */
const SECURE = typeof location !== 'undefined'
  && (location.protocol === 'https:' || location.hostname === 'localhost'
      || location.hostname === '127.0.0.1')

function AccountCard({ a, onChange }: { a: AccountRow; onChange: () => void }) {
  const [label, setLabel] = useState(a.label)
  const [key, setKey] = useState('')
  const [secret, setSecret] = useState('')
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err' | 'info'; text: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [showCreds, setShowCreds] = useState(false)

  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true); setMsg(null)
    try { await fn(); if (ok) setMsg({ kind: 'ok', text: ok }); onChange() }
    catch (e) { setMsg({ kind: 'err', text: (e as Error).message }) }
    finally { setBusy(false) }
  }

  const saveCreds = async () => {
    setBusy(true); setMsg(null)
    try {
      const r = await api.setCredentials(a.id, { api_key: key, api_secret: secret })
      setKey(''); setSecret('')
      setMsg(r.verified
        ? { kind: 'ok', text: `已保存并校验通过，读到 ${r.assets} 个币种余额` }
        : { kind: 'info', text: r.error || '已保存，但校验没通过' })
      onChange()
    } catch (e) {
      setMsg({ kind: 'err', text: (e as Error).message })
    } finally { setBusy(false) }
  }

  const del = async () => {
    const u = await api.accountUsage(a.id).catch(() => null)
    const warn = u && (u.trades || u.income)
      ? `该账户下有 ${u.trades} 条成交、${u.income} 条流水，删不掉。建议改为「停用」。`
      : `确定删除「${a.label}」？它的 API 凭据会一并抹掉。`
    if (!confirm(warn)) return
    await run(() => api.deleteAccount(a.id), '已删除')
  }

  return (
    <div className="acct-card">
      <div className="acct-card-head">
        <input value={label} onChange={(e) => setLabel(e.target.value)}
               onBlur={() => label !== a.label && label.trim()
                 && run(() => api.patchAccount(a.id, { label: label.trim() }))} />
        <span className={`tag ${a.configured ? 'live' : ''}`}>
          {a.configured ? '已配凭据' : '未配凭据'}
        </span>
        <span className="muted" style={{ fontSize: 11 }}>
          {a.exchange} · {a.market} · {a.trades} 笔成交
        </span>
        <div className="spacer" />
        <button className="sm" disabled={busy}
                onClick={() => run(() => api.patchAccount(a.id, { enabled: !a.enabled }))}>
          {a.enabled ? '停用' : '启用'}
        </button>
        <button className="sm danger" disabled={busy} onClick={del}>删除</button>
      </div>

      {Object.keys(a.credentials).length > 0 && (
        <div className="acct-creds mono">
          {Object.entries(a.credentials).map(([k, v]) => (
            <span key={k}>{k}: {v}</span>
          ))}
        </div>
      )}

      {!showCreds ? (
        <button className="ghost sm" onClick={() => setShowCreds(true)}>
          {a.configured ? '更换 API 凭据' : '录入 API 凭据'}
        </button>
      ) : !SECURE ? (
        <div className="msg err">
          当前是明文 HTTP，后端会拒绝接收 API 密钥——密钥会明文经过网络。
          请先配置 HTTPS；急用的话在服务器上写进 <code>backend/.env</code>。
        </div>
      ) : (
        <div className="col" style={{ gap: 8 }}>
          <input placeholder="API Key" autoComplete="off"
                 value={key} onChange={(e) => setKey(e.target.value)} />
          <input placeholder="API Secret" type="password" autoComplete="new-password"
                 value={secret} onChange={(e) => setSecret(e.target.value)} />
          <div className="toolbar">
            <button className="primary sm" disabled={busy || !key || !secret}
                    onClick={saveCreds}>{busy ? '校验中…' : '保存并校验'}</button>
            <button className="sm" onClick={() => { setShowCreds(false); setKey(''); setSecret('') }}>
              取消
            </button>
            <span className="muted" style={{ fontSize: 11 }}>
              交易所侧请只勾选读取权限，不要开交易与提现
            </span>
          </div>
        </div>
      )}

      {msg && <div className={`msg ${msg.kind}`}>{msg.text}</div>}
    </div>
  )
}

export default function Settings() {
  const [rows, setRows] = useState<AccountRow[]>([])
  const [newLabel, setNewLabel] = useState('')
  const [sessions, setSessions] = useState<{ last_seen: number; label: string }[]>([])
  const [cur, setCur] = useState(''); const [next, setNext] = useState('')
  const [pwMsg, setPwMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [err, setErr] = useState('')

  const reload = useCallback(async () => {
    try { setRows((await api.accounts()).rows); setErr('') }
    catch (e) { setErr((e as Error).message) }
    api.sessions().then((r) => setSessions(r.rows)).catch(() => {})
  }, [])
  useEffect(() => { reload() }, [reload])

  const add = async () => {
    if (!newLabel.trim()) return
    try { await api.createAccount({ label: newLabel.trim() }); setNewLabel(''); reload() }
    catch (e) { setErr((e as Error).message) }
  }

  const changePw = async (e: React.FormEvent) => {
    e.preventDefault(); setPwMsg(null)
    try {
      const r = await api.changePassword(cur, next)
      setCur(''); setNext(''); setPwMsg({ kind: 'ok', text: r.note }); reload()
    } catch (ex) { setPwMsg({ kind: 'err', text: (ex as Error).message }) }
  }

  return (
    <div className="page col">
      <div className="panel">
        <div className="panel-head">
          交易所账户
          <span className="muted" style={{ fontWeight: 400 }}>
            一个人可以在多个交易所、多个账户下交易；成交与盈亏按账户隔离统计
          </span>
        </div>
        <div className="panel-body col" style={{ gap: 12 }}>
          {err && <div className="msg err">{err}</div>}
          {rows.map((a) => (
            <AccountCard key={a.id} a={a} onChange={reload} />
          ))}
          <div className="toolbar">
            <input placeholder="新账户名称，如「OKX 主账户」" value={newLabel}
                   onChange={(e) => setNewLabel(e.target.value)}
                   onKeyDown={(e) => e.key === 'Enter' && add()} style={{ width: 240 }} />
            <button className="primary sm" onClick={add} disabled={!newLabel.trim()}>
              添加账户
            </button>
          </div>
        </div>
      </div>

      <div className="row">
        <div className="panel" style={{ flex: 1, minWidth: 0 }}>
          <div className="panel-head">站点口令</div>
          <div className="panel-body">
            <form onSubmit={changePw} className="col" style={{ gap: 8, maxWidth: 320 }}>
              <input type="password" placeholder="当前口令" autoComplete="current-password"
                     value={cur} onChange={(e) => setCur(e.target.value)} />
              <input type="password" placeholder="新口令（至少 8 位）" autoComplete="new-password"
                     value={next} onChange={(e) => setNext(e.target.value)} />
              <button className="primary sm" type="submit" disabled={!cur || next.length < 8}>
                修改口令
              </button>
            </form>
            {pwMsg && <div className={`msg ${pwMsg.kind}`}>{pwMsg.text}</div>}
            {!SECURE && (
              <div className="msg info">
                当前是明文 HTTP，口令在网络上是明文传输的。配置 HTTPS 后才安全。
              </div>
            )}
          </div>
        </div>

        <div className="panel" style={{ flex: 1, minWidth: 0 }}>
          <div className="panel-head">登录中的设备<span className="muted">{sessions.length}</span></div>
          <div className="panel-body">
            <div className="acct-sessions" style={{ maxHeight: 160 }}>
              {sessions.map((s, i) => (
                <div key={i} className="acct-session">
                  <span className="muted">{fmtTime(s.last_seen * 1000)}</span>
                  <span className="acct-ua" title={s.label}>{s.label || '未知设备'}</span>
                </div>
              ))}
            </div>
            <div className="toolbar" style={{ marginTop: 10 }}>
              <button className="sm" onClick={async () => {
                await api.logout(); authState.set('out')
              }}>退出登录</button>
              <button className="sm danger" onClick={async () => {
                await api.revokeAllSessions(); activeAccount.set(null); authState.set('out')
              }}>在所有设备退出</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
