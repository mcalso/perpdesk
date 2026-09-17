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

/**
 * 起一个干净的 jsdom，挂好浏览器全局。
 *
 * `until` 给了就轮询等它成立，而不是睡一个固定时长 —— recharts 要等
 * ResizeObserver 回调才开始画，固定睡法在机器忙时会偶发失败，而偶发失败的
 * 测试比没有测试更糟：它训练你忽略红色。（本文件真出现过这种抖动。）
 */
async function mount({ hash = '#/market', routes = {}, tag, until, timeout = 6000 }) {
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
  // 必须带上 readyState 与 OPEN/CLOSED 这些静态常量。少了它们，业务代码里
  // `ws.readyState === WebSocket.OPEN` 会退化成 undefined === undefined 恒真，
  // 于是跑进只有真浏览器才有的分支 —— 这个假实现曾因此漏掉一次真实崩溃。
  globalThis.WebSocket = class {
    static CONNECTING = 0
    static OPEN = 1
    static CLOSING = 2
    static CLOSED = 3
    constructor() { this.readyState = 0 }   // 没人触发 onopen，就停在 CONNECTING
    send() {}
    close() { this.readyState = 3 }
    addEventListener() {}
  }
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
  const doc = dom.window.document
  if (until) {
    const deadline = Date.now() + timeout
    while (Date.now() < deadline && !until(doc)) {
      await new Promise((r) => setTimeout(r, 60))
    }
  } else {
    await new Promise((r) => setTimeout(r, 800))
  }
  console.error = origErr

  return { dom, doc, calls, errors,
           html: doc.getElementById('root').innerHTML }
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
  // 顶栏大盘靠 WS 推数据，冒烟里 WS 是假的，所以只验它不渲染时也不报错
  check('顶栏无 WS 时不崩', !html.includes('pulse-item'))
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
    until: (d) => d.querySelectorAll('.deck-thumb-body svg').length === 4,
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
  check('缩略图画出了图形', doc.querySelectorAll('.deck-thumb-body svg').length === 4,
        `svg=${doc.querySelectorAll('.deck-thumb-body svg').length} thumb=${doc.querySelectorAll('.deck-thumb-body').length}`)

  const before = title().slice(0, 7)
  const target = doc.querySelectorAll('.deck-thumb')[1]
  const label = target.querySelector('.deck-thumb-title').textContent
  target.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
  for (let i = 0; i < 60 && !title().startsWith(label); i++) {
    await new Promise((r) => setTimeout(r, 40))
  }

  check('点击后换到大图位', title().startsWith(label), `→ ${title().slice(0, 12)}`)
  check('原大图退回轨道', thumbs().includes(before))
  check('轨道恒为 N-1', thumbs().length === 4)
  check('轨道保持原序', thumbs()[0] === before, JSON.stringify(thumbs()))
})

