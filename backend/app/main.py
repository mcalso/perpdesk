"""perpdesk 后端入口。

本地自用站点：无鉴权，默认只监听 127.0.0.1。
要给内网其他机器看，用 PERPDESK_HOST=0.0.0.0 启动并自行考虑访问控制。
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import account as account_api
from . import binance, config, db, icons
from .hub import hub
from .routers import account, market, portfolio, watchlist

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("perpdesk")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    await hub.start()
    # 实时订阅 = 自选 ∪ 持仓。持仓估值最需要准确及时，不能只靠 REST 轮询。
    def _refresh_ws_symbols(position_symbols: list[str] | None = None) -> None:
        syms = set(db.list_watchlist())
        syms |= set(position_symbols or [p["symbol"] for p in account_api.cache.positions])
        hub.set_ws_symbols(sorted(syms))

    hub.on_watchlist_change = _refresh_ws_symbols
    account_api.cache.on_positions = _refresh_ws_symbols
    _refresh_ws_symbols([])
    await account_api.cache.start()
    # 按成交额从高到低预热，热门标的先有图
    prewarm_task = asyncio.create_task(
        icons.prewarm(lambda: [r["symbol"] for r in
                               sorted(hub.rich_rows(), key=lambda x: -x["quoteVolume"])]),
        name="icon-prewarm",
    )
    log.info("perpdesk backend ready: %s", hub.status())
    yield
    prewarm_task.cancel()
    await account_api.cache.stop()
    await hub.stop()
    await binance.close()
    await icons.close()
    await account_api.close()
    db.close()


app = FastAPI(title="perpdesk", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:18200", "http://127.0.0.1:18200"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(market.router)
app.include_router(account.router)
app.include_router(watchlist.router)
app.include_router(portfolio.router)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "hub": hub.status()}


# 前端构建产物存在时一并托管，单进程即可访问整站
_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        candidate = _DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
