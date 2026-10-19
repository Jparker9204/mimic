import re

import pytest

from mimic import texts
from mimic.trades import best_worst, coin_profits, luck, sol_usd_of

ALLOWED = {"b", "/b", "i", "/i", "code", "/code"}


def pos(mint, sol_in, sol_out, tok_in, tok_out, price_now=None, dead=False, first_t=0, sell_t=None, sym=None):
    return {"mint": mint, "sym": sym or mint[:4], "sol_in": sol_in, "sol_out": sol_out, "tok_in": tok_in,
            "tok_out": tok_out, "usd_in": sol_in * 100, "price_now": price_now, "dead": dead, "first_t": first_t,
            "first_sell_t": sell_t}


def scan(positions, sol_usd=100.0):
    return {"wallet": "W" * 44, "days": 14, "scanned_at": 100_000, "sol_usd": sol_usd, "positions": positions}


def by(rows):
    return {r["mint"]: r for r in rows}


def test_coin_profit_realized_plus_unrealized():
    s = scan([pos("A" * 44, 1.0, 1.5, 100, 50, price_now=2.0, first_t=0, sell_t=3600),    # 50 left * $2 / $100 = 1 SOL
              pos("B" * 44, 2.0, 0.5, 100, 100, first_t=0, sell_t=600),                     # sold all, lost 1.5
              pos("C" * 44, 1.0, 0.0, 100, 0, dead=True, first_t=0),                         # dead, held = 0
              pos("D" * 44, 1.0, 0.0, 100, 0, price_now=None)])                              # still held, no price
    rows, unpriced = coin_profits(s)
    r = by(rows)
    assert r["A" * 44]["profit_sol"] == pytest.approx(1.5) and r["A" * 44]["holding_pct"] == 50
    assert r["A" * 44]["still_in"] and r["A" * 44]["held_s"] == 100_000       # held so far
    assert r["B" * 44]["profit_sol"] == pytest.approx(-1.5) and r["B" * 44]["held_s"] == 600
    assert r["C" * 44]["profit_sol"] == pytest.approx(-1.0) and r["C" * 44]["dead"]
    assert unpriced == 1 and "D" * 44 not in r


def test_selling_more_than_bought_counts_only_bought_part():
    rows, _ = coin_profits(scan([pos("A" * 44, 1.0, 10.0, 100, 1000)]))      # sold 1000, only bought 100 in window
    assert rows[0]["profit_sol"] == pytest.approx(0.0) and rows[0]["holding_pct"] == 0


def test_best_worst_split_and_cap():
    rows = [{"p": x} for x in (5, 4, 3, 2, 1, 0.5, -1, -2, -3, -4, -5, -6, 0)]
    best, worst = best_worst(rows, "p")
    assert [r["p"] for r in best] == [5, 4, 3, 2, 1] and [r["p"] for r in worst] == [-6, -5, -4, -3, -2]
    b, w = best_worst([{"p": 1}, {"p": 2}], "p")
    assert len(b) == 2 and w == []                                           # never the same coin twice


def test_luck_check():
    lk = luck([{"p": 8.5}, {"p": 1.0}, {"p": 0.5}], "p")
    assert lk["share"] == 85 and lk["without_best"] == pytest.approx(1.5)
    lk = luck([{"p": 3.0}, {"p": -3.4}, {"p": 0.5}], "p")
    assert lk["total"] == pytest.approx(0.1) and lk["share"] == 3000 and lk["without_best"] == pytest.approx(-2.9)
    assert luck([{"p": -1.0}], "p")["share"] is None


def test_best_worst_text_labels_luck_and_html():
    s = scan([pos("A" * 44, 1.0, 9.0, 100, 100, first_t=0, sell_t=7500, sym="<WIN>"),
              pos("B" * 44, 1.0, 0.2, 100, 100, first_t=0, sell_t=60),
              pos("C" * 44, 1.0, 0.6, 100, 100, first_t=0, sell_t=60)])
    out = texts.best_worst_text(s)
    assert "1. <b>&lt;WIN&gt;</b> · <b>+8.00 SOL (+800%)</b>" in out
    assert "Put in: 1.00 SOL · Held: 2h 5m · Still holding: 0%" in out
    assert "<code>" + "A" * 44 + "</code>" in out
    assert "Best 5" in out and "Worst 5" in out
    assert "🎲 Luck check: best coin = 116% of total profit" not in out
    assert "more than the whole total" in out                               # 8.0 vs total 6.8
    assert "Without its best trade: <b>-1.20 SOL</b>" in out and "comes from one coin" in out
    assert all(t in ALLOWED for t in re.findall(r"<([^>]*)>", out))


