import asyncio
import pytest
from mimic.db import DB
from mimic.jobqueue import JobQueue
from mimic.limits import Limits, day_start_utc

DAY = 1_800_000_000 - (1_800_000_000 % 86400)   # a UTC midnight


@pytest.fixture
def lim():
    db = DB(":memory:")
    return db, Limits(db, limits={"wallet": 5, "coin": 3, "sim": 2}, cooldown_s=25, admins={999})


def run_ok(db, uid, kind, t):
    cid = db.start_check(uid, kind, "x", now=t)
    db.finish_check(cid, True, now=t + 1)
    return cid


def test_daily_limit(lim):
    db, L = lim
    t = DAY + 3600
    for i in range(3):
        assert L.check(1, "coin", now=t + i * 30).ok
        run_ok(db, 1, "coin", t + i * 30)
    v = L.check(1, "coin", now=t + 200)
    assert not v.ok and v.reason == "limit" and v.used == 3
    # wallet scans are a separate bucket
    assert L.check(1, "wallet", now=t + 200).ok
    # next UTC day resets
    assert L.check(1, "coin", now=DAY + 86400 + 5).ok


def test_failed_jobs_do_not_count(lim):
    db, L = lim
    t = DAY + 100
    for i in range(5):
        cid = db.start_check(1, "sim", "x", now=t + i * 30)
        db.finish_check(cid, False, now=t + i * 30 + 1)
    assert L.check(1, "sim", now=t + 500).ok
    assert db.count_since(1, "sim", day_start_utc(t)) == 0


def test_cooldown_countdown(lim):
    db, L = lim
    t = DAY + 1000
    run_ok(db, 1, "wallet", t)
    v = L.check(1, "coin", now=t + 10)          # cooldown applies across kinds
    assert not v.ok and v.reason == "cooldown" and v.wait_s == 15
    assert L.check(1, "coin", now=t + 25).ok
    # a failed check does not start a cooldown
    cid = db.start_check(1, "coin", "x", now=t + 30)
    db.finish_check(cid, False)
    assert L.check(1, "coin", now=t + 31).ok


def test_one_running_job_per_user_and_admin_bypass(lim):
    db, L = lim
    t = DAY + 5000
    db.start_check(1, "wallet", "x", now=t)
    assert L.check(1, "coin", now=t + 100).reason == "busy"
    for i in range(10):
        run_ok(db, 999, "sim", t + i)
    assert L.check(999, "sim", now=t + 11).ok


def test_stale_running_marked_failed(lim):
    db, L = lim
    db.start_check(1, "wallet", "x", now=DAY + 10)
    db.fail_stale_running()
    assert db.count_since(1, "wallet", DAY) == 0


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
