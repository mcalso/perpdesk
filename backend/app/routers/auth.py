"""登录接口。"""
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .. import auth, config

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    password: str


class ChangeIn(BaseModel):
    current: str
    password: str


def _secure_cookie(request: Request) -> bool:
    """HTTP 下不能置 Secure，否则浏览器根本不会回传 cookie，表现为"登录了又弹回来"。

    默认按 nginx 转发过来的 X-Forwarded-Proto 判断。信这个头是安全的：
    后端只监听 127.0.0.1，除 nginx 外没人能直接连上来伪造它。
    """
    mode = config.COOKIE_SECURE.lower()
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto == "https"


def _set_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        auth.COOKIE_NAME, token,
        max_age=auth.SESSION_TTL,
        httponly=True,              # JS 读不到，XSS 偷不走
        samesite="lax",             # 挡掉跨站发起的写操作
        secure=_secure_cookie(request),
        path="/",
    )


@router.get("/me")
async def me(request: Request) -> dict:
    """当前登录状态。前端启动时先问这个，决定显示登录页还是主界面。"""
    token = request.cookies.get(auth.COOKIE_NAME)
    return {
        "authenticated": auth.validate(token),
        "hasPassword": auth.has_password(),
    }


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict:
    wait = auth.throttled()
    if wait:
        raise HTTPException(429, f"登录失败次数过多，请 {wait:.0f} 秒后再试",
                            headers={"Retry-After": str(int(wait))})
    if not auth.verify_password(body.password):
        auth.record_failure()
        # 单用户站点没有"用户名不存在"这种信息可泄露，统一一句话即可
        raise HTTPException(401, "口令不正确")
    auth.clear_failures()
    token = auth.create_session(request.headers.get("user-agent", ""))
    _set_cookie(response, request, token)
    return {"ok": True}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    auth.destroy(request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/password")
async def change_password(body: ChangeIn, request: Request, response: Response) -> dict:
    """改口令。要求先验证当前口令，改完所有会话失效、当前浏览器重新发一个。"""
    if not auth.verify_password(body.current):
        raise HTTPException(401, "当前口令不正确")
    try:
        auth.set_password(body.password)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    token = auth.create_session(request.headers.get("user-agent", ""))
    _set_cookie(response, request, token)
    return {"ok": True, "note": "其他设备上的会话已全部失效"}


@router.get("/sessions")
async def sessions() -> dict:
    return {"rows": auth.sessions(), "now": int(time.time())}


@router.post("/sessions/revoke-all")
async def revoke_all(request: Request, response: Response) -> dict:
    """在所有设备上退出，包括当前这个。"""
    auth.destroy_all()
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}
