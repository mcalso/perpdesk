"""全市场行情快照中心。

两条数据通道（详见 docs/DATA_SOURCES.md）：

* **REST 轮询（权威源）** —— `premiumIndex` 5s 给标记价与资金费率，
  `ticker/24hr` 20s 给涨跌幅与成交额。全市场 WS 数组流在两个官方端点上
  分别表现为「不推数据」和「数据错误/缺失」，不能作为行情来源。
* **WS bookTicker（实时层）** —— 只订阅自选与持仓标的，提供实时买一/卖一。
  这是实测唯一稳定可靠的流。

也就是说：你关注的标的是实时的，全市场榜单 5~20 秒刷新。正确性优先于实时性 ——
标记价直接决定持仓估值，宁可慢几秒也不能错。

对前端的推送是**按需 + 增量**的：客户端用 viewport 消息声明自己此刻需要哪些
标的，只有这些标的、且只有值变了的行才会被推出去。详见 Subscriber。
"""
import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Iterable
from typing import Any

import websockets

from . import binance, config

log = logging.getLogger("perpdesk.hub")


class Subscriber:
    """一条前端连接。

    symbols 为 None 表示客户端还没上报视口，此时一行都不推 —— 首屏数据由
    REST /api/market/tickers 提供，WS 只负责让「看得见的那些行」跳动。

    为什么必须按视口推：早先这里无条件推全市场 718 行，1 秒一帧。实测
    未压缩 61.7 KB、permessage-deflate 压缩后仍有 25.6 KB，合 204 kbps，
    而部署机的上行只有约 1 Mbps —— 一个标签页就吃掉大半。改成只推可见的
    约 110 行后，端到端实测 3.1 kbps，降到 1/66。

    sent 缓存每个标的上一次发出去的那一行，值没变就不再发。行情里绝大多数
    标的在相邻两秒是完全不动的，这一步几乎白送。

    pending 是攒着还没送出去的变化，按 symbol 覆盖写入。为什么需要它：
    增量帧一旦丢失就是永久丢失。早先的实现是每个 tick 把序列化好的帧塞进
    一个 maxsize=2 的队列，满了丢最旧的一帧 —— 可那一帧里的行**已经**记进
    sent 了，于是再也不会补发，客户端上那一行就永久停在旧值。推全量快照
    时丢帧无害（下一帧什么都有），改成增量之后这个前提就没了。

    现在的模型：变化只进 pending，发送成功才清空。客户端慢时同一标的的
    多次变化被覆盖成最新值，既不丢数据，占用也被视口大小封顶。
    """

    __slots__ = ("wake", "symbols", "sent", "pending")

    def __init__(self) -> None:
        # 只作唤醒信号用，不承载数据 —— 数据在 pending 里
        self.wake = asyncio.Event()
        self.symbols: list[str] | None = None
        self.sent: dict[str, list] = {}
        self.pending: dict[str, list] = {}


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
        self._subscribers: set[Subscriber] = set()
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
            with contextlib.suppress(asyncio.CancelledError):
                await t
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
                    # 保留而非替换：拿不到的标的宁可用旧值，也别从榜单上消失。
                    # 真正下架的由 _prune_delisted() 按 exchangeInfo 剔除，
                    # 两者判据不同，不能混为一谈。
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
                self._prune_delisted()
                await asyncio.sleep(config.META_REFRESH_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("exchangeInfo refresh failed: %s", exc)
                await asyncio.sleep(60.0)

    def _prune_delisted(self) -> None:
        """把已下架的标的从快照里清掉。

        判据只认 meta —— exchangeInfo 里 status 不再是 TRADING 的才算下架。
        不能拿「本次 ticker/24hr 没返回它」当判据：那正是 _ticker_loop 里
        update() 刻意要容忍的抽风，按它删会让榜单闪烁。

        meta 为空说明 exchangeInfo 还没拉到（启动瞬间或连续失败），这时一个
        都不能删，否则整张表会被清空。

        不做这件事的后果是一行「幽灵」：premium 每轮按 meta 重建会剔除它，
        snapshot 却因为 update() 只进不出而永久保留，于是那一行冻结在最后
        一口价上永不再变，资金费率显示 0，还会进 mark_prices() 污染持仓估值。
        """
        if not self.meta:
            return
        gone = self.snapshot.keys() - self.meta.keys()
        if not gone:
            return
        for sym in gone:
            self.snapshot.pop(sym, None)
            self.book.pop(sym, None)
        # 重算实时订阅：下架的标的若还挂在自选里，会一直占着一个流名额
        self.set_ws_symbols(self.ws_symbols)
        log.info("已下架，移出快照: %s", ", ".join(sorted(gone)))

    # ---------- WS：自选标的实时盘口 ----------

    def set_ws_symbols(self, symbols: list[str]) -> None:
        """设定要实时订阅的标的（自选 ∪ 持仓），变化时触发重新订阅。

        持仓标的必须包含在内：持仓估值是这个站最需要准确且及时的数字，
        而全市场 REST 轮询只有 5~20 秒粒度。
        """
        wanted = sorted({s.upper() for s in symbols if s})
        # 滤掉 exchangeInfo 里已经没有的：自选表里留着一个已下架的标的时，
        # 继续向币安订阅它只会白占 WS_MAX_STREAMS 的名额（对方也不会推）。
        # meta 为空说明 exchangeInfo 还没拉到，那时一个都不能滤，否则会
        # 把所有订阅都清掉。
        if self.meta:
            wanted = [s for s in wanted if s in self.meta]
        wanted = wanted[: config.WS_MAX_STREAMS]
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
                        except TimeoutError:
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

    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)

    def set_viewport(self, sub: Subscriber, symbols: Iterable[Any]) -> None:
        """客户端声明它此刻需要哪些标的。重复与空值忽略，超出上限直接截断。

        截断是有意的：上限存在的意义就是不让任何客户端把整个市场要过去。
        """
        wanted: list[str] = []
        seen: set[str] = set()
        for raw in symbols:
            # 只认字符串：JSON 里的 null / 数字经 str() 会变成 "NONE" / "3"
            # 这种看着像 symbol 的垃圾，白占视口名额
            if not isinstance(raw, str):
                continue
            sym = raw.strip().upper()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            wanted.append(sym)
            if len(wanted) >= config.VIEWPORT_MAX:
                log.warning("viewport 超过上限 %d，已截断", config.VIEWPORT_MAX)
                break
        sub.symbols = wanted
        # 移出视口的标的要从 sent 里清掉。否则它再次进入视口时，会因为
        # 「值和上次发的一样」被增量逻辑跳过，那一行就再也不更新了。
        for stale in sub.sent.keys() - seen:
            sub.sent.pop(stale, None)
        # pending 同理：已经不在视口里的行没必要再发过去
        for stale in sub.pending.keys() - seen:
            sub.pending.pop(stale, None)

    def stage(self, sub: Subscriber) -> bool:
        """把这一刻的变化并入待发集合，返回「有东西要发吗」。

        只写 pending，不做序列化也不碰传输层 —— 发不发得出去是 drain()
        那一侧的事。同一标的重复变化在这里被覆盖成最新值。
        """
        if not sub.symbols:
            return False                  # 客户端还没上报视口，一行都不推
        for row in self.compact_rows(sub.symbols):
            if sub.sent.get(row[0]) == row:
                continue                  # 这一秒没动，不必重发
            sub.sent[row[0]] = row
            sub.pending[row[0]] = row
        return bool(sub.pending)

    def drain(self, sub: Subscriber) -> str | None:
        """取走待发的行并序列化成一帧；没有就返回 None。

        ⚠️ 调用方必须真的把返回的帧发出去 —— 这里一取就清空了。
        发送失败意味着连接已断，订阅者随即被移除，所以不必回滚。
        """
        if not sub.pending:
            return None
        rows = list(sub.pending.values())
        sub.pending.clear()
        return json.dumps({"type": "tickers", "rows": rows})

    async def _broadcast_loop(self) -> None:
        while True:
            await asyncio.sleep(config.BROADCAST_INTERVAL)
            for sub in list(self._subscribers):
                if self.stage(sub):
                    sub.wake.set()        # 真正的发送在各连接自己的 pump 里

    # ---------- 读取视图 ----------

    def _live_price(self, sym: str, fallback: float) -> float:
        """自选标的用 WS 盘口中价，其余用 REST 最新成交价。"""
        b = self.book.get(sym)
        return b["mid"] if b else fallback

    def compact_rows(self, symbols: Iterable[str] | None = None) -> list[list]:
        """紧凑数组帧，字段顺序与 market.py 的 FRAME_FIELDS 一致。

        symbols 为 None 时返回全市场；给定时只返回其中存在的标的
        （客户端可能要到已下架或拼错的 symbol，静默跳过即可）。
        """
        rows = []
        for sym in (self.snapshot.keys() if symbols is None else symbols):
            t = self.snapshot.get(sym)
            if t is None:
                continue
            p = self.premium.get(sym) or {}
            last = self._live_price(sym, t["last"])
            rows.append([
                sym, last, t["chgPct"], p.get("markPrice", last),
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
                "sector": meta.get("sector", "other"),
                "onboardDate": meta.get("onboardDate", 0),
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
