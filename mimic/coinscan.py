"""Coin Scan (v1): who traded this coin early, ranked by realized profit on it."""
from __future__ import annotations
import time
from . import config
from .scan import Services
from .solana import STABLES, WSOL, parse_swap


def aggregate_traders(swaps: list[tuple[float, str, tuple]], mint: str) -> list[dict]:
    """swaps: [(time, trader, (side, mint, qty, sol))]. Realized profit = sell proceeds minus the
    cost of the tokens that were sold (average cost). Traders with no buy seen are left out."""
    tr: dict[str, dict] = {}
    order = 0
    for t, who, (side, m, q, s) in sorted(swaps, key=lambda x: x[0]):
        if m != mint:
            continue
        x = tr.setdefault(who, {"wallet": who, "buys": 0, "sells": 0, "sol_in": 0.0, "sol_out": 0.0,
                                "tok_in": 0.0, "tok_out": 0.0, "first_t": None, "entry_rank": None})
        if side == "BUY":
            if x["entry_rank"] is None:
                order += 1
                x["entry_rank"] = order
                x["first_t"] = t
            x["buys"] += 1
            x["sol_in"] += s
            x["tok_in"] += q
        else:
            x["sells"] += 1
            x["sol_out"] += s
            x["tok_out"] += q
    out = []
    for x in tr.values():
        if not x["tok_in"]:
            continue
        sold_frac = min(1.0, x["tok_out"] / x["tok_in"])
        x["realized_sol"] = round(x["sol_out"] - x["sol_in"] * sold_frac, 4)
        x["holding_pct"] = round(100 * (1 - sold_frac))
        out.append(x)
    # traders who sold something first (best realized profit on top), then the biggest early holders
    out.sort(key=lambda x: (x["sells"] == 0, -x["realized_sol"] if x["sells"] else -x["sol_in"]))
    return out


def pick_sigs(sigs_newest_first: list[dict], cap: int) -> tuple[list[dict], bool]:
    """All if small; else the earliest 60% + newest 40% of the cap."""
    if len(sigs_newest_first) <= cap:
        return sigs_newest_first, False
    early = sigs_newest_first[-int(cap * 0.6):]
    late = sigs_newest_first[:cap - len(early)]
    return late + early, True


async def coin_scan(svc: Services, mint: str, progress=None) -> dict:
    t0 = time.monotonic()
    if mint in STABLES or mint == WSOL:
        raise ValueError("That's SOL or a stablecoin — paste a memecoin's mint instead.")
    if progress:
        await progress("📜 Finding trades for this coin…")
    sigs = await svc.rpc.signatures(mint, 0, 5000)
    sigs = [s for s in sigs if not s.get("err")]
    if not sigs:
        raise ValueError("No trades found for this coin.")
    chosen, sampled = pick_sigs(sigs, config.COIN_MAX_TXS)

    async def txprog(done, total):
        if progress:
            await progress(f"📜 Reading trades… {done}/{total}")
    got = await svc.rpc.many_tx(chosen, config.TX_BUDGET_S, progress=txprog)
    swaps = []
    for s, red in got:
        who = red.get("payer")
        sw = parse_swap(red, who) if who else None
        if sw and sw[1] == mint:
            swaps.append((red.get("t") or s.get("blockTime") or 0, who, sw))
    traders = aggregate_traders(swaps, mint)
    dex = (await svc.market.dex_info([mint])).get(mint)
    return {"mint": mint, "sym": (dex or {}).get("sym") or "?", "sigs": len(sigs), "txs": len(got),
            "sampled": sampled or len(sigs) >= 5000, "swaps": len(swaps), "traders": traders[:10],
            "total_traders": len(traders), "seconds": round(time.monotonic() - t0, 1),
            "first_seen": min((s.get("blockTime") or 0) for s in sigs)}
