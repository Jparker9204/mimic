"""Credits: free credits (refilled lazily every N hours, capped) + purchased credits (no cap, never expire).

Every change is written to the credit_ledger table (for refund disputes). No payments here:
purchased credits can only be granted by an admin for now."""
from __future__ import annotations
import time
from contextlib import contextmanager
from dataclasses import dataclass
from . import config
from .db import DB


class InsufficientCredits(Exception):
    pass


@dataclass
class Balance:
    free: int
    bought: int
    last_refill: float

    @property
    def total(self) -> int:
        return self.free + self.bought


def refill_math(free: int, last: float, now: float, refill: int, interval_s: float, cap: int) -> tuple[int, float, int]:
    """Apply all complete refill periods since `last`. Returns (new_free, new_last, added).

    Partial periods are kept (the clock advances by whole periods only). Free credits never
    grow past `cap`, however long the user was away; a balance already above cap (from a refund)
    is never reduced."""
    if now <= last or interval_s <= 0:
        return free, last, 0
    n = int((now - last) // interval_s)
    if n <= 0:
        return free, last, 0
    new_last = last + n * interval_s
    if free >= cap or refill <= 0:
        return free, new_last, 0
    new_free = min(cap, free + n * refill)
    return new_free, new_last, new_free - free


class Credits:
    def __init__(self, db: DB, refill: int | None = None, interval_h: float | None = None, cap: int | None = None):
        self.db = db
        self.refill = config.FREE_REFILL if refill is None else refill
        self.interval_s = (config.REFILL_HOURS if interval_h is None else interval_h) * 3600
        self.cap = config.FREE_CAP if cap is None else cap

    # ---------------------------------------------------------------- internals
    @contextmanager
    def _txn(self):
        c = self.db.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")

    def _log(self, c, uid: int, kind: str, d_free: int, d_bought: int, b: Balance, now: float,
             check_id: int | None = None, actor: int | None = None, note: str = "") -> None:
        c.execute("INSERT INTO credit_ledger(user_id,ts,kind,delta_free,delta_bought,free_after,bought_after,"
                  "check_id,actor,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (uid, now, kind, d_free, d_bought, b.free, b.bought, check_id, actor, note[:300]))

    def _save(self, c, uid: int, b: Balance) -> None:
        c.execute("UPDATE credits SET free=?, bought=?, last_refill=? WHERE user_id=?", (b.free, b.bought, b.last_refill, uid))

    def _load(self, c, uid: int, now: float) -> Balance:
        """Create the account if needed (new users and pre-credits users both start with one refill),
        then apply any pending refills. Must run inside _txn."""
        r = c.execute("SELECT free, bought, last_refill FROM credits WHERE user_id=?", (uid,)).fetchone()
        if r is None:
            start = min(self.refill, self.cap)
            b = Balance(start, 0, now)
            c.execute("INSERT INTO credits(user_id,free,bought,last_refill,created_at) VALUES(?,?,?,?,?)",
                      (uid, b.free, 0, now, now))
            kind = "migrate" if self.db.has_history(uid) else "signup"
            self._log(c, uid, kind, start, 0, b, now, note="starting free credits")
            return b
        b = Balance(int(r[0]), int(r[1]), float(r[2]))
        new_free, new_last, added = refill_math(b.free, b.last_refill, now, self.refill, self.interval_s, self.cap)
        if new_last != b.last_refill:
            b = Balance(new_free, b.bought, new_last)
            self._save(c, uid, b)
            if added:
                self._log(c, uid, "refill", added, 0, b, now)
        return b

    # ---------------------------------------------------------------- public
    def balance(self, uid: int, now: float | None = None) -> Balance:
        now = time.time() if now is None else now
        with self._txn() as c:
            return self._load(c, uid, now)

    def next_refill_in(self, b: Balance, now: float | None = None) -> int | None:
        """Seconds until the next +refill, or None when free credits are full."""
        now = time.time() if now is None else now
        if b.free >= self.cap or self.refill <= 0:
            return None
        return max(0, int(b.last_refill + self.interval_s - now + 0.999))

    def time_until(self, b: Balance, need: int, now: float | None = None) -> int | None:
        """Seconds until the balance reaches `need` from refills alone (0 = already), None = never."""
        now = time.time() if now is None else now
        deficit = need - b.total
        if deficit <= 0:
            return 0
        if self.refill <= 0 or b.bought + max(self.cap, b.free) < need:
            return None
        periods = -(-deficit // self.refill)
        return max(0, int(b.last_refill + periods * self.interval_s - now + 0.999))

    def spend(self, uid: int, amount: int, check_id: int | None = None, now: float | None = None,
              note: str = "") -> tuple[int, int]:
        """Spend free credits first, then purchased. Returns (from_free, from_bought)."""
        now = time.time() if now is None else now
        if amount <= 0:
            return 0, 0
        with self._txn() as c:
            b = self._load(c, uid, now)
            if b.total < amount:
                raise InsufficientCredits(f"need {amount}, have {b.total}")
            from_free = min(b.free, amount)
            from_bought = amount - from_free
            b = Balance(b.free - from_free, b.bought - from_bought, b.last_refill)
            self._save(c, uid, b)
            self._log(c, uid, "spend", -from_free, -from_bought, b, now, check_id=check_id, note=note)
            return from_free, from_bought

    def refund(self, check_id: int, now: float | None = None, note: str = "") -> int:
        """Give back exactly what a check spent, to the same buckets. Idempotent; returns credits refunded."""
        now = time.time() if now is None else now
        with self._txn() as c:
            spent = c.execute("SELECT user_id, delta_free, delta_bought FROM credit_ledger "
                              "WHERE check_id=? AND kind='spend'", (check_id,)).fetchall()
            done = c.execute("SELECT 1 FROM credit_ledger WHERE check_id=? AND kind='refund' LIMIT 1",
                             (check_id,)).fetchone()
            if not spent or done:
                return 0
            uid = int(spent[0][0])
            d_free = -sum(int(r[1]) for r in spent)
            d_bought = -sum(int(r[2]) for r in spent)
            b = self._load(c, uid, now)
            # Refunded free credits go back even if a refill topped up meanwhile (may sit above cap
            # briefly; refills simply pause until it drops below).
            b = Balance(b.free + d_free, b.bought + d_bought, b.last_refill)
            self._save(c, uid, b)
            self._log(c, uid, "refund", d_free, d_bought, b, now, check_id=check_id, note=note)
            return d_free + d_bought

    def grant(self, uid: int, amount: int, actor: int | None = None, note: str = "",
              now: float | None = None) -> Balance:
        """Admin: add (or, if negative, remove) purchased credits. Never below zero."""
        now = time.time() if now is None else now
        with self._txn() as c:
            b = self._load(c, uid, now)
            if b.bought + amount < 0:
                raise InsufficientCredits(f"user has only {b.bought} purchased credits")
            b = Balance(b.free, b.bought + amount, b.last_refill)
            self._save(c, uid, b)
            self._log(c, uid, "grant", 0, amount, b, now, actor=actor, note=note)
            return b

    def ledger(self, uid: int, limit: int = 15) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT id, ts, kind, delta_free, delta_bought, free_after, bought_after, check_id, actor, note "
            "FROM credit_ledger WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit)).fetchall()
        keys = ("id", "ts", "kind", "delta_free", "delta_bought", "free_after", "bought_after", "check_id", "actor", "note")
        return [dict(zip(keys, r)) for r in rows]
