"""合约元信息：板块（underlyingSubType）与大类（underlyingType）的归并。

这块的价值在于**别再把 526 个加密标的堆成一坨**。币安其实给了细分标签，
项目早期完全没读，行情页上 73% 的标的挤在一个「加密」格子里。
"""
import asyncio

import pytest

from backend.app import binance
from backend.app.hub import TickerHub


# ---------------- 板块 ----------------

@pytest.mark.parametrize("sub_types, expect", [
    (["DeFi", "Crypto"], "DeFi"),
    (["Meme", "Crypto"], "Meme"),
    (["Pre-IPO", "TradFi"], "Pre-IPO"),
    # 桶标签（Crypto / TradFi）不是板块，只打了桶的落到 other
    (["Crypto"], "other"),
    (["TradFi"], "other"),
    ([], "other"),
    (None, "other"),
])
def test_sector_extraction(sub_types, expect):
    assert binance._sector(sub_types) == expect


@pytest.mark.parametrize("sub_types, expect", [
    (["Alpha", "DeFi", "Crypto"], "Alpha"),
    (["Alpha", "AI", "Crypto"], "Alpha"),
])
def test_multi_tag_takes_the_first(sub_types, expect):
    """带多个板块时只取第一个，一个标的只归一类。

    若按多归属处理，标签页的计数加起来会大于总数 ——「加密 526」和分项
    之和对不上，用户没法判断筛选到底选中了什么。币安把 Alpha 排在具体
    板块之前，这个顺序正合用：Alpha 是上币层级（早期项目、风险不同），
    比赛道更该先看见。
    """
    assert binance._sector(sub_types) == expect


def test_sectors_partition_exactly():
    """板块必须是**划分**：每个标的恰好归一类，计数之和等于总数。"""
    universe = [
        ["DeFi", "Crypto"], ["DeFi", "Crypto"], ["Meme", "Crypto"],
        ["Alpha", "AI", "Crypto"], ["Crypto"], ["TradFi"],
    ]
    got = [binance._sector(x) for x in universe]
    assert len(got) == len(universe)
    assert sorted(got) == ["Alpha", "DeFi", "DeFi", "Meme", "other", "other"]


# ---------------- 大类 ----------------

@pytest.mark.parametrize("underlying, expect", [
    ("COIN", "crypto"), ("EQUITY", "us_equity"), ("HK_EQUITY", "hk_equity"),
    ("INDEX", "index"), ("COMMODITY", "commodity"), ("KR_EQUITY", "kr_equity"),
    # 币安新增品类时不该报错，小写化后原样透出，前端标签表兜不住就显示原文
    ("SOMETHING_NEW", "something_new"), ("", "other"),
])
def test_asset_class(underlying, expect):
    assert binance._asset_class(underlying) == expect


# ---------------- 端到端：字段真的落进 meta ----------------

SAMPLE = {"symbols": [
    {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT",
     "contractType": "PERPETUAL", "status": "TRADING",
     "pricePrecision": 2, "quantityPrecision": 3,
     "underlyingType": "COIN", "underlyingSubType": ["Layer-1", "Crypto"]},
    {"symbol": "NVDAUSDT", "baseAsset": "NVDA", "quoteAsset": "USDT",
     "contractType": "TRADIFI_PERPETUAL", "status": "TRADING",
     "pricePrecision": 2, "quantityPrecision": 3,
     "underlyingType": "EQUITY", "underlyingSubType": ["TradFi"]},
    {"symbol": "GONEUSDT", "baseAsset": "GONE", "quoteAsset": "USDT",
     "contractType": "PERPETUAL", "status": "SETTLING",      # 非 TRADING，要被滤掉
     "pricePrecision": 2, "quantityPrecision": 3,
     "underlyingType": "COIN", "underlyingSubType": ["Meme", "Crypto"]},
]}


def test_exchange_info_carries_sector(monkeypatch):
    async def fake_get(path, retries=3):
        return SAMPLE

    monkeypatch.setattr(binance, "_get", fake_get)
    meta = asyncio.run(binance.exchange_info(force=True))

    assert set(meta) == {"ETHUSDT", "NVDAUSDT"}       # SETTLING 的被滤掉
    assert meta["ETHUSDT"]["sector"] == "Layer-1"
    assert meta["ETHUSDT"]["assetClass"] == "crypto"
    assert meta["NVDAUSDT"]["sector"] == "other"      # TradFi 只是桶
    assert meta["NVDAUSDT"]["assetClass"] == "us_equity"


def test_rich_rows_exposes_sector():
    """行情表要按板块筛选，sector 必须出现在 REST 快照里。

    刻意**不**进 WS 帧：板块是静态属性，每秒重复推它是白费带宽。
    """
    h = TickerHub()
    h.meta = {"ETHUSDT": {"symbol": "ETHUSDT", "base": "ETH",
                          "assetClass": "crypto", "sector": "Layer-1"}}
    h.snapshot = {"ETHUSDT": {"symbol": "ETHUSDT", "last": 4200.0, "chgPct": 1.0,
                              "quoteVolume": 1e9, "high": 4300.0, "low": 4100.0}}
    row = h.rich_rows()[0]
    assert row["sector"] == "Layer-1"

    sub = h.subscribe()
    h.set_viewport(sub, ["ETHUSDT"])
    from backend.app.routers.market import FRAME_FIELDS
    assert "sector" not in FRAME_FIELDS           # 不该混进每秒推送的帧
    assert len(h.compact_rows(["ETHUSDT"])[0]) == len(FRAME_FIELDS)


def test_meta_without_subtype_falls_back():
    """老合约或币安漏打标签时不能崩，落到 other。"""
    h = TickerHub()
    h.meta = {"OLDUSDT": {"symbol": "OLDUSDT", "base": "OLD"}}   # 没有 sector 键
    h.snapshot = {"OLDUSDT": {"symbol": "OLDUSDT", "last": 1.0, "chgPct": 0.0,
                              "quoteVolume": 0.0, "high": 1.0, "low": 1.0}}
    assert h.rich_rows()[0]["sector"] == "other"
