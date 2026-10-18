"""All settings come from environment variables (see deploy/mimic.env.example)."""
from __future__ import annotations
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


BOT_TOKEN = (os.getenv("MIMIC_BOT_TOKEN") or "").strip()
DB_PATH = os.getenv("MIMIC_DB") or "mimic.db"
# Optional private RPC (e.g. a free Helius/QuickNode key). Strongly recommended:
# the free public endpoints are slow and only some keep full history.
CUSTOM_RPC = (os.getenv("MIMIC_RPC_URL") or "").strip()
PUBLICNODE_RPC = "https://solana-rpc.publicnode.com"   # fast, but only ~1.5 days of tx history
MAINNET_RPC = "https://api.mainnet-beta.solana.com"     # full history, heavily rate limited
RECENT_RPC_HISTORY_HOURS = _float("MIMIC_RECENT_RPC_HOURS", 30)

ADMIN_IDS = {int(x) for x in (os.getenv("MIMIC_ADMIN_IDS") or "").replace(" ", "").split(",") if x.isdigit()}

# Free tier limits (per Telegram user, per UTC day)
FREE_LIMITS = {
    "coin": _int("MIMIC_FREE_COIN", 3),
    "wallet": _int("MIMIC_FREE_WALLET", 5),
    "sim": _int("MIMIC_FREE_SIM", 2),
}
COOLDOWN_S = _int("MIMIC_COOLDOWN_S", 25)
MAX_HEAVY_JOBS = _int("MIMIC_MAX_JOBS", 2)

# Scan scope
SCAN_DAYS = _int("MIMIC_SCAN_DAYS", 14)
MAX_SIGS = _int("MIMIC_MAX_SIGS", 3000)
MAX_TXS = _int("MIMIC_MAX_TXS", 800)
TX_BUDGET_S = _int("MIMIC_TX_BUDGET_S", 100)     # time budget for fetching wallet txs
MAX_TOKENS = _int("MIMIC_MAX_TOKENS", 40)        # newest tokens priced per scan
COIN_MAX_TXS = _int("MIMIC_COIN_MAX_TXS", 400)
CACHE_TTL_S = _int("MIMIC_CACHE_TTL_MIN", 45) * 60

# Sim cost assumptions
ENTRY_SLIPPAGE = 0.02
SWAP_FEE = 0.01
NETWORK_FEE_SOL = 0.0005
DEAD_LIQ_USD = 1000.0
