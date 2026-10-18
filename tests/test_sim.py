import pytest
from mimic.sim import SimParams, Trade, build_path, price_at, simulate, simulate_trade

SOL = lambda t: 100.0   # flat SOL price keeps the math easy
T0 = 1_000_000


def trade(path, sell_t=None, sell_price=None, now_price=1.0, dead=False, wallet_price=1.0):
    return Trade("M", "M", T0, wallet_price, sell_t, sell_price, path, now_price, dead)


def c(start, o, h, l, cl, secs=60):
    return (start, secs, o, h, l, cl)


def expected_out(size, entry, exitp, fee=0.01, slip=0.02, net=0.0005):
    tokens = size * (1 - fee) / (entry * (1 + slip) / 100)
    return tokens * exitp / 100 * (1 - fee) - net


def test_take_profit_math():
    path = [c(T0 + 60, 1.0, 1.2, 0.95, 1.1), c(T0 + 120, 1.1, 2.0, 1.05, 1.9)]
    r = simulate_trade(trade(path), SimParams(delay_s=0, size_sol=0.1, tp=1.5, exit="sl"), SOL, now=T0 + 999)
    assert r["exit"] == "take-profit"
    fill = 1.02
    assert r["sol_out"] == pytest.approx(expected_out(0.1, 1.0, fill * 1.5), rel=1e-6)
    assert r["sol_in"] == pytest.approx(0.1005)


def test_stop_loss_wins_when_candle_touches_both():
    path = [c(T0 + 60, 1.0, 3.0, 0.5, 1.0)]
    r = simulate_trade(trade(path), SimParams(delay_s=0, tp=1.5, exit="sl", sl=0.3), SOL, now=T0 + 999)
    assert r["exit"] == "stop-loss"
    assert r["exit_usd"] == pytest.approx(1.02 * 0.7)


def test_stop_loss_gap_fills_at_open():
    path = [c(T0 + 60, 0.4, 0.45, 0.3, 0.35)]
    r = simulate_trade(trade(path), SimParams(delay_s=0, tp=None, exit="sl", sl=0.3), SOL, now=T0 + 999)
    assert r["exit_usd"] == pytest.approx(0.4)


def test_mirror_exit_uses_wallet_sell_and_delay_price():
    path = [c(T0, 1.0, 1.0, 1.0, 1.0), c(T0 + 60, 1.1, 1.2, 1.0, 1.1), c(T0 + 300, 1.3, 1.3, 1.3, 1.3)]
    tr = trade(path, sell_t=T0 + 290, sell_price=1.25)
    r0 = simulate_trade(tr, SimParams(delay_s=0, tp=2.0, exit="mirror"), SOL, now=T0 + 999)
    assert r0["exit"] == "wallet sold" and r0["exit_usd"] == 1.25
    r15 = simulate_trade(tr, SimParams(tp=2.0, exit="mirror", delay_s=15), SOL, now=T0 + 999)
    assert r15["exit"] == "wallet sold" and r15["exit_usd"] == pytest.approx(1.3)
    assert r15["entry_usd"] == pytest.approx(1.0 * 1.02)     # avg of the 1-min candle at T0+15


def test_entry_candle_is_skipped_for_exits():
    # the high of the candle we buy into must not trigger TP
    path = [c(T0, 1.0, 5.0, 1.0, 1.0)]
    r = simulate_trade(trade(path, now_price=1.0), SimParams(tp=1.5, exit="sl", delay_s=15), SOL, now=T0 + 999)
    assert r["exit"] == "still holding"


def test_dead_coin_is_zero_and_summary():
    t1 = trade([c(T0 + 60, 1.0, 1.1, 0.9, 1.0)], dead=True)
    t2 = trade([c(T0 + 60, 1.0, 2.0, 0.9, 1.0)])
    res = simulate([t1, t2], SimParams(delay_s=0, size_sol=0.2, tp=1.5, exit="sl"), SOL, now=T0 + 999)
    s = res.summary
    assert s["trades"] == 2 and s["wins"] == 1 and s["losses"] == 1
    assert s["worst"] == pytest.approx(-0.2005)
    assert s["sol_in"] == pytest.approx(0.401)


def test_price_at_and_path():
    p = build_path([[T0, 1, 2, 0.5, 1.5]], [[T0 - 900, 9, 9, 9, 9], [T0 + 900, 3, 4, 2, 3]])
    assert [x[1] for x in p] == [60, 900]
    assert price_at(p, T0 + 10)[0] == pytest.approx(1.25)
    assert price_at(p, T0 + 300)[0] == 1.5            # gap -> last close
    assert price_at(p, T0 - 5000)[0] == 1             # before data -> next open


def test_default_delay_is_15s_and_labels_are_plain():
    from mimic import texts
    p = SimParams(size_sol=0.2, tp=1.5, exit="mirror")
    assert p.delay_s == 15
    assert p.label() == "Size: 0.2 SOL per trade · Take profit: +50% · Exit: when the wallet sells"
    assert SimParams(tp=2.0, exit="sl", sl=0.2).label().endswith("Take profit: 2x · Exit: stop-loss -20%")
    summ = {"net_sol": 0.117, "net_pct": 3.2, "trades": 18, "wins": 9, "losses": 9, "sol_in": 3.609,
            "sol_out": 3.726, "worst": -0.153, "best": 0.192, "skipped": 0}
    out = texts.sim_text("W" * 44, p.label(), summ, p.delay_s)
    assert "<code>" + "W" * 44 + "</code>" in out and "Assumes ~15s copy delay" in out
    assert "Put in: 3.609 SOL · Got back: 3.726 SOL" in out


def test_sim_setup_has_three_steps_no_delay_choice():
    from mimic.bot import MimicBot
    from mimic.db import DB
    b = MimicBot(DB(":memory:"))

    class Ctx:
        user_data = {"last_wallet": "x" * 44}
    seen = []
    for step in ("size", "tp", "exit", "ready"):
        Ctx.user_data["sim_step"] = step
        text, kb = b._sim_screen(Ctx)
        seen.append(text)
        datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        assert not any(d.startswith("dl:") for d in datas)
        assert all(len(d) <= 64 for d in datas)
    assert "Step 1 of 3" in seen[0] and "Step 3 of 3" in seen[2] and "15s copy delay" in seen[3]