testCase('行情看板', async () => {
  const NOW = Date.now()
  const DAY = 86400000
  const T = (symbol, base, assetClass, sector, chgPct, quoteVolume, live, extra = {}) => ({
    symbol, base, assetClass, sector, last: 100, open: 0, high: 110, low: 90,
    chgPct, quoteVolume, volume: 0, trades: 0, fundingRate: 0.0001,
    markPrice: 100, bid: null, ask: null, live, ts: 0,
    onboardDate: NOW - 800 * DAY,          // 默认是老标的
    // 多给 30 秒余量：倒计时是向下取整，跑测试的这一两秒会让
    // 2h13m00s 掉成 2h12m，断言就成了偶发红灯
    nextFundingTime: NOW + (2 * 60 + 13) * 60000 + 30000,
    ...extra,
  })
  const tickers = [
    T('BTCUSDT', 'BTC', 'crypto', 'PoW', 3.2, 9.8e9, true),
    T('ETHUSDT', 'ETH', 'crypto', 'Layer-1', -1.4, 3.1e9, true),
    // 3 天前上架 → 该有 NEW；且 high === low（新标的常见）→ 区间条必须不画
    T('SOLUSDT', 'SOL', 'crypto', 'Layer-1', 2.0, 1.5e9, true,
      { onboardDate: NOW - 3 * DAY, high: 100, low: 100 }),
    // 30 天前上架：刚过 14 天阈值。没有这个样本的话，把阈值从 14 调到
    // 400 也不会有任何用例报红 —— 变异测试实测存活过一次
    T('NVDAUSDT', 'NVDA', 'us_equity', 'other', 0.6, 2.2e7, false,
      { onboardDate: NOW - 30 * DAY }),
  ]
  const { dom, doc, html } = await mount({
    tag: 'market', hash: '#/market',
    routes: {
      '/auth/me': AUTHED,
      '/market/tickers': { rows: tickers, total: 4, status: {} },
      '/api/watchlist': { rows: [] },
    },
    until: (d) => d.querySelectorAll('tbody tr').length === 4,
  })
  check('表格渲染出所有标的', doc.querySelectorAll('tbody tr').length === 4)
  check('涨跌比例条已画', doc.querySelectorAll('.ratio .up-part').length === 1)
  check('领涨/领跌可点击', doc.querySelectorAll('.lead').length === 2,
        `${doc.querySelectorAll('.lead').length} 个`)
  const bars = [...doc.querySelectorAll('.bar-cell')]
  check('数据条已套用', bars.length === 8, `${bars.length} 格`)
  check('数据条按量级缩放',
        bars.some((b) => /--bar:\s*100%/.test(b.getAttribute('style') || '')),
        bars.slice(0, 2).map((b) => b.getAttribute('style')).join(' | '))
  check('未出现遗留的内联 fontWeight', !html.includes('font-weight: 400'))

  // 列宽写死在 <colgroup> 里，和表头是两处独立声明 —— 将来加一列只改了
  // thead 的话，整张表的列宽会从那一列起全部错位，且不报任何错
  const cols = doc.querySelectorAll('.market-table colgroup col').length
  const ths = doc.querySelectorAll('.market-table thead th').length
  check('colgroup 列数与表头一致', cols === ths && cols === 7, `col=${cols} th=${ths}`)
  check('标的列不写死宽度（吃剩余空间）',
        !doc.querySelectorAll('.market-table colgroup col')[1].getAttribute('style'))
  check('行情页用占满视口的布局', !!doc.querySelector('.page.fill'))

  // ---- 零成本三项：区间条 / 结算倒计时 / 新上架角标 ----
  const dots = [...doc.querySelectorAll('.range-dot')]
  check('24h 区间条已画', dots.length === 3, `${dots.length} 根（SOL 的 high===low 不该画）`)
  check('区间条位置按 (last-low)/(high-low) 算',
        /left:\s*50%/.test(dots[0].getAttribute('style') || ''),
        dots[0].getAttribute('style'))
  // high === low 时除零会得到 NaN，CSS 里 left:NaN% 整条规则被丢弃，
  // 圆点默认贴最左 —— 看着像「就在 24h 最低点」，是会误导判断的假信号
  const solRow = [...doc.querySelectorAll('tbody tr')].find(
    (tr) => tr.textContent.includes('SOL'))
  check('区间为零时不画条（除零会造出假信号）',
        !solRow.querySelector('.range'))

  check('新上架打了 NEW 角标',
        solRow.querySelector('.tag.new')?.textContent === 'NEW')
  const rowOf = (name) => [...doc.querySelectorAll('tbody tr')].find(
    (tr) => tr.textContent.includes(name))
  check('老标的不打 NEW', !rowOf('BTC').querySelector('.tag.new'))
  // 30 天 > 14 天阈值，必须不打 —— 这条才真正守住阈值本身。
  // 注：把阈值从 14 改成 20 是等价变异，这里抓不住，也不该抓 ——
  // 样本是 3 天和 30 天，两个阈值下行为一样。要抓就得放 15 天和 19 天的
  // 样本，那是在钉一个产品判断出来的常数，不是在测行为。
  check('刚过阈值的不打 NEW', !rowOf('NVDA').querySelector('.tag.new'))
  check('全表只有一个 NEW', doc.querySelectorAll('.tag.new').length === 1,
        `${doc.querySelectorAll('.tag.new').length} 个`)

  check('资金费率下有结算倒计时',
        [...doc.querySelectorAll('.sub-line')].some((e) => e.textContent === '2h13m'),
        JSON.stringify([...doc.querySelectorAll('.sub-line')].map((e) => e.textContent)))

  // ---- 二级板块筛选 ----
  const chips = () => [...doc.querySelectorAll('.chips button')].map((b) => b.textContent)
  const tab = (name) => [...doc.querySelectorAll('.seg button')].find(
    (b) => b.textContent.startsWith(name))
  const clickAndWait = async (el, ready) => {
    el.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
    for (let i = 0; i < 60 && !ready(); i++) await new Promise((r) => setTimeout(r, 40))
  }

  // 「全部」下不该出现二级：那会把 155 个美股并进「其他」，
  // 既和上面的「美股 155」重复，计数也不再是对当前视图的划分
  check('「全部」下不显示二级', doc.querySelectorAll('.chips button').length === 0,
        JSON.stringify(chips()))

  await clickAndWait(tab('加密'), () => doc.querySelectorAll('.chips button').length > 0)
  check('选定大类后二级才出现', doc.querySelectorAll('.chips button').length === 3,
        JSON.stringify(chips()))
  check('板块带计数且按数量降序',
        /^全部3$/.test(chips()[0]) && /^Layer-12$/.test(chips()[1])
          && /^PoW1$/.test(chips()[2]),
        JSON.stringify(chips()))
  check('行内打了板块标签',
        [...doc.querySelectorAll('.sym-cell .tag')].map((e) => e.textContent).includes('Layer-1'),
        JSON.stringify([...doc.querySelectorAll('.sym-cell .tag')].map((e) => e.textContent)))

  const layer1 = [...doc.querySelectorAll('.chips button')].find(
    (b) => b.textContent.startsWith('Layer-1'))
  layer1.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
  for (let i = 0; i < 60 && doc.querySelectorAll('tbody tr').length !== 2; i++) {
    await new Promise((r) => setTimeout(r, 40))
  }
  const shown = [...doc.querySelectorAll('tbody .sym-base')].map((e) => e.textContent)
  check('点板块后只剩该板块的标的', shown.length === 2 && shown.includes('ETH')
        && shown.includes('SOL'), JSON.stringify(shown))

  // 换大类时二级必须归位，否则会得到一张空表而用户不知道是谁在过滤
  await clickAndWait(tab('美股'), () => doc.querySelectorAll('tbody tr').length === 1)
  check('换大类后二级筛选已归位',
        doc.querySelectorAll('tbody tr').length === 1
          && doc.querySelectorAll('tbody .sym-base')[0]?.textContent === 'NVDA',
        `${doc.querySelectorAll('tbody tr').length} 行`)
})

