import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Line, LineChart,
  ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { useActiveAccount } from '../lib/account'
import { AnimatedNumber } from '../components/AnimatedNumber'
import { ChartDeck, type ChartSpec } from '../components/ChartDeck'
import { ExchangeAccount } from '../components/ExchangeAccount'
import { SortHeader } from '../components/SortHeader'
import { SymbolIcon } from '../components/SymbolIcon'
import { TableSkeleton } from '../components/TableSkeleton'
import { useSort } from '../lib/useSort'
import { api, type PortfolioSummary, type Trade } from '../lib/api'
import { fmtDate, fmtPrice, fmtQty, fmtTime, fmtUsd, trendClass } from '../lib/format'

const UP = '#26a69a'
const DOWN = '#ef5350'
const ACCENT = '#5b8dff'
const WARN = '#ff9800'
const GRID = '#242938'
const AXIS = '#767a88'

// 五张图共用一套 tooltip 样式
const TIP = {
  contentStyle: { background: '#1e2330', border: '1px solid #333a4d',
                  borderRadius: 8, fontSize: 12,
                  boxShadow: '0 8px 24px -6px rgba(0,0,0,.6)' },
}

// 历史盈亏的统计区间。null = 全部历史
const RANGES: { v: number | null; label: string }[] = [
  { v: 7, label: '7天' }, { v: 30, label: '30天' },
  { v: 90, label: '90天' }, { v: 365, label: '1年' }, { v: null, label: '全部' },
]

function Empty({ mode, loaded, text }: {
  mode: 'large' | 'thumb'; loaded: boolean; text: string
}) {
  if (!loaded) return <div className="skeleton skeleton-chart" />
  // 缩略图太小，放文字只会挤成一团，留空即可
  return mode === 'thumb' ? <div /> : <div className="empty">{text}</div>
}


function StatCard({ label, value, format, sub, cls, hero }: {
  label: string
  /** 传 number 并给 format 时数字会滚动；传 string 则直接显示 */
  value: string | number | null | undefined
  format?: (v: number | null | undefined) => string
  sub?: string; cls?: string; hero?: boolean
}) {
  return (
    <div className={hero ? 'stat hero' : 'stat'}>
      <div className="label">{label}</div>
      <div className={`value ${cls || ''}`}>
        {format
          ? <AnimatedNumber value={value as number | null | undefined} format={format} />
          : value}
      </div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  )
}

