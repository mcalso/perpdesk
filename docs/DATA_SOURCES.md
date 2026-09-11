# 数据源实测记录

本文档记录 Binance 各数据通道的实测行为。项目里几处不显然的设计都源自这里，
改动数据层前请先读完。

测试时间：2026-09，在中国大陆开发机与阿里云香港服务器上分别复现。

## 结论速查

| 通道 | 状态 | 项目里的用法 |
|---|---|---|
| WS `stream.binancefuture.com` 全市场流 | ✅ 正常 | **主数据源**：`!ticker@arr` + `!markPrice@arr@1s`，1 秒一推 |
| WS `fstream.binance.com` 全市场流 | ❌ **静默，勿用** | 见下节 |
| WS `bookTicker`（两个端点都行） | ✅ 正常 | 自选标的实时买一/卖一 |
| REST `fapi.binance.com` | ✅ 正常，注意权重 | 首屏一次 + WS 断流兜底 + exchangeInfo |
| TradingView widget | ✅ | 图表，浏览器直连，不经后端 |

## 1. fstream.binance.com 的数组流是坏的（重要）

`wss://fstream.binance.com` 上，以下流**连得上但一帧数据都不推**：

```
!ticker@arr          SILENT    !miniTicker@arr    SILENT
!markPrice@arr@1s    SILENT    btcusdt@aggTrade   SILENT
btcusdt@bookTicker   OK        !bookTicker        OK
```

不是握手失败 —— TCP + TLS 正常，用 `{"method":"SUBSCRIBE",...}` 订阅甚至会收到
**成功应答**（`{"result":null,"id":1}`），然后就没有然后了。

**换成官方的另一个基础端点 `wss://stream.binancefuture.com`，同样的流全部正常：**

```
!ticker@arr          OK 1.05s  108,994B  358 条
!miniTicker@arr      OK 0.78s   77,173B  427 条
!markPrice@arr@1s    OK 0.92s    6,859B   39 条
btcusdt@aggTrade     OK 0.41s      167B
```

排查过程中排除的因素（都不是原因）：

- 合并流形式 —— `/stream?streams=` 和 `/ws/` 表现一致；
- permessage-deflate 压缩 —— 开关结果相同；
- 出口 IP / 地域 —— 国内机器与阿里云香港**都能复现**，换 HTTP 代理出口也一样；
- 客户端库版本 —— websockets 15 与 17 表现相同；
- 流名写错 —— SUBSCRIBE 返回的是成功应答。

推测与 Binance 正在进行的 **CM migration**（币本位合约并入同一套流）有关：恢复正常后的
payload 里出现了新增的 `st`（1=UM，2=CM）和 `ps` 字段。

> **教训**：这个现象一度被误判为"国内网络受限"，项目为此把数据层改成 REST 30 秒轮询，
> 实时性白白降了 30 倍。遇到"连得上但无数据"，**先换官方备用端点试一次**再怀疑网络。

## 2. `st` 字段必须过滤

CM 合并后，同一条流里既有 U 本位也有币本位合约。不按 `st == 1` 过滤，
币本位合约会混进 USDT 列表。

## 3. REST 权重是 IP 维度的

`x-mbx-used-weight-1m` 会被**同一出口 IP 上的所有程序**共同消耗。在一台还跑着其他
采集脚本的机器上，观测到权重在 272→930 之间攀升，418 在远未到 2400 上限时就出现，
且带不带 API key 都一样（市场数据限速不看 key）。曾见 `Retry-After: 1675`（28 分钟封禁）。

独占的机器上则很干净（香港服务器实测 `used-weight` 从 1 开始）。

对策（`backend/app/binance.py` 的 `_get`）：418/429 按 `Retry-After` 退避重试，
单次最长睡 30s；启动时 `exchange_info(retries=0)` 快速失败不阻塞；轮询失败保留上一份快照。

改用 WS 主数据源后，REST 正常情况下只在启动时调用一次。

## 4. API key 的能力边界

| 用途 | 能否 |
|---|---|
| 读取真实持仓 / 余额 / 成交明细 | ✅ 需要 key + secret |
| 提升市场数据限速 | ❌ 市场数据按 IP 限速 |
| 让 WS 公开流可用 | ❌ 公开流不校验 key |

项目只实现 GET 查询，`backend/app/account.py` 刻意不提供任何下单接口，
配**只读权限**的 key 即可。

## 5. userTrades 的 7 天窗口

`/fapi/v1/userTrades` 的 `startTime`/`endTime` 跨度不能超过 7 天，两者都不传则只返回
最近 7 天。直接传 30 天前的 `startTime` **不会报错**，但只会拿到很少的数据——
很容易误以为"账户就这么点成交"。

`account.user_trades_range()` 按 7 天窗口滚动分页并按 `tradeId` 去重；
单窗口打满 1000 条时从最后一条的时间继续，避免漏单。

## 6. 合约类型不止 PERPETUAL

Binance 还有 `TRADIFI_PERPETUAL`（股票 / 指数 / 商品代币化永续，`underlyingType`
为 `EQUITY` / `HK_EQUITY` / `INDEX` / `COMMODITY` 等）。按 `contractType == "PERPETUAL"`
过滤会把它们全漏掉，表现为**持有该类合约却查不到标记价**。放行后 USDT 标的数 528 → 718。

## 7. 字段名 REST 与 WS 不一致

REST 用完整名（`symbol` / `lastPrice` / `priceChangePercent`），WS 用缩写
（`s` / `c` / `P`）。`binance._norm_ticker()` 两边都认——其中 `symbol` 曾经漏了
WS 的 `s`，因为 fstream 那条流从来没推过数据，这个 KeyError 潜伏了很久才暴露。
