import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { SymbolIcon } from '../components/SymbolIcon'
import { api, type Ticker } from '../lib/api'
import { liveFeed, type LiveRow } from '../lib/ws'
import { fmtCompact, fmtPct, fmtPrice, trendClass } from '../lib/format'

type SortKey = 'chgPct' | 'quoteVolume' | 'fundingRate' | 'symbol' | 'last'

// Binance 现在既有加密永续，也有股票/指数代币化永续(TRADIFI_PERPETUAL)，分开看更清楚。
// 类别列表从行情数据里动态取，Binance 新增品类时不用改代码。
const CLASS_LABEL: Record<string, string> = {
  crypto: '加密', us_equity: '美股', hk_equity: '港股', cn_equity: 'A股',
  kr_equity: '韩股', index: '指数', commodity: '商品', premarket: '盘前',
}
const classLabel = (v: string) => CLASS_LABEL[v] || v

const COLUMNS: { key: SortKey; label: string; right?: boolean }[] = [
  { key: 'symbol', label: '标的' },
  { key: 'last', label: '最新价', right: true },
  { key: 'chgPct', label: '24h 涨跌', right: true },
  { key: 'quoteVolume', label: '24h 成交额', right: true },
  { key: 'fundingRate', label: '资金费率', right: true },
]

