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

    # 交易所若给了这笔的已实现盈亏，一律以它为准：账户可能用过双向持仓模式
    # （同一标的同时持多空），本地的净额加权平均法在那种情况下算不出正确结果。
    exchange_pnl = t.get("realized_pnl")

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
        if exchange_pnl is None:
            b["realized"] += pnl
            gain += pnl
        remain = pos + delta
        if _sign(remain) != 0 and _sign(remain) != _sign(pos):
            b["avgCost"] = price          # 反手，新仓成本就是本次成交价
        elif remain == 0:
            b["avgCost"] = 0.0
        b["qty"] = remain

    if exchange_pnl is not None:
        b["realized"] += exchange_pnl
        gain += exchange_pnl

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


def downsample(points: list[dict], limit: int) -> list[dict]:
    """把曲线抽稀到 limit 个点以内。

    图表宽度撑死几百像素，几千个点纯属浪费带宽与渲染时间（实测 8105 个点
    约 400KB，在 3Mbps 的小机器上光传输就要 1 秒多）。

    分段取样时保留每段的**极值**而不只是段末值：盈亏曲线的峰值与谷底
    直接对应最大回撤，抹掉它们会让图形失真。
    """
    if limit <= 0 or len(points) <= limit:
        return points
    buckets = max(1, limit // 3)          # 每段最多贡献首/极值/末三个点
    size = len(points) / buckets
    out: list[dict] = []
    for i in range(buckets):
        seg = points[int(i * size):int((i + 1) * size)] or []
        if not seg:
            continue
        lo = min(seg, key=lambda p: p["realized"])
        hi = max(seg, key=lambda p: p["realized"])
        picked = sorted({id(lo): lo, id(hi): hi, id(seg[-1]): seg[-1]}.values(),
                        key=lambda p: p["t"])
        out.extend(picked)
    if out and out[0]["t"] != points[0]["t"]:
        out.insert(0, points[0])
    if out and out[-1]["t"] != points[-1]["t"]:
        out.append(points[-1])
    return out


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


def daily_buckets(trades: list[dict], since: int | None = None,
                  tz_offset_min: int = 0) -> list[dict]:
    """按自然日聚合已实现盈亏、手续费与成交笔数。

    tz_offset_min 是本地时区相对 UTC 的分钟偏移（东八区 = 480）。
    "今天赚了多少"里的"今天"是用户屏幕上的那一天：东八区的 UTC 日界落在
    早上 8 点，按 UTC 分桶会把一个交易日从中间劈开，日线柱全是错位的。

    与 build_summary 一样先全量回放再按区间累加 —— 只回放区间内的成交，
    持仓成本会来自不存在的开仓，盈亏全错。
    """
    books: dict[str, dict] = {}
    out: dict[int, dict] = {}
    off = tz_offset_min * 60_000
    day_ms = 86_400_000
    for t in sorted(trades, key=lambda x: (x["traded_at"], x["id"])):
        gain = apply_trade(books, t)
        if since is not None and t["traded_at"] < since:
            continue
        day = ((t["traded_at"] + off) // day_ms) * day_ms - off
        b = out.setdefault(day, {"d": day, "realized": 0.0, "fee": 0.0,
                                 "funding": 0.0, "trades": 0})
        b["realized"] += gain          # gain 已扣手续费，与 build_summary 同口径
        b["fee"] += t["fee"]
        b["trades"] += 1
    return sorted(out.values(), key=lambda x: x["d"])
