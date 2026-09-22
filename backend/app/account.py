"""Binance 账户只读接口。

安全边界（刻意约束，不要放宽）：
  * 只实现 GET 查询，不实现任何下单/撤单/改杠杆接口；
  * 凭据从加密存储读取（见 vault.py），绝不落日志、绝不返回给前端；
  * secret 缺失时所有接口返回"未配置"，而不是抛错刷屏。
"""
import asyncio
import contextlib
import hashlib
import hmac
import logging
import time
from datetime import UTC
from typing import Any
from urllib.parse import urlencode

import httpx

from . import config, db, vault

log = logging.getLogger("perpdesk.account")

_client: httpx.AsyncClient | None = None


class NotConfiguredError(RuntimeError):
    """缺少 API key / secret。"""


def credentials(account_id: int | None = None) -> tuple[str, str]:
    """某账户的 key/secret 明文。

    只在签名请求时用，拿到就用完即弃 —— 不要缓存到模块变量里，
    也不要传给任何会被序列化的地方。
    """
    acct = db.default_account_id() if account_id is None else account_id
    try:
        return vault.get(acct, "api_key") or "", vault.get(acct, "api_secret") or ""
    except vault.VaultError as exc:
        # 解不开要吵，不能静默退回"未配置" —— 那会让人以为是没填，
        # 实际是主密钥换了、库里的凭据全成了废数据。
        log.error("账户 %s 的凭据解密失败：%s", acct, exc)
        return "", ""


def configured(account_id: int | None = None) -> bool:
    key, secret = credentials(account_id)
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


async def _signed_get(path: str, params: dict | None = None,
                      account_id: int | None = None) -> Any:
    key, secret = credentials(account_id)
    if not key or not secret:
        raise NotConfiguredError(
            f"账户 {account_id if account_id is not None else '默认'} 没有可用的 API 凭据")

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


async def balances(account_id: int | None = None) -> list[dict]:
    """各币种钱包余额，只保留非零的。"""
    rows = await _signed_get("/fapi/v2/balance", account_id=account_id)
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


async def positions(account_id: int | None = None) -> list[dict]:
    """当前真实持仓（来自交易所，非本地流水推算）。"""
    rows = await _signed_get("/fapi/v2/positionRisk", account_id=account_id)
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


async def user_trades(symbol: str, start_ms: int | None = None,
                      end_ms: int | None = None, limit: int = 1000,
                      from_id: int | None = None,
                      account_id: int | None = None) -> list[dict]:
    """某个标的的成交明细。Binance 要求必须指定 symbol，一次最多 1000 条。

    传 from_id 时按 tradeId 递增取（推荐，见 user_trades_all）；
    Binance 不允许 fromId 与 startTime/endTime 同时使用。
    """
    params: dict[str, Any] = {"symbol": symbol.upper(), "limit": min(limit, 1000)}
    if from_id is not None:
        params["fromId"] = from_id
    else:
        if start_ms:
            params["startTime"] = start_ms
        if end_ms:
            params["endTime"] = end_ms
    rows = await _signed_get("/fapi/v1/userTrades", params, account_id=account_id)
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
            "tradeId": int(r["id"]),
            "maker": bool(r.get("maker")),
        }
        for r in rows
    ]


async def user_trades_all(symbol: str, start_ms: int | None = None,
                          account_id: int | None = None) -> list[dict]:
    """拉取某标的的全部成交，按 tradeId 分页。

    **必须用 fromId 分页，不能用时间游标。** 按 `max(time)+1` 往前滚会跳过同一
    毫秒内的其他成交：实测某高频标的 3 天内成交 4623 笔，时间游标只拿到 4574 笔，
    漏掉的 49 笔直接把净持仓算成了 4147（真实为 0）——本地推算的持仓与已实现
    盈亏会因此严重失真。tradeId 严格递增，不存在这个问题。

    start_ms 只用于结果过滤，分页本身始终从头遍历，避免边界漏单。
    """
    out: dict[int, dict] = {}
    from_id = 0
    for _ in range(200):                      # 20 万笔封顶，防御性上限
        rows = await user_trades(symbol, from_id=from_id, limit=1000,
                                 account_id=account_id)
        if not rows:
            break
        for r in rows:
            out[r["tradeId"]] = r
        if len(rows) < 1000:
            break
        from_id = max(r["tradeId"] for r in rows) + 1
    fills = sorted(out.values(), key=lambda r: r["traded_at"])
    return [f for f in fills if not start_ms or f["traded_at"] >= start_ms]


