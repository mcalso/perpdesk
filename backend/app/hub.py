"""全市场行情快照中心。

两条数据通道（详见 docs/DATA_SOURCES.md）：

* **REST 轮询（权威源）** —— `premiumIndex` 5s 给标记价与资金费率，
  `ticker/24hr` 20s 给涨跌幅与成交额。全市场 WS 数组流在两个官方端点上
  分别表现为「不推数据」和「数据错误/缺失」，不能作为行情来源。
* **WS bookTicker（实时层）** —— 只订阅自选与持仓标的，提供实时买一/卖一。
  这是实测唯一稳定可靠的流。

也就是说：你关注的标的是实时的，全市场榜单 5~20 秒刷新。正确性优先于实时性 ——
标记价直接决定持仓估值，宁可慢几秒也不能错。
"""
import asyncio
import json
import logging
import time
from typing import Any

import websockets

from . import binance, config

log = logging.getLogger("perpdesk.hub")


class TickerHub:
    def __init__(self) -> None:
        self.snapshot: dict[str, dict] = {}      # symbol -> 24h 行情
        self.premium: dict[str, dict] = {}       # symbol -> 标记价/资金费率
        self.book: dict[str, dict] = {}          # symbol -> 实时买一卖一（仅自选）
        self.meta: dict[str, dict] = {}
        self.last_premium_at = 0.0
        self.ws_connected = False
        self.ws_symbols: list[str] = []
        self.last_rest_ok = 0.0
        self.last_rest_error = ""
        self.last_book_at = 0.0
        # 由 main 注入：自选变化时重新汇总「自选 ∪ 持仓」
        self.on_watchlist_change: Any = None
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
            asyncio.create_task(self._premium_loop(), name="hub-premium"),
            asyncio.create_task(self._ticker_loop(), name="hub-ticker"),
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

    # ---------- REST 轮询：全市场行情（权威源） ----------

    async def _premium_loop(self) -> None:
        """标记价 + 资金费率。持仓估值依赖它，所以刷得比 24h 行情勤。"""
        delay = config.PREMIUM_POLL_INTERVAL
        while True:
            try:
                rows = await binance.premium_index(retries=1)
                if rows:
                    self.premium = {k: v for k, v in rows.items()
                                    if not self.meta or k in self.meta}
                    self.last_premium_at = time.time()
                    self.last_rest_error = ""
                    delay = config.PREMIUM_POLL_INTERVAL
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_rest_error = str(exc)[:120]
                delay = min(delay * 1.6, config.REST_POLL_MAX_INTERVAL)
                log.warning("premiumIndex poll failed (%s), next in %.0fs", exc, delay)
            await asyncio.sleep(delay)

    async def _ticker_loop(self) -> None:
        """24h 涨跌幅与成交额。变化慢，20s 足够，权重也更贵（40）。"""
        delay = config.TICKER_POLL_INTERVAL
        while True:
            try:
                rows = await binance.tickers_24h(retries=1)
                if rows:
                    # 保留而非替换：拿不到的标的宁可用旧值，也别从榜单上消失
                    self.snapshot.update({k: v for k, v in rows.items()
                                          if not self.meta or k in self.meta})
                    self.last_rest_ok = time.time()
                    delay = config.TICKER_POLL_INTERVAL
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_rest_error = str(exc)[:120]
                delay = min(delay * 1.6, config.REST_POLL_MAX_INTERVAL)
                log.warning("ticker/24hr poll failed (%s), next in %.0fs", exc, delay)
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
        """设定要实时订阅的标的（自选 ∪ 持仓），变化时触发重新订阅。

        持仓标的必须包含在内：持仓估值是这个站最需要准确且及时的数字，
        而全市场 REST 轮询只有 5~20 秒粒度。
        """
        wanted = sorted({s.upper() for s in symbols if s})[: config.WS_MAX_STREAMS]
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
            "premiumAgeSec": round(now - self.last_premium_at, 1) if self.last_premium_at else None,
            "restAgeSec": round(now - self.last_rest_ok, 1) if self.last_rest_ok else None,
            "restError": self.last_rest_error,
            "wsConnected": self.ws_connected,
            "wsSymbols": len(self.ws_symbols),
            "bookAgeSec": round(now - self.last_book_at, 1) if self.last_book_at else None,
            "subscribers": len(self._subscribers),
        }


hub = TickerHub()
