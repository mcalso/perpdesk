"""持仓与盈亏：交易流水录入 / CSV 导入 / 持仓汇总 / 已实现盈亏曲线。"""
import csv
import io
import time

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from .. import db, pnl
from ..hub import hub

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


class TradeIn(BaseModel):
    symbol: str
    side: str
    qty: float = Field(gt=0)
    price: float = Field(ge=0)
    fee: float = Field(default=0.0, ge=0)
    traded_at: int | None = Field(default=None, description="毫秒时间戳，留空取当前时间")
    note: str = ""

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("side")
    @classmethod
    def _side(cls, v: str) -> str:
        s = v.strip().upper()
        if s not in ("BUY", "SELL"):
            raise ValueError("side 必须是 BUY 或 SELL")
        return s


class ImportIn(BaseModel):
    csv_text: str = Field(description="表头: symbol,side,qty,price,fee,traded_at,note")


# 所有接口都接受 ?account=<id>，省略时落到默认账户。
AccountQ = Query(None, description="账户 id，省略则用默认账户")


def _resolve(account_id: int | None) -> int:
    if account_id is None:
        return db.default_account_id()
    if db.get_account(account_id) is None:
        raise HTTPException(404, f"账户 {account_id} 不存在")
    return account_id


def _marks() -> dict[str, float]:
    return hub.mark_prices()


@router.get("/trades")
async def list_trades(
    symbol: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    account_id: int | None = AccountQ,
) -> dict:
    """成交流水，按时间倒序分页。"""
    acct = _resolve(account_id)
    rows, total = db.page_trades(symbol.upper() if symbol else None, limit, offset,
                                 account_id=acct)
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


@router.post("/trades")
async def add_trade(body: TradeIn, account_id: int | None = AccountQ) -> dict:
    ts = body.traded_at or int(time.time() * 1000)
    tid = db.add_trade(body.symbol, body.side, body.qty, body.price, body.fee, ts,
                       body.note, account_id=_resolve(account_id))
    return {"ok": True, "id": tid}


@router.delete("/trades/{trade_id}")
async def delete_trade(trade_id: int, account_id: int | None = AccountQ) -> dict:
    if not db.delete_trade(trade_id, account_id=_resolve(account_id)):
        raise HTTPException(404, "trade not found")
    return {"ok": True}


@router.post("/import")
async def import_csv(body: ImportIn, account_id: int | None = AccountQ) -> dict:
    """容错导入：逐行校验，好行入库，坏行原样报回，不做全量回滚。"""
    reader = csv.DictReader(io.StringIO(body.csv_text.strip()))
    if not reader.fieldnames or "symbol" not in reader.fieldnames:
        raise HTTPException(400, "CSV 缺少表头，至少需要 symbol,side,qty,price")

    rows, errors = [], []
    for i, raw in enumerate(reader, start=2):
        try:
            symbol = (raw.get("symbol") or "").strip().upper()
            side = (raw.get("side") or "").strip().upper()
            if side in ("LONG", "B", "买"):
                side = "BUY"
            elif side in ("SHORT", "S", "卖"):
                side = "SELL"
            if not symbol or side not in ("BUY", "SELL"):
                raise ValueError("symbol/side 非法")
            qty = float(raw["qty"])
            price = float(raw["price"])
            if qty <= 0 or price < 0:
                raise ValueError("qty 必须 > 0，price 不能为负")
            fee = float(raw.get("fee") or 0)
            ts_raw = (raw.get("traded_at") or "").strip()
            traded_at = _parse_ts(ts_raw) if ts_raw else int(time.time() * 1000)
            rows.append((symbol, side, qty, price, fee, traded_at, (raw.get("note") or "").strip()))
        except Exception as exc:
            errors.append({"line": i, "error": str(exc), "raw": raw})

    inserted = db.add_trades_bulk(rows, account_id=_resolve(account_id)) if rows else 0
    return {"inserted": inserted, "failed": len(errors), "errors": errors[:20]}


def _parse_ts(s: str) -> int:
    """接受毫秒/秒时间戳，或 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS'。"""
    if s.isdigit():
        n = int(s)
        return n if n > 10_000_000_000 else n * 1000
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return int(time.mktime(time.strptime(s, fmt)) * 1000)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {s}")


