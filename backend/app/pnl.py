"""持仓与盈亏计算：净额移动加权平均成本法，支持多空与反手。"""
from typing import Any


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def new_book(t: dict) -> dict:
    return {
        "qty": 0.0, "avgCost": 0.0, "realized": 0.0, "fee": 0.0,
        "buyQty": 0.0, "sellQty": 0.0, "tradeCount": 0,
        "firstAt": t["traded_at"], "lastAt": t["traded_at"],
    }


def apply_trade(books: dict[str, dict], t: dict) -> None:
    """把一笔成交并入账本（原地修改）。

    规则：
      - 同向加仓 → 重算加权均价；
      - 反向 → 先按均价结平已有仓位计已实现盈亏，超出部分反手开新仓（均价=成交价）；
      - 手续费不进成本，单独累计并从已实现盈亏中扣除。
    """
    sym = t["symbol"]
    b = books.setdefault(sym, new_book(t))
    delta = t["qty"] if t["side"] == "BUY" else -t["qty"]
    price = t["price"]

    b["fee"] += t["fee"]
    b["realized"] -= t["fee"]
    b["tradeCount"] += 1
    b["lastAt"] = t["traded_at"]
    if t["side"] == "BUY":
        b["buyQty"] += t["qty"]
    else:
        b["sellQty"] += t["qty"]

    pos = b["qty"]
    if pos == 0 or _sign(pos) == _sign(delta):
        total = abs(pos) + abs(delta)
        b["avgCost"] = (b["avgCost"] * abs(pos) + price * abs(delta)) / total
        b["qty"] = pos + delta
    else:
        closed = min(abs(pos), abs(delta))
        b["realized"] += closed * (price - b["avgCost"]) * _sign(pos)
        remain = pos + delta
        if _sign(remain) != 0 and _sign(remain) != _sign(pos):
            b["avgCost"] = price          # 反手，新仓成本就是本次成交价
        elif remain == 0:
            b["avgCost"] = 0.0
        b["qty"] = remain

    if abs(b["qty"]) < 1e-12:             # 浮点残渣归零
        b["qty"] = 0.0
        b["avgCost"] = 0.0


def compute_positions(trades: list[dict]) -> dict[str, dict]:
    """按 symbol 回放交易流水，得到当前净持仓、均价与已实现盈亏。"""
    books: dict[str, dict] = {}
    for t in sorted(trades, key=lambda x: (x["traded_at"], x["id"])):
        apply_trade(books, t)
    return books


def build_summary(trades: list[dict], marks: dict[str, float]) -> dict[str, Any]:
    """结合当前标记价，产出前端要的持仓表 + 组合汇总。"""
    books = compute_positions(trades)
    positions = []
    total_realized = 0.0
    total_unrealized = 0.0
    total_fee = 0.0
    gross_exposure = 0.0
    net_exposure = 0.0

    for sym, b in books.items():
        mark = marks.get(sym, 0.0)
        qty = b["qty"]
        value = qty * mark
        unrealized = (mark - b["avgCost"]) * qty if qty and mark else 0.0
        cost_basis = abs(qty) * b["avgCost"]

        total_realized += b["realized"]
        total_unrealized += unrealized
        total_fee += b["fee"]
        gross_exposure += abs(value)
        net_exposure += value

        positions.append({
            "symbol": sym,
            "qty": qty,
            "avgCost": b["avgCost"],
            "markPrice": mark,
            "value": value,
            "costBasis": cost_basis,
            "unrealized": unrealized,
            "unrealizedPct": (unrealized / cost_basis * 100) if cost_basis else 0.0,
            "realized": b["realized"],
            "fee": b["fee"],
            "tradeCount": b["tradeCount"],
            "side": "LONG" if qty > 0 else ("SHORT" if qty < 0 else "FLAT"),
            "lastAt": b["lastAt"],
        })

    positions.sort(key=lambda p: abs(p["value"]), reverse=True)
    open_positions = [p for p in positions if p["qty"] != 0]

    return {
        "positions": positions,
        "summary": {
            "totalRealized": total_realized,
            "totalUnrealized": total_unrealized,
            "totalPnl": total_realized + total_unrealized,
            "totalFee": total_fee,
            "grossExposure": gross_exposure,
            "netExposure": net_exposure,
            "openCount": len(open_positions),
            "symbolCount": len(positions),
        },
    }


def equity_curve(trades: list[dict]) -> list[dict]:
    """累计已实现盈亏曲线（含手续费）。

    刻意不画历史浮盈：历史时点的浮盈要用当时的市价才准，用今天的价格回算
    会把整条曲线拉成"事后诸葛亮"。当前浮盈由 build_summary 单独给出。
    """
    if not trades:
        return []
    books: dict[str, dict] = {}
    curve = []
    for t in sorted(trades, key=lambda x: (x["traded_at"], x["id"])):
        apply_trade(books, t)
        curve.append({
            "t": t["traded_at"],
            "realized": sum(b["realized"] for b in books.values()),
        })
    return curve
