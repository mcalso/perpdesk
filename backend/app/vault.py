"""账户凭据的加密存储。

**这层加密防住什么、防不住什么，必须说清楚**，否则它只会制造安全感：

  防住  数据库文件单独泄露 —— 迁移前的自动备份、误发出去的 perpdesk.db、
        云盘快照、手滑提交进 git。自托管工具最常见的泄露就是这一类。
  防不住  能读服务器文件系统的人。主密钥默认就在同一台机器上
        （backend/.master.key，权限 600），能拿到库的人多半也拿得到它。
        也防不住能读进程内存的人。

想把边界推远一点，可以用 PERPDESK_MASTER_KEY 环境变量从外部注入主密钥，
让它根本不落盘——代价是每次重启都要重新提供。

明文只在两个地方出现：进程内存里，以及签名请求时。任何接口都不返回明文，
对外只给 `status()` 那种掩码信息。
"""
from __future__ import annotations

import binascii
import logging
import os
import secrets as _secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config, db

log = logging.getLogger("tradview.vault")

KEY_BYTES = 32   # AES-256
NONCE_BYTES = 12  # GCM 推荐长度

_key: bytes | None = None


class VaultError(RuntimeError):
    """主密钥缺失/损坏，或密文解不开。"""


# ---------------------------------------------------------------- 主密钥

def _read_key_file(path: Path) -> bytes | None:
    if not path.is_file():
        return None
    raw = path.read_text().strip()
    try:
        key = binascii.unhexlify(raw)
    except ValueError as exc:
        # binascii.Error 是 ValueError 的子类，但非 ASCII 内容抛的是裸
        # ValueError —— 只 catch binascii.Error 的话用户看到的是 traceback
        raise VaultError(f"主密钥文件 {path} 不是合法的十六进制：{exc}") from exc
    if len(key) != KEY_BYTES:
        raise VaultError(f"主密钥长度应为 {KEY_BYTES} 字节，实际 {len(key)}")
    return key


def _create_key_file(path: Path) -> bytes:
    """生成并落盘。

    用 O_EXCL 独占创建并直接指定 0600：先 write_text 再 chmod 的话，
    文件会有一瞬间是默认权限（通常 644），同机器上的其他用户刚好能读到。
    """
    key = _secrets.token_bytes(KEY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, binascii.hexlify(key) + b"\n")
    finally:
        os.close(fd)
    log.warning(
        "已生成新的凭据主密钥 %s（权限 600）。丢了它库里的凭据就解不开了，"
        "请与数据库分开备份。", path)
    return key


def master_key() -> bytes:
    """取主密钥，没有就生成一把。环境变量优先于文件。"""
    global _key
    if _key is not None:
        return _key
    if config.MASTER_KEY_ENV:
        try:
            key = binascii.unhexlify(config.MASTER_KEY_ENV.strip())
        except ValueError as exc:
            raise VaultError(f"PERPDESK_MASTER_KEY 不是合法的十六进制：{exc}") from exc
        if len(key) != KEY_BYTES:
            raise VaultError(f"PERPDESK_MASTER_KEY 应为 {KEY_BYTES} 字节的十六进制")
        _key = key
        return _key
    key = _read_key_file(config.MASTER_KEY_PATH)
    if key is None:
        # 库里已经有密文却找不到主密钥 —— 别声不响地生成一把新的。
        # 那样只会把"密钥丢了"变成后面某次取凭据时的解密失败，
        # 而真正的现场（密钥文件何时消失的）早就没了。
        # 真实触发路径：rsync --delete 没排除 .master.key 就同步了一次。
        n = db.count_credentials()
        if n:
            raise VaultError(
                f"主密钥文件 {config.MASTER_KEY_PATH} 不存在，但库里有 {n} 条加密凭据。"
                "拒绝生成新密钥——那会让这些凭据永久解不开。"
                "请找回原密钥文件（或设 PERPDESK_MASTER_KEY）；"
                "确实丢了就删掉 credentials 表里的记录重新录入。"
            )
        key = _create_key_file(config.MASTER_KEY_PATH)
    _key = key
    return _key


def reset_cache() -> None:
    """丢掉进程内缓存的主密钥（换库、测试时用）。"""
    global _key
    _key = None


# ---------------------------------------------------------------- 加解密

def _aad(account_id: int, name: str) -> bytes:
    """把密文绑定到它所属的账户与字段。

    没有这层绑定的话，能改库的人可以把 1 号账户的 api_secret 整行复制到
    2 号账户下——密文照样解得开，于是 2 号账户拿着 1 号的密钥去下单。
    """
    return f"{account_id}:{name}".encode()


def encrypt(account_id: int, name: str, value: str) -> tuple[bytes, bytes]:
    nonce = _secrets.token_bytes(NONCE_BYTES)
    ct = AESGCM(master_key()).encrypt(nonce, value.encode(), _aad(account_id, name))
    return nonce, ct


def decrypt(account_id: int, name: str, nonce: bytes, ct: bytes) -> str:
    try:
        return AESGCM(master_key()).decrypt(nonce, ct, _aad(account_id, name)).decode()
    except InvalidTag as exc:
        raise VaultError(
            f"账户 {account_id} 的 {name} 解密失败：主密钥不对，或密文被改过。"
            "（换过主密钥的话，库里的旧凭据需要重新录入）") from exc


# ---------------------------------------------------------------- 存取

def put(account_id: int, name: str, value: str) -> None:
    nonce, ct = encrypt(account_id, name, value)
    db.set_credential(account_id, name, nonce, ct)


def get(account_id: int, name: str) -> str | None:
    row = db.get_credential(account_id, name)
    return None if row is None else decrypt(account_id, name, row["nonce"], row["ciphertext"])


def names(account_id: int) -> list[str]:
    return db.credential_names(account_id)


def delete(account_id: int, name: str) -> bool:
    return db.delete_credential(account_id, name)


def mask(value: str) -> str:
    """给人看的掩码。短到看不出首尾的就整串遮掉。"""
    if len(value) <= 12:
        return "•" * len(value)
    return f"{value[:4]}{'•' * 8}{value[-4:]}"


def status(account_id: int) -> dict[str, str]:
    """哪些凭据已配置，各自长什么样——**只给掩码，永远不返回明文**。"""
    out: dict[str, str] = {}
    for name in names(account_id):
        try:
            out[name] = mask(get(account_id, name) or "")
        except VaultError:
            out[name] = "（解不开）"
    return out


# ---------------------------------------------------------------- 从 .env 接管

ENV_FIELDS = {"api_key": "BINANCE_API_KEY", "api_secret": "BINANCE_API_SECRET"}


def import_from_env(account_id: int) -> list[str]:
    """把 backend/.env 里的凭据搬进加密存储。

    只在该账户还没有对应凭据时才写，所以重复调用是安全的，也不会覆盖
    用户后来改过的值。**不动 .env 文件本身**——那是用户的文件。
    """
    env = config.load_env()
    have = set(names(account_id))
    moved = []
    for name, env_key in ENV_FIELDS.items():
        if name in have:
            continue
        value = env.get(env_key, "").strip()
        if value:
            put(account_id, name, value)
            moved.append(name)
    if moved:
        log.info("已把 %s 从 backend/.env 接管进加密存储（.env 未改动，可自行清理）",
                 "、".join(moved))
    return moved
