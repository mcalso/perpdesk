"""标的图标服务。

图标统一由后端代理并落盘缓存，不让浏览器直连外部 CDN —— 一来国内浏览器未必
连得上，二来一次抓取可以长期复用。

来源用 TradingView 的 symbol logo：加密与股票代币化合约都覆盖得到
（BTC → crypto/XTVCBTC，INTC → intel，SPCX → spacex）。
拿不到的（如 HK0625）生成首字母占位图，保证 100% 有图可显示。
"""
import asyncio
import hashlib
import json
import logging
import re

import httpx

from . import config

log = logging.getLogger("perpdesk.icons")

SEARCH_URL = "https://symbol-search.tradingview.com/symbol_search/"
LOGO_URL = "https://s3-symbol-logo.tradingview.com/{logoid}--big.svg"

# symbol -> logoid 的解析结果，落盘避免每次启动重新 search
_MAP_FILE = config.ICON_DIR / "_logoid.json"
_logoid_map: dict[str, dict] = {}
_map_loaded = False

# 同时对外的请求数，别把 TradingView 打急了
_sem = asyncio.Semaphore(6)
# 同一 symbol 并发请求只解析一次
_inflight: dict[str, asyncio.Task] = {}

_search_client: httpx.AsyncClient | None = None
_logo_client: httpx.AsyncClient | None = None

# 占位图配色，按 symbol 哈希稳定选取
_PALETTE = ["#2962ff", "#26a69a", "#ef5350", "#ab47bc", "#ff9800",
            "#26c6da", "#9ccc65", "#5c6bc0", "#ec407a", "#8d6e63"]


def _clients() -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
    global _search_client, _logo_client
    if _search_client is None:
        # symbol search 走系统代理更稳；logo 静态资源直连即可
        _search_client = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0), trust_env=True, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0",
                     "Origin": "https://www.tradingview.com",
                     "Referer": "https://www.tradingview.com/"},
        )
    if _logo_client is None:
        _logo_client = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0), trust_env=False, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
    return _search_client, _logo_client


async def close() -> None:
    global _search_client, _logo_client
    for c in (_search_client, _logo_client):
        if c is not None:
            await c.aclose()
    _search_client = _logo_client = None


def _load_map() -> None:
    global _map_loaded
    if _map_loaded:
        return
    config.ICON_DIR.mkdir(parents=True, exist_ok=True)
    if _MAP_FILE.is_file():
        try:
            raw = json.loads(_MAP_FILE.read_text())
            # 兼容旧格式（值是裸 logoid 字符串）
            _logoid_map.update({
                k: (v if isinstance(v, dict) else {"logoid": v, "tv": bool(v)})
                for k, v in raw.items()
            })
        except Exception as exc:
            log.warning("logoid map unreadable, starting fresh: %s", exc)
    _map_loaded = True


def _save_map() -> None:
    try:
        _MAP_FILE.write_text(json.dumps(_logoid_map, indent=0, sort_keys=True))
    except Exception as exc:
        log.warning("cannot persist logoid map: %s", exc)


def _safe(symbol: str) -> str:
    """把 symbol 变成安全的文件名。

    不能简单剥掉非 ASCII：Binance 有中文 symbol（龙虾USDT / 哈基米USDT /
    币安人生USDT …），剥完全都变成 "USDT"，5 个标的会共用同一个缓存文件，
    谁先请求谁的图标就覆盖掉其他人的。非 ASCII 一律走哈希，保证唯一。
    """
    up = symbol.upper()
    if up.isascii():
        return re.sub(r"[^A-Z0-9_]", "", up)[:32]
    return "h" + hashlib.md5(up.encode("utf-8")).hexdigest()[:20]


def placeholder(symbol: str, base: str = "") -> bytes:
    """首字母占位图。没有外部依赖，永远可用。"""
    label = (base or symbol.removesuffix("USDT") or "?")[:3].upper()
    h = int(hashlib.md5(symbol.encode()).hexdigest()[:8], 16)
    color = _PALETTE[h % len(_PALETTE)]
    size = 14 if len(label) == 3 else 17
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" width="40" height="40">'
        f'<circle cx="20" cy="20" r="20" fill="{color}"/>'
        f'<text x="20" y="20" fill="#fff" font-size="{size}" font-weight="600" '
        f'font-family="-apple-system,Segoe UI,Roboto,sans-serif" '
        f'text-anchor="middle" dominant-baseline="central">{label}</text></svg>'
    ).encode()


