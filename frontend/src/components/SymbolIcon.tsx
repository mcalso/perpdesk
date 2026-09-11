interface Props {
  symbol: string
  size?: number
}

/**
 * 标的图标。
 *
 * 图标由后端代理并落盘缓存（见 backend/app/icons.py），前端只管按 symbol 取；
 * 后端保证一定有返回（查不到 logo 会给首字母占位图），所以这里不用再写兜底 UI。
 */
export function SymbolIcon({ symbol, size = 20 }: Props) {
  return (
    <img
      src={`/api/market/icon/${symbol}`}
      alt=""
      width={size}
      height={size}
      loading="lazy"
      decoding="async"
      style={{
        width: size, height: size, borderRadius: '50%',
        flexShrink: 0, verticalAlign: 'middle', background: 'var(--panel-2)',
      }}
    />
  )
}
