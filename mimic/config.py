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

# Credits (all optional env overrides; restart the service to apply)
# Cost of each action in credits. Cached free extras (best/worst trades, sim trades) cost nothing.
COSTS = {
    "coin": max(0, _int("MIMIC_COST_COIN", 10)),
    "wallet": max(0, _int("MIMIC_COST_WALLET", 3)),
    "sim": max(0, _int("MIMIC_COST_SIM", 15)),
}
FREE_REFILL = max(0, _int("MIMIC_FREE_REFILL", 40))          # free credits added per interval (also the start balance)
REFILL_HOURS = max(0.01, _float("MIMIC_REFILL_HOURS", 6))    # refill interval
FREE_CAP = max(0, _int("MIMIC_FREE_CAP", 500))               # free credits never stack above this


def _packs(raw: str) -> list[tuple[int, float]]:
    """Parse "100:0.05,250:0.1" into [(100, 0.05), (250, 0.1)]. Display only: payments aren't live."""
    out = []
    for part in raw.replace(" ", "").split(","):
        try:
            c, p = part.split(":")
            if int(c) > 0 and float(p) > 0:
                out.append((int(c), float(p)))
        except ValueError:
            continue
    return out


PACKS = _packs(os.getenv("MIMIC_PACKS") or "") or [(100, 0.05), (250, 0.1)]
COOLDOWN_S = _int("MIMIC_COOLDOWN_S", 25)
MAX_HEAVY_JOBS = _int("MIMIC_MAX_JOBS", 2)

# Scan scope
SCAN_DAYS = _int("MIMIC_SCAN_DAYS", 14)
MAX_SIGS = _int("MIMIC_MAX_SIGS", 3000)
MAX_TXS = _int("MIMIC_MAX_TXS", 800)
TX_BUDGET_S = _int("MIMIC_TX_BUDGET_S", 100)     # time budget for fetching wallet txs
MAX_TOKENS = _int("MIMIC_MAX_TOKENS", 40)        # newest tokens priced per scan
COIN_MAX_TXS = _int("MIMIC_COIN_MAX_TXS", 400)
COIN_MAX_SIGS = _int("MIMIC_COIN_MAX_SIGS", 50000)   # page back this far to find the launch
CACHE_TTL_S = _int("MIMIC_CACHE_TTL_MIN", 45) * 60

# Sim cost assumptions
ENTRY_SLIPPAGE = 0.02
SWAP_FEE = 0.01
NETWORK_FEE_SOL = 0.0005
DEAD_LIQ_USD = 1000.0
