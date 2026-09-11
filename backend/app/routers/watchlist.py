"""自选列表：增删排序，并直接带出当前行情，前端一次请求就能渲染。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from .. import db
from ..hub import hub

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class SymbolIn(BaseModel):
    symbol: str

    @field_validator("symbol")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.strip().upper()


class OrderIn(BaseModel):
    symbols: list[str]


def _sync_ws() -> list[str]:
    """自选变更后重新汇总实时订阅（自选 ∪ 持仓）。"""
    symbols = db.list_watchlist()
    if hub.on_watchlist_change:
        hub.on_watchlist_change(None)
    else:
        hub.set_ws_symbols(symbols)
    return symbols


@router.get("")
async def get_watchlist() -> dict:
    symbols = _sync_ws()
    by_symbol = {r["symbol"]: r for r in hub.rich_rows()}
    rows = []
    for s in symbols:
        row = by_symbol.get(s)
        rows.append(row or {"symbol": s, "last": 0, "chgPct": 0, "quoteVolume": 0,
                            "stale": True})
    return {"rows": rows}


@router.post("")
async def add(body: SymbolIn) -> dict:
    if body.symbol not in hub.meta:
        raise HTTPException(404, f"{body.symbol} 不是正在交易的 USDT 永续合约")
    db.add_watch(body.symbol)
    return {"ok": True, "symbols": _sync_ws()}


@router.delete("/{symbol}")
async def remove(symbol: str) -> dict:
    db.remove_watch(symbol.upper())
    return {"ok": True, "symbols": _sync_ws()}


@router.put("/order")
async def reorder(body: OrderIn) -> dict:
    db.reorder_watchlist([s.upper() for s in body.symbols])
    return {"ok": True, "symbols": _sync_ws()}
