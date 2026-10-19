"""SQLite storage: check log, credit balances + ledger, per-user settings and a TTL key/value cache."""
from __future__ import annotations
import json, os, sqlite3, time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,            -- wallet | coin | sim
  target TEXT,
  status TEXT NOT NULL,          -- running | ok | failed
  created_at REAL NOT NULL,
  finished_at REAL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS ix_checks_user ON checks(user_id, created_at);
CREATE TABLE IF NOT EXISTS user_settings (
  user_id INTEGER NOT NULL,
  k TEXT NOT NULL,
  v TEXT NOT NULL,
  PRIMARY KEY (user_id, k)
);
CREATE TABLE IF NOT EXISTS credits (
  user_id INTEGER PRIMARY KEY,
  free INTEGER NOT NULL,          -- free credits (refilled lazily, capped)
  bought INTEGER NOT NULL DEFAULT 0,  -- purchased/granted credits (no cap, never expire)
  last_refill REAL NOT NULL,      -- start of the current refill period
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS credit_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,             -- signup | migrate | refill | spend | refund | grant
  delta_free INTEGER NOT NULL,
  delta_bought INTEGER NOT NULL,
  free_after INTEGER NOT NULL,
  bought_after INTEGER NOT NULL,
  check_id INTEGER,
  actor INTEGER,                  -- admin user id for grants
  note TEXT
);
CREATE INDEX IF NOT EXISTS ix_ledger_user ON credit_ledger(user_id, ts);
CREATE INDEX IF NOT EXISTS ix_ledger_check ON credit_ledger(check_id);
CREATE TABLE IF NOT EXISTS cache (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL,
  expires REAL NOT NULL
);
"""


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    # ---- checks -----------------------------------------------------------
    def start_check(self, user_id: int, kind: str, target: str, now: float | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO checks(user_id,kind,target,status,created_at) VALUES(?,?,?,?,?)",
            (user_id, kind, target, "running", now if now is not None else time.time()))
        return int(cur.lastrowid)

    def finish_check(self, check_id: int, ok: bool, note: str = "", now: float | None = None) -> None:
        self.conn.execute("UPDATE checks SET status=?, finished_at=?, note=? WHERE id=?",
                          ("ok" if ok else "failed", now if now is not None else time.time(), note[:500], check_id))

    def count_since(self, user_id: int, kind: str, since: float) -> int:
        """Checks that count against limits: ok or still running (failed ones never count)."""
        r = self.conn.execute(
            "SELECT COUNT(*) FROM checks WHERE user_id=? AND kind=? AND created_at>=? AND status IN ('ok','running')",
            (user_id, kind, since)).fetchone()
        return int(r[0])

    def last_counted_start(self, user_id: int) -> float | None:
        r = self.conn.execute(
            "SELECT MAX(created_at) FROM checks WHERE user_id=? AND status IN ('ok','running')", (user_id,)).fetchone()
        return r[0]

    def running_for(self, user_id: int) -> int:
        r = self.conn.execute("SELECT COUNT(*) FROM checks WHERE user_id=? AND status='running'", (user_id,)).fetchone()
        return int(r[0])

    def fail_stale_running(self) -> list[int]:
        """On startup, anything still 'running' died with the old process: mark failed (doesn't count).
        Returns their ids so their credits can be refunded."""
        ids = [int(r[0]) for r in self.conn.execute("SELECT id FROM checks WHERE status='running'")]
        self.conn.execute("UPDATE checks SET status='failed', note='interrupted' WHERE status='running'")
        return ids

    def has_history(self, user_id: int) -> bool:
        """True if this user used the bot before credits existed (any check or saved setting)."""
        return bool(self.conn.execute("SELECT 1 FROM checks WHERE user_id=? LIMIT 1", (user_id,)).fetchone()
                    or self.conn.execute("SELECT 1 FROM user_settings WHERE user_id=? LIMIT 1", (user_id,)).fetchone())

    # ---- per-user settings ------------------------------------------------
    def get_setting(self, user_id: int, k: str) -> Any | None:
        r = self.conn.execute("SELECT v FROM user_settings WHERE user_id=? AND k=?", (user_id, k)).fetchone()
        return json.loads(r[0]) if r else None

    def set_setting(self, user_id: int, k: str, v: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO user_settings(user_id,k,v) VALUES(?,?,?)", (user_id, k, json.dumps(v)))

    # ---- cache ------------------------------------------------------------
    def cache_get(self, k: str) -> Any | None:
        r = self.conn.execute("SELECT v, expires FROM cache WHERE k=?", (k,)).fetchone()
        if not r or r[1] < time.time():
            return None
        return json.loads(r[0])

    def cache_set(self, k: str, v: Any, ttl_s: float) -> None:
        self.conn.execute("INSERT OR REPLACE INTO cache(k,v,expires) VALUES(?,?,?)",
                          (k, json.dumps(v, separators=(",", ":")), time.time() + ttl_s))

    def prune(self) -> None:
        self.conn.execute("DELETE FROM cache WHERE expires < ?", (time.time(),))
