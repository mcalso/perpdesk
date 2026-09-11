"""SQLite 持久层：自选列表 + 交易流水。本地自用，单文件即可。"""
import sqlite3
import time
from typing import Any

from . import config

_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    symbol     TEXT PRIMARY KEY,
    sort_order INTEGER NOT NULL DEFAULT 0,
    added_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty        REAL NOT NULL CHECK (qty > 0),
    price      REAL NOT NULL CHECK (price >= 0),
    fee        REAL NOT NULL DEFAULT 0,
    traded_at  INTEGER NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol_time ON trades (symbol, traded_at);

-- 交易所资金流水：资金费、已实现盈亏、手续费等。
-- 资金费不体现在成交记录里，但对长期持仓（尤其高杠杆）是实打实的损益，
-- 必须单独同步，否则盈亏统计会系统性偏离交易所口径。
CREATE TABLE IF NOT EXISTS income (
    tran_id  TEXT PRIMARY KEY,
    symbol   TEXT NOT NULL DEFAULT '',
    type     TEXT NOT NULL,
    amount   REAL NOT NULL,
    asset    TEXT NOT NULL DEFAULT '',
    ts       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_income_type_time ON income (type, ts);
CREATE INDEX IF NOT EXISTS idx_income_symbol ON income (symbol);
"""


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _seed_watchlist(_conn)
        _conn.commit()
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def _seed_watchlist(conn: sqlite3.Connection) -> None:
    (count,) = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()
    if count:
        return
    now = int(time.time() * 1000)
    conn.executemany(
        "INSERT INTO watchlist (symbol, sort_order, added_at) VALUES (?, ?, ?)",
        [(s, i, now) for i, s in enumerate(config.DEFAULT_WATCHLIST)],
    )


# ---------- watchlist ----------

def list_watchlist() -> list[str]:
    rows = connect().execute(
        "SELECT symbol FROM watchlist ORDER BY sort_order, added_at"
    ).fetchall()
    return [r["symbol"] for r in rows]


def add_watch(symbol: str) -> None:
    conn = connect()
    (nxt,) = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM watchlist"
    ).fetchone()
    conn.execute(
        "INSERT OR IGNORE INTO watchlist (symbol, sort_order, added_at) VALUES (?, ?, ?)",
        (symbol, nxt, int(time.time() * 1000)),
    )
    conn.commit()


def remove_watch(symbol: str) -> None:
    conn = connect()
    conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
    conn.commit()


def reorder_watchlist(symbols: list[str]) -> None:
    conn = connect()
    conn.executemany(
        "UPDATE watchlist SET sort_order = ? WHERE symbol = ?",
        [(i, s) for i, s in enumerate(symbols)],
    )
    conn.commit()


# ---------- trades ----------

def list_trades(symbol: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM trades"
    args: tuple = ()
    if symbol:
        sql += " WHERE symbol = ?"
        args = (symbol,)
    sql += " ORDER BY traded_at, id"
    return [dict(r) for r in connect().execute(sql, args).fetchall()]


def add_trade(
    symbol: str, side: str, qty: float, price: float, fee: float, traded_at: int, note: str
) -> int:
    conn = connect()
    cur = conn.execute(
        """INSERT INTO trades (symbol, side, qty, price, fee, traded_at, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (symbol, side, qty, price, fee, traded_at, note, int(time.time() * 1000)),
    )
    conn.commit()
    return int(cur.lastrowid)


def add_trades_bulk(rows: list[tuple]) -> int:
    """rows: (symbol, side, qty, price, fee, traded_at, note)"""
    conn = connect()
    now = int(time.time() * 1000)
    cur = conn.executemany(
        """INSERT INTO trades (symbol, side, qty, price, fee, traded_at, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [(*r, now) for r in rows],
    )
    conn.commit()
    return cur.rowcount


# ---------- income ----------

def upsert_income(rows: list[tuple]) -> int:
    """rows: (tran_id, symbol, type, amount, asset, ts)。按 tran_id 幂等。"""
    conn = connect()
    cur = conn.executemany(
        """INSERT OR IGNORE INTO income (tran_id, symbol, type, amount, asset, ts)
           VALUES (?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    return cur.rowcount


def income_totals() -> dict[str, dict[str, float]]:
    """按 symbol 汇总各科目金额。"""
    out: dict[str, dict[str, float]] = {}
    for r in connect().execute(
        "SELECT symbol, type, SUM(amount) AS total FROM income GROUP BY symbol, type"
    ):
        out.setdefault(r["symbol"], {})[r["type"]] = r["total"]
    return out


def income_by_type() -> dict[str, float]:
    return {
        r["type"]: r["total"]
        for r in connect().execute(
            "SELECT type, SUM(amount) AS total FROM income GROUP BY type"
        )
    }


def income_symbols() -> list[str]:
    """所有出现过流水的标的——用它反查有交易历史的 symbol，含已平仓的。"""
    return [
        r["symbol"]
        for r in connect().execute(
            "SELECT DISTINCT symbol FROM income WHERE symbol != '' ORDER BY symbol"
        )
    ]


def delete_trade(trade_id: int) -> bool:
    conn = connect()
    cur = conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
    conn.commit()
    return cur.rowcount > 0
