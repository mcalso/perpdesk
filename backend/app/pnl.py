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


def apply_trade(books: dict[str, dict], t: dict) -> float:
    """把一笔成交并入账本（原地修改），返回本笔产生的已实现盈亏增量。

    返回增量是为了支持「只看某段时间」：区间统计不能只拿区间内的成交回放
    —— 持仓成本往往来自更早的开仓，那样算出来的均价是错的。正确做法是
    回放全部成交保证成本正确，再只累加落在区间内的增量。

    规则：
      - 同向加仓 → 重算加权均价；
      - 反向 → 先按均价结平已有仓位计已实现盈亏，超出部分反手开新仓（均价=成交价）；
      - 手续费不进成本，单独累计并从已实现盈亏中扣除。
    """
    sym = t["symbol"]
    b = books.setdefault(sym, new_book(t))
    delta = t["qty"] if t["side"] == "BUY" else -t["qty"]
    price = t["price"]
    gain = -t["fee"]                      # 本笔增量：先计手续费

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
        pnl = closed * (price - b["avgCost"]) * _sign(pos)
        b["realized"] += pnl
        gain += pnl
        remain = pos + delta
        if _sign(remain) != 0 and _sign(remain) != _sign(pos):
            b["avgCost"] = price          # 反手，新仓成本就是本次成交价
        elif remain == 0:
            b["avgCost"] = 0.0
        b["qty"] = remain

    if abs(b["qty"]) < 1e-12:             # 浮点残渣归零
        b["qty"] = 0.0
        b["avgCost"] = 0.0
    return gain


def compute_positions(trades: list[dict]) -> dict[str, dict]:
    """按 symbol 回放交易流水，得到当前净持仓、均价与已实现盈亏。"""
    books: dict[str, dict] = {}
    for t in sorted(trades, key=lambda x: (x["traded_at"], x["id"])):
        apply_trade(books, t)
    return books


def build_summary(trades: list[dict], marks: dict[str, float],
                  since: int | None = None) -> dict[str, Any]:
    """结合当前标记价，产出持仓表 + 组合汇总。

    since 给定时，持仓与均价仍按全部成交回放（否则成本是错的），
    但已实现盈亏与手续费只统计该时刻之后发生的部分。
    """
    books: dict[str, dict] = {}
    windowed: dict[str, dict] = {}
    for t in sorted(trades, key=lambda x: (x["traded_at"], x["id"])):
        gain = apply_trade(books, t)
        if since is None or t["traded_at"] >= since:
            w = windowed.setdefault(t["symbol"], {"realized": 0.0, "fee": 0.0, "count": 0})
            w["realized"] += gain
            w["fee"] += t["fee"]
            w["count"] += 1
    positions = []
    total_realized = 0.0
    total_unrealized = 0.0
    total_fee = 0.0
    gross_exposure = 0.0
    net_exposure = 0.0

    for sym, b in books.items():
        w = windowed.get(sym) or {"realized": 0.0, "fee": 0.0, "count": 0}
        mark = marks.get(sym, 0.0)
        qty = b["qty"]
        value = qty * mark
        unrealized = (mark - b["avgCost"]) * qty if qty and mark else 0.0
        cost_basis = abs(qty) * b["avgCost"]

        total_realized += w["realized"]
        total_unrealized += unrealized
        total_fee += w["fee"]
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
            "realized": w["realized"],
            "fee": w["fee"],
            "tradeCount": w["count"] if since is not None else b["tradeCount"],
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


def equity_curve(trades: list[dict], since: int | None = None) -> list[dict]:
    """累计已实现盈亏曲线（含手续费）。

    刻意不画历史浮盈：历史时点的浮盈要用当时的市价才准，用今天的价格回算
    会把整条曲线拉成"事后诸葛亮"。当前浮盈由交易所持仓那一侧给出。

    since 给定时输出区间损益：仍回放全部成交（保证持仓成本正确），
    但只保留区间内的点，并把起点归零 —— 回答的是"这段时间赚了多少"。
    """
    if not trades:
        return []
    books: dict[str, dict] = {}
    total = 0.0
    base = 0.0
    started = since is None
    curve = []
    for t in sorted(trades, key=lambda x: (x["traded_at"], x["id"])):
        gain = apply_trade(books, t)
        if not started and t["traded_at"] < since:
            total += gain                 # 区间之前的只累计，不出点
            continue
        if not started:
            base = total                  # 基准取「加本笔之前」的累计，否则会漏掉本笔增量
            started = True
        total += gain
        curve.append({"t": t["traded_at"], "realized": total - base})
    return curve
