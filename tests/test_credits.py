import importlib
import pytest
from mimic import texts
from mimic.credits import Balance, Credits, InsufficientCredits, refill_math
from mimic.db import DB

T0 = 1_800_000_000.0
H = 3600
P = 6 * H


@pytest.fixture
def cr():
    return Credits(DB(":memory:"), refill=40, interval_h=6, cap=150)


# ---------------------------------------------------------------- refill math
def test_refill_partial_periods():
    assert refill_math(0, T0, T0 + P - 1, 40, P, 150) == (0, T0, 0)          # not yet
    assert refill_math(0, T0, T0 + P, 40, P, 150) == (40, T0 + P, 40)        # exactly one
    # 2.5 periods: two refills, clock keeps the half period of progress
    assert refill_math(10, T0, T0 + 2.5 * P, 40, P, 150) == (90, T0 + 2 * P, 80)


def test_refill_cap_and_inactive_users():
    # a year away still ends at the cap, never beyond
    assert refill_math(40, T0, T0 + 365 * 86400, 40, P, 150)[0] == 150
    assert refill_math(130, T0, T0 + P, 40, P, 150) == (150, T0 + P, 20)
    # at/above the cap: clock moves, nothing added, nothing taken away
    assert refill_math(150, T0, T0 + 3 * P, 40, P, 150) == (150, T0 + 3 * P, 0)
    assert refill_math(160, T0, T0 + P, 40, P, 150) == (160, T0 + P, 0)
    # clock going backwards never breaks anything
    assert refill_math(10, T0, T0 - 100, 40, P, 150) == (10, T0, 0)


def test_new_account_starts_with_40_and_refills_lazily(cr):
    b = cr.balance(1, now=T0)
    assert (b.free, b.bought) == (40, 0)
    assert cr.next_refill_in(b, now=T0 + H) == 5 * H
    b = cr.balance(1, now=T0 + 13 * H)               # 2 full periods + 1h
    assert b.free == 120 and cr.next_refill_in(b, now=T0 + 13 * H) == 5 * H
    b = cr.balance(1, now=T0 + 1000 * P)
    assert b.free == 150 and cr.next_refill_in(b, now=T0 + 1000 * P) is None
    kinds = [r["kind"] for r in cr.ledger(1)]
    assert kinds == ["refill", "refill", "signup"]


def test_spend_free_first_then_bought(cr):
    cr.grant(1, 25, actor=999, note="test", now=T0)
    assert cr.spend(1, 30, check_id=1, now=T0) == (30, 0)
    assert cr.spend(1, 15, check_id=2, now=T0) == (10, 5)
    b = cr.balance(1, now=T0)
    assert (b.free, b.bought) == (0, 20)
    with pytest.raises(InsufficientCredits):
        cr.spend(1, 21, check_id=3, now=T0)
    b = cr.balance(1, now=T0)
    assert (b.free, b.bought) == (0, 20)              # failed spend changes nothing


def test_refund_goes_back_to_same_buckets_once(cr):
    cr.grant(1, 10, actor=999, now=T0)
    cr.spend(1, 45, check_id=7, now=T0)               # 40 free + 5 bought
    assert cr.refund(7, now=T0 + 60, note="busy") == 45
    assert cr.refund(7, now=T0 + 61) == 0             # idempotent
    assert cr.refund(12345, now=T0) == 0              # nothing spent (admin) → nothing refunded
    b = cr.balance(1, now=T0 + 62)
    assert (b.free, b.bought) == (40, 10)
    r = cr.ledger(1)[0]
    assert r["kind"] == "refund" and r["check_id"] == 7 and (r["delta_free"], r["delta_bought"]) == (40, 5)


def test_purchased_credits_no_cap_and_grant_rules(cr):
    b = cr.grant(1, 10_000, actor=999, now=T0)
    assert b.bought == 10_000 and b.total == 10_040
    b = cr.balance(1, now=T0 + 100 * P)
    assert (b.free, b.bought) == (150, 10_000)       # cap is free credits only; bought never expire
    b = cr.grant(1, -4_000, actor=999, note="refund dispute", now=T0 + 100 * P)
    assert b.bought == 6_000
    with pytest.raises(InsufficientCredits):
        cr.grant(1, -7_000, actor=999, now=T0 + 100 * P)
    g = [r for r in cr.ledger(1, 50) if r["kind"] == "grant"]
    assert [r["delta_bought"] for r in g] == [-4000, 10000] and all(r["actor"] == 999 for r in g)


def test_ledger_records_every_change(cr):
    cr.balance(1, now=T0)
    cr.spend(1, 10, check_id=1, now=T0)
    cr.refund(1, now=T0)
    cr.grant(1, 5, actor=9, now=T0)
    cr.balance(1, now=T0 + P)
    rows = cr.ledger(1, 50)
    assert [r["kind"] for r in rows] == ["refill", "grant", "refund", "spend", "signup"]
    last = rows[0]
    assert (last["free_after"], last["bought_after"]) == (80, 5)