export default function Market() {
  const nav = useNavigate()
  const [rows, setRows] = useState<Ticker[]>([])
  const [live, setLive] = useState<Map<string, LiveRow>>(new Map())
  const [watch, setWatch] = useState<Set<string>>(new Set())
  const [sort, setSort] = useState<SortKey>('quoteVolume')
  const [desc, setDesc] = useState(true)
  const [search, setSearch] = useState('')
  const [cls, setCls] = useState('all')
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)

  // 首屏与兜底刷新走 REST（字段最全），价格跳动交给 WS
  useEffect(() => {
    let alive = true
    const load = () =>
      api.tickers({ limit: 1000 })
        .then((r) => { if (alive) { setRows(r.rows); setErr(''); setLoading(false) } })
        .catch((e: Error) => { if (alive) { setErr(e.message); setLoading(false) } })
    load()
    const t = setInterval(load, 30000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  useEffect(() => liveFeed.subscribe((m) => setLive(new Map(m))), [])

  useEffect(() => {
    api.watchlist()
      .then((r) => setWatch(new Set(r.rows.map((x) => x.symbol))))
      .catch(() => { /* 自选拉取失败不影响看板主体 */ })
  }, [])

  const toggleWatch = async (symbol: string, e: React.MouseEvent) => {
    e.stopPropagation()
    const next = new Set(watch)
    try {
      if (watch.has(symbol)) { await api.removeWatch(symbol); next.delete(symbol) }
      else { await api.addWatch(symbol); next.add(symbol) }
      setWatch(next)
    } catch (ex) { setErr((ex as Error).message) }
  }

  // WS 只覆盖价格类字段，成交额等仍以 REST 快照为准
  const merged = useMemo(() => {
    const out = rows.map((r) => {
      const l = live.get(r.symbol)
      return l ? { ...r, last: l.last, chgPct: l.chgPct, markPrice: l.markPrice } : r
    })
    const needle = search.trim().toUpperCase()
    let filtered = needle ? out.filter((r) => r.symbol.includes(needle)) : out
    if (cls !== 'all') filtered = filtered.filter((r) => r.assetClass === cls)
    const dir = desc ? -1 : 1
    return [...filtered].sort((a, b) => {
      const av = a[sort], bv = b[sort]
      if (typeof av === 'string' || typeof bv === 'string')
        return String(av).localeCompare(String(bv)) * dir
      return ((av as number) - (bv as number)) * dir
    })
  }, [rows, live, sort, desc, search, cls])

  // 按标的数量排序，常用的排前面
  const classes = useMemo(() => {
    const count = new Map<string, number>()
    for (const r of rows) count.set(r.assetClass, (count.get(r.assetClass) || 0) + 1)
    return [...count.entries()].sort((a, b) => b[1] - a[1])
  }, [rows])

  const stats = useMemo(() => {
    const scope = cls === 'all' ? rows : rows.filter((r) => r.assetClass === cls)
    const up = scope.filter((r) => r.chgPct > 0).length
    const down = scope.filter((r) => r.chgPct < 0).length
    const vol = scope.reduce((s, r) => s + r.quoteVolume, 0)
    const sorted = [...scope].sort((a, b) => b.chgPct - a.chgPct)
    return { n: scope.length, up, down, vol, top: sorted[0], bottom: sorted[sorted.length - 1] }
  }, [rows, cls])

  const clickSort = (k: SortKey) => {
    if (k === sort) setDesc(!desc)
    else { setSort(k); setDesc(true) }
  }

  return (
    <div className="page col">
      <div className="stats">
        <div className="stat">
          <div className="label">全市场标的</div>
          <div className="value">{stats.n}</div>
          <div className="sub">{cls === 'all' ? 'Binance U 本位永续' : `${classLabel(cls)}类`}</div>
        </div>
        <div className="stat">
          <div className="label">涨 / 跌</div>
          <div className="value"><span className="up">{stats.up}</span> / <span className="down">{stats.down}</span></div>
          <div className="sub">{stats.n ? `${((stats.up / stats.n) * 100).toFixed(0)}% 上涨` : '—'}</div>
        </div>
        <div className="stat">
          <div className="label">24h 总成交额</div>
          <div className="value">${fmtCompact(stats.vol)}</div>
          <div className="sub">全市场合计</div>
        </div>
        <div className="stat">
          <div className="label">领涨</div>
          <div className="value up" style={{ fontSize: 16 }}>
            {stats.top ? `${stats.top.base} ${fmtPct(stats.top.chgPct)}` : '—'}
          </div>
        </div>
        <div className="stat">
          <div className="label">领跌</div>
          <div className="value down" style={{ fontSize: 16 }}>
            {stats.bottom ? `${stats.bottom.base} ${fmtPct(stats.bottom.chgPct)}` : '—'}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          全市场行情
          <span className="muted" style={{ fontWeight: 400 }}>
            {merged.length} 个标的{search ? '（已过滤）' : ''}
          </span>
          <div className="seg">
            <button className={cls === 'all' ? 'on' : ''} onClick={() => setCls('all')}>
              全部 {rows.length}
            </button>
            {classes.map(([v, n]) => (
              <button key={v} className={cls === v ? 'on' : ''} onClick={() => setCls(v)}>
                {classLabel(v)} {n}
              </button>
            ))}
          </div>
          <div className="spacer" />
          <input
            placeholder="搜索 BTC / ETH …"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ width: 180 }}
          />
        </div>
        {err && <div className="msg err" style={{ margin: 12 }}>{err}</div>}
        {loading ? (
          <div className="empty">加载中…</div>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 36 }} />
                  {COLUMNS.map((c) => (
                    <th
                      key={c.key}
                      className={`sortable${c.right ? ' right' : ''}`}
                      onClick={() => clickSort(c.key)}
                    >
                      {c.label}{sort === c.key ? (desc ? ' ↓' : ' ↑') : ''}
                    </th>
                  ))}
                  <th className="right">状态</th>
                </tr>
              </thead>
              <tbody>
                {merged.slice(0, 300).map((r) => (
                  <tr key={r.symbol} className="clickable" onClick={() => nav(`/chart/${r.symbol}`)}>
                    <td>
                      <button
                        className="ghost sm"
                        title={watch.has(r.symbol) ? '从自选移除' : '加入自选'}
                        onClick={(e) => toggleWatch(r.symbol, e)}
                        style={{ color: watch.has(r.symbol) ? 'var(--warn)' : 'var(--text-dim)' }}
                      >
                        {watch.has(r.symbol) ? '★' : '☆'}
                      </button>
                    </td>
                    <td className="sym">
                      <div className="sym-cell">
                        <SymbolIcon symbol={r.symbol} />
                        <span className="sym-base">{r.base}</span>
                        <span className="sym-quote">/USDT</span>
                        {r.assetClass !== 'crypto' && (
                          <span className="tag">{classLabel(r.assetClass)}</span>
                        )}
                      </div>
                    </td>
                    <td className="right mono">{fmtPrice(r.last)}</td>
                    <td className={`right mono ${trendClass(r.chgPct)}`}>{fmtPct(r.chgPct)}</td>
                    <td className="right mono">${fmtCompact(r.quoteVolume)}</td>
                    <td className={`right mono ${trendClass(r.fundingRate)}`}>
                      {(r.fundingRate * 100).toFixed(4)}%
                    </td>
                    <td className="right">
                      {r.live ? <span className="tag live">实时</span> : <span className="tag">30s</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {merged.length > 300 && (
              <div className="empty">仅显示前 300 条，用搜索框缩小范围</div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