export default function Portfolio() {
  const acct = useActiveAccount()
  const [sum, setSum] = useState<PortfolioSummary | null>(null)
  // 同上：区分「首次还没拉回来」和「拉回来了确实没记录」，
  // 否则后端不通时会一直显示骨架屏
  const [loaded, setLoaded] = useState(false)
  const [curve, setCurve] = useState<{ t: number; realized: number }[]>([])
  const [daily, setDaily] = useState<
    { d: number; realized: number; fee: number; funding: number; trades: number }[]>([])
  const [dailyStat, setDailyStat] = useState({ winDays: 0, lossDays: 0 })
  const [trades, setTrades] = useState<Trade[]>([])
  const [tradeTotal, setTradeTotal] = useState(0)
  const [tradePage, setTradePage] = useState(0)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err' | 'info'; text: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [showCsv, setShowCsv] = useState(false)
  const [days, setDays] = useState<number | null>(null)
  const [range, setRange] = useState<{ from: number | null; to: number | null }>(
    { from: null, to: null })
  const [csv, setCsv] = useState('')

  const [form, setForm] = useState({
    symbol: '', side: 'BUY' as 'BUY' | 'SELL',
    qty: '', price: '', fee: '', date: '', note: '',
  })

  const TRADE_PAGE = 50

  const reload = useCallback(async () => {
    try {
      const [s, c, t, d] = await Promise.all([
        api.summary(days), api.curve(days),
        api.trades({ limit: TRADE_PAGE, offset: tradePage * TRADE_PAGE }),
        api.daily(days),
      ])
      setSum(s); setCurve(c.points)
      setDaily(d.rows); setDailyStat({ winDays: d.winDays, lossDays: d.lossDays })
      setTrades(t.rows); setTradeTotal(t.total)
      setRange({ from: c.from, to: c.to })
    } catch (e) { setMsg({ kind: 'err', text: (e as Error).message }) }
    finally { setLoaded(true) }
    // acct 进依赖：切账户要重算，否则盈亏还是上一个账户的
  }, [days, tradePage, acct])

  useEffect(() => {
    reload()
    // 这一区都是已落袋的历史数据，只在同步成交后才变化，无需高频轮询
    // （浮盈与持仓在上方交易所区，那边是 2 秒刷新的）
    const t = setInterval(reload, 120000)
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
  const closed = useMemo(() => sum?.positions.filter((p) => p.qty === 0) || [], [sum])
  const closedSort = useSort(closed, 'realized')
  const tradeSort = useSort(trades, 'traded_at')

  // 五张图共用两个接口的数据：曲线走 /curve，其余四张走 /daily。
  // 缩略图刻意砍掉坐标轴、网格、tooltip 与入场动画 —— 一次要同时渲染四张，
  // 留着那些既看不清也拖慢每次数据刷新。
  const charts: ChartSpec[] = useMemo(() => {
    const thumb = { top: 2, right: 2, bottom: 2, left: 2 }
    const large = { top: 5, right: 10, bottom: 5, left: 0 }
    const cumFee = (() => {
      let f = 0, g = 0
      return daily.map((d) => ({ d: d.d, fee: (f += d.fee), funding: (g += d.funding) }))
    })()
    const bySymbol = [...(sum?.positions ?? [])]
      .filter((p) => p.realized !== 0 || p.funding !== 0)
      .map((p) => ({ symbol: p.symbol.replace(/USDT$/, ''), realized: p.realized + p.funding }))
      .sort((a, b) => b.realized - a.realized)
    const ranked = bySymbol.length > 12
      ? [...bySymbol.slice(0, 6), ...bySymbol.slice(-6)]   // 只留最赚与最亏的各 6 个
      : bySymbol
    const grossRealized = (s?.totalRealized ?? 0) + (s?.totalFee ?? 0)
    const cost = [
      { name: '毛利', v: grossRealized, fill: grossRealized >= 0 ? UP : DOWN },
      { name: '手续费', v: -(s?.totalFee ?? 0), fill: DOWN },
      { name: '资金费', v: s?.totalFunding ?? 0, fill: (s?.totalFunding ?? 0) >= 0 ? UP : WARN },
      { name: '净额', v: (s?.totalRealized ?? 0) + (s?.totalFunding ?? 0),
        fill: ACCENT },
    ]
    const winRate = dailyStat.winDays + dailyStat.lossDays
      ? (dailyStat.winDays / (dailyStat.winDays + dailyStat.lossDays)) * 100 : 0

    return [
      {
        key: 'curve',
        title: '已实现盈亏曲线',
        sub: `${days ? `最近 ${days} 天的区间损益` : '全部历史逐笔累计'}，含手续费；浮盈不计入（历史时点用当前价回算会失真）`,
        badge: { text: fmtUsd(curve.length ? curve[curve.length - 1].realized : 0),
                 cls: trendClass(curve.length ? curve[curve.length - 1].realized : 0) },
        render: (mode) => curve.length > 1 ? (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={curve} margin={mode === 'thumb' ? thumb : large}>
              <defs>
                <linearGradient id="pnlFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={ACCENT} stopOpacity={0.45} />
                  <stop offset="100%" stopColor={ACCENT} stopOpacity={0} />
                </linearGradient>
              </defs>
              {mode === 'large' && <CartesianGrid stroke={GRID} strokeDasharray="3 3" />}
              {mode === 'large' && <XAxis dataKey="t" tickFormatter={fmtDate} stroke={AXIS} fontSize={11} />}
              {mode === 'large' && <YAxis stroke={AXIS} fontSize={11} width={70}
                                          tickFormatter={(v: number) => `$${v.toFixed(0)}`} />}
              {mode === 'large' && (
                <Tooltip {...TIP} labelFormatter={(v) => fmtTime(Number(v))}
                         formatter={(v: number) => [fmtUsd(v), '累计已实现']} />
              )}
              <Area type="monotone" dataKey="realized" stroke={ACCENT} strokeWidth={2}
                    fill="url(#pnlFill)" dot={false} isAnimationActive={mode === 'large'}
                    activeDot={mode === 'large' ? { r: 4, strokeWidth: 0 } : false} />
            </AreaChart>
          </ResponsiveContainer>
        ) : <Empty mode={mode} loaded={loaded} text={sum ? '至少需要 2 笔交易才能画曲线' : '读不到盈亏数据'} />,
      },
      {
        key: 'daily',
        title: '每日盈亏',
        sub: '按本地自然日分桶，含手续费与资金费',
        badge: { text: `${dailyStat.winDays} 胜 / ${dailyStat.lossDays} 负`,
                 cls: winRate >= 50 ? 'up' : 'down' },
        render: (mode) => daily.length ? (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={daily} margin={mode === 'thumb' ? thumb : large}>
              {mode === 'large' && <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />}
              {mode === 'large' && <XAxis dataKey="d" tickFormatter={fmtDate} stroke={AXIS} fontSize={11} />}
              {mode === 'large' && <YAxis stroke={AXIS} fontSize={11} width={70}
                                          tickFormatter={(v: number) => `$${v.toFixed(0)}`} />}
              {mode === 'large' && (
                <Tooltip {...TIP} cursor={{ fill: 'rgba(255,255,255,.04)' }}
                         labelFormatter={(v) => fmtDate(Number(v))}
                         formatter={(v: number, _n, p) => {
                           const r = p.payload as { fee: number; funding: number; trades: number }
                           return [`${fmtUsd(v)} · ${r.trades} 笔 · 手续费 ${fmtUsd(r.fee)}`, '当日盈亏']
                         }} />
              )}
              {mode === 'large' && <ReferenceLine y={0} stroke={AXIS} />}
              <Bar dataKey="realized" isAnimationActive={mode === 'large'}>
                {daily.map((d, i) => (
                  <Cell key={i} fill={d.realized >= 0 ? UP : DOWN} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        ) : <Empty mode={mode} loaded={loaded} text="这段时间没有成交" />,
      },
      {
        key: 'symbols',
        title: '标的盈亏排行',
        sub: ranked.length < bySymbol.length ? '只显示最赚与最亏的各 6 个' : '已实现 + 资金费',
        badge: { text: `${bySymbol.length} 个标的` },
        render: (mode) => ranked.length ? (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={ranked} layout="vertical"
                      margin={mode === 'thumb' ? thumb : { top: 5, right: 20, bottom: 5, left: 10 }}>
              {mode === 'large' && <CartesianGrid stroke={GRID} strokeDasharray="3 3" horizontal={false} />}
              <XAxis type="number" hide={mode === 'thumb'} stroke={AXIS} fontSize={11}
                     tickFormatter={(v: number) => `$${v.toFixed(0)}`} />
              <YAxis type="category" dataKey="symbol" hide={mode === 'thumb'}
                     stroke={AXIS} fontSize={11} width={72} />
              {mode === 'large' && (
                <Tooltip {...TIP} cursor={{ fill: 'rgba(255,255,255,.04)' }}
                         formatter={(v: number) => [fmtUsd(v), '已实现 + 资金费']} />
              )}
              <Bar dataKey="realized" isAnimationActive={mode === 'large'}>
                {ranked.map((r, i) => (
                  <Cell key={i} fill={r.realized >= 0 ? UP : DOWN} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        ) : <Empty mode={mode} loaded={loaded} text="还没有已平仓的标的" />,
      },
      {
        key: 'cost',
        title: '成本构成',
        sub: '毛利被手续费和资金费吃掉多少',
        badge: { text: fmtUsd(s?.totalFee), cls: 'down' },
        render: (mode) => (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={cost} margin={mode === 'thumb' ? thumb : large}>
              {mode === 'large' && <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />}
              {mode === 'large' && <XAxis dataKey="name" stroke={AXIS} fontSize={11} />}
              {mode === 'large' && <YAxis stroke={AXIS} fontSize={11} width={70}
                                          tickFormatter={(v: number) => `$${v.toFixed(0)}`} />}
              {mode === 'large' && (
                <Tooltip {...TIP} cursor={{ fill: 'rgba(255,255,255,.04)' }}
                         formatter={(v: number) => [fmtUsd(v), '金额']} />
              )}
              {mode === 'large' && <ReferenceLine y={0} stroke={AXIS} />}
              <Bar dataKey="v" isAnimationActive={mode === 'large'}>
                {cost.map((c, i) => <Cell key={i} fill={c.fill} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        ),
      },
      {
        key: 'fees',
        title: '成本累计',
        sub: '手续费与资金费随时间的累计值',
        badge: { text: fmtUsd(-(s?.totalFee ?? 0)), cls: 'down' },
        render: (mode) => cumFee.length > 1 ? (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={cumFee} margin={mode === 'thumb' ? thumb : large}>
              {mode === 'large' && <CartesianGrid stroke={GRID} strokeDasharray="3 3" />}
              {mode === 'large' && <XAxis dataKey="d" tickFormatter={fmtDate} stroke={AXIS} fontSize={11} />}
              {mode === 'large' && <YAxis stroke={AXIS} fontSize={11} width={70}
                                          tickFormatter={(v: number) => `$${v.toFixed(0)}`} />}
              {mode === 'large' && (
                <Tooltip {...TIP} labelFormatter={(v) => fmtDate(Number(v))}
                         formatter={(v: number, n) => [fmtUsd(v), n === 'fee' ? '累计手续费' : '累计资金费']} />
              )}
              <Line type="monotone" dataKey="fee" stroke={DOWN} strokeWidth={2} dot={false}
                    isAnimationActive={mode === 'large'} />
              <Line type="monotone" dataKey="funding" stroke={WARN} strokeWidth={2} dot={false}
                    isAnimationActive={mode === 'large'} />
            </LineChart>
          </ResponsiveContainer>
        ) : <Empty mode={mode} loaded={loaded} text="这段时间没有成本记录" />,
      },
    ]
  }, [curve, daily, dailyStat, sum, s, days, loaded])

  return (
    <div className="page col">
      <ExchangeAccount onSynced={reload} />

      <div className="panel-head" style={{ border: 'none', padding: '4px 0', color: 'var(--text-dim)' }}>
        历史盈亏分析
        <span className="muted" style={{ fontWeight: 400 }}>
          按成交流水回放，统计已落袋的损益；当前持仓与浮盈以上方交易所数据为准
        </span>
        <div className="spacer" />
        <div className="seg">
          {RANGES.map((r) => (
            <button key={r.label} className={days === r.v ? 'on' : ''} onClick={() => setDays(r.v)}>
              {r.label}
            </button>
          ))}
        </div>
      </div>
      {range.from && (
        <div className="muted" style={{ fontSize: 11, marginTop: -8 }}>
          统计区间：{fmtTime(range.from)} ~ {fmtTime(range.to || Date.now())}
          {days ? '（区间损益，起点归零）' : '（全部历史，从第一笔成交起算）'}
        </div>
      )}
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
        <StatCard label="已实现盈亏" value={s?.totalRealized} format={fmtUsd}
                  cls={trendClass(s?.totalRealized)} sub="平仓损益，已扣手续费" hero />
        <StatCard label="资金费" value={s?.totalFunding} format={fmtUsd}
                  cls={trendClass(s?.totalFunding)}
                  sub={s?.hasIncome ? '来自交易所流水' : '点同步后可见'} />
        <StatCard label="手续费" value={s?.totalFee} format={fmtUsd} cls="down"
                  sub="累计支出" />
        <StatCard label="落袋合计" value={(s?.totalRealized ?? 0) + (s?.totalFunding ?? 0)}
                  format={fmtUsd}
                  cls={trendClass((s?.totalRealized ?? 0) + (s?.totalFunding ?? 0))}
                  sub="已实现 + 资金费，不含浮盈" />
        <StatCard label="交易标的" value={String(s?.symbolCount ?? 0)}
                  sub={`当前持仓 ${s?.openCount ?? 0} 个`} />
      </div>

      <ChartDeck charts={charts} height={300} />

      {closed.length > 0 && (
        <div className="panel">
          <div className="panel-head">已平仓标的<span className="muted" style={{ fontWeight: 400 }}>{closed.length}</span></div>
          <table className="cards">
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
                  <td className="sym card-title">
                    <div className="sym-cell"><SymbolIcon symbol={p.symbol} size={18} />{p.symbol}</div>
                  </td>
                  <td className={`right mono ${trendClass(p.realized)}`} data-label="已实现盈亏">
                    {fmtUsd(p.realized)}
                  </td>
                  <td className="right mono" data-label="手续费">{fmtUsd(p.fee)}</td>
                  <td className={`right mono ${trendClass(p.funding)}`} data-label="资金费">
                    {fmtUsd(p.funding)}
                  </td>
                  <td className="right mono" data-label="成交笔数">{p.tradeCount}</td>
                  <td className="right muted" data-label="最后交易">{fmtTime(p.lastAt)}</td>
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
        <div className="panel-head">
          交易流水
          <span className="muted" style={{ fontWeight: 400 }}>
            共 {tradeTotal.toLocaleString()} 笔
          </span>
          <div className="spacer" />
          <button className="sm" disabled={tradePage === 0}
                  onClick={() => setTradePage(tradePage - 1)}>上一页</button>
          <span className="muted">
            {tradeTotal ? `${tradePage * 50 + 1}–${Math.min((tradePage + 1) * 50, tradeTotal)}` : '—'}
          </span>
          <button className="sm" disabled={(tradePage + 1) * 50 >= tradeTotal}
                  onClick={() => setTradePage(tradePage + 1)}>下一页</button>
        </div>
        {trades.length ? (
          <div className="table-scroll" style={{ maxHeight: 360 }}>
            <table className="cards">
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
                    <td className="muted" data-label="时间">{fmtTime(t.traded_at)}</td>
                    <td className="sym card-title">
                      <div className="sym-cell"><SymbolIcon symbol={t.symbol} size={18} />{t.symbol}</div>
                    </td>
                    <td className={t.side === 'BUY' ? 'up' : 'down'} data-label="方向">
                      {t.side === 'BUY' ? '买入' : '卖出'}
                    </td>
                    <td className="right mono" data-label="数量">{fmtQty(t.qty)}</td>
                    <td className="right mono" data-label="价格">{fmtPrice(t.price)}</td>
                    <td className="right mono" data-label="手续费">{t.fee ? fmtUsd(t.fee) : '—'}</td>
                    <td className="muted" data-label="备注">{t.note || '—'}</td>
                    <td className="right card-corner">
                      <button className="ghost sm danger" onClick={() => del(t.id)}>删除</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : loaded ? (
          <div className="empty">{sum ? '还没有交易记录' : '读不到交易数据'}</div>
        ) : (
          <TableSkeleton rows={8} cols={8} />
        )}
      </div>
    </div>
  )
}
