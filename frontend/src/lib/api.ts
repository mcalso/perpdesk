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

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  })
  if (!res.ok) {
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

  trades: (opts: { symbol?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    if (opts.symbol) q.set('symbol', opts.symbol)
    q.set('limit', String(opts.limit ?? 200))
    q.set('offset', String(opts.offset ?? 0))
    return req<{ rows: Trade[]; total: number; limit: number; offset: number }>(
      `/api/portfolio/trades?${q}`)
  },
  addTrade: (t: Omit<Trade, 'id' | 'created_at' | 'traded_at'> & { traded_at?: number | null }) =>
    req<{ ok: boolean; id: number }>('/api/portfolio/trades', {
      method: 'POST', body: JSON.stringify(t),
    }),
  deleteTrade: (id: number) =>
    req<{ ok: boolean }>(`/api/portfolio/trades/${id}`, { method: 'DELETE' }),
  importCsv: (csv_text: string) =>
    req<{ inserted: number; failed: number; errors: { line: number; error: string }[] }>(
      '/api/portfolio/import', { method: 'POST', body: JSON.stringify({ csv_text }) }),

  summary: (days?: number | null) =>
    req<PortfolioSummary>(`/api/portfolio/summary${days ? `?days=${days}` : ''}`),

  accountStatus: () => req<AccountStatus>('/api/account/status'),
  accountOverview: () => req<AccountOverview>('/api/account/overview'),
  syncTrades: (days = 30) =>
    req<{ inserted: number; skipped: number; symbols: string[] }>(
      `/api/account/sync-trades?days=${days}`, { method: 'POST' }),
  curve: (days?: number | null) =>
    req<{ points: { t: number; realized: number }[]; from: number | null; to: number | null }>(
      `/api/portfolio/curve${days ? `?days=${days}` : ''}`),
}
