/**
 * 前端渲染冒烟测试：在 jsdom 里跑构建产物，确认关键路径不白屏。
 *
 *   npm run build && npm run smoke
 *
 * 为什么不上 vitest / testing-library：这里要验的不是组件单元行为，而是
 * "打包出来的东西在浏览器里到底能不能挂起来"。真跑构建产物能抓到
 * hook 顺序、懒加载分块、路由配置这些只在集成后才暴露的问题——
 * 「未配凭据打开持仓页白屏」就是这么抓到的，单测碰不到。
 *
 * 每个用例开一个子进程跑。同进程内连跑会出 React #321：入口可以用查询串
 * 强制重新求值，但它动态 import 的懒加载分块按自身 URL 缓存，于是四个用例
 * 共用第一个实例里的 React，而各自的根组件用的是新实例的 React —— 两份
 * React 混在一棵树里，hook 调用直接报错。
 */
import { JSDOM } from 'jsdom'
import { readdirSync, existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const DIST = join(dirname(fileURLToPath(import.meta.url)), '..', 'dist', 'assets')
if (!existsSync(DIST)) {
  console.error('找不到 dist/，先跑 npm run build')
  process.exit(1)
}
const ENTRY = readdirSync(DIST).find((f) => /^index-.*\.js$/.test(f))

const DAY = 86400000
const T0 = Date.parse('2026-01-01T00:00:00Z')

const fixtures = {
  daily: Array.from({ length: 20 }, (_, i) => ({
    d: T0 + i * DAY, realized: (i % 3 === 0 ? -1 : 1) * (10 + i),
    fee: 0.5 + i * 0.1, funding: -0.2, trades: 2 + (i % 4),
  })),
  curve: Array.from({ length: 40 }, (_, i) => ({ t: T0 + i * DAY / 2, realized: i * 3 - 20 })),
  positions: ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'DOGEUSDT'].map((symbol, i) => ({
    symbol, qty: 0, avgCost: 0, markPrice: 0, value: 0, costBasis: 0,
    unrealized: 0, unrealizedPct: 0, realized: (i - 2) * 40, fee: 3, funding: -1,
    tradeCount: 5, side: 'FLAT', lastAt: T0,
  })),
}

/** 起一个干净的 jsdom，挂好浏览器全局，返回 { dom, calls } */
async function mount({ hash = '#/market', routes = {}, tag }) {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>',
                        { url: `http://localhost/${hash}`, pretendToBeVisual: true })
  globalThis.window = dom.window
  globalThis.document = dom.window.document
  for (const k of ['navigator', 'HTMLElement', 'Element', 'Node', 'CustomEvent', 'Event',
                   'MutationObserver', 'getComputedStyle', 'requestAnimationFrame',
                   'cancelAnimationFrame', 'location', 'history', 'localStorage', 'SVGElement']) {
    if (dom.window[k] !== undefined) globalThis[k] = dom.window[k]
  }
  globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
  dom.window.matchMedia = globalThis.matchMedia
  // recharts 的 ResponsiveContainer 拿不到尺寸就不画图，喂一个固定尺寸
  globalThis.ResizeObserver = class {
    constructor(cb) { this.cb = cb }
    observe(el) { this.cb([{ target: el, contentRect: { width: 600, height: 300 } }]) }
    unobserve() {} disconnect() {}
  }
  dom.window.ResizeObserver = globalThis.ResizeObserver
  globalThis.WebSocket = class { constructor() {} close() {} addEventListener() {} }
  dom.window.WebSocket = globalThis.WebSocket

  const calls = []
  const errors = []
  globalThis.fetch = async (url) => {
    const u = String(url)
    calls.push(u)
    for (const [frag, body] of Object.entries(routes)) {
      if (u.includes(frag)) {
        if (typeof body === 'number') {
          return { ok: false, status: body, statusText: String(body),
                   json: async () => ({ detail: `mock ${body}` }) }
        }
        return { ok: true, json: async () => body }
      }
    }
    return { ok: true, json: async () => ({ rows: [], total: 0 }) }
  }
  dom.window.fetch = globalThis.fetch
  const origErr = console.error
  console.error = (...a) => { errors.push(a.map(String).join(' ')) }

  await import(pathToFileURL(join(DIST, ENTRY)).href + `?case=${encodeURIComponent(tag)}`)
  await new Promise((r) => setTimeout(r, 900))
  console.error = origErr

  return { dom, doc: dom.window.document, calls, errors,
           html: dom.window.document.getElementById('root').innerHTML }
}

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok, detail })
  console.log(`  ${ok ? '✓' : '✗'} ${name}${detail ? `  ${detail}` : ''}`)
}

const CASES = {}
const testCase = (name, fn) => { CASES[name] = fn }

const AUTHED = { authenticated: true, hasPassword: true }
const OVERVIEW = {
  balances: [{ asset: 'USDT', balance: 1000, available: 800, unrealized: 0 }],
  positions: [], equity: 1000, wallet: 1000, totalUnrealized: 0,
  grossNotional: 0, ageSec: 1, error: '', pollInterval: 6,
}
const SUMMARY = {
  positions: fixtures.positions, allocation: [],
  summary: { totalRealized: 120, totalUnrealized: 0, totalPnl: 120, totalFee: 33,
             grossExposure: 0, netExposure: 0, openCount: 0, symbolCount: 4,
             totalFunding: -4, exchangeRealized: 0, exchangeCommission: 0,
             hasIncome: true, days: null, rangeFrom: null, accountId: 1 },
}

