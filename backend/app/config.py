"""全局配置。所有路径与外部端点集中在此。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("PERPDESK_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "perpdesk.db"
ENV_PATH = BASE_DIR / "backend" / ".env"

# Binance U 本位合约。直连比走代理快，客户端一律 trust_env=False。
FAPI_BASE = "https://fapi.binance.com"

# ⚠️ 用 stream.binancefuture.com 而不是 fstream.binance.com。
# 两者都是官方合约 WS 端点，但 fstream 的全市场数组流（!ticker@arr /
# !miniTicker@arr / !markPrice@arr）以及 aggTrade 实测**连得上却一帧不推**
# （SUBSCRIBE 甚至会返回成功应答），国内机器与境外机器都能复现，不是网络问题。
# 详见 docs/DATA_SOURCES.md。
FSTREAM_BASE = "wss://stream.binancefuture.com"

# 全市场流：!ticker@arr 给 24h 行情，!markPrice@arr@1s 给标记价 + 资金费率。
# 一条连接喂所有前端，且完全不消耗 REST 权重。
MARKET_STREAMS = "!ticker@arr/!markPrice@arr@1s"

HOST = os.getenv("PERPDESK_HOST", "127.0.0.1")
PORT = int(os.getenv("PERPDESK_PORT", "18090"))

# 全市场行情以 WS 为主。REST 只在两种情况下用：
#   1) 启动首屏（WS 的数组流是增量推送，要几十秒才覆盖全市场）；
#   2) WS 断流超过 WS_STALE_SEC 时兜底 —— fstream 那次故障说明端点会坏，
#      留一条退路，但正常情况下一次也不会调用。
WS_STALE_SEC = 45.0
REST_POLL_INTERVAL = 30.0
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
