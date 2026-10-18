"""Coin Scan: who traded this coin, ranked by realized profit, with bundle/transfer wallets filtered out.

Flags (wallet is hidden from the ranking and listed under "Show flagged"):
- dev:        the wallet that created the coin
- launch:     bought in the creation block or in the very first trading block (snipers/bundles)
- cluster:    3+ wallets bought in the same block with near-identical SOL amounts
- transfer:   got a big share of its tokens by transfer instead of buying
- from dev:   received tokens directly from the dev wallet
- no buy:     spent < 0.01 SOL in total (tokens came from somewhere else)
Unflagged wallets that spent < 0.05 SOL are not ranked either (too small to matter).
"""
from __future__ import annotations
import time
from collections import defaultdict
from . import config
from .scan import Services
from .solana import STABLES, WSOL, parse_swap

MIN_REAL_SOL = 0.05     # rank only traders who put in at least this much
NO_BUY_SOL = 0.01       # below this, the wallet basically didn't buy
CLUSTER_MIN = 3         # wallets in one block with similar size
CLUSTER_SPREAD = 1.10   # max/min SOL ratio inside a cluster
TRANSFER_SHARE = 0.5    # transferred-in tokens >= 50% of bought tokens

REASONS = {"dev": "dev wallet", "launch": "bought in launch block", "cluster": "same-block bundle",
           "transfer": "got tokens by transfer", "from_dev": "got tokens from dev", "no_buy": "no real buy (under 0.01 SOL)"}


def extract_events(got: list[tuple[dict, dict]], mint: str) -> tuple[list[dict], list[dict]]:
    """From reduced txs, the swaps by the fee payer and token transfers of `mint`.
    A tx without a payer swap that moves the coin is treated as a transfer: owners whose balance
    went up are receivers, owners whose balance went down are senders."""
    swaps, transfers = [], []
    for s, red in got:
        t = red.get("t") or s.get("blockTime") or 0
        slot = s.get("slot") or 0
        who = red.get("payer")
        sw = parse_swap(red, who) if who else None
        if sw and sw[1] == mint:
            swaps.append({"t": t, "slot": slot, "who": who, "side": sw[0], "q": sw[2], "sol": sw[3], "sig": s["signature"]})
            continue
        if red.get("err"):
            continue
        deltas = {o: d.get(mint, 0.0) for o, d in (red.get("tok") or {}).items() if abs(d.get(mint, 0.0)) > 1e-9}
        senders = [o for o, v in deltas.items() if v < 0]
        for o, v in deltas.items():
            if v > 0:
                transfers.append({"t": t, "slot": slot, "to": o, "from": senders, "q": v})
    return swaps, transfers


def find_clusters(swaps: list[dict]) -> set[str]:
    by_slot: dict[int, list[tuple[float, str]]] = defaultdict(list)
    for e in swaps:
        if e["side"] == "BUY" and e["slot"]:
            by_slot[e["slot"]].append((e["sol"], e["who"]))
    flagged: set[str] = set()
    for buys in by_slot.values():
        if len({w for _, w in buys}) < CLUSTER_MIN:
            continue
        buys.sort()
        i = 0
        for j in range(len(buys)):
            while buys[j][0] > buys[i][0] * CLUSTER_SPREAD:
                i += 1
            group = {w for _, w in buys[i:j + 1]}
            if len(group) >= CLUSTER_MIN:
                flagged |= group
    return flagged


