"""行情接口：全市场快照、K 线代理、实时推送。"""
import asyncio
import json

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from .. import binance, icons
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
) -> dict:
    if sort not in SORT_KEYS:
        raise HTTPException(400, f"sort must be one of {sorted(SORT_KEYS)}")
    rows = hub.rich_rows()
    if search:
        needle = search.upper()
        rows = [r for r in rows if needle in r["symbol"]]
    rows.sort(key=SORT_KEYS[sort], reverse=desc)
    return {"rows": rows[:limit], "total": len(rows), "status": hub.status()}


@router.get("/icon/{symbol}")
async def icon(symbol: str) -> Response:
    """标的图标。后端代理 + 落盘缓存，查不到的返回首字母占位图。"""
    meta = hub.meta.get(symbol.upper()) or {}
    data, media = await icons.get(symbol, meta.get("base", ""))
    return Response(
        content=data, media_type=media,
        # 图标基本不变，让浏览器缓存 7 天，翻页不再回源
        headers={"Cache-Control": "public, max-age=604800"},
    )


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
    """紧凑数组帧，1s 一推。字段顺序见 hub.compact_rows()。"""
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
