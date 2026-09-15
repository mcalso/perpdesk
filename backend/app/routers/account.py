"""账户只读接口 + 把交易所成交同步进本地流水。"""
import asyncio
import time

from fastapi import APIRouter, HTTPException, Query

from .. import account, db, vault
from ..hub import hub

router = APIRouter(prefix="/api/account", tags=["account"])

# 所有接口都接受 ?account=<id>，省略时落到默认账户。
# 校验放在一处：账户不存在要 404，不能静默落到默认账户去读写别人的数据。
AccountQ = Query(None, description="账户 id，省略则用默认账户")


def _resolve(account_id: int | None) -> int:
    if account_id is None:
        return db.default_account_id()
    if db.get_account(account_id) is None:
        raise HTTPException(404, f"账户 {account_id} 不存在")
    return account_id


def _guard(account_id: int) -> None:
    if not account.configured(account_id):
        raise HTTPException(
            412, f"账户 {account_id} 尚未配置 API 凭据（见 docs/SECURITY.md）")


@router.get("/accounts")
async def accounts() -> dict:
    """账户列表。凭据只给掩码，永远不返回明文。"""
    rows = []
    for a in db.list_accounts():
        rows.append({
            **a,
            "configured": account.configured(a["id"]),
            "credentials": vault.status(a["id"]),
            "trades": db.trades_version(a["id"])[0],
        })
    return {"rows": rows, "defaultId": db.default_account_id()}


@router.get("/status")
async def status(account_id: int | None = AccountQ) -> dict:
    acct = _resolve(account_id)
    key, secret = account.credentials(acct)
    snap = account.registry.get(acct).snapshot()
    return {
        "accountId": acct,
        "configured": account.configured(acct),
        "hasKey": bool(key),
        "hasSecret": bool(secret),
        "ageSec": snap["ageSec"],
        "error": snap["error"],
        "hint": "" if account.configured(acct)
                else "在 backend/.env 填入 BINANCE_API_SECRET 后即可读取真实持仓",
    }


@router.get("/overview")
async def overview(account_id: int | None = AccountQ) -> dict:
    """读后端集中维护的账户快照，不直接打 Binance（见 account.AccountCache）。"""
    acct = _resolve(account_id)
    _guard(acct)
    # 注入行情中心的实时标记价，让浮盈按 1 秒级行情走
    snap = account.registry.get(acct).snapshot(hub.mark_prices())
    if snap["ageSec"] is None and snap["error"]:
        raise HTTPException(502, snap["error"])
    return snap


