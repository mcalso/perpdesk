import { useLayoutEffect, useRef } from 'react'

interface Props {
  value: number | null | undefined
  /** 数字 → 显示文本。滚动过程中每帧都会调用 */
  format: (v: number | null | undefined) => string
  className?: string
  /** 滚动时长，毫秒 */
  duration?: number
}

const easeOutCubic = (p: number) => 1 - Math.pow(1 - p, 3)

/**
 * 数值变化时从旧值滚到新值。
 *
 * 只用在 hero 卡这类大字号数字上 —— 表格里几百个格子同时滚会糊成一片，
 * 那边用 FlashCell 的闪烁更合适。
 *
 * 与 FlashCell 同样走 DOM 直写：rAF 每帧一次 setState 的话，
 * 一次 420ms 的滚动就是 25 次组件重渲染。
 */
export function AnimatedNumber({ value, format, className, duration = 420 }: Props) {
  const ref = useRef<HTMLSpanElement>(null)
  const from = useRef<number | null | undefined>(undefined)
  const raf = useRef(0)
  // format 常以内联箭头函数传入，身份每次渲染都变；放进 ref 才不会反复触发动画
  const fmt = useRef(format)
  fmt.current = format

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return

    const start = from.current
    from.current = value

    const settle = () => { el.textContent = fmt.current(value) }

    // 首帧、空值、无变化：直接落定，不做动画。
    // 首帧刻意不从 0 滚上来 —— 那是金额，凭空长出来的过程会误导人。
    if (start == null || value == null || start === value) return settle()
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return settle()

    const t0 = performance.now()
    const tick = (now: number) => {
      const p = Math.min(1, (now - t0) / duration)
      el.textContent = fmt.current(start + (value - start) * easeOutCubic(p))
      if (p < 1) raf.current = requestAnimationFrame(tick)
    }
    cancelAnimationFrame(raf.current)
    raf.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf.current)
  }, [value, duration])

  return <span ref={ref} className={className} />
}
