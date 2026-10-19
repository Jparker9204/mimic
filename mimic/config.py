"""All settings come from environment variables (see deploy/mimic.env.example)."""
from __future__ import annotations
import os
from decimal import ROUND_DOWN, Decimal, InvalidOperation


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


def _is_dec(x: str) -> bool:
    try:
        return Decimal(x).is_finite() and Decimal(x) > 0
    except (InvalidOperation, ValueError):
        return False


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
FREE_CAP = max(0, _int("MIMIC_FREE_CAP", 150))               # free credits never stack above this


# ---- SOL credit purchases (Solana Pay). The bot only WATCHES a public receiving address:
# it never holds a private key, never connects to a wallet and never sends funds.
LAMPORTS = 10 ** 9


def _sol_lamports(name: str, default: str) -> int:
    """Read a SOL amount as exact integer lamports (Decimal, no float artifacts)."""
    raw = (os.getenv(name) or "").strip() or default
    try:
        d = Decimal(raw)
        if not d.is_finite() or d <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        d = Decimal(default)
    return int((d * LAMPORTS).to_integral_value(rounding=ROUND_DOWN))


def _pay_address(raw: str) -> str:
    """The public receiving address, or "" when unset/invalid (base58 that decodes to 32 bytes)."""
    raw = (raw or "").strip()
    if not 32 <= len(raw) <= 44:
        return ""
    n = 0
    for ch in raw:
        i = _B58.find(ch)
        if i < 0:
            return ""
        n = n * 58 + i
    size = (n.bit_length() + 7) // 8 + (len(raw) - len(raw.lstrip("1")))
    return raw if size == 32 else ""


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
PAY_ADDRESS = _pay_address(os.getenv("MIMIC_PAY_ADDRESS") or "")
PAY_ADDRESS_INVALID = bool((os.getenv("MIMIC_PAY_ADDRESS") or "").strip()) and not PAY_ADDRESS
PRICE_LAMPORTS = max(1, _sol_lamports("MIMIC_PRICE_PER_CREDIT_SOL", "0.001"))     # per credit
MIN_PURCHASE_LAMPORTS = max(PRICE_LAMPORTS, _sol_lamports("MIMIC_MIN_PURCHASE_SOL", "0.02"))
MAX_PURCHASE_LAMPORTS = max(MIN_PURCHASE_LAMPORTS, _sol_lamports("MIMIC_MAX_PURCHASE_SOL", "5"))
# Quick-amount buttons on the buy screen (only those within min..max are shown)
BUY_QUICK_LAMPORTS = sorted({_sol_lamports("_", x) for x in
                             (os.getenv("MIMIC_BUY_AMOUNTS") or "0.02,0.05,0.1,0.25").replace(" ", "").split(",")
                             if _is_dec(x)})
ORDER_TTL_S = max(60, _int("MIMIC_ORDER_MINUTES", 15) * 60)        # payment request validity
LATE_PAYMENT_S = max(0, _int("MIMIC_LATE_PAYMENT_HOURS", 24) * 3600)   # still credit payments this long after creation
PAY_CHECK_S = max(3, _int("MIMIC_PAY_CHECK_S", 10))                # open orders checked this often
PAY_LATE_CHECK_S = max(10, _int("MIMIC_PAY_LATE_CHECK_S", 60))     # expired/cancelled unpaid orders
PAY_TOLERANCE_LAMPORTS = 1000                                      # 0.000001 SOL

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
