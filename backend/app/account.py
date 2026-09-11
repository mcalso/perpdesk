"""Binance 账户只读接口。

安全边界（刻意约束，不要放宽）：
  * 只实现 GET 查询，不实现任何下单/撤单/改杠杆接口；
  * 凭据只从 backend/.env 读取，绝不落日志、绝不返回给前端；
  * secret 缺失时所有接口返回"未配置"，而不是抛错刷屏。
"""
import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from . import config

log = logging.getLogger("perpdesk.account")

_client: httpx.AsyncClient | None = None


class NotConfigured(RuntimeError):
    """缺少 API key / secret。"""


def credentials() -> tuple[str, str]:
    env = config.load_env()
    return env.get("BINANCE_API_KEY", ""), env.get("BINANCE_API_SECRET", "")


def configured() -> bool:
    key, secret = credentials()
    return bool(key and secret)


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=config.FAPI_BASE, timeout=httpx.Timeout(15.0), trust_env=False
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _signed_get(path: str, params: dict | None = None) -> Any:
    key, secret = credentials()
    if not key or not secret:
        raise NotConfigured("backend/.env 里缺少 BINANCE_API_KEY / BINANCE_API_SECRET")

    payload = dict(params or {})
    payload["timestamp"] = int(time.time() * 1000)
    payload.setdefault("recvWindow", 10000)
    query = urlencode(payload)
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    resp = await client().get(
        f"{path}?{query}&signature={signature}", headers={"X-MBX-APIKEY": key}
    )
    if resp.status_code != 200:
        # 只透出 Binance 的错误码与文案，请求里带签名，不要整体回显
        raise RuntimeError(f"binance {path} -> {resp.status_code} {resp.text[:200]}")
    return resp.json()


async def balances() -> list[dict]:
    """各币种钱包余额，只保留非零的。"""
    rows = await _signed_get("/fapi/v2/balance")
    out = []
    for r in rows:
        wallet = float(r.get("balance") or 0)
        if abs(wallet) > 1e-12:
            out.append({
                "asset": r["asset"],
                "balance": wallet,
                "available": float(r.get("availableBalance") or 0),
                "unrealized": float(r.get("crossUnPnl") or 0),
            })
    return sorted(out, key=lambda x: -abs(x["balance"]))


async def positions() -> list[dict]:
    """当前真实持仓（来自交易所，非本地流水推算）。"""
    rows = await _signed_get("/fapi/v2/positionRisk")
    out = []
    for r in rows:
        qty = float(r.get("positionAmt") or 0)
        if abs(qty) < 1e-12:
            continue
        entry = float(r.get("entryPrice") or 0)
        mark = float(r.get("markPrice") or 0)
        out.append({
            "symbol": r["symbol"],
            "qty": qty,
            "entryPrice": entry,
            "markPrice": mark,
            "unrealized": float(r.get("unRealizedProfit") or 0),
            "leverage": float(r.get("leverage") or 0),
            "liquidationPrice": float(r.get("liquidationPrice") or 0),
            "notional": abs(qty) * mark,
            "side": "LONG" if qty > 0 else "SHORT",
            "marginType": r.get("marginType", ""),
        })
    return sorted(out, key=lambda x: -x["notional"])


# Binance 限制：userTrades 的 startTime/endTime 跨度不能超过 7 天；
# 两者都不传则只返回最近 7 天。要拉更长历史必须自己按窗口滚动。
_TRADE_WINDOW_MS = 7 * 86400 * 1000


