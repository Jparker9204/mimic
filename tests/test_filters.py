from mimic.solana import WSOL, is_address, parse_swap, reduce_tx, short

W = "4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T"   # arbitrary valid-looking address
MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def tx(pre_tok, post_tok, pre_sol, post_sol, err=None, loaded=None):
    meta = {"err": err, "preBalances": pre_sol, "postBalances": post_sol,
            "preTokenBalances": pre_tok, "postTokenBalances": post_tok}
    if loaded:
        meta["loadedAddresses"] = loaded
    return {"blockTime": 1, "meta": meta, "transaction": {"message": {"accountKeys": [W, "Other111111111111111111111111111111111111111"]}}}


def tb(mint, owner, amt):
    return {"mint": mint, "owner": owner, "uiTokenAmount": {"uiAmount": amt}}


def test_address_validation():
    assert is_address(W) and is_address(MINT)
    assert not is_address("hello")
    assert not is_address("0OIl" * 10)          # not base58
    assert not is_address(W[:20])
    assert short(MINT) == "DezX…B263"


def test_buy_and_sell():
    buy = reduce_tx(tx([], [tb(MINT, W, 1000)], [5_000_000_000, 0], [4_000_000_000, 0]))
    assert parse_swap(buy, W) == ("BUY", MINT, 1000, 1.0)
    sell = reduce_tx(tx([tb(MINT, W, 1000)], [tb(MINT, W, 0)], [1_000_000_000, 0], [3_000_000_000, 0]))
    assert parse_swap(sell, W) == ("SELL", MINT, 1000, 2.0)


def test_wsol_counts_as_sol_and_stables_ignored():
    red = reduce_tx(tx([tb(WSOL, W, 2.0)], [tb(WSOL, W, 0.5), tb(MINT, W, 50), tb(USDC, W, 0)],
                       [10_000_000_000, 0], [10_000_000_000, 0]))
    side, mint, q, s = parse_swap(red, W)
    assert (side, mint, q) == ("BUY", MINT, 50) and abs(s - 1.5) < 1e-9


def test_filters_reject_noise():
    # failed tx
    assert parse_swap(reduce_tx(tx([], [tb(MINT, W, 5)], [2e9, 0], [1e9, 0], err={"x": 1})), W) is None
    # two different tokens moved -> not a simple swap
    two = reduce_tx(tx([], [tb(MINT, W, 5), tb("So1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1", W, 3)], [2e9, 0], [1e9, 0]))
    assert parse_swap(two, W) is None
    # token received for free (airdrop) -> not a buy
    assert parse_swap(reduce_tx(tx([], [tb(MINT, W, 5)], [2e9, 0], [2e9, 0])), W) is None
    # dust SOL change
    assert parse_swap(reduce_tx(tx([], [tb(MINT, W, 5)], [2e9, 0], [2e9 - 100_000, 0])), W) is None


def test_loaded_addresses_for_versioned_tx():
    t = tx([], [tb(MINT, "Loaded1111111111111111111111111111111111111", 7)], [0, 0, 9e9], [0, 0, 8e9],
           loaded={"writable": ["Loaded1111111111111111111111111111111111111"], "readonly": []})
    red = reduce_tx(t)
    assert parse_swap(red, "Loaded1111111111111111111111111111111111111")[0] == "BUY"


def test_rpc_requests_versioned_transactions():
    import mimic.solana as s
    assert s.MAX_TX_VERSION >= 1
