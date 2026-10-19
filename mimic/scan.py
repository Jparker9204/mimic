"""Wallet Scan: last N days of a wallet's token buys, how they did, and how copyable it is."""
from __future__ import annotations
import statistics, time
from dataclasses import dataclass
from . import config
from .market import Market, SolPrice
from .solana import Rpc, parse_swap


@dataclass
class Services:
    rpc: Rpc
    market: Market


def build_positions(swaps: list[tuple[float, str, tuple]], sol: SolPrice) -> dict[str, dict]:
    """swaps: [(time, sig, (side, mint, qty, sol_amt))]. Positions keyed by mint, for tokens BOUGHT in the window."""
    pos: dict[str, dict] = {}
    for t, sig, (side, mint, q, s) in sorted(swaps, key=lambda x: x[0]):
        if side == "BUY":
            p = pos.setdefault(mint, {"mint": mint, "buys": 0, "sells": 0, "sol_in": 0.0, "sol_out": 0.0,
                                      "tok_in": 0.0, "tok_out": 0.0, "usd_in": 0.0, "first_t": t, "first_sig": sig,
                                      "first_price_usd": s / q * sol.at(t), "first_sell_t": None,
                                      "first_sell_price_usd": None})
            p["buys"] += 1
            p["sol_in"] += s
            p["tok_in"] += q
            p["usd_in"] += s * sol.at(t)
        elif mint in pos:
            p = pos[mint]
            p["sells"] += 1
            p["sol_out"] += s
            p["tok_out"] += q
            if p["first_sell_t"] is None:
                p["first_sell_t"] = t
                p["first_sell_price_usd"] = s / q * sol.at(t)
    for p in pos.values():
        p["entry_usd"] = p["usd_in"] / p["tok_in"] if p["tok_in"] else None
    return pos


def peak_after(candles: list[list[float]], t0: float) -> float | None:
    """Highest price in candles that START at/after the buy (conservative: skips the buy's own candle)."""
    highs = [c[2] for c in candles if c[0] >= t0]
    return max(highs) if highs else None


def copyability(median_hold_min: float | None, buys_per_token: float) -> str:
    if median_hold_min is None:
        timing = "No sells yet in this window — hold time unknown."
    elif median_hold_min < 2:
        timing = "🔴 Hard to copy: usually flips in under 2 min."
    elif median_hold_min < 10:
        timing = "🟡 Tricky: fast trader, you need very quick alerts."
    else:
        timing = f"🟢 Copyable timing: usually holds ~{median_hold_min:.0f} min."
    if buys_per_token >= 4:
        style = "Buys the same coin many times — copying just the first buy misses a lot."
    elif buys_per_token >= 2:
        style = "Often adds to positions."
    else:
        style = "Mostly one buy per coin."
    return f"{timing}\n{style}"


def summarize(positions: list[dict]) -> dict:
    n = len(positions)
    priced = [p for p in positions if p.get("peak_x") is not None]
    holds = [(p["first_sell_t"] - p["first_t"]) / 60 for p in positions if p.get("first_sell_t")]
    up = sum(1 for p in positions if not p.get("dead") and (p.get("now_x") or 0) > 1)
    dead = sum(1 for p in positions if p.get("dead"))
    buys = sum(p["buys"] for p in positions)

    def pct(k, base):
        return round(100 * k / base) if base else None
    med = statistics.median(holds) if holds else None
    bpt = buys / n if n else 0.0
    return {
        "tokens": n, "buys": buys, "buys_per_token": round(bpt, 1), "priced": len(priced),
        "hit25": pct(sum(p["peak_x"] >= 1.25 for p in priced), len(priced)),
        "hit50": pct(sum(p["peak_x"] >= 1.5 for p in priced), len(priced)),
        "hit2x": pct(sum(p["peak_x"] >= 2.0 for p in priced), len(priced)),
        "up_now": pct(up, n), "down_now": pct(n - up, n), "dead": pct(dead, n),
        "sol_in": round(sum(p["sol_in"] for p in positions), 3),
        "sol_out": round(sum(p["sol_out"] for p in positions), 3),
        "median_hold_min": round(med, 1) if med is not None else None,
        "sold_count": len(holds),
        "copyability": copyability(med, bpt),
    }


async def wallet_scan(svc: Services, wallet: str, progress=None, days: int | None = None) -> dict:
    days = days or config.SCAN_DAYS
    now = time.time()
    since = now - days * 86400
    t_start = time.monotonic()
    if progress:
        await progress("📜 Reading trade history…")
    sigs = await svc.rpc.signatures(wallet, since, config.MAX_SIGS)
    sigs = [s for s in sigs if not s.get("err")]
    to_fetch = sigs[:config.MAX_TXS]

    async def txprog(done, total):
        if progress:
            await progress(f"📜 Reading trades… {done}/{total}")
    got = await svc.rpc.many_tx(to_fetch, config.TX_BUDGET_S, progress=txprog)
    if to_fetch:
        svc.rpc.check_batch(min_attempts=min(10, len(to_fetch)))     # rate limited -> DataBusy, not a fake result
    covered_from = min((s.get("blockTime") or now) for s, _ in got) if got else now
    capped = len(got) < len(sigs)
    swaps = []
    for s, red in got:
        sw = parse_swap(red, wallet)
        if sw:
            swaps.append((red.get("t") or s.get("blockTime") or 0, s["signature"], sw))
    if progress:
        await progress(f"💹 Checking prices for {len({x[2][1] for x in swaps if x[2][0] == 'BUY'})} coins…")
    sol = SolPrice(await svc.market.sol_usd_series())
    pos = build_positions(swaps, sol)
    dex = await svc.market.dex_info(list(pos))
    newest = sorted(pos.values(), key=lambda p: -p["first_t"])
    for i, p in enumerate(newest):
        d = dex.get(p["mint"])
        p["sym"] = (d or {}).get("sym") or "?"
        p["liq"] = (d or {}).get("liq") or 0.0
        p["price_now"] = (d or {}).get("price") or None
        p["dead"] = (not d) or p["liq"] < config.DEAD_LIQ_USD
        p["now_x"] = (p["price_now"] / p["entry_usd"]) if (p["price_now"] and p["entry_usd"]) else None
        p["pool"] = None
        p["peak_x"] = None
        if i < config.MAX_TOKENS:
            pool = await svc.market.pool_for(p["mint"], d)
            p["pool"] = pool
            if pool:
                c15 = await svc.market.candles_since(pool, p["mint"], p["first_t"], now)
                pk = peak_after(c15, p["first_t"])
                if pk and p["entry_usd"]:
                    p["peak_x"] = pk / p["entry_usd"]
            if progress and i % 5 == 4:
                await progress(f"💹 Checking prices… {i + 1}/{min(len(newest), config.MAX_TOKENS)}")
    stats = summarize(newest)
    return {"wallet": wallet, "days": days, "scanned_at": now, "covered_from": covered_from, "sol_usd": sol.at(now),
            "sigs": len(sigs), "txs": len(got), "capped": capped, "stats": stats,
            "positions": newest, "seconds": round(time.monotonic() - t_start, 1)}
