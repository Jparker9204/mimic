"""Who may run a check right now: credits, the cooldown between scans, one job at a time, admin bypass."""
from __future__ import annotations
import time
from dataclasses import dataclass
from . import config
from .credits import Balance, Credits
from .db import DB

KIND_LABELS = {"wallet": "Wallet Scan", "coin": "Coin Scan", "sim": "Copytrade Sim"}


@dataclass
class Verdict:
    ok: bool
    reason: str = ""          # "", "credits", "cooldown", "busy"
    wait_s: int = 0           # cooldown left, or time to next refill when out of credits
    cost: int = 0
    balance: Balance | None = None
    enough_in: int | None = None   # out of credits: seconds until refills cover the cost (None = never)


class Limits:
    def __init__(self, db: DB, credits: Credits | None = None, costs: dict[str, int] | None = None,
                 cooldown_s: int | None = None, admins: set[int] | None = None):
        self.db = db
        self.credits = credits or Credits(db)
        self.costs = dict(config.COSTS) if costs is None else costs
        self.cooldown_s = config.COOLDOWN_S if cooldown_s is None else cooldown_s
        self.admins = config.ADMIN_IDS if admins is None else admins

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admins

    def cost(self, user_id: int, kind: str) -> int:
        return 0 if self.is_admin(user_id) else self.costs.get(kind, 0)

    def check(self, user_id: int, kind: str, now: float | None = None) -> Verdict:
        now = time.time() if now is None else now
        if self.is_admin(user_id):
            return Verdict(True)
        cost = self.cost(user_id, kind)
        if self.db.running_for(user_id):
            return Verdict(False, "busy", cost=cost)
        bal = self.credits.balance(user_id, now)
        if bal.total < cost:
            nxt = self.credits.next_refill_in(bal, now)
            return Verdict(False, "credits", wait_s=nxt or 0, cost=cost, balance=bal,
                           enough_in=self.credits.time_until(bal, cost, now))
        last = self.db.last_counted_start(user_id)
        if last is not None and now - last < self.cooldown_s:
            return Verdict(False, "cooldown", wait_s=int(self.cooldown_s - (now - last) + 0.999), cost=cost, balance=bal)
        return Verdict(True, cost=cost, balance=bal)

    def charge(self, user_id: int, kind: str, check_id: int, now: float | None = None) -> int:
        """Take the credits for a started check (raises InsufficientCredits). Admins pay nothing."""
        cost = self.cost(user_id, kind)
        if cost:
            self.credits.spend(user_id, cost, check_id=check_id, now=now, note=kind)
        return cost

    def refund(self, check_id: int, note: str = "", now: float | None = None) -> int:
        return self.credits.refund(check_id, now=now, note=note)
