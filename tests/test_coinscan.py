import asyncio
import re

import pytest

from mimic import texts
from mimic.coinscan import (classify, find_clusters, flag_wallets, merge_candidates, quote_values, wallet_pnl)
from mimic.solana import STABLES, TOKEN_2022, WSOL, DataBusy, Rpc, ata_address, reduce_tx

MINT = "M" * 44
WBTC = "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"
USDC = next(iter(STABLES))


def red(wallet, tok_delta, sol_delta, other=None, err=False):
    d = {MINT: tok_delta}
    d.update(other or {})
    return {"t": 100, "err": err, "payer": wallet, "tok": {wallet: d}, "sol": {wallet: sol_delta}}


def ev(kind, q, s, t=0, slot=0):
    return (t, slot, (kind, q, s))


# ---------------------------------------------------------------- classify
def test_classify_sol_buy_sell_and_transfers():
    assert classify(red("w", 100, -1.0), "w", MINT, {}) == ("BUY", 100, 1.0)
    assert classify(red("w", -100, 2.0), "w", MINT, {}) == ("SELL", 100, 2.0)
    assert classify(red("w", 100, -0.000005), "w", MINT, {}) == ("XFER_IN", 100, 0.0)
    assert classify(red("w", -100, -0.000005), "w", MINT, {}) == ("XFER_OUT", 100, 0.0)
    assert classify(red("w", 0, -1.0), "w", MINT, {}) is None          # didn't touch this coin
    assert classify(red("w", 100, -1.0, err=True), "w", MINT, {}) is None


def test_classify_wsol_and_non_sol_quotes():
    # paid with wrapped SOL
    assert classify(red("w", 100, -0.002, {WSOL: -1.0}), "w", MINT, {}) == ("BUY", 100, pytest.approx(1.002))
    # ETCHY-style WBTC pair: 0.001 WBTC worth 0.5 SOL each unit -> 500 SOL/unit
    vals = {WBTC: 500.0}
    k, q, s = classify(red("w", -100, -0.00001, {WBTC: 0.004}), "w", MINT, vals)
    assert k == "SELL" and s == pytest.approx(2.0, abs=1e-3)
    # USDC buy priced at 1/150 SOL per dollar
    k, q, s = classify(red("w", 100, -0.00001, {USDC: -30.0}), "w", MINT, {USDC: 1 / 150})
    assert k == "BUY" and s == pytest.approx(0.2, abs=1e-3)
    # a token we can't price -> UNKNOWN (never guessed)
    assert classify(red("w", 100, 0.0, {"X" * 44: -5}), "w", MINT, {})[0] == "UNKNOWN"


def test_quote_values_from_pairs():
    pairs = [{"quoteToken": {"address": WBTC}, "priceUsd": "0.01", "priceNative": "0.0000001"},
             {"quoteToken": {"address": WSOL}, "priceUsd": "0.01", "priceNative": "0.0000666"}]
    v = quote_values(pairs, 150.0)
    assert v[WBTC] == pytest.approx(100000 / 150)       # WBTC = $100k
    assert WSOL not in v and v[USDC] == pytest.approx(1 / 150)


# ---------------------------------------------------------------- pnl
def test_wallet_pnl_realized_and_holding_from_chain_balance():
    x = wallet_pnl("w", [ev("BUY", 100, 1.0, 10, 1), ev("SELL", 50, 2.0, 20, 2)], balance=50)
    assert x["realized_sol"] == pytest.approx(1.5)          # sold half (cost 0.5) for 2.0
    assert x["held_tokens"] == 50 and x["holding_pct"] == 50 and x["held_cost_sol"] == pytest.approx(0.5)
    assert x["first_t"] == 10 and x["balance_ok"]
    # chain says they hold less (moved tokens elsewhere): never count more than the chain shows
    y = wallet_pnl("w", [ev("BUY", 100, 1.0)], balance=10)
    assert y["held_tokens"] == 10 and y["holding_pct"] == 10


def test_selling_transferred_tokens_does_not_inflate_profit():
    x = wallet_pnl("w", [ev("BUY", 10, 0.1, 1, 1), ev("XFER_IN", 990, 0, 2, 2), ev("SELL", 1000, 10.0, 3, 3)], 0)
    assert x["realized_sol"] == pytest.approx(10.0 * 10 / 1000 - 0.1)    # only the 10 bought tokens count


