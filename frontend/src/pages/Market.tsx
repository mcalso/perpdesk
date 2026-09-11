import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { SortHeader } from '../components/SortHeader'
import { SymbolIcon } from '../components/SymbolIcon'
import { api, type Ticker } from '../lib/api'
import { useSort } from '../lib/useSort'
import { liveFeed, type LiveRow } from '../lib/ws'
import { fmtCompact, fmtPct, fmtPrice, trendClass } from '../lib/format'

// Binance 现在既有加密永续，也有股票/指数代币化永续(TRADIFI_PERPETUAL)，分开看更清楚。
// 类别列表从行情数据里动态取，Binance 新增品类时不用改代码。
const CLASS_LABEL: Record<string, string> = {
  crypto: '加密', us_equity: '美股', hk_equity: '港股', cn_equity: 'A股',
  kr_equity: '韩股', index: '指数', commodity: '商品', premarket: '盘前',
}
const classLabel = (v: string) => CLASS_LABEL[v] || v

const PAGE_SIZE = 100

export default function Market() {
  const nav = useNavigate()
  const [rows, setRows] = useState<Ticker[]>([])
  const [live, setLive] = useState<Map<string, LiveRow>>(new Map())
  const [watch, setWatch] = useState<Set<string>>(new Set())
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
  const filtered = useMemo(() => {
    const out = rows.map((r) => {
      const l = live.get(r.symbol)
      return l ? { ...r, last: l.last, chgPct: l.chgPct, markPrice: l.markPrice } : r
    })
    const needle = search.trim().toUpperCase()
    let hit = needle ? out.filter((r) => r.symbol.includes(needle)) : out
    if (cls !== 'all') hit = hit.filter((r) => r.assetClass === cls)
    return hit
  }, [rows, live, search, cls])

  const { sorted, sortKey, sortDir, toggle } = useSort(filtered, 'quoteVolume')

  // 分页而不是截断：718 个标的全都要能翻到，同时把每秒重渲染的 DOM 控制在一页内
  const [page, setPage] = useState(0)
  const pageCount = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE))
  useEffect(() => { setPage(0) }, [search, cls, sortKey, sortDir])
  const pageRows = useMemo(
    () => sorted.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE),
    [sorted, page],
  )

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
            {sorted.length} 个标的{search || cls !== 'all' ? '（已过滤）' : ''}
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
                  <SortHeader label="标的" sortKey="symbol" current={sortKey} dir={sortDir} onSort={toggle} />
                  <SortHeader label="最新价" sortKey="last" current={sortKey} dir={sortDir} onSort={toggle} right />
                  <SortHeader label="24h 涨跌" sortKey="chgPct" current={sortKey} dir={sortDir} onSort={toggle} right />
                  <SortHeader label="24h 成交额" sortKey="quoteVolume" current={sortKey} dir={sortDir} onSort={toggle} right />
                  <SortHeader label="资金费率" sortKey="fundingRate" current={sortKey} dir={sortDir} onSort={toggle} right />
                  <th className="right">状态</th>
                </tr>
              </thead>
              <tbody>
                {pageRows.map((r) => (
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

          </div>
        )}
        {pageCount > 1 && (
          <div className="pager">
            <button className="sm" disabled={page === 0} onClick={() => setPage(0)}>« 首页</button>
            <button className="sm" disabled={page === 0} onClick={() => setPage(page - 1)}>上一页</button>
            <span className="muted">
              第 {page + 1} / {pageCount} 页 · 第 {page * PAGE_SIZE + 1}–
              {Math.min((page + 1) * PAGE_SIZE, sorted.length)} 条
            </span>
            <button className="sm" disabled={page >= pageCount - 1}
                    onClick={() => setPage(page + 1)}>下一页</button>
            <button className="sm" disabled={page >= pageCount - 1}
                    onClick={() => setPage(pageCount - 1)}>末页 »</button>
          </div>
        )}
      </div>
    </div>
  )
}
