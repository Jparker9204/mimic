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
    r = simulate_trade(trade(path), SimParams(size_sol=0.1, tp=1.5, exit="sl"), SOL, now=T0 + 999)
    assert r["exit"] == "take-profit"
    fill = 1.02
    assert r["sol_out"] == pytest.approx(expected_out(0.1, 1.0, fill * 1.5), rel=1e-6)
    assert r["sol_in"] == pytest.approx(0.1005)


def test_stop_loss_wins_when_candle_touches_both():
    path = [c(T0 + 60, 1.0, 3.0, 0.5, 1.0)]
    r = simulate_trade(trade(path), SimParams(tp=1.5, exit="sl", sl=0.3), SOL, now=T0 + 999)
    assert r["exit"] == "stop-loss"
    assert r["exit_usd"] == pytest.approx(1.02 * 0.7)


def test_stop_loss_gap_fills_at_open():
    path = [c(T0 + 60, 0.4, 0.45, 0.3, 0.35)]
    r = simulate_trade(trade(path), SimParams(tp=None, exit="sl", sl=0.3), SOL, now=T0 + 999)
    assert r["exit_usd"] == pytest.approx(0.4)


def test_mirror_exit_uses_wallet_sell_and_delay_price():
    path = [c(T0, 1.0, 1.0, 1.0, 1.0), c(T0 + 60, 1.1, 1.2, 1.0, 1.1), c(T0 + 300, 1.3, 1.3, 1.3, 1.3)]
    tr = trade(path, sell_t=T0 + 290, sell_price=1.25)
    r0 = simulate_trade(tr, SimParams(tp=2.0, exit="mirror"), SOL, now=T0 + 999)
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
    res = simulate([t1, t2], SimParams(size_sol=0.2, tp=1.5, exit="sl"), SOL, now=T0 + 999)
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