async def income_range(start_ms: int, end_ms: int | None = None,
                       income_type: str | None = None,
                       account_id: int | None = None) -> list[dict]:
    """拉取一段时间的资金流水，用 page 翻页。

    两个踩过的坑：

    * **不能用时间游标分页**（`max(time)+1`）——同一毫秒内的其他记录会被整段
      跳过，实测某标的 612 笔成交对应的 REALIZED_PNL 一条都没同步到。
    * **income 没有 userTrades 那种 7 天跨度限制**，不要照搬着切窗口：
      365 天切成 52 段、每段权重 30，一次同步就打掉 1500+ 权重直接 429。
      整段时间一次给，靠 page 翻页即可。
    """
    end_ms = end_ms or int(time.time() * 1000)
    seen: dict[Any, dict] = {}
    for page in range(1, 201):               # 20 万条封顶
        rows = await income(income_type, start_ms, end_ms, page=page, limit=1000,
                            account_id=account_id)
        if not rows:
            break
        for r in rows:
            seen[r["tranId"]] = r
        if len(rows) < 1000:
            break
        await asyncio.sleep(0.25)            # 轻度节流，别把权重打满
    return sorted(seen.values(), key=lambda r: r["t"])


async def income(income_type: str | None = None, start_ms: int | None = None,
                 end_ms: int | None = None, page: int | None = None,
                 limit: int = 1000, account_id: int | None = None) -> list[dict]:
    """资金流水：REALIZED_PNL / FUNDING_FEE / COMMISSION / TRANSFER 等。"""
    params: dict[str, Any] = {"limit": min(limit, 1000)}
    if income_type:
        params["incomeType"] = income_type
    if start_ms:
        params["startTime"] = start_ms
    if end_ms:
        params["endTime"] = end_ms
    if page:
        params["page"] = page
    rows = await _signed_get("/fapi/v1/income", params, account_id=account_id)
    return [
        {
            "symbol": r.get("symbol") or "",
            "type": r.get("incomeType", ""),
            "amount": float(r.get("income") or 0),
            "asset": r.get("asset", ""),
            "t": int(r.get("time") or 0),
            # tranId 在同一笔交易的不同科目间可能重复，拼上科目与标的才唯一，
            # 否则 INSERT OR IGNORE 会把同一笔的手续费或盈亏吞掉一条
            "tranId": (f"{r.get('tranId') or r.get('time')}"
                       f"-{r.get('incomeType', '')}-{r.get('symbol', '')}"),
        }
        for r in rows
    ]


class AccountCache:
    """账户快照集中轮询。

    前端有多少个标签页都共用这一份，避免每个浏览器各打一次 Binance 签名接口
    （这台机器的出口 IP 本来就在和其他采集进程抢权重）。
    拉取失败时保留上一份快照，前端据 ageSec 自行判断新鲜度。
    """

    def __init__(self, account_id: int) -> None:
        self.account_id = account_id
        self.balances: list[dict] = []
        self.positions: list[dict] = []
        # 持仓变化时回调，让行情中心把这些标的加入实时订阅
        self.on_positions: Any = None
        self.last_ok = 0.0
        self.last_error = ""
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._loop(), name=f"account-poll-{self.account_id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            # 每轮都重读凭据：用户可能在服务运行中才把凭据填进去
            if configured(self.account_id):
                try:
                    self.balances = await balances(self.account_id)
                    prev = {p["symbol"] for p in self.positions}
                    self.positions = await positions(self.account_id)
                    self.last_ok = time.time()
                    self.last_error = ""
                    now_syms = {p["symbol"] for p in self.positions}
                    if now_syms != prev and self.on_positions:
                        self.on_positions(sorted(now_syms))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = str(exc)[:200]
                    log.warning("账户 %s 快照刷新失败：%s",
                                self.account_id, self.last_error)
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
            "accountId": self.account_id,
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

