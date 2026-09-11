import { useMemo, useState } from 'react'

export type SortDir = 'asc' | 'desc'

/**
 * 表格排序。
 *
 * 数值按大小比，字符串按 localeCompare，null/undefined 一律沉底（无论升降序）——
 * 否则"没有强平价"的行会在升序时霸占榜首，看着像最危险的仓位。
 */
export function useSort<T extends object>(
  rows: T[],
  initialKey: string,
  initialDir: SortDir = 'desc',
) {
  const [sortKey, setSortKey] = useState(initialKey)
  const [sortDir, setSortDir] = useState<SortDir>(initialDir)

  const sorted = useMemo(() => {
    const mul = sortDir === 'desc' ? -1 : 1
    return [...rows].sort((a, b) => {
      const av = (a as Record<string, unknown>)[sortKey]
      const bv = (b as Record<string, unknown>)[sortKey]
      const aEmpty = av === null || av === undefined || av === ''
      const bEmpty = bv === null || bv === undefined || bv === ''
      if (aEmpty && bEmpty) return 0
      if (aEmpty) return 1
      if (bEmpty) return -1
      if (typeof av === 'string' || typeof bv === 'string') {
        return String(av).localeCompare(String(bv)) * mul
      }
      return (Number(av) - Number(bv)) * mul
    })
  }, [rows, sortKey, sortDir])

  /** 点同一列切换升降，点新列默认降序（金额类降序更常用） */
  const toggle = (key: string) => {
    if (key === sortKey) setSortDir(sortDir === 'desc' ? 'asc' : 'desc')
    else {
      setSortKey(key)
      setSortDir('desc')
    }
  }

  return { sorted, sortKey, sortDir, toggle }
}
