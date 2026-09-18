"""账户只读接口 + 把交易所成交同步进本地流水。"""
import asyncio
import sqlite3
import time

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, field_validator

from .. import account, config, db, fx, vault
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


class AccountIn(BaseModel):
    label: str
    exchange: str = "binance"
    market: str = "usdm"
    sort_order: int = 0

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        v = v.strip()
        if not 1 <= len(v) <= 40:
            raise ValueError("名称需要 1-40 个字符")
        return v


class AccountPatch(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    sort_order: int | None = None


class CredentialsIn(BaseModel):
    api_key: str
    api_secret: str
    passphrase: str | None = None      # OKX 之类需要，币安不用


def _require_secure(request: Request) -> None:
    """凭据写入必须走 HTTPS。

    明文 HTTP 下提交 API key，密钥就是明文过网线的 —— 这跟把它贴在公告板上
    没多大区别。所以这里直接拒绝，而不是提示一下就放行。

    本机访问放行：命令行/本地调试时没有中间链路可窃听，且首次配置往往
    正是在还没有证书的时候做的。
    """
    if config.ALLOW_INSECURE_CREDENTIALS:
        return
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = (request.client.host if request.client else "") or ""
    if proto == "https" or host in ("127.0.0.1", "::1", "localhost"):
        return
    raise HTTPException(
        421,
        "拒绝在明文 HTTP 上接收 API 凭据：密钥会明文经过网络。"
        "请先配置 HTTPS（见 docs/SECURITY.md），"
        "或临时用命令行写入 backend/.env。",
    )


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


@router.post("/accounts", status_code=201)
async def create_account(body: AccountIn) -> dict:
    acct = db.add_account(body.exchange, body.label, body.market, body.sort_order)
    await account.registry.sync()        # 让新账户立刻开始轮询
    return {"id": acct, "label": body.label}


@router.patch("/accounts/{acct}")
async def patch_account(acct: int, body: AccountPatch) -> dict:
    if db.get_account(acct) is None:
        raise HTTPException(404, f"账户 {acct} 不存在")
    changed = db.update_account(
        acct, label=body.label, sort_order=body.sort_order,
        enabled=None if body.enabled is None else int(body.enabled))
    await account.registry.sync()        # 停用的要停掉轮询，启用的要拉起来
    return {"ok": changed, "account": db.get_account(acct)}


@router.get("/accounts/{acct}/usage")
async def account_usage(acct: int) -> dict:
    if db.get_account(acct) is None:
        raise HTTPException(404, f"账户 {acct} 不存在")
    return db.account_usage(acct)


@router.delete("/accounts/{acct}")
async def remove_account(acct: int) -> dict:
    """删账户。账户下还有成交时会被外键挡住 —— 那是有意的。"""
    if db.get_account(acct) is None:
        raise HTTPException(404, f"账户 {acct} 不存在")
    if len(db.list_accounts()) <= 1:
        raise HTTPException(409, "这是最后一个账户，删掉之后成交流水就没有归属了")
    usage = db.account_usage(acct)
    if usage["trades"] or usage["income"]:
        raise HTTPException(
            409,
            f"该账户下还有 {usage['trades']} 条成交、{usage['income']} 条流水。"
            "删掉账户会让这些记录失去归属，请先停用（enabled=false）而不是删除。")
    try:
        db.delete_account(acct)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"账户仍被引用，无法删除：{exc}") from exc
    await account.registry.sync()
    return {"ok": True}


@router.put("/accounts/{acct}/credentials")
async def set_credentials(acct: int, body: CredentialsIn, request: Request) -> dict:
    """写入凭据并立刻校验。

    写完马上打一次只读接口验证：填错了当场知道，而不是等到持仓页一直空着
    才去翻日志。
    """
    _require_secure(request)
    if db.get_account(acct) is None:
        raise HTTPException(404, f"账户 {acct} 不存在")
    vault.put(acct, "api_key", body.api_key.strip())
    vault.put(acct, "api_secret", body.api_secret.strip())
    if body.passphrase:
        vault.put(acct, "passphrase", body.passphrase.strip())

    try:
        balances = await account.balances(acct)
    except Exception as exc:
        # 不回滚：用户可能只是暂时网络不通，凭据本身是对的。
        # 但要如实告诉他校验没过。
        return {"ok": True, "verified": False,
                "error": f"凭据已保存，但校验请求失败：{str(exc)[:160]}"}
    await account.registry.sync()
    return {"ok": True, "verified": True,
            "assets": len(balances),
            "credentials": vault.status(acct)}


@router.delete("/accounts/{acct}/credentials/{name}")
async def drop_credential(acct: int, name: str) -> dict:
    if name not in ("api_key", "api_secret", "passphrase"):
        raise HTTPException(400, "未知的凭据字段")
    return {"ok": vault.delete(acct, name)}


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
    # 汇率搭这趟车，不另开一个请求 —— 这个端点前端本来就 2 秒轮询一次。
    # 读的是后台循环维护的缓存，不会在请求路径上打第三方接口。
    snap["fx"] = fx.snapshot()
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