class CacheRegistry:
    """每个启用账户一份快照轮询。

    账户在运行期可增删，所以注册表要跟着变：`sync()` 对照 accounts 表起停任务。
    已存在的缓存**不重建** —— 重建会丢掉上一份快照，界面上表现为持仓瞬间
    清空又冒出来，而且会白白多打一轮交易所接口。

    权重提醒：每个账户各自轮询 balance + positionRisk（各权重 5），
    按 ACCOUNT_POLL_INTERVAL 计算，账户数乘上去就是总消耗。
    签名接口的权重按 API key 计，不同账户互不挤占；但出口 IP 的总限额是共享的。
    """

    def __init__(self) -> None:
        self._caches: dict[int, AccountCache] = {}
        # 任一账户持仓变化时回调，传入所有账户持仓标的的并集
        self.on_positions: Any = None

    def ids(self) -> list[int]:
        return sorted(self._caches)

    def get(self, account_id: int | None = None) -> AccountCache:
        """取某账户的缓存。account_id 为 None 时给默认账户。

        账户存在但还没起轮询（刚建出来）时临时建一个空缓存返回，
        让界面显示"加载中"而不是 500。
        """
        acct = db.default_account_id() if account_id is None else account_id
        if acct not in self._caches:
            self._caches[acct] = AccountCache(acct)
        return self._caches[acct]

    def position_symbols(self) -> list[str]:
        """所有账户持仓标的的并集 —— 实时订阅要覆盖到每一个。"""
        syms: set[str] = set()
        for c in self._caches.values():
            syms |= {p["symbol"] for p in c.positions}
        return sorted(syms)

    def _fanout(self, _changed: list[str] | None = None) -> None:
        if self.on_positions:
            self.on_positions(self.position_symbols())

    async def sync(self) -> None:
        """对照 accounts 表起停轮询任务。新增/停用账户后调用。"""
        want = {a["id"] for a in db.list_accounts(enabled_only=True)}
        for acct in want - set(self._caches):
            cache = self._caches.setdefault(acct, AccountCache(acct))
            cache.on_positions = self._fanout
            await cache.start()
            log.info("账户 %s 开始轮询", acct)
        for acct in set(self._caches) - want:
            await self._caches.pop(acct).stop()
            log.info("账户 %s 停止轮询", acct)
        self._fanout()

    async def stop_all(self) -> None:
        for cache in list(self._caches.values()):
            await cache.stop()
        self._caches.clear()


registry = CacheRegistry()


# ---------- 历史成交导出（超出 userTrades 保留期的唯一途径） ----------

async def request_trade_export(start_ms: int, end_ms: int,
                               account_id: int | None = None) -> str:
    """申请异步导出成交历史，返回 downloadId。

    为什么需要它：`userTrades` 即便用 fromId=0 也只覆盖一段保留期，
    更早的成交查不到（实测账户 2025-10 的成交在 userTrades 里完全取不到，
    fromId=0 返回的是保留期内的最早一笔，很容易误以为那就是账户起点）。
    异步导出不需要指定 symbol，也能覆盖已下架的合约。

    注意：该接口权重高且**每月仅允许 5 次**，不要放进定时任务。
    """
    data = await _signed_get("/fapi/v1/trade/asyn",
                             {"startTime": start_ms, "endTime": end_ms},
                             account_id=account_id)
    return str(data.get("downloadId") or "")


async def get_export_url(download_id: str,
                         account_id: int | None = None) -> str | None:
    """查询导出任务；未完成返回 None。"""
    data = await _signed_get("/fapi/v1/trade/asyn/id", {"downloadId": download_id},
                             account_id=account_id)
    if data.get("status") == "completed" and data.get("url"):
        return data["url"]
    return None


def parse_trade_export(raw: bytes) -> list[dict]:
    """解析导出的 ZIP/CSV。

    导出的字段与 userTrades 不同：时间是 UTC 字符串、手续费带币种后缀
    （"0.0048 USDT"）、盈亏可能是科学计数法（"0E-8"），且带 Position Side ——
    账户用过双向持仓模式时同一标的会同时有 LONG/SHORT 记录。
    """
    import csv
    import io
    import zipfile
    from datetime import datetime

    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            text = z.read(z.namelist()[0]).decode("utf-8-sig")
    else:
        text = raw.decode("utf-8-sig", "replace")

    out = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            ts = int(datetime.strptime(r["Time(UTC)"], "%Y-%m-%d %H:%M:%S")
                     .replace(tzinfo=UTC).timestamp() * 1000)
            out.append({
                "symbol": r["Symbol"],
                "side": "BUY" if r["Side"].upper() == "BUY" else "SELL",
                "qty": abs(float(r["Quantity"])),
                "price": float(r["Price"]),
                "fee": float(r["Fee"].split()[0]) if r.get("Fee") else 0.0,
                "realizedPnl": float(r.get("Realized Profit") or 0),
                "traded_at": ts,
                "tradeId": int(r["Trade Id"]),
                "positionSide": r.get("Position Side", ""),
            })
        except (KeyError, ValueError):
            continue
    return sorted(out, key=lambda x: x["traded_at"])


async def download_export(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=120, trust_env=False,
                                 follow_redirects=True) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        return resp.content
