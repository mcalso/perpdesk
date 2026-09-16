"""盈亏引擎的测试。

这块到目前为止出过四次正确性问题，其中两次是能在这里覆盖的：
  * 账户用过双向持仓模式（同一标的同时持多空），本地净额加权平均法算不对，
    必须以交易所给的每笔已实现盈亏为准；
  * 区间曲线的基准取错了位置，差一笔的增量（实测 19.9 vs 应为 19.8）。
另外两次（成交分页漏 49 笔造出幽灵持仓、资金流水漏 41%）出在取数侧，
不属于本模块。

写这些用例的取舍：宁可用手算得出、一眼能验证的小数字，也不要用真实数据
片段——真实数据看着"像对的"，但没人能一眼说出期望值是多少。
"""
import pytest

from backend.app import pnl


_seq = 0


def trade(symbol="BTCUSDT", side="BUY", qty=1.0, price=100.0, fee=0.0,
          at=None, realized_pnl=None, **kw):
    """造一笔成交。id 自增以保证同毫秒内的稳定排序。"""
    global _seq
    _seq += 1
    return {
        "id": kw.pop("id", _seq),
        "symbol": symbol, "side": side, "qty": qty, "price": price, "fee": fee,
        "traded_at": _seq * 1000 if at is None else at,
        "note": "", "realized_pnl": realized_pnl, **kw,
    }


def book_of(trades, symbol="BTCUSDT"):
    return pnl.compute_positions(trades)[symbol]


# ---------------------------------------------------------------- 基本算法

def test_open_and_close_long():
    b = book_of([
        trade(side="BUY", qty=1, price=100),
        trade(side="SELL", qty=1, price=110),
    ])
    assert b["realized"] == pytest.approx(10.0)
    assert b["qty"] == 0.0
    assert b["avgCost"] == 0.0


def test_open_and_close_short():
    """做空赚的是跌幅：100 卖出、90 买回 → +10。"""
    b = book_of([
        trade(side="SELL", qty=1, price=100),
        trade(side="BUY", qty=1, price=90),
    ])
    assert b["realized"] == pytest.approx(10.0)
    assert b["qty"] == 0.0


def test_weighted_average_cost_on_add():
    b = book_of([
        trade(side="BUY", qty=1, price=100),
        trade(side="BUY", qty=3, price=200),
    ])
    assert b["qty"] == pytest.approx(4.0)
    assert b["avgCost"] == pytest.approx(175.0)   # (100*1 + 200*3) / 4


def test_partial_close_keeps_cost_basis():
    """部分平仓不改变剩余仓位的成本。"""
    b = book_of([
        trade(side="BUY", qty=4, price=100),
        trade(side="SELL", qty=1, price=150),
    ])
    assert b["qty"] == pytest.approx(3.0)
    assert b["avgCost"] == pytest.approx(100.0)
    assert b["realized"] == pytest.approx(50.0)


def test_flip_closes_then_opens_at_trade_price():
    """反手：持多 1 时卖 3 → 先平掉 1 结算盈亏，剩 -2 的新空仓成本是成交价。"""
    b = book_of([
        trade(side="BUY", qty=1, price=100),
        trade(side="SELL", qty=3, price=120),
    ])
    assert b["realized"] == pytest.approx(20.0)   # 只有被平掉的那 1 个计盈亏
    assert b["qty"] == pytest.approx(-2.0)
    assert b["avgCost"] == pytest.approx(120.0)   # 不是 100


def test_fee_is_excluded_from_cost_but_deducted_from_realized():
    b = book_of([
        trade(side="BUY", qty=1, price=100, fee=0.5),
        trade(side="SELL", qty=1, price=110, fee=0.5),
    ])
    assert b["fee"] == pytest.approx(1.0)
    assert b["realized"] == pytest.approx(9.0)    # 10 的价差 - 1 的手续费


def test_float_residue_is_zeroed():
    """逐笔累加会攒出浮点残渣：0.1*3 - 0.3 = 5.55e-17，不归零就会显示成幽灵持仓。"""
    b = book_of([
        trade(side="BUY", qty=0.1, price=100),
        trade(side="BUY", qty=0.1, price=100),
        trade(side="BUY", qty=0.1, price=100),
        trade(side="SELL", qty=0.3, price=100),
    ])
    assert b["qty"] == 0.0        # 严格等于，不是 approx
    assert b["avgCost"] == 0.0


