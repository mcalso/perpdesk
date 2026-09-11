import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { api, type HubStatus } from './lib/api'
import Market from './pages/Market'
import Chart from './pages/Chart'
import Portfolio from './pages/Portfolio'

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

  // REST 快照超过 2 分钟没刷新就提示，通常是被 Binance 限速了
  const restStale = s.restAgeSec === null || s.restAgeSec > 120
  return (
    <div className="status-chip">
      <span title={s.restError || '全市场 24h 行情，REST 轮询'}>
        <span className={`dot ${restStale ? 'warn' : 'ok'}`} />
        行情 {s.snapshot} · {s.restAgeSec === null ? '未就绪' : `${s.restAgeSec.toFixed(0)}s 前`}
      </span>
      <span title="自选标的实时盘口，WebSocket">
        <span className={`dot ${s.wsConnected ? 'ok' : 'bad'}`} />
        实时 {s.wsSymbols}
      </span>
    </div>
  )
}

export default function App() {
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">trad<span>view</span></div>
        <nav className="nav">
          <NavLink to="/market" className={({ isActive }) => (isActive ? 'active' : '')}>行情看板</NavLink>
          <NavLink to="/chart" className={({ isActive }) => (isActive ? 'active' : '')}>图表分析</NavLink>
          <NavLink to="/portfolio" className={({ isActive }) => (isActive ? 'active' : '')}>持仓盈亏</NavLink>
        </nav>
        <StatusChip />
      </header>
      <Routes>
        <Route path="/" element={<Navigate to="/market" replace />} />
        <Route path="/market" element={<Market />} />
        <Route path="/chart" element={<Chart />} />
        <Route path="/chart/:symbol" element={<Chart />} />
        <Route path="/portfolio" element={<Portfolio />} />
      </Routes>
    </div>
  )
}
