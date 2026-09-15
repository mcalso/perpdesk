import { useRef, useState } from 'react'
import { authState } from '../lib/auth'
import { api } from '../lib/api'

export function Login() {
  const [pw, setPw] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const input = useRef<HTMLInputElement>(null)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true); setErr('')
    try {
      await api.login(pw)
      setPw('')
      authState.set('in')
    } catch (ex) {
      setErr((ex as Error).message)
      setPw('')
      input.current?.focus()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <div className="brand login-brand">perp<span>desk</span></div>
        <p className="muted login-sub">这是私有实例，需要口令才能查看持仓与盈亏。</p>
        <input
          ref={input}
          type="password"
          autoFocus
          autoComplete="current-password"
          placeholder="登录口令"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
        />
        <button className="primary" type="submit" disabled={busy || !pw}>
          {busy ? '验证中…' : '登录'}
        </button>
        {err && <div className="msg err">{err}</div>}
        <p className="muted login-hint">
          首次部署的口令会打印在后端日志里：systemd 部署看{' '}
          <code>/var/log/perpdesk/api.log</code>，本地运行看{' '}
          <code>./scripts/perpdesk.sh logs</code>。登录后请立即修改。
        </p>
      </form>
    </div>
  )
}
