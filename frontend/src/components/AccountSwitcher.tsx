import { useEffect, useState } from 'react'
import { activeAccount, useActiveAccount, type AccountRow } from '../lib/account'
import { api } from '../lib/api'

/**
 * 顶栏的账户切换。
 *
 * 只有一个账户时整个控件不渲染 —— 绝大多数使用者只有一个账户，
 * 给他们一个永远只有一项的下拉框是纯噪音。
 */
export function AccountSwitcher() {
  const active = useActiveAccount()
  const [rows, setRows] = useState<AccountRow[]>([])

  useEffect(() => {
    let alive = true
    api.accounts()
      .then((r) => { if (alive) setRows(r.rows) })
      .catch(() => { /* 账户列表拉不到不影响行情，静默即可 */ })
    return () => { alive = false }
  }, [])

  if (rows.length <= 1) return null

  const current = rows.find((r) => r.id === active) ?? rows[0]
  return (
    <div className="acct-switch" title={`${current.exchange} · ${current.market}`}>
      <select
        value={active ?? rows[0].id}
        onChange={(e) => activeAccount.set(Number(e.target.value))}
      >
        {rows.map((r) => (
          <option key={r.id} value={r.id}>
            {r.label}{r.configured ? '' : '（未配凭据）'}
          </option>
        ))}
      </select>
    </div>
  )
}