def test_unsorted_input_gives_same_result():
    """调用方不保证顺序，引擎自己按 (traded_at, id) 排。"""
    ts = [
        trade(side="BUY", qty=1, price=100, at=1000, id=1),
        trade(side="SELL", qty=1, price=110, at=2000, id=2),
        trade(side="BUY", qty=1, price=90, at=3000, id=3),
    ]
    assert pnl.compute_positions(ts) == pnl.compute_positions(list(reversed(ts)))


def test_returned_delta_matches_accumulated_realized():
    """apply_trade 的返回值逐笔加起来，必须等于账本里的 realized。

    区间统计完全建立在这个等式上——不成立的话，区间和全量会对不上。
    """
    books = {}
    total = sum(pnl.apply_trade(books, t) for t in [
        trade(side="BUY", qty=2, price=100, fee=0.1),
        trade(side="SELL", qty=1, price=130, fee=0.2),
        trade(side="SELL", qty=3, price=90, fee=0.3),
    ])
    assert total == pytest.approx(books["BTCUSDT"]["realized"])


# ---------------------------------------------------------------- 交易所口径优先

def test_exchange_realized_pnl_overrides_local_calculation():
    """账户用过双向持仓模式时，本地净额加权平均法算不出正确结果。

    交易所给了这笔的已实现盈亏就一律以它为准，哪怕和本地算的差很远。
    """
    b = book_of([
        trade(side="BUY", qty=1, price=100, realized_pnl=0.0),
        trade(side="SELL", qty=1, price=110, realized_pnl=3.0),
    ])
    assert b["realized"] == pytest.approx(3.0), "应采信交易所的 3.0，而不是本地算的 10.0"


def test_exchange_zero_is_a_real_value_not_missing():
    """0.0 是交易所给出的有效值，不能当成"没给"。

    绝大多数开仓成交的 realized_pnl 就是 0.0。要是写成 `if exchange_pnl:`
    而不是 `is not None`，这些笔会悄悄回退到本地计算，同一标的就会变成
    一半交易所口径、一半本地口径的混合结果——数字看着正常，但没有意义。
    """
    b = book_of([
        trade(side="BUY", qty=1, price=100, realized_pnl=0.0),
        trade(side="SELL", qty=1, price=110, realized_pnl=0.0),
    ])
    assert b["realized"] == pytest.approx(0.0), "0.0 被当成缺失值，回退到本地计算了"


def test_manual_trades_without_exchange_pnl_fall_back_to_replay():
    """手工录入的成交没有交易所盈亏字段，得由本地回放补算。"""
    b = book_of([
        trade(side="BUY", qty=1, price=100, realized_pnl=None),
        trade(side="SELL", qty=1, price=110, realized_pnl=None),
    ])
    assert b["realized"] == pytest.approx(10.0)


# ---------------------------------------------------------------- 区间统计

def test_summary_window_keeps_cost_basis_from_before_the_window():
    """区间统计的关键：持仓成本来自窗口之前的开仓，不能只回放窗口内的成交。

    只拿区间内的成交回放，这里算出来的均价会变成 0（没有开仓记录），
    已实现盈亏就会虚高成整个卖出金额。
    """
    trades = [
        trade(side="BUY", qty=1, price=100, at=1000),   # 窗口之前开仓
        trade(side="SELL", qty=1, price=110, at=5000),  # 窗口之内平仓
    ]
    out = pnl.build_summary(trades, marks={"BTCUSDT": 110.0}, since=3000)
    assert out["summary"]["totalRealized"] == pytest.approx(10.0)


def test_summary_window_excludes_earlier_realized():
    trades = [
        trade(side="BUY", qty=1, price=100, at=1000),
        trade(side="SELL", qty=1, price=200, at=2000),   # 窗口之前赚的 100
        trade(side="BUY", qty=1, price=100, at=5000),
        trade(side="SELL", qty=1, price=110, at=6000),   # 窗口之内赚的 10
    ]
    out = pnl.build_summary(trades, marks={}, since=3000)
    assert out["summary"]["totalRealized"] == pytest.approx(10.0), "窗口外的 100 不该算进来"


def test_summary_unrealized_uses_mark_price():
    out = pnl.build_summary(
        [trade(side="BUY", qty=2, price=100)], marks={"BTCUSDT": 130.0})
    p = out["positions"][0]
    assert p["unrealized"] == pytest.approx(60.0)      # (130-100) * 2
    assert p["unrealizedPct"] == pytest.approx(30.0)   # 60 / 200
    assert p["side"] == "LONG"


