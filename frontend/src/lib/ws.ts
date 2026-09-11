import type { HubStatus } from './api'

/** 紧凑帧的字段顺序，与后端 hub.compact_rows() 一致 */
export interface LiveRow {
  symbol: string; last: number; chgPct: number; quoteVolume: number
  high: number; low: number; fundingRate: number; markPrice: number
}

type Handler = (rows: Map<string, LiveRow>, status: HubStatus | null) => void

const decode = (r: unknown[]): LiveRow => ({
  symbol: r[0] as string,
  last: r[1] as number,
  chgPct: r[2] as number,
  quoteVolume: r[3] as number,
  high: r[4] as number,
  low: r[5] as number,
  fundingRate: r[6] as number,
  markPrice: r[7] as number,
})

/**
 * 行情推送连接，自动重连。
 * 全局单例：多个页面共用一条 WS，避免每个组件各连一次后端。
 */
class LiveFeed {
  private ws: WebSocket | null = null
  private handlers = new Set<Handler>()
  private rows = new Map<string, LiveRow>()
  private status: HubStatus | null = null
  private backoff = 1000
  private timer: number | null = null
  private closed = false

  subscribe(h: Handler): () => void {
    this.handlers.add(h)
    if (this.rows.size) h(this.rows, this.status)
    this.ensure()
    return () => {
      this.handlers.delete(h)
      if (!this.handlers.size) this.disconnect()
    }
  }

  private ensure() {
    if (this.ws || !this.handlers.size) return
    this.closed = false
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/api/market/ws`)
    this.ws = ws

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data)
        if (msg.type === 'tickers') {
          for (const r of msg.rows as unknown[][]) {
            const row = decode(r)
            this.rows.set(row.symbol, row)
          }
          if (msg.status) this.status = msg.status
          this.handlers.forEach((h) => h(this.rows, this.status))
        }
      } catch { /* 坏帧直接丢弃，下一帧 1 秒后就到 */ }
    }
    ws.onopen = () => { this.backoff = 1000 }
    ws.onclose = () => {
      this.ws = null
      if (this.closed || !this.handlers.size) return
      this.timer = window.setTimeout(() => this.ensure(), this.backoff)
      this.backoff = Math.min(this.backoff * 2, 30000)
    }
    ws.onerror = () => ws.close()
  }

  private disconnect() {
    this.closed = true
    if (this.timer) { clearTimeout(this.timer); this.timer = null }
    this.ws?.close()
    this.ws = null
  }
}

export const liveFeed = new LiveFeed()
