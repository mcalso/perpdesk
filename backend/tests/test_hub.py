"""行情中心：下架剔除与按视口增量推送。

这两块都是「不做会静默出错」的类型 —— 下架的标的不会报错，只会永远
冻结在最后一口价上；推送过量也不会报错，只会把小机器的上行带宽吃光。
所以用例都盯着可观察的后果写，不盯实现细节。
"""
import json

import pytest

from backend.app import config
from backend.app.hub import TickerHub


def tick(symbol, last=100.0, chg=1.0, vol=1e6, high=110.0, low=90.0):
    return {"symbol": symbol, "last": last, "chgPct": chg,
            "quoteVolume": vol, "high": high, "low": low}


def meta(symbol):
    return {"symbol": symbol, "base": symbol.removesuffix("USDT"),
            "assetClass": "crypto"}


@pytest.fixture
def hub():
    h = TickerHub()
    h.meta = {s: meta(s) for s in ("BTCUSDT", "ETHUSDT", "FOOUSDT")}
    h.snapshot = {s: tick(s) for s in ("BTCUSDT", "ETHUSDT", "FOOUSDT")}
    h.premium = {s: {"markPrice": 100.1, "fundingRate": 1e-4, "nextFundingTime": 0}
                 for s in h.snapshot}
    return h


# ---------------- 下架剔除 ----------------

def test_delisted_symbol_disappears(hub):
    """exchangeInfo 不再包含的标的必须从快照、盘口、估值里一起消失。

    回归的是「幽灵行」：snapshot 用 update() 只进不出，下架标的会永久留存，
    在榜单上冻结成一个永不更新的价格，还会进 mark_prices() 污染持仓估值。
    """
    hub.book["FOOUSDT"] = {"bid": 1.0, "ask": 1.1, "mid": 1.05,
                           "bidQty": 0, "askQty": 0, "ts": 0}
    del hub.meta["FOOUSDT"]
    hub._prune_delisted()

    assert "FOOUSDT" not in hub.snapshot
    assert "FOOUSDT" not in hub.book
    assert "FOOUSDT" not in hub.mark_prices()
    assert [r["symbol"] for r in hub.rich_rows()] == ["BTCUSDT", "ETHUSDT"]
    # 三个计数从此必须自洽，对不上就说明又漏了一处
    st = hub.status()
    assert st["symbols"] == st["snapshot"] == 2


def test_rest_flake_does_not_drop_symbols(hub):
    """币安某轮 ticker/24hr 少返回几个标的时，它们必须留在榜单上。

    这是 _ticker_loop 用 update() 而不是整体替换的原因。判据只能是
    exchangeInfo，不能是「这轮 REST 没返回」，否则榜单会闪烁。
    """
    only_btc = {"BTCUSDT": tick("BTCUSDT", last=200.0)}
    hub.snapshot.update({k: v for k, v in only_btc.items() if k in hub.meta})
    hub._prune_delisted()

    assert set(hub.snapshot) == {"BTCUSDT", "ETHUSDT", "FOOUSDT"}
    assert hub.snapshot["BTCUSDT"]["last"] == 200.0    # 返回了的要更新


def test_prune_is_noop_when_meta_unavailable(hub):
    """exchangeInfo 还没拉到时一个都不能删，否则整张表会被清空。"""
    hub.meta = {}
    hub._prune_delisted()
    assert len(hub.snapshot) == 3


# ---------------- 按视口推送 ----------------

def rows_of(payload):
    return {r[0]: r for r in json.loads(payload)["rows"]}


def frame(h, sub):
    """走一遍真实链路：stage（攒变化）→ drain（取出并清空）。

    刻意不提供「一步到位」的封装。生产代码里这两步是分开的，中间隔着
    连接自己的 pump —— 而正是那条缝里曾经藏过一个数据永久丢失的 bug
    （见 test_slow_client_loses_nothing）。测试要走一样的路。
    """
    h.stage(sub)
    return h.drain(sub)


def test_nothing_pushed_before_viewport_reported(hub):
    """客户端没声明视口之前，一行都不推。

    首屏数据由 REST /api/market/tickers 给，所以这里推空是安全的；
    反过来如果默认推全量，一个标签页就要 204 kbps。
    """
    sub = hub.subscribe()
    assert sub.symbols is None
    assert frame(hub, sub) is None


def test_frame_contains_only_requested_symbols(hub):
    sub = hub.subscribe()
    hub.set_viewport(sub, ["BTCUSDT"])
    assert set(rows_of(frame(hub, sub))) == {"BTCUSDT"}


