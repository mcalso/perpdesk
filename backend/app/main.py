"""perpdesk 后端入口。

本地自用站点：无鉴权，默认只监听 127.0.0.1。
要给内网其他机器看，用 PERPDESK_HOST=0.0.0.0 启动并自行考虑访问控制。
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import account as account_api
from . import auth as auth_mod
from . import binance, config, db, icons, vault
from .hub import hub
from .routers import account, auth, market, portfolio, watchlist

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
    # 没有口令就不鉴权是不行的：那会让一个刚部署、还没来得及设密码的实例
    # 在公网上裸奔。首次启动生成一个打进日志，用户取走后自行修改。
    auth_mod.ensure_password()
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

# 除少数几个白名单路径外，所有 /api 都要登录。
# 白名单只放登录本身与健康检查 —— 健康检查不含任何账户信息，
# 留着方便 systemd / 反代做存活探测。
PUBLIC_PATHS = frozenset({
    "/api/auth/me", "/api/auth/login", "/api/auth/logout", "/api/health",
})


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_PATHS:
        if not auth_mod.validate(request.cookies.get(auth_mod.COOKIE_NAME)):
            return JSONResponse({"detail": "未登录"}, status_code=401)
    return await call_next(request)


app.include_router(auth.router)
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
