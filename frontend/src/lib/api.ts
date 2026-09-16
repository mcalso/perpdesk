import { activeAccount, type AccountRow } from './account'
import { authState } from './auth'

export type { AccountRow }

export type AssetClass = 'crypto' | 'us_equity' | 'hk_equity' | 'index' | string

export interface Ticker {
  symbol: string; base: string; assetClass: AssetClass
  last: number; open: number; high: number; low: number
  chgPct: number; quoteVolume: number; volume: number; trades: number
  fundingRate: number; markPrice: number; nextFundingTime: number
  bid: number | null; ask: number | null; live: boolean
  ts: number
}

export interface HubStatus {
  symbols: number; snapshot: number; premium: number
  premiumAgeSec: number | null
  restAgeSec: number | null; restError: string
  wsConnected: boolean; wsSymbols: number
  bookAgeSec: number | null; subscribers: number
}

export interface Trade {
  id: number; symbol: string; side: 'BUY' | 'SELL'
  qty: number; price: number; fee: number
  traded_at: number; note: string; created_at: number
}

export interface Position {
  symbol: string; qty: number; avgCost: number; markPrice: number
  value: number; costBasis: number
  unrealized: number; unrealizedPct: number; realized: number; fee: number
  funding: number
  tradeCount: number; side: 'LONG' | 'SHORT' | 'FLAT'; lastAt: number
}

export interface AccountStatus {
  configured: boolean; hasKey: boolean; hasSecret: boolean; hint: string
}

export interface ExchangeBalance {
  asset: string; balance: number; available: number; unrealized: number
}

export interface ExchangePosition {
  symbol: string; qty: number; entryPrice: number; markPrice: number
  unrealized: number; leverage: number; liquidationPrice: number
  notional: number; side: 'LONG' | 'SHORT'; marginType: string
  weight: number
  exchangeUnrealized: number
  live: boolean
}

export interface AccountOverview {
  balances: ExchangeBalance[]
  positions: ExchangePosition[]
  equity: number
  wallet: number
  totalUnrealized: number
  grossNotional: number
  ageSec: number | null
  error: string
  pollInterval: number
}

export interface Flash {
  id: number
  source: string
  ts: number
  title: string
  content: string
  link: string
  important: boolean
  tags: string[]
  symbols: string[]
}

export interface NewsStatus {
  sources: string[]
  pollInterval: number
  total: number
  latest: number
  ageSec: number | null
  error: string
  symbolsKnown: number
}

export interface PortfolioSummary {
  positions: Position[]
  summary: {
    totalRealized: number; totalUnrealized: number; totalPnl: number
    totalFee: number; grossExposure: number; netExposure: number
    openCount: number; symbolCount: number
    totalFunding: number; exchangeRealized: number; exchangeCommission: number
    hasIncome: boolean
    days: number | null; rangeFrom: number | null
  }
  allocation: { symbol: string; value: number; weight: number; side: string }[]
}

/**
 * 给账户相关的请求附上当前账户号。
 *
 * 统一在这里加，而不是让每个调用点自己传：漏掉一处的表现是"显示了另一个
 * 账户的数据"，页面不会报错，数字看着也正常——正是最难发现的那类问题。
 */
function acct(q: URLSearchParams): URLSearchParams {
  const id = activeAccount.get()
  if (id !== null) q.set('account_id', String(id))
  return q
}


