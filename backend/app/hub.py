"""全市场行情快照中心。

三条数据通道，各司其职（详见 docs/DATA_SOURCES.md）：

* **全市场 24h 行情 + 标记价/资金费率** —— WS 合并流 `!ticker@arr` +
  `!markPrice@arr@1s`，1 秒一推，零 REST 权重。注意这两个都是**增量**流，
  只推有变化的标的，所以快照要累积更新而不是整体替换。
* **自选标的实时盘口** —— WS `bookTicker` 合并流，提供买一/卖一。
* **REST** —— 只做两件事：启动首屏填充（增量流要几十秒才覆盖全市场），
  以及 WS 断流超时后的兜底轮询。正常运行时一次都不会调用。
"""
import asyncio
import json
import logging
import time

import websockets

from . import binance, config

log = logging.getLogger("perpdesk.hub")


class TickerHub:
    def __init__(self) -> None:
        self.snapshot: dict[str, dict] = {}      # symbol -> 24h 行情
        self.premium: dict[str, dict] = {}       # symbol -> 标记价/资金费率
        self.book: dict[str, dict] = {}          # symbol -> 实时买一卖一（仅自选）
        self.meta: dict[str, dict] = {}
        self.market_ws_connected = False
        self.last_market_at = 0.0
        self.ws_connected = False
        self.ws_symbols: list[str] = []
        self.last_rest_ok = 0.0
        self.last_rest_error = ""
        self.rest_fallback_used = 0
        self.last_book_at = 0.0
        self._subscribers: set[asyncio.Queue] = set()
        self._tasks: list[asyncio.Task] = []
        self._resubscribe = asyncio.Event()

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        try:
            # 快速失败：启动不为 REST 重试阻塞，后台循环会持续补齐
            self.meta = await binance.exchange_info(retries=0)
        except Exception as exc:
            log.error("initial exchangeInfo failed, retrying in background: %s", exc)
        # 首屏拉一次 REST：全市场数组流是增量的，冷启动要几十秒才铺满，
        # 这一次调用（权重约 40）换来的是打开即有数据。
        try:
            rows = await binance.tickers_24h(retries=1)
            self.snapshot = {k: v for k, v in rows.items() if not self.meta or k in self.meta}
            self.last_rest_ok = time.time()
        except Exception as exc:
            log.warning("initial ticker snapshot failed, WS will fill in: %s", exc)

        self._tasks = [
            asyncio.create_task(self._market_loop(), name="hub-market"),
            asyncio.create_task(self._rest_fallback_loop(), name="hub-rest-fallback"),
            asyncio.create_task(self._meta_loop(), name="hub-meta"),
            asyncio.create_task(self._book_loop(), name="hub-book"),
            asyncio.create_task(self._broadcast_loop(), name="hub-broadcast"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    # ---------- WS：全市场行情（主数据源） ----------

    async def _market_loop(self) -> None:
        url = f"{config.FSTREAM_BASE}/stream?streams={config.MARKET_STREAMS}"
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, proxy=None, max_size=32 << 20
                ) as ws:
                    self.market_ws_connected = True
                    backoff = 1.0
                    log.info("market ws connected: %s", config.MARKET_STREAMS)
                    async for raw in ws:
                        self._handle_market(json.loads(raw))
                        self.last_market_at = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.market_ws_connected = False
                log.warning("market ws dropped (%s), retry in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _handle_market(self, msg: dict) -> None:
        """combined 帧按 stream 名分流。

        payload 里的 `st` 是合约品种标记（1=U 本位 UM，2=币本位 CM）——
        CM 合并进来之后同一条流里两种都有，不过滤会把币本位混进 USDT 列表。
        """
        stream = msg.get("stream", "")
        rows = msg.get("data") or []
        if not isinstance(rows, list):
            rows = [rows]

        if stream.startswith("!ticker"):
            for r in rows:
                sym = r.get("s")
                if not sym or r.get("st") not in (None, 1):
                    continue
                if self.meta and sym not in self.meta:
                    continue
                self.snapshot[sym] = binance.norm_ws_ticker(r)
        elif stream.startswith("!markPrice"):
            for r in rows:
                sym = r.get("s")
                if not sym or r.get("st") not in (None, 1):
                    continue
                if self.meta and sym not in self.meta:
                    continue
                self.premium[sym] = {
                    "markPrice": float(r.get("p") or 0),
                    "indexPrice": float(r.get("i") or 0),
                    "fundingRate": float(r.get("r") or 0),
                    "nextFundingTime": int(r.get("T") or 0),
                }

    # ---------- REST 兜底：只在 WS 断流时启用 ----------

    async def _rest_fallback_loop(self) -> None:
        delay = config.REST_POLL_INTERVAL
        # 还没收到过 WS 数据时以启动时刻计时，否则刚启动的一瞬间会被误判成断流，
        # 白白多打一次权重 40 的接口。
        started = time.time()
        while True:
            await asyncio.sleep(5.0)
            last = self.last_market_at or started
            if time.time() - last < config.WS_STALE_SEC:
                delay = config.REST_POLL_INTERVAL
                continue
            # WS 久无数据，退回 REST 拉一次，避免前端长时间看陈旧价格
            try:
                rows = await binance.tickers_24h(retries=1)
                if rows:
                    self.snapshot.update(
                        {k: v for k, v in rows.items() if not self.meta or k in self.meta}
                    )
                    self.last_rest_ok = time.time()
                    self.last_rest_error = ""
                    self.rest_fallback_used += 1
                    log.warning("market ws stale, fell back to REST (#%d)", self.rest_fallback_used)
                try:
                    rows = await binance.premium_index(retries=1)
                    self.premium.update(
                        {k: v for k, v in rows.items() if not self.meta or k in self.meta}
                    )
                except Exception as exc:
                    log.debug("premiumIndex fallback failed: %s", exc)
                delay = config.REST_POLL_INTERVAL
            except Exception as exc:
                self.last_rest_error = str(exc)[:120]
                delay = min(delay * 1.6, config.REST_POLL_MAX_INTERVAL)
                log.warning("REST fallback failed (%s), next in %.0fs", exc, delay)
            await asyncio.sleep(delay)

    async def _meta_loop(self) -> None:
        while True:
            try:
                self.meta = await binance.exchange_info(force=True)
                await asyncio.sleep(config.META_REFRESH_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("exchangeInfo refresh failed: %s", exc)
                await asyncio.sleep(60.0)

    # ---------- WS：自选标的实时盘口 ----------

    def set_ws_symbols(self, symbols: list[str]) -> None:
        """自选列表变化时调用，触发 WS 重新订阅。"""
        wanted = [s.upper() for s in symbols][: config.WS_MAX_STREAMS]
        if wanted != self.ws_symbols:
            self.ws_symbols = wanted
            self._resubscribe.set()

    async def _book_loop(self) -> None:
        backoff = 1.0
        while True:
            if not self.ws_symbols:
                await asyncio.sleep(2.0)
                continue
            symbols = list(self.ws_symbols)
            streams = "/".join(f"{s.lower()}@bookTicker" for s in symbols)
            url = f"{config.FSTREAM_BASE}/stream?streams={streams}"
            self._resubscribe.clear()
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, proxy=None, max_size=8 << 20
                ) as ws:
                    self.ws_connected = True
                    backoff = 1.0
                    log.info("bookTicker ws connected: %d symbols", len(symbols))
                    while True:
                        if self._resubscribe.is_set():
                            log.info("watchlist changed, resubscribing")
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            continue          # 冷门标的可能几秒无盘口变化，正常
                        self._handle_book(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ws_connected = False
                log.warning("bookTicker ws dropped (%s), retry in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _handle_book(self, msg: dict) -> None:
        d = msg.get("data", msg)
        sym = d.get("s")
        if not sym:
            return
        try:
            bid, ask = float(d["b"]), float(d["a"])
        except (KeyError, TypeError, ValueError):
            return
        self.book[sym] = {
            "bid": bid, "ask": ask, "mid": (bid + ask) / 2,
            "bidQty": float(d.get("B") or 0), "askQty": float(d.get("A") or 0),
            "ts": int(d.get("E") or 0),
        }
        self.last_book_at = time.time()

    # ---------- 对前端广播 ----------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def _broadcast_loop(self) -> None:
        while True:
            await asyncio.sleep(config.BROADCAST_INTERVAL)
            if not self._subscribers:
                continue
            payload = json.dumps({"type": "tickers", "rows": self.compact_rows(),
                                  "status": self.status()})
            for q in list(self._subscribers):
                if q.full():                  # 慢客户端：丢旧帧只保最新
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

    # ---------- 读取视图 ----------

    def _live_price(self, sym: str, fallback: float) -> float:
        """自选标的用 WS 盘口中价，其余用 REST 最新成交价。"""
        b = self.book.get(sym)
        return b["mid"] if b else fallback

    def compact_rows(self) -> list[list]:
        """紧凑数组帧，字段顺序与 market.py 的 schema 消息一致。"""
        rows = []
        for sym, t in self.snapshot.items():
            p = self.premium.get(sym) or {}
            last = self._live_price(sym, t["last"])
            rows.append([
                sym, last, t["chgPct"], t["quoteVolume"], t["high"], t["low"],
                p.get("fundingRate", 0.0), p.get("markPrice", last),
            ])
        return rows

    def rich_rows(self) -> list[dict]:
        out = []
        for sym, t in self.snapshot.items():
            meta = self.meta.get(sym) or {}
            p = self.premium.get(sym) or {}
            b = self.book.get(sym)
            last = self._live_price(sym, t["last"])
            out.append({
                **t,
                "last": last,
                "restLast": t["last"],
                "base": meta.get("base", sym.removesuffix("USDT")),
                "assetClass": meta.get("assetClass", "crypto"),
                "fundingRate": p.get("fundingRate", 0.0),
                "markPrice": p.get("markPrice", last),
                "nextFundingTime": p.get("nextFundingTime", 0),
                "bid": b["bid"] if b else None,
                "ask": b["ask"] if b else None,
                "live": b is not None,
            })
        return out

    def mark_prices(self) -> dict[str, float]:
        """持仓估值：标记价 > WS 中价 > REST 成交价。"""
        out = {}
        for sym, t in self.snapshot.items():
            p = self.premium.get(sym) or {}
            out[sym] = p.get("markPrice") or self._live_price(sym, t["last"])
        for sym, b in self.book.items():        # 快照还没覆盖到的自选标的
            out.setdefault(sym, b["mid"])
        return out

    def status(self) -> dict:
        now = time.time()
        return {
            "symbols": len(self.meta),
            "snapshot": len(self.snapshot),
            "premium": len(self.premium),
            "marketWsConnected": self.market_ws_connected,
            "marketAgeSec": round(now - self.last_market_at, 1) if self.last_market_at else None,
            "restAgeSec": round(now - self.last_rest_ok, 1) if self.last_rest_ok else None,
            "restError": self.last_rest_error,
            "restFallbackCount": self.rest_fallback_used,
            "wsConnected": self.ws_connected,
            "wsSymbols": len(self.ws_symbols),
            "bookAgeSec": round(now - self.last_book_at, 1) if self.last_book_at else None,
            "subscribers": len(self._subscribers),
        }


hub = TickerHub()
