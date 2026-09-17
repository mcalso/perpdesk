import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { liveFeed, type LiveRow } from '../lib/ws'
import { fmtPct, fmtPrice, trendClass } from '../lib/format'

const WATCH = ['BTCUSDT', 'ETHUSDT']

/**
 * 顶栏常驻的大盘行情。
 *
 * 不管在哪一页都能看到 BTC/ETH —— 交易时最常问的就是"大盘怎么样"，
 * 为这个来回切页面很烦。数据走已有的全局 WS 单例，不额外发请求，
 * 也不依赖自选列表（hub 推的是全量快照）。
 */
export function MarketPulse() {
  const nav = useNavigate()
  const [rows, setRows] = useState<Map<string, LiveRow>>(new Map())
  useEffect(() => liveFeed.subscribe((m) => setRows(new Map(m))), [])

  const shown = WATCH.map((s) => rows.get(s)).filter(Boolean) as LiveRow[]
  if (!shown.length) return null

  return (
    <div className="pulse">
      {shown.map((r) => (
        <button key={r.symbol} className="pulse-item"
                onClick={() => nav(`/chart/${r.symbol}`, { viewTransition: true })}
                title={`查看 ${r.symbol} 图表`}>
          <span className="pulse-sym">{r.symbol.replace('USDT', '')}</span>
          <span className="mono">{fmtPrice(r.last)}</span>
          <span className={`mono ${trendClass(r.chgPct)}`}>{fmtPct(r.chgPct)}</span>
        </button>
      ))}
    </div>
  )
}