def test_summary_without_mark_price_reports_zero_unrealized():
    """行情缺失时浮盈记 0，而不是拿 0 当价格算出 -100% 的巨亏。"""
    out = pnl.build_summary([trade(side="BUY", qty=2, price=100)], marks={})
    assert out["positions"][0]["unrealized"] == 0.0


def test_summary_counts_open_positions_only():
    out = pnl.build_summary([
        trade(symbol="AAA", side="BUY", qty=1, price=100),
        trade(symbol="BBB", side="BUY", qty=1, price=100),
        trade(symbol="BBB", side="SELL", qty=1, price=100),   # 已平
    ], marks={"AAA": 100.0, "BBB": 100.0})
    assert out["summary"]["openCount"] == 1
    assert out["summary"]["symbolCount"] == 2


# ---------------------------------------------------------------- 曲线

def test_equity_curve_accumulates():
    curve = pnl.equity_curve([
        trade(side="BUY", qty=1, price=100, at=1000),
        trade(side="SELL", qty=1, price=110, at=2000),
        trade(side="BUY", qty=1, price=100, at=3000),
        trade(side="SELL", qty=1, price=105, at=4000),
    ])
    assert [p["realized"] for p in curve] == pytest.approx([0.0, 10.0, 10.0, 15.0])


def test_equity_curve_window_baseline_includes_the_first_trade_in_range():
    """区间曲线的基准必须取"加本笔之前"的累计值。

    取成"加本笔之后"就会漏掉区间内第一笔的增量：这里第一个点会变成 0
    而不是 10，整条曲线整体下移。这个差一错误实测出现过（19.9 vs 19.8）。
    """
    trades = [
        trade(side="BUY", qty=1, price=100, at=1000),
        trade(side="SELL", qty=1, price=110, at=2000),   # +10，区间内第一笔
        trade(side="BUY", qty=1, price=90, at=3000),
        trade(side="SELL", qty=1, price=95, at=4000),    # +5
    ]
    curve = pnl.equity_curve(trades, since=2000)

    assert [p["t"] for p in curve] == [2000, 3000, 4000], "区间之前的点不该出现"
    assert [p["realized"] for p in curve] == pytest.approx([10.0, 10.0, 15.0]), \
        "区间内第一笔的增量被基准吃掉了"


def test_equity_curve_window_starts_from_zero_baseline():
    """区间曲线回答的是"这段时间赚了多少"，起点要归零。"""
    trades = [
        trade(side="BUY", qty=1, price=100, at=1000),
        trade(side="SELL", qty=1, price=200, at=2000),   # 区间外赚了 100
        trade(side="BUY", qty=1, price=100, at=5000),
        trade(side="SELL", qty=1, price=110, at=6000),
    ]
    curve = pnl.equity_curve(trades, since=4000)
    assert curve[0]["realized"] == pytest.approx(0.0), "区间外的 100 不该带进来"
    assert curve[-1]["realized"] == pytest.approx(10.0)


def test_equity_curve_empty_input():
    assert pnl.equity_curve([]) == []


# ---------------------------------------------------------------- 抽稀

def _pts(values):
    return [{"t": i * 1000, "realized": v} for i, v in enumerate(values)]


def test_downsample_returns_input_when_short_enough():
    pts = _pts([1, 2, 3])
    assert pnl.downsample(pts, 10) is pts
    assert pnl.downsample(pts, 0) is pts


def test_downsample_respects_limit_and_keeps_endpoints():
    pts = _pts(range(1000))
    out = pnl.downsample(pts, 60)
    assert len(out) <= 60 + 2          # 首尾可能各补一个
    assert out[0]["t"] == pts[0]["t"]
    assert out[-1]["t"] == pts[-1]["t"]
    assert [p["t"] for p in out] == sorted(p["t"] for p in out)


def test_downsample_preserves_extremes():
    """峰值与谷底直接对应最大回撤，抽稀时抹掉它们会让图形失真。"""
    values = [0.0] * 500
    values[123] = 999.0      # 尖峰
    values[377] = -999.0     # 深谷
    out = pnl.downsample(_pts(values), 30)
    kept = [p["realized"] for p in out]
    assert 999.0 in kept, "峰值被抽掉了"
    assert -999.0 in kept, "谷底被抽掉了"