// ---------------------------------------------------------------- 用例

testCase('未登录', async () => {
  const { html, calls } = await mount({
    tag: 'anon', routes: { '/auth/me': { authenticated: false, hasPassword: true } },
  })
  check('只渲染登录页', html.includes('login-card') && !html.includes('行情看板'))
  check('不发任何业务请求', calls.filter((c) => !c.includes('/api/auth/')).length === 0)
})

testCase('已登录', async () => {
  const { html, errors } = await mount({ tag: 'authed', routes: { '/auth/me': AUTHED } })
  check('进入主界面', html.includes('行情看板') && !html.includes('login-card'))
  check('无渲染报错', errors.filter((e) => /Error|错误/.test(e)).length === 0,
        errors[0]?.slice(0, 80) || '')
})

testCase('未配置 API 凭据（回归：曾因 hook 顺序白屏）', async () => {
  const { html, errors } = await mount({
    tag: 'noapi', hash: '#/portfolio',
    routes: {
      '/auth/me': AUTHED,
      '/account/status': { configured: false, hasKey: false, hasSecret: false, hint: '' },
      '/account/overview': 412,
      '/portfolio/summary': SUMMARY,
      '/portfolio/curve': { points: fixtures.curve, from: T0, to: T0 + 20 * DAY, total: 40, final: 100 },
      '/portfolio/daily': { rows: fixtures.daily, winDays: 13, lossDays: 7 },
    },
  })
  check('持仓页不白屏', html.length > 200 && html.includes('历史盈亏分析'),
        `${html.length} 字节`)
  check('给出"未配置凭据"提示', html.includes('未配置 API 凭据'))
  check('没有 hook 顺序错误', !errors.some((e) => e.includes('fewer hooks')))
})

testCase('图表组', async () => {
  const { doc, dom } = await mount({
    tag: 'deck', hash: '#/portfolio',
    routes: {
      '/auth/me': AUTHED,
      '/account/status': { configured: true, hasKey: true, hasSecret: true, hint: '' },
      '/account/overview': OVERVIEW,
      '/portfolio/summary': SUMMARY,
      '/portfolio/curve': { points: fixtures.curve, from: T0, to: T0 + 20 * DAY, total: 40, final: 100 },
      '/portfolio/daily': { rows: fixtures.daily, winDays: 13, lossDays: 7 },
    },
  })
  const title = () => doc.querySelector('.deck-main .panel-head')?.textContent?.trim() || ''
  const thumbs = () => [...doc.querySelectorAll('.deck-thumb-title')].map((e) => e.textContent)

  check('默认放大第一张', title().startsWith('已实现盈亏曲线'), title().slice(0, 12))
  check('轨道是其余 N-1 张', thumbs().length === 4, JSON.stringify(thumbs()))
  check('缩略图画出了图形', doc.querySelectorAll('.deck-thumb-body svg').length === 4)

  const before = title().slice(0, 7)
  const target = doc.querySelectorAll('.deck-thumb')[1]
  const label = target.querySelector('.deck-thumb-title').textContent
  target.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
  await new Promise((r) => setTimeout(r, 300))

  check('点击后换到大图位', title().startsWith(label), `→ ${title().slice(0, 12)}`)
  check('原大图退回轨道', thumbs().includes(before))
  check('轨道恒为 N-1', thumbs().length === 4)
  check('轨道保持原序', thumbs()[0] === before, JSON.stringify(thumbs()))
})

// ---------------------------------------------------------------- 执行

const only = process.argv.find((a) => a.startsWith('--case='))?.slice(7)

if (only) {
  // 子进程：只跑一个用例，结果按行输出给父进程
  const fn = CASES[only]
  if (!fn) { console.error(`没有用例 ${only}`); process.exit(2) }
  await fn()
  console.log('__RESULT__' + JSON.stringify(results))
  process.exit(results.every((r) => r.ok) ? 0 : 1)
}

// 父进程：每个用例一个子进程，互不污染模块缓存与定时器
const { spawnSync } = await import('node:child_process')
const self = fileURLToPath(import.meta.url)
let pass = 0, total = 0
const failures = []

for (const name of Object.keys(CASES)) {
  console.log(`\n${name}`)
  const r = spawnSync(process.execPath, [self, `--case=${name}`],
                      { encoding: 'utf8', timeout: 90_000 })
  const line = (r.stdout || '').split('\n').find((l) => l.startsWith('__RESULT__'))
  if (!line) {
    console.log('  ✗ 子进程未产出结果')
    console.log((r.stdout || '').split('\n').filter(Boolean).slice(-4).map(l => '    ' + l).join('\n'))
    console.log((r.stderr || '').split('\n').filter(Boolean).slice(-4).map(l => '    ' + l).join('\n'))
    failures.push(name); total += 1
    continue
  }
  for (const c of JSON.parse(line.slice(10))) {
    total += 1
    if (c.ok) pass += 1
    else failures.push(`${name} / ${c.name}`)
    console.log(`  ${c.ok ? '✓' : '✗'} ${c.name}${c.detail ? `  ${c.detail}` : ''}`)
  }
}

console.log(`\n${pass}/${total} 通过`)
if (failures.length) {
  console.log('失败：' + failures.join('、'))
  process.exit(1)
}
