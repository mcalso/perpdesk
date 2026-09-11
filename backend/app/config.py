"""全局配置。所有路径与外部端点集中在此。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("TRADVIEW_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "tradview.db"
ENV_PATH = BASE_DIR / "backend" / ".env"

# Binance U 本位合约。直连比走代理快，客户端一律 trust_env=False。
FAPI_BASE = "https://fapi.binance.com"
FSTREAM_BASE = "wss://fstream.binance.com"

HOST = os.getenv("TRADVIEW_HOST", "127.0.0.1")
PORT = int(os.getenv("TRADVIEW_PORT", "18090"))

# 全市场 24h 行情靠 REST 轮询（本机到 Binance 的全市场 WS 流不可用）。
# 间隔取 30s：ticker/24hr 全市场权重约 80，30s 一次约 160 权重/分钟，
# 给同机其他采集进程留出余量，仍会间歇 418，失败按 1.6 倍退避。
REST_POLL_INTERVAL = 30.0
REST_POLL_MAX_INTERVAL = 180.0

# exchangeInfo 刷新间隔（秒），让新上架合约无需重启即可出现
META_REFRESH_INTERVAL = 600.0
EXCHANGE_INFO_TTL = 600.0

# 账户快照轮询间隔（秒）。后端集中拉一次喂所有前端连接，
# 权重恒定（balance 5 + positionRisk 5 = 10/次），不随打开的标签页数量增长。
ACCOUNT_POLL_INTERVAL = 12.0

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