async def user_trades_range(symbol: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
    """拉取一段时间内的全部成交，自动按 7 天窗口分段并去重。"""
    end_ms = end_ms or int(time.time() * 1000)
    seen: dict[Any, dict] = {}
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(cursor + _TRADE_WINDOW_MS, end_ms)
        rows = await user_trades(symbol, cursor, chunk_end)
        for r in rows:
            seen[r["tradeId"]] = r
        # 单窗口打满 1000 条说明可能被截断，从最后一条的时间继续，避免漏单
        if len(rows) >= 1000:
            cursor = max(r["traded_at"] for r in rows) + 1
        else:
            cursor = chunk_end + 1
    return sorted(seen.values(), key=lambda r: r["traded_at"])


async def user_trades(symbol: str, start_ms: int | None = None,
                      end_ms: int | None = None, limit: int = 1000) -> list[dict]:
    """某个标的的成交明细。Binance 要求必须指定 symbol，一次最多 1000 条。"""
    params: dict[str, Any] = {"symbol": symbol.upper(), "limit": min(limit, 1000)}
    if start_ms:
        params["startTime"] = start_ms
    if end_ms:
        params["endTime"] = end_ms
    rows = await _signed_get("/fapi/v1/userTrades", params)
    return [
        {
            "symbol": r["symbol"],
            "side": "BUY" if r["side"] == "BUY" else "SELL",
            "qty": float(r["qty"]),
            "price": float(r["price"]),
            "fee": float(r.get("commission") or 0),
            "feeAsset": r.get("commissionAsset", ""),
            "realizedPnl": float(r.get("realizedPnl") or 0),
            "traded_at": int(r["time"]),
            "tradeId": r.get("id"),
            "maker": bool(r.get("maker")),
        }
        for r in rows
    ]


async def income_range(start_ms: int, end_ms: int | None = None,
                       income_type: str | None = None) -> list[dict]:
    """拉取一段时间的资金流水，自动分页并按 tranId 去重。

    与 userTrades 一样受时间窗口与单次 1000 条限制，用游标往前滚。
    """
    end_ms = end_ms or int(time.time() * 1000)
    seen: dict[Any, dict] = {}
    cursor = start_ms
    guard = 0
    while cursor < end_ms and guard < 200:
        guard += 1
        rows = await income(income_type, cursor, limit=1000)
        if not rows:
            break
        for r in rows:
            seen[r["tranId"]] = r
        if len(rows) < 1000:
            break
        nxt = max(r["t"] for r in rows) + 1
        if nxt <= cursor:          # 时间戳重复，防死循环
            break
        cursor = nxt
    return sorted(seen.values(), key=lambda r: r["t"])


async def income(income_type: str | None = None, start_ms: int | None = None,
                 limit: int = 1000) -> list[dict]:
    """资金流水：REALIZED_PNL / FUNDING_FEE / COMMISSION 等。"""
    params: dict[str, Any] = {"limit": min(limit, 1000)}
    if income_type:
        params["incomeType"] = income_type
    if start_ms:
        params["startTime"] = start_ms
    rows = await _signed_get("/fapi/v1/income", params)
    return [
        {
            "symbol": r.get("symbol") or "",
            "type": r.get("incomeType", ""),
            "amount": float(r.get("income") or 0),
            "asset": r.get("asset", ""),
            "t": int(r.get("time") or 0),
            "tranId": str(r.get("tranId") or f"{r.get('symbol','')}-{r.get('time')}-{r.get('incomeType')}"),
        }
        for r in rows
    ]


class AccountCache:
    """账户快照集中轮询。

    前端有多少个标签页都共用这一份，避免每个浏览器各打一次 Binance 签名接口
    （这台机器的出口 IP 本来就在和其他采集进程抢权重）。
    拉取失败时保留上一份快照，前端据 ageSec 自行判断新鲜度。
    """

    def __init__(self) -> None:
        self.balances: list[dict] = []
        self.positions: list[dict] = []
        self.last_ok = 0.0
        self.last_error = ""
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="account-poll")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            # 每轮都重读凭据：用户可能在服务运行中才把 secret 填进 .env
            if configured():
                try:
                    self.balances = await balances()
                    self.positions = await positions()
                    self.last_ok = time.time()
                    self.last_error = ""
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = str(exc)[:200]
                    log.warning("account snapshot refresh failed: %s", self.last_error)
            await asyncio.sleep(config.ACCOUNT_POLL_INTERVAL)

    def snapshot(self, marks: dict[str, float] | None = None) -> dict:
        """账户快照。

        `marks` 传入行情中心的实时标记价时，浮动盈亏 / 名义价值 / 占比全部按它
        重算 —— 这些量只随价格变化，用 1 秒级的行情本地算即可，不必为了让浮盈
        跳动去加快对交易所的轮询（那既费权重又更慢）。
        持仓量与开仓价仍以交易所返回的为准，只有下单成交时才会变。
        """
        marks = marks or {}
        positions = []
        live_unrealized = 0.0
        for p in self.positions:
            mark = marks.get(p["symbol"]) or p["markPrice"]
            qty, entry = p["qty"], p["entryPrice"]
            unrealized = (mark - entry) * qty if entry else p["unrealized"]
            notional = abs(qty) * mark
            live_unrealized += unrealized
            positions.append({
                **p,
                "markPrice": mark,
                "unrealized": unrealized,
                "notional": notional,
                # 交易所口径的那份留着，便于核对本地计算是否漂移
                "exchangeUnrealized": p["unrealized"],
                "live": p["symbol"] in marks,
            })

        gross = sum(p["notional"] for p in positions)
        for p in positions:
            # 占比按名义价值的绝对值算，多空都计入敞口
            p["weight"] = (p["notional"] / gross * 100) if gross else 0.0

        # 权益 = 钱包余额 + 实时浮盈（余额本身只在成交/结算时变）
        wallet = sum(b["balance"] for b in self.balances if b["asset"] in ("USDT", "USDC"))
        return {
            "balances": self.balances,
            "positions": positions,
            "equity": wallet + live_unrealized,
            "wallet": wallet,
            "totalUnrealized": live_unrealized,
            "grossNotional": gross,
            "ageSec": round(time.time() - self.last_ok, 1) if self.last_ok else None,
            "error": self.last_error,
            "pollInterval": config.ACCOUNT_POLL_INTERVAL,
        }

cache = AccountCache()
