"""USDT → 人民币的参考汇率。

**为什么不用美元官方汇率**：账户是以 USDT 计价的，而国内 USDT 的场外价
与 USD/CNY 官方中间价长期存在价差（通常是溢价）。要回答「我这些钱换成
人民币大概多少」，该用的是 USDT 实际能换到的价，不是美元的牌价。

取价方式：币安 C2C 的买、卖两侧各取若干条广告的**中位价**，再取两者中点。
  * 取中位而非最优价 —— 榜首广告常带着极端限额（只收 3 万以上之类），
    不代表实际能成交的水平；
  * 两侧取中点 —— 单取一侧会系统性地偏向买价或卖价。实测两侧中位价
    几乎相等（价差约 0.15%），中点足够中性。

⚠️ 这是从币安网页前端扒的非公开接口，随时可能变。所有失败都只降级为
「不显示人民币」，绝不能影响账户数据本身。置空 PERPDESK_FX_SOURCE 可关闭。
"""
import asyncio
import logging
import statistics
import time

import httpx

from . import config

log = logging.getLogger("perpdesk.fx")

P2P_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
UA = "Mozilla/5.0 (compatible; perpdesk/0.1)"

# 超过这个岁数就不再显示：汇率本身变得慢，但摆一个来路不明的陈旧数字
# 在权益旁边，比不显示更糟
MAX_AGE = 24 * 3600.0

_rate: float | None = None
_at: float = 0.0
_task: asyncio.Task | None = None
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0), trust_env=False,
            headers={"User-Agent": UA, "Content-Type": "application/json"},
        )
    return _client


async def _side(trade_type: str) -> float | None:
    """某一侧广告的中位价。拿不到返回 None。"""
    r = await _http().post(P2P_URL, json={
        "asset": "USDT", "fiat": "CNY", "tradeType": trade_type,
        "page": 1, "rows": 20, "payTypes": [], "publisherType": None,
    })
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"C2C 返回 success=false: {body.get('code')}")
    prices = []
    for ad in body.get("data") or []:
        try:
            p = float(ad["adv"]["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < p < 1000:              # 明显离谱的直接扔，防止一条脏数据带偏中位
            prices.append(p)
    return statistics.median(prices) if prices else None


async def refresh() -> float | None:
    """抓一次并更新缓存。失败时保留上一次的好值。"""
    global _rate, _at
    buy, sell = await asyncio.gather(_side("BUY"), _side("SELL"),
                                     return_exceptions=True)
    got = []
    for name, v in (("买侧", buy), ("卖侧", sell)):
        if isinstance(v, float):
            got.append(v)
        else:
            # gather 会把异常吞成返回值。不记下来的话，长期只有一侧能用
            # 也看不出来 —— 汇率照常显示，只是悄悄偏向了另一侧。
            log.warning("%s取价失败: %s", name, v if v is not None else "无有效报价")
    if not got:
        raise RuntimeError("买卖两侧都没取到价")
    _rate = round(sum(got) / len(got), 4)
    _at = time.time()
    return _rate


def snapshot() -> dict | None:
    """给前端看的当前汇率；没有或过期时返回 None（前端据此不显示人民币）。"""
    if _rate is None or time.time() - _at > MAX_AGE:
        return None
    return {"cny": _rate, "ageSec": round(time.time() - _at, 1),
            "source": "binance-c2c"}


async def _loop() -> None:
    delay = 5.0                       # 启动后稍等，别和首屏那批请求挤在一起
    while True:
        await asyncio.sleep(delay)
        try:
            await refresh()
            delay = config.FX_REFRESH_INTERVAL
            log.info("USDT/CNY = %s", _rate)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 退避但封顶，不要因为一个「锦上添花」的功能反复砸第三方接口
            delay = min(max(delay * 2, 60.0), 1800.0)
            log.warning("汇率刷新失败（%s），%.0fs 后重试", exc, delay)


async def start() -> None:
    global _task
    if not config.FX_SOURCE:
        log.info("汇率来源已关闭（PERPDESK_FX_SOURCE 为空）")
        return
    _task = asyncio.create_task(_loop(), name="fx-rate")


async def stop() -> None:
    global _task, _client
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    if _client is not None:
        await _client.aclose()
        _client = None
