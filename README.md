# perpdesk

自托管的永续合约分析工作台。行情与图表参考 TradingView 的使用习惯，数据来自 Binance
U 本位合约公开接口，以及你自己的账户（**只读**）。

单机部署、无外部依赖服务，适合个人自用。

> 本项目与 TradingView 无任何隶属关系。图表部分嵌入的是 TradingView 官方公开提供的
> 免费 Advanced Chart widget。
>
> 本项目仅做数据展示与记账分析，**不含任何下单、撤单或资金操作能力**，
> 也不构成任何投资建议。

## 功能

- **行情看板** —— 全市场 718 个 USDT 永续（加密 + 股票/指数代币化）的涨跌幅 / 成交额 / 资金费率排行，可搜索、
  可排序、一键加自选。
- **图表分析** —— 嵌入 TradingView 官方免费 Advanced Chart，指标、画线工具、多周期
  全部是 TradingView 原生的，中文界面；左侧自选列表联动切换。
- **持仓盈亏** —— 两套并存：
  - *交易所账户*：真实持仓、开仓价、浮动盈亏、杠杆、强平价、保证金占用（只读 API）；
    后端每 12s 集中拉一次快照喂所有前端连接，多开标签页不会放大对交易所的请求量；
  - *本地流水*：手工记账或从交易所同步成交，用净额移动加权平均成本法算已实现/浮动
    盈亏、持仓分布、累计已实现盈亏曲线。

## 快速开始

```bash
./scripts/perpdesk.sh start     # 同时起后端(18090)与前端热更新(18200)
./scripts/perpdesk.sh status
./scripts/perpdesk.sh logs      # 跟踪后端日志
./scripts/perpdesk.sh stop
```

- 日常开发访问 <http://127.0.0.1:18200>（Vite 热更新，API 自动代理到后端）
- 只要后端也行：`./scripts/perpdesk.sh start-api` 后访问 <http://127.0.0.1:18090>，
  后端会直接托管 `frontend/dist` 的构建产物（改完前端需 `npm run build --prefix frontend`）

> 服务默认只监听 `127.0.0.1`。部署在远程机器时，在 Cursor / VSCode remote 里端口会
> 自动转发；纯 SSH 的话自己开隧道：`ssh -L 18090:127.0.0.1:18090 user@your-server`
>
> ⚠️ 不要用 `pkill -f uvicorn` 之类停服务 —— 启动命令里含 `uvicorn`/`vite` 字样，
> `pkill -f` 会把执行它的那个 shell 自己也匹配上杀掉。用 `scripts/perpdesk.sh`，
> 它按 PID 文件管理。

## 配置

`backend/.env`（已 gitignore，权限 600）：

```
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
```

**只需要读取权限**，不要开交易/提现权限。不配也能用，只是持仓页少了"交易所账户"
那一块，本地记账功能不受影响。

端口通过环境变量覆盖：`PERPDESK_PORT`（后端）、`PERPDESK_WEB_PORT`（前端）。

## 架构

```
frontend/  React 18 + Vite 5 + TypeScript + recharts
           图表用 TradingView 官方 widget（浏览器直连 TradingView，不经后端）
backend/   FastAPI + SQLite
  binance.py   公开行情 REST（带 418/429 退避重试）
  account.py   账户只读接口（HMAC 签名；刻意不实现任何下单接口）
  hub.py       行情快照中心：REST 轮询全市场 + WS bookTicker 推自选
  icons.py     标的图标：TradingView logo 源 + 落盘缓存 + 首字母占位兜底
  pnl.py       净额移动加权平均成本法，支持多空与反手
data/      perpdesk.db（自选、交易流水）、icons/（图标缓存）
```

数据层为什么这么设计，见 **[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)** ——
某些网络环境下 Binance 合约的全市场 WS 流是不通的、REST 会间歇 418，那份文档记录了
完整实测过程与应对。**如果你部署在境外云服务器上，可以直接改用 WS 全市场流**，
实时性从 30s 提升到 1s，文档末尾写了怎么改。

## 刷新节奏

| 数据 | 周期 | 说明 |
|---|---|---|
| 自选标的价格 | 1s | WS `bookTicker` 实时推送 |
| 全市场 24h 行情 / 资金费率 | 30s | 后端 REST 轮询，失败保留上一份快照 |
| 交易所持仓 / 权益 | 12s | 后端集中轮询，前端 5s 读缓存 |
| 本地流水盈亏 | 20s | 本地计算，跟随行情标记价 |
| 合约列表 exchangeInfo | 10min | 新上架标的自动出现 |

账户与行情都在后端做集中缓存，**打开多个标签页不会增加对 Binance 的请求量**。

## 标的图标

图标由后端代理并落盘到 `data/icons/`，浏览器不直连外部 CDN（国内未必通）。
来源是 TradingView 的 symbol logo，加密与股票代币化合约都覆盖得到
（BTC → `crypto/XTVCBTC`，INTC → `intel`，SPCX → `spacex`）；个别查不到 logo 的
（如 HK0625）生成首字母占位图，保证一定有图可显示。

服务启动 30s 后会后台慢速预热全市场图标（约 2-3 分钟抓完 680 个），之后翻页都是
磁盘命中 + 浏览器 7 天缓存。想关掉把 `config.ICON_PREWARM_DELAY` 设为 0。
进度看 `/api/market/icon-stats`。

## 图表方案

当前用 TradingView 官方免费 widget：零维护、功能最全、中文界面，代价是**喂不进
自定义数据**。若以后要画自己的因子或自研指标，替换 `components/TradingViewChart.tsx`
即可，候选：

- [`klinecharts` v10](https://github.com/klinecharts/KLineChart)（Apache-2.0，活跃维护，
  指标和画线模型都在 core 里）+ 自写 UI 外壳；
- [`@klinecharts/pro`](https://github.com/klinecharts/pro)（开箱即用的 TradingView 式外壳，
  Datafeed 只需实现 4 个方法，但停更在 2023-03 / v0.1.1，peer 锁 `klinecharts@9`，
  要 vendor 进来自己养）；
- [`lightweight-charts`](https://github.com/tradingview/lightweight-charts)（TradingView 官方
  开源，活跃，但不含任何内置指标）。

## 已知限制

- 全市场行情 30 秒刷新一次（受限于 REST 轮询），只有自选标的是实时的；
- 被 Binance 限速时行情会停在上一份快照，顶栏状态灯会转黄并显示数据年龄；
- 本地流水的盈亏曲线只画已实现部分 —— 历史时点的浮盈要用当时市价才准，
  用当前价回算会把曲线变成"事后诸葛亮"；
- 无鉴权，只监听回环地址，不要直接绑 `0.0.0.0` 暴露到内网。
