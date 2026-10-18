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


def test_texts_show_hidden_count_and_flag_reasons():
    swaps = [sw("bundle", "BUY", 1, 0.001, 20), sw("bundle", "SELL", 1, 0.14, 30), sw("real", "BUY", 100, 0.5, 21)]
    ranked, flagged, small = aggregate_traders(swaps)
    res = {"mint": M, "sym": "WEFT", "sigs": 10, "txs": 10, "sampled": False, "traders": ranked,
           "flagged": flagged, "flagged_count": len(flagged), "small": small, "launch_checked": True}
    out = texts.coin_scan_text(res)
    assert "🚩 Hid 1 bundle/transfer wallet" in out and "bund" not in out.split("🚩")[0]
    assert "no real buy" in texts.flagged_text(res)


def test_coin_text_is_valid_telegram_html():
    import re
    swaps = [sw("bundle", "BUY", 1, 0.001, 20), sw("bundle", "SELL", 1, 0.14, 30), sw("t", "BUY", 1, 0.02, 21)]
    ranked, flagged, small = aggregate_traders(swaps)
    res = {"mint": M, "sym": "<b>x", "sigs": 10, "txs": 10, "sampled": False, "traders": ranked,
           "flagged": flagged, "flagged_count": len(flagged), "small": small, "launch_checked": False}
    allowed = {"b", "/b", "i", "/i", "code", "/code"}
    for out in (texts.coin_scan_text(res), texts.flagged_text(res)):
        tags = re.findall(r"<([^>]*)>", out)
        assert all(t in allowed for t in tags), tags
