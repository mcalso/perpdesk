"""全市场行情快照中心。

数据源按实测网络能力选定（见 docs/DATA_SOURCES.md）：

* 全市场 24h 行情 / 资金费率 —— REST 低频轮询。部分网络环境下 Binance 合约的
  `!ticker@arr` / `!miniTicker@arr` / `!markPrice@arr` 等全市场 WS 流会全部静默，
  只能退回 REST；出口 IP 若与其他程序共享权重还会间歇 418，因此失败时
  保留上一份快照并指数退避，绝不把空数据推给前端。
  网络条件好的机器（如境外云服务器）可以直接改回 WS 全市场流，见 docs/DATA_SOURCES.md。
* 自选标的实时价 —— WS `bookTicker` 合并流。受限网络下这是唯一能稳定出数据的流，
  给的是实时买一/卖一，够自选列表跳动用。
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
        self.ws_connected = False
        self.ws_symbols: list[str] = []
        self.last_rest_ok = 0.0
        self.last_rest_error = ""
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
        self._tasks = [
            asyncio.create_task(self._rest_loop(), name="hub-rest"),
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

    # ---------- REST 轮询：全市场 24h 行情 + 资金费率 ----------

    async def _rest_loop(self) -> None:
        delay = config.REST_POLL_INTERVAL
        while True:
            try:
                rows = await binance.tickers_24h(retries=1)
                if rows:
                    self.snapshot = {s: t for s, t in rows.items()
                                     if not self.meta or s in self.meta}
                    self.last_rest_ok = time.time()
                    self.last_rest_error = ""
                    delay = config.REST_POLL_INTERVAL
            except Exception as exc:
                self.last_rest_error = str(exc)[:120]
                delay = min(delay * 1.6, config.REST_POLL_MAX_INTERVAL)
                log.warning("ticker/24hr poll failed (%s), next in %.0fs", exc, delay)

            try:
                self.premium = await binance.premium_index(retries=1)
            except Exception as exc:
                log.debug("premiumIndex poll failed: %s", exc)

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
            "restAgeSec": round(now - self.last_rest_ok, 1) if self.last_rest_ok else None,
            "restError": self.last_rest_error,
            "wsConnected": self.ws_connected,
            "wsSymbols": len(self.ws_symbols),
            "bookAgeSec": round(now - self.last_book_at, 1) if self.last_book_at else None,
            "subscribers": len(self._subscribers),
        }


hub = TickerHub()
