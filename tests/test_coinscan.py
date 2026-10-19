from mimic.coinscan import aggregate_traders, extract_events, find_clusters
from mimic import texts

M = "MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def sw(who, side, q, sol, slot, t=None):
    return {"t": t or slot, "slot": slot, "who": who, "side": side, "q": q, "sol": sol, "sig": f"{who}{slot}{side}"}


def names(lst):
    return [x["wallet"] for x in lst]


def test_realized_profit_and_sellers_first():
    swaps = [sw("w1", "BUY", 100, 1.0, 10), sw("w2", "BUY", 100, 2.0, 11), sw("w1", "SELL", 50, 3.0, 12),
             sw("w2", "SELL", 100, 1.0, 13), sw("big", "BUY", 100, 50.0, 14)]
    ranked, flagged, small = aggregate_traders(swaps)
    assert names(ranked) == ["w1", "w2", "big"] and not flagged
    assert ranked[0]["realized_sol"] == 2.5 and ranked[0]["holding_pct"] == 50
    assert ranked[1]["realized_sol"] == -1.0


def test_dust_buy_then_sell_transferred_tokens_is_flagged_not_ranked():
    # the WEFT case: "in 0.00" but sold for +0.14 SOL
    swaps = [sw("bundle", "BUY", 1, 0.001, 20), sw("bundle", "SELL", 1000, 0.14, 30), sw("real", "BUY", 100, 0.5, 21)]
    xfers = [{"t": 25, "slot": 25, "to": "bundle", "from": ["dev"], "q": 999}]
    ranked, flagged, _ = aggregate_traders(swaps, xfers, dev="dev")
    assert names(ranked) == ["real"]
    f = flagged[0]
    assert f["wallet"] == "bundle" and {"no_buy", "transfer", "from_dev"} <= set(f["flags"])
    # profit from selling transferred tokens is not counted
    assert f["realized_sol"] < 0.01


def test_selling_transferred_tokens_does_not_inflate_profit():
    swaps = [sw("x", "BUY", 100, 1.0, 40), sw("x", "SELL", 1100, 11.0, 50)]
    xfers = [{"t": 45, "slot": 45, "to": "x", "from": ["someone"], "q": 1000}]
    ranked, flagged, _ = aggregate_traders(swaps, xfers)
    assert not ranked and flagged[0]["flags"] == ["transfer"]
    assert flagged[0]["realized_sol"] == 0.0          # 100 bought tokens sold at the same price as bought


def test_launch_block_and_dev_flags():
    swaps = [sw("dev", "BUY", 1000, 1.0, 100), sw("sniper", "BUY", 500, 0.8, 100),
             sw("late", "BUY", 100, 0.5, 105)]
    ranked, flagged, _ = aggregate_traders(swaps, dev="dev", launch_slots={100})
    assert names(ranked) == ["late"]
    by = {x["wallet"]: x["flags"] for x in flagged}
    assert "dev" in by["dev"] and by["sniper"] == ["launch"]


def test_same_slot_cluster_with_similar_amounts():
    swaps = [sw("a", "BUY", 10, 1.00, 7), sw("b", "BUY", 10, 1.02, 7), sw("c", "BUY", 10, 0.97, 7),
             sw("d", "BUY", 10, 5.0, 7),                     # same slot, very different size
             sw("e", "BUY", 10, 1.0, 8), sw("f", "BUY", 10, 1.0, 8)]   # only 2 in this slot
    assert find_clusters(swaps) == {"a", "b", "c"}


def test_small_buyers_not_ranked():
    ranked, flagged, small = aggregate_traders([sw("tiny", "BUY", 10, 0.02, 5), sw("ok", "BUY", 10, 0.05, 6)])
    assert names(ranked) == ["ok"] and small == 1 and not flagged


def test_extract_events_payer_swap_and_transfer():
    buy = ({"signature": "s1", "slot": 9, "blockTime": 1},
           {"t": 1, "err": False, "payer": "p", "tok": {"p": {M: 50.0}, "pool": {M: -50.0}}, "sol": {"p": -1.0}})
    xfer = ({"signature": "s2", "slot": 10, "blockTime": 2},
            {"t": 2, "err": False, "payer": "dev", "tok": {"dev": {M: -30.0}, "r1": {M: 10.0}, "r2": {M: 20.0}},
             "sol": {"dev": -0.00001}})
    swaps, transfers = extract_events([buy, xfer], M)
    assert [(e["who"], e["side"]) for e in swaps] == [("p", "BUY")]
    assert {(t["to"], tuple(t["from"])) for t in transfers} == {("r1", ("dev",)), ("r2", ("dev",))}


def res_for(ranked, flagged, small, price=None):
    return {"mint": M, "sym": "WEFT", "sigs": 10, "txs": 10, "sampled": False, "price_sol": price,
            "candidates": ranked, "flagged": flagged, "flagged_count": len(flagged),
            "small": small, "launch_checked": True, "priced": price is not None}


