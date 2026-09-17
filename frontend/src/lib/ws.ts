/** 紧凑帧的字段顺序，与后端 routers/market.py 的 FRAME_FIELDS 一致 */
export interface LiveRow {
  symbol: string; last: number; chgPct: number; markPrice: number
}

type Handler = (rows: Map<string, LiveRow>) => void

const decode = (r: unknown[]): LiveRow => ({
  symbol: r[0] as string,
  last: r[1] as number,
  chgPct: r[2] as number,
  markPrice: r[3] as number,
})

/**
 * 行情推送连接，自动重连。
 * 全局单例：多个页面共用一条 WS，避免每个组件各连一次后端。
 *
 * 推送是**按需**的：组件用 want() 声明自己需要哪些标的，这里取并集上报给
 * 后端，后端只推这些、且只推值变了的行。后端默认一行都不推，所以组件不
 * 声明就什么都收不到 —— 这是有意的，见 hub.Subscriber 的说明。
 */
class LiveFeed {
  private ws: WebSocket | null = null
  private handlers = new Set<Handler>()
  private rows = new Map<string, LiveRow>()
  private wants = new Map<string, string[]>()
  private sent = ''
  private backoff = 1000
  private timer: number | null = null
  private closed = false

  subscribe(h: Handler): () => void {
    this.handlers.add(h)
    if (this.rows.size) h(this.rows)
    this.ensure()
    return () => {
      this.handlers.delete(h)
      if (!this.handlers.size) this.disconnect()
    }
  }

  /** 声明 key 这个组件需要哪些标的。同内容重复调用不会产生网络包。 */
  want(key: string, symbols: string[]): void {
    this.wants.set(key, symbols)
    this.pushViewport()
  }

  /** 组件卸载时撤回它的声明。 */
  drop(key: string): void {
    if (this.wants.delete(key)) this.pushViewport()
  }

  private emit() {
    this.handlers.forEach((h) => h(this.rows))
  }

  private pushViewport() {
    const all = new Set<string>()
    for (const list of this.wants.values()) for (const s of list) all.add(s)
    const symbols = [...all].sort()
    const key = symbols.join(',')
    if (key === this.sent) return
    this.sent = key

    // 离开视口的标的必须从本地缓存里删掉。后端不再推它，留着就是一份
    // 永不更新的旧值 —— 而页面渲染时 live 优先于 REST 快照，那会让
    // 翻过页的行显示得比 30s 一刷的 REST 数据还要旧。
    let pruned = false
    for (const sym of [...this.rows.keys()]) {
      if (!all.has(sym)) { this.rows.delete(sym); pruned = true }
    }
    if (pruned) this.emit()

    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'viewport', symbols }))
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
          // rows 是增量，只含变化的标的，并进来即可，不要清空重建
          for (const r of msg.rows as unknown[][]) {
            const row = decode(r)
            this.rows.set(row.symbol, row)
          }
          this.emit()
        }
      } catch { /* 坏帧直接丢弃，下一帧 1 秒后就到 */ }
    }
    ws.onopen = () => {
      this.backoff = 1000
      // 重连后服务端是一个全新的订阅者，视口得重新报一次，否则它不会推任何东西
      this.sent = ''
      this.pushViewport()
    }
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
    this.sent = ''
  }
}

export const liveFeed = new LiveFeed()
