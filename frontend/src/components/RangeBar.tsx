import { fmtPrice, rangePos } from '../lib/format'

/**
 * 24h 区间条：当前价落在 [最低, 最高] 的什么位置。
 *
 * 数据来自 ticker/24hr 的 high/low，本来就在取，画它不产生任何额外请求。
 * 左红右绿和涨跌同一套色：贴左边 = 在 24h 低位，贴右边 = 在高位。
 */
export function RangeBar({ low, high, last }: { low: number; high: number; last: number }) {
  const pos = rangePos(low, high, last)
  if (pos === null) return null
  return (
    <span className="range" title={`24h 区间 ${fmtPrice(low)} ~ ${fmtPrice(high)}`}>
      <span className="range-dot" style={{ left: `${pos}%` }} />
    </span>
  )
}