def test_only_meaningful_winners_ranked_never_losers():
    from mimic.coinscan import rank_profitable
    swaps = [sw("big", "BUY", 100, 1.0, 10), sw("big", "SELL", 100, 2.5, 20),        # +1.5 SOL (+150%)
             sw("tiny", "BUY", 100, 0.5, 11), sw("tiny", "SELL", 100, 0.52, 21),     # +0.02 -> too small
             sw("pct", "BUY", 100, 5.0, 12), sw("pct", "SELL", 100, 5.3, 22),        # +0.3 but only 6%
             sw("loser", "BUY", 100, 1.0, 13), sw("loser", "SELL", 100, 0.2, 23),
             sw("holder", "BUY", 100, 0.5, 14)]                                       # unrealized only
    ranked, _, _ = aggregate_traders(swaps)
    win = rank_profitable(ranked, price_sol=0.02)       # holder's 100 tokens now worth 2.0 SOL -> +1.5
    assert [x["wallet"] for x in win] == ["big", "holder"]
    assert win[0]["profit_sol"] == 1.5 and win[0]["profit_pct"] == 150
    assert win[1]["unrealized_sol"] == 1.5 and win[1]["holding_pct"] == 100
    assert all(x["profit_sol"] > 0 for x in win)
    # without a current price, holders get no paper profit
    assert [x["wallet"] for x in rank_profitable(ranked, None)] == ["big"]


def test_coin_text_labels_full_address_and_only_n_line():
    swaps = [sw("A" * 44, "BUY", 100, 0.75, 10), sw("A" * 44, "SELL", 15, 1.0, 30)]
    ranked, flagged, small = aggregate_traders(swaps)
    out = texts.coin_scan_text(res_for(ranked, flagged, small, price=0.02))
    assert "<code>" + "A" * 44 + "</code>" in out
    assert "1. Profit: <b>+" in out and "Put in: 0.75 SOL · Still holding: 85%" in out
    assert "Bought: #1st buyer" in out and "Only 1 wallet made real profit" in out
    assert " in 0." not in out and "entry #" not in out


def test_texts_show_hidden_count_and_flag_reasons():
    swaps = [sw("bundle", "BUY", 1, 0.001, 20), sw("bundle", "SELL", 1, 0.14, 30), sw("real", "BUY", 100, 0.5, 21)]
    ranked, flagged, small = aggregate_traders(swaps)
    res = res_for(ranked, flagged, small)
    out = texts.coin_scan_text(res)
    assert "🚩 Hid 1 bundle/transfer wallet" in out and "No wallet made real profit" in out
    fl = texts.flagged_text(res)
    assert "no real buy" in fl and "<code>bundle</code>" in fl and "Got back: 0.14 SOL" in fl


def test_coin_text_is_valid_telegram_html():
    import re
    swaps = [sw("bundle", "BUY", 1, 0.001, 20), sw("bundle", "SELL", 1, 0.14, 30), sw("t", "BUY", 1, 0.02, 21),
             sw("w", "BUY", 10, 1.0, 22), sw("w", "SELL", 10, 3.0, 40)]
    ranked, flagged, small = aggregate_traders(swaps)
    res = res_for(ranked, flagged, small)
    res["sym"] = "<b>x"
    res["launch_checked"] = False
    allowed = {"b", "/b", "i", "/i", "code", "/code"}
    for out in (texts.coin_scan_text(res), texts.flagged_text(res)):
        tags = re.findall(r"<([^>]*)>", out)
        assert all(t in allowed for t in tags), tags


def test_ordinals():
    assert [texts.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 101, 111, 116)] == \
        ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "101st", "111th", "116th"]


def test_split_message_respects_telegram_limit():
    long = "\n".join(f"{i}. <code>{'B' * 44}</code>" for i in range(400))
    parts = texts.split_message(long)
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)
    assert "\n".join(parts) == long
    assert all(p.count("<code>") == p.count("</code>") for p in parts)
    assert texts.split_message("short") == ["short"]


def test_flagged_list_25_wallets_splits_cleanly():
    fl = [{"wallet": "C" * 44, "sol_in": 1.0, "sol_out": 2.0, "reasons": ["same-block bundle", "got tokens by transfer"]}] * 25
    out = texts.flagged_text({"sym": "X", "flagged": fl, "flagged_count": 81})
    assert all(len(p) <= 4000 for p in texts.split_message(out))


def test_onchain_balance_overrides_sampled_holding():
    from mimic.coinscan import apply_balance, rank_profitable
    ranked, _, _ = aggregate_traders([sw("h", "BUY", 100, 1.0, 10)])
    x = ranked[0]
    apply_balance(x, 10.0)                 # really holds only 10 of the 100 we think
    assert x["held_tokens"] == 10 and x["holding_pct"] == 10 and abs(x["held_cost_sol"] - 0.1) < 1e-9
    win = rank_profitable(ranked, price_sol=0.05)   # 10 tokens worth 0.5 SOL vs cost 0.1 -> +0.4
    assert win and abs(win[0]["profit_sol"] - 0.4) < 1e-9
    ranked2, _, _ = aggregate_traders([sw("h", "BUY", 100, 1.0, 10)])
    apply_balance(ranked2[0], None)        # RPC failed: keep estimate
    assert ranked2[0]["held_tokens"] == 100


def test_unverified_holdings_count_sells_only_when_required():
    from mimic.coinscan import apply_balance, rank_profitable
    ranked, _, _ = aggregate_traders([sw("a", "BUY", 100, 1.0, 10), sw("b", "BUY", 100, 1.0, 11)])
    apply_balance(ranked[0], 100.0)        # "a" verified on-chain, "b" not checked
    win = rank_profitable(ranked, price_sol=0.05, require_verified=True)
    assert [x["wallet"] for x in win] == ["a"]
