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


def test_tags_joined_and_theme_filter(ext_db):
    c = sqlite3.connect(config.NEWS_DB)
    c.executescript("""
        CREATE TABLE news_tag (news_id INTEGER PRIMARY KEY, status TEXT, themes TEXT,
            sentiment TEXT, scope TEXT, importance INTEGER, model TEXT, version INTEGER,
            attempts INTEGER,
            error TEXT, updated_at INTEGER);
        CREATE TABLE news_theme (news_id INTEGER, theme TEXT, ts INTEGER);
        INSERT INTO news_tag VALUES
            (2, 'ok', '["exchange_notice"]', 'neutral', 'single', 1, 'm', 1, 1, '', 0),
            (3, 'ok', '["geopolitics"]', 'bearish', 'macro', 3, 'm', 1, 1, '', 0);
        INSERT INTO news_theme VALUES (2, 'exchange_notice', 2000), (3, 'geopolitics', 3000);
    """)
    c.commit()
    c.close()
    external._local.__dict__.clear()
    rows = {r["id"]: r for r in external.page()}
    assert rows[3]["themes"] == ["geopolitics"] and rows[3]["importance"] == 3
    assert rows[1]["themes"] == [] and rows[1]["importance"] == 0          # 还没打标签
    assert [r["id"] for r in external.page(theme="geopolitics")] == [3]
    # "只看重要" = 源站标记重要 或 大模型判为重大
    assert [r["id"] for r in external.page(important_only=True)] == [3, 1]
    assert [r["id"] for r in external.page(theme="exchange_notice", symbols=["XIAOMIUSDT"])] == [2]
