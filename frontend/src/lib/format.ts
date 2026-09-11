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
