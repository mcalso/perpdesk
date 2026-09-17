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


def test_nothing_pushed_before_viewport_reported(hub):
    """客户端没声明视口之前，一行都不推。

    首屏数据由 REST /api/market/tickers 给，所以这里推空是安全的；
    反过来如果默认推全量，一个标签页就要 204 kbps。
    """
    sub = hub.subscribe()
    assert sub.symbols is None
    assert hub.frame_for(sub) is None


def test_frame_contains_only_requested_symbols(hub):
    sub = hub.subscribe()
    hub.set_viewport(sub, ["BTCUSDT"])
    assert set(rows_of(hub.frame_for(sub))) == {"BTCUSDT"}


def test_unchanged_rows_are_not_resent(hub):
    """第二帧里没有任何没动过的行；全都没动时整帧都不发。"""
    sub = hub.subscribe()
    hub.set_viewport(sub, ["BTCUSDT", "ETHUSDT"])
    assert set(rows_of(hub.frame_for(sub))) == {"BTCUSDT", "ETHUSDT"}

    assert hub.frame_for(sub) is None                  # 全没动 → 不发

    hub.snapshot["BTCUSDT"] = tick("BTCUSDT", last=101.0)
    assert set(rows_of(hub.frame_for(sub))) == {"BTCUSDT"}   # 只发动了的


def test_symbol_reentering_viewport_is_resent(hub):
    """标的移出视口再回来时必须重发，哪怕它的值一点没变。

    回归的是增量缓存没跟着视口收缩：翻页走了又翻回来，那一行会因为
    「和上次发的一样」被永久跳过，从此再也不更新。
    """
    sub = hub.subscribe()
    hub.set_viewport(sub, ["BTCUSDT"])
    hub.frame_for(sub)                                 # BTCUSDT 已发过

    hub.set_viewport(sub, ["ETHUSDT"])                 # 翻页，BTC 移出视口
    hub.frame_for(sub)
    hub.set_viewport(sub, ["BTCUSDT"])                 # 翻回来
    assert set(rows_of(hub.frame_for(sub))) == {"BTCUSDT"}


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
    assert set(rows_of(hub.frame_for(sub))) == {"BTCUSDT"}


def test_frame_field_order_matches_schema(hub):
    """帧是位置数组，字段顺序在后端两处 + 前端 decode() 共三处写死。"""
    from backend.app.routers.market import FRAME_FIELDS

    sub = hub.subscribe()
    hub.set_viewport(sub, ["BTCUSDT"])
    row = rows_of(hub.frame_for(sub))["BTCUSDT"]
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
