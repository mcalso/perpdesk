import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { SymbolIcon } from '../components/SymbolIcon'
import { TableSkeleton } from '../components/TableSkeleton'
import { useActiveAccount } from '../lib/account'
import { api, type Flash, type NewsStatus } from '../lib/api'

const CAT_LABEL: Record<string, string> = {
  macro: '宏观', equity: '股市', crypto: '加密', commodity: '商品', geo: '地缘',
}

const PAGE = 50

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
  const [loaded, setLoaded] = useState(false)
  const [more, setMore] = useState(true)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  // 用户翻到下面时不要把列表顶掉；只有在顶部才自动合并新条目
  const atTop = useRef(true)

  const load = useCallback(async (reset: boolean) => {
    try {
      const r = await api.news({
        limit: PAGE,
        before: reset ? undefined : rows[rows.length - 1]?.ts,
        important: filter === 'important',
        mine: filter === 'mine',
      })
      setStatus(r.status)
      setErr('')
      if (reset) {
        setRows(r.rows)
        setMore(r.rows.length >= PAGE)
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
    }
  }, [filter, rows])

  useEffect(() => {
    setLoaded(false); setRows([]); setMore(true)
    load(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter, acct])

  useEffect(() => {
    const t = setInterval(() => { if (atTop.current) load(true) }, 60000)
    const onScroll = () => { atTop.current = window.scrollY < 120 }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => { clearInterval(t); window.removeEventListener('scroll', onScroll) }
  }, [load])

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
          <span className="muted" style={{ fontWeight: 400 }}>
            {status ? `${status.sources.join('、')} · 共 ${status.total} 条` : ''}
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
          <div className="spacer" />
          <button className="sm" onClick={refresh} disabled={busy}>
            {busy ? '抓取中…' : '立即刷新'}
          </button>
        </div>

        {err && <div className="msg err" style={{ margin: 12 }}>{err}</div>}

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
          <div className="flash-list">
            {rows.map((f) => (
              <article key={f.id} className={`flash${f.important ? ' hot' : ''}`}>
                <time className="flash-time mono">{stamp(f.ts)}</time>
                <div className="flash-body">
                  {f.title && <div className="flash-title">{f.title}</div>}
                  <div className="flash-text">{f.content}</div>
                  <div className="flash-meta">
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
                    {f.link && (
                      <a className="flash-link muted" href={f.link}
                         target="_blank" rel="noopener noreferrer">原文 ↗</a>
                    )}
                  </div>
                </div>
              </article>
            ))}
          </div>
        )}

        {loaded && rows.length > 0 && (
          <div className="pager">
            {more ? (
              <button className="sm" onClick={() => load(false)}>加载更早的</button>
            ) : (
              <span className="muted">没有更多了</span>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