async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  })
  if (!res.ok) {
    // 会话过期时把全局状态置为未登录，正在看的页面会自动退回登录页，
    // 而不是满屏"请求失败"
    if (res.status === 401) authState.set('out')
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch { /* 响应不是 JSON，用状态码兜底 */ }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export const api = {
  me: () => req<{ authenticated: boolean; hasPassword: boolean }>('/api/auth/me'),
  login: (password: string) =>
    req<{ ok: boolean }>('/api/auth/login', {
      method: 'POST', body: JSON.stringify({ password }),
    }),
  logout: () => req<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  changePassword: (current: string, password: string) =>
    req<{ ok: boolean; note: string }>('/api/auth/password', {
      method: 'POST', body: JSON.stringify({ current, password }),
    }),
  sessions: () => req<{
    rows: { created_at: number; expires_at: number; last_seen: number; label: string }[]
    now: number
  }>('/api/auth/sessions'),
  revokeAllSessions: () =>
    req<{ ok: boolean }>('/api/auth/sessions/revoke-all', { method: 'POST' }),

  health: () => req<{ ok: boolean; hub: HubStatus }>('/api/health'),

  tickers: (p: { sort?: string; desc?: boolean; limit?: number; search?: string } = {}) => {
    const q = new URLSearchParams()
    if (p.sort) q.set('sort', p.sort)
    if (p.desc !== undefined) q.set('desc', String(p.desc))
    if (p.limit) q.set('limit', String(p.limit))
    if (p.search) q.set('search', p.search)
    return req<{ rows: Ticker[]; total: number; status: HubStatus }>(`/api/market/tickers?${q}`)
  },

  symbols: () => req<{ symbol: string; base: string }[]>('/api/market/symbols'),

  klines: (symbol: string, interval: string, limit = 500) =>
    req<{ t: number; open: number; high: number; low: number; close: number; volume: number }[]>(
      `/api/market/klines?symbol=${encodeURIComponent(symbol)}&interval=${interval}&limit=${limit}`),

  chartSource: (symbol: string) =>
    req<{ symbol: string; tradingview: boolean; tvSymbol: string | null; reason: string }>(
      `/api/market/chart-source/${encodeURIComponent(symbol)}`),

  watchlist: () => req<{ rows: Ticker[] }>('/api/watchlist'),
  addWatch: (symbol: string) =>
    req<{ ok: boolean; symbols: string[] }>('/api/watchlist', {
      method: 'POST', body: JSON.stringify({ symbol }),
    }),
  removeWatch: (symbol: string) =>
    req<{ ok: boolean; symbols: string[] }>(`/api/watchlist/${symbol}`, { method: 'DELETE' }),

  accounts: () => req<{ rows: AccountRow[]; defaultId: number }>('/api/account/accounts'),

  news: (opts: { limit?: number; before?: number; important?: boolean; mine?: boolean } = {}) => {
    const q = new URLSearchParams()
    q.set('limit', String(opts.limit ?? 50))
    if (opts.before) q.set('before', String(opts.before))
    if (opts.important) q.set('important', 'true')
    if (opts.mine) q.set('mine', 'true')
    return req<{ rows: Flash[]; filteredBy: string[]; status: NewsStatus }>(
      `/api/news?${acct(q)}`)
  },
  refreshNews: () =>
    req<{ inserted: number; status: NewsStatus }>('/api/news/refresh', { method: 'POST' }),

  trades: (opts: { symbol?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    if (opts.symbol) q.set('symbol', opts.symbol)
    q.set('limit', String(opts.limit ?? 200))
    q.set('offset', String(opts.offset ?? 0))
    return req<{ rows: Trade[]; total: number; limit: number; offset: number }>(
      `/api/portfolio/trades?${acct(q)}`)
  },
  addTrade: (t: Omit<Trade, 'id' | 'created_at' | 'traded_at'> & { traded_at?: number | null }) =>
    req<{ ok: boolean; id: number }>(`/api/portfolio/trades?${acct(new URLSearchParams())}`, {
      method: 'POST', body: JSON.stringify(t),
    }),
  deleteTrade: (id: number) =>
    req<{ ok: boolean }>(`/api/portfolio/trades/${id}?${acct(new URLSearchParams())}`,
                         { method: 'DELETE' }),
  importCsv: (csv_text: string) =>
    req<{ inserted: number; failed: number; errors: { line: number; error: string }[] }>(
      `/api/portfolio/import?${acct(new URLSearchParams())}`,
      { method: 'POST', body: JSON.stringify({ csv_text }) }),

  summary: (days?: number | null) => {
    const q = new URLSearchParams()
    if (days) q.set('days', String(days))
    return req<PortfolioSummary>(`/api/portfolio/summary?${acct(q)}`)
  },

  accountStatus: () =>
    req<AccountStatus>(`/api/account/status?${acct(new URLSearchParams())}`),
  accountOverview: () =>
    req<AccountOverview>(`/api/account/overview?${acct(new URLSearchParams())}`),
  syncTrades: (days = 30) => {
    const q = new URLSearchParams({ days: String(days) })
    return req<{ inserted: number; skipped: number; symbols: string[] }>(
      `/api/account/sync-trades?${acct(q)}`, { method: 'POST' })
  },
  daily: (days?: number | null) => {
    const q = new URLSearchParams()
    if (days) q.set('days', String(days))
    // getTimezoneOffset 返回的是"落后 UTC 多少分钟"，东八区是 -480，
    // 后端要的是相对 UTC 的偏移，取反
    q.set('tz_offset_min', String(-new Date().getTimezoneOffset()))
    return req<{
      rows: { d: number; realized: number; fee: number; funding: number; trades: number }[]
      winDays: number; lossDays: number
    }>(`/api/portfolio/daily?${acct(q)}`)
  },

  curve: (days?: number | null) => {
    const q = new URLSearchParams()
    if (days) q.set('days', String(days))
    return req<{ points: { t: number; realized: number }[]; from: number | null; to: number | null }>(
      `/api/portfolio/curve?${acct(q)}`)
  },
}
