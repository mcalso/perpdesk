"""账户管理接口的测试。

两类重点：
  * 删除的护栏 —— 账户下还有成交时不能删，否则那些流水会变成查不到出处的孤儿；
  * 凭据写入的传输安全 —— 明文 HTTP 下必须拒绝，而不是提示一下就放行。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def api(tmp_path, monkeypatch):
    from backend.app import config, db, vault
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "perpdesk.db")
    monkeypatch.setattr(config, "MASTER_KEY_PATH", tmp_path / ".master.key")
    monkeypatch.setattr(config, "MASTER_KEY_ENV", "")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "none.env")
    monkeypatch.setattr(config, "ALLOW_INSECURE_CREDENTIALS", False)
    db.close(); vault.reset_cache(); db.connect()

    from backend.app.routers import account as r
    app = FastAPI()
    app.include_router(r.router)
    with TestClient(app) as c:
        yield c, db, vault
    db.close(); vault.reset_cache()


# ---------------------------------------------------------------- 增改

def test_create_account(api):
    client, db, _ = api
    r = client.post("/api/account/accounts", json={"label": "OKX 主账户", "exchange": "okx"})
    assert r.status_code == 201
    acct = db.get_account(r.json()["id"])
    assert (acct["label"], acct["exchange"], acct["enabled"]) == ("OKX 主账户", "okx", 1)


def test_create_rejects_blank_label(api):
    client, _, _ = api
    assert client.post("/api/account/accounts", json={"label": "   "}).status_code == 422


def test_rename(api):
    client, db, _ = api
    a = db.default_account_id()
    assert client.patch(f"/api/account/accounts/{a}", json={"label": "改过的"}).status_code == 200
    assert db.get_account(a)["label"] == "改过的"


def test_disable_account_changes_the_default(api):
    """停用当前默认账户后，默认要落到下一个启用的账户上。"""
    client, db, _ = api
    a = db.default_account_id()
    b = client.post("/api/account/accounts", json={"label": "二号"}).json()["id"]
    client.patch(f"/api/account/accounts/{a}", json={"enabled": False})
    assert db.default_account_id() == b


def test_patch_ignores_unknown_fields(api):
    """只有白名单里的列能改 —— 别让接口顺手改了 id 或 created_at。"""
    client, db, _ = api
    a = db.default_account_id()
    before = db.get_account(a)
    client.patch(f"/api/account/accounts/{a}", json={"label": "x", "exchange": "evil"})
    after = db.get_account(a)
    assert after["label"] == "x"
    assert after["exchange"] == before["exchange"], "exchange 不在 PATCH 的白名单里"


def test_unknown_account_is_404(api):
    client, _, _ = api
    assert client.patch("/api/account/accounts/999", json={"label": "x"}).status_code == 404
    assert client.delete("/api/account/accounts/999").status_code == 404


# ---------------------------------------------------------------- 删除护栏

def test_cannot_delete_account_with_trades(api):
    """账户下还有成交时删账户，那些流水就查不到出处了。"""
    client, db, _ = api
    a = db.default_account_id()
    client.post("/api/account/accounts", json={"label": "二号"})
    db.add_trade("BTCUSDT", "BUY", 1, 100, 0, 1_700_000_000_000, "t", account_id=a)

    r = client.delete(f"/api/account/accounts/{a}")
    assert r.status_code == 409
    assert "停用" in r.json()["detail"]
    assert db.get_account(a) is not None


def test_cannot_delete_the_last_account(api):
    client, db, _ = api
    a = db.default_account_id()
    r = client.delete(f"/api/account/accounts/{a}")
    assert r.status_code == 409
    assert db.get_account(a) is not None


def test_delete_empty_account_works_and_drops_its_credentials(api):
    """凭据是 ON DELETE CASCADE：账户没了，密钥留着是纯风险。"""
    client, db, vault = api
    b = client.post("/api/account/accounts", json={"label": "二号"}).json()["id"]
    vault.put(b, "api_key", "some-key-value-1234")
    assert vault.names(b) == ["api_key"]

    assert client.delete(f"/api/account/accounts/{b}").status_code == 200
    assert db.get_account(b) is None
    assert db.credential_names(b) == []


def test_usage_reports_what_hangs_off_the_account(api):
    client, db, _ = api
    a = db.default_account_id()
    db.add_trade("BTCUSDT", "BUY", 1, 100, 0, 1_700_000_000_000, "t", account_id=a)
    db.upsert_income([("t1", "BTCUSDT", "FUNDING_FEE", -1, "USDT", 1)], account_id=a)
    u = client.get(f"/api/account/accounts/{a}/usage").json()
    assert (u["trades"], u["income"]) == (1, 1)


# ---------------------------------------------------------------- 传输安全

CREDS = {"api_key": "key-1234567890abcdef", "api_secret": "secret-1234567890abcdef"}


def test_plaintext_http_refuses_credentials(api):
    """明文 HTTP 下提交 API key，密钥就是明文过网线的。

    这里必须拒绝而不是提示一下放行 —— 提示没人看，密钥泄露不可逆。
    """
    client, db, vault = api
    a = db.default_account_id()
    r = client.put(f"/api/account/accounts/{a}/credentials", json=CREDS,
                   headers={"X-Forwarded-Proto": "http", "X-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 421
    assert vault.names(a) == [], "拒绝之后不该留下任何凭据"


def test_https_accepts_credentials(api, monkeypatch):
    client, db, vault = api
    a = db.default_account_id()
    from backend.app import account as acct_mod

    async def fake_balances(account_id=None):
        return [{"asset": "USDT", "balance": 1.0, "available": 1.0, "unrealized": 0.0}]
    monkeypatch.setattr(acct_mod, "balances", fake_balances)

    r = client.put(f"/api/account/accounts/{a}/credentials", json=CREDS,
                   headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200
    assert r.json()["verified"] is True
    assert vault.get(a, "api_secret") == CREDS["api_secret"]
    # 回给前端的只能是掩码
    assert CREDS["api_secret"] not in r.text


def test_bad_credentials_are_saved_but_reported_unverified(api, monkeypatch):
    """校验失败不回滚 —— 可能只是网络不通，但要如实说校验没过。"""
    client, db, vault = api
    a = db.default_account_id()
    from backend.app import account as acct_mod

    async def boom(account_id=None):
        raise RuntimeError("binance -401 invalid key")
    monkeypatch.setattr(acct_mod, "balances", boom)

    r = client.put(f"/api/account/accounts/{a}/credentials", json=CREDS,
                   headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200
    body = r.json()
    assert body["verified"] is False and "invalid key" in body["error"]
    assert vault.get(a, "api_key") == CREDS["api_key"]


def test_escape_hatch_allows_plaintext_when_explicitly_enabled(api, monkeypatch):
    """自己在前面挡了 TLS、后端拿不到 X-Forwarded-Proto 时的出口。"""
    client, db, vault = api
    from backend.app import config, account as acct_mod
    monkeypatch.setattr(config, "ALLOW_INSECURE_CREDENTIALS", True)

    async def fake_balances(account_id=None):
        return []
    monkeypatch.setattr(acct_mod, "balances", fake_balances)

    a = db.default_account_id()
    r = client.put(f"/api/account/accounts/{a}/credentials", json=CREDS,
                   headers={"X-Forwarded-Proto": "http", "X-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 200


@pytest.mark.parametrize("name", ["nope", "../../etc", "api_key%00", "*"])
def test_credential_field_whitelist(api, name):
    """只认 api_key / api_secret / passphrase 三个字段名。

    断言"没成功"而不是具体状态码：路径穿越那种会被路由层先挡下返回 422，
    正常的未知字段是 400，两者都算拒绝。
    """
    client, db, vault = api
    a = db.default_account_id()
    vault.put(a, "api_key", "key-1234567890abcdef")
    r = client.delete(f"/api/account/accounts/{a}/credentials/{name}")
    assert r.status_code != 200 or r.json().get("ok") is False
    assert vault.names(a) == ["api_key"], "不该误删已有凭据"