def test_migration_existing_users_get_40_once():
    db = DB(":memory:")
    cid = db.start_check(5, "wallet", "x", now=T0 - 86400)       # used the bot before credits
    db.finish_check(cid, True, now=T0 - 86400)
    db.set_setting(6, "coin_filter", {"preset": "any"})           # only saved a filter
    cr = Credits(db, refill=40, interval_h=6, cap=150)
    for uid in (5, 6):
        b = cr.balance(uid, now=T0)
        assert (b.free, b.bought) == (40, 0)
        assert cr.ledger(uid)[0]["kind"] == "migrate"
        assert cr.balance(uid, now=T0 + 10).free == 40              # no double grant
    assert cr.ledger(7) == [] and cr.balance(7, now=T0).free == 40 and cr.ledger(7)[0]["kind"] == "signup"


def test_migration_on_old_database_file(tmp_path):
    """A DB created by the old code (no credit tables) upgrades in place."""
    import sqlite3
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE checks (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
                    "kind TEXT NOT NULL, target TEXT, status TEXT NOT NULL, created_at REAL NOT NULL, "
                    "finished_at REAL, note TEXT);"
                    "INSERT INTO checks(user_id,kind,target,status,created_at) VALUES(42,'coin','m','ok',1);"
                    "INSERT INTO checks(user_id,kind,target,status,created_at) VALUES(43,'coin','m','running',2);")
    c.commit()
    c.close()
    db = DB(path)
    assert db.fail_stale_running() == [2]
    cr = Credits(db, refill=40, interval_h=6, cap=150)
    assert cr.refund(2) == 0                                       # old checks never charged
    assert cr.balance(42, now=T0).free == 40 and cr.ledger(42)[0]["kind"] == "migrate"


def test_time_until_enough(cr):
    b = Balance(5, 0, T0)
    assert cr.time_until(b, 15, now=T0 + H) == 5 * H                 # one refill
    assert cr.time_until(b, 100, now=T0 + H) == 3 * P - H            # 5 + 3*40 >= 100
    assert cr.time_until(b, 3, now=T0) == 0
    assert cr.time_until(b, 200, now=T0) is None                    # beyond the free cap


def test_balance_line_and_texts(cr):
    b = Balance(80, 40, T0)
    line = texts.balance_line(b, cr, now=T0 + 2 * H + 48 * 60)
    assert "Credits: <b>120</b> (free 80/150, bought 40). Next +40 in 3h 12m" in line
    assert "full" in texts.balance_line(Balance(150, 0, T0), cr, now=T0)
    assert "unlimited" in texts.balance_line(None, cr, admin=True)
    msg = texts.no_credits_text("sim", 15, Balance(5, 0, T0), cr, 5 * H, now=T0 + H)
    assert "costs <b>15</b>" in msg and "Next +40 in 5h 0m" in msg
    assert "100 for 0.05 SOL, 250 for 0.1 SOL" in msg and "coming soon" in msg
    assert "Coin Scan: 10 credits" in texts.help_text()


def test_env_overrides(monkeypatch):
    from mimic import config
    env = {"MIMIC_COST_COIN": "12", "MIMIC_COST_WALLET": "0", "MIMIC_COST_SIM": "20", "MIMIC_FREE_REFILL": "25",
           "MIMIC_REFILL_HOURS": "4", "MIMIC_FREE_CAP": "300", "MIMIC_PACKS": "200:0.08, 1000:0.3"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    try:
        importlib.reload(config)
        assert config.COSTS == {"coin": 12, "wallet": 0, "sim": 20}
        assert (config.FREE_REFILL, config.REFILL_HOURS, config.FREE_CAP) == (25, 4.0, 300)
        assert config.PACKS == [(200, 0.08), (1000, 0.3)]
        assert "200 for 0.08 SOL, 1000 for 0.3 SOL" in texts.packs_note()
        assert texts.cost_label("wallet") == "free"
        c = Credits(DB(":memory:"))
        assert (c.refill, c.interval_s, c.cap) == (25, 4 * H, 300)
        monkeypatch.setenv("MIMIC_PACKS", "garbage")
        importlib.reload(config)
        assert config.PACKS == [(100, 0.05), (250, 0.1)]
    finally:
        for k in env:
            monkeypatch.delenv(k, raising=False)
        importlib.reload(config)
    assert config.COSTS == {"coin": 10, "wallet": 3, "sim": 15}
    assert (config.FREE_REFILL, config.REFILL_HOURS, config.FREE_CAP) == (40, 6.0, 150)
