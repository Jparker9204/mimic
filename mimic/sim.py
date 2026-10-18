"""Copytrade simulation: copy each coin's FIRST buy with a fixed size, then exit by rules.

Conservative rules:
- entry = wallet's own price (0s delay) or the average price of the 1-minute candle at buy+delay,
  plus 2% slippage; 1% fee on buy and on sell; 0.0005 SOL network fee per transaction;
- TP/SL are checked from the candle AFTER the entry candle;
- if one candle touches both the stop-loss and the take-profit, the stop-loss wins;
- a stop-loss fills at the worse of the SL level or the candle open (gaps down);
- coins with no sell at the end are valued at the current price; dead coins at 0.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Callable
from . import config
from .market import Market, SolPrice

Candle = tuple  # (start_ts, seconds, open, high, low, close)


@dataclass
class SimParams:
    size_sol: float = 0.1
    delay_s: int = 0
    tp: float | None = 1.5          # take-profit multiple, e.g. 1.5 = +50%; None = no TP
    exit: str = "mirror"            # "mirror" (sell when the wallet sells) or "sl"
    sl: float = 0.30                # stop-loss drop, 0.30 = -30% (used when exit == "sl")
    slippage: float = config.ENTRY_SLIPPAGE
    fee: float = config.SWAP_FEE
    net_fee: float = config.NETWORK_FEE_SOL

    def label(self) -> str:
        tp = "no TP" if not self.tp else (f"TP {self.tp:g}x" if self.tp >= 2 else f"TP +{(self.tp - 1) * 100:.0f}%")
        ex = "mirror wallet sell" if self.exit == "mirror" else f"SL -{self.sl * 100:.0f}%"
        return f"{self.size_sol:g} SOL · {self.delay_s}s delay · {tp} · {ex}"


@dataclass
class Trade:
    mint: str
    sym: str
    first_t: float
    wallet_price: float                 # USD/token the wallet paid on its first buy
    sell_t: float | None                # wallet's first sell after that
    sell_price: float | None
    path: list[Candle]                  # 1m candles then 15m candles, oldest first
    price_now: float | None
    dead: bool


@dataclass
class SimResult:
    params: SimParams
    rows: list[dict] = field(default_factory=list)
    skipped: int = 0

    @property
    def summary(self) -> dict:
        n = len(self.rows)
        sin = sum(r["sol_in"] for r in self.rows)
        sout = sum(r["sol_out"] for r in self.rows)
        pnl = [r["pnl_sol"] for r in self.rows]
        return {"trades": n, "wins": sum(1 for p in pnl if p > 0), "losses": sum(1 for p in pnl if p <= 0),
                "sol_in": round(sin, 4), "sol_out": round(sout, 4), "net_sol": round(sout - sin, 4),
                "net_pct": round(100 * (sout - sin) / sin, 1) if sin else 0.0,
                "worst": round(min(pnl), 4) if pnl else 0.0, "best": round(max(pnl), 4) if pnl else 0.0,
                "skipped": self.skipped}


def build_path(c1m: list[list[float]], c15m: list[list[float]]) -> list[Candle]:
    a = [(r[0], 60, r[1], r[2], r[3], r[4]) for r in c1m]
    end = a[-1][0] + 60 if a else 0
    b = [(r[0], 900, r[1], r[2], r[3], r[4]) for r in c15m if r[0] >= end]
    return a + b


def price_at(path: list[Candle], t: float) -> tuple[float | None, float]:
    """Price at time t and the time from which to start checking exits.
    Inside a candle: its average (O+H+L+C)/4. In a gap: last close. Before data: next open."""
    best = None
    for c in path:
        start, secs = c[0], c[1]
        if start <= t < start + secs:
            return (c[2] + c[3] + c[4] + c[5]) / 4, start + secs
        if start + secs <= t:
            best = (c[5], t)
        elif start > t:
            return best if best else (c[2], start + secs)
    return best if best else (None, t)


def simulate_trade(tr: Trade, p: SimParams, sol: Callable[[float], float], now: float) -> dict | None:
    if p.delay_s == 0:
        e, check_from = tr.wallet_price, tr.first_t + 1
    else:
        e, check_from = price_at(tr.path, tr.first_t + p.delay_s)
    if not e:
        return None
    t_in = tr.first_t + p.delay_s
    fill = e * (1 + p.slippage)
    tokens = p.size_sol * (1 - p.fee) / (fill / sol(t_in))
    tp_px = fill * p.tp if p.tp else None
    sl_px = fill * (1 - p.sl) if p.exit == "sl" else None
    sell_t = tr.sell_t if p.exit == "mirror" else None
    exit_ = None
    for c in tr.path:
        start, secs, o, h, lo, _ = c
        if start < check_from:
            continue
        if sell_t is not None and start + secs > sell_t:
            break
        if sl_px is not None and lo <= sl_px:                     # SL first on ambiguous candles
            exit_ = ("stop-loss", min(sl_px, o), start)
            break
        if tp_px is not None and h >= tp_px:
            exit_ = ("take-profit", max(tp_px, o) if o < tp_px * 3 else tp_px, start)
            break
    if exit_ is None and sell_t is not None:
        if p.delay_s == 0:
            exit_ = ("wallet sold", tr.sell_price or 0.0, sell_t)
        else:
            px, _ = price_at(tr.path, sell_t + p.delay_s)
            exit_ = ("wallet sold", px or 0.0, sell_t + p.delay_s)
    if exit_ is None:
        exit_ = ("dead (≈0)", 0.0, now) if tr.dead else ("still holding", tr.price_now or 0.0, now)
    why, px, t_out = exit_
    sol_in = p.size_sol + p.net_fee
    sol_out = 0.0 if not px else max(0.0, tokens * px / sol(t_out) * (1 - p.fee) - p.net_fee)
    return {"mint": tr.mint, "sym": tr.sym, "first_buy": tr.first_t, "entry_usd": fill, "exit": why,
            "exit_usd": px, "exit_t": t_out, "sol_in": round(sol_in, 6), "sol_out": round(sol_out, 6),
            "pnl_sol": round(sol_out - sol_in, 6)}


def simulate(trades: list[Trade], p: SimParams, sol: Callable[[float], float], now: float | None = None) -> SimResult:
    now = time.time() if now is None else now
    res = SimResult(p)
    skipped = 0
    for tr in trades:
        r = simulate_trade(tr, p, sol, now)
        if r:
            res.rows.append(r)
        else:
            skipped += 1
    res.skipped = skipped
    return res


async def build_trades(market: Market, scan: dict, progress=None) -> tuple[list[Trade], SolPrice]:
    """Fetch the 1m (and 15m) candles a sim needs for a finished wallet scan."""
    sol = SolPrice(await market.sol_usd_series())
    now = scan["scanned_at"]
    trades = []
    usable = [p for p in scan["positions"] if p.get("pool") and p.get("first_price_usd")]
    for i, p in enumerate(usable):
        c1 = await market.candles_1m_after(p["pool"], p["mint"], p["first_t"])
        c15 = await market.candles_since(p["pool"], p["mint"], p["first_t"], now)
        trades.append(Trade(p["mint"], p.get("sym", "?"), p["first_t"], p["first_price_usd"],
                            p.get("first_sell_t"), p.get("first_sell_price_usd"),
                            build_path(c1, c15), p.get("price_now"), bool(p.get("dead"))))
        if progress and i % 5 == 4:
            await progress(f"🧪 Loading 1-min charts… {i + 1}/{len(usable)}")
    return trades, sol