@router.post("/sync-trades")
async def sync_trades(
    symbols: str = Query("", description="逗号分隔；留空则自动覆盖所有有交易记录的标的"),
    days: int = Query(90, ge=1, le=365),
    account_id: int | None = AccountQ,
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
    acct = _resolve(account_id)
    _guard(acct)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 86400 * 1000

    # 1) 资金流水（顺带得到"哪些标的有过交易"）
    try:
        inc = await account.income_range(start_ms, account_id=acct)
    except Exception as exc:
        raise HTTPException(502, f"income: {exc}") from exc
    income_new = db.upsert_income([
        (r["tranId"], r["symbol"], r["type"], r["amount"], r["asset"], r["t"])
        for r in inc
    ], account_id=acct) if inc else 0

    # 2) 待同步标的。fromId 分页会遍历该标的全部历史，不必再算时间窗口。
    if symbols:
        wanted = {x.strip().upper() for x in symbols.split(",") if x.strip()}
    else:
        wanted = {r["symbol"] for r in inc if r["symbol"]}
        try:
            wanted |= {p["symbol"] for p in await account.positions(acct)}
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc

    if not wanted:
        return {"inserted": 0, "skipped": 0, "incomeInserted": income_new,
                "symbols": [], "note": "这段时间内没有任何交易记录"}

    # 3) 成交明细（按 tradeId 去重，note 里存 binance:<tradeId>）
    existing = db.existing_trade_notes(acct)
    rows, backfill, skipped, failed = [], [], 0, []
    for i, sym in enumerate(sorted(wanted)):
        try:
            fills = await account.user_trades_all(sym, start_ms, account_id=acct)
        except Exception as exc:
            failed.append({"symbol": sym, "error": str(exc)[:120]})
            continue
        for f in fills:
            tag = f"binance:{f['tradeId']}"
            if tag in existing:
                skipped += 1
                # 已存在的也要回填盈亏字段：早期同步没存，不补就会造成
                # 同一标的一部分用交易所口径、一部分用本地回放的混合结果
                backfill.append((f["realizedPnl"], tag))
                continue
            existing.add(tag)
            rows.append((f["symbol"], f["side"], f["qty"], f["price"], f["fee"],
                         f["traded_at"], tag, f["realizedPnl"]))
        if i % 8 == 7:                 # 节流，避免瞬时打满权重
            await asyncio.sleep(1.0)

    inserted = db.add_trades_bulk(rows, account_id=acct) if rows else 0
    filled = db.backfill_realized_pnl(backfill, account_id=acct) if backfill else 0
    return {
        "accountId": acct,
        "inserted": inserted,
        "backfilled": filled,
        "skipped": skipped,
        "incomeInserted": income_new,
        "symbols": sorted(wanted),
        "symbolCount": len(wanted),
        "days": days,
        "failed": failed[:10],
    }


@router.post("/import-history")
async def import_history(
    days: int = Query(365, ge=1, le=365, description="往前追溯多少天（交易所限制单次最多 365 天）"),
    wait: int = Query(180, ge=10, le=600, description="最多等待导出生成的秒数"),
    account_id: int | None = AccountQ,
) -> dict:
    """用异步导出补全历史成交。

    `sync-trades` 只能覆盖 userTrades 的保留期，更早的成交必须走这个接口。
    交易所每月仅允许 5 次导出申请，所以它是手动触发的，不要做成定时任务。
    """
    acct = _resolve(account_id)
    _guard(acct)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 86400 * 1000

    try:
        download_id = await account.request_trade_export(start_ms, now_ms, account_id=acct)
    except Exception as exc:
        raise HTTPException(502, f"申请导出失败（每月限 5 次）：{exc}") from exc
    if not download_id:
        raise HTTPException(502, "交易所未返回 downloadId")

    url = None
    deadline = time.time() + wait
    while time.time() < deadline:
        await asyncio.sleep(6)
        try:
            url = await account.get_export_url(download_id, account_id=acct)
        except Exception as exc:
            raise HTTPException(502, f"查询导出状态失败：{exc}") from exc
        if url:
            break
    if not url:
        return {"ok": False, "downloadId": download_id,
                "note": f"导出仍在生成，稍后用同一个 downloadId 重试即可（不消耗新的配额）"}

    raw = await account.download_export(url)
    fills = account.parse_trade_export(raw)
    existing = db.existing_trade_notes(acct)
    rows, skipped = [], 0
    for f in fills:
        tag = f"binance:{f['tradeId']}"
        if tag in existing:
            skipped += 1
            continue
        existing.add(tag)
        rows.append((f["symbol"], f["side"], f["qty"], f["price"], f["fee"],
                     f["traded_at"], tag, f["realizedPnl"]))
    inserted = db.add_trades_bulk(rows, account_id=acct) if rows else 0
    span = (fills[0]["traded_at"], fills[-1]["traded_at"]) if fills else (None, None)
    return {
        "ok": True,
        "accountId": acct,
        "parsed": len(fills),
        "inserted": inserted,
        "skipped": skipped,
        "symbols": len({f["symbol"] for f in fills}),
        "from": span[0],
        "to": span[1],
        "hedgeMode": any(f["positionSide"] in ("LONG", "SHORT") for f in fills),
    }
