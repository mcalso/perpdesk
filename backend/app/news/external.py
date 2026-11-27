"""外部资讯库：读 news-collector 采集好的 SQLite（只读）。

设置 PERPDESK_NEWS_DB=/path/to/news.db 后，资讯页改读这个库，perpdesk 自己的
金十轮询不再启动。news-collector 的 news 表与本项目的 flashes 表字段一致
（source / source_id / ts / title / content / link / important / tags / symbols），
另外有 news_symbol 表（资讯 × 合约）可以直接按标的筛选，不用 LIKE 扫 JSON。

以 mode=ro 打开：perpdesk 绝不写这个库，采集进程是唯一的写入方。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .. import config

_local = threading.local()


def enabled() -> bool:
    return bool(config.NEWS_DB)


def _conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(f"file:{config.NEWS_DB}?mode=ro", uri=True, timeout=10,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def _has_tags(c: sqlite3.Connection) -> bool:
    row = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='news_tag'").fetchone()
    return row is not None


def page(limit: int = 50, before: int | None = None, important_only: bool = False,
         symbols: list[str] | None = None, theme: str | None = None) -> list[dict[str, Any]]:
    """与 db.page_flashes 同样的语义：按时间倒序，before 为上一页最后一条的 ts。

    news-collector 给每条资讯打了事件主题标签（news_tag / news_theme 表）时一并返回：
    themes / sentiment / scope / importance。"只看重要"同时包含源站标记的重要和
    大模型判为重大（importance = 3）的。theme 按主题筛选。
    """
    c = _conn()
    tags = _has_tags(c)
    cols = "n.*" + (", t.themes AS t_themes, t.sentiment AS t_sentiment, t.scope AS t_scope, "
                    "t.importance AS t_importance" if tags else "")
    joins = " LEFT JOIN news_tag t ON t.news_id = n.id AND t.status = 'ok'" if tags else ""
    where, args = [], []
    if symbols:
        joins = f" JOIN news_symbol s ON s.news_id = n.id{joins}"
        where.append(f"s.symbol IN ({','.join('?' * len(symbols))})")
        args += symbols
    if theme and tags:
        joins += " JOIN news_theme th ON th.news_id = n.id"
        where.append("th.theme = ?")
        args.append(theme)
    if before:
        where.append("n.ts < ?")
        args.append(before)
    if important_only:
        where.append("(n.important = 1" + (" OR t.importance = 3)" if tags else ")"))
    sql = f"SELECT DISTINCT {cols} FROM news n{joins}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY n.ts DESC, n.id DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in c.execute(sql, args):
        d = dict(r)
        if tags:
            d["themes"] = json.loads(d.pop("t_themes") or "[]")
            d["sentiment"] = d.pop("t_sentiment") or ""
            d["scope"] = d.pop("t_scope") or ""
            d["importance"] = d.pop("t_importance") or 0
        out.append(d)
    return out


def status() -> dict[str, Any]:
    """返回与 NewsPoller.status() 相同结构，前端不用区分数据来自哪里。"""
    c = _conn()
    r = c.execute("SELECT COUNT(*) AS n, COALESCE(MAX(ts), 0) AS latest, "
                  "COALESCE(MAX(created_at), 0) AS ins FROM news").fetchone()
    sources = [x[0] for x in c.execute("SELECT DISTINCT source FROM news ORDER BY source")]
    errors = []
    try:
        for s in c.execute("SELECT source, last_error FROM source_status "
                           "WHERE fails > 0 AND last_error != ''"):
            errors.append(f"{s['source']}: {s['last_error'][:80]}")
    except sqlite3.OperationalError:          # 旧版采集库没有 source_status 表
        pass
    try:
        known = c.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
    except sqlite3.OperationalError:
        known = 0
    return {
        "sources": sources,
        "pollInterval": 0,
        "total": int(r["n"]),
        "latest": int(r["latest"]),
        "ageSec": round(time.time() - r["ins"] / 1000, 1) if r["ins"] else None,
        "error": "；".join(errors),
        "symbolsKnown": known,
    }