def test_luck_share_line_and_spread_verdict():
    s = scan([pos(m * 44, 1.0, 1.0 + g, 100, 100, sell_t=10) for m, g in (("A", 1.0), ("B", 1.0), ("C", 1.0), ("D", 1.0))])
    out = texts.best_worst_text(s)
    assert "best coin = 25% of total profit" in out and "spread over several coins" in out


def test_best_worst_text_fits_telegram_with_long_tickers():
    ps = [pos(f"{i:02d}" + "M" * 42, 1.0, 1.0 + (i - 20) * 0.1, 100, 100, sell_t=10, sym="X" * 30) for i in range(40)]
    out = texts.best_worst_text(scan(ps))
    assert len(out) < 4000 and out.count("<code>") == 11


def test_old_cached_scan_without_saved_sol_price():
    s = scan([pos("A" * 44, 1.0, 0.0, 100, 0, price_now=1.0, first_t=5)])
    del s["sol_usd"]
    assert sol_usd_of(s) == 100.0
    rows, _ = coin_profits(s)
    assert rows[0]["profit_sol"] == pytest.approx(0.0)


def sim(rows):
    return {"wallet": "W" * 44, "label": "Size: 0.2 SOL per trade · Take profit: +50% · Exit: when the wallet sells",
            "rows": rows}


def srow(sym, pnl, exit_="take-profit"):
    return {"mint": sym * 44, "sym": sym, "pnl_sol": pnl, "sol_in": 0.2005, "exit": exit_}


def test_sim_trades_text():
    out = texts.sim_trades_text(sim([srow("A", 0.098), srow("B", -0.06, "stop-loss"), srow("C", -0.2005, "dead (≈0)"),
                                     srow("D", 0.01, "wallet sold")]))
    assert "1. <b>A</b> · <b>+0.098 SOL (+49%)</b> · Exit: take-profit" in out
    assert "1. <b>C</b> · <b>-0.201 SOL (-100%)</b> · Exit: dead (≈0)" in out
    assert "Without its best trade: <b>-0.25 SOL</b>" in out
    assert "4 copied trades · total -0.15" in out
    assert all(t in ALLOWED for t in re.findall(r"<([^>]*)>", out)) and len(out) < 4000
    assert "No copied trades" in texts.sim_trades_text(sim([]))


async def test_buttons_are_free_and_use_cache_only(monkeypatch):
    from mimic.bot import MimicBot
    from mimic.db import DB
    b = MimicBot(DB(":memory:"))

    async def boom(*a, **k):
        raise AssertionError("no API calls")
    monkeypatch.setattr(b.net, "request", boom)
    b.db.cache_set("scan:" + "W" * 44, scan([pos("A" * 44, 1.0, 2.0, 100, 100, sell_t=10)]), 600)
    b.db.cache_set("simres:abc", sim([srow("A", 0.1)]), 600)
    sent = []

    class Msg:
        async def reply_text(self, text, **k):
            sent.append(text)

    class Q:
        message = Msg()
        def __init__(self, data): self.data = data
        async def answer(self, *a, **k): pass

    class U:
        def __init__(self, data):
            self.callback_query = Q(data)
            self.effective_user = type("u", (), {"id": 7})()
            self.effective_chat = type("c", (), {"id": 7})()

    class Ctx:
        user_data: dict = {}
    for d in ("wb:" + "W" * 44, "st:abc", "st:gone"):
        await b.on_button(U(d), Ctx())
    assert "Best &amp; worst trades" in sent[0] and "Sim trades" in sent[1] and "expired" in sent[2]
    assert b.db.count_since(7, "wallet", 0) == 0 and b.db.count_since(7, "sim", 0) == 0
    assert len("wb:" + "W" * 44) <= 64