def test_downsample_reinserts_first_point_when_bucketing_drops_it():
    """首点若既不是所在段的极值、也不是段末，会被抽掉——必须补回来。

    曲线的起点就是用户选的区间起点，丢了的话图表会从一个莫名其妙的
    时间开始，看上去像数据缺了一截。
    """
    values = [5.0] * 100
    values[10] = -100.0     # 首段的谷底
    values[20] = 100.0      # 首段的尖峰
    out = pnl.downsample(_pts(values), 9)
    assert out[0]["t"] == 0, "起点被抽掉且没补回来"


def test_downsample_reinserts_last_point_when_bucketing_drops_it():
    """分桶是按浮点步长切的，末尾可能差一个点覆盖不到。

    49 个点、limit=33 时分 11 段，步长 4.4545…，11 段只覆盖到第 48 个，
    最后一个点落在段外。终点丢了意味着图表停在倒数第二笔，
    而用户最关心的恰恰是"现在是多少"。
    """
    pts = _pts(range(49))
    out = pnl.downsample(pts, 33)
    assert out[-1]["t"] == pts[-1]["t"], "终点被抽掉且没补回来"


# ---------------------------------------------------------------- 按日聚合

def _at(iso_utc: str) -> int:
    """UTC 时间字符串 → 毫秒戳。"""
    from datetime import datetime, timezone
    return int(datetime.strptime(iso_utc, "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone.utc).timestamp() * 1000)


def test_daily_buckets_sum_to_the_total():
    trades = [
        trade(side="BUY", qty=1, price=100, fee=0.1, at=_at("2026-01-01 03:00")),
        trade(side="SELL", qty=1, price=150, fee=0.1, at=_at("2026-01-02 03:00")),
        trade(side="BUY", qty=1, price=100, fee=0.1, at=_at("2026-01-03 03:00")),
        trade(side="SELL", qty=1, price=90, fee=0.1, at=_at("2026-01-03 05:00")),
    ]
    rows = pnl.daily_buckets(trades)
    total = sum(r["realized"] for r in rows)
    assert total == pytest.approx(book_of(trades)["realized"]), "分桶后对不上总账"
    assert sum(r["trades"] for r in rows) == 4


def test_daily_buckets_split_by_local_midnight_not_utc():
    """东八区的 UTC 日界落在早上 8 点，按 UTC 分桶会把一个交易日从中间劈开。

    这两笔在北京时间都是 1 月 2 日（00:30 和 15:00），按 UTC 却分属
    1 月 1 日和 1 月 2 日。日线柱错位一天，"昨天赚了多少"就永远是错的。
    """
    trades = [
        trade(side="BUY", qty=1, price=100, at=_at("2026-01-01 16:30")),   # 北京 1/2 00:30
        trade(side="SELL", qty=1, price=110, at=_at("2026-01-02 07:00")),  # 北京 1/2 15:00
    ]
    utc_days = pnl.daily_buckets(trades, tz_offset_min=0)
    cn_days = pnl.daily_buckets(trades, tz_offset_min=480)

    assert len(utc_days) == 2, "UTC 下这两笔本来就跨日"
    assert len(cn_days) == 1, "东八区下应当合并成同一天"
    assert cn_days[0]["realized"] == pytest.approx(10.0)


def test_daily_bucket_boundary_is_local_midnight():
    """桶的起点必须正好是本地零点。"""
    trades = [trade(side="BUY", qty=1, price=100, at=_at("2026-01-01 16:00"))]  # 北京 1/2 00:00
    (row,) = pnl.daily_buckets(trades, tz_offset_min=480)
    assert row["d"] == _at("2026-01-01 16:00"), "桶起点不是北京时间的零点"


def test_daily_buckets_replay_full_history_for_correct_cost():
    """区间统计仍要全量回放：只回放区间内的成交，成本会来自不存在的开仓。"""
    trades = [
        trade(side="BUY", qty=1, price=100, at=_at("2026-01-01 03:00")),   # 区间之前开仓
        trade(side="SELL", qty=1, price=150, at=_at("2026-02-01 03:00")),  # 区间之内平仓
    ]
    rows = pnl.daily_buckets(trades, since=_at("2026-01-15 00:00"))
    assert len(rows) == 1
    assert rows[0]["realized"] == pytest.approx(50.0), "成本基准丢了"


def test_daily_buckets_empty_input():
    assert pnl.daily_buckets([]) == []
