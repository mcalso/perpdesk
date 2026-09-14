import { useEffect, useRef, useState } from 'react'
import {
  CandlestickSeries, HistogramSeries, createChart,
  type IChartApi, type ISeriesApi, type UTCTimestamp,
} from 'lightweight-charts'
import { api } from '../lib/api'

interface Props {
  symbol: string
  /** TradingView 的周期码，与内置图表的 Binance interval 做映射 */
  interval?: string
  height?: number
}

// 图表页的周期选择器用的是 TradingView 的码，这里转成 Binance 的 interval
const TV_TO_BINANCE: Record<string, string> = {
  '1': '1m', '5': '5m', '15': '15m', '30': '30m',
  '60': '1h', '240': '4h', 'D': '1d', 'W': '1w',
}

/**
 * 内置 K 线图，用于 TradingView 没有的合约。
 *
 * Binance 上有中文 symbol（龙虾USDT / 哈基米USDT / 币安人生USDT …），
 * TradingView 不存在对应符号，widget 只会显示 Invalid symbol，
 * 这些标的必须用自己的数据画图。数据走后端的 /api/market/klines。
 */
export function NativeChart({ symbol, interval = '60', height = 640 }: Props) {
  const box = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volumeRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)

  // 建图只做一次，切标的/周期时只换数据，避免整块重建导致闪烁
  useEffect(() => {
    if (!box.current) return
    const chart = createChart(box.current, {
      layout: {
        background: { color: '#161a25' },
        textColor: '#d9dce5',
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: 'rgba(36, 41, 56, 0.7)' },
        horzLines: { color: 'rgba(36, 41, 56, 0.7)' },
      },
      crosshair: { mode: 0 },
      rightPriceScale: { borderColor: '#242938', scaleMargins: { top: 0.08, bottom: 0.26 } },
      timeScale: { borderColor: '#242938', timeVisible: true, secondsVisible: false },
      localization: { locale: 'zh-CN' },
    })
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
      wickUpColor: '#26a69a', wickDownColor: '#ef5350',
    })
    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
    })
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })

    chartRef.current = chart
    candleRef.current = candle
    volumeRef.current = volume

    const ro = new ResizeObserver(() => {
      if (box.current) chart.applyOptions({ width: box.current.clientWidth })
    })
    ro.observe(box.current)
    chart.applyOptions({ width: box.current.clientWidth, height })

    return () => { ro.disconnect(); chart.remove(); chartRef.current = null }
  }, [height])

  useEffect(() => {
    let alive = true
    setLoading(true)
    setErr('')
    const binanceInterval = TV_TO_BINANCE[interval] || '1h'

    const load = () =>
      api.klines(symbol, binanceInterval, 500)
        .then((rows) => {
          if (!alive || !candleRef.current || !volumeRef.current) return
          candleRef.current.setData(rows.map((k) => ({
            time: (k.t / 1000) as UTCTimestamp,
            open: k.open, high: k.high, low: k.low, close: k.close,
          })))
          volumeRef.current.setData(rows.map((k) => ({
            time: (k.t / 1000) as UTCTimestamp,
            value: k.volume,
            color: k.close >= k.open ? 'rgba(38,166,154,.5)' : 'rgba(239,83,80,.5)',
          })))
          chartRef.current?.timeScale().fitContent()
          setLoading(false)
        })
        .catch((e: Error) => { if (alive) { setErr(e.message); setLoading(false) } })

    load()
    // 分钟级周期刷新勤一些，日线没必要
    const ms = ['1', '5', '15'].includes(interval) ? 15000 : 60000
    const t = setInterval(load, ms)
    return () => { alive = false; clearInterval(t) }
  }, [symbol, interval])

  return (
    <div style={{ position: 'relative' }}>
      <div ref={box} style={{ height }} />
      {(loading || err) && (
        <div className="chart-overlay">
          {err ? <span className="down">加载失败：{err}</span> : '加载中…'}
        </div>
      )}
    </div>
  )
}
