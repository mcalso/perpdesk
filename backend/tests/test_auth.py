"""登录体系的测试。

这一层错了不会有人报错，只会有人进得来。所以重点在拒绝路径：
没 cookie 能不能读到数据、WebSocket 是不是绕过了鉴权、改口令后旧会话
还认不认、爆破有没有被挡住。
"""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PW = "correct horse battery"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from backend.app import auth, config, db, vault
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "perpdesk.db")
    monkeypatch.setattr(config, "MASTER_KEY_PATH", tmp_path / ".master.key")
    monkeypatch.setattr(config, "MASTER_KEY_ENV", "")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "")
    monkeypatch.setattr(config, "COOKIE_SECURE", "auto")
    db.close()
    vault.reset_cache()
    db.connect()
    auth.clear_failures()
    yield auth, db
    db.close()
    vault.reset_cache()
    auth.clear_failures()


# ---------------------------------------------------------------- 口令

def test_password_roundtrip(env):
    auth, _ = env
    auth.set_password(PW)
    assert auth.verify_password(PW) is True
    assert auth.verify_password(PW + "x") is False
    assert auth.verify_password("") is False


def test_password_is_not_stored_in_the_clear(env, tmp_path):
    auth, db = env
    auth.set_password(PW)
    db.connect().commit()
    blob = b"".join(p.read_bytes() for p in tmp_path.glob("perpdesk.db*"))
    assert PW.encode() not in blob


def test_short_password_rejected(env):
    auth, _ = env
    with pytest.raises(auth.AuthError, match="至少 8 位"):
        auth.set_password("short")


def test_same_password_hashes_differently(env):
    """每次换盐。两个部署用同一个口令，哈希也必须不同。"""
    auth, db = env
    auth.set_password(PW)
    first = bytes(db.get_auth()["password_hash"])
    auth.set_password(PW)
    assert bytes(db.get_auth()["password_hash"]) != first


def test_verify_before_any_password_is_set(env):
    auth, _ = env
    assert auth.has_password() is False
    assert auth.verify_password("anything") is False


def test_ensure_password_generates_one_on_first_run(env):
    """"没设口令就不鉴权"是不行的——刚部署的实例会在公网裸奔。"""
    auth, _ = env
    generated = auth.ensure_password()
    assert generated and len(generated) >= 12
    assert auth.verify_password(generated)
    assert auth.ensure_password() is None, "已有口令时不该再生成"


def test_ensure_password_uses_env_when_given(env, monkeypatch):
    auth, _ = env
    from backend.app import config
    monkeypatch.setattr(config, "AUTH_PASSWORD", "from-env-password")
    assert auth.ensure_password() is None
    assert auth.verify_password("from-env-password")


# ---------------------------------------------------------------- 会话

def test_session_lifecycle(env):
    auth, _ = env
    auth.set_password(PW)
    token = auth.create_session("pytest")
    assert auth.validate(token) is True
    auth.destroy(token)
    assert auth.validate(token) is False


def test_raw_token_is_never_stored(env, tmp_path):
    """库里只存哈希：库泄露不该等于会话被接管。"""
    auth, db = env
    auth.set_password(PW)
    token = auth.create_session()
    db.connect().commit()
    blob = b"".join(p.read_bytes() for p in tmp_path.glob("perpdesk.db*"))
    assert token.encode() not in blob


def test_invalid_and_empty_tokens_are_rejected(env):
    auth, _ = env
    auth.set_password(PW)
    auth.create_session()
    for bad in (None, "", "nope", "a" * 43):
        assert auth.validate(bad) is False


def test_expired_session_is_rejected_and_purged(env, monkeypatch):
    auth, db = env
    auth.set_password(PW)
    # TTL 要在建会话之前改：expires_at 是建的时候算好写进库的，
    # 事后改模块常量不会动到已存的那一行
    monkeypatch.setattr(auth, "SESSION_TTL", -1)
    expired = auth.create_session("过期的")
    assert auth.validate(expired) is False

    monkeypatch.setattr(auth, "SESSION_TTL", 3600)
    fresh = auth.create_session("新的")        # 建新会话时顺带清理过期的
    assert auth.validate(fresh) is True
    rows = db.list_sessions(int(time.time()))
    assert [r["label"] for r in rows] == ["新的"], "过期会话没被清掉"


def test_changing_password_invalidates_every_session(env):
    """改口令的常见动机就是"怀疑被人登进来了"，旧会话必须一起断。"""
    auth, _ = env
    auth.set_password(PW)
    a, b = auth.create_session("设备A"), auth.create_session("设备B")
    auth.set_password("a brand new password")
    assert auth.validate(a) is False
    assert auth.validate(b) is False


def test_session_listing_excludes_token_hashes(env):
    auth, _ = env
    auth.set_password(PW)
    auth.create_session("Mozilla/5.0 测试")
    rows = auth.sessions()
    assert rows[0]["label"] == "Mozilla/5.0 测试"
    assert "token_hash" not in rows[0]


# ---------------------------------------------------------------- 爆破节流

def test_repeated_failures_are_throttled(env):
    auth, _ = env
    auth.set_password(PW)
    assert auth.throttled() == 0
    for _ in range(auth.FAIL_LIMIT):
        auth.record_failure()
    assert auth.throttled() > 0
    auth.clear_failures()
    assert auth.throttled() == 0


def test_throttle_window_slides(env, monkeypatch):
    auth, _ = env
    for _ in range(auth.FAIL_LIMIT):
        auth.record_failure()
    assert auth.throttled() > 0
    monkeypatch.setattr(auth, "FAIL_WINDOW", 0.0)   # 窗口过去了
    assert auth.throttled() == 0


