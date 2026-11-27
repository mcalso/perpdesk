import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { SymbolIcon } from '../components/SymbolIcon'
import { TableSkeleton } from '../components/TableSkeleton'
import { useActiveAccount } from '../lib/account'
import { api, type Flash, type NewsStatus } from '../lib/api'

const CAT_LABEL: Record<string, string> = {
  macro: '宏观', equity: '股市', crypto: '加密', commodity: '商品', geo: '地缘',
}

// 外部资讯库（news-collector）接入后会有多个来源，列表里标一下出处
const SRC_LABEL: Record<string, string> = {
  jin10: '金十', binance: '币安公告', okx: 'OKX 公告', bybit: 'Bybit 公告', bitget: 'Bitget 公告',
}

// news-collector 的事件主题（与 collector/tagger.py 的 THEMES 对应）
const THEME_LABEL: Record<string, string> = {
  macro_data: '经济数据', central_bank: '央行', fiscal: '财政', trade: '贸易关税',
  geopolitics: '地缘', politics: '政治', regulation: '监管',
  earnings: '财报', guidance: '业绩指引', analyst: '机构观点', mna: '并购',
  capital: '融资回购', product: '产品订单', management: '高管', legal: '诉讼调查',
  operations: '生产经营', commodity: '商品供需', market_move: '行情异动', flows: '资金流向',
  fx_rates: '汇率利率', crypto: '加密行业', exchange_notice: '交易所公告', other: '其他',
}
const SENT_MARK: Record<string, { text: string; cls: string }> = {
  bullish: { text: '利多', cls: 'up' }, bearish: { text: '利空', cls: 'down' },
  mixed: { text: '多空交织', cls: 'muted' },
}

const PAGE = 50
// 正文超过这个字数就先折叠成 3 行，点一下展开。
// 金十的"数据整理"类汇总动辄上千字，不折叠的话一条就占满一屏
const LONG_TEXT = 140

