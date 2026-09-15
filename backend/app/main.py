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
from . import binance, config, db, icons, vault
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
    # 老用户的凭据还在 backend/.env 里，首次启动时接管进加密存储。
    # 已经有凭据的账户不会被覆盖，.env 文件本身也不动。
    try:
        vault.import_from_env(db.default_account_id())
    except vault.VaultError as exc:
        # 凭据出问题不该拖垮整个服务：行情不依赖它，照常可用；
        # 账户那一侧会显示"未配置"，日志里说清到底是什么原因。
        log.error("凭据存储不可用，账户功能将不可用：%s", exc)
    await hub.start()
    # 实时订阅 = 自选 ∪ 全部账户的持仓。持仓估值最需要准确及时，
    # 不能只靠 REST 轮询；多账户时并集里少一个标的，那个仓位的浮盈就是滞后的。
    def _refresh_ws_symbols(position_symbols: list[str] | None = None) -> None:
        syms = set(db.list_watchlist())
        syms |= set(position_symbols if position_symbols is not None
                    else account_api.registry.position_symbols())
        hub.set_ws_symbols(sorted(syms))

    hub.on_watchlist_change = _refresh_ws_symbols
    account_api.registry.on_positions = _refresh_ws_symbols
    _refresh_ws_symbols([])
    await account_api.registry.sync()
    # 按成交额从高到低预热，热门标的先有图
    prewarm_task = asyncio.create_task(
        icons.prewarm(lambda: [r["symbol"] for r in
                               sorted(hub.rich_rows(), key=lambda x: -x["quoteVolume"])]),
        name="icon-prewarm",
    )
    log.info("perpdesk backend ready: %s", hub.status())
    yield
    prewarm_task.cancel()
    await account_api.registry.stop_all()
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
