import { useState, type ReactNode } from 'react'

export interface ChartSpec {
  key: string
  title: string
  /** 大图下标题旁的说明，缩略图不显示 */
  sub?: string
  /** 右上角的关键数字，缩略图上也显示 */
  badge?: { text: string; cls?: string }
  render: (mode: 'large' | 'thumb') => ReactNode
}

/**
 * 一大 N 小的图表组：点缩略图把它换到大图位置，原来的大图退回缩略图轨道。
 *
 * 轨道里按原始顺序排列并**略过当前选中的那张** —— 保留原序而不是把选中项
 * 移到末尾，点来点去时其余图的位置才不会跳。
 */
export function ChartDeck({ charts, height = 300 }: { charts: ChartSpec[]; height?: number }) {
  const [active, setActive] = useState(charts[0]?.key)
  const current = charts.find((c) => c.key === active) ?? charts[0]
  if (!current) return null

  return (
    <div className="deck">
      <div className="panel deck-main">
        <div className="panel-head">
          {current.title}
          {current.sub && (
            <span className="muted" style={{ fontWeight: 400 }}>{current.sub}</span>
          )}
          <div className="spacer" />
          {current.badge && (
            <span className={`deck-badge ${current.badge.cls || ''}`}>{current.badge.text}</span>
          )}
        </div>
        <div className="panel-body" style={{ height }}>
          {current.render('large')}
        </div>
      </div>

      <div className="deck-rail">
        {charts.filter((c) => c.key !== current.key).map((c) => (
          <button key={c.key} className="deck-thumb" onClick={() => setActive(c.key)}
                  title={`切换到「${c.title}」`}>
            <div className="deck-thumb-head">
              <span className="deck-thumb-title">{c.title}</span>
              {c.badge && (
                <span className={`deck-badge sm ${c.badge.cls || ''}`}>{c.badge.text}</span>
              )}
            </div>
            <div className="deck-thumb-body">{c.render('thumb')}</div>
          </button>
        ))}
      </div>
    </div>
  )
}
