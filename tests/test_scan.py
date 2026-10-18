from mimic.coinscan import pick_sigs
from mimic.market import SolPrice
from mimic.scan import build_positions, copyability, peak_after, summarize


def test_positions_hold_time_and_summary():
    sol = SolPrice([[0, 100, 100, 100, 100]])
    swaps = [(1000, "s1", ("BUY", "A", 100, 1.0)), (1060, "s2", ("BUY", "A", 100, 1.0)),
             (1000 + 90, "s3", ("SELL", "A", 200, 3.0)), (5000, "s4", ("BUY", "B", 10, 0.5)),
             (100, "s0", ("SELL", "C", 5, 1.0))]                       # sold before any buy -> ignored
    pos = build_positions(swaps, sol)
    assert set(pos) == {"A", "B"}
    a = pos["A"]
    assert a["buys"] == 2 and a["sol_in"] == 2.0 and a["sol_out"] == 3.0
    assert a["entry_usd"] == 1.0 and a["first_sell_t"] == 1090
    a.update(peak_x=2.5, now_x=0.5, dead=False)
    pos["B"].update(peak_x=1.3, now_x=1.2, dead=False)
    s = summarize(list(pos.values()))
    assert s["tokens"] == 2 and s["hit25"] == 100 and s["hit50"] == 50 and s["hit2x"] == 50
    assert s["up_now"] == 50 and s["median_hold_min"] == 1.5
    assert "Hard to copy" in s["copyability"]


def test_peak_skips_buy_candle():
    assert peak_after([[0, 1, 9, 1, 1], [900, 1, 2, 1, 1]], 10) == 2
    assert peak_after([], 10) is None


def test_copyability_notes():
    assert "Copyable" in copyability(76, 5)
    assert "Tricky" in copyability(5, 1)
    assert "unknown" in copyability(None, 1)


def test_pick_sigs_sampling():
    sigs = [{"signature": str(i)} for i in range(1000)]   # newest first
    chosen, sampled = pick_sigs(sigs, 100)
    assert sampled and len(chosen) == 100
    assert chosen[0]["signature"] == "999"            # oldest (launch) first
    assert {"0", "940"} <= {c["signature"] for c in chosen}
    assert pick_sigs(sigs[:50], 100) == (list(reversed(sigs[:50])), False)


