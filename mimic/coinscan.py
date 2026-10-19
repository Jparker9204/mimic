"""Coin Scan v2: per-wallet PnL on one coin.

1. Candidates: top ~20 holders (getTokenLargestAccounts, real wallets only) + recent big/active traders
   from GeckoTerminal pool trades. Capped at ~40.
2. For each candidate, read the history of THEIR token account for this coin (small, complete even on
   busy coins), value every buy/sell in SOL (SOL, WSOL, stables and the pair's quote token), and add the
   current holding at today's price.
3. Flag bundle/transfer/launch-block wallets; rank real winners with the user's filter (see filters.py).
If the free data sources rate-limit us, raise DataBusy instead of returning a misleading "no profit".
"""
from __future__ import annotations
import asyncio
import logging, time
from collections import defaultdict
from . import config
from .market import SolPrice
from .scan import Services
from .solana import (STABLES, SYSTEM_PROGRAM, TOKEN_PROGRAM, WSOL, DataBusy, ata_address)

log = logging.getLogger(__name__)

MIN_REAL_SOL = 0.05     # only traders who put in at least this much are kept as candidates
NO_BUY_SOL = 0.01       # below this, the wallet basically didn't buy
CLUSTER_MIN = 3
CLUSTER_SPREAD = 1.10
TRANSFER_SHARE = 0.5
LAUNCH_WINDOW_S = 2     # first buy within 2s of the pool's creation = launch block
MAX_CANDIDATES = 40
MAX_HOLDERS = 20
PER_WALLET_MAX_TX = 40  # wallets with more trades on this coin are bots; skipped (history too long)
BUDGET_S = 75           # time budget for reading wallet histories

REASONS = {"launch": "bought in launch block", "cluster": "same-block bundle", "transfer": "got tokens by transfer",
           "no_buy": "no real buy (under 0.01 SOL)"}


# ------------------------------------------------------------------ pure helpers (tested)
def classify(red: dict, wallet: str, mint: str, value_sol: dict[str, float]) -> tuple[str, float, float] | None:
    """What did `wallet` do with `mint` in this tx? -> (kind, tokens, sol_value)
    kind: BUY / SELL / XFER_IN / XFER_OUT / UNKNOWN (paid with a token we can't price)."""
    if not red or red.get("err"):
        return None
    d = dict(red["tok"].get(wallet) or {})
    q = d.pop(mint, 0.0)
    if abs(q) < 1e-12:
        return None
    sol = red["sol"].get(wallet, 0.0) + d.pop(WSOL, 0.0)
    for tok, delta in d.items():
        if abs(delta) < 1e-9:
            continue
        v = value_sol.get(tok)
        if v is None:
            return ("UNKNOWN", abs(q), 0.0)
        sol += delta * v
    if q > 0:
        return ("BUY", q, -sol) if sol < -0.001 else ("XFER_IN", q, 0.0)
    return ("SELL", -q, sol) if sol > 0.0005 else ("XFER_OUT", -q, 0.0)


def wallet_pnl(wallet: str, events: list[tuple[float, int, tuple]], balance: float) -> dict:
    """events: [(time, slot, (kind, tokens, sol))]. Average-cost PnL on tokens the wallet actually bought."""
    x = {"wallet": wallet, "buys": 0, "sells": 0, "sol_in": 0.0, "sol_out": 0.0, "tok_in": 0.0, "tok_out": 0.0,
         "xfer_in": 0.0, "xfer_out": 0.0, "unknown": 0, "first_t": None, "first_slot": None, "first_sol": 0.0}
    for t, slot, (kind, q, s) in sorted(events, key=lambda e: (e[1], e[0])):
        if kind == "BUY":
            if x["first_t"] is None:
                x["first_t"], x["first_slot"], x["first_sol"] = t, slot, s
            x["buys"] += 1
            x["sol_in"] += s
            x["tok_in"] += q
        elif kind == "SELL":
            x["sells"] += 1
            x["sol_out"] += s
            x["tok_out"] += q
        elif kind == "XFER_IN":
            x["xfer_in"] += q
        elif kind == "XFER_OUT":
            x["xfer_out"] += q
        else:
            x["unknown"] += 1
    bought_sold = min(x["tok_out"], x["tok_in"])
    sold_frac = bought_sold / x["tok_in"] if x["tok_in"] else 0.0
    proceeds = x["sol_out"] * (bought_sold / x["tok_out"]) if x["tok_out"] else 0.0
    x["realized_sol"] = round(proceeds - x["sol_in"] * sold_frac, 4)
    unsold = max(0.0, x["tok_in"] - bought_sold)
    held = min(max(balance, 0.0), unsold)
    x["held_tokens"] = held
    x["held_cost_sol"] = x["sol_in"] * (held / x["tok_in"]) if x["tok_in"] else 0.0
    x["holding_pct"] = round(100 * held / x["tok_in"]) if x["tok_in"] else 0
    x["balance_ok"] = True          # balance comes straight from the chain
    return x