def aggregate_traders(swaps: list[dict], transfers: list[dict] | None = None, *, dev: str | None = None,
                      launch_slots: set[int] | None = None) -> tuple[list[dict], list[dict], int]:
    """Returns (ranked real traders, flagged wallets, number of small unflagged buyers).
    Realized profit only counts the tokens the wallet actually bought (average cost);
    proceeds from selling transferred-in tokens are ignored."""
    transfers = transfers or []
    launch_slots = launch_slots or set()
    tr: dict[str, dict] = {}
    order = 0

    def get(w):
        return tr.setdefault(w, {"wallet": w, "buys": 0, "sells": 0, "sol_in": 0.0, "sol_out": 0.0, "tok_in": 0.0,
                                 "tok_out": 0.0, "tok_xfer_in": 0.0, "first_t": None, "first_slot": None,
                                 "entry_rank": None, "flags": set()})
    for e in sorted(swaps, key=lambda x: (x["slot"], x["t"])):
        x = get(e["who"])
        if e["side"] == "BUY":
            if x["entry_rank"] is None:
                order += 1
                x["entry_rank"], x["first_t"], x["first_slot"] = order, e["t"], e["slot"]
            x["buys"] += 1
            x["sol_in"] += e["sol"]
            x["tok_in"] += e["q"]
            if e["slot"] in launch_slots:
                x["flags"].add("launch")
        else:
            x["sells"] += 1
            x["sol_out"] += e["sol"]
            x["tok_out"] += e["q"]
    for e in transfers:
        if e["to"] not in tr:      # receivers that never traded don't matter for the ranking
            continue
        x = tr[e["to"]]
        x["tok_xfer_in"] += e["q"]
        if dev and dev in e["from"] and e["to"] != dev:
            x["flags"].add("from_dev")
    for w in find_clusters(swaps):
        tr[w]["flags"].add("cluster")
    if dev and dev in tr:
        tr[dev]["flags"].add("dev")

    ranked, flagged, small = [], [], 0
    for x in tr.values():
        if x["sol_in"] < NO_BUY_SOL and (x["sells"] or x["tok_xfer_in"]):
            x["flags"].add("no_buy")
        if x["tok_xfer_in"] > 0 and x["tok_xfer_in"] >= TRANSFER_SHARE * max(x["tok_in"], 1e-12):
            x["flags"].add("transfer")
        bought_sold = min(x["tok_out"], x["tok_in"])
        sold_frac = bought_sold / x["tok_in"] if x["tok_in"] else 0.0
        proceeds = x["sol_out"] * (bought_sold / x["tok_out"]) if x["tok_out"] else 0.0
        x["realized_sol"] = round(proceeds - x["sol_in"] * sold_frac, 4)
        x["holding_pct"] = round(100 * (1 - sold_frac)) if x["tok_in"] else 0
        x["flags"] = sorted(x["flags"])
        x["reasons"] = [REASONS[f] for f in x["flags"]]
        if x["flags"]:
            flagged.append(x)
        elif x["sol_in"] >= MIN_REAL_SOL:
            ranked.append(x)
        elif x["tok_in"]:
            small += 1
    ranked.sort(key=lambda x: (x["sells"] == 0, -x["realized_sol"] if x["sells"] else -x["sol_in"]))
    flagged.sort(key=lambda x: -(x["sol_out"] + x["sol_in"]))
    return ranked, flagged, small


def pick_sigs(sigs_newest_first: list[dict], cap: int) -> tuple[list[dict], bool]:
    """Returns sigs OLDEST first (launch matters most). All if small; else earliest 60% + newest 40%."""
    if len(sigs_newest_first) <= cap:
        return list(reversed(sigs_newest_first)), False
    early = sigs_newest_first[-int(cap * 0.6):]
    late = sigs_newest_first[:cap - len(early)]
    return list(reversed(early)) + list(reversed(late)), True


async def coin_scan(svc: Services, mint: str, progress=None) -> dict:
    t0 = time.monotonic()
    if mint in STABLES or mint == WSOL:
        raise ValueError("That's SOL or a stablecoin — paste a memecoin's mint instead.")
    if progress:
        await progress("📜 Finding trades for this coin…")
    cap = config.COIN_MAX_SIGS
    all_sigs = await svc.rpc.signatures(mint, 0, cap)
    sigs = [s for s in all_sigs if not s.get("err")]
    if not sigs:
        raise ValueError("No trades found for this coin.")
    full_history = len(all_sigs) < cap
    chosen, sampled = pick_sigs(sigs, config.COIN_MAX_TXS)

    async def txprog(done, total):
        if progress:
            await progress(f"📜 Reading trades… {done}/{total}")
    got = await svc.rpc.many_tx(chosen, config.TX_BUDGET_S, progress=txprog)
    swaps, transfers = extract_events(got, mint)
    dev, launch_slots = None, set()
    if full_history:
        oldest = sigs[-1]
        creation = next((red for s, red in got if s["signature"] == oldest["signature"]), None)
        dev = creation.get("payer") if creation else None
        if oldest.get("slot"):
            launch_slots.add(oldest["slot"])
    buy_slots = [e["slot"] for e in swaps if e["side"] == "BUY" and e["slot"]]
    if buy_slots and (full_history or not sampled):
        launch_slots.add(min(buy_slots))
    ranked, flagged, small = aggregate_traders(swaps, transfers, dev=dev, launch_slots=launch_slots)
    dex = (await svc.market.dex_info([mint])).get(mint)
    return {"mint": mint, "sym": (dex or {}).get("sym") or "?", "sigs": len(sigs), "txs": len(got),
            "sampled": sampled or not full_history, "sig_cap": cap, "swaps": len(swaps), "traders": ranked[:10],
            "total_traders": len(ranked), "flagged": flagged[:25], "flagged_count": len(flagged), "small": small,
            "dev": dev, "launch_checked": bool(launch_slots), "seconds": round(time.monotonic() - t0, 1),
            "first_seen": min((s.get("blockTime") or 0) for s in sigs)}
