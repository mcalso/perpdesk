"""行情接口：全市场快照、K 线代理、实时推送。"""
import asyncio
import json

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from .. import binance, icons
from .. import auth
from ..hub import hub

router = APIRouter(prefix="/api/market", tags=["market"])

# 紧凑帧的字段顺序，与 hub.compact_rows() 一一对应。前端 lib/ws.ts 的
# decode() 按这个顺序解包，三处必须同时改。
FRAME_FIELDS = ["symbol", "last", "chgPct", "quoteVolume", "high", "low",
                "fundingRate", "markPrice"]

SORT_KEYS = {
    "chgPct": lambda r: r["chgPct"],
    "quoteVolume": lambda r: r["quoteVolume"],
    "fundingRate": lambda r: r["fundingRate"],
    "symbol": lambda r: r["symbol"],
    "last": lambda r: r["last"],
}


@router.get("/status")
async def status() -> dict:
    return hub.status()


@router.get("/symbols")
async def symbols() -> list[dict]:
    return sorted(hub.meta.values(), key=lambda m: m["symbol"])


@router.get("/tickers")
async def tickers(
    sort: str = Query("quoteVolume", description="排序字段"),
    desc: bool = Query(True),
    limit: int = Query(500, ge=1, le=1000),
    search: str = Query("", description="按 symbol 模糊过滤"),
    slim: bool = Query(True, description="只返回看板用得到的字段"),
) -> dict:
    if sort not in SORT_KEYS:
        raise HTTPException(400, f"sort must be one of {sorted(SORT_KEYS)}")
    rows = hub.rich_rows()
    if search:
        needle = search.upper()
        rows = [r for r in rows if needle in r["symbol"]]
    total = len(rows)
    rows.sort(key=SORT_KEYS[sort], reverse=desc)
    rows = rows[:limit]
    if slim:
        # 看板只用得上这几个字段；全量字段 718 行约 240KB，瘦身后不到三分之一
        # sector 只走这条 REST（30s 一刷），刻意不进 WS 帧：
        # 板块是静态属性，每秒重复推它纯属浪费刚省下来的带宽
        keep = ("symbol", "base", "assetClass", "sector", "last", "chgPct",
                "quoteVolume", "fundingRate", "markPrice", "live")
        rows = [{k: r[k] for k in keep if k in r} for r in rows]
    return {"rows": rows, "total": total, "status": hub.status()}


@router.get("/icon/{symbol}")
async def icon(symbol: str) -> Response:
    """标的图标。后端代理 + 落盘缓存，查不到的返回首字母占位图。"""
    meta = hub.meta.get(symbol.upper()) or {}
    data, media = await icons.get(symbol, meta.get("base", ""))
    # 占位图只缓存 10 分钟：它可能在后续解析中变成真实 logo，
    # 若按真实 logo 那样缓存 7 天，浏览器会长期显示过时的占位图。
    placeholder = icons.PLACEHOLDER_MARK in data[:400]
    ttl = 600 if placeholder else 604800
    return Response(
        content=data, media_type=media,
        headers={"Cache-Control": f"public, max-age={ttl}"},
    )


@router.get("/chart-source/{symbol}")
async def chart_source(symbol: str) -> dict:
    """该标的用哪种图表渲染，以及它在 TradingView 上的符号名。

    中文 symbol 在 TradingView 上是拼音名（牛来USDT → NIULAIUSDT.P），
    不能直接拼 `BINANCE:{symbol}.P`，要按 base asset 反查。
    查不到的才回退内置图表。
    """
    symbol = symbol.upper()
    base = (hub.meta.get(symbol) or {}).get("base", "")
    try:
        info = await icons.chart_symbol(symbol, base)
    except Exception:
        info = {"tv": symbol.isascii(), "tvSymbol": f"{symbol}.P"}
    tv = bool(info.get("tv"))
    name = info.get("tvSymbol") or f"{symbol}.P"
    return {
        "symbol": symbol,
        "tradingview": tv,
        "tvSymbol": f"BINANCE:{name}" if tv else None,
        "reason": "" if tv else "TradingView 没有该合约，使用内置图表",
    }


@router.get("/icon-stats")
async def icon_stats() -> dict:
    return icons.stats()


@router.get("/klines")
async def klines(
    symbol: str,
    interval: str = Query("1h"),
    limit: int = Query(300, ge=1, le=1500),
) -> list[dict]:
    try:
        return await binance.klines(symbol, interval, limit)
    except Exception as exc:
        raise HTTPException(502, f"binance klines failed: {exc}") from exc


@router.websocket("/ws")
async def ws_tickers(ws: WebSocket) -> None:
    """按需增量推送。字段顺序见 FRAME_FIELDS。

    协议：连上先收一条 schema，然后客户端**必须**发 viewport 声明自己需要
    哪些标的，服务端才会开始推 —— 在那之前一行都不推。首屏数据由
    REST /api/market/tickers 提供，所以这个等待期对用户是无感的。

        客户端 → {"type": "viewport", "symbols": ["BTCUSDT", ...]}
        服务端 → {"type": "tickers", "rows": [[...], ...]}      只含变化的行

    rows 是增量：值没变的标的不会出现在帧里，客户端把它并进自己的 Map 即可。

    帧里不带 status：前端没有任何地方消费它，而它每秒都在变，反倒会
    抵消跨帧压缩字典的效果 —— 增量帧本身往往只有几百字节，塞一份
    status 进去就翻倍了。站点状态由 App.tsx 每 10s 轮询 /api/health。

    鉴权必须在这里单独做：HTTP 中间件管不到 WebSocket 握手，
    只靠中间件的话这条流就是整站唯一一个不需要登录的数据出口。
    """
    if not auth.validate(ws.cookies.get(auth.COOKIE_NAME)):
        await ws.close(code=1008, reason="未登录")   # 1008 = policy violation
        return
    await ws.accept()
    sub = hub.subscribe()

    async def pump() -> None:
        while True:
            await ws.send_text(await sub.queue.get())

    async def listen() -> None:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue                  # 坏帧不该掀翻整条连接
            if isinstance(msg, dict) and msg.get("type") == "viewport":
                syms = msg.get("symbols")
                hub.set_viewport(sub, syms if isinstance(syms, list) else [])

    tasks: list[asyncio.Task] = []
    try:
        await ws.send_text(json.dumps({"type": "schema", "fields": FRAME_FIELDS}))
        tasks = [asyncio.create_task(pump()), asyncio.create_task(listen())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        pass
    finally:
        for t in tasks:
            t.cancel()
        # 必须把子任务收干净：不 gather 的话，pump/listen 里断连抛出的异常
        # 无人取走，asyncio 会在 GC 时打一条 "exception was never retrieved"。
        await asyncio.gather(*tasks, return_exceptions=True)
        hub.unsubscribe(sub)