async def _resolve_logoid(symbol: str) -> str:
    """查 TradingView 拿 logoid，顺带记下它有没有这个符号。

    查不到 logoid 记空串，避免反复无效查询；`tv` 字段供图表页判断该用
    TradingView widget 还是自建图表（中文 symbol 在 TradingView 上不存在）。
    """
    _load_map()
    if symbol in _logoid_map:
        return _logoid_map[symbol].get("logoid", "")

    search, _ = _clients()
    logoid, has_tv = "", False
    try:
        resp = await search.get(SEARCH_URL, params={"text": symbol, "exchange": "BINANCE"})
        if resp.status_code == 200:
            want = f"{symbol}.P"
            for hit in resp.json():
                name = re.sub(r"</?em>", "", hit.get("symbol", "")).upper()
                if hit.get("type") == "swap" and name == want:
                    has_tv = True
                    logoid = hit.get("logoid") or hit.get("base-currency-logoid") or ""
                    break
    except Exception as exc:
        log.debug("logoid lookup failed for %s: %s", symbol, exc)
        return ""            # 不写缓存，下次还能重试

    _logoid_map[symbol] = {"logoid": logoid, "tv": has_tv}
    _save_map()
    return logoid


async def tradingview_supported(symbol: str) -> bool:
    """TradingView 是否有该合约的图表（中文 symbol 一律没有）。"""
    symbol = symbol.upper()
    _load_map()
    if symbol not in _logoid_map:
        await _resolve_logoid(symbol)
    return bool(_logoid_map.get(symbol, {}).get("tv"))


async def _fetch(symbol: str, base: str) -> tuple[bytes, str]:
    """返回 (内容, content-type)，并落盘缓存。"""
    path = config.ICON_DIR / f"{_safe(symbol)}.svg"
    async with _sem:
        logoid = await _resolve_logoid(symbol)
        if logoid:
            _, logo = _clients()
            try:
                r = await logo.get(LOGO_URL.format(logoid=logoid))
                if r.status_code == 200 and len(r.content) > 80:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(r.content)
                    return r.content, "image/svg+xml"
            except Exception as exc:
                log.debug("logo download failed for %s: %s", symbol, exc)

        # 兜底占位也落盘：避免每次请求都重跑一遍解析流程
        data = placeholder(symbol, base)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data, "image/svg+xml"


async def get(symbol: str, base: str = "") -> tuple[bytes, str]:
    symbol = _safe(symbol)
    if not symbol:
        return placeholder("?"), "image/svg+xml"

    path = config.ICON_DIR / f"{symbol}.svg"
    if path.is_file():
        return path.read_bytes(), "image/svg+xml"

    # 同一 symbol 的并发请求合流，首屏几百个图标不会放大成几百次外部请求
    task = _inflight.get(symbol)
    if task is None:
        task = asyncio.create_task(_fetch(symbol, base))
        _inflight[symbol] = task
        task.add_done_callback(lambda _t, s=symbol: _inflight.pop(s, None))
    return await task


def stats() -> dict:
    _load_map()
    cached = len(list(config.ICON_DIR.glob("*.svg"))) if config.ICON_DIR.is_dir() else 0
    resolved = sum(1 for v in _logoid_map.values() if v.get("logoid"))
    return {"cachedFiles": cached, "resolved": resolved,
            "noLogo": len(_logoid_map) - resolved, "inflight": len(_inflight)}


async def prewarm(symbols_provider) -> None:
    """后台把全市场图标抓齐。

    symbols_provider 是个可调用对象，延迟到真正开跑时才取 symbol 列表
    （启动瞬间 hub.meta 可能还没就绪）。
    """
    if not config.ICON_PREWARM_DELAY:
        return
    await asyncio.sleep(config.ICON_PREWARM_DELAY)

    symbols = [s for s in symbols_provider() if not (config.ICON_DIR / f"{_safe(s)}.svg").is_file()]
    if not symbols:
        log.info("icon prewarm: nothing to do")
        return

    log.info("icon prewarm: %d symbols to fetch", len(symbols))
    done = 0
    for i in range(0, len(symbols), config.ICON_PREWARM_BATCH):
        batch = symbols[i:i + config.ICON_PREWARM_BATCH]
        try:
            await asyncio.gather(*[get(s) for s in batch], return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("icon prewarm batch failed: %s", exc)
        done += len(batch)
        if done % 60 == 0:
            log.info("icon prewarm: %d/%d", done, len(symbols))
        await asyncio.sleep(config.ICON_PREWARM_PAUSE)
    log.info("icon prewarm done: %s", stats())
