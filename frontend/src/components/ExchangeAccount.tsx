import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts'
import { SortHeader } from './SortHeader'
import { SymbolIcon } from './SymbolIcon'
import { useSort } from '../lib/useSort'
import { api, type AccountOverview, type AccountStatus } from '../lib/api'
import { fmtPct, fmtPrice, fmtUsd, trendClass } from '../lib/format'

const PIE_COLORS = ['#2962ff', '#26a69a', '#ff9800', '#ab47bc', '#ef5350',
                    '#26c6da', '#9ccc65', '#ffa726', '#5c6bc0', '#8d6e63']

/**
 * 交易所账户实况。
 *
 * 与本地流水推算的持仓是两套数据，刻意并存：
 *   - 这里是交易所的真相（含杠杆、强平价、未实现盈亏）；
 *   - 本地流水用于历史盈亏分析，可以手工补录交易所查不到的久远记录。
 */
export function ExchangeAccount({ onSynced }: { onSynced?: () => void }) {
  const nav = useNavigate()
  const [status, setStatus] = useState<AccountStatus | null>(null)
  const [data, setData] = useState<AccountOverview | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [syncMsg, setSyncMsg] = useState('')

  const load = useCallback(async () => {
    try {
      const st = await api.accountStatus()
      setStatus(st)
      if (!st.configured) return
      setData(await api.accountOverview())
      setErr('')
    } catch (e) { setErr((e as Error).message) }
  }, [])

  useEffect(() => {
    load()
    // 浮动盈亏在后端用 1 秒级的实时标记价本地重算，所以这里读得勤一点就能跳动；
    // 读的是后端缓存，多开几个标签页也不会放大对交易所的请求量
    const t = setInterval(load, 2000)
    return () => clearInterval(t)
  }, [load])

  const sync = async () => {
    setBusy(true); setSyncMsg('')
    try {
      const r = await api.syncTrades(30)
      setSyncMsg(`同步 ${r.inserted} 条成交，跳过 ${r.skipped} 条重复（${r.symbols.join(', ')}）`)
      onSynced?.()
    } catch (e) { setSyncMsg(`失败：${(e as Error).message}`) }
    finally { setBusy(false) }
  }

  if (status && !status.configured) {
    return (
      <div className="panel">
        <div className="panel-head">交易所账户</div>
        <div className="panel-body muted">
          未配置 API 凭据（{status.hasKey ? '缺 secret' : '缺 key'}）。
          在 <code>backend/.env</code> 填入 <code>BINANCE_API_KEY</code> 与{' '}
          <code>BINANCE_API_SECRET</code> 后自动启用。只读即可，不需要交易权限。
        </div>
      </div>
    )
  }

  const usdt = data?.balances.find((b) => b.asset === 'USDT')
  const pie = useMemo(
    () => (data?.positions || []).map((p) => ({
      symbol: p.symbol, abs: p.notional, weight: p.weight, unrealized: p.unrealized,
    })),
    [data],
  )

  // 派生两个排序维度：收益率、距强平距离。后者是风险视角下最该能排序的一列。
  const rows = useMemo(
    () => (data?.positions || []).map((p) => ({
      ...p,
      pnlPct: p.entryPrice
        ? ((p.markPrice - p.entryPrice) / p.entryPrice) * (p.qty > 0 ? 1 : -1) * 100
        : 0,
      liqDistPct: p.liquidationPrice > 0 && p.markPrice > 0
        ? (Math.abs(p.markPrice - p.liquidationPrice) / p.markPrice) * 100
        : null,
    })),
    [data],
  )
  const { sorted, sortKey, sortDir, toggle } = useSort(rows, 'notional')

  return (
    <>
      <div className="stats">
        <div className="stat">
          <div className="label">账户权益</div>
          <div className="value">{fmtUsd(data?.equity)}</div>
          <div className="sub">钱包 {fmtUsd(usdt?.balance)} · 可用 {fmtUsd(usdt?.available)}</div>
        </div>
        <div className="stat">
          <div className="label">交易所浮动盈亏</div>
          <div className={`value ${trendClass(data?.totalUnrealized)}`}>
            {fmtUsd(data?.totalUnrealized)}
          </div>
          <div className="sub">{data?.positions.length ?? 0} 个持仓</div>
        </div>
        <div className="stat">
          <div className="label">名义敞口</div>
          <div className="value">{fmtUsd(data?.grossNotional)}</div>
          <div className="sub">
            {data && usdt && usdt.balance > 0
              ? `${(data.grossNotional / usdt.balance).toFixed(2)}x 杠杆率`
              : '—'}
          </div>
        </div>
        <div className="stat">
          <div className="label">保证金占用</div>
          <div className="value">
            {usdt ? fmtUsd(usdt.balance - usdt.available) : '—'}
          </div>
          <div className="sub">
            {usdt && usdt.balance > 0
              ? `占权益 ${(((usdt.balance - usdt.available) / usdt.balance) * 100).toFixed(0)}%`
              : '—'}
          </div>
        </div>
      </div>

      <div className="row">
      <div className="panel" style={{ flex: 3, minWidth: 0 }}>
        <div className="panel-head">
          交易所持仓
          <span className="muted" style={{ fontWeight: 400 }}>
            浮盈实时 · 持仓结构每 {data?.pollInterval ?? 6}s 校准
            {data?.ageSec != null && ` · ${data.ageSec.toFixed(0)}s 前`}
            {data?.error && ` · ⚠ ${data.error}`}
          </span>
          <div className="spacer" />
          <button onClick={sync} disabled={busy}>
            {busy ? '同步中…' : '同步近 30 天成交到本地'}
          </button>
        </div>
        {err && <div className="msg err" style={{ margin: 12 }}>{err}</div>}
        {syncMsg && <div className="msg info" style={{ margin: 12 }}>{syncMsg}</div>}
        {data?.positions.length ? (
          <table>
            <thead>
              <tr>
                <SortHeader label="标的" sortKey="symbol" current={sortKey} dir={sortDir} onSort={toggle} />
                <SortHeader label="方向" sortKey="side" current={sortKey} dir={sortDir} onSort={toggle} />
                <SortHeader label="持仓量" sortKey="qty" current={sortKey} dir={sortDir} onSort={toggle} right />
                <SortHeader label="开仓价" sortKey="entryPrice" current={sortKey} dir={sortDir} onSort={toggle} right />
                <SortHeader label="标记价" sortKey="markPrice" current={sortKey} dir={sortDir} onSort={toggle} right />
                <SortHeader label="名义价值" sortKey="notional" current={sortKey} dir={sortDir} onSort={toggle} right />
                <SortHeader label="占比" sortKey="weight" current={sortKey} dir={sortDir} onSort={toggle} right />
                <SortHeader label="浮动盈亏" sortKey="unrealized" current={sortKey} dir={sortDir} onSort={toggle} right />
                <SortHeader label="收益率" sortKey="pnlPct" current={sortKey} dir={sortDir} onSort={toggle} right />
                <SortHeader label="杠杆" sortKey="leverage" current={sortKey} dir={sortDir} onSort={toggle} right />
                <SortHeader label="距强平" sortKey="liqDistPct" current={sortKey} dir={sortDir}
                            onSort={toggle} right title="标记价距强平价的百分比，升序排在最前的是最危险的仓位" />
                <SortHeader label="强平价" sortKey="liquidationPrice" current={sortKey} dir={sortDir} onSort={toggle} right />
              </tr>
            </thead>
            <tbody>
              {sorted.map((p) => {
                const near = p.liqDistPct !== null && p.liqDistPct < 15
                return (
                  <tr key={p.symbol} className="clickable"
                      onClick={() => nav(`/chart/${p.symbol}`)}>
                    <td className="sym">
                      <div className="sym-cell">
                        <SymbolIcon symbol={p.symbol} />
                        {p.symbol}
                      </div>
                    </td>
                    <td><span className={`tag ${p.side.toLowerCase()}`}>
                      {p.side === 'LONG' ? '多' : '空'}</span></td>
                    <td className="right mono">{p.qty}</td>
                    <td className="right mono">{fmtPrice(p.entryPrice)}</td>
                    <td className="right mono">{fmtPrice(p.markPrice)}</td>
                    <td className="right mono">{fmtUsd(p.notional)}</td>
                    <td className="right">
                      <div className="weight-cell">
                        <div className="weight-track">
                          <div className="weight-bar" style={{ width: `${Math.min(p.weight, 100)}%` }} />
                        </div>
                        <span className="mono">{p.weight.toFixed(1)}%</span>
                      </div>
                    </td>
                    <td className={`right mono ${trendClass(p.unrealized)}`}>
                      {fmtUsd(p.unrealized)}
                    </td>
                    <td className={`right mono ${trendClass(p.pnlPct)}`}>{fmtPct(p.pnlPct)}</td>
                    <td className="right mono">{p.leverage}x</td>
                    <td className={`right mono ${near ? 'down' : 'muted'}`}
                        title={near ? '距强平价不足 15%' : ''}>
                      {p.liqDistPct !== null ? `${p.liqDistPct.toFixed(1)}%` : '—'}
                    </td>
                    <td className={`right mono ${near ? 'down' : 'muted'}`}>
                      {p.liquidationPrice > 0 ? fmtPrice(p.liquidationPrice) : '—'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        ) : (
          <div className="empty">{data ? '当前无持仓' : '加载中…'}</div>
        )}
      </div>

      <div className="panel" style={{ flex: 1, minWidth: 0 }}>
        <div className="panel-head">
          持仓分布
          <span className="muted" style={{ fontWeight: 400 }}>按名义敞口</span>
        </div>
        <div className="panel-body" style={{ height: 300 }}>
          {pie.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie data={pie} dataKey="abs" nameKey="symbol" cx="50%" cy="50%"
                     innerRadius={50} outerRadius={95} paddingAngle={2}>
                  {pie.map((_, i) => (
                    <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} stroke="#131722" />
                  ))}
                </Pie>
                <Tooltip
                  contentStyle={{ background: '#1e222d', border: '1px solid #2a2e39',
                                  borderRadius: 6, fontSize: 12 }}
                  formatter={(v: number, _n, p) => {
                    const d = p.payload as { symbol: string; weight: number; unrealized: number }
                    return [`${fmtUsd(v)} · ${d.weight.toFixed(1)}% · 浮盈 ${fmtUsd(d.unrealized)}`,
                            d.symbol]
                  }}
                />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <div className="empty">暂无持仓</div>
          )}
        </div>
      </div>
      </div>
    </>
  )
}
