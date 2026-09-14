"""版本化数据库迁移。

为什么不能继续用 ad-hoc 的 `PRAGMA table_info` 打补丁：自己用时库就在手边，
改坏了能看能修；一旦别人把自己的交易历史放进来，那个库你既看不到、也没法
手工修。迁移必须有序、可重放、失败整体回滚，并且在动存量数据之前先留备份。

版本号存在 `PRAGMA user_version`（SQLite 文件头里的 32 位整数）而不是自建表：
它跟着数据库文件走，不会被 DROP TABLE 误删，读写也是事务性的（已实测：
BEGIN 里改完 ROLLBACK 会退回原值）。`_migrations` 表只是给人看的执行日志，
任何判断都不读它。

加新迁移的规矩：
  * 只在列表末尾追加，**永远不要改已发布的那几条** —— 别人的库已经按老版本
    跑过了，改了等于两边 schema 悄悄分叉；
  * 每条都要能在"已经是目标状态"的库上安全重跑（IF NOT EXISTS / 先查后改）；
  * 需要回填数据的迁移，把回填写在同一条里，跟 DDL 共享一个事务。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("tradview.db")

Apply = Callable[[sqlite3.Connection], None]


class MigrationError(RuntimeError):
    """迁移失败。调用方应当让进程停下，而不是带着半迁移的库继续跑。"""


# ---------------------------------------------------------------- 各版本

def _exec_all(conn: sqlite3.Connection, statements: tuple[str, ...]) -> None:
    """逐条执行。

    **不要用 conn.executescript()** —— 它会在执行前隐式 COMMIT 掉当前事务，
    外层 BEGIN 就此丢失，迁移不再是原子的：中途失败会留下半建的表，而
    user_version 还停在旧值，下次启动又从头跑一遍。这条是测试里抓出来的。
    """
    for sql in statements:
        conn.execute(sql)


_BASELINE = (
    """CREATE TABLE IF NOT EXISTS watchlist (
        symbol     TEXT PRIMARY KEY,
        sort_order INTEGER NOT NULL DEFAULT 0,
        added_at   INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS trades (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol     TEXT NOT NULL,
        side       TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        qty        REAL NOT NULL CHECK (qty > 0),
        price      REAL NOT NULL CHECK (price >= 0),
        fee        REAL NOT NULL DEFAULT 0,
        traded_at  INTEGER NOT NULL,
        note       TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        -- 交易所给的每笔已实现盈亏。优先用它而不是本地回放：账户可能用过
        -- 双向持仓模式（同一标的同时持多空），净额加权平均法算不对。
        -- 手工录入的成交没有这个值，留 NULL，由回放补算。
        realized_pnl REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_trades_symbol_time ON trades (symbol, traded_at)",
    # 交易所资金流水：资金费、已实现盈亏、手续费等。资金费不体现在成交记录里，
    # 但对长期持仓（尤其高杠杆）是实打实的损益，必须单独同步，
    # 否则盈亏统计会系统性偏离交易所口径。
    """CREATE TABLE IF NOT EXISTS income (
        tran_id  TEXT PRIMARY KEY,
        symbol   TEXT NOT NULL DEFAULT '',
        type     TEXT NOT NULL,
        amount   REAL NOT NULL,
        asset    TEXT NOT NULL DEFAULT '',
        ts       INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_income_type_time ON income (type, ts)",
    "CREATE INDEX IF NOT EXISTS idx_income_symbol ON income (symbol)",
)


def _m001_baseline(conn: sqlite3.Connection) -> None:
    """基线：迁移机制引入之前，由一条 executescript 建出来的那套表。

    全部 IF NOT EXISTS —— 已经在跑的库走到这里应当是无害的空操作，
    只把 user_version 抬到基线，不重建任何东西。
    """
    _exec_all(conn, _BASELINE)


def _m002_trades_realized_pnl(conn: sqlite3.Connection) -> None:
    """给更早的库补 trades.realized_pnl。

    这一列 2026-09 才进 schema，在那之前建的库里没有。原先靠每次连接时
    PRAGMA 查一遍补列，现在收编进迁移序列。基线里已经带了这一列，所以
    新库走到这儿是空操作 —— 这条只为老库存在。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    if "realized_pnl" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN realized_pnl REAL")


_M003_ACCOUNTS = (
    """CREATE TABLE IF NOT EXISTS accounts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        exchange   TEXT    NOT NULL,              -- 'binance'
        market     TEXT    NOT NULL DEFAULT 'usdm',  -- 'usdm' / 'coinm' / 'spot'
        label      TEXT    NOT NULL,              -- 界面上显示的名字
        enabled    INTEGER NOT NULL DEFAULT 1,    -- 停用而不删除，历史成交要留着
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    )""",
)

# trades / income 都要重建而不是 ALTER：SQLite 不允许 ADD COLUMN 同时带
# NOT NULL 和 REFERENCES（"Cannot add a REFERENCES column with non-NULL
# default value"），而这两个约束都想要 —— NOT NULL 让漏传 account_id 的写入
# 当场失败而不是悄悄落到 1 号账户，外键防止删账户留下一堆孤儿成交。
# income 另有一层：主键必须从 tran_id 改成 (account_id, tran_id)，
# 交易所的流水号只在单账户内唯一，换个账户就可能撞。
_M003_TRADES = (
    """CREATE TABLE trades_m003 (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL REFERENCES accounts(id),
        symbol     TEXT NOT NULL,
        side       TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        qty        REAL NOT NULL CHECK (qty > 0),
        price      REAL NOT NULL CHECK (price >= 0),
        fee        REAL NOT NULL DEFAULT 0,
        traded_at  INTEGER NOT NULL,
        note       TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        realized_pnl REAL
    )""",
    """INSERT INTO trades_m003
           (id, account_id, symbol, side, qty, price, fee, traded_at, note,
            created_at, realized_pnl)
       SELECT id, 1, symbol, side, qty, price, fee, traded_at, note,
              created_at, realized_pnl
         FROM trades""",
    "DROP TABLE trades",
    "ALTER TABLE trades_m003 RENAME TO trades",
    "CREATE INDEX idx_trades_acct_symbol_time ON trades (account_id, symbol, traded_at)",
)

_M003_INCOME = (
    """CREATE TABLE income_m003 (
        account_id INTEGER NOT NULL REFERENCES accounts(id),
        tran_id  TEXT NOT NULL,
        symbol   TEXT NOT NULL DEFAULT '',
        type     TEXT NOT NULL,
        amount   REAL NOT NULL,
        asset    TEXT NOT NULL DEFAULT '',
        ts       INTEGER NOT NULL,
        PRIMARY KEY (account_id, tran_id)
    )""",
    """INSERT INTO income_m003 (account_id, tran_id, symbol, type, amount, asset, ts)
       SELECT 1, tran_id, symbol, type, amount, asset, ts FROM income""",
    "DROP TABLE income",
    "ALTER TABLE income_m003 RENAME TO income",
    "CREATE INDEX idx_income_acct_type_time ON income (account_id, type, ts)",
    "CREATE INDEX idx_income_acct_symbol ON income (account_id, symbol)",
)


def _m003_accounts(conn: sqlite3.Connection) -> None:
    """引入账户维度。

    在此之前 trades / income 里的每一行都隐式属于"backend/.env 里配的那个
    币安账户"。存量数据全部归到 1 号账户 —— 这一步能干净地做，正是因为
    此刻只存在一个账户，不存在归属判断；等两个账户的成交混在一起就晚了。

    凭据不放进这张表：那要先有加密存储，是下一步的事。现在 1 号账户仍然
    读 backend/.env。
    """
    if "account_id" in {r[1] for r in conn.execute("PRAGMA table_info(trades)")}:
        return  # 已经是目标状态（迁移必须能安全重跑）

    _exec_all(conn, _M003_ACCOUNTS)
    if not conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
        conn.execute(
            "INSERT INTO accounts (id, exchange, market, label, created_at) "
            "VALUES (1, 'binance', 'usdm', ?, ?)",
            ("Binance U 本位", int(time.time() * 1000)),
        )
    _exec_all(conn, _M003_TRADES)
    _exec_all(conn, _M003_INCOME)


MIGRATIONS: list[tuple[int, str, Apply]] = [
    (1, "baseline", _m001_baseline),
    (2, "trades.realized_pnl", _m002_trades_realized_pnl),
    (3, "accounts", _m003_accounts),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


# ---------------------------------------------------------------- 版本查询

def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def pending(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """还没跑的迁移，(版本号, 名字)。只读，不产生副作用。"""
    have = current_version(conn)
    return [(v, n) for v, n, _ in MIGRATIONS if v > have]


# ---------------------------------------------------------------- 备份

def _has_rows(conn: sqlite3.Connection) -> bool:
    """库里有没有值得备份的东西。全新库不必备份。"""
    for table in ("trades", "income"):
        try:
            if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return True
        except sqlite3.OperationalError:
            pass  # 表还不存在，说明是新库
    return False


def _prune_backups(db_path: Path, keep: int) -> None:
    old = sorted(db_path.parent.glob(f"{db_path.stem}.bak-v*{db_path.suffix}"))
    for p in old[:-keep] if keep > 0 else old:
        p.unlink(missing_ok=True)


def backup(conn: sqlite3.Connection, db_path: Path, keep: int = 5) -> Path:
    """迁移前的一致性快照。

    用 VACUUM INTO 而不是 shutil.copy：WAL 模式下直接拷主文件会漏掉还没
    checkpoint 的事务，拷出来是个撕裂的库 —— 而那恰恰是你最需要它的时候。
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = db_path.with_name(
        f"{db_path.stem}.bak-v{current_version(conn)}-{ts}{db_path.suffix}")
    conn.execute("VACUUM INTO ?", (str(dst),))
    _prune_backups(db_path, keep)
    return dst


# ---------------------------------------------------------------- 执行

def _record(conn: sqlite3.Connection, ver: int, name: str, ms: float) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at INTEGER NOT NULL,
            took_ms    INTEGER NOT NULL
        )""")
    conn.execute(
        "INSERT OR REPLACE INTO _migrations VALUES (?, ?, ?, ?)",
        (ver, name, int(time.time() * 1000), int(ms)),
    )


def run(conn: sqlite3.Connection, db_path: Path | None = None) -> list[tuple[int, str]]:
    """把库迁到 SCHEMA_VERSION，返回本次实际执行的迁移。

    db_path 给了才会在动存量数据前备份（测试用内存库时传 None）。
    """
    have = current_version(conn)
    if have > SCHEMA_VERSION:
        # 降级运行是真实场景：用户 git pull 到新版建了新库，又切回旧 tag。
        # 旧代码不认识新 schema，硬跑下去就是静默写坏数据，不如直接停。
        raise MigrationError(
            f"数据库版本 v{have} 高于本程序支持的 v{SCHEMA_VERSION}，"
            "多半是用旧版本程序打开了新版本建的库。请升级程序后再启动。"
        )

    todo = [m for m in MIGRATIONS if m[0] > have]
    if not todo:
        return []

    saved = conn.isolation_level
    conn.isolation_level = None  # 交出事务控制权，下面显式 BEGIN/COMMIT
    applied: list[tuple[int, str]] = []
    try:
        if db_path is not None and _has_rows(conn):
            log.warning("数据库需要迁移 v%d → v%d，先备份到 %s",
                        have, SCHEMA_VERSION, backup(conn, db_path))

        for ver, name, fn in todo:
            t0 = time.perf_counter()
            conn.execute("BEGIN IMMEDIATE")
            try:
                fn(conn)
                # PRAGMA 的值不能用占位符绑定；ver 来自本模块的常量列表，非外部输入
                conn.execute(f"PRAGMA user_version = {int(ver)}")
                conn.execute("COMMIT")
            except Exception as exc:
                conn.execute("ROLLBACK")
                raise MigrationError(f"迁移 v{ver}({name}) 失败，已整体回滚：{exc}") from exc
            took = (time.perf_counter() - t0) * 1000
            _record(conn, ver, name, took)
            applied.append((ver, name))
            log.info("数据库迁移 v%d %s 完成（%.0f ms）", ver, name, took)
    finally:
        conn.isolation_level = saved
    return applied
