"""SQLite 持久层：自选列表 + 交易流水。本地自用，单文件即可。"""
import sqlite3
import time
from typing import Any

from . import config, migrations

_conn: sqlite3.Connection | None = None
_default_account: int | None = None


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        # 建表与后续所有 schema 变更都归迁移管，这里不再有第二份 schema 定义
        migrations.run(_conn, config.DB_PATH)
        _seed_watchlist(_conn)
        _conn.commit()
    return _conn


def close() -> None:
    global _conn, _default_account
    if _conn is not None:
        _conn.close()
        _conn = None
    _default_account = None      # 换库后默认账户可能不同，别把上一个库的缓存带过去


def _seed_watchlist(conn: sqlite3.Connection) -> None:
    (count,) = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()
    if count:
        return
    now = int(time.time() * 1000)
    conn.executemany(
        "INSERT INTO watchlist (symbol, sort_order, added_at) VALUES (?, ?, ?)",
        [(s, i, now) for i, s in enumerate(config.DEFAULT_WATCHLIST)],
    )


# ---------- accounts ----------
#
# 所有成交与流水都挂在某个账户下。约定：account_id 传 None 表示"默认账户"
# （sort_order 最小的启用账户）。合并多账户的视图等有了账户切换界面再说 ——
# 在那之前默认只看一个账户，比悄悄把几个账户的持仓加在一起安全。

