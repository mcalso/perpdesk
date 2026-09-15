import { useSyncExternalStore } from 'react'

/**
 * 登录状态的全局单点。
 *
 * 除了启动时问一次 /api/auth/me，任何请求收到 401 也会把这里置为未登录 ——
 * 会话过期时用户正在看的页面会自动退回登录页，而不是满屏"请求失败"。
 */
type State = 'unknown' | 'in' | 'out'

let state: State = 'unknown'
const listeners = new Set<() => void>()

export const authState = {
  get: (): State => state,
  set(next: State) {
    if (next === state) return
    state = next
    listeners.forEach((fn) => fn())
  },
  subscribe(fn: () => void) {
    listeners.add(fn)
    return () => { listeners.delete(fn) }
  },
}

export function useAuthState(): State {
  return useSyncExternalStore(authState.subscribe, authState.get, () => 'unknown' as State)
}