/** 同一天的只显示时分，跨天才标日期 —— 快讯列表里满屏完整日期是噪音 */
function stamp(ts: number): string {
  const d = new Date(ts)
  const hm = d.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' })
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  return sameDay ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`
}

export default function News() {
  const nav = useNavigate()
  const acct = useActiveAccount()
  const [rows, setRows] = useState<Flash[]>([])
  const [status, setStatus] = useState<NewsStatus | null>(null)
  const [filter, setFilter] = useState<'all' | 'important' | 'mine'>('all')
  const [theme, setTheme] = useState('')
  // 展开过的条目 id。按 id 记而不是按位置：60 秒自动刷新会在顶部插入新条目
  const [opened, setOpened] = useState<Set<number>>(() => new Set())
  const toggle = (id: number) => {
    // 拖选文字复制时不要顺手把它折叠回去
    if (window.getSelection()?.toString()) return
    setOpened((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  const [loaded, setLoaded] = useState(false)
  const [more, setMore] = useState(true)
  const [busy, setBusy] = useState(false)
  const [fetchingMore, setFetchingMore] = useState(false)
  const [err, setErr] = useState('')
  // 列表在面板内部滚动（.flash-list 有 max-height），"是否在顶部"要看它的 scrollTop，
  // 不是 window.scrollY —— 否则翻到下面时自动刷新照样会触发
  const listRef = useRef<HTMLDivElement>(null)
  const sentinelRef = useRef<HTMLDivElement>(null)
  const atTop = useRef(true)
  // 同一时间只发一个"更早的"请求：滚动时 IntersectionObserver 会连续触发
  const inflight = useRef(false)

  const load = useCallback(async (reset: boolean) => {
    if (!reset) {
      if (inflight.current) return
      inflight.current = true
      setFetchingMore(true)
    }
    try {
      const r = await api.news({
        limit: PAGE,
        before: reset ? undefined : rows[rows.length - 1]?.ts,
        important: filter === 'important',
        mine: filter === 'mine',
        theme: theme || undefined,
      })
      setStatus(r.status)
      setErr('')
      if (reset) {
        setRows((prev) => {
          // 已经往下翻过好几页时，刷新只把新条目插到顶部，不丢掉已加载的更早条目
          if (prev.length <= PAGE) return r.rows
          const seen = new Set(prev.map((x) => x.id))
          return [...r.rows.filter((x) => !seen.has(x.id)), ...prev]
        })
        if (rows.length <= PAGE) setMore(r.rows.length >= PAGE)
      } else {
        // 按 id 去重：游标用的是时间，同一毫秒的条目可能跨页重复出现
        setRows((prev) => {
          const seen = new Set(prev.map((x) => x.id))
          return [...prev, ...r.rows.filter((x) => !seen.has(x.id))]
        })
        setMore(r.rows.length >= PAGE)
      }
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setLoaded(true)
      if (!reset) {
        inflight.current = false
        setFetchingMore(false)
      }
    }
  }, [filter, theme, rows])

  useEffect(() => {
    setLoaded(false); setRows([]); setMore(true)
    listRef.current?.scrollTo({ top: 0 })
    load(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter, theme, acct])

  useEffect(() => {
    const t = setInterval(() => { if (atTop.current) load(true) }, 60000)
    const el = listRef.current
    const onScroll = () => { atTop.current = (el?.scrollTop ?? 0) < 120 && window.scrollY < 120 }
    el?.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      clearInterval(t)
      el?.removeEventListener('scroll', onScroll)
      window.removeEventListener('scroll', onScroll)
    }
  }, [load, loaded])

  // 懒加载：列表底部的哨兵进入视野前 600px 就开始取下一页，滚到底时通常已经到了
  const hasRows = rows.length > 0
  const loadMore = useRef(() => {})
  loadMore.current = () => { if (more && loaded) load(false) }
  useEffect(() => {
    const el = sentinelRef.current
    if (!el || typeof IntersectionObserver === 'undefined') return
    const io = new IntersectionObserver(
      (entries) => { if (entries.some((e) => e.isIntersecting)) loadMore.current() },
      { root: listRef.current, rootMargin: '0px 0px 600px 0px' })
    io.observe(el)
    return () => io.disconnect()
  }, [loaded, hasRows])

  // 只有接了 news-collector（有主题标签）时才显示主题筛选
  const hasThemes = rows.some((f) => (f.themes ?? []).length > 0) || theme !== ''

  const refresh = async () => {
    setBusy(true)
    try { await api.refreshNews(); await load(true) }
    catch (e) { setErr((e as Error).message) }
    finally { setBusy(false) }
  }

  return (
    <div className="page col">
      <div className="panel">
        <div className="panel-head">
          资讯快讯
          <span className="panel-sub">
            {status ? `${status.sources.map((x) => SRC_LABEL[x] ?? x).join('、')} · 共 ${status.total} 条` : ''}
            {status?.ageSec != null && ` · ${status.ageSec.toFixed(0)}s 前更新`}
            {status?.error && ` · ⚠ ${status.error}`}
          </span>
          <div className="seg">
            <button className={filter === 'all' ? 'on' : ''} onClick={() => setFilter('all')}>全部</button>
            <button className={filter === 'important' ? 'on' : ''}
                    onClick={() => setFilter('important')}>重要</button>
            <button className={filter === 'mine' ? 'on' : ''}
                    onClick={() => setFilter('mine')}>与我相关</button>
          </div>
          {hasThemes && (
            <select className="sm" value={theme} onChange={(e) => setTheme(e.target.value)}
                    title="按事件主题筛选">
              <option value="">全部主题</option>
              {Object.entries(THEME_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          )}
          <div className="spacer" />
          <button className="sm" onClick={refresh} disabled={busy}>
            {busy ? '抓取中…' : '立即刷新'}
          </button>
        </div>

        {err && <div className="msg err">{err}</div>}

        {!loaded ? (
          <TableSkeleton rows={10} cols={3} />
        ) : rows.length === 0 ? (
          <div className="empty">
            {filter === 'mine'
              ? '暂时没有与你持仓/自选相关的快讯'
              : filter === 'important'
                ? '暂时没有标记为重要的快讯'
                : '还没有抓到快讯，点「立即刷新」试试'}
          </div>
        ) : (
          <div className="flash-list" ref={listRef}>
            {rows.map((f) => (
              <article key={f.id}
                       className={`flash${f.important || f.importance === 3 ? ' hot' : ''}`}>
                <time className="flash-time mono">{stamp(f.ts)}</time>
                <div className="flash-body">
                  {f.title && <div className="flash-title">{f.title}</div>}
                  {f.content.length > LONG_TEXT ? (
                    <>
                      <div className={`flash-text${opened.has(f.id) ? '' : ' clamp'}`}
                           onClick={() => toggle(f.id)}
                           title={opened.has(f.id) ? '' : '点击展开全文'}>
                        {f.content}
                      </div>
                      <button className="flash-more" onClick={() => toggle(f.id)}>
                        {opened.has(f.id) ? '收起 ▴' : `展开全文（${f.content.length} 字）▾`}
                      </button>
                    </>
                  ) : (
                    <div className="flash-text">{f.content}</div>
                  )}
                  <div className="flash-meta">
                    {status && status.sources.length > 1 && (
                      <span className="tag">{SRC_LABEL[f.source] ?? f.source}</span>
                    )}
                    {f.symbols.map((s) => (
                      <button key={s} className="flash-sym" title={`查看 ${s} 图表`}
                              onClick={() => nav(`/chart/${s}`, { viewTransition: true })}>
                        <SymbolIcon symbol={s} size={14} />
                        {s.replace(/USDT$|USDC$/, '')}
                      </button>
                    ))}
                    {f.tags.filter((t) => CAT_LABEL[t]).map((t) => (
                      <span key={t} className="tag">{CAT_LABEL[t]}</span>
                    ))}
                    {(f.themes ?? []).map((t) => (
                      <button key={t} className="tag" title="只看这个主题"
                              onClick={() => setTheme(t)}>{THEME_LABEL[t] ?? t}</button>
                    ))}
                    {f.sentiment && SENT_MARK[f.sentiment] && (
                      <span className={`tag ${SENT_MARK[f.sentiment].cls}`}>{SENT_MARK[f.sentiment].text}</span>
                    )}
                    {f.importance === 3 && <span className="tag down" title="大模型判为重大">重大</span>}
                    {f.link && (
                      <a className="flash-link muted" href={f.link}
                         target="_blank" rel="noopener noreferrer">原文 ↗</a>
                    )}
                  </div>
                </div>
              </article>
            ))}
            {/* 懒加载哨兵：滚到附近自动取下一页；按钮保留给不支持 IntersectionObserver 的环境 */}
            <div className="pager" ref={sentinelRef}>
              {fetchingMore ? (
                <span className="muted">加载中…</span>
              ) : more ? (
                <button className="sm" onClick={() => load(false)}>加载更早的</button>
              ) : (
                <span className="muted">没有更多了</span>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
