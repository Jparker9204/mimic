import pytest
from mimic import texts
from mimic.db import DB
from mimic.filters import PRESETS, Filter, apply_filter, describe


def cand(w, sol_in, profit, rank, holding=0, verified_unreal=0.0):
    """A cached candidate whose realized profit is `profit` (plus optional verified paper profit)."""
    held = 100.0 if verified_unreal else 0.0
    return {"wallet": w, "sol_in": sol_in, "sol_out": sol_in + profit, "realized_sol": profit,
            "held_tokens": held, "held_cost_sol": 0.0, "holding_pct": holding, "entry_rank": rank,
            "balance_ok": bool(verified_unreal), "sells": 1, "buys": 1, "tok_in": 100}


C = [cand("whale1", 25.0, 12.0, 40), cand("whale2", 12.0, 4.0, 3),       # whale2 profit < 5
     cand("mid1", 3.0, 2.0, 10), cand("mid2", 8.0, 0.5, 5),              # mid2 < 1 SOL
     cand("small1", 0.5, 0.4, 100), cand("small2", 0.9, 0.05, 2),        # small2 < 0.1 SOL
     cand("loser", 2.0, -1.0, 1), cand("lowpct", 30.0, 2.0, 7),          # lowpct: 2/30 = 6.7% < 10%
     cand("holder", 1.5, 0.0, 60, holding=50, verified_unreal=0.02)]     # 100 tokens * 0.02 = +2 SOL paper


def names(rows):
    return [r["wallet"] for r in rows]


def test_presets():
    assert names(apply_filter(C, PRESETS["whale"], 0.02)[0]) == ["whale1"]
    assert names(apply_filter(C, PRESETS["mid"], 0.02)[0]) == ["mid1", "holder"]
    assert names(apply_filter(C, PRESETS["small"], 0.02)[0]) == ["small1"]
    rows, total = apply_filter(C, PRESETS["any"], 0.02)
    assert names(rows) == ["whale1", "whale2", "mid1", "holder", "small1"] and total == 5   # mid2 is only 6%
    assert "loser" not in names(rows) and "lowpct" not in names(rows)


def test_custom_values_holding_rank_and_sort():
    f = Filter().set("max_rank", 50).set("min_pct", 50)
    assert f.preset == "custom"
    assert names(apply_filter(C, f, 0.02)[0]) == ["mid1"]      # whale1 48% < 50%, small1 is buyer #100
    assert names(apply_filter(C, Filter().set("holding", "holding"), 0.02)[0]) == ["holder"]
    assert "holder" not in names(apply_filter(C, Filter().set("holding", "sold"), 0.02)[0])
    assert names(apply_filter(C, Filter().set("sort", "early"), 0.02)[0])[:2] == ["whale2", "mid1"]
    assert names(apply_filter(C, Filter().set("sort", "pct"), 0.02)[0])[0] == "holder"      # +133%
    # never losers, even with min profit 0 and min % 0
    assert "loser" not in names(apply_filter(C, Filter().set("min_profit", 0).set("min_pct", 0), 0.02)[0])
    # unknown price: paper profit ignored
    assert "holder" not in names(apply_filter(C, Filter(), None)[0])


def test_set_validation():
    f = Filter().set("min_in", 0.001)
    assert f.min_in == 0.05                       # floor: the scan keeps nothing smaller
    assert Filter().set("max_in", 0).max_in is None and Filter().set("max_rank", 0).max_rank is None
    assert Filter().set("max_rank", 50.7).max_rank == 50


def test_describe_lines():
    assert describe(PRESETS["whale"]) == "Filter: 🐳 Whale · put in ≥10 SOL · profit ≥5 SOL"
    assert describe(PRESETS["small"]) == "Filter: 🐟 Small · put in 0.05–1 SOL · profit ≥0.1 SOL"
    d = describe(Filter().set("max_rank", 50).set("holding", "holding").set("sort", "early").set("min_pct", 30))
    assert d == ("Filter: ⚙️ Custom · put in ≥0.05 SOL · profit ≥0.1 SOL · profit ≥30% · first 50 buyers"
                 " · still holding · sorted by earliest buyer")


def test_filter_saved_per_user():
    db = DB(":memory:")
    db.set_setting(1, "coin_filter", PRESETS["whale"].to_dict())
    assert Filter.from_dict(db.get_setting(1, "coin_filter")) == PRESETS["whale"]
    assert db.get_setting(2, "coin_filter") is None
    assert Filter.from_dict(None) == PRESETS["any"]
    assert Filter.from_dict({"preset": "mid", "bogus": 1}).preset == "mid"


def res(cands):
    return {"mint": "M" * 44, "sym": "WEFT", "sigs": 100, "txs": 100, "sampled": False, "price_sol": 0.02,
            "candidates": cands, "flagged": [], "flagged_count": 0, "small": 0}


def test_text_has_filter_line_and_match_counts():
    out = texts.coin_scan_text(res(C), PRESETS["whale"])
    assert "Filter: 🐳 Whale · put in ≥10 SOL · profit ≥5 SOL" in out
    assert "Only 1 wallet matches this filter." in out
    assert "No wallets match this filter" in texts.coin_scan_text(res([C[6]]), PRESETS["whale"])


async def test_refilter_uses_cache_only_no_api_no_check(monkeypatch):
    from mimic.bot import MimicBot
    b = MimicBot(DB(":memory:"))

    async def boom(*a, **k):
        raise AssertionError("no API calls allowed when re-filtering")
    monkeypatch.setattr(b.net, "request", boom)
    r = res(C)
    b.db.cache_set("coin4:" + r["mint"], r, 600)
    b.db.set_setting(7, "coin_filter", PRESETS["mid"].to_dict())
    text, kb = b.coin_view(b.db.cache_get("coin4:" + r["mint"]), b.get_filter(7))
    assert "🐬 Mid" in text and "mid1" in text and "whale1" not in text
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "cflt:" + r["mint"] in datas and all(len(d) <= 64 for d in datas)
    assert b.db.count_since(7, "coin", 0) == 0                 # no check logged


def test_filters_screen_buttons():
    from mimic.bot import MimicBot
    b = MimicBot(DB(":memory:"))
    t, kb = b.filters_screen(1, {})
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    for d in ("fp:small", "fp:mid", "fp:whale", "fp:any", "fs:min_in", "fs:max_in", "fs:min_profit",
              "fs:min_pct", "fs:max_rank", "fc:holding", "fc:sort"):
        assert d in datas
    assert "fa" not in datas                                   # no cached coin to apply to
    assert "Filter: Any" in t


def test_pick_for_verification_covers_every_preset():
    from mimic.coinscan import pick_for_verification
    cs = [dict(cand(f"w{i}", sol, 0.0, i), held_tokens=100.0, balance_ok=False)
          for i, sol in enumerate([0.1] * 30 + [5.0] * 30 + [50.0] * 30)]
    picked = pick_for_verification(cs, 0.05)
    sizes = {x["sol_in"] for x in picked}
    assert {0.1, 5.0, 50.0} <= sizes and len(picked) <= 40
    assert pick_for_verification(cs, None) == []


def test_buyer_rank_wording_when_launch_not_visible():
    r = res(C)
    assert "Bought: #40th buyer\n" in texts.coin_scan_text(r, PRESETS["whale"])
    r["launch_checked"] = False
    assert "Bought: #40th buyer in our sample" in texts.coin_scan_text(r, PRESETS["whale"])
