import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { AccountSwitcher } from './components/AccountSwitcher'
import { Login } from './components/Login'
import { authState, useAuthState } from './lib/auth'
import { api, type HubStatus } from './lib/api'

function StatusChip() {
  const [s, setS] = useState<HubStatus | null>(null)
  const [err, setErr] = useState(false)

  useEffect(() => {
    let alive = true
    const tick = () =>
      api.health()
        .then((r) => { if (alive) { setS(r.hub); setErr(false) } })
        .catch(() => { if (alive) setErr(true) })
    tick()
    const t = setInterval(tick, 10000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  if (err) return <div className="status-chip"><span className="dot bad" />后端未连接</div>
  if (!s) return <div className="status-chip">…</div>

  // 标记价直接决定持仓估值，它陈旧比 24h 行情陈旧严重得多，单独标出来
  const markStale = s.premiumAgeSec === null || s.premiumAgeSec > 30
  const tickStale = s.restAgeSec === null || s.restAgeSec > 120
  return (
    <div className="status-chip">
      <span title={s.restError || '标记价与资金费率，REST 轮询（持仓估值依赖它）'}>
        <span className={`dot ${markStale ? 'warn' : 'ok'}`} />
        标记价 {s.premium} · {s.premiumAgeSec === null ? '未就绪' : `${s.premiumAgeSec.toFixed(0)}s 前`}
      </span>
      <span title="全市场 24h 涨跌与成交额">
        <span className={`dot ${tickStale ? 'warn' : 'ok'}`} />
        行情 {s.snapshot}
      </span>
      <span title="自选与持仓标的的实时盘口，WebSocket">
        <span className={`dot ${s.wsConnected ? 'ok' : 'bad'}`} />
        实时 {s.wsSymbols}
      </span>
    </div>
  )
}

export default function App() {
  const auth = useAuthState()

  useEffect(() => {
    // 启动时问一次登录状态。此后任何请求收到 401 也会把状态置回未登录
    // （见 lib/api.ts），所以会话过期能自动退回登录页。
    api.me()
      .then((r) => authState.set(r.authenticated ? 'in' : 'out'))
      .catch(() => authState.set('out'))
  }, [])

  // 未知状态时先不渲染：直接渲染主界面会先闪一下再跳登录页，
  // 而且那一瞬间已经发出去一批必然 401 的请求
  if (auth === 'unknown') return <div className="login-wrap" />
  if (auth === 'out') return <Login />

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">trad<span>view</span></div>
        <nav className="nav">
          {/* viewTransition：切页时走浏览器原生的交叉淡入淡出。
              不支持的浏览器会直接跳转，不会报错 */}
          <NavLink to="/market" viewTransition
                   className={({ isActive }) => (isActive ? 'active' : '')}>行情看板</NavLink>
          <NavLink to="/chart" viewTransition
                   className={({ isActive }) => (isActive ? 'active' : '')}>图表分析</NavLink>
          <NavLink to="/portfolio" viewTransition
                   className={({ isActive }) => (isActive ? 'active' : '')}>持仓盈亏</NavLink>
          <NavLink to="/news" viewTransition
                   className={({ isActive }) => (isActive ? 'active' : '')}>资讯</NavLink>
        </nav>
        <AccountSwitcher />
        <StatusChip />
        <NavLink to="/settings" viewTransition className="ghost sm settings-link"
                 title="设置">⚙</NavLink>
      </header>
      <Outlet />
    </div>
  )
}
