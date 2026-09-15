"""接口层的账户隔离测试。

账户维度要穿过 路由 → 持久层 → 盈亏引擎 三层，任何一层漏传都会让
两个账户的数据串在一起。而串了之后接口照常返回 200，只是数字不对——
这正是必须用测试盯住的那类问题。

刻意不走完整的 app（lifespan 会连交易所），只挂路由。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def api(tmp_path, monkeypatch):
    from backend.app import config, db, vault
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "perpdesk.db")
    monkeypatch.setattr(config, "MASTER_KEY_PATH", tmp_path / ".master.key")
    monkeypatch.setattr(config, "MASTER_KEY_ENV", "")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "none.env")
    db.close(); vault.reset_cache(); db.connect()

    from backend.app.routers import account as account_router
    from backend.app.routers import portfolio as portfolio_router
    portfolio_router._calc_cache.clear()

    app = FastAPI()
    app.include_router(portfolio_router.router)
    app.include_router(account_router.router)
    with TestClient(app) as client:
        yield client, db
    db.close(); vault.reset_cache()


def _fill(db, account_id, symbol, buy, sell, fee=0.0):
    db.add_trade(symbol, "BUY", 1, buy, fee, 1_700_000_000_000, "t", account_id=account_id)
    db.add_trade(symbol, "SELL", 1, sell, fee, 1_700_000_100_000, "t", account_id=account_id)


# ---------------------------------------------------------------- 隔离

def test_summary_is_isolated_per_account(api):
    client, db = api
    a = db.default_account_id()
    b = db.add_account("binance", "二号")
    _fill(db, a, "BTCUSDT", 100, 150)      # +50
    _fill(db, b, "ETHUSDT", 100, 80)       # -20

    sa = client.get("/api/portfolio/summary", params={"account_id": a}).json()["summary"]
    sb = client.get("/api/portfolio/summary", params={"account_id": b}).json()["summary"]

    assert sa["totalRealized"] == pytest.approx(50.0)
    assert sb["totalRealized"] == pytest.approx(-20.0)
    assert (sa["accountId"], sb["accountId"]) == (a, b)


def test_omitting_account_falls_back_to_the_default(api):
    client, db = api
    a = db.default_account_id()
    b = db.add_account("binance", "二号")
    _fill(db, a, "BTCUSDT", 100, 150)
    _fill(db, b, "ETHUSDT", 100, 80)

    default = client.get("/api/portfolio/summary").json()["summary"]
    assert default["accountId"] == a
    assert default["totalRealized"] == pytest.approx(50.0)


def test_trade_listing_is_isolated(api):
    client, db = api
    a = db.default_account_id()
    b = db.add_account("binance", "二号")
    _fill(db, a, "BTCUSDT", 100, 150)

    assert client.get("/api/portfolio/trades", params={"account_id": a}).json()["total"] == 2
    assert client.get("/api/portfolio/trades", params={"account_id": b}).json()["total"] == 0


def test_curve_is_isolated(api):
    client, db = api
    a = db.default_account_id()
    b = db.add_account("binance", "二号")
    _fill(db, a, "BTCUSDT", 100, 150)
    _fill(db, b, "ETHUSDT", 100, 80)

    ca = client.get("/api/portfolio/curve", params={"account_id": a}).json()
    cb = client.get("/api/portfolio/curve", params={"account_id": b}).json()
    assert ca["final"] == pytest.approx(50.0)
    assert cb["final"] == pytest.approx(-20.0)


def test_replay_cache_does_not_bleed_between_accounts(api):
    """两个账户共用缓存键的话，先查的那个结果会被后查的账户读到。"""
    client, db = api
    a = db.default_account_id()
    b = db.add_account("binance", "二号")
    _fill(db, a, "BTCUSDT", 100, 150)
    _fill(db, b, "ETHUSDT", 100, 80)

    for _ in range(3):        # 交替查，逼出串号
        assert client.get("/api/portfolio/summary",
                          params={"account_id": a}).json()["summary"]["totalRealized"] == pytest.approx(50.0)
        assert client.get("/api/portfolio/summary",
                          params={"account_id": b}).json()["summary"]["totalRealized"] == pytest.approx(-20.0)


def test_writing_to_one_account_invalidates_only_its_own_cache(api):
    client, db = api
    a = db.default_account_id()
    b = db.add_account("binance", "二号")
    _fill(db, a, "BTCUSDT", 100, 150)
    _fill(db, b, "ETHUSDT", 100, 80)
    client.get("/api/portfolio/summary", params={"account_id": a})
    client.get("/api/portfolio/summary", params={"account_id": b})

    _fill(db, a, "BTCUSDT", 10, 20)       # 只动 a
    sa = client.get("/api/portfolio/summary", params={"account_id": a}).json()["summary"]
    sb = client.get("/api/portfolio/summary", params={"account_id": b}).json()["summary"]
    assert sa["totalRealized"] == pytest.approx(60.0), "a 的缓存没失效"
    assert sb["totalRealized"] == pytest.approx(-20.0), "b 的结果被 a 的写入带歪了"


# ---------------------------------------------------------------- 校验

def test_unknown_account_is_404_not_a_silent_fallback(api):
    """落回默认账户就等于拿别人的数据当自己的，必须报错。"""
    client, _ = api
    for path in ("/api/portfolio/summary", "/api/portfolio/curve",
                 "/api/portfolio/trades", "/api/account/status"):
        assert client.get(path, params={"account_id": 999}).status_code == 404, path


def test_delete_cannot_cross_accounts(api):
    client, db = api
    a = db.default_account_id()
    b = db.add_account("binance", "二号")
    tid = db.add_trade("BTCUSDT", "BUY", 1, 100, 0, 1_700_000_000_000, "t", account_id=a)

    assert client.delete(f"/api/portfolio/trades/{tid}", params={"account_id": b}).status_code == 404
    assert client.get("/api/portfolio/trades", params={"account_id": a}).json()["total"] == 1
    assert client.delete(f"/api/portfolio/trades/{tid}", params={"account_id": a}).status_code == 200


def test_manual_trade_lands_in_the_requested_account(api):
    client, db = api
    b = db.add_account("binance", "二号")
    client.post("/api/portfolio/trades", params={"account_id": b}, json={
        "symbol": "SOLUSDT", "side": "BUY", "qty": 1, "price": 10,
        "fee": 0, "date": "2026-01-01 00:00:00", "note": "手工",
    })
    assert client.get("/api/portfolio/trades", params={"account_id": b}).json()["total"] == 1
    assert client.get("/api/portfolio/trades").json()["total"] == 0


# ---------------------------------------------------------------- 账户列表

def test_accounts_listing_never_exposes_plaintext(api):
    client, db = api
    from backend.app import vault
    secret = "TfEQovGwxgkHZeyml4FmzUtkxbzkYZYzaEhNsCFFQITuURwGLBbkfMaOvBmevjJh"
    vault.put(db.default_account_id(), "api_secret", secret)

    body = client.get("/api/account/accounts").text
    assert secret not in body, "账户列表把明文 secret 吐出来了"
    rows = client.get("/api/account/accounts").json()["rows"]
    assert rows[0]["credentials"]["api_secret"].count("•") == 8


def test_accounts_listing_reports_trade_counts_per_account(api):
    client, db = api
    a = db.default_account_id()
    b = db.add_account("binance", "二号")
    _fill(db, a, "BTCUSDT", 100, 150)

    rows = {r["id"]: r for r in client.get("/api/account/accounts").json()["rows"]}
    assert rows[a]["trades"] == 2
    assert rows[b]["trades"] == 0


def test_cache_key_distinguishes_accounts_even_when_versions_collide(api, monkeypatch):
    """白盒：缓存键必须含账户号。

    黑盒测不出来——trades.id 是全局自增的，两个账户的 (笔数, 最大 id) 天然不会
    撞，只有都为空时才撞，而那时结果本来就一样。但这层"撞不上"靠的是 id 全局
    唯一这个当前实现细节，不是缓存该依赖的东西。这里把版本号钉成常量，
    强行制造撞键，验证账户号确实在键里起作用。
    """
    client, db = api
    from backend.app import db as dbmod
    a = db.default_account_id()
    b = db.add_account("binance", "二号")
    _fill(db, a, "BTCUSDT", 100, 150)      # +50
    _fill(db, b, "ETHUSDT", 100, 80)       # -20

    monkeypatch.setattr(dbmod, "trades_version", lambda account_id=None: (1, 1))

    sa = client.get("/api/portfolio/summary", params={"account_id": a}).json()["summary"]
    sb = client.get("/api/portfolio/summary", params={"account_id": b}).json()["summary"]
    assert sa["totalRealized"] == pytest.approx(50.0)
    assert sb["totalRealized"] == pytest.approx(-20.0), "版本号相同时两账户共用了缓存"


def test_cache_eviction_only_drops_the_written_account(api):
    """淘汰要按账户号筛。键里第一位不是账户号的话，这个筛选恒不命中，
    缓存就只增不减——不会算错，但会一直涨。"""
    client, db = api
    from backend.app.routers import portfolio as pf
    a = db.default_account_id()
    b = db.add_account("binance", "二号")
    _fill(db, a, "BTCUSDT", 100, 150)
    _fill(db, b, "ETHUSDT", 100, 80)
    client.get("/api/portfolio/summary", params={"account_id": a})
    client.get("/api/portfolio/summary", params={"account_id": b})
    assert len(pf._calc_cache) == 2

    _fill(db, a, "BTCUSDT", 10, 20)                       # 只动 a
    client.get("/api/portfolio/summary", params={"account_id": a})

    keys = list(pf._calc_cache)
    assert all(k[0] in (a, b) for k in keys), "缓存键第一位不是账户号"
    assert len([k for k in keys if k[0] == a]) == 1, "a 的旧条目没被淘汰"
    assert len([k for k in keys if k[0] == b]) == 1, "b 的条目被误删了"
