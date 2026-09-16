"""资讯源适配器。

**这些都是从各家网页前端扒出来的非公开接口**，没有文档、没有 SLA，
随时可能改路径、加签名或封 IP。所以：

  * 每个源独立，一个挂掉不影响其他源，也不影响整站；
  * 解析失败记日志继续，不抛到轮询循环外面；
  * 源列表可配置，用户可以全关。

实测过但不可用的（2026-09）：BlockBeats 开放接口恒返回空数组、
PANews 与金十日历 CDN 在境外机器上 DNS 解析不了、Odaily 各路径均 404、
ChainCatcher 只有 SSR 页面、Bitpush 挂着 Cloudflare 盾。
"""
from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("perpdesk.news")

# 金十的时间戳不带时区，实测是北京时间（对比服务器 UTC 时钟确认过）。
# 当成 UTC 解析会整体偏 8 小时，而且因为条目本来就新，偏了也不显眼。
CST = timezone(timedelta(hours=8))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


@dataclass
class Flash:
    source: str
    source_id: str
    ts: int                     # 毫秒
    content: str
    title: str = ""
    link: str = ""
    important: bool = False
    tags: list[str] = field(default_factory=list)


class Jin10:
    """金十数据 7×24 快讯。

    用 www.jin10.com/flash_newest.js —— 它是页面直接引用的静态文件，
    没有签名，返回最近 50 条（实测覆盖约 24 分钟）。
    带签名的 flash-api.jin10.com 无 key 返回 502，不走它。
    """

    name = "jin10"
    url = "https://www.jin10.com/flash_newest.js"

    # 形如 【标题】正文…… ——把标题拆出来单独显示
    _BRACKET = re.compile(r"^【([^】]{2,60})】\s*(.*)$", re.S)
    _CJK = re.compile(r"[\u4e00-\u9fff]")
    # 经济数据类快讯会用 <b> 给关键数字加粗。前端绝不渲染原始 HTML，
    # 所以在源头剥掉 —— 顺带避免把标签当正文参与标的匹配。
    _TAG = re.compile(r"<[^>]{0,80}>")

    def __init__(self, lang: str = "cn") -> None:
        # 金十每条消息中英文各发一遍（实测 50 条正好 25 中 25 英），
        # 不过滤的话列表里每条都重复出现，可读性直接减半。
        # 按有没有中日韩字符判断，比去猜 channel 的语义可靠。
        self.lang = lang

    def _clean(self, raw: str | None) -> str:
        return html.unescape(self._TAG.sub("", raw or "")).strip()

    def _keep(self, text: str) -> bool:
        if self.lang == "all":
            return True
        has_cjk = bool(self._CJK.search(text))
        return has_cjk if self.lang == "cn" else not has_cjk

    async def fetch(self, client: httpx.AsyncClient) -> list[Flash]:
        resp = await client.get(
            self.url, headers={"User-Agent": UA, "Referer": "https://www.jin10.com/"})
        resp.raise_for_status()
        text = resp.text
        try:
            raw = json.loads(text[text.index("["):text.rindex("]") + 1])
        except (ValueError, IndexError) as exc:
            raise RuntimeError(f"金十返回的不是预期的 JS 数组：{text[:80]!r}") from exc

        out: list[Flash] = []
        for item in raw:
            data = item.get("data") or {}
            content = self._clean(data.get("content"))
            title = self._clean(data.get("title"))
            if not content and not title:
                continue                      # 纯图片/广告位，跳过
            if not self._keep(f"{title}{content}"):
                continue
            if not title:
                m = self._BRACKET.match(content)
                if m:
                    title, content = m.group(1).strip(), m.group(2).strip() or content
            try:
                ts = int(datetime.strptime(item["time"], "%Y-%m-%d %H:%M:%S")
                         .replace(tzinfo=CST).timestamp() * 1000)
            except (KeyError, ValueError):
                continue
            out.append(Flash(
                source=self.name,
                source_id=str(item.get("id") or ts),
                ts=ts,
                title=title,
                content=content,
                link=(data.get("source_link") or data.get("link") or "").strip(),
                important=bool(item.get("important")),
                tags=[t for t in (item.get("tags") or []) if isinstance(t, str)],
            ))
        return out


def build_sources(lang: str = "cn") -> dict[str, object]:
    return {s.name: s for s in (Jin10(lang),)}


SOURCES: dict[str, object] = build_sources()