def list_accounts(enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM accounts"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY sort_order, id"
    return [dict(r) for r in connect().execute(sql)]


def get_account(account_id: int) -> dict[str, Any] | None:
    r = connect().execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return dict(r) if r else None


def add_account(exchange: str, label: str, market: str = "usdm",
                sort_order: int = 0) -> int:
    global _default_account
    conn = connect()
    cur = conn.execute(
        """INSERT INTO accounts (exchange, market, label, sort_order, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (exchange, market, label, sort_order, int(time.time() * 1000)),
    )
    conn.commit()
    _default_account = None          # 新账户可能排在更前面
    return int(cur.lastrowid)


def default_account_id() -> int:
    """默认账户。缓存起来 —— 每次写入都查一次没必要。"""
    global _default_account
    if _default_account is None:
        r = connect().execute(
            "SELECT id FROM accounts WHERE enabled = 1 ORDER BY sort_order, id LIMIT 1"
        ).fetchone()
        if r is None:
            raise RuntimeError("库里一个账户都没有，迁移 _m003 应当建过默认账户")
        _default_account = int(r["id"])
    return _default_account


def _acct(account_id: int | None) -> int:
    return default_account_id() if account_id is None else account_id


# ---------- credentials ----------
#
# 只存密文。加解密与主密钥在 vault.py —— 这一层刻意不认识明文，
# 免得哪天有人图省事在这里加个"顺手解个密"的便利函数。

def set_credential(account_id: int, name: str, nonce: bytes, ciphertext: bytes) -> None:
    conn = connect()
    conn.execute(
        """INSERT INTO credentials (account_id, name, nonce, ciphertext, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (account_id, name) DO UPDATE SET
               nonce = excluded.nonce,
               ciphertext = excluded.ciphertext,
               updated_at = excluded.updated_at""",
        (account_id, name, nonce, ciphertext, int(time.time() * 1000)),
    )
    conn.commit()


def get_credential(account_id: int, name: str) -> dict[str, Any] | None:
    r = connect().execute(
        "SELECT nonce, ciphertext, updated_at FROM credentials "
        "WHERE account_id = ? AND name = ?",
        (account_id, name),
    ).fetchone()
    return dict(r) if r else None


def count_credentials() -> int:
    """全库密文条数。vault 用它判断"主密钥不见了"还是"本来就是新库"。"""
    try:
        return int(connect().execute("SELECT COUNT(*) FROM credentials").fetchone()[0])
    except sqlite3.OperationalError:
        return 0        # 表还没建（迁移之前）


def credential_names(account_id: int) -> list[str]:
    return [r["name"] for r in connect().execute(
        "SELECT name FROM credentials WHERE account_id = ? ORDER BY name",
        (account_id,))]


def delete_credential(account_id: int, name: str) -> bool:
    conn = connect()
    cur = conn.execute("DELETE FROM credentials WHERE account_id = ? AND name = ?",
                       (account_id, name))
    conn.commit()
    return cur.rowcount > 0


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

def list_trades(symbol: str | None = None,
                account_id: int | None = None) -> list[dict[str, Any]]:
    """某账户的全部成交，按时间升序。供盈亏回放用——回放必须拿全量，不能分页。"""
    sql = "SELECT * FROM trades WHERE account_id = ?"
    args: list = [_acct(account_id)]
    if symbol:
        sql += " AND symbol = ?"
        args.append(symbol)
    sql += " ORDER BY traded_at, id"
    return [dict(r) for r in connect().execute(sql, args).fetchall()]


def page_trades(symbol: str | None = None, limit: int = 200, offset: int = 0,
                account_id: int | None = None) -> tuple[list[dict[str, Any]], int]:
    """给界面看的成交流水，按时间倒序分页。

    这张表有上万行，全量返回是 2MB 级的响应——在小带宽机器上要几十秒，
    而界面一次也就显示几十行。
    """
    where, args = " WHERE account_id = ?", [_acct(account_id)]
    if symbol:
        where += " AND symbol = ?"
        args.append(symbol)
    total = connect().execute(f"SELECT COUNT(*) FROM trades{where}", args).fetchone()[0]
    rows = connect().execute(
        f"SELECT * FROM trades{where} ORDER BY traded_at DESC, id DESC LIMIT ? OFFSET ?",
        (*args, limit, offset),
    ).fetchall()
    return [dict(r) for r in rows], total


def trades_version(account_id: int | None = None) -> tuple[int, int]:
    """(行数, 最大 id)，作为缓存版本号——成交没变就不必重算盈亏。

    必须按账户算：两个账户共用一个版本号的话，A 账户同步完成会把
    B 账户的缓存一起顶掉，白白重算。
    """
    r = connect().execute(
        "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM trades WHERE account_id = ?",
        (_acct(account_id),),
    ).fetchone()
    return int(r[0]), int(r[1])


def add_trade(
    symbol: str, side: str, qty: float, price: float, fee: float, traded_at: int,
    note: str, account_id: int | None = None
) -> int:
    conn = connect()
    cur = conn.execute(
        """INSERT INTO trades
           (account_id, symbol, side, qty, price, fee, traded_at, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (_acct(account_id), symbol, side, qty, price, fee, traded_at, note,
         int(time.time() * 1000)),
    )
    conn.commit()
    return int(cur.lastrowid)


def add_trades_bulk(rows: list[tuple], account_id: int | None = None) -> int:
    """rows: (symbol, side, qty, price, fee, traded_at, note[, realized_pnl])"""
    conn = connect()
    now = int(time.time() * 1000)
    acct = _acct(account_id)
    norm = [(r if len(r) == 8 else (*r, None)) for r in
            [(*x, None) if len(x) == 7 else x for x in rows]]
    cur = conn.executemany(
        """INSERT INTO trades
           (account_id, symbol, side, qty, price, fee, traded_at, note,
            realized_pnl, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(acct, *r, now) for r in norm],
    )
    conn.commit()
    return cur.rowcount


def backfill_realized_pnl(rows: list[tuple], account_id: int | None = None) -> int:
    """回填已有成交的交易所盈亏。rows: (realized_pnl, note)

    早期同步只存了成交本身没存盈亏字段，若不回填，同一标的会出现
    一部分用交易所口径、另一部分用本地回放的混合计算，结果没有意义。
    """
    conn = connect()
    acct = _acct(account_id)
    cur = conn.executemany(
        "UPDATE trades SET realized_pnl = ? "
        "WHERE account_id = ? AND note = ? AND realized_pnl IS NULL",
        [(pnl, acct, note) for pnl, note in rows],
    )
    conn.commit()
    return cur.rowcount


def existing_trade_notes(account_id: int | None = None) -> set[str]:
    """某账户已同步成交的去重标记（note 形如 binance:<tradeId>）。

    必须按账户查：交易所的 tradeId 只在单账户内唯一，跨账户去重会把
    另一个账户的成交当成重复丢掉。
    """
    return {
        r["note"] for r in connect().execute(
            "SELECT note FROM trades WHERE account_id = ? AND note LIKE 'binance:%'",
            (_acct(account_id),))
    }


# ---------- income ----------

def upsert_income(rows: list[tuple], account_id: int | None = None) -> int:
    """rows: (tran_id, symbol, type, amount, asset, ts)。按 (账户, tran_id) 幂等。

    主键含账户是必须的：交易所的流水号只在单账户内唯一，跨账户同号的流水
    会被 INSERT OR IGNORE 静默丢掉 —— 资金费算少了还不报错。
    """
    conn = connect()
    acct = _acct(account_id)
    cur = conn.executemany(
        """INSERT OR IGNORE INTO income
           (account_id, tran_id, symbol, type, amount, asset, ts)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(acct, *r) for r in rows],
    )
    conn.commit()
    return cur.rowcount


def income_totals(since: int | None = None,
                  account_id: int | None = None) -> dict[str, dict[str, float]]:
    """按 symbol 汇总各科目金额。since 给定时只统计该时刻之后。"""
    sql = "SELECT symbol, type, SUM(amount) AS total FROM income WHERE account_id = ?"
    args: list = [_acct(account_id)]
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    sql += " GROUP BY symbol, type"
    out: dict[str, dict[str, float]] = {}
    for r in connect().execute(sql, args):
        out.setdefault(r["symbol"], {})[r["type"]] = r["total"]
    return out


def income_by_type(since: int | None = None,
                   account_id: int | None = None) -> dict[str, float]:
    sql = "SELECT type, SUM(amount) AS total FROM income WHERE account_id = ?"
    args: list = [_acct(account_id)]
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    sql += " GROUP BY type"
    return {r["type"]: r["total"] for r in connect().execute(sql, args)}


def income_symbols(account_id: int | None = None) -> list[str]:
    """所有出现过流水的标的——用它反查有交易历史的 symbol，含已平仓的。"""
    return [
        r["symbol"]
        for r in connect().execute(
            "SELECT DISTINCT symbol FROM income "
            "WHERE account_id = ? AND symbol != '' ORDER BY symbol",
            (_acct(account_id),),
        )
    ]


def delete_trade(trade_id: int, account_id: int | None = None) -> bool:
    conn = connect()
    # 带上账户条件：id 是全局自增的，不限定账户就能用猜到的 id 删掉别的账户的成交
    cur = conn.execute("DELETE FROM trades WHERE id = ? AND account_id = ?",
                       (trade_id, _acct(account_id)))
    conn.commit()
    return cur.rowcount > 0
