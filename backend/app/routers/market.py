"""行情接口：全市场快照、K 线代理、实时推送。"""
import asyncio
import json

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from .. import binance, icons
from .. import auth
from ..hub import hub

router = APIRouter(prefix="/api/market", tags=["market"])

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
        keep = ("symbol", "base", "assetClass", "last", "chgPct",
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
    """紧凑数组帧，1s 一推。字段顺序见 hub.compact_rows()。

    鉴权必须在这里单独做：HTTP 中间件管不到 WebSocket 握手，
    只靠中间件的话这条流就是整站唯一一个不需要登录的数据出口。
    """
    if not auth.validate(ws.cookies.get(auth.COOKIE_NAME)):
        await ws.close(code=1008, reason="未登录")   # 1008 = policy violation
        return
    await ws.accept()
    q = hub.subscribe()
    try:
        await ws.send_text(json.dumps({
            "type": "schema",
            "fields": ["symbol", "last", "chgPct", "quoteVolume", "high", "low",
                       "fundingRate", "markPrice"],
        }))
        await ws.send_text(json.dumps({"type": "tickers", "rows": hub.compact_rows()}))
        while True:
            payload = await q.get()
            await ws.send_text(payload)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        pass
    finally:
        hub.unsubscribe(q)
