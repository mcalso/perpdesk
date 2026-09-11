import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { SymbolIcon } from '../components/SymbolIcon'
import { NativeChart } from '../components/NativeChart'
import { TradingViewChart } from '../components/TradingViewChart'
import { api, type Ticker } from '../lib/api'
import { liveFeed, type LiveRow } from '../lib/ws'
import { fmtCompact, fmtPct, fmtPrice, trendClass } from '../lib/format'

const INTERVALS: { v: string; label: string }[] = [
  { v: '5', label: '5m' }, { v: '15', label: '15m' }, { v: '60', label: '1h' },
  { v: '240', label: '4h' }, { v: 'D', label: '1D' }, { v: 'W', label: '1W' },
]

export default function Chart() {
  const { symbol: routeSymbol } = useParams()
  const nav = useNavigate()
  const [watch, setWatch] = useState<Ticker[]>([])
  const [live, setLive] = useState<Map<string, LiveRow>>(new Map())
  const [interval, setIntervalV] = useState('60')
  const [adding, setAdding] = useState('')
  const [err, setErr] = useState('')
  // Binance 有中文 symbol，TradingView 上不存在，得回退到内置图表
  const [useTV, setUseTV] = useState<boolean | null>(null)
  const [tvReason, setTvReason] = useState('')

  const symbol = (routeSymbol || watch[0]?.symbol || 'BTCUSDT').toUpperCase()

  const loadWatch = () =>
    api.watchlist().then((r) => setWatch(r.rows)).catch((e: Error) => setErr(e.message))

  useEffect(() => { loadWatch() }, [])
  useEffect(() => liveFeed.subscribe((m) => setLive(new Map(m))), [])

  useEffect(() => {
    let alive = true
    setUseTV(null)
    api.chartSource(symbol)
      .then((r) => { if (alive) { setUseTV(r.tradingview); setTvReason(r.reason) } })
      .catch(() => { if (alive) setUseTV(symbol === encodeURIComponent(symbol)) })
    return () => { alive = false }
  }, [symbol])

  const rows = useMemo(
    () => watch.map((w) => {
      const l = live.get(w.symbol)
      return l ? { ...w, last: l.last, chgPct: l.chgPct } : w
    }),
    [watch, live],
  )

  const current = rows.find((r) => r.symbol === symbol)

  const add = async () => {
    const s = adding.trim().toUpperCase()
    if (!s) return
    try {
      await api.addWatch(s.endsWith('USDT') ? s : `${s}USDT`)
      setAdding(''); setErr(''); await loadWatch()
    } catch (e) { setErr((e as Error).message) }
  }

  const remove = async (s: string, e: React.MouseEvent) => {
    e.stopPropagation()
    try { await api.removeWatch(s); await loadWatch() } catch (ex) { setErr((ex as Error).message) }
  }

  return (
    <div className="page">
      <div className="chart-layout">
        <div className="panel">
          <div className="panel-head">自选<span className="muted" style={{ fontWeight: 400 }}>{rows.length}</span></div>
          <div className="panel-body" style={{ padding: 10 }}>
            <div className="toolbar">
              <input
                placeholder="加自选，如 SUI"
                value={adding}
                onChange={(e) => setAdding(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && add()}
                style={{ flex: 1, minWidth: 0 }}
              />
              <button className="primary" onClick={add}>+</button>
            </div>
            {err && <div className="msg err">{err}</div>}
          </div>
          <div>
            {rows.map((r) => (
              <div
                key={r.symbol}
                className={`wl-item${r.symbol === symbol ? ' on' : ''}`}
                onClick={() => nav(`/chart/${r.symbol}`)}
              >
                <SymbolIcon symbol={r.symbol} size={22} />
                <div>
                  <div className="sym">{r.base || r.symbol.replace('USDT', '')}</div>
                  <div className="muted" style={{ fontSize: 10 }}>
                    {r.live ? '实时' : '30s'}
                  </div>
                </div>
                <div className="px">
                  <div>{fmtPrice(r.last)}</div>
                  <div className={`chg ${trendClass(r.chgPct)}`}>{fmtPct(r.chgPct)}</div>
                </div>
                <button className="ghost sm danger" onClick={(e) => remove(r.symbol, e)}>×</button>
              </div>
            ))}
            {!rows.length && <div className="empty">还没有自选，去行情看板点 ☆ 添加</div>}
          </div>
        </div>

        <div className="col">
          <div className="panel">
            <div className="panel-head">
              <div className="sym-cell">
                <SymbolIcon symbol={symbol} size={22} />
                <span style={{ fontSize: 15 }}>{symbol}</span>
              </div>
              {current && (
                <>
                  <span className="mono" style={{ fontSize: 15 }}>{fmtPrice(current.last)}</span>
                  <span className={`mono ${trendClass(current.chgPct)}`}>{fmtPct(current.chgPct)}</span>
                  <span className="muted">
                    24h 量 ${fmtCompact(current.quoteVolume)} · 资金费率{' '}
                    <span className={trendClass(current.fundingRate)}>
                      {(current.fundingRate * 100).toFixed(4)}%
                    </span>
                  </span>
                </>
              )}
              <div className="spacer" />
              <div className="seg">
                {INTERVALS.map((i) => (
                  <button
                    key={i.v}
                    className={interval === i.v ? 'on' : ''}
                    onClick={() => setIntervalV(i.v)}
                  >
                    {i.label}
                  </button>
                ))}
              </div>
            </div>
            {useTV === null ? (
              <div className="empty" style={{ height: 640, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                加载图表…
              </div>
            ) : useTV ? (
              <TradingViewChart symbol={symbol} interval={interval} height={640} />
            ) : (
              <>
                <div className="msg info" style={{ margin: '10px 14px 0' }}>
                  {tvReason || 'TradingView 没有该合约，使用内置图表'}
                </div>
                <NativeChart symbol={symbol} interval={interval} height={600} />
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
