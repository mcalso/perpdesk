import type React from 'react'

/**
 * 表格骨架屏。
 *
 * 首屏那几百毫秒里显示一行"加载中…"会让人以为卡住了；骨架屏把版式先立住，
 * 数据到位时是"填进去"而不是"整块蹦出来"。
 */
export function TableSkeleton({ rows = 8, cols = 6 }: { rows?: number; cols?: number }) {
  return (
    <div
      className="skeleton-table"
      role="status"
      aria-label="加载中"
      style={{ '--sk-cols': cols } as React.CSSProperties}
    >
      {Array.from({ length: rows }, (_, r) => (
        <div className="skeleton-row" key={r} aria-hidden>
          {Array.from({ length: cols }, (_, c) => (
            // 宽度按下标做确定性抖动：每格一样宽会显得很假，
            // 用 Math.random 则每次重渲染都在跳
            <div
              key={c}
              className="skeleton"
              style={{ width: `${46 + ((r * 7 + c * 13) % 5) * 11}%` }}
            />
          ))}
        </div>
      ))}
    </div>
  )
}
