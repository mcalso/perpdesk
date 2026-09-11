import type { SortDir } from '../lib/useSort'

interface Props {
  label: string
  sortKey: string
  current: string
  dir: SortDir
  onSort: (key: string) => void
  right?: boolean
  title?: string
}

export function SortHeader({ label, sortKey, current, dir, onSort, right, title }: Props) {
  const active = current === sortKey
  return (
    <th
      className={`sortable${right ? ' right' : ''}${active ? ' sorted' : ''}`}
      onClick={() => onSort(sortKey)}
      title={title}
    >
      {label}
      <span className="sort-arrow">{active ? (dir === 'desc' ? '↓' : '↑') : ''}</span>
    </th>
  )
}
