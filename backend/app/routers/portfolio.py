"""持仓与盈亏：交易流水录入 / CSV 导入 / 持仓汇总 / 已实现盈亏曲线。"""
import csv
import io
import time

from fastapi import APIRouter, HTTPException
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


def _marks() -> dict[str, float]:
    return hub.mark_prices()


@router.get("/trades")
async def list_trades(symbol: str | None = None) -> list[dict]:
    return db.list_trades(symbol.upper() if symbol else None)


@router.post("/trades")
async def add_trade(body: TradeIn) -> dict:
    ts = body.traded_at or int(time.time() * 1000)
    tid = db.add_trade(body.symbol, body.side, body.qty, body.price, body.fee, ts, body.note)
    return {"ok": True, "id": tid}


@router.delete("/trades/{trade_id}")
async def delete_trade(trade_id: int) -> dict:
    if not db.delete_trade(trade_id):
        raise HTTPException(404, "trade not found")
    return {"ok": True}


@router.post("/import")
async def import_csv(body: ImportIn) -> dict:
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

    inserted = db.add_trades_bulk(rows) if rows else 0
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


@router.get("/summary")
async def summary() -> dict:
    trades = db.list_trades()
    result = pnl.build_summary(trades, _marks())
    result["allocation"] = _allocation(result["positions"])
    return result


@router.get("/curve")
async def curve() -> dict:
    trades = db.list_trades()
    return {"points": pnl.equity_curve(trades)}


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
