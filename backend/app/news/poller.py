"""资讯轮询：定时抓各源 → 关联标的 → 落库。"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from .. import config, db
from . import relevance
from .sources import Flash, build_sources

log = logging.getLogger("perpdesk.news")


class NewsPoller:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        # {baseAsset: symbol}。由 main 注入一个取值函数而不是一份快照：
        # 启动时 exchangeInfo 可能还在 418 退避，那会儿抓到的快讯若按空映射
        # 入库，就**永久**没有标的关联了 —— INSERT OR IGNORE 之后不会再匹配。
        # 每轮现取，映射晚到几十秒也只影响那一轮。
        self.symbol_provider = None
        self._sources = build_sources(config.NEWS_LANG)
        self.symbols: dict[str, str] = {}
        self.last_ok = 0.0
        self.last_error = ""
        self.inserted = 0

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            # 与行情客户端一致：不走系统代理，直连更快也更可预期
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0), trust_env=False)
        return self._client

    async def start(self) -> None:
        if not config.NEWS_SOURCES:
            log.info("未启用任何资讯源，轮询不启动")
            return
        self._task = asyncio.create_task(self._loop(), name="news-poll")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:                     # 轮询循环不能被任何源打死
                self.last_error = str(exc)[:200]
                log.warning("资讯轮询异常：%s", self.last_error)
            await asyncio.sleep(config.NEWS_POLL_INTERVAL)

    async def poll_once(self) -> int:
        """抓一轮，返回新增条数。各源互相独立，一个失败不影响其他。"""
        if self.symbol_provider is not None:
            try:
                self.symbols = self.symbol_provider() or self.symbols
            except Exception:
                pass          # 取不到就沿用上一轮，总比丢关联好
        rows: list[tuple] = []
        errors: list[str] = []
        for name in config.NEWS_SOURCES:
            src = self._sources.get(name)
            if src is None:
                continue
            try:
                flashes = await src.fetch(self.client())
            except Exception as exc:
                errors.append(f"{name}: {str(exc)[:80]}")
                log.warning("资讯源 %s 抓取失败：%s", name, exc)
                continue
            rows += [self._row(f) for f in flashes]

        inserted = db.upsert_flashes(rows) if rows else 0
        self.inserted += inserted
        if rows:
            self.last_ok = time.time()
        self.last_error = "；".join(errors)

        # 资讯的价值随时间衰减很快，留着只占地方
        if inserted:
            cutoff = int((time.time() - config.NEWS_KEEP_DAYS * 86400) * 1000)
            db.purge_flashes(cutoff)
        return inserted

    def _row(self, f: Flash) -> tuple:
        text = f"{f.title} {f.content}".strip()
        syms = relevance.match_symbols(text, self.symbols) if self.symbols else []
        cats = relevance.categories(text)
        return (f.source, f.source_id, f.ts, f.title, f.content, f.link,
                1 if f.important else 0,
                json.dumps(cats + f.tags, ensure_ascii=False),
                json.dumps(syms, ensure_ascii=False))

    def status(self) -> dict:
        stats = db.flash_stats()
        return {
            "sources": list(config.NEWS_SOURCES),
            "pollInterval": config.NEWS_POLL_INTERVAL,
            "total": stats["total"],
            "latest": stats["latest"],
            "ageSec": round(time.time() - self.last_ok, 1) if self.last_ok else None,
            "error": self.last_error,
            "symbolsKnown": len(self.symbols),
        }


poller = NewsPoller()
