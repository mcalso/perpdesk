import { useEffect, useRef, type ReactNode } from 'react'

interface Props {
  /** 用来判断涨跌方向的数值；null/undefined 时不闪 */
  value: number | null | undefined
  className?: string
  title?: string
  children: ReactNode
}

/**
 * 数值变化时闪一下的表格单元格：涨绿闪、跌红闪。
 *
 * 刻意走 DOM 直接操作而不是 React state —— 行情看板一页 100 行，
 * 每行两个闪烁格，用 state 会在每次报价跳动时多触发 200 次渲染。
 * 这里只改 className，React 完全不参与。
 */
export function FlashCell({ value, className = '', title, children }: Props) {
  const ref = useRef<HTMLTableCellElement>(null)
  const prev = useRef(value)

  useEffect(() => {
    const before = prev.current
    prev.current = value
    const el = ref.current
    if (!el || before == null || value == null || value === before) return

    el.classList.remove('flash-up', 'flash-down')
    // 强制一次回流：不这么做的话，同方向连续跳动时 class 前后相同，
    // 浏览器认为没变化，CSS 动画不会重新播放
    void el.offsetWidth
    el.classList.add(value > before ? 'flash-up' : 'flash-down')
  }, [value])

  return <td ref={ref} className={className} title={title}>{children}</td>
}
