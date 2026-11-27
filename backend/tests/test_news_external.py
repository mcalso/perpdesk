"""外部资讯库（news-collector）接入。"""
import json
import sqlite3

import pytest

from backend.app import config
from backend.app.news import external


@pytest.fixture
def ext_db(tmp_path, monkeypatch):
    p = tmp_path / "news.db"
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE news (id INTEGER PRIMARY KEY, source TEXT, source_id TEXT, ts INTEGER,
            title TEXT, content TEXT, link TEXT, important INTEGER, tags TEXT, symbols TEXT,
            hits TEXT, kw_version TEXT, created_at INTEGER);
        CREATE TABLE news_symbol (news_id INTEGER, symbol TEXT, ts INTEGER);
        CREATE TABLE source_status (source TEXT, last_try INTEGER, last_ok INTEGER,
            last_error TEXT, error_ts INTEGER, fails INTEGER, new_total INTEGER);
        CREATE TABLE universe (symbol TEXT PRIMARY KEY);
    """)
    rows = [(1, "jin10", "a", 1000, "英伟达大涨", "", "", 1, "[]", '["NVDAUSDT"]'),
            (2, "binance", "b", 2000, "Launch XIAOMIUSDT", "", "", 0, "[]", '["XIAOMIUSDT"]'),
            (3, "jin10", "c", 3000, "无关", "", "", 0, "[]", "[]")]
    for r in rows:
        c.execute("INSERT INTO news VALUES (?,?,?,?,?,?,?,?,?,?,'[]','',?)", (*r, r[3] + 5))
        for s in json.loads(r[9]):
            c.execute("INSERT INTO news_symbol VALUES (?,?,?)", (r[0], s, r[3]))
    c.execute("INSERT INTO source_status VALUES ('okx', 0, 0, 'ConnectTimeout', 0, 2, 0)")
    c.execute("INSERT INTO universe VALUES ('NVDAUSDT')")
    c.commit()
    c.close()
    monkeypatch.setattr(config, "NEWS_DB", str(p))
    external._local.__dict__.clear()
    yield
    external._local.__dict__.clear()


def test_page_order_cursor_and_filters(ext_db):
    assert [r["id"] for r in external.page()] == [3, 2, 1]
    assert [r["id"] for r in external.page(before=3000)] == [2, 1]
    assert [r["id"] for r in external.page(important_only=True)] == [1]
    assert [r["id"] for r in external.page(symbols=["XIAOMIUSDT", "NVDAUSDT"])] == [2, 1]


def test_status_shape(ext_db):
    st = external.status()
    assert st["total"] == 3 and st["latest"] == 3000
    assert st["sources"] == ["binance", "jin10"]
    assert "okx" in st["error"] and st["symbolsKnown"] == 1


def test_read_only(ext_db):
    with pytest.raises(sqlite3.OperationalError):
        external._conn().execute("DELETE FROM news")


def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(config, "NEWS_DB", "")
    assert not external.enabled()