def find_clusters(first_buys: list[tuple[int, float, str]]) -> set[str]:
    """first_buys: [(slot, sol, wallet)]. 3+ wallets whose first buy is in one block with similar size."""
    by_slot: dict[int, list[tuple[float, str]]] = defaultdict(list)
    for slot, sol, w in first_buys:
        if slot:
            by_slot[slot].append((sol, w))
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


def flag_wallets(rows: list[dict], launch_ts: float | None) -> None:
    clusters = find_clusters([(r["first_slot"], r["first_sol"], r["wallet"]) for r in rows if r["first_slot"]])
    for r in rows:
        f = set()
        if r["sol_in"] < NO_BUY_SOL and (r["sells"] or r["xfer_in"]):
            f.add("no_buy")
        if r["xfer_in"] > 0 and r["xfer_in"] >= TRANSFER_SHARE * max(r["tok_in"], 1e-12):
            f.add("transfer")
        if launch_ts and r["first_t"] and r["first_t"] - launch_ts <= LAUNCH_WINDOW_S:
            f.add("launch")
        if r["wallet"] in clusters:
            f.add("cluster")
        r["flags"] = sorted(f)
        r["reasons"] = [REASONS[k] for k in r["flags"]]


def merge_candidates(holders: list[tuple[str, str]], trades: list[dict], cap: int = MAX_CANDIDATES,
                     exclude: set[str] | None = None) -> list[tuple[str, str | None, str]]:
    """-> [(wallet, token_account or None, source)] holders first, then traders by USD volume."""
    exclude = exclude or set()
    out: dict[str, tuple[str, str | None, str]] = {}
    for w, acc in holders[:MAX_HOLDERS]:
        if w not in exclude:
            out.setdefault(w, (w, acc, "holder"))
    vol: dict[str, float] = defaultdict(float)
    for t in trades:
        vol[t["wallet"]] += t["usd"]
    for w in sorted(vol, key=lambda k: -vol[k]):
        if len(out) >= cap:
            break
        if w not in exclude:
            out.setdefault(w, (w, None, "trader"))
    return list(out.values())[:cap]


def quote_values(pairs: list[dict], sol_usd: float) -> dict[str, float]:
    """SOL value of one unit of each token we can price: stables and the pairs' quote tokens."""
    v = {s: 1.0 / sol_usd for s in STABLES}
    for p in pairs:
        q = (p.get("quoteToken") or {}).get("address")
        try:
            pu, pn = float(p.get("priceUsd") or 0), float(p.get("priceNative") or 0)
        except (TypeError, ValueError):
            continue
        if q and q != WSOL and q not in v and pu > 0 and pn > 0:
            v[q] = (pu / pn) / sol_usd
    return v