def test_flags_launch_cluster_transfer_no_buy():
    rows = [wallet_pnl("dust", [ev("BUY", 1, 0.001, 50, 5), ev("SELL", 1, 0.2, 60, 6)], 0),
            wallet_pnl("gift", [ev("BUY", 10, 0.1, 50, 7), ev("XFER_IN", 100, 0, 51, 8)], 110),
            wallet_pnl("sniper", [ev("BUY", 10, 1.0, 1001, 9)], 10),
            wallet_pnl("real", [ev("BUY", 10, 1.0, 5000, 99)], 10)]
    rows += [wallet_pnl(f"b{i}", [ev("BUY", 10, 1.0 + i * 0.02, 3000, 42)], 10) for i in range(3)]
    flag_wallets(rows, launch_ts=1000)
    f = {r["wallet"]: r["flags"] for r in rows}
    assert "no_buy" in f["dust"] and "transfer" in f["gift"] and "launch" in f["sniper"]
    assert all("cluster" in f[f"b{i}"] for i in range(3)) and f["real"] == []


def test_cluster_needs_similar_amounts():
    assert find_clusters([(5, 1.0, "a"), (5, 3.0, "b"), (5, 9.0, "c")]) == set()
    assert find_clusters([(5, 1.0, "a"), (5, 1.05, "b"), (5, 1.08, "c")]) == {"a", "b", "c"}


def test_merge_candidates_holders_first_then_volume_and_pools_excluded():
    holders = [("h1", "acc1"), ("pool", "accp"), ("h2", "acc2")]
    trades = [{"wallet": "t1", "usd": 50}, {"wallet": "t2", "usd": 500}, {"wallet": "h1", "usd": 9}]
    out = merge_candidates(holders, trades, cap=4, exclude={"pool"})
    assert out == [("h1", "acc1", "holder"), ("h2", "acc2", "holder"), ("t2", None, "trader"), ("t1", None, "trader")]
    assert len(merge_candidates(holders, trades, cap=2, exclude={"pool"})) == 2


# ---------------------------------------------------------------- chain helpers
def test_ata_address_known_vector():
    # verified on mainnet: PIERRE (Token-2022) account of a real holder
    assert ata_address("8kWY4VmkZ18pzCBtbPjH9fqfdupbovB7D8VCwJ7f326C",
                       "D3S6J9s1byrNBt3ZKwEDiPsNRqHtoEP8zQKdhyBdpump", TOKEN_2022) == \
        "8PSRNa45svwbbTWBFqwbxKPwTgcVVfo5V17p6XLNaM26"


def test_reduce_tx_reads_v0_loaded_addresses():
    tx = {"blockTime": 5, "transaction": {"message": {"accountKeys": ["payer"]}},
          "meta": {"err": None, "preBalances": [10 ** 9, 0], "postBalances": [5 * 10 ** 8, 0],
                   "loadedAddresses": {"writable": ["other"], "readonly": []},
                   "preTokenBalances": [], "postTokenBalances": [
                       {"owner": "payer", "mint": MINT, "uiTokenAmount": {"uiAmount": 100.0}}]}}
    r = reduce_tx(tx)
    assert r["sol"]["payer"] == pytest.approx(-0.5) and r["tok"]["payer"][MINT] == 100.0


class _DB:
    def cache_get(self, k): return None
    def cache_set(self, *a): pass


class _Net:
    def __init__(self, fail): self.fail = fail
    async def request(self, *a, **k):
        if self.fail:
            return None                                   # 429 after retries
        return {"result": {"blockTime": 1, "transaction": {"message": {"accountKeys": ["p"]}},
                           "meta": {"preBalances": [0], "postBalances": [0]}}}


def test_rate_limited_batch_raises_databusy_instead_of_partial_result():
    rpc = Rpc(_Net(fail=True), _DB())
    sigs = [{"signature": str(i), "blockTime": None} for i in range(40)]
    got = asyncio.run(rpc.many_tx(sigs, 10, concurrency=4))
    assert got == []
    with pytest.raises(DataBusy):
        rpc.check_batch()
    ok = Rpc(_Net(fail=False), _DB())
    assert len(asyncio.run(ok.many_tx(sigs, 10, concurrency=4))) == 40
    ok.check_batch()                                       # fine: no exception