def test_unchanged_rows_are_not_resent(hub):
    """第二帧里没有任何没动过的行；全都没动时整帧都不发。"""
    sub = hub.subscribe()
    hub.set_viewport(sub, ["BTCUSDT", "ETHUSDT"])
    assert set(rows_of(frame(hub, sub))) == {"BTCUSDT", "ETHUSDT"}

    assert frame(hub, sub) is None                  # 全没动 → 不发

    hub.snapshot["BTCUSDT"] = tick("BTCUSDT", last=101.0)
    assert set(rows_of(frame(hub, sub))) == {"BTCUSDT"}   # 只发动了的


def test_symbol_reentering_viewport_is_resent(hub):
    """标的移出视口再回来时必须重发，哪怕它的值一点没变。

    回归的是增量缓存没跟着视口收缩：翻页走了又翻回来，那一行会因为
    「和上次发的一样」被永久跳过，从此再也不更新。
    """
    sub = hub.subscribe()
    hub.set_viewport(sub, ["BTCUSDT"])
    frame(hub, sub)                                 # BTCUSDT 已发过

    hub.set_viewport(sub, ["ETHUSDT"])                 # 翻页，BTC 移出视口
    frame(hub, sub)
    hub.set_viewport(sub, ["BTCUSDT"])                 # 翻回来
    assert set(rows_of(frame(hub, sub))) == {"BTCUSDT"}


def test_viewport_normalises_input(hub):
    """大小写、重复、空值都由服务端归一，客户端不必自律。"""
    sub = hub.subscribe()
    hub.set_viewport(sub, ["btcusdt", "BTCUSDT", " ethusdt ", "", None])
    assert sub.symbols == ["BTCUSDT", "ETHUSDT"]


def test_viewport_is_capped(hub):
    """上限存在的意义就是不让客户端把整个市场要过去。"""
    sub = hub.subscribe()
    hub.set_viewport(sub, [f"S{i}USDT" for i in range(config.VIEWPORT_MAX + 50)])
    assert len(sub.symbols) == config.VIEWPORT_MAX


def test_unknown_symbol_is_skipped_silently(hub):
    """客户端要了个不存在/已下架的标的，跳过即可，不能让整帧失败。"""
    sub = hub.subscribe()
    hub.set_viewport(sub, ["BTCUSDT", "NOPEUSDT"])
    assert set(rows_of(frame(hub, sub))) == {"BTCUSDT"}


def test_frame_field_order_matches_schema(hub):
    """帧是位置数组，字段顺序在后端两处 + 前端 decode() 共三处写死。"""
    from backend.app.routers.market import FRAME_FIELDS

    sub = hub.subscribe()
    hub.set_viewport(sub, ["BTCUSDT"])
    row = rows_of(frame(hub, sub))["BTCUSDT"]
    assert len(row) == len(FRAME_FIELDS)
    assert row[FRAME_FIELDS.index("symbol")] == "BTCUSDT"
    assert row[FRAME_FIELDS.index("chgPct")] == 1.0
    assert row[FRAME_FIELDS.index("markPrice")] == 100.1


def test_unsubscribe_removes_subscriber(hub):
    sub = hub.subscribe()
    assert hub.status()["subscribers"] == 1
    hub.unsubscribe(sub)
    assert hub.status()["subscribers"] == 0


def test_meta_refresh_triggers_prune(hub, monkeypatch):
    """剔除必须真的挂在 exchangeInfo 刷新之后。

    只测 _prune_delisted() 本身是不够的 —— 把那一行调用从 _meta_loop 里
    删掉，其余用例依然全绿，而线上就再也不会剔除任何东西了。
    """
    import asyncio

    from backend.app import hub as hub_mod

    async def fake_exchange_info(force=False, retries=3):
        return {s: meta(s) for s in ("BTCUSDT", "ETHUSDT")}   # FOOUSDT 下架

    monkeypatch.setattr(hub_mod.binance, "exchange_info", fake_exchange_info)

    async def drive():
        task = asyncio.create_task(hub._meta_loop())
        for _ in range(100):                  # 让它跑完第一轮后进入长 sleep
            await asyncio.sleep(0)
            if "FOOUSDT" not in hub.snapshot:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert "FOOUSDT" not in hub.snapshot


# ---------------- 慢客户端：增量不能丢 ----------------

