import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { ExchangeAccount } from '../components/ExchangeAccount'
import { SortHeader } from '../components/SortHeader'
import { SymbolIcon } from '../components/SymbolIcon'
import { useSort } from '../lib/useSort'
import { api, type PortfolioSummary, type Trade } from '../lib/api'
import { fmtDate, fmtPct, fmtPrice, fmtTime, fmtUsd, trendClass } from '../lib/format'

function StatCard({ label, value, sub, cls }: {
  label: string; value: string; sub?: string; cls?: string
}) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className={`value ${cls || ''}`}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  )
}

export default function Portfolio() {
  const [sum, setSum] = useState<PortfolioSummary | null>(null)
  const [curve, setCurve] = useState<{ t: number; realized: number }[]>([])
  const [trades, setTrades] = useState<Trade[]>([])
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err' | 'info'; text: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [showCsv, setShowCsv] = useState(false)
  const [csv, setCsv] = useState('')

  const [form, setForm] = useState({
    symbol: '', side: 'BUY' as 'BUY' | 'SELL',
    qty: '', price: '', fee: '', date: '', note: '',
  })

  const reload = useCallback(async () => {
    try {
      const [s, c, t] = await Promise.all([api.summary(), api.curve(), api.trades()])
      setSum(s); setCurve(c.points); setTrades(t)
    } catch (e) { setMsg({ kind: 'err', text: (e as Error).message }) }
  }, [])

  useEffect(() => {
    reload()
    const t = setInterval(reload, 20000)   // 跟随行情刷新，让浮盈动起来
    return () => clearInterval(t)
  }, [reload])

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true); setMsg(null)
    try {
      await api.addTrade({
        symbol: form.symbol.trim().toUpperCase(),
        side: form.side,
        qty: Number(form.qty),
        price: Number(form.price),
        fee: Number(form.fee || 0),
        traded_at: form.date ? new Date(form.date).getTime() : null,
        note: form.note,
      })
      setForm({ ...form, qty: '', price: '', fee: '', note: '' })
      setMsg({ kind: 'ok', text: '已记录' })
      await reload()
    } catch (ex) {
      setMsg({ kind: 'err', text: (ex as Error).message })
    } finally { setBusy(false) }
  }

  const doImport = async () => {
    setBusy(true); setMsg(null)
    try {
      const r = await api.importCsv(csv)
      setMsg({
        kind: r.failed ? 'info' : 'ok',
        text: `导入 ${r.inserted} 条${r.failed ? `，${r.failed} 条失败：${r.errors.map((e) => `第${e.line}行 ${e.error}`).join('; ')}` : ''}`,
      })
      if (r.inserted) { setCsv(''); await reload() }
    } catch (e) {
      setMsg({ kind: 'err', text: (e as Error).message })
    } finally { setBusy(false) }
  }

  const del = async (id: number) => {
    try { await api.deleteTrade(id); await reload() }
    catch (e) { setMsg({ kind: 'err', text: (e as Error).message }) }
  }

  // 本地流水靠手动/定时同步，可能落后于交易所；缺哪些标的要说清楚
  const [exchangeSymbols, setExchangeSymbols] = useState<string[]>([])
  useEffect(() => {
    // 独立定时器，不挂在 sum 上：否则本地数据每刷新一次就白白多打一次接口
    const load = () =>
      api.accountOverview()
        .then((d) => setExchangeSymbols(d.positions.map((p) => p.symbol)))
        .catch(() => { /* 未配置凭据时忽略 */ })
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [])

  const missing = useMemo(() => {
    const local = new Set((sum?.positions || []).filter((p) => p.qty !== 0).map((p) => p.symbol))
    return exchangeSymbols.filter((s) => !local.has(s))
  }, [exchangeSymbols, sum])

  const s = sum?.summary
  const open = useMemo(() => sum?.positions.filter((p) => p.qty !== 0) || [], [sum])
  const closed = useMemo(() => sum?.positions.filter((p) => p.qty === 0) || [], [sum])
  const openSort = useSort(open, 'value')
  const closedSort = useSort(closed, 'realized')
  const tradeSort = useSort(trades, 'traded_at')

  return (
    <div className="page col">
      <ExchangeAccount onSynced={reload} />

      <div className="panel-head" style={{ border: 'none', padding: '4px 0', color: 'var(--text-dim)' }}>
        本地流水分析
        <span className="muted" style={{ fontWeight: 400 }}>
          按记账流水推算，用于历史盈亏；当前持仓以上方交易所数据为准
        </span>
      </div>
      {s?.hasIncome && Math.abs((s.totalRealized + s.totalFunding) - (s.exchangeRealized + s.exchangeCommission + s.totalFunding)) > 1 && (
        <div className="msg info">
          本地流水算出的已实现盈亏 {fmtUsd(s.totalRealized)}，交易所流水口径为{' '}
          {fmtUsd(s.exchangeRealized + s.exchangeCommission)}（含手续费）。
          差异通常意味着还有成交没同步进来——点上方「同步近 30 天成交」并适当加大天数。
        </div>
      )}
      {missing.length > 0 && (
        <div className="msg info">
          交易所有 {missing.length} 个标的（{missing.join('、')}）在本地流水里没有记录，
          下方的已实现盈亏与曲线不含它们。点上方「同步近 30 天成交」可补齐。
        </div>
      )}

      <div className="stats">
        <StatCard label="总盈亏" value={fmtUsd(s?.totalPnl)} cls={trendClass(s?.totalPnl)}
                  sub="已实现 + 浮动 + 资金费" />
        <StatCard label="已实现" value={fmtUsd(s?.totalRealized)} cls={trendClass(s?.totalRealized)}
                  sub={`含手续费 ${fmtUsd(s?.totalFee)}`} />
        <StatCard label="浮动盈亏" value={fmtUsd(s?.totalUnrealized)} cls={trendClass(s?.totalUnrealized)}
                  sub="按当前标记价" />
        <StatCard label="资金费" value={fmtUsd(s?.totalFunding)} cls={trendClass(s?.totalFunding)}
                  sub={s?.hasIncome ? '来自交易所流水' : '点同步后可见'} />
        <StatCard label="总敞口" value={fmtUsd(s?.grossExposure)}
                  sub={`净 ${fmtUsd(s?.netExposure)}`} />
        <StatCard label="持仓数" value={String(s?.openCount ?? 0)}
                  sub={`历史交易过 ${s?.symbolCount ?? 0} 个标的`} />
      </div>

      <div className="row">
        <div className="panel" style={{ flex: 1, minWidth: 0 }}>
          <div className="panel-head">已实现盈亏曲线
            <span className="muted" style={{ fontWeight: 400 }}>
              逐笔累计，含手续费；浮盈不计入（历史时点用当前价回算会失真）
            </span>
          </div>
          <div className="panel-body" style={{ height: 260 }}>
            {curve.length > 1 ? (
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={curve} margin={{ top: 5, right: 10, bottom: 5, left: 0 }}>
                  <CartesianGrid stroke="#2a2e39" strokeDasharray="3 3" />
                  <XAxis dataKey="t" tickFormatter={fmtDate} stroke="#787b86" fontSize={11} />
                  <YAxis stroke="#787b86" fontSize={11} width={70}
                         tickFormatter={(v: number) => `$${v.toFixed(0)}`} />
                  <Tooltip
                    contentStyle={{ background: '#1e222d', border: '1px solid #2a2e39',
                                    borderRadius: 6, fontSize: 12 }}
                    labelFormatter={(v) => fmtTime(Number(v))}
                    formatter={(v: number) => [fmtUsd(v), '累计已实现']}
                  />
                  <Line type="monotone" dataKey="realized" stroke="#2962ff" strokeWidth={2}
                        dot={false} />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <div className="empty">至少需要 2 笔交易才能画曲线</div>
            )}
          </div>
        </div>

      </div>

      <div className="panel">
        <div className="panel-head">
          当前持仓<span className="muted" style={{ fontWeight: 400 }}>{open.length}</span>
        </div>
        {open.length ? (
          <table>
            <thead>
              <tr>
                <SortHeader label="标的" sortKey="symbol" current={openSort.sortKey} dir={openSort.sortDir} onSort={openSort.toggle} />
                <SortHeader label="方向" sortKey="side" current={openSort.sortKey} dir={openSort.sortDir} onSort={openSort.toggle} />
                <SortHeader label="持仓量" sortKey="qty" current={openSort.sortKey} dir={openSort.sortDir} onSort={openSort.toggle} right />
                <SortHeader label="开仓均价" sortKey="avgCost" current={openSort.sortKey} dir={openSort.sortDir} onSort={openSort.toggle} right />
                <SortHeader label="标记价" sortKey="markPrice" current={openSort.sortKey} dir={openSort.sortDir} onSort={openSort.toggle} right />
                <SortHeader label="名义价值" sortKey="value" current={openSort.sortKey} dir={openSort.sortDir} onSort={openSort.toggle} right />
                <SortHeader label="浮动盈亏" sortKey="unrealized" current={openSort.sortKey} dir={openSort.sortDir} onSort={openSort.toggle} right />
                <SortHeader label="收益率" sortKey="unrealizedPct" current={openSort.sortKey} dir={openSort.sortDir} onSort={openSort.toggle} right />
                <SortHeader label="已实现" sortKey="realized" current={openSort.sortKey} dir={openSort.sortDir} onSort={openSort.toggle} right />
              </tr>
            </thead>
            <tbody>
              {openSort.sorted.map((p) => (
                <tr key={p.symbol}>
                  <td className="sym">
                    <div className="sym-cell"><SymbolIcon symbol={p.symbol} />{p.symbol}</div>
                  </td>
                  <td><span className={`tag ${p.side.toLowerCase()}`}>{p.side === 'LONG' ? '多' : '空'}</span></td>
                  <td className="right mono">{p.qty}</td>
                  <td className="right mono">{fmtPrice(p.avgCost)}</td>
                  <td className="right mono">{fmtPrice(p.markPrice)}</td>
                  <td className="right mono">{fmtUsd(p.value)}</td>
                  <td className={`right mono ${trendClass(p.unrealized)}`}>{fmtUsd(p.unrealized)}</td>
                  <td className={`right mono ${trendClass(p.unrealizedPct)}`}>{fmtPct(p.unrealizedPct)}</td>
                  <td className={`right mono ${trendClass(p.realized)}`}>{fmtUsd(p.realized)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty">暂无持仓。在下方录入交易，或配置 API secret 后从 Binance 同步。</div>
        )}
      </div>

      {closed.length > 0 && (
        <div className="panel">
          <div className="panel-head">已平仓标的<span className="muted" style={{ fontWeight: 400 }}>{closed.length}</span></div>
          <table>
            <thead>
              <tr>
                <SortHeader label="标的" sortKey="symbol" current={closedSort.sortKey} dir={closedSort.sortDir} onSort={closedSort.toggle} />
                <SortHeader label="已实现盈亏" sortKey="realized" current={closedSort.sortKey} dir={closedSort.sortDir} onSort={closedSort.toggle} right />
                <SortHeader label="手续费" sortKey="fee" current={closedSort.sortKey} dir={closedSort.sortDir} onSort={closedSort.toggle} right />
                <SortHeader label="资金费" sortKey="funding" current={closedSort.sortKey} dir={closedSort.sortDir} onSort={closedSort.toggle} right />
                <SortHeader label="成交笔数" sortKey="tradeCount" current={closedSort.sortKey} dir={closedSort.sortDir} onSort={closedSort.toggle} right />
                <SortHeader label="最后交易" sortKey="lastAt" current={closedSort.sortKey} dir={closedSort.sortDir} onSort={closedSort.toggle} right />
              </tr>
            </thead>
            <tbody>
              {closedSort.sorted.map((p) => (
                <tr key={p.symbol}>
                  <td className="sym">
                    <div className="sym-cell"><SymbolIcon symbol={p.symbol} size={18} />{p.symbol}</div>
                  </td>
                  <td className={`right mono ${trendClass(p.realized)}`}>{fmtUsd(p.realized)}</td>
                  <td className="right mono">{fmtUsd(p.fee)}</td>
                  <td className={`right mono ${trendClass(p.funding)}`}>{fmtUsd(p.funding)}</td>
                  <td className="right mono">{p.tradeCount}</td>
                  <td className="right muted">{fmtTime(p.lastAt)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="panel">
        <div className="panel-head">
          记一笔交易
          <div className="spacer" />
          <button className="ghost" onClick={() => setShowCsv(!showCsv)}>
            {showCsv ? '收起 CSV 导入' : 'CSV 批量导入'}
          </button>
        </div>
        <div className="panel-body">
          <form onSubmit={submit} className="form-grid">
            <div className="field">
              <label>标的</label>
              <input required placeholder="BTCUSDT" value={form.symbol}
                     onChange={(e) => setForm({ ...form, symbol: e.target.value })} />
            </div>
            <div className="field">
              <label>方向</label>
              <select value={form.side}
                      onChange={(e) => setForm({ ...form, side: e.target.value as 'BUY' | 'SELL' })}>
                <option value="BUY">买入 / 开多</option>
                <option value="SELL">卖出 / 开空</option>
              </select>
            </div>
            <div className="field">
              <label>数量</label>
              <input required type="number" step="any" min="0" placeholder="0.5" value={form.qty}
                     onChange={(e) => setForm({ ...form, qty: e.target.value })} />
            </div>
            <div className="field">
              <label>成交价</label>
              <input required type="number" step="any" min="0" placeholder="77000" value={form.price}
                     onChange={(e) => setForm({ ...form, price: e.target.value })} />
            </div>
            <div className="field">
              <label>手续费</label>
              <input type="number" step="any" min="0" placeholder="0" value={form.fee}
                     onChange={(e) => setForm({ ...form, fee: e.target.value })} />
            </div>
            <div className="field">
              <label>成交时间（留空=现在）</label>
              <input type="datetime-local" value={form.date}
                     onChange={(e) => setForm({ ...form, date: e.target.value })} />
            </div>
            <div className="field">
              <label>备注</label>
              <input placeholder="可选" value={form.note}
                     onChange={(e) => setForm({ ...form, note: e.target.value })} />
            </div>
            <button className="primary" type="submit" disabled={busy}>记录</button>
          </form>

          {showCsv && (
            <div style={{ marginTop: 14 }}>
              <div className="muted" style={{ marginBottom: 6 }}>
                表头需含 <code>symbol,side,qty,price</code>，可选 <code>fee,traded_at,note</code>；
                时间支持毫秒/秒时间戳或 <code>2026-09-01 14:30:00</code>
              </div>
              <textarea
                rows={6}
                placeholder={'symbol,side,qty,price,fee,traded_at,note\nBTCUSDT,BUY,0.5,76000,2.1,2026-09-01 10:00:00,建仓\nBTCUSDT,SELL,0.2,78000,0.9,2026-09-05 15:30:00,止盈'}
                value={csv}
                onChange={(e) => setCsv(e.target.value)}
              />
              <div className="toolbar" style={{ marginTop: 8 }}>
                <button className="primary" onClick={doImport} disabled={busy || !csv.trim()}>
                  导入
                </button>
                <button className="ghost" onClick={() => setCsv('')}>清空</button>
              </div>
            </div>
          )}

          {msg && <div className={`msg ${msg.kind}`}>{msg.text}</div>}
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">交易流水<span className="muted" style={{ fontWeight: 400 }}>{trades.length} 笔</span></div>
        {trades.length ? (
          <div className="table-scroll" style={{ maxHeight: 360 }}>
            <table>
              <thead>
                <tr>
                  <SortHeader label="时间" sortKey="traded_at" current={tradeSort.sortKey} dir={tradeSort.sortDir} onSort={tradeSort.toggle} />
                  <SortHeader label="标的" sortKey="symbol" current={tradeSort.sortKey} dir={tradeSort.sortDir} onSort={tradeSort.toggle} />
                  <SortHeader label="方向" sortKey="side" current={tradeSort.sortKey} dir={tradeSort.sortDir} onSort={tradeSort.toggle} />
                  <SortHeader label="数量" sortKey="qty" current={tradeSort.sortKey} dir={tradeSort.sortDir} onSort={tradeSort.toggle} right />
                  <SortHeader label="价格" sortKey="price" current={tradeSort.sortKey} dir={tradeSort.sortDir} onSort={tradeSort.toggle} right />
                  <SortHeader label="手续费" sortKey="fee" current={tradeSort.sortKey} dir={tradeSort.sortDir} onSort={tradeSort.toggle} right />
                  <SortHeader label="备注" sortKey="note" current={tradeSort.sortKey} dir={tradeSort.sortDir} onSort={tradeSort.toggle} />
                  <th />
                </tr>
              </thead>
              <tbody>
                {tradeSort.sorted.map((t) => (
                  <tr key={t.id}>
                    <td className="muted">{fmtTime(t.traded_at)}</td>
                    <td className="sym">
                      <div className="sym-cell"><SymbolIcon symbol={t.symbol} size={18} />{t.symbol}</div>
                    </td>
                    <td className={t.side === 'BUY' ? 'up' : 'down'}>
                      {t.side === 'BUY' ? '买入' : '卖出'}
                    </td>
                    <td className="right mono">{t.qty}</td>
                    <td className="right mono">{fmtPrice(t.price)}</td>
                    <td className="right mono">{t.fee ? fmtUsd(t.fee) : '—'}</td>
                    <td className="muted">{t.note || '—'}</td>
                    <td className="right">
                      <button className="ghost sm danger" onClick={() => del(t.id)}>删除</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty">还没有交易记录</div>
        )}
      </div>
    </div>
  )
}
