"""快讯关联标的的测试。

这一层两个方向的错都很贵：漏了（持仓标的出事没提示）、多了（满屏噪音，
很快就没人看这一页了）。下面的用例基本都来自在真实金十数据上实测踩到的坑。
"""
import pytest

from backend.app.news import relevance as rel

# 取几个有代表性的：加密、美股、港股、商品、以及与英文常用词重名的
SYMS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "NVDA": "NVDAUSDT", "TSLA": "TSLAUSDT",
    "TENCENT": "TENCENTUSDT", "XAU": "XAUUSDT", "NATGAS": "NATGASUSDT",
    "ROSE": "ROSEUSDT", "TRUMP": "TRUMPUSDC", "PEOPLE": "PEOPLEUSDT",
    "THE": "THEUSDT", "ALL": "ALLUSDT", "US": "USUSDT", "龙虾": "龙虾USDT",
    "SKHYNIX": "SKHYNIXUSDT",
}


def m(text):
    return rel.match_symbols(text, SYMS)


# ---------------------------------------------------------------- 应当命中

def test_full_contract_name():
    assert m("BTCUSDT 永续合约资金费率转正") == ["BTCUSDT"]


def test_dollar_prefixed_ticker():
    assert m("市场关注 $ETH 的表现") == ["ETHUSDT"]


def test_parenthesised_ticker():
    """中文财经新闻的标准写法：英伟达（NVDA）。"""
    assert m("英伟达（NVDA）盘后大涨") == ["NVDAUSDT"]


def test_bare_uppercase_ticker():
    assert m("BTC 突破 12 万美元") == ["BTCUSDT"]


@pytest.mark.parametrize("text,expect", [
    ("英伟达发布新一代芯片", "NVDAUSDT"),
    ("特斯拉三季度交付量超预期", "TSLAUSDT"),
    ("腾讯回购股份", "TENCENTUSDT"),
    ("金价站上历史新高，黄金需求旺盛", "XAUUSDT"),
    ("德国加大天然气储备", "NATGASUSDT"),
    ("SK海力士上调资本开支", "SKHYNIXUSDT"),
    ("比特币日内涨超 3%", "BTCUSDT"),
])
def test_chinese_aliases(text, expect):
    """中文财经新闻写"英伟达"不写 NVDA。

    漏掉这层的话，股票与商品类标的基本全瞎 —— 而这类合约占币安在架的四分之一。
    """
    assert expect in m(text)


# ---------------------------------------------------------------- 不该命中

@pytest.mark.parametrize("text", [
    "UK private rents rose 3.8% YoY in the 12 months to August",
    "Belarus state news agency cited Trump's envoy Cole saying",
    "Two local sources said people were evacuated from the area",
])
def test_lowercase_english_words_are_not_tickers(text):
    """曾经的真 bug：先 text.upper() 再找大写词，等于把每个英文单词都当成 ticker。

    实测 "rents rose" 命中 ROSEUSDT、"Trump's envoy" 命中 TRUMPUSDC、
    "people" 命中 PEOPLEUSDT —— 三条全是噪音。加密与股票代号在正文里
    一律大写书写，所以裸写匹配必须在原文上做。
    """
    assert m(text) == []


@pytest.mark.parametrize("text", [
    "THE company said ALL of US will benefit",
    "IN ON ME RE AT",
])
def test_common_words_that_are_also_tickers_need_a_marker(text):
    """在架合约里真有 THE / ALL / US / IN / ON 这些 base，裸写必然误报。"""
    assert m(text) == []


def test_ambiguous_ticker_still_matches_when_marked():
    """加了标记就认 —— 屏蔽的是裸写，不是这个标的。"""
    assert m("$THE 上线合约") == ["THEUSDT"]
    assert m("THEUSDT 成交放量") == ["THEUSDT"]


def test_chinese_everyday_word_base():
    """龙虾USDT 是真实合约，但"龙虾"也是日常词：海鲜新闻不该命中它。"""
    assert m("受台风影响，龙虾价格上涨三成") == []
    assert m("龙虾USDT 合约上线") == ["龙虾USDT"]


# ---------------------------------------------------------------- 板块

@pytest.mark.parametrize("text,cat", [
    ("美联储宣布降息 25 个基点", "macro"),
    ("美股三大指数集体收涨，纳指涨 1.2%", "equity"),
    ("比特币现货 ETF 单日净流入创新高", "crypto"),
    ("OPEC 宣布减产，原油大涨", "commodity"),
    ("美国宣布对部分商品加征关税", "geo"),
])
def test_categories(text, cat):
    assert cat in rel.categories(text)


def test_mining_accident_is_not_crypto():
    """"矿工""挖矿"单独出现不是加密语境 —— 金矿坍塌、煤矿事故都会命中。

    这条是实测踩到的：苏丹金矿坍塌的快讯被判成了 crypto。
    """
    text = "苏丹一金矿发生坍塌事故，造成至少 60 名矿工遇难"
    assert "crypto" not in rel.categories(text)


def test_gold_industry_news_maps_to_gold_contract():
    """同一条金矿新闻里提到"黄金生产和出口"，关联 XAU 是合理的。"""
    text = "苏丹矿产资源丰富，黄金生产和出口在非洲国家中名列前茅"
    assert m(text) == ["XAUUSDT"]
    assert "commodity" in rel.categories(text)


def test_unrelated_news_matches_nothing():
    text = "中共中央政治局委员、外交部长王毅在北京同伊朗外长阿拉格齐举行会谈"
    assert m(text) == []
    assert rel.categories(text) == []


def test_symbol_not_in_universe_is_ignored():
    """别名表里有但这个交易所没有的标的，不该凭空冒出来。"""
    assert rel.match_symbols("苹果公司发布新品", {"BTC": "BTCUSDT"}) == []
