import { useSyncExternalStore } from 'react'

export interface AccountRow {
  id: number
  exchange: string
  market: string
  label: string
  enabled: number
  sort_order: number
  created_at: number
  configured: boolean
  credentials: Record<string, string>   // 只有掩码，后端不返回明文
  trades: number
}

const STORAGE_KEY = 'perpdesk.activeAccount'

/**
 * 当前查看的账户。null 表示「默认账户」，由后端决定是哪个。
 *
 * 做成全局单点而不是层层传参：账户号要附加到每一个账户相关的请求上，
 * 靠调用点自己记得传的话，漏一个就是把别的账户的持仓显示成你的。
 * 所以由 api 客户端统一读这里（见 lib/api.ts 的 acct()）。
 */
let activeId: number | null = readStored()
const listeners = new Set<() => void>()

function readStored(): number | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    const n = raw === null ? NaN : Number(raw)
    return Number.isFinite(n) ? n : null
  } catch {
    return null    // 隐私模式 / 禁用了存储，退回默认账户即可
  }
}

export const activeAccount = {
  get: (): number | null => activeId,
  set(id: number | null) {
    if (id === activeId) return
    activeId = id
    try {
      if (id === null) localStorage.removeItem(STORAGE_KEY)
      else localStorage.setItem(STORAGE_KEY, String(id))
    } catch { /* 存不下不影响本次会话 */ }
    listeners.forEach((fn) => fn())
  },
  subscribe(fn: () => void) {
    listeners.add(fn)
    return () => { listeners.delete(fn) }
  },
}

/** 组件里读当前账户。把返回值放进 effect 依赖，切账户就会自动重新拉数。 */
export function useActiveAccount(): number | null {
  return useSyncExternalStore(activeAccount.subscribe, activeAccount.get, () => null)
}
