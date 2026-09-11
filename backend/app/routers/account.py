"""账户只读接口 + 把交易所成交同步进本地流水。"""
import asyncio
import time

from fastapi import APIRouter, HTTPException, Query

from .. import account, db
from ..hub import hub

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
    # 注入行情中心的实时标记价，让浮盈按 1 秒级行情走
    snap = account.cache.snapshot(hub.mark_prices())
    if snap["ageSec"] is None and snap["error"]:
        raise HTTPException(502, snap["error"])
    return snap


@router.post("/sync-trades")
async def sync_trades(
    symbols: str = Query("", description="逗号分隔；留空则自动覆盖所有有交易记录的标的"),
    days: int = Query(90, ge=1, le=365),
) -> dict:
    """把交易所的成交明细与资金流水同步到本地。

    两件事都做，缺一不可：

    * **成交明细** —— 决定持仓与已实现盈亏。注意不能只同步「当前持仓」的标的，
      否则已平仓标的的盈亏（往往正是亏损的那些）会全部丢失，统计出来的
      总盈亏会严重偏向乐观。这里用资金流水反查出所有交易过的标的。
    * **资金流水** —— 资金费不出现在成交记录里，但对长期/高杠杆持仓是实打实的
      损益项，必须单独计。

    为控制权重：userTrades 每标的每 7 天一次请求（权重 5），90 天 × 30 标的可达
    2000+ 权重，逼近 2400/分钟 的上限。因此用资金流水的时间范围把每个已平仓
    标的的查询窗口收窄，并对请求做节流。
    """
    _guard()
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 86400 * 1000

    # 1) 资金流水（顺带得到"哪些标的有过交易"）
    try:
        inc = await account.income_range(start_ms)
    except Exception as exc:
        raise HTTPException(502, f"income: {exc}") from exc
    income_new = db.upsert_income([
        (r["tranId"], r["symbol"], r["type"], r["amount"], r["asset"], r["t"])
        for r in inc
    ]) if inc else 0

    # 2) 待同步标的。fromId 分页会遍历该标的全部历史，不必再算时间窗口。
    if symbols:
        wanted = {x.strip().upper() for x in symbols.split(",") if x.strip()}
    else:
        wanted = {r["symbol"] for r in inc if r["symbol"]}
        try:
            wanted |= {p["symbol"] for p in await account.positions()}
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc

    if not wanted:
        return {"inserted": 0, "skipped": 0, "incomeInserted": income_new,
                "symbols": [], "note": "这段时间内没有任何交易记录"}

    # 3) 成交明细（按 tradeId 去重，note 里存 binance:<tradeId>）
    existing = {
        t["note"] for t in db.list_trades() if t["note"].startswith("binance:")
    }
    rows, skipped, failed = [], 0, []
    for i, sym in enumerate(sorted(wanted)):
        try:
            fills = await account.user_trades_all(sym, start_ms)
        except Exception as exc:
            failed.append({"symbol": sym, "error": str(exc)[:120]})
            continue
        for f in fills:
            tag = f"binance:{f['tradeId']}"
            if tag in existing:
                skipped += 1
                continue
            existing.add(tag)
            rows.append((f["symbol"], f["side"], f["qty"], f["price"], f["fee"],
                         f["traded_at"], tag))
        if i % 8 == 7:                 # 节流，避免瞬时打满权重
            await asyncio.sleep(1.0)

    inserted = db.add_trades_bulk(rows) if rows else 0
    return {
        "inserted": inserted,
        "skipped": skipped,
        "incomeInserted": income_new,
        "symbols": sorted(wanted),
        "symbolCount": len(wanted),
        "days": days,
        "failed": failed[:10],
    }
