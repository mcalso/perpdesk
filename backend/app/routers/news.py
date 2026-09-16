"""资讯接口。"""
import json

from fastapi import APIRouter, Query

from .. import account, db
from ..news.poller import poller

router = APIRouter(prefix="/api/news", tags=["news"])


def _row(r: dict) -> dict:
    return {
        "id": r["id"],
        "source": r["source"],
        "ts": r["ts"],
        "title": r["title"],
        "content": r["content"],
        "link": r["link"],
        "important": bool(r["important"]),
        "tags": json.loads(r["tags"] or "[]"),
        "symbols": json.loads(r["symbols"] or "[]"),
    }


@router.get("")
async def flashes(
    limit: int = Query(50, ge=1, le=200),
    before: int | None = Query(None, description="上一页最后一条的 ts，用作游标"),
    important: bool = Query(False, description="只看源站标为重要的"),
    mine: bool = Query(False, description="只看与当前账户持仓/自选相关的"),
    account_id: int | None = Query(None),
) -> dict:
    """快讯列表，按时间倒序。

    翻页用时间游标而不是 offset：资讯是持续追加的，翻页期间前面插进新条目
    会让 offset 错位、把同一条重复显示。
    """
    symbols: list[str] | None = None
    if mine:
        acct = db.default_account_id() if account_id is None else account_id
        held = {p["symbol"] for p in account.registry.get(acct).positions}
        symbols = sorted(held | set(db.list_watchlist()))
        if not symbols:
            return {"rows": [], "total": 0, "filteredBy": [], "status": poller.status()}

    rows = db.page_flashes(limit=limit, before=before,
                           important_only=important, symbols=symbols)
    return {
        "rows": [_row(r) for r in rows],
        "filteredBy": symbols or [],
        "status": poller.status(),
    }


@router.post("/refresh")
async def refresh() -> dict:
    """手动抓一轮。等不及下一个轮询周期时用。"""
    inserted = await poller.poll_once()
    return {"inserted": inserted, "status": poller.status()}
