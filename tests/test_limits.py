import asyncio
import pytest
from mimic.credits import Credits
from mimic.db import DB
from mimic.jobqueue import JobQueue
from mimic.limits import Limits

T0 = 1_800_000_000.0
COSTS = {"wallet": 3, "coin": 10, "sim": 15}


@pytest.fixture
def lim():
    db = DB(":memory:")
    cr = Credits(db, refill=40, interval_h=6, cap=500)
    return db, Limits(db, credits=cr, costs=COSTS, cooldown_s=25, admins={999})


def run(db, L, uid, kind, t, ok=True):
    """Start a check the way the bot does: check → start → charge → finish (refund on failure)."""
    v = L.check(uid, kind, now=t)
    assert v.ok, v
    cid = db.start_check(uid, kind, "x", now=t)
    L.charge(uid, kind, cid, now=t)
    db.finish_check(cid, ok, now=t + 1)
    if not ok:
        L.refund(cid, now=t + 1)
    return cid


def test_no_daily_limit_only_credits(lim):
    db, L = lim
    # 40 starting credits = 13 wallet scans (39), no per-feature daily cap any more
    for i in range(13):
        run(db, L, 1, "wallet", T0 + i * 30)
    v = L.check(1, "wallet", now=T0 + 13 * 30)
    assert not v.ok and v.reason == "credits" and v.balance.total == 1 and v.cost == 3


def test_insufficient_credits_message_data(lim):
    db, L = lim
    run(db, L, 1, "coin", T0)            # 30 left
    run(db, L, 1, "coin", T0 + 30)       # 20 left
    run(db, L, 1, "sim", T0 + 60)        # 5 left
    v = L.check(1, "sim", now=T0 + 100)
    assert v.reason == "credits" and v.balance.total == 5
    assert v.wait_s == 6 * 3600 - 100            # next refill
    assert v.enough_in == 6 * 3600 - 100         # one refill (+40) covers 15
    assert L.check(1, "wallet", now=T0 + 100).ok  # cheaper action still fine


def test_cache_hit_still_costs(lim):
    # Credits are taken per check regardless of what the work does (cache or not).
    db, L = lim
    run(db, L, 1, "wallet", T0)
    run(db, L, 1, "wallet", T0 + 30)
    assert L.credits.balance(1, now=T0 + 60).total == 34


def test_failed_checks_refunded_and_no_cooldown(lim):
    db, L = lim
    for i in range(5):
        run(db, L, 1, "sim", T0 + i, ok=False)
    b = L.credits.balance(1, now=T0 + 10)
    assert b.total == 40
    assert L.check(1, "sim", now=T0 + 10).ok      # failed checks don't start a cooldown


def test_cooldown_countdown(lim):
    db, L = lim
    run(db, L, 1, "wallet", T0)
    v = L.check(1, "coin", now=T0 + 10)            # cooldown applies across kinds
    assert not v.ok and v.reason == "cooldown" and v.wait_s == 15
    assert L.check(1, "coin", now=T0 + 25).ok


def test_one_running_job_per_user(lim):
    db, L = lim
    cid = db.start_check(1, "wallet", "x", now=T0)
    L.charge(1, "wallet", cid, now=T0)
    assert L.check(1, "coin", now=T0 + 100).reason == "busy"


def test_admin_unlimited_free_no_cooldown(lim):
    db, L = lim
    for i in range(50):
        cid = db.start_check(999, "coin", "x", now=T0 + i)
        assert L.charge(999, "coin", cid, now=T0 + i) == 0
        db.finish_check(cid, True, now=T0 + i)
        assert L.check(999, "sim", now=T0 + i + 0.5).ok
    assert L.credits.ledger(999) == [] or all(r["kind"] != "spend" for r in L.credits.ledger(999))


def test_stale_running_refunded_on_restart(lim):
    db, L = lim
    cid = db.start_check(1, "coin", "x", now=T0)
    L.charge(1, "coin", cid, now=T0)
    assert L.credits.balance(1, now=T0).total == 30
    for stale in db.fail_stale_running():
        L.refund(stale, note="interrupted by restart", now=T0 + 5)
    assert L.credits.balance(1, now=T0 + 5).total == 40
    assert db.running_for(1) == 0


async def test_queue_positions_and_concurrency():
    q = JobQueue(2)
    running, peak, positions = 0, 0, []
    gate = asyncio.Event()

    async def job():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await gate.wait()
        running -= 1

    async def pos(p):
        positions.append(p)

    tasks = [asyncio.create_task(q.run(job, on_position=pos)) for _ in range(4)]
    await asyncio.sleep(0.05)
    assert running == 2 and sorted(positions) == [1, 2]
    gate.set()
    await asyncio.gather(*tasks)
    assert peak == 2 and q.active == 0
