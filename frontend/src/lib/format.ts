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

/**
 * 人民币估值。刻意不带小数：这是按场外中位价折出来的近似值，
 * 写到分位会假装出它没有的精度。
 */
export const fmtCny = (v: number | null | undefined): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const sign = v < 0 ? '-' : ''
  return `${sign}¥${Math.abs(v).toLocaleString('en-US', { maximumFractionDigits: 0 })}`
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

/** 上架多少天以内算「新」。14 天时全市场约 5 个，既有信号又不会满屏角标 */
export const NEW_LISTING_DAYS = 14

export const isNewListing = (onboardDate: number, now: number): boolean =>
  onboardDate > 0 && now - onboardDate <= NEW_LISTING_DAYS * 86_400_000

/**
 * 距下次资金费结算还剩多久。
 *
 * 只精确到分钟：结算间隔是 8 小时（少数标的 4h/1h），秒级精度没有意义，
 * 却会逼着整张表每秒重渲染一次。
 */
export const untilFunding = (at: number, now: number): string => {
  if (!at) return ''
  const ms = at - now
  if (ms <= 0) return '结算中'
  const m = Math.floor(ms / 60_000)
  if (m >= 60) return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`
  return m >= 1 ? `${m}m` : '<1m'
}

/**
 * 当前价在 24h 区间里的位置，0~100。
 *
 * high === low 时（新上架或完全无成交）返回 null，让调用方别画这根条 ——
 * 除零会得到 NaN，CSS 里 `left: NaN%` 会被整条规则丢掉，圆点默认落在最左，
 * 看上去像「贴着 24h 最低点」，是个会误导交易判断的假信号。
 */
export const rangePos = (low: number, high: number, last: number): number | null => {
  if (!(high > low) || !last) return null
  return Math.min(100, Math.max(0, ((last - low) / (high - low)) * 100))
}
