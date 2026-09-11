# 数据源实测记录

本文档记录**受限网络环境**下 Binance 各数据通道的真实可用性。项目里几处看起来"绕"的
设计都源自这里的实测结果，改动数据层前请先读完。

> 测试环境是一台位于中国大陆的开发机，出口 IP 与其他程序共享。
> **如果你部署在境外云服务器上，多半不会遇到下面的限制**，可以直接改用 WS 全市场流
> （见文末），实时性会好很多。

测试时间：2026-09

## 结论速查

| 通道 | 可用性 | 项目里的用法 |
|---|---|---|
| REST `fapi.binance.com` | ⚠️ 可用但间歇 418 | 全市场 24h 行情、资金费率（30s 轮询 + 退避） |
| WS `bookTicker`（单标的 / 合并流） | ✅ 稳定 | 自选标的实时盘口 |
| WS `!ticker@arr` / `!miniTicker@arr` / `!markPrice@arr` | ❌ 连得上但永远静默 | 不可用 |
| WS `aggTrade` / `markPrice@1s` | ❌ 静默 | 不可用 |
| 现货 WS `!miniTicker@arr` | ✅ 可用 | 未使用（本项目只做合约） |
| TradingView widget | ✅ | 图表，由**浏览器**直连，不经后端 |

## 关键实测细节

### 1. 合约全市场数组流全部静默

连接能建立（TCP + TLS 握手成功、`websockets.connect` 正常返回），但一帧数据都不来。
受控实验（同一脚本并发建连，排除时序因素；`compression` 取 deflate / None 两组）：

```
FUT bookTicker          deflate/none  → OK 0.29s  163B
FUT combined x3         deflate/none  → OK 0.27s  202B
FUT aggTrade            deflate/none  → SILENT 25s
FUT markPrice@1s        deflate/none  → SILENT 25s
FUT !miniTicker@arr     deflate/none  → SILENT 25s
FUT !markPrice@arr@1s   deflate/none  → SILENT 25s
```

排除的可能原因：

- **不是合并流的问题** —— `combined x3 bookTicker` 正常；
- **不是压缩协商** —— 开关 deflate 结果一致；
- **不是出口 IP 风控** —— 换另一个出口（境外 HTTP 代理）同样静默；
- **不是 `fstream` 整体被封** —— 同域名的 `bookTicker` 一直正常。

具体成因未能定位（疑似中间链路对特定流量模式的处理），但结论足够指导架构：
**在这类网络下只能用 `bookTicker` + REST**。

### 2. REST 间歇性 418

`x-mbx-used-weight-1m` 观测到在 272 → 930 之间攀升，418 在权重远未到 2400 上限时
就会出现，且带不带 API key 都会中招：

```
#   with key          no key
1   418 w=272         200 w=352
3   200 w=516         418 w=556
```

**市场数据限速是 IP 维度的，API key 不参与。** 出口 IP 上若还有别的程序在调 Binance，
权重会叠加。曾观测到 `Retry-After: 1675`（28 分钟封禁）。

对策（见 `backend/app/binance.py` 的 `_get`）：

- 418/429 按 `Retry-After` 退避重试，单次最长睡 30s；
- 启动时 `exchange_info(retries=0)` 快速失败，不阻塞服务启动；
- 轮询失败**保留上一份快照**，绝不把空数据推给前端；
- 避免不带 symbol 的重权重接口（`ticker/24hr` 全市场是权重 80，已是必需的最低限度）。

### 3. API key 的能力边界

| 用途 | 能否 |
|---|---|
| 读取真实持仓 / 余额 / 成交明细 | ✅ 需要 key + secret |
| 提升市场数据限速 | ❌ 市场数据按 IP 限速 |
| 让 WS 全市场流可用 | ❌ 公开流不校验 key |

项目只实现 GET 查询，`backend/app/account.py` 刻意不提供任何下单接口，
配一个**只读权限**的 key 即可。

### 4. userTrades 的 7 天窗口

`/fapi/v1/userTrades` 的 `startTime`/`endTime` 跨度不能超过 7 天，两者都不传则只返回
最近 7 天。直接传 30 天前的 `startTime` 不会报错，但只会拿到很少的数据 —— 很容易
误以为"账户就这么点成交"。

`account.user_trades_range()` 按 7 天窗口滚动分页并按 `tradeId` 去重；单窗口打满
1000 条时从最后一条的时间继续，避免漏单。

### 5. 合约类型不止 PERPETUAL

Binance 现在还有 `TRADIFI_PERPETUAL`（股票 / 指数 / 商品代币化永续，
`underlyingType` 为 `EQUITY` / `HK_EQUITY` / `INDEX` / `COMMODITY` 等）。
按 `contractType == "PERPETUAL"` 过滤会把它们全漏掉，表现为**持有该类合约却查不到
标记价**。放行后 USDT 标的数从 528 → 718。

## 如果你的网络没有这些限制

境外云服务器上建议改回 WS 全市场流，实时性从 30s 提升到 1s：

1. `config.py` 的 `FSTREAM_URL` 改为
   `wss://fstream.binance.com/stream?streams=!ticker@arr/!markPrice@arr@1s`；
2. `hub.py` 里把 `_rest_loop` 降级为兜底（或只保留 `exchange_info` 刷新），
   新增一个 WS 循环，按 `stream` 名把 `!ticker` 帧写入 `snapshot`、
   `!markPrice` 帧写入 `premium`；
3. `bookTicker` 那条可以保留，也可以去掉 —— `!ticker@arr` 已经覆盖全市场。

Git 历史里有这版 WS 实现（后来因实测不可用才改成 REST 轮询），可以直接捡回来。
