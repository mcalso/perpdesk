"""站点登录：单用户口令 + 服务端会话。

**没有用户体系是刻意的。** 这是自托管单人工具，一个部署对应一个主人。
多租户意味着要处理越权、会话劫持、用户枚举、密码重置链路，每一样都是
凭据泄露的新入口——而这个程序手里握着交易所 API key。`auth` 表上的
`CHECK (id = 1)` 把这个决定写进了 schema。

几个刻意的选择：

* 口令用 **scrypt**（标准库自带，内存硬，抗 GPU 爆破）。参数存进库里，
  以后调强度时老口令仍能验证，下次登录再按新参数重算。
* 会话表只存 token 的 **sha256**，不存 token 本身。库泄露不等于会话被接管。
* 首次启动没有口令时**自动生成一个并打到日志**，而不是"没设口令就不鉴权"
  ——后者会让一个刚部署、还没来得及设密码的实例在公网上裸奔。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time

from . import config, db

log = logging.getLogger("perpdesk.auth")

# n=2^15 约 32MB / 100ms。注意 maxmem 必须显式传：
# OpenSSL 默认上限装不下 n=2^15，不传会直接报 "memory limit exceeded"。
KDF = {"n": 1 << 15, "r": 8, "p": 1, "dklen": 32}
MAXMEM = 128 * 1024 * 1024

SESSION_TTL = 30 * 86400        # 30 天
TOKEN_BYTES = 32
COOKIE_NAME = "perpdesk_session"

# 登录失败节流。单用户工具没有"按用户隔离"的意义，全局一个计数器即可；
# 代价是攻击者能把真正的主人也一起挡在外面几分钟，但那远好过被爆破。
_failures: list[float] = []
FAIL_WINDOW = 900.0             # 15 分钟
FAIL_LIMIT = 10


class AuthError(RuntimeError):
    pass


# ---------------------------------------------------------------- 口令

def _derive(password: str, salt: bytes, params: dict) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt,
        n=params["n"], r=params["r"], p=params["p"], dklen=params["dklen"],
        maxmem=MAXMEM,
    )


def has_password() -> bool:
    return db.get_auth() is not None


def set_password(password: str) -> None:
    """设置/修改口令。改口令会让所有已有会话失效。"""
    if len(password) < 8:
        raise AuthError("口令至少 8 位")
    salt = secrets.token_bytes(16)
    db.set_auth(_derive(password, salt, KDF), salt, json.dumps(KDF))
    db.delete_all_sessions()
    log.info("站点口令已更新，所有会话已失效")


def verify_password(password: str) -> bool:
    row = db.get_auth()
    if row is None:
        return False
    params = json.loads(row["params"])
    expected = bytes(row["password_hash"])
    got = _derive(password, bytes(row["salt"]), params)
    if not hmac.compare_digest(got, expected):
        return False
    # 参数升级过就顺手按新参数重算一遍，用户无感
    if params != KDF:
        salt = secrets.token_bytes(16)
        db.set_auth(_derive(password, salt, KDF), salt, json.dumps(KDF))
        log.info("口令哈希已按新的 KDF 参数重算")
    return True


def ensure_password() -> str | None:
    """首次启动时确保有口令。返回生成的口令（仅此一次），已有则返回 None。

    没设口令就不鉴权是不行的：那会让一个刚部署、还没来得及设密码的实例
    在公网上裸奔。生成一个打到日志里，用户从日志取走再改掉。
    """
    if has_password():
        return None
    env = (config.AUTH_PASSWORD or "").strip()
    if env:
        set_password(env)
        log.warning("已用 PERPDESK_PASSWORD 设置站点口令")
        return None
    generated = secrets.token_urlsafe(12)
    set_password(generated)
    log.warning(
        "首次启动，已生成站点登录口令：%s\n"
        "    请登录后立即修改。也可以设 PERPDESK_PASSWORD 自行指定。",
        generated)
    return generated


# ---------------------------------------------------------------- 会话

def _hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def create_session(label: str = "") -> str:
    """新建会话，返回原始 token（只在这一刻存在，库里存的是它的哈希）。"""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = int(time.time())
    db.add_session(_hash_token(token), now, now + SESSION_TTL, label[:120])
    db.purge_expired_sessions(now)
    return token


def validate(token: str | None) -> bool:
    if not token:
        return False
    row = db.get_session(_hash_token(token))
    now = int(time.time())
    if row is None or row["expires_at"] <= now:
        return False
    # last_seen 每分钟最多写一次，避免每个请求都写库
    if now - row["last_seen"] > 60:
        db.touch_session(_hash_token(token), now)
    return True


def destroy(token: str | None) -> None:
    if token:
        db.delete_session(_hash_token(token))


def destroy_all() -> None:
    db.delete_all_sessions()


def sessions() -> list[dict]:
    """当前会话列表，供"在别处退出"用。不含 token。"""
    return db.list_sessions(int(time.time()))


# ---------------------------------------------------------------- 失败节流

def throttled() -> float:
    """还需等待多少秒才能再试。0 表示可以试。"""
    now = time.time()
    _failures[:] = [t for t in _failures if now - t < FAIL_WINDOW]
    if len(_failures) < FAIL_LIMIT:
        return 0.0
    return round(FAIL_WINDOW - (now - _failures[0]), 1)


def record_failure() -> None:
    _failures.append(time.time())


def clear_failures() -> None:
    _failures.clear()
