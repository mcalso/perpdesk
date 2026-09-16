"""把一条快讯关联到具体合约标的。

这一层决定整个资讯功能是有用还是噪音，两个方向的错都很贵：
漏了（你持仓的标的出事了却没提示）、和多了（满屏无关条目，很快就没人看了）。

三个必须处理的现实：

1. **中文财经新闻写"英伟达"不写 NVDA。** 币安的 exchangeInfo 里只有
   baseAsset（AAPL / TENCENT / HK0700 / XAU），没有任何本地化名称，
   所以中文别名只能自己维护 —— 见 ALIASES。

2. **很多 base 就是英文常用词。** 在架合约里有 THE / NOT / NOW / ALL /
   APP / NET / GAS / ONE / CAP / RED / SKY，还有 14 个单字母 base
   （A B C F G ...）。裸写匹配它们必然误报，所以这些只认带标记的写法。

3. **宏观消息不指向单一标的。** 加息、CPI、非农对整个组合都有影响，
   硬塞给某个 symbol 反而失真，所以单独走 categories。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 中文别名
#
# 只收"新闻里真会出现、且币安有对应合约"的名字。宁可少收，误报比漏报更伤
# —— 漏一条你还会去看原文，满屏噪音会让你直接不看这一页。
ALIASES: dict[str, tuple[str, ...]] = {
    # 加密
    "BTC": ("比特币", "大饼"), "ETH": ("以太坊", "以太"), "SOL": ("索拉纳",),
    "DOGE": ("狗狗币",), "XRP": ("瑞波", "瑞波币"), "BNB": ("币安币",),
    "ADA": ("艾达币",), "LTC": ("莱特币",), "BCH": ("比特现金",),
    "ZEC": ("大零币",), "USDC": ("USDC",), "PEPE": ("佩佩",),
    # 美股（代币化）
    "AAPL": ("苹果", "苹果公司"), "NVDA": ("英伟达",), "TSLA": ("特斯拉",),
    "MSFT": ("微软",), "AMZN": ("亚马逊",), "META": ("Meta", "脸书"),
    "AMD": ("超威", "AMD"), "INTC": ("英特尔",), "ARM": ("ARM", "安谋"),
    "TSM": ("台积电",), "ASML": ("阿斯麦",), "AVGO": ("博通",),
    "ORCL": ("甲骨文",), "CRM": ("赛富时",), "ADBE": ("Adobe",),
    "NFLX": ("奈飞", "网飞"), "UBER": ("优步",), "COIN": ("Coinbase",),
    "MSTR": ("微策略", "Strategy"), "HOOD": ("罗宾汉",), "PLTR": ("Palantir",),
    "BABA": ("阿里巴巴", "阿里"), "PDD": ("拼多多",), "MRNA": ("莫德纳",),
    "LLY": ("礼来",), "JPM": ("摩根大通",), "WMT": ("沃尔玛",),
    "COST": ("好市多",), "SONY": ("索尼",), "IBM": ("IBM",), "QCOM": ("高通",),
    "MU": ("美光",), "DELL": ("戴尔",), "CSCO": ("思科",), "SMCI": ("超微",),
    "MRVL": ("迈威尔",), "KLAC": ("科磊",), "AMAT": ("应用材料",),
    "LRCX": ("泛林",), "NXPC": ("恩智浦",), "GME": ("游戏驿站",),
    # 指数 / ETF
    "SPY": ("标普500", "标普", "标普指数"), "QQQ": ("纳斯达克100", "纳指"),
    "IWM": ("罗素2000",),
    # 港股 / 中概
    "TENCENT": ("腾讯",), "HK0700": ("腾讯",), "MEITUAN": ("美团",),
    "KUAISHOU": ("快手",), "BYD": ("比亚迪",), "POPMART": ("泡泡玛特",),
    "HK1810": ("小米",), "HK0992": ("联想",), "GIGADEV": ("兆易创新",),
    "ZHIPU": ("智谱",), "MINIMAX": ("MiniMax",),
    # A 股 / 韩股
    "UNITREE": ("宇树",), "CXMT": ("长鑫存储",),
    "SAMSUNG": ("三星",), "SKHYNIX": ("SK海力士", "海力士"),
    "HYUNDAI": ("现代汽车",), "NAVER": ("Naver",), "LGELECTRONICS": ("LG电子",),
    # 商品
    "XAU": ("黄金", "金价"), "XAG": ("白银", "银价"),
    "XPT": ("铂金",), "XPD": ("钯金",),
    "CL": ("原油", "WTI"), "BZ": ("布伦特", "布油"),
    "NATGAS": ("天然气",), "COPPER": ("铜价", "沪铜", "伦铜"),
}

# 这些 base 与英文常用词/常见缩写重名，裸写必然误报，只认带标记的写法
AMBIGUOUS = {
    "ALL", "ALT", "ACT", "APP", "APR", "ARM", "ARK", "ATH", "BAN", "BAT", "BOT",
    "CAP", "CAT", "COW", "DIA", "DIS", "EDU", "ERA", "ESP", "GAS", "GUN", "HOT",
    "JOE", "LAB", "LIT", "MET", "NET", "NEW", "NOM", "NOT", "NOW", "ONE", "RAM",
    "RED", "SKY", "SUN", "TAG", "THE", "WEN", "WET", "HOME", "OPEN", "PLAY",
    "SAFE", "STAR", "TEAM", "TAKE", "MOVE", "FORM", "EDGE", "BANK", "BILL",
    "CHIP", "DEEP", "FLEX", "GRAM", "LITE", "MEME", "RARE", "SIGN", "SOON",
    "TREE", "US", "IN", "ON", "ME", "RE", "AT", "LA", "ID", "IO", "BE", "AR",
    # 中文 base 里也有日常词：海鲜涨价的新闻不该命中龙虾USDT
    "龙虾", "牛来",
}

# 宏观事件不指向单一标的，单列成类别
CATEGORIES: dict[str, str] = {
    "macro": r"美联储|加息|降息|利率|CPI|PPI|非农|失业率|GDP|通胀|鲍威尔|"
             r"央行|欧央行|日银|财政部|国债收益率|经济数据|PMI",
    "equity": r"美股|纳指|道指|标普|财报|营收|每股收益|盘前|盘后|上市|IPO|"
              r"港股|恒指|A股|新股",
    # 注意别放"矿工|挖矿"：金矿坍塌、煤矿事故都会命中，实测已踩过
    "crypto": r"比特币|以太|加密|数字货币|虚拟货币|区块链|代币|链上|稳定币|"
              r"DeFi|NFT|Web3|USDT|USDC|meme|币圈|比特币矿工",
    "commodity": r"黄金|白银|原油|布伦特|天然气|铜价|贵金属|OPEC|减产",
    "geo": r"关税|制裁|战争|冲突|停火|地缘|出口管制|禁运",
}
_CAT_RE = {k: re.compile(v) for k, v in CATEGORIES.items()}

_MARKED = re.compile(r"[$＄]([A-Za-z0-9]{1,14})|[（(]([A-Za-z0-9]{2,14})[)）]")
_BARE = re.compile(r"[A-Z0-9]{3,14}")


def categories(text: str) -> list[str]:
    """这条快讯涉及哪些板块。可以为空（纯资讯、无关行情）。"""
    return sorted(k for k, r in _CAT_RE.items() if r.search(text))


def match_symbols(text: str, symbols: dict[str, str]) -> list[str]:
    """从文本里找出涉及的合约。

    symbols: {baseAsset: symbol}，由调用方从 exchangeInfo 传进来 ——
    这层不自己去拿行情，好单独测试。
    """
    hits: set[str] = set()
    upper = text.upper()

    # 1) 完整合约名（BTCUSDT）：最可靠，无歧义
    for base, sym in symbols.items():
        if sym.upper() in upper:
            hits.add(sym)

    # 2) 带标记的写法：$BTC、（BTC）、(NVDA) —— 中文财经新闻里很常见
    for m in _MARKED.finditer(text):
        tok = (m.group(1) or m.group(2) or "").upper()
        if tok in symbols:
            hits.add(symbols[tok])

    # 3) 中文别名：财经新闻写"英伟达"不写 NVDA，漏了这层等于对股票类全瞎
    for base, names in ALIASES.items():
        if base in symbols and any(n in text for n in names):
            hits.add(symbols[base])

    # 4) 裸写的 ticker：**在原文上找，只认本来就大写的**。
    #    先 upper() 再找的话，每个英文单词都成了潜在 ticker ——
    #    实测 "rents rose" 命中 ROSEUSDT、"Trump's envoy" 命中 TRUMPUSDC、
    #    "people" 命中 PEOPLEUSDT，全是噪音。
    #    加密/股票的代号在正文里一律大写书写，这条约束几乎不漏真阳性。
    for tok in _BARE.findall(text):
        if tok in symbols and tok not in AMBIGUOUS:
            hits.add(symbols[tok])

    return sorted(hits)
