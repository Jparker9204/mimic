"""Free-tier daily limits and the cooldown between scans."""
from __future__ import annotations
import time
from dataclasses import dataclass
from . import config
from .db import DB

KIND_NAMES = {"wallet": "wallet scans", "coin": "coin scans", "sim": "sims"}


def day_start_utc(now: float) -> float:
    return now - (now % 86400)


@dataclass
class Verdict:
    ok: bool
    reason: str = ""          # "", "limit", "cooldown", "busy"
    wait_s: int = 0
    used: int = 0
    limit: int = 0


class Limits:
    def __init__(self, db: DB, limits: dict[str, int] | None = None, cooldown_s: int | None = None,
                 admins: set[int] | None = None):
        self.db = db
        self.limits = limits if limits is not None else dict(config.FREE_LIMITS)
        self.cooldown_s = config.COOLDOWN_S if cooldown_s is None else cooldown_s
        self.admins = config.ADMIN_IDS if admins is None else admins

    def check(self, user_id: int, kind: str, now: float | None = None) -> Verdict:
        now = time.time() if now is None else now
        limit = self.limits.get(kind, 0)
        used = self.db.count_since(user_id, kind, day_start_utc(now))
        if user_id in self.admins:
            return Verdict(True, used=used, limit=limit)
        if self.db.running_for(user_id):
            return Verdict(False, "busy", used=used, limit=limit)
        if used >= limit:
            return Verdict(False, "limit", wait_s=int(day_start_utc(now) + 86400 - now), used=used, limit=limit)
        last = self.db.last_counted_start(user_id)
        if last is not None and now - last < self.cooldown_s:
            return Verdict(False, "cooldown", wait_s=int(self.cooldown_s - (now - last) + 0.999), used=used, limit=limit)
        return Verdict(True, used=used, limit=limit)

    def usage(self, user_id: int, now: float | None = None) -> dict[str, tuple[int, int]]:
        now = time.time() if now is None else now
        return {k: (self.db.count_since(user_id, k, day_start_utc(now)), v) for k, v in self.limits.items()}