testCase('资讯页', async () => {
  const T = Date.now()
  const rows = [
    { id: 3, source: 'jin10', ts: T - 60000, title: '美联储维持利率不变',
      content: '美联储宣布维持基准利率在 4.25%-4.50% 区间不变。', link: 'https://x',
      important: true, tags: ['macro'], symbols: [] },
    { id: 2, source: 'jin10', ts: T - 120000, title: '',
      content: '德国加大天然气储备填充力度。', link: '',
      important: false, tags: ['commodity'], symbols: ['NATGASUSDT'] },
    { id: 1, source: 'jin10', ts: T - 900000, title: '英伟达发布新一代芯片',
      content: '英伟达在发布会上公布了下一代 AI 加速卡。', link: '',
      important: false, tags: ['equity'], symbols: ['NVDAUSDT'] },
  ]
  const { doc, html } = await mount({
    tag: 'news', hash: '#/news',
    until: (d) => d.querySelectorAll('.flash').length === 3,
    routes: {
      '/auth/me': AUTHED,
      '/api/news': { rows, filteredBy: [],
                     status: { sources: ['jin10'], pollInterval: 60, total: 3,
                               latest: T, ageSec: 5, error: '', symbolsKnown: 718 } },
    },
  })
  check('渲染出快讯条目', doc.querySelectorAll('.flash').length === 3,
        `${doc.querySelectorAll('.flash').length} 条`)
  check('重要条目有标记', doc.querySelectorAll('.flash.hot').length === 1)
  check('关联标的可点', doc.querySelectorAll('.flash-sym').length === 2)
  check('原始 HTML 不被注入', !html.includes('<b>') && !html.includes('<script'))
  check('板块标签已中文化', html.includes('宏观') && html.includes('商品'))
})

testCase('设置页', async () => {
  const accounts = [
    { id: 1, exchange: 'binance', market: 'usdm', label: '主账户', enabled: 1, sort_order: 0,
      created_at: 0, configured: true, credentials: { api_key: 'abcd••••••••wxyz' }, trades: 120 },
    { id: 2, exchange: 'okx', market: 'usdm', label: '二号', enabled: 0, sort_order: 1,
      created_at: 0, configured: false, credentials: {}, trades: 0 },
  ]
  const { doc, dom, html } = await mount({
    tag: 'settings', hash: '#/settings',
    until: (d) => d.querySelectorAll('.acct-card').length === 2,
    routes: {
      '/auth/me': AUTHED,
      '/account/accounts': { rows: accounts, defaultId: 1 },
      '/auth/sessions': { rows: [{ created_at: 0, expires_at: 0, last_seen: 1789000000,
                                   label: 'Mozilla/5.0 测试' }], now: 1789000001 },
    },
  })
  check('列出所有账户', doc.querySelectorAll('.acct-card').length === 2,
        `${doc.querySelectorAll('.acct-card').length} 个`)
  check('凭据只显示掩码', html.includes('abcd') && !html.includes('api_secret:'))
  check('区分已配/未配凭据', html.includes('已配凭据') && html.includes('未配凭据'))
  check('登录设备已列出', html.includes('Mozilla/5.0 测试'))

  // jsdom 跑在 http://localhost 上，本机访问按设计是允许录入凭据的
  // （没有中间链路可窃听，且首次配置往往就发生在还没证书的时候）
  const btn = [...doc.querySelectorAll('button')].find((b) => b.textContent.includes('录入 API 凭据'))
  check('未配凭据的账户有录入入口', !!btn)
  btn?.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
  await new Promise((r) => setTimeout(r, 200))
  const inputs = [...doc.querySelectorAll('input')].map((i) => i.placeholder)
  check('本机访问放行凭据表单', inputs.includes('API Key') && inputs.includes('API Secret'),
        JSON.stringify(inputs.filter(Boolean)))
  check('secret 输入框是密码类型',
        [...doc.querySelectorAll('input')].some((i) => i.placeholder === 'API Secret'
                                                    && i.type === 'password'))
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
