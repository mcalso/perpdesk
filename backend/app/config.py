"""全局配置。所有路径与外部端点集中在此。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("PERPDESK_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "perpdesk.db"
ENV_PATH = BASE_DIR / "backend" / ".env"

# Binance U 本位合约。直连比走代理快，客户端一律 trust_env=False。
FAPI_BASE = "https://fapi.binance.com"

# ⚠️ 全市场 WS 数组流两个官方端点都不可用，行情一律以 REST 为权威源：
#   - fstream.binance.com：!ticker@arr / !markPrice@arr / aggTrade 连得上却一帧不推
#     （SUBSCRIBE 还会返回成功应答），国内与境外机器都能复现。
#   - stream.binancefuture.com：推得动，但数据不可信 —— 实测 KORUUSDT 标记价
#     报 498.27（真实 22.91，偏差 2079%，且指数价/结算价全等于同一个值、
#     资金费率为 0），另有多个活跃标的长时间完全不推送（含真实持仓）。
# 单标的 bookTicker 流在两个端点上都稳定可靠，仅用它做自选与持仓的实时价。
# 详见 docs/DATA_SOURCES.md。
FSTREAM_BASE = "wss://fstream.binance.com"

HOST = os.getenv("PERPDESK_HOST", "127.0.0.1")
PORT = int(os.getenv("PERPDESK_PORT", "18090"))

# REST 轮询是全市场行情的权威源。权重预算（独占机器上限 2400/分钟）：
#   premiumIndex 权重 10，5s 一次 = 120/分钟   —— 标记价与资金费率，持仓估值靠它
#   ticker/24hr  权重 40，20s 一次 = 120/分钟  —— 涨跌幅与成交额，变化慢，无需更快
# 合计约 240/分钟，占上限一成。失败按 1.6 倍退避，并始终保留上一份快照。
PREMIUM_POLL_INTERVAL = 5.0
TICKER_POLL_INTERVAL = 20.0
REST_POLL_MAX_INTERVAL = 180.0

# exchangeInfo 刷新间隔（秒），让新上架合约无需重启即可出现
META_REFRESH_INTERVAL = 600.0
EXCHANGE_INFO_TTL = 600.0

# 账户快照轮询间隔（秒）。后端集中拉一次喂所有前端连接，
# 权重恒定（balance 5 + positionRisk 5 = 10/次），不随标签页数量增长。
#
# 这个间隔只决定「新开的仓位多久出现」——浮动盈亏、名义价值、占比都由
# snapshot() 用行情中心的实时标记价本地重算，是 1 秒级的，与此无关。
# 6s 约合 100 权重/分钟，占 2400 限额的 4%。
ACCOUNT_POLL_INTERVAL = 6.0

# 自选标的图标缓存目录
ICON_DIR = DATA_DIR / "icons"

# 图标后台预热：服务起稳后慢速把全市场图标抓齐，之后翻页都是磁盘命中。
# 设 0 可关闭。批间隔刻意留白，别和用户的正常请求抢外部带宽。
ICON_PREWARM_DELAY = 30.0
ICON_PREWARM_BATCH = 6
ICON_PREWARM_PAUSE = 0.6

# 自选标的实时盘口订阅上限（Binance 单连接上限 1024 个流）
WS_MAX_STREAMS = 100

# 前端广播间隔（秒）
BROADCAST_INTERVAL = 1.0

DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]


def load_env() -> dict[str, str]:
    """读取 backend/.env 里的凭据。文件不存在也不报错。"""
    out: dict[str, str] = {}
    if ENV_PATH.is_file():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out
