import { useEffect, useRef, useState } from 'react'

declare global {
  interface Window { TradingView?: { widget: new (cfg: Record<string, unknown>) => unknown } }
}

const TV_SCRIPT = 'https://s3.tradingview.com/tv.js'
let scriptPromise: Promise<void> | null = null

/** tv.js 只加载一次，多次切换标的复用同一份脚本 */
function loadTradingView(): Promise<void> {
  if (window.TradingView) return Promise.resolve()
  if (scriptPromise) return scriptPromise
  scriptPromise = new Promise((resolve, reject) => {
    const el = document.createElement('script')
    el.src = TV_SCRIPT
    el.async = true
    el.onload = () => resolve()
    el.onerror = () => { scriptPromise = null; reject(new Error('无法加载 TradingView 脚本')) }
    document.head.appendChild(el)
  })
  return scriptPromise
}

interface Props {
  /**
   * TradingView 的完整符号，如 `BINANCE:BTCUSDT.P`。
   * 由后端 /api/market/chart-source 解析给出 —— 中文标的在 TradingView 上
   * 是拼音名（牛来USDT → BINANCE:NIULAIUSDT.P），不能在前端拼。
   */
  tvSymbol: string
  interval?: string
  height?: number
}

/**
 * TradingView 官方免费 Advanced Chart。
 *
 * 图表由浏览器直连 TradingView 渲染，不经过本项目后端 —— 所以它不受后端到
 * Binance 那条链路的限速影响，指标与画线工具也都是 TradingView 原生的。
 * 代价是喂不进自定义数据；真要自研指标得换成 klinecharts，见 README。
 */
export function TradingViewChart({ tvSymbol, interval = '60', height = 620 }: Props) {
  const box = useRef<HTMLDivElement>(null)
  const [error, setError] = useState<string | null>(null)
  const containerId = useRef(`tv_${Math.random().toString(36).slice(2)}`)

  useEffect(() => {
    let cancelled = false
    setError(null)

    loadTradingView()
      .then(() => {
        if (cancelled || !box.current || !window.TradingView) return
        box.current.innerHTML = ''
        const host = document.createElement('div')
        host.id = containerId.current
        host.style.height = '100%'
        box.current.appendChild(host)

        new window.TradingView.widget({
          container_id: containerId.current,
          symbol: tvSymbol,
          interval,
          timezone: 'Asia/Shanghai',
          theme: 'dark',
          style: '1',
          locale: 'zh_CN',
          autosize: true,
          withdateranges: true,
          allow_symbol_change: true,
          details: true,
          hide_side_toolbar: false,
          studies: ['MASimple@tv-basicstudies', 'Volume@tv-basicstudies'],
          // 让 widget 的底色贴合页面面板；widget 不认识的键会被忽略，
          // 最坏情况是退回 TradingView 自带的 dark 主题（色差很小）
          backgroundColor: '#161a25',
          gridColor: 'rgba(36, 41, 56, 0.7)',
          overrides: {
            'paneProperties.background': '#161a25',
            'paneProperties.backgroundType': 'solid',
            'paneProperties.vertGridProperties.color': 'rgba(36, 41, 56, 0.7)',
            'paneProperties.horzGridProperties.color': 'rgba(36, 41, 56, 0.7)',
          },
        })
      })
      .catch((e: Error) => { if (!cancelled) setError(e.message) })

    return () => { cancelled = true }
  }, [tvSymbol, interval])

  if (error) {
    return (
      <div className="tv-fallback" style={{ height }}>
        <div className="tv-fallback-title">图表加载失败</div>
        <p>{error}</p>
        <p className="muted">
          浏览器需要能访问 <code>s3.tradingview.com</code>。
          若长期不通，可改用自建图表方案（见 README 的「图表方案」一节）。
        </p>
      </div>
    )
  }
  return <div className="tv-chart" ref={box} style={{ height }} />
}