# ------------------------------------------------------------------ the scan
async def coin_scan(svc: Services, mint: str, progress=None) -> dict:
    t0 = time.monotonic()
    if mint in STABLES or mint == WSOL:
        raise ValueError("That's SOL or a stablecoin — paste a memecoin's mint instead.")
    rpc, market = svc.rpc, svc.market
    if progress:
        await progress("🔎 Finding top holders and big traders…")
    pairs = await market.pairs(mint)
    if not pairs:
        raise ValueError("No trading pair found for this coin on DexScreener.")
    sol_usd = SolPrice(await market.sol_usd_series()).at(time.time())
    best = pairs[0]
    price_usd = float(best.get("priceUsd") or 0)
    price_sol = price_usd / sol_usd if price_usd else None
    launch_ms = min((p.get("pairCreatedAt") or 0) for p in pairs if p.get("pairCreatedAt")) if any(
        p.get("pairCreatedAt") for p in pairs) else None
    launch_ts = launch_ms / 1000 if launch_ms else None
    values = quote_values(pairs, sol_usd)
    pool_addrs = {p.get("pairAddress") for p in pairs}

    token_program = ((await rpc.account_info(mint)) or {}).get("owner") or TOKEN_PROGRAM

    # candidates: holders (real wallets only) + traders, fetched at the same time
    async def get_holders():
        accs = await rpc.largest_holders(mint)
        parsed = await rpc.accounts(accs)
        owners = [((((a or {}).get("data") or {}).get("parsed") or {}).get("info") or {}).get("owner") for a in parsed]
        pairs_owned = [(o, acc) for o, acc in zip(owners, accs) if o]
        owner_info = await rpc.accounts([o for o, _ in pairs_owned], parsed=False)
        return [(o, acc) for (o, acc), info in zip(pairs_owned, owner_info)
                if info and info.get("owner") == SYSTEM_PROGRAM and o not in pool_addrs]

    async def get_trades():
        out: list[dict] = []
        for p in pairs[:2]:
            if p.get("pairAddress"):
                out += await market.pool_trades(p["pairAddress"], 1000)
                out += await market.pool_trades(p["pairAddress"], 0)
        return out
    h_res, trades = await asyncio.gather(get_holders(), get_trades(), return_exceptions=True)
    holder_err = isinstance(h_res, BaseException)
    if holder_err and not isinstance(h_res, DataBusy):
        log.warning("holders lookup failed: %r", h_res)
    holders = [] if holder_err else h_res
    if isinstance(trades, BaseException):
        log.warning("pool trades failed: %r", trades)
        trades = []
    cands = merge_candidates(holders, trades, exclude=pool_addrs)
    if not cands:
        if holder_err:
            raise DataBusy("no candidates: holders call failed and no recent trades")
        raise ValueError("No recent traders or holders found for this coin.")

    # each candidate's own token-account history
    if progress:
        await progress(f"📜 Reading {len(cands)} wallets' history on this coin…")
    sem = asyncio.Semaphore(8 if config.CUSTOM_RPC else 3)
    sig_lists: dict[str, list[dict] | None] = {}
    accts: dict[str, str] = {}

    async def sigs_for(w, acc):
        account = acc or ata_address(w, mint, token_program)
        accts[w] = account
        async with sem:
            try:
                res = await rpc.call("getSignaturesForAddress", [account, {"limit": PER_WALLET_MAX_TX + 1}])
            except DataBusy:
                sig_lists[w] = None
                return
        sig_lists[w] = [{"signature": s["signature"], "slot": s.get("slot"), "blockTime": s.get("blockTime")}
                        for s in res or [] if not s.get("err")]
    await asyncio.gather(*(sigs_for(w, acc) for w, acc, _ in cands))
    failed_sig = [w for w, _, _ in cands if sig_lists.get(w) is None]
    if len(failed_sig) > len(cands) / 2:
        raise DataBusy(f"history lookups failed for {len(failed_sig)} of {len(cands)} wallets")
    very_active = [w for w, _, _ in cands if sig_lists.get(w) and len(sig_lists[w]) > PER_WALLET_MAX_TX]
    todo = [(w, src) for w, _, src in cands if sig_lists.get(w) is not None and w not in very_active]
    # fewest transactions first: a wallet only counts once ALL its txs are read, so this checks the most wallets
    todo.sort(key=lambda x: len(sig_lists[x[0]]))
    seen_sig: set[str] = set()
    all_sigs = []
    for w, _ in todo:
        for s in sig_lists[w]:
            if s["signature"] not in seen_sig:
                seen_sig.add(s["signature"])
                all_sigs.append(s)

    async def txprog(done, total):
        if progress:
            await progress(f"📜 Reading trades… {done}/{total}")
    got = await rpc.many_tx(all_sigs, max(10.0, BUDGET_S - (time.monotonic() - t0)),
                            concurrency=16 if config.CUSTOM_RPC else 6, progress=txprog)
    if all_sigs:
        rpc.check_batch(min_attempts=min(10, len(all_sigs)))
    reds = {s["signature"]: red for s, red in got}

    # current balances of each wallet's token account (one call per 100)
    accounts = [accts[w] for w, _ in todo]
    bal_raw = await rpc.accounts(accounts) if accounts else []
    rows, unchecked = [], 0
    for (w, src), acc_info in zip(todo, bal_raw):
        sl = sig_lists[w]
        if any(s["signature"] not in reds for s in sl):
            unchecked += 1                       # ran out of time / failed: don't guess
            continue
        info = ((((acc_info or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
        balance = float((info.get("tokenAmount") or {}).get("uiAmount") or 0)
        events = []
        for s in sl:
            c = classify(reds[s["signature"]], w, mint, values)
            if c:
                events.append((reds[s["signature"]].get("t") or s.get("blockTime") or 0, s.get("slot") or 0, c))
        r = wallet_pnl(w, events, balance)
        r["source"] = src
        rows.append(r)
    flag_wallets(rows, launch_ts)
    order = sorted((r for r in rows if r["first_t"]), key=lambda r: r["first_t"])
    for i, r in enumerate(order, 1):
        r["entry_rank"] = i
    flagged = [r for r in rows if r["flags"]]
    real = [r for r in rows if not r["flags"] and not r["unknown"] and r["sol_in"] >= MIN_REAL_SOL]
    keep = ("wallet", "buys", "sells", "sol_in", "sol_out", "realized_sol", "held_tokens", "held_cost_sol",
            "holding_pct", "entry_rank", "balance_ok", "tok_in", "first_t", "source")
    flagged.sort(key=lambda x: -(x["sol_out"] + x["sol_in"]))
    return {"mint": mint, "sym": (best.get("baseToken") or {}).get("symbol") or "?", "version": 2,
            "candidates": [{k: r.get(k) for k in keep} for r in real], "price_sol": price_sol,
            "priced": price_sol is not None, "launch_ts": launch_ts,
            "checked": len(rows), "wanted": len(cands), "very_active": len(very_active), "unchecked": unchecked,
            "history_failed": len(failed_sig), "holders_found": len(holders), "holders_failed": holder_err,
            "unvalued": sum(1 for r in rows if r["unknown"]), "small": sum(1 for r in rows if not r["flags"]
                                                                          and r["sol_in"] < MIN_REAL_SOL),
            "flagged": [{k: r.get(k) for k in ("wallet", "sol_in", "sol_out", "reasons")} for r in flagged[:25]],
            "flagged_count": len(flagged), "seconds": round(time.monotonic() - t0, 1)}
