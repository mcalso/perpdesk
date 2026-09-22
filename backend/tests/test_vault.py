"""凭据加密存储的测试。

重点不是"存进去能取出来"，而是那些声称有、但很容易其实没有的安全属性：
密文有没有真的加密、能不能被挪到别的账户下解开、主密钥文件权限对不对、
掩码会不会漏出明文。
"""
import binascii
import stat

import pytest


@pytest.fixture
def vlt(tmp_path, monkeypatch):
    from backend.app import config, db, vault
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "perpdesk.db")
    monkeypatch.setattr(config, "MASTER_KEY_PATH", tmp_path / "keys" / ".master.key")
    monkeypatch.setattr(config, "MASTER_KEY_ENV", "")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "nonexistent.env")
    db.close()
    vault.reset_cache()
    db.connect()
    yield vault
    db.close()
    vault.reset_cache()


SECRET = "TfEQovGwxgkHZeyml4FmzUtkxbzkYZYzaEhNsCFFQITuURwGLBbkfMaOvBmevjJh"


# ---------------------------------------------------------------- 主密钥

def test_master_key_is_created_with_owner_only_permissions(vlt, tmp_path):
    """先 write 再 chmod 的话，文件会有一瞬间是 644，同机器的其他用户刚好能读到。"""
    vlt.master_key()
    path = tmp_path / "keys" / ".master.key"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(binascii.unhexlify(path.read_text().strip())) == 32


def test_master_key_is_stable_across_reloads(vlt):
    first = vlt.master_key()
    vlt.reset_cache()
    assert vlt.master_key() == first, "每次重启都换密钥的话，已存的凭据全都解不开"


def test_env_master_key_takes_priority_and_never_touches_disk(vlt, tmp_path, monkeypatch):
    from backend.app import config
    key = binascii.hexlify(b"\x01" * 32).decode()
    monkeypatch.setattr(config, "MASTER_KEY_ENV", key)
    vlt.reset_cache()
    assert vlt.master_key() == b"\x01" * 32
    assert not (tmp_path / "keys" / ".master.key").exists(), "设了环境变量就不该落盘"


def test_corrupt_master_key_file_is_rejected_loudly(vlt, tmp_path):
    vlt.master_key()
    path = tmp_path / "keys" / ".master.key"
    path.write_text("这不是十六进制")
    vlt.reset_cache()
    with pytest.raises(vlt.VaultError, match="十六进制"):
        vlt.master_key()


def test_wrong_length_master_key_is_rejected(vlt, tmp_path):
    vlt.master_key()
    (tmp_path / "keys" / ".master.key").write_text(binascii.hexlify(b"\x00" * 16).decode())
    vlt.reset_cache()
    with pytest.raises(vlt.VaultError, match="32 字节"):
        vlt.master_key()


# ---------------------------------------------------------------- 加密本身

def test_roundtrip(vlt):
    vlt.put(1, "api_secret", SECRET)
    assert vlt.get(1, "api_secret") == SECRET


def test_plaintext_never_appears_in_the_database_file(vlt, tmp_path):
    """最基本的一条：把库当二进制扫一遍，不能扫到明文。"""
    vlt.put(1, "api_key", SECRET)
    from backend.app import db
    db.connect().commit()
    blob = b"".join(p.read_bytes() for p in tmp_path.glob("perpdesk.db*"))
    assert SECRET.encode() not in blob, "明文直接躺在库里，加密没生效"


def test_same_value_encrypts_differently_each_time(vlt):
    """随机 nonce：同一个值写两次，密文必须不同，否则能从密文相等推出明文相等。"""
    from backend.app import db
    vlt.put(1, "api_key", SECRET)
    ct1 = db.get_credential(1, "api_key")["ciphertext"]
    vlt.put(1, "api_key", SECRET)
    ct2 = db.get_credential(1, "api_key")["ciphertext"]
    assert ct1 != ct2
    assert vlt.get(1, "api_key") == SECRET


def test_ciphertext_cannot_be_moved_to_another_account(vlt):
    """AAD 把密文绑死在 (账户, 字段) 上。

    没有这层绑定，能改库的人可以把 1 号账户的 api_secret 整行复制到 2 号
    账户下——密文照样解得开，于是 2 号账户拿着 1 号的密钥去下单。
    """
    from backend.app import db
    db.add_account("binance", "二号账户")
    vlt.put(1, "api_secret", SECRET)
    row = db.get_credential(1, "api_secret")

    db.set_credential(2, "api_secret", row["nonce"], row["ciphertext"])

    with pytest.raises(vlt.VaultError, match="解密失败"):
        vlt.get(2, "api_secret")


