"""持久层的账户隔离测试。

_m003 把 account_id 穿进了每一条读写路径。漏掉任何一处的后果都是静默的：
要么两个账户的成交混在一起算盈亏，要么去重把别人的成交当成重复丢掉 ——
都不会报错，只会让数字慢慢变得不对。
"""

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    """每个用例一个独立的库。"""
    from backend.app import config, db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "perpdesk.db")
    db.close()                      # 丢掉上一个用例的连接与默认账户缓存
    db.connect()
    yield db
    db.close()


def _trade(db, symbol="BTCUSDT", note="", account_id=None, at=1000):
    return db.add_trade(symbol, "BUY", 1.0, 100.0, 0.0, at, note, account_id=account_id)


# ---------------------------------------------------------------- 账户本身

def test_migration_leaves_exactly_one_default_account(store):
    accounts = store.list_accounts()
    assert len(accounts) == 1
    assert accounts[0]["exchange"] == "binance"
    assert store.default_account_id() == accounts[0]["id"]


def test_default_account_prefers_lowest_sort_order(store):
    new_id = store.add_account("okx", "OKX 主账户", sort_order=-1)
    assert store.default_account_id() == new_id, "add_account 之后默认账户缓存没失效"


def test_disabled_account_is_not_the_default(store):
    store.add_account("okx", "停用的", sort_order=-5)
    store.connect().execute("UPDATE accounts SET enabled = 0 WHERE exchange = 'okx'")
    store.connect().commit()
    store._default_account = None
    assert store.get_account(store.default_account_id())["exchange"] == "binance"


# ---------------------------------------------------------------- 成交隔离

def test_trades_are_scoped_to_their_account(store):
    a = store.default_account_id()
    b = store.add_account("binance", "二号账户")
    _trade(store, "BTCUSDT", account_id=a)
    _trade(store, "ETHUSDT", account_id=b)
    _trade(store, "SOLUSDT", account_id=b)

    assert [t["symbol"] for t in store.list_trades(account_id=a)] == ["BTCUSDT"]
    assert {t["symbol"] for t in store.list_trades(account_id=b)} == {"ETHUSDT", "SOLUSDT"}
    assert store.page_trades(account_id=a)[1] == 1
    assert store.page_trades(account_id=b)[1] == 2


def test_trades_version_is_per_account(store):
    """两个账户共用版本号的话，A 同步完会把 B 的盈亏缓存一起顶掉。"""
    a = store.default_account_id()
    b = store.add_account("binance", "二号账户")
    _trade(store, account_id=a)
    before_b = store.trades_version(account_id=b)

    _trade(store, account_id=a)

    assert store.trades_version(account_id=b) == before_b, "B 的版本号被 A 的写入带动了"
    assert store.trades_version(account_id=a) != before_b


def test_dedup_notes_do_not_leak_across_accounts(store):
    """交易所的 tradeId 只在单账户内唯一。

    跨账户去重会把第二个账户的成交当成"已同步"直接跳过 —— 那个账户的
    历史就永远缺一块，而且同步时显示"跳过 N 条重复"，看上去一切正常。
    """
    a = store.default_account_id()
    b = store.add_account("binance", "二号账户")
    _trade(store, note="binance:12345", account_id=a)

    assert store.existing_trade_notes(account_id=a) == {"binance:12345"}
    assert store.existing_trade_notes(account_id=b) == set(), "B 账户看到了 A 的去重标记"


def test_delete_trade_cannot_cross_accounts(store):
    """id 是全局自增的，不限定账户就能用猜到的 id 删掉别的账户的成交。"""
    a = store.default_account_id()
    b = store.add_account("binance", "二号账户")
    tid = _trade(store, account_id=a)

    assert store.delete_trade(tid, account_id=b) is False
    assert store.page_trades(account_id=a)[1] == 1
    assert store.delete_trade(tid, account_id=a) is True


def test_backfill_realized_pnl_is_scoped(store):
    a = store.default_account_id()
    b = store.add_account("binance", "二号账户")
    _trade(store, note="binance:1", account_id=a)
    _trade(store, note="binance:1", account_id=b)

    store.backfill_realized_pnl([(7.5, "binance:1")], account_id=a)

    assert store.list_trades(account_id=a)[0]["realized_pnl"] == 7.5
    assert store.list_trades(account_id=b)[0]["realized_pnl"] is None


def test_bulk_insert_lands_in_the_right_account(store):
    b = store.add_account("binance", "二号账户")
    store.add_trades_bulk(
        [("BTCUSDT", "BUY", 1.0, 100.0, 0.0, 1000, "bulk", 3.3)], account_id=b)
    assert store.list_trades(account_id=b)[0]["realized_pnl"] == 3.3
    assert store.list_trades()[0:1] == []


# ---------------------------------------------------------------- 流水隔离

def test_income_dedup_is_per_account(store):
    """同号流水在不同账户下必须各存一份，否则资金费会算少还不报错。"""
    a = store.default_account_id()
    b = store.add_account("binance", "二号账户")
    row = ("tran-1", "BTCUSDT", "FUNDING_FEE", -1.5, "USDT", 1000)

    assert store.upsert_income([row], account_id=a) == 1
    assert store.upsert_income([row], account_id=a) == 0, "同账户内应当幂等"
    assert store.upsert_income([row], account_id=b) == 1, "跨账户同号流水被吞了"

    assert store.income_by_type(account_id=a) == {"FUNDING_FEE": -1.5}
    assert store.income_by_type(account_id=b) == {"FUNDING_FEE": -1.5}


def test_income_aggregates_are_scoped(store):
    a = store.default_account_id()
    b = store.add_account("binance", "二号账户")
    store.upsert_income([("x1", "BTCUSDT", "FUNDING_FEE", -1.0, "USDT", 1000)], account_id=a)
    store.upsert_income([("y1", "ETHUSDT", "FUNDING_FEE", -9.0, "USDT", 1000)], account_id=b)

    assert store.income_totals(account_id=a) == {"BTCUSDT": {"FUNDING_FEE": -1.0}}
    assert store.income_symbols(account_id=a) == ["BTCUSDT"]
    assert store.income_symbols(account_id=b) == ["ETHUSDT"]


# ---------------------------------------------------------------- 默认行为

def test_account_id_none_means_the_default_account(store):
    """调用方不传 account_id 时落到默认账户，老代码路径不受影响。"""
    a = store.default_account_id()
    _trade(store, "BTCUSDT")                      # 不传
    assert store.list_trades(account_id=a)[0]["symbol"] == "BTCUSDT"
    assert store.page_trades()[1] == 1
