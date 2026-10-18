"""Run Mimic's logic without Telegram: python scripts/dry_run.py <wallet> [--coin MINT] [--db path]

Prints the same text the bot would send plus timings. Uses its own DB (default dry_run.db)."""
from __future__ import annotations
import argparse, asyncio, json, logging, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mimic import texts                                   # noqa: E402
from mimic.coinscan import coin_scan                      # noqa: E402
from mimic.db import DB                                   # noqa: E402
from mimic.market import Market                           # noqa: E402
from mimic.net import Net                                 # noqa: E402
from mimic.scan import Services, wallet_scan              # noqa: E402
from mimic.sim import SimParams, build_trades, simulate   # noqa: E402
from mimic.solana import Rpc                              # noqa: E402


def plain(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wallet")
    ap.add_argument("--coin")
    ap.add_argument("--db", default="dry_run.db")
    a = ap.parse_args()
    db = DB(a.db)
    net = Net()
    svc = Services(Rpc(net, db), Market(net, db))
    t = {}

    async def prog(msg):
        print("  …", msg, flush=True)

    t0 = time.monotonic()
    scan = await wallet_scan(svc, a.wallet, prog)
    t["wallet_scan_s"] = round(time.monotonic() - t0, 1)
    print("\n" + plain(texts.wallet_scan_text(scan)) + "\n")

    t0 = time.monotonic()
    trades, sol = await build_trades(svc.market, scan, prog)
    t["sim_load_s"] = round(time.monotonic() - t0, 1)
    for p in (SimParams(0.2, 0, 1.5, "mirror"), SimParams(0.2, 15, 2.0, "mirror"), SimParams(0.2, 15, 1.5, "sl", 0.3)):
        t1 = time.monotonic()
        res = simulate(trades, p, sol.at, now=scan["scanned_at"])
        print(plain(texts.sim_text(a.wallet, p.label(), res.summary)), f"\n  (sim math {time.monotonic() - t1:.2f}s)\n")

    # the bot caches finished scans; a repeat scan (same wallet within 45 min) is a cache read
    db.cache_set("scan:" + a.wallet, scan, 2700)
    t0 = time.monotonic()
    assert db.cache_get("scan:" + a.wallet) is not None
    t["wallet_rescan_cached_s"] = round(time.monotonic() - t0, 3)
    # a fresh scan later re-uses cached transactions and candles
    t0 = time.monotonic()
    await wallet_scan(svc, a.wallet, None)
    t["wallet_rescan_after_cache_expiry_s"] = round(time.monotonic() - t0, 1)

    if a.coin:
        t0 = time.monotonic()
        cs = await coin_scan(svc, a.coin, prog)
        t["coin_scan_s"] = round(time.monotonic() - t0, 1)
        print("\n" + plain(texts.coin_scan_text(cs)) + "\n")
    print("TIMINGS", json.dumps(t))
    await net.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
