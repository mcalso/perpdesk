export const fmtNum = (v: number | null | undefined, digits = 2): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return v.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

/** 价格按量级自适应小数位：BTC 给 2 位，SHIB 那种要 8 位 */
export const fmtPrice = (v: number | null | undefined): string => {
  if (v === null || v === undefined || Number.isNaN(v) || v === 0) return '—'
  const a = Math.abs(v)
  const d = a >= 1000 ? 2 : a >= 1 ? 4 : a >= 0.01 ? 5 : 8
  return v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })
}

/**
 * 持仓量/成交量。
 *
 * 逐笔累加会攒出浮点误差（104 变成 103.99999999999999，
 * -409.13 变成 -409.13000000000005），显示前按有效位归整。
 * 加密标的的量级跨度极大（BTC 0.001 到 SHIB 上亿），所以按绝对值定小数位。
 */
export const fmtQty = (v: number | null | undefined): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const a = Math.abs(v)
  const d = a >= 1000 ? 2 : a >= 1 ? 4 : 8
  // toFixed 后去掉多余的 0，避免 121598.0000 这种
  const fixed = Number(v.toFixed(d))
  return fixed.toLocaleString('en-US', { maximumFractionDigits: d })
}

export const fmtUsd = (v: number | null | undefined, digits = 2): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const sign = v < 0 ? '-' : ''
  return `${sign}$${Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  })}`
}

export const fmtCompact = (v: number | null | undefined): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const a = Math.abs(v)
  if (a >= 1e9) return `${(v / 1e9).toFixed(2)}B`
  if (a >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  if (a >= 1e3) return `${(v / 1e3).toFixed(1)}K`
  return v.toFixed(2)
}

export const fmtPct = (v: number | null | undefined, digits = 2): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`
}

export const fmtTime = (ms: number): string =>
  new Date(ms).toLocaleString('zh-CN', { hour12: false })

export const fmtDate = (ms: number): string =>
  new Date(ms).toLocaleDateString('zh-CN')

/** 涨跌方向 → CSS class，统一红绿口径（绿涨红跌，国际惯例） */
export const trendClass = (v: number | null | undefined): string =>
  v === null || v === undefined || v === 0 ? 'flat' : v > 0 ? 'up' : 'down'