def test_mostly_failed_batch_is_busy():
    rpc = Rpc(_Net(False), _DB())
    rpc.last_batch = {"wanted": 400, "attempted": 400, "failed": 390}   # the ETCHY "10 of 37393" case
    with pytest.raises(DataBusy):
        rpc.check_batch()
    rpc.last_batch = {"wanted": 400, "attempted": 400, "failed": 20}
    rpc.check_batch()


# ---------------------------------------------------------------- texts
def res(cands, **kw):
    r = {"mint": MINT, "sym": "ETCHY", "version": 2, "candidates": cands, "price_sol": 0.02, "priced": True,
         "launch_ts": 1000.0, "checked": 37, "wanted": 40, "very_active": 2, "unchecked": 1, "history_failed": 0,
         "holders_found": 18, "holders_failed": False, "unvalued": 0, "small": 0, "flagged": [], "flagged_count": 0}
    r.update(kw)
    return r


def winner(w="A" * 44, sol_in=0.75, realized=0.5, first_t=1000 + 3600 * 3 + 120):
    return {"wallet": w, "buys": 1, "sells": 1, "sol_in": sol_in, "sol_out": sol_in + realized, "realized_sol": realized,
            "held_tokens": 0.0, "held_cost_sol": 0.0, "holding_pct": 0, "entry_rank": 1, "balance_ok": True,
            "tok_in": 100, "first_t": first_t, "source": "holder"}


def test_coin_text_v2_labels_counts_and_honest_notes():
    out = texts.coin_scan_text(res([winner()]))
    assert "<code>" + "A" * 44 + "</code>" in out
    assert "1. Profit: <b>+0.50 SOL (+67%)</b>" in out and "Put in: 0.75 SOL · Still holding: 0%" in out
    assert "Bought: 3h 2m after launch" in out
    assert "Only 1 wallet made real profit" in out
    assert "Checked 37 of 40 wallets" in out
    assert "2 very active" in out and "1 not read in time" in out


def test_no_winners_and_holders_unavailable():
    out = texts.coin_scan_text(res([winner(realized=-0.3)], holders_failed=True, flagged_count=1,
                                   flagged=[{"wallet": "b", "sol_in": 0.001, "sol_out": 0.14,
                                             "reasons": ["no real buy (under 0.01 SOL)"]}]))
    assert "No checked wallet made real profit" in out
    assert "Top-holder list was unavailable" in out and "🚩 Hid 1 bundle/transfer wallet" in out
    fl = texts.flagged_text(res([], flagged=[{"wallet": "b", "sol_in": 0.001, "sol_out": 0.14,
                                               "reasons": ["no real buy (under 0.01 SOL)"]}], flagged_count=1))
    assert "no real buy" in fl and "<code>b</code>" in fl and "Got back: 0.14 SOL" in fl


def test_coin_text_is_valid_telegram_html():
    r = res([winner()], sym="<b>x", launch_ts=None, holders_failed=True, unvalued=3)
    allowed = {"b", "/b", "i", "/i", "code", "/code"}
    for out in (texts.coin_scan_text(r), texts.flagged_text(r)):
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
    assert texts.split_message("short") == ["short"]


def test_flagged_list_25_wallets_splits_cleanly():
    fl = [{"wallet": "C" * 44, "sol_in": 1.0, "sol_out": 2.0, "reasons": ["same-block bundle"]}] * 25
    out = texts.flagged_text({"sym": "X", "flagged": fl, "flagged_count": 81})
    assert all(len(p) <= 4000 for p in texts.split_message(out))


# ---------------------------------------------------------------- bot: busy never counts
async def test_bot_data_busy_not_counted(monkeypatch):
    from mimic.bot import MimicBot
    from mimic.db import DB
    b = MimicBot(DB(":memory:"))
    shown = []

    async def fake_send(chat_id, ctx, text, kb=None):
        shown.append(text)
        return object()

    async def fake_edit(msg, text, kb=None):
        shown.append(text)
    monkeypatch.setattr(b, "_send", fake_send)
    monkeypatch.setattr(b, "_edit", fake_edit)

    async def work(progress):
        raise DataBusy("getTransaction 429")
    await b.run_check(1, 7, None, "coin", MINT, work)
    assert "busy" in shown[-1] and "didn't count" in shown[-1]
    assert b.db.count_since(7, "coin", 0) == 0


def test_bot_busy_source():
    import inspect
    from mimic import bot
    src = inspect.getsource(bot.MimicBot.run_check)
    assert "except DataBusy" in src and "didn't count" in src
    assert src.index("except DataBusy") < src.rindex("except Exception")