# ---------------------------------------------------------------- 接口

@pytest.fixture
def api(env):
    auth, db = env
    auth.set_password(PW)
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from backend.app import auth as auth_mod
    from backend.app.routers import auth as auth_router
    from backend.app.routers import portfolio as portfolio_router

    app = FastAPI()

    # 与 main.py 同一份白名单与判定逻辑
    PUBLIC = {"/api/auth/me", "/api/auth/login", "/api/auth/logout", "/api/health"}

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        p = request.url.path
        if (p.startswith("/api/") and p not in PUBLIC
                and not auth_mod.validate(request.cookies.get(auth_mod.COOKIE_NAME))):
            return JSONResponse({"detail": "未登录"}, status_code=401)
        return await call_next(request)

    app.include_router(auth_router.router)
    app.include_router(portfolio_router.router)
    portfolio_router._calc_cache.clear()
    with TestClient(app) as c:
        yield c, auth


def test_protected_endpoints_reject_anonymous(api):
    client, _ = api
    for path in ("/api/portfolio/summary", "/api/portfolio/trades", "/api/auth/sessions"):
        assert client.get(path).status_code == 401, path


def test_login_then_access(api):
    client, _ = api
    assert client.get("/api/auth/me").json()["authenticated"] is False
    assert client.post("/api/auth/login", json={"password": PW}).status_code == 200
    assert client.get("/api/auth/me").json()["authenticated"] is True
    assert client.get("/api/portfolio/summary").status_code == 200


def test_wrong_password_is_401_and_sets_no_cookie(api):
    client, auth = api
    r = client.post("/api/auth/login", json={"password": "wrong password"})
    assert r.status_code == 401
    assert auth.COOKIE_NAME not in r.cookies
    assert client.get("/api/portfolio/summary").status_code == 401


def test_logout_drops_the_session(api):
    client, _ = api
    client.post("/api/auth/login", json={"password": PW})
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/portfolio/summary").status_code == 401


def test_cookie_is_httponly_and_lax(api):
    """HttpOnly 让 XSS 偷不走会话；SameSite=Lax 挡掉跨站发起的写操作。"""
    client, auth = api
    r = client.post("/api/auth/login", json={"password": PW})
    raw = r.headers["set-cookie"].lower()
    assert "httponly" in raw
    assert "samesite=lax" in raw
    assert "secure" not in raw, "HTTP 下置 Secure 会导致浏览器根本不回传 cookie"


def test_login_throttling_returns_429(api):
    client, auth = api
    for _ in range(auth.FAIL_LIMIT):
        client.post("/api/auth/login", json={"password": "nope"})
    r = client.post("/api/auth/login", json={"password": "nope"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    # 节流期间即使口令正确也不放行，否则爆破者只要撞对一次就赢了
    assert client.post("/api/auth/login", json={"password": PW}).status_code == 429


def test_change_password_requires_the_current_one(api):
    client, _ = api
    client.post("/api/auth/login", json={"password": PW})
    bad = client.post("/api/auth/password",
                      json={"current": "wrong", "password": "another good one"})
    assert bad.status_code == 401
    ok = client.post("/api/auth/password",
                     json={"current": PW, "password": "another good one"})
    assert ok.status_code == 200
    assert client.get("/api/portfolio/summary").status_code == 200, "改完当前浏览器应保持登录"


def test_revoke_all_logs_out_everywhere(api):
    client, _ = api
    client.post("/api/auth/login", json={"password": PW})
    client.post("/api/auth/sessions/revoke-all")
    assert client.get("/api/portfolio/summary").status_code == 401


# ---------------------------------------------------------------- WebSocket

def test_websocket_requires_login(env):
    """HTTP 中间件管不到 WebSocket 握手。

    只靠中间件的话，这条流就是整站唯一一个不需要登录的数据出口——
    行情、自选、持仓标的都会从这里漏出去。
    """
    auth, _ = env
    auth.set_password(PW)
    from starlette.websockets import WebSocketDisconnect

    from backend.app.routers import market

    app = FastAPI()
    app.include_router(market.router)
    # 故意不把三个 with 合成一句：合并后读者要自己数到第三项才知道「哪一句
    # 应该抛异常」，而这正是本用例的重点。
    with TestClient(app) as c, pytest.raises(WebSocketDisconnect) as err:  # noqa: SIM117
        with c.websocket_connect("/api/market/ws"):
            pass
    assert err.value.code == 1008, "未登录的 WS 应当以 policy violation 关闭"


def test_cookie_gets_secure_flag_behind_https(api):
    """上了 HTTPS 之后 cookie 必须带 Secure，否则会被降级到 http 的请求带出去。

    判定依据是 nginx 转发的 X-Forwarded-Proto。信这个头是安全的：后端只监听
    127.0.0.1，除 nginx 外没人能直接连上来伪造它。
    """
    client, auth = api
    r = client.post("/api/auth/login", json={"password": PW},
                    headers={"X-Forwarded-Proto": "https"})
    raw = r.headers["set-cookie"].lower()
    assert "secure" in raw
    assert "httponly" in raw and "samesite=lax" in raw


def test_cookie_secure_can_be_forced_off(api, monkeypatch):
    """自己在前面挡了 TLS、后端拿不到 X-Forwarded-Proto 时的出口。"""
    from backend.app import config
    monkeypatch.setattr(config, "COOKIE_SECURE", "false")
    client, _ = api
    r = client.post("/api/auth/login", json={"password": PW},
                    headers={"X-Forwarded-Proto": "https"})
    assert "secure" not in r.headers["set-cookie"].lower()