def test_slow_client_loses_nothing(hub):
    """客户端卡住若干 tick 期间攒下的变化，一条都不能少。

    回归的是增量推送引入过的真 bug：早先每个 tick 直接把序列化好的帧塞进
    一个 maxsize=2 的队列，满了丢最旧的一帧 —— 而那一帧里的行**已经**被
    记进 sent，于是永远不会补发。表现是客户端上那一行永久停在旧值，而且
    只影响「变一次之后就不再动」的标的，行情表里一抓一大把。

    推全量快照时丢帧是无害的（下一帧什么都有），改成增量之后这个前提就
    没了 —— 这是那次改动真正的代价，当时没看见。
    """
    sub = hub.subscribe()
    hub.set_viewport(sub, ["BTCUSDT", "ETHUSDT", "FOOUSDT"])
    frame(hub, sub)                                    # 首帧已送达

    # 客户端卡住。FOOUSDT 变一次之后就再也不动，另两个随后各变一次
    hub.snapshot["FOOUSDT"] = tick("FOOUSDT", last=777.0)
    hub.stage(sub)
    hub.snapshot["BTCUSDT"] = tick("BTCUSDT", last=101.0)
    hub.stage(sub)
    hub.snapshot["ETHUSDT"] = tick("ETHUSDT", last=102.0)
    hub.stage(sub)

    # 客户端终于能收了：三条最新值一次给全
    rows = rows_of(hub.drain(sub))
    assert set(rows) == {"BTCUSDT", "ETHUSDT", "FOOUSDT"}
    assert rows["FOOUSDT"][1] == 777.0
    assert rows["BTCUSDT"][1] == 101.0
    assert rows["ETHUSDT"][1] == 102.0


def test_repeated_changes_coalesce(hub):
    """同一标的卡住期间变了多次，只发最后一个值，不积压成多条。"""
    sub = hub.subscribe()
    hub.set_viewport(sub, ["BTCUSDT"])
    frame(hub, sub)

    for price in (101.0, 102.0, 103.0):
        hub.snapshot["BTCUSDT"] = tick("BTCUSDT", last=price)
        hub.stage(sub)

    rows = rows_of(hub.drain(sub))
    assert len(rows) == 1
    assert rows["BTCUSDT"][1] == 103.0


def test_pending_is_bounded_by_viewport(hub):
    """卡再久，待发量也不会超过视口大小 —— 覆盖写入，不是追加。"""
    sub = hub.subscribe()
    hub.set_viewport(sub, ["BTCUSDT", "ETHUSDT"])
    for i in range(200):
        hub.snapshot["BTCUSDT"] = tick("BTCUSDT", last=100.0 + i)
        hub.stage(sub)
    assert len(sub.pending) <= 2


def test_drain_clears_so_nothing_is_sent_twice(hub):
    sub = hub.subscribe()
    hub.set_viewport(sub, ["BTCUSDT"])
    assert hub.drain(sub) is None                      # 还没 stage，没东西可发
    hub.stage(sub)
    assert hub.drain(sub) is not None
    assert hub.drain(sub) is None                      # 取过就空了


def test_stage_reports_whether_there_is_work(hub):
    """stage 的返回值是广播循环用来决定要不要唤醒 pump 的，不能骗人。"""
    sub = hub.subscribe()
    assert hub.stage(sub) is False                     # 还没上报视口
    hub.set_viewport(sub, ["BTCUSDT"])
    assert hub.stage(sub) is True
    hub.drain(sub)
    assert hub.stage(sub) is False                     # 没有新变化


def test_viewport_shrink_drops_pending_rows(hub):
    """翻页之后，上一页还没发出去的行不该再发过去。"""
    sub = hub.subscribe()
    hub.set_viewport(sub, ["BTCUSDT", "ETHUSDT"])
    hub.stage(sub)                                     # 两行都在 pending 里
    hub.set_viewport(sub, ["ETHUSDT"])                 # 翻页
    assert set(rows_of(hub.drain(sub))) == {"ETHUSDT"}


# ---------------- 下架后不再占用实时订阅名额 ----------------

def test_delisted_symbol_leaves_ws_subscription(hub):
    """自选里留着一个已下架的标的时，不该继续向币安订阅它。

    不修的话它会一直占着 WS_MAX_STREAMS 的一个名额，而对方根本不会推。
    """
    hub.set_ws_symbols(["BTCUSDT", "FOOUSDT"])
    assert hub.ws_symbols == ["BTCUSDT", "FOOUSDT"]

    del hub.meta["FOOUSDT"]
    hub._prune_delisted()
    assert hub.ws_symbols == ["BTCUSDT"]


def test_ws_subscription_not_filtered_before_meta_arrives(hub):
    """exchangeInfo 还没拉到时一个都不能滤，否则实时订阅会被全部清空。"""
    hub.meta = {}
    hub.set_ws_symbols(["BTCUSDT", "ETHUSDT"])
    assert hub.ws_symbols == ["BTCUSDT", "ETHUSDT"]