# 盈亏回放的结果缓存。键是 (成交版本号, 区间)，成交表一变即自动失效。
# 回放本身只要几十毫秒，但页面会反复切区间、定时刷新，缓存能省掉重复计算。
_calc_cache: dict[tuple, Any] = {}


def _cached(kind: str, days: int | None, account_id: int, build):
    key = (account_id, kind, db.trades_version(account_id), days)
    if key not in _calc_cache:
        # 只清这个账户的旧条目：整体清空的话，A 账户同步一次会把 B 账户
        # 算好的结果一起丢掉，白白重算
        for stale in [k for k in _calc_cache if k[0] == account_id]:
            del _calc_cache[stale]
        _calc_cache[key] = build()
    return _calc_cache[key]



def _since(days: int | None) -> int | None:
    """天数转起始时间戳。days 为空表示全部历史。"""
    return None if not days else int((time.time() - days * 86400) * 1000)


@router.get("/summary")
async def summary(days: int | None = Query(None, ge=1, le=3650,
                                           description="只统计最近 N 天，留空为全部历史"),
                  account_id: int | None = AccountQ) -> dict:
    acct = _resolve(account_id)
    since = _since(days)
    # 持仓估值要用实时标记价，所以缓存的是回放结果而非最终响应
    result = _cached("summary", days, acct,
                     lambda: pnl.build_summary(db.list_trades(account_id=acct), {}, since=since))
    marks = _marks()
    for p in result["positions"]:
        mark = marks.get(p["symbol"], 0.0)
        if mark and p["qty"]:
            p["markPrice"] = mark
            p["unrealized"] = (mark - p["avgCost"]) * p["qty"]
            p["value"] = p["qty"] * mark

    # 资金费单列：它不出现在成交记录里，但对长期/高杠杆持仓是实打实的损益，
    # 漏掉会让统计系统性偏乐观。已实现盈亏仍只算平仓部分，两者不混。
    per_symbol = db.income_totals(since, account_id=acct)
    totals = db.income_by_type(since, account_id=acct)
    for p in result["positions"]:
        p["funding"] = (per_symbol.get(p["symbol"]) or {}).get("FUNDING_FEE", 0.0)

    funding = totals.get("FUNDING_FEE", 0.0)
    s = result["summary"]
    s["totalFunding"] = funding
    s["totalPnl"] = s["totalRealized"] + s["totalUnrealized"] + funding
    s["exchangeRealized"] = totals.get("REALIZED_PNL", 0.0)
    s["exchangeCommission"] = totals.get("COMMISSION", 0.0)
    s["hasIncome"] = bool(totals)

    s["days"] = days
    s["rangeFrom"] = since
    s["accountId"] = acct
    result["allocation"] = _allocation(result["positions"])
    return result


@router.get("/curve")
async def curve(
    days: int | None = Query(None, ge=1, le=3650),
    points: int = Query(600, ge=50, le=5000, description="最多返回多少个点，0 表示不抽稀"),
    account_id: int | None = AccountQ,
) -> dict:
    """已实现盈亏曲线。days 给定时输出区间损益（起点归零）。

    默认抽稀到 600 个点：原始曲线可达数千点（约数百 KB），传输与渲染都很慢，
    而抽稀保留了每段极值，视觉上看不出差别。
    """
    acct = _resolve(account_id)
    since = _since(days)
    full = _cached("curve", days, acct,
                   lambda: pnl.equity_curve(db.list_trades(account_id=acct), since=since))
    shown = pnl.downsample(full, points) if points else full
    return {
        "points": shown,
        "days": days,
        "total": len(full),
        "sampled": len(full) != len(shown),
        "from": full[0]["t"] if full else None,
        "to": full[-1]["t"] if full else None,
        "final": full[-1]["realized"] if full else 0.0,
    }


def _allocation(positions: list[dict]) -> list[dict]:
    """按名义敞口的绝对值算占比，多空都算敞口。"""
    live = [p for p in positions if p["qty"] != 0]
    total = sum(abs(p["value"]) for p in live)
    if not total:
        return []
    return [
        {
            "symbol": p["symbol"],
            "value": p["value"],
            "weight": abs(p["value"]) / total * 100,
            "side": p["side"],
        }
        for p in sorted(live, key=lambda x: abs(x["value"]), reverse=True)
    ]
