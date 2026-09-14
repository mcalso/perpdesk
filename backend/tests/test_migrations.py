"""迁移机制的测试。

重点不在"能不能建表"，而在出事的路径：迁移中途失败、版本倒挂、
存量数据有没有被备份。这些都是别人的交易历史，错一次就没了。
"""
import sqlite3

import pytest

from backend.app import migrations


def _open(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(conn, table) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "perpdesk.db"
    conn = _open(path)
    yield conn, path
    conn.close()


# ---------------------------------------------------------------- 正常路径

def test_fresh_database_migrates_to_head(db):
    conn, path = db
    applied = migrations.run(conn, path)

    assert [v for v, _ in applied] == [v for v, _, _ in migrations.MIGRATIONS]
    assert migrations.current_version(conn) == migrations.SCHEMA_VERSION
    assert {"watchlist", "trades", "income"} <= _tables(conn)
    assert "realized_pnl" in _cols(conn, "trades")


def test_running_twice_is_a_noop(db):
    conn, path = db
    migrations.run(conn, path)
    assert migrations.run(conn, path) == []
    assert migrations.pending(conn) == []


def test_migration_log_records_what_ran(db):
    conn, path = db
    migrations.run(conn, path)
    rows = conn.execute("SELECT version, name FROM _migrations ORDER BY version").fetchall()
    assert [r["version"] for r in rows] == [v for v, _, _ in migrations.MIGRATIONS]


def test_pending_is_read_only(db):
    """pending() 只是查询，不能顺手把库改了。"""
    conn, path = db
    before = migrations.current_version(conn)
    assert len(migrations.pending(conn)) == len(migrations.MIGRATIONS)
    assert migrations.current_version(conn) == before
    assert _tables(conn) == set()


# ---------------------------------------------------------------- 存量库接管

LEGACY_SCHEMA = """
CREATE TABLE watchlist (
    symbol TEXT PRIMARY KEY, sort_order INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL);
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    qty REAL NOT NULL CHECK (qty > 0), price REAL NOT NULL CHECK (price >= 0),
    fee REAL NOT NULL DEFAULT 0, traded_at INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
CREATE TABLE income (
    tran_id TEXT PRIMARY KEY, symbol TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL, amount REAL NOT NULL,
    asset TEXT NOT NULL DEFAULT '', ts INTEGER NOT NULL);
"""


def test_adopts_legacy_database_without_losing_data(db):
    """迁移机制引入之前建的库：没有 user_version，也没有 realized_pnl 列。"""
    conn, path = db
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("""INSERT INTO trades
        (symbol, side, qty, price, fee, traded_at, note, created_at)
        VALUES ('BTCUSDT', 'BUY', 1.5, 60000, 0.1, 1000, '', 1000)""")
    conn.execute("INSERT INTO income VALUES ('t1', 'BTCUSDT', 'FUNDING_FEE', -0.3, 'USDT', 1000)")
    conn.commit()
    assert migrations.current_version(conn) == 0
    assert "realized_pnl" not in _cols(conn, "trades")

    migrations.run(conn, path)

    assert migrations.current_version(conn) == migrations.SCHEMA_VERSION
    assert "realized_pnl" in _cols(conn, "trades")
    row = conn.execute("SELECT * FROM trades").fetchone()
    assert (row["symbol"], row["qty"], row["price"]) == ("BTCUSDT", 1.5, 60000)
    assert row["realized_pnl"] is None
    assert conn.execute("SELECT COUNT(*) FROM income").fetchone()[0] == 1


def test_baseline_does_not_clobber_existing_rows(db):
    """基线全是 IF NOT EXISTS，跑在已有数据的库上必须是空操作。"""
    conn, path = db
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO watchlist VALUES ('ETHUSDT', 0, 1)")
    conn.commit()
    migrations.run(conn, path)
    assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 1


# ---------------------------------------------------------------- 备份

def test_backup_taken_before_touching_existing_data(db, tmp_path):
    conn, path = db
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("""INSERT INTO trades
        (symbol, side, qty, price, fee, traded_at, note, created_at)
        VALUES ('BTCUSDT', 'BUY', 1, 1, 0, 1, '', 1)""")
    conn.commit()

    migrations.run(conn, path)

    baks = list(tmp_path.glob("perpdesk.bak-v*.db"))
    assert len(baks) == 1, "有存量数据就必须留备份"
    # 备份得是个能打开、且数据还在的库，不能是撕裂的文件
    bak = _open(baks[0])
    assert bak.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert bak.execute("PRAGMA user_version").fetchone()[0] == 0, "备份应是迁移前的状态"
    bak.close()


def test_no_backup_for_empty_database(db, tmp_path):
    conn, path = db
    migrations.run(conn, path)
    assert list(tmp_path.glob("perpdesk.bak-v*.db")) == [], "新库不必备份"


def test_backups_are_pruned(db, tmp_path):
    conn, path = db
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("""INSERT INTO trades
        (symbol, side, qty, price, fee, traded_at, note, created_at)
        VALUES ('BTCUSDT', 'BUY', 1, 1, 0, 1, '', 1)""")
    conn.commit()
    conn.isolation_level = None
    for i in range(8):
        (tmp_path / f"perpdesk.bak-v0-2026010{i}_000000.db").write_bytes(b"")
    migrations.backup(conn, path, keep=3)
    assert len(list(tmp_path.glob("perpdesk.bak-v*.db"))) == 3


# ---------------------------------------------------------------- 失败路径

def test_failed_migration_rolls_back_everything(db, monkeypatch):
    """迁移中途炸了，DDL 和版本号都必须退回去 —— 不能留个半迁移的库。"""
    conn, path = db

    def _boom(c):
        c.execute("CREATE TABLE half_done (x)")
        raise RuntimeError("模拟迁移中途失败")

    monkeypatch.setattr(migrations, "MIGRATIONS",
                        [*migrations.MIGRATIONS, (99, "boom", _boom)])

    with pytest.raises(migrations.MigrationError, match="v99"):
        migrations.run(conn, path)

    assert migrations.current_version(conn) == migrations.SCHEMA_VERSION, "版本不该前进"
    assert "half_done" not in _tables(conn), "失败迁移建的表必须回滚掉"
    # 失败前成功的那几条应当已经落地
    assert {"watchlist", "trades", "income"} <= _tables(conn)


def test_refuses_database_from_a_newer_version(db):
    """用户 git pull 到新版建了库，又切回旧 tag —— 旧代码不认识新 schema。"""
    conn, path = db
    migrations.run(conn, path)
    conn.execute(f"PRAGMA user_version = {migrations.SCHEMA_VERSION + 5}")

    with pytest.raises(migrations.MigrationError, match="高于本程序支持"):
        migrations.run(conn, path)


def test_isolation_level_restored_after_failure(db, monkeypatch):
    """迁移期间会临时接管事务控制权，失败也得还回去。"""
    conn, path = db
    before = conn.isolation_level

    def _boom(c):
        raise RuntimeError("boom")

    monkeypatch.setattr(migrations, "MIGRATIONS", [(1, "boom", _boom)])
    with pytest.raises(migrations.MigrationError):
        migrations.run(conn, path)
    assert conn.isolation_level == before


def test_every_migration_stays_inside_its_transaction(db, monkeypatch):
    """守卫：迁移函数里不能用 executescript。

    它会在执行前隐式 COMMIT 掉外层事务，迁移就不再是原子的 —— 中途失败会
    留下半建的表，而 user_version 还停在旧值，下次启动又从头跑一遍。
    这个坑是初版基线真踩过的，留个测试别再踩第二次。
    """
    conn, path = db
    still_in_tx: list[bool] = []

    def guard(fn):
        def inner(c):
            fn(c)
            still_in_tx.append(c.in_transaction)
        return inner

    monkeypatch.setattr(migrations, "MIGRATIONS",
                        [(v, n, guard(f)) for v, n, f in migrations.MIGRATIONS])
    migrations.run(conn, path)

    assert len(still_in_tx) == len(migrations.MIGRATIONS)
    assert all(still_in_tx), "某条迁移跑完后事务已经没了，多半用了 executescript"
