"""Best & worst trades: per-coin profit from a cached Wallet Scan or a finished Copytrade Sim. Pure, no API calls."""
from __future__ import annotations

TOP_N = 5


def sol_usd_of(scan: dict) -> float | None:
    """SOL price saved with the scan; older cached scans: the price at the newest buy."""
    if scan.get("sol_usd"):
        return scan["sol_usd"]
    for p in sorted(scan.get("positions") or [], key=lambda p: -(p.get("first_t") or 0)):
        if p.get("sol_in") and p.get("usd_in"):
            return p["usd_in"] / p["sol_in"]
    return None


def coin_profits(scan: dict) -> tuple[list[dict], int]:
    """-> (rows, unpriced). Profit = sells (only on tokens bought in the window) + value of tokens still held - SOL put in.
    Dead coins' holdings are worth 0. Coins still held with no current price are left out (we'd only be guessing)."""
    sol_usd = sol_usd_of(scan)
    now = scan.get("scanned_at") or 0
    rows, unpriced = [], 0
    for p in scan.get("positions") or []:
        tin, tout, sin = p.get("tok_in") or 0.0, p.get("tok_out") or 0.0, p.get("sol_in") or 0.0
        if tin <= 0 or sin <= 0:
            continue
        sold = min(tout, tin)
        proceeds = (p.get("sol_out") or 0.0) * (sold / tout) if tout else 0.0
        held = tin - sold
        held_pct = round(100 * held / tin)
        if held / tin < 0.01 or p.get("dead"):
            held_value = 0.0
        elif p.get("price_now") and sol_usd:
            held_value = held * p["price_now"] / sol_usd
        else:
            unpriced += 1
            continue
        profit = proceeds + held_value - sin
        end = p.get("first_sell_t") if held_pct == 0 and p.get("first_sell_t") else None
        rows.append({"mint": p["mint"], "sym": p.get("sym") or "?", "profit_sol": profit,
                     "profit_pct": round(100 * profit / sin), "sol_in": sin, "holding_pct": held_pct,
                     "dead": bool(p.get("dead")),
                     "held_s": ((end or now) - p["first_t"]) if p.get("first_t") is not None else None,
                     "first_sell_t": p.get("first_sell_t"), "still_in": held_pct > 0})
    return rows, unpriced


def best_worst(rows: list[dict], key: str, n: int = TOP_N) -> tuple[list[dict], list[dict]]:
    """Top n winners (profit > 0) and bottom n losers (profit < 0), never the same row twice."""
    best = sorted((r for r in rows if r[key] > 0), key=lambda r: -r[key])[:n]
    worst = sorted((r for r in rows if r[key] < 0), key=lambda r: r[key])[:n]
    return best, worst


def luck(rows: list[dict], key: str) -> dict:
    """How much of the total comes from the single best trade."""
    total = sum(r[key] for r in rows)
    best = max((r[key] for r in rows), default=0.0)
    share = round(100 * best / total) if total > 0 and best > 0 else None
    return {"total": total, "best": best, "without_best": total - max(best, 0.0), "share": share, "n": len(rows)}
