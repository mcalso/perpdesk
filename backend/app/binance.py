"""Binance U 本位合约公开 REST 接口封装（只读，无需 API key）。"""
import asyncio
import logging
import time
from typing import Any

import httpx

from . import config

log = logging.getLogger("perpdesk.binance")

# Binance 限速码：429 超频、418 已被临时封禁
_RATE_LIMIT_CODES = {418, 429}

# 绕开系统代理：多数环境下直连 Binance 更快也更稳
_client: httpx.AsyncClient | None = None

# Binance 合约类型：PERPETUAL 是加密永续，TRADIFI_PERPETUAL 是股票/指数代币化永续
# （underlyingType = EQUITY / HK_EQUITY 等）。两者都要收，否则持有股票合约的账户
# 会出现"有持仓但查不到标记价"。
ALLOWED_CONTRACT_TYPES = {"PERPETUAL", "TRADIFI_PERPETUAL"}

_exchange_info_cache: dict[str, Any] = {}
_exchange_info_at: float = 0.0


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=config.FAPI_BASE,
            timeout=httpx.Timeout(10.0),
            trust_env=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; perpdesk/0.1)"},
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _get(path: str, params: dict | None = None, retries: int = 3) -> Any:
    """带退避重试。

    市场数据限速是 IP 维度的：出口 IP 若和其他程序共用（同机的采集脚本、NAT 后的
    其他机器），权重会叠加，偶发 418/429 属正常，退避重试即可，不要让调用方直接炸。
    """
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client().get(path, params=params)
            if resp.status_code in _RATE_LIMIT_CODES:
                retry_after = float(resp.headers.get("Retry-After") or delay)
                nap = min(retry_after, 30.0)
                log.warning(
                    "binance %s rate-limited (%s), Retry-After=%.0fs, sleep %.1fs [%d/%d]",
                    path, resp.status_code, retry_after, nap, attempt + 1, retries + 1,
                )
                if attempt < retries:
                    await asyncio.sleep(nap)
                    delay = min(delay * 2, 30.0)
                    continue
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            last_exc = exc
            if attempt >= retries:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
    raise last_exc if last_exc else RuntimeError(f"binance GET {path} failed")


async def exchange_info(force: bool = False, retries: int = 3) -> dict[str, dict]:
    """返回 {symbol: meta}，只含正在交易的 USDT 永续。带 TTL 缓存。"""
    global _exchange_info_at
    now = time.time()
    if not force and _exchange_info_cache and now - _exchange_info_at < config.EXCHANGE_INFO_TTL:
        return _exchange_info_cache

    data = await _get("/fapi/v1/exchangeInfo", retries=retries)
    fresh = {}
    for s in data.get("symbols", []):
        if (
            s.get("quoteAsset") == "USDT"
            and s.get("contractType") in ALLOWED_CONTRACT_TYPES
            and s.get("status") == "TRADING"
        ):
            underlying = s.get("underlyingType", "COIN")
            fresh[s["symbol"]] = {
                "symbol": s["symbol"],
                "base": s["baseAsset"],
                "quote": s["quoteAsset"],
                "pricePrecision": s["pricePrecision"],
                "qtyPrecision": s["quantityPrecision"],
                "onboardDate": s.get("onboardDate", 0),
                "contractType": s.get("contractType", ""),
                "underlyingType": underlying,
                "assetClass": _asset_class(underlying),
                "sector": _sector(s.get("underlyingSubType")),
            }
    _exchange_info_cache.clear()
    _exchange_info_cache.update(fresh)
    _exchange_info_at = now
    return _exchange_info_cache


async def premium_index(retries: int = 3) -> dict[str, dict]:
    """全市场标记价 + 当期资金费率 + 下次结算时间。"""
    rows = await _get("/fapi/v1/premiumIndex", retries=retries)
    out = {}
    for r in rows:
        out[r["symbol"]] = {
            "markPrice": float(r["markPrice"]),
            "indexPrice": float(r["indexPrice"]) if r.get("indexPrice") else 0.0,
            "fundingRate": float(r.get("lastFundingRate") or 0.0),
            "nextFundingTime": int(r.get("nextFundingTime") or 0),
        }
    return out


async def tickers_24h(retries: int = 3) -> dict[str, dict]:
    """全市场 24h 行情。本机唯一可用的全市场数据源（权重约 80）。"""
    rows = await _get("/fapi/v1/ticker/24hr", retries=retries)
    return {r["symbol"]: _norm_ticker(r) for r in rows}


# underlyingSubType 末尾那个是大类桶（Crypto / TradFi），不是板块。
# 真正的板块标签排在它前面，例如 ['DeFi', 'Crypto']、['Alpha', 'DeFi', 'Crypto']。
_SECTOR_BUCKETS = frozenset({"Crypto", "TradFi"})


def _sector(sub_types: list[str] | None) -> str:
    """从 underlyingSubType 取主板块，没有细分的归 other。

    只取**第一个**非桶标签，一个标的只归一类。少数标的带多个板块
    （实测 2 个，如 ['Alpha','DeFi','Crypto']），若按多归属处理，
    标签页的计数加起来会大于总数，「全部 526」和分项之和对不上，
    筛选行为也变得有歧义 —— 对一张用来扫盘的表，这个代价不值得。

    币安把 Alpha 排在具体板块之前，这个顺序正好符合需要：Alpha 是上币
    层级（早期项目，风险等级不同），比「它属于哪个赛道」更该先看见。
    """
    for t in sub_types or []:
        if t not in _SECTOR_BUCKETS:
            return t
    return "other"


def _asset_class(underlying_type: str) -> str:
    """把 Binance 的 underlyingType 归成前端好展示的几类。"""
    if underlying_type == "COIN":
        return "crypto"
    if underlying_type == "HK_EQUITY":
        return "hk_equity"
    if underlying_type == "EQUITY":
        return "us_equity"
    if underlying_type == "INDEX":
        return "index"
    return underlying_type.lower() or "other"


def _norm_ticker(r: dict) -> dict:
    """REST 与 WS 字段名不同，统一成内部结构。

    REST 用完整字段名（symbol/lastPrice/...），WS 用缩写（s/c/...）。
    每个字段都要两边都认，symbol 也不例外。
    """
    return {
        "symbol": r.get("symbol") or r.get("s") or "",
        "last": float(r.get("lastPrice") or r.get("c") or 0),
        "open": float(r.get("openPrice") or r.get("o") or 0),
        "high": float(r.get("highPrice") or r.get("h") or 0),
        "low": float(r.get("lowPrice") or r.get("l") or 0),
        "chgPct": float(r.get("priceChangePercent") or r.get("P") or 0),
        "quoteVolume": float(r.get("quoteVolume") or r.get("q") or 0),
        "volume": float(r.get("volume") or r.get("v") or 0),
        "trades": int(r.get("count") or r.get("n") or 0),
        "ts": int(r.get("closeTime") or r.get("E") or 0),
    }


def norm_ws_ticker(r: dict) -> dict:
    """!ticker@arr 的单条推送，字段是缩写形式。"""
    return _norm_ticker(r)


async def klines(symbol: str, interval: str, limit: int = 500) -> list[dict]:
    rows = await _get(
        "/fapi/v1/klines",
        {"symbol": symbol.upper(), "interval": interval, "limit": min(limit, 1500)},
    )
    return [
        {
            "t": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
            "quoteVolume": float(r[7]),
        }
        for r in rows
    ]
