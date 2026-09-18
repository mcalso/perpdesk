"""USDT→CNY 参考汇率。

这个功能是「锦上添花」，所以用例的重点不是算得多准，而是**它坏掉的时候
不能拖累任何别的东西**：取不到就不显示，绝不能让账户接口报错，也不能
在权益旁边摆一个陈旧或离谱的数字。
"""
import asyncio
import time

import pytest

from backend.app import fx


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._p


class FakeClient:
    """按 tradeType 返回不同结果；值可以是 payload，也可以是要抛的异常。"""

    def __init__(self, by_side):
        self.by_side = by_side
        self.calls = []

    async def post(self, url, json=None, **kw):
        side = (json or {}).get("tradeType")
        self.calls.append(side)
        v = self.by_side[side]
        if isinstance(v, Exception):
            raise v
        return FakeResp(v)


def ads(*prices):
    return {"success": True, "code": "000000",
            "data": [{"adv": {"price": str(p)}} for p in prices]}


@pytest.fixture(autouse=True)
def _clean():
    """每个用例都从空缓存开始 —— 模块级状态会串味。"""
    fx._rate, fx._at = None, 0.0
    yield
    fx._rate, fx._at = None, 0.0


def use(monkeypatch, by_side):
    c = FakeClient(by_side)
    monkeypatch.setattr(fx, "_http", lambda: c)
    return c


# ---------------- 取价 ----------------

def test_uses_median_not_best_price(monkeypatch):
    """取中位而不是榜首。

    榜首广告常带着极端限额（只收 3 万以上之类），不代表实际能成交的水平。
    这里榜首 6.90 明显偏离，中位数 6.66 才是真实水平。
    """
    use(monkeypatch, {"BUY": ads(6.90, 6.66, 6.66, 6.65, 6.65),
                      "SELL": ads(6.90, 6.66, 6.66, 6.65, 6.65)})
    assert asyncio.run(fx.refresh()) == 6.66


def test_midpoint_of_both_sides(monkeypatch):
    """两侧取中点，不偏向买价或卖价。"""
    use(monkeypatch, {"BUY": ads(6.60), "SELL": ads(6.70)})
    assert asyncio.run(fx.refresh()) == 6.65


def test_absurd_prices_are_dropped(monkeypatch):
    """脏数据不能带偏中位。

    样本要选能真正区分开的：脏数据必须多到足以移动中位数，否则过滤与不过滤
    结果相同，用例形同虚设（变异测试实测存活过一次 —— 原样本是
    [0, 6.66, 6.66, 99999]，两种实现算出来都是 6.66）。

    这里三条 0 价 + 两条正常：不过滤的话中位数是 0，权益会显示 ¥0。
    """
    use(monkeypatch, {"BUY": ads(0, 0, 0, 6.66, 6.66),
                      "SELL": ads(0, 0, 0, 6.66, 6.66)})
    assert asyncio.run(fx.refresh()) == 6.66


def test_one_side_down_still_works(monkeypatch):
    """一侧挂了就用另一侧，不要因为半边故障整个不显示。"""
    use(monkeypatch, {"BUY": RuntimeError("超时"), "SELL": ads(6.70, 6.70)})
    assert asyncio.run(fx.refresh()) == 6.70


def test_both_sides_down_keeps_last_good_value(monkeypatch):
    """全挂时保留上一次的好值，不要把已有汇率清成 None。"""
    use(monkeypatch, {"BUY": ads(6.66), "SELL": ads(6.66)})
    asyncio.run(fx.refresh())

    use(monkeypatch, {"BUY": RuntimeError("挂了"), "SELL": RuntimeError("也挂了")})
    with pytest.raises(RuntimeError):
        asyncio.run(fx.refresh())
    assert fx.snapshot()["cny"] == 6.66


def test_success_false_is_an_error(monkeypatch):
    """接口返回 200 但 success=false，不能当成功解析。"""
    use(monkeypatch, {"BUY": {"success": False, "code": "000002"},
                      "SELL": {"success": False, "code": "000002"}})
    with pytest.raises(RuntimeError):
        asyncio.run(fx.refresh())
    assert fx.snapshot() is None


def test_empty_ad_list_is_not_a_zero_rate(monkeypatch):
    """一条广告都没有时必须报错，绝不能算出 0 —— 那会让权益显示 ¥0。"""
    use(monkeypatch, {"BUY": ads(), "SELL": ads()})
    with pytest.raises(RuntimeError):
        asyncio.run(fx.refresh())
    assert fx.snapshot() is None


# ---------------- 对外快照 ----------------

def test_snapshot_is_none_before_first_fetch():
    assert fx.snapshot() is None


def test_stale_rate_is_withheld(monkeypatch):
    """过期的宁可不显示：摆一个来路不明的陈旧数字比空着更糟。"""
    use(monkeypatch, {"BUY": ads(6.66), "SELL": ads(6.66)})
    asyncio.run(fx.refresh())
    assert fx.snapshot() is not None

    fx._at = time.time() - fx.MAX_AGE - 1
    assert fx.snapshot() is None


def test_snapshot_shape(monkeypatch):
    use(monkeypatch, {"BUY": ads(6.66), "SELL": ads(6.66)})
    asyncio.run(fx.refresh())
    snap = fx.snapshot()
    assert set(snap) == {"cny", "ageSec", "source"}
    assert snap["source"] == "binance-c2c"
    assert snap["ageSec"] < 5


def test_disabled_source_starts_nothing(monkeypatch):
    """置空 PERPDESK_FX_SOURCE 就完全不起后台任务。"""
    monkeypatch.setattr(fx.config, "FX_SOURCE", "")
    asyncio.run(fx.start())
    assert fx._task is None


def test_overview_carries_fx_field():
    """权益旁边的人民币靠这个字段；它必须挂在 overview 上，
    而不是另开一个请求 —— 那个端点前端是 2 秒轮询一次的。"""
    import inspect

    from backend.app.routers import account as account_router

    src = inspect.getsource(account_router.overview)
    assert 'snap["fx"] = fx.snapshot()' in src
