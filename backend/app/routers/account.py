"""账户只读接口 + 把交易所成交同步进本地流水。"""
import time

from fastapi import APIRouter, HTTPException, Query

from .. import account, db

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("/status")
async def status() -> dict:
    key, secret = account.credentials()
    snap = account.cache.snapshot()
    return {
        "configured": account.configured(),
        "hasKey": bool(key),
        "hasSecret": bool(secret),
        "ageSec": snap["ageSec"],
        "error": snap["error"],
        "hint": "" if account.configured()
                else "在 backend/.env 填入 BINANCE_API_SECRET 后即可读取真实持仓",
    }


def _guard() -> None:
    if not account.configured():
        raise HTTPException(
            412, "尚未配置 Binance API secret，请在 backend/.env 中填写 BINANCE_API_SECRET"
        )


@router.get("/overview")
async def overview() -> dict:
    """读后端集中维护的账户快照，不直接打 Binance（见 account.AccountCache）。"""
    _guard()
    snap = account.cache.snapshot()
    if snap["ageSec"] is None and snap["error"]:
        raise HTTPException(502, snap["error"])
    return snap


@router.post("/sync-trades")
async def sync_trades(
    symbols: str = Query("", description="逗号分隔；留空则同步当前持仓的标的"),
    days: int = Query(30, ge=1, le=365),
) -> dict:
    """把交易所成交明细拉进本地 trades 表，按 (symbol, tradeId) 去重。"""
    _guard()
    start_ms = int((time.time() - days * 86400) * 1000)

    wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not wanted:
        try:
            wanted = [p["symbol"] for p in await account.positions()]
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc
    if not wanted:
        return {"inserted": 0, "skipped": 0, "symbols": [], "note": "当前没有持仓，请显式指定 symbols"}

    existing = {
        (t["symbol"], t["note"]) for t in db.list_trades() if t["note"].startswith("binance:")
    }
    rows, skipped = [], 0
    for sym in wanted:
        try:
            fills = await account.user_trades_range(sym, start_ms)
        except Exception as exc:
            raise HTTPException(502, f"{sym}: {exc}") from exc
        for f in fills:
            tag = f"binance:{f['tradeId']}"
            if (f["symbol"], tag) in existing:
                skipped += 1
                continue
            rows.append((f["symbol"], f["side"], f["qty"], f["price"], f["fee"],
                         f["traded_at"], tag))

    inserted = db.add_trades_bulk(rows) if rows else 0
    return {"inserted": inserted, "skipped": skipped, "symbols": wanted, "days": days}