def test_ciphertext_cannot_be_moved_to_another_field(vlt):
    from backend.app import db
    vlt.put(1, "api_secret", SECRET)
    row = db.get_credential(1, "api_secret")
    db.set_credential(1, "api_key", row["nonce"], row["ciphertext"])
    with pytest.raises(vlt.VaultError, match="解密失败"):
        vlt.get(1, "api_key")


def test_tampered_ciphertext_is_detected(vlt):
    """GCM 是 AEAD，改一个字节就该认不出来，而不是解出一段垃圾。"""
    from backend.app import db
    vlt.put(1, "api_key", SECRET)
    row = db.get_credential(1, "api_key")
    broken = bytearray(row["ciphertext"])
    broken[0] ^= 0xFF
    db.set_credential(1, "api_key", row["nonce"], bytes(broken))
    with pytest.raises(vlt.VaultError):
        vlt.get(1, "api_key")


def test_wrong_master_key_fails_loudly(vlt, tmp_path, monkeypatch):
    """换了主密钥必须报错，不能静默当成"没配置"——那会让人以为只是忘了填。"""
    from backend.app import config
    vlt.put(1, "api_key", SECRET)
    monkeypatch.setattr(config, "MASTER_KEY_ENV", binascii.hexlify(b"\x02" * 32).decode())
    vlt.reset_cache()
    with pytest.raises(vlt.VaultError, match="主密钥不对"):
        vlt.get(1, "api_key")


# ---------------------------------------------------------------- 对外暴露

def test_status_only_exposes_masks(vlt):
    vlt.put(1, "api_key", SECRET)
    vlt.put(1, "api_secret", SECRET)
    st = vlt.status(1)
    assert set(st) == {"api_key", "api_secret"}
    for shown in st.values():
        assert SECRET not in shown
        assert len(shown) < len(SECRET)
        assert shown.startswith(SECRET[:4]) and shown.endswith(SECRET[-4:])


def test_short_values_are_fully_masked(vlt):
    """短到看得出首尾的，就别露首尾。"""
    assert set(vlt.mask("abc")) == {"•"}
    assert set(vlt.mask("a" * 12)) == {"•"}


def test_missing_credential_is_none_not_error(vlt):
    assert vlt.get(1, "passphrase") is None
    assert vlt.names(1) == []
    assert vlt.status(1) == {}


def test_delete(vlt):
    vlt.put(1, "api_key", SECRET)
    assert vlt.delete(1, "api_key") is True
    assert vlt.get(1, "api_key") is None
    assert vlt.delete(1, "api_key") is False


# ---------------------------------------------------------------- 从 .env 接管

def test_import_from_env_moves_credentials_without_touching_the_file(vlt, tmp_path, monkeypatch):
    from backend.app import config
    env = tmp_path / ".env"
    env.write_text(f"BINANCE_API_KEY=thekey12345678\nBINANCE_API_SECRET={SECRET}\n")
    monkeypatch.setattr(config, "ENV_PATH", env)
    before = env.read_text()

    assert sorted(vlt.import_from_env(1)) == ["api_key", "api_secret"]

    assert vlt.get(1, "api_secret") == SECRET
    assert env.read_text() == before, ".env 是用户的文件，不该被改动"


def test_import_from_env_is_idempotent_and_never_overwrites(vlt, tmp_path, monkeypatch):
    """重复启动不能把用户后来改过的凭据又覆盖回 .env 里的旧值。"""
    from backend.app import config
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=oldkey12345678\nBINANCE_API_SECRET=oldsecret12345678\n")
    monkeypatch.setattr(config, "ENV_PATH", env)
    vlt.import_from_env(1)

    vlt.put(1, "api_key", "newkey12345678")
    assert vlt.import_from_env(1) == []
    assert vlt.get(1, "api_key") == "newkey12345678"


def test_refuses_to_generate_a_new_key_when_ciphertext_exists(vlt, tmp_path):
    """主密钥丢了但库里还有密文时，绝不能声不响地生成新密钥。

    真实触发路径：部署脚本的 rsync --delete 没排除 .master.key，同步一次
    就把它删了。自动生成新密钥的话，故障会推迟到下一次取凭据时才冒出来，
    表现成"解密失败"，而密钥是什么时候没的、还能不能找回来，现场早没了。
    """
    vlt.put(1, "api_key", SECRET)
    (tmp_path / "keys" / ".master.key").unlink()
    vlt.reset_cache()

    with pytest.raises(vlt.VaultError, match="拒绝生成新密钥"):
        vlt.master_key()


def test_generates_a_key_freely_when_there_is_no_ciphertext(vlt, tmp_path):
    """反过来：全新部署就该安安静静生成一把，不该拦着用户。"""
    vlt.master_key()
    (tmp_path / "keys" / ".master.key").unlink()
    vlt.reset_cache()
    assert len(vlt.master_key()) == 32
