"""SOL credit purchases with Solana Pay transfer requests.

Safety model: Mimic only WATCHES a public receiving address. It never generates, stores or uses a
private key or seed, never connects to a wallet and never sends funds. Each order gets a random
"reference" public key (32 random bytes, base58; nobody has a private key for it). The payer's wallet
adds it as a read-only account to the transfer, so we can find the payment with
getSignaturesForAddress(reference) and check it with getTransaction (finalized only).

Payments made by copying the address and amount (no reference) are matched by exact amount to a
single unpaid order; anything ambiguous is recorded as unmatched for an admin (/orders)."""
from __future__ import annotations
import asyncio, io, logging, re, secrets, time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from . import config
from .credits import Credits
from .db import DB
from .net import Net
from .solana import b58decode, b58encode, is_address

log = logging.getLogger("mimic.pay")
LAMPORTS = 10 ** 9
UNPAID = ("open", "expired", "cancelled", "replaced")
VERSION_RE = re.compile(r"[Tt]ransaction version \((\d+)\)")


# ---------------------------------------------------------------- pure helpers
def fmt_sol(lamports: int) -> str:
    """Exact decimal SOL with no float artifacts or exponent: 20000000 -> '0.02', 10**9 -> '1'."""
    d = (Decimal(int(lamports)) / LAMPORTS).normalize()
    s = format(d, "f")
    return s


def parse_sol(text: str) -> int | None:
    """User input like '0.05', '0,05', '0.05 SOL' -> lamports, or None. Max 9 decimals."""
    t = (text or "").strip().lower().replace(",", ".").replace("sol", "").replace("◎", "").strip()
    if not t or len(t) > 20:
        return None
    try:
        d = Decimal(t)
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d <= 0:
        return None
    lam = d * LAMPORTS
    if lam != lam.to_integral_value():
        return None          # more than 9 decimals
    return int(lam)


def credits_for(lamports: int, price_lamports: int | None = None) -> int:
    """credits = floor(SOL / price)."""
    p = config.PRICE_LAMPORTS if price_lamports is None else price_lamports
    return max(0, int(lamports) // max(1, int(p)))


def check_amount(lamports: int | None, min_l: int | None = None, max_l: int | None = None) -> str | None:
    """None if OK, else a short reason the user can read."""
    min_l = config.MIN_PURCHASE_LAMPORTS if min_l is None else min_l
    max_l = config.MAX_PURCHASE_LAMPORTS if max_l is None else max_l
    if lamports is None:
        return "Please type an amount in SOL, e.g. 0.05"
    if lamports < min_l:
        return f"The minimum is {fmt_sol(min_l)} SOL."
    if lamports > max_l:
        return f"The maximum per purchase is {fmt_sol(max_l)} SOL."
    return None


def new_reference() -> str:
    """A unique random public key for one order: 32 random bytes in base58. It's only a tag the wallet
    adds to the transfer so we can find it; no private key exists for it and none is needed."""
    while True:
        ref = b58encode(secrets.token_bytes(32))
        if len(b58decode(ref)) == 32:
            return ref


def pay_url(recipient: str, lamports: int, reference: str, label: str = "Mimic",
            message: str = "Mimic credits") -> str:
    """Solana Pay transfer request: solana:<recipient>?amount=..&reference=..&label=..&message=.."""
    return (f"solana:{recipient}?amount={fmt_sol(lamports)}&reference={reference}"
            f"&label={quote(label, safe='')}&message={quote(message, safe='')}")


def qr_png(data: str) -> bytes:
    import segno   # pure Python, no dependencies
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=8, border=4, dark="#000000", light="#ffffff")
    return buf.getvalue()


def tx_keys(tx: dict) -> list[str]:
    """All account keys of a getTransaction result (json or jsonParsed), incl. v0 lookup-table ones."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    raw = msg.get("accountKeys") or []
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in raw]
    if not any(isinstance(k, dict) for k in raw):
        la = (tx.get("meta") or {}).get("loadedAddresses") or {}
        keys += list(la.get("writable") or []) + list(la.get("readonly") or [])
    return [k for k in keys if isinstance(k, str)]


@dataclass
class TxCheck:
    ok: bool
    lamports: int       # SOL the receiving address gained (0 when not ok)
    reason: str = ""    # "", failed, no_reference, wrong_recipient, not_incoming, missing


def verify_tx(tx: dict | None, recipient: str, reference: str | None) -> TxCheck:
    """Did this transaction pay `recipient`? Requires no error, the reference among the account keys
    (when given) and a positive SOL balance change of the recipient (post - pre)."""
    if not tx:
        return TxCheck(False, 0, "missing")
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return TxCheck(False, 0, "failed")
    keys = tx_keys(tx)
    if reference is not None and reference not in keys:
        return TxCheck(False, 0, "no_reference")
    if recipient not in keys:
        return TxCheck(False, 0, "wrong_recipient")
    i = keys.index(recipient)
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if i >= len(pre) or i >= len(post):
        return TxCheck(False, 0, "wrong_recipient")
    delta = int(post[i]) - int(pre[i])
    if delta <= 0:
        return TxCheck(False, 0, "not_incoming")
    return TxCheck(True, delta)


# ---------------------------------------------------------------- storage
@dataclass
class Order:
    id: int
    user_id: int
    chat_id: int
    reference: str
    recipient: str
    lamports: int
    credits: int
    price_lamports: int
    status: str
    created_at: float
    expires_at: float
    last_checked: float
    paid_at: float | None
    received_lamports: int
    credited: int
    signature: str | None

    @property
    def url(self) -> str:
        return pay_url(self.recipient, self.lamports, self.reference)


ORDER_COLS = ("id, user_id, chat_id, reference, recipient, lamports, credits, price_lamports, status, created_at, "
              "expires_at, last_checked, paid_at, received_lamports, credited, signature")


@dataclass
class PayEvent:
    kind: str            # paid | short
    order: Order
    signature: str
    received: int        # total lamports received for the order so far
    credits: int = 0     # credited (paid)
    missing: int = 0     # lamports still missing (short)
    late: bool = False   # paid after the order expired/was cancelled


class Payments:
    def __init__(self, db: DB, credits: Credits, tolerance: int | None = None):
        self.db, self.credits = db, credits
        self.tol = config.PAY_TOLERANCE_LAMPORTS if tolerance is None else tolerance

    def _row(self, r) -> Order | None:
        return Order(*r) if r else None

    def get(self, order_id: int) -> Order | None:
        return self._row(self.db.conn.execute(f"SELECT {ORDER_COLS} FROM pay_orders WHERE id=?", (order_id,)).fetchone())

    def open_order(self, user_id: int) -> Order | None:
        return self._row(self.db.conn.execute(f"SELECT {ORDER_COLS} FROM pay_orders WHERE user_id=? AND status='open'",
                                              (user_id,)).fetchone())

    def create(self, user_id: int, chat_id: int, lamports: int, recipient: str | None = None,
               now: float | None = None) -> Order:
        """New order (cancels the user's previous open one). Raises ValueError on bad input."""
        now = time.time() if now is None else now
        recipient = config.PAY_ADDRESS if recipient is None else recipient
        if not is_address(recipient):
            raise ValueError("payments are not configured")
        err = check_amount(lamports)
        if err:
            raise ValueError(err)
        price = config.PRICE_LAMPORTS
        c = self.db.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("UPDATE pay_orders SET status='replaced' WHERE user_id=? AND status='open'", (user_id,))
            for _ in range(5):
                ref = new_reference()
                if not c.execute("SELECT 1 FROM pay_orders WHERE reference=?", (ref,)).fetchone():
                    break
            cur = c.execute("INSERT INTO pay_orders(user_id,chat_id,reference,recipient,lamports,credits,price_lamports,"
                            "status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (user_id, chat_id, ref, recipient, lamports, credits_for(lamports, price), price,
                             "open", now, now + config.ORDER_TTL_S))
            oid = int(cur.lastrowid)
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")
        return self.get(oid)

    def cancel(self, order_id: int, user_id: int) -> bool:
        cur = self.db.conn.execute("UPDATE pay_orders SET status='cancelled' WHERE id=? AND user_id=? AND status IN "
                                   "('open','expired')", (order_id, user_id))
        return cur.rowcount == 1

    def expire(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return self.db.conn.execute("UPDATE pay_orders SET status='expired' WHERE status='open' AND expires_at<?",
                                    (now,)).rowcount

    def due(self, now: float | None = None) -> list[Order]:
        """Unpaid orders whose next check is due: open ones every PAY_CHECK_S, the rest (expired,
        cancelled, replaced: people sometimes pay late) every PAY_LATE_CHECK_S, for LATE_PAYMENT_S."""
        now = time.time() if now is None else now
        rows = self.db.conn.execute(
            f"SELECT {ORDER_COLS} FROM pay_orders WHERE status IN ('open','expired','cancelled','replaced') "
            "AND created_at>=? AND ((status='open' AND last_checked<=?) OR (status!='open' AND last_checked<=?)) "
            "ORDER BY status='open' DESC, last_checked LIMIT 60",
            (now - config.LATE_PAYMENT_S, now - config.PAY_CHECK_S + 0.5, now - config.PAY_LATE_CHECK_S + 0.5)).fetchall()
        return [Order(*r) for r in rows]

    def unpaid_recent(self, now: float | None = None) -> list[Order]:
        now = time.time() if now is None else now
        rows = self.db.conn.execute(
            f"SELECT {ORDER_COLS} FROM pay_orders WHERE status IN ('open','expired','cancelled','replaced') "
            "AND created_at>=?", (now - config.LATE_PAYMENT_S,)).fetchall()
        return [Order(*r) for r in rows]

    def mark_checked(self, order_id: int, now: float | None = None) -> None:
        self.db.conn.execute("UPDATE pay_orders SET last_checked=? WHERE id=?",
                             (time.time() if now is None else now, order_id))

    def seen(self, signature: str) -> bool:
        return bool(self.db.conn.execute("SELECT 1 FROM pay_txs WHERE signature=?", (signature,)).fetchone())

    def known_reference(self, keys: list[str]) -> bool:
        """Does a tx carry any of our order references? (then the reference check handles it)"""
        ks = [k for k in keys if k]
        if not ks:
            return False
        q = ",".join("?" * len(ks))
        return bool(self.db.conn.execute(f"SELECT 1 FROM pay_orders WHERE reference IN ({q}) LIMIT 1", ks).fetchone())

    def record_unmatched(self, signature: str, lamports: int, block_time: float | None, note: str,
                         now: float | None = None) -> None:
        self.db.conn.execute("INSERT OR IGNORE INTO pay_txs(signature,order_id,lamports,ok,block_time,seen_at,note) "
                             "VALUES(?,?,?,?,?,?,?)", (signature, None, lamports, 0, block_time,
                                                       time.time() if now is None else now, note[:200]))

    def apply_tx(self, order_id: int, signature: str, check: TxCheck, block_time: float | None = None,
                 now: float | None = None, note: str = "") -> PayEvent | None:
        """Record one transaction for an order and credit the order when it's fully paid.

        One DB transaction: the tx row (signature is the primary key, so a signature is processed once),
        the order update, the pay_credits row (UNIQUE order_id and signature), the balance and the
        ledger entry either all commit or none do. Returns an event to tell the user, or None."""
        now = time.time() if now is None else now
        c = self.db.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            if c.execute("SELECT 1 FROM pay_txs WHERE signature=?", (signature,)).fetchone():
                c.execute("ROLLBACK")
                return None
            o = self._row(c.execute(f"SELECT {ORDER_COLS} FROM pay_orders WHERE id=?", (order_id,)).fetchone())
            if o is None:
                c.execute("ROLLBACK")
                return None
            counted = check.ok and o.status != "paid"
            c.execute("INSERT INTO pay_txs(signature,order_id,lamports,ok,block_time,seen_at,note) VALUES(?,?,?,?,?,?,?)",
                      (signature, order_id, check.lamports if check.ok else 0, int(counted), block_time, now,
                       (note or check.reason or ("after paid" if check.ok and not counted else ""))[:200]))
            if not counted:
                c.execute("COMMIT")
                if check.ok:
                    log.warning("order %s: extra payment after it was paid (admin: see /orders)", order_id)
                return None
            total = int(c.execute("SELECT COALESCE(SUM(lamports),0) FROM pay_txs WHERE order_id=? AND ok=1",
                                  (order_id,)).fetchone()[0])
            if total + self.tol < o.lamports:
                c.execute("UPDATE pay_orders SET received_lamports=? WHERE id=?", (total, order_id))
                c.execute("COMMIT")
                return PayEvent("short", o, signature, total, missing=o.lamports - total)
            # paid: credit on what actually arrived (overpayment counts), never less than quoted
            n = max(o.credits, credits_for(total, o.price_lamports))
            late = o.status != "open"
            cur = c.execute("UPDATE pay_orders SET status='paid', paid_at=?, received_lamports=?, credited=?, signature=? "
                            "WHERE id=? AND status!='paid'", (now, total, n, signature, order_id))
            if cur.rowcount != 1:
                raise RuntimeError("order already paid")
            _, lid = self.credits.add_purchased(
                c, o.user_id, n, now, note=f"order #{order_id} · {fmt_sol(total)} SOL · tx {signature}")
            c.execute("INSERT INTO pay_credits(order_id,signature,user_id,lamports,credits,ts,ledger_id) VALUES(?,?,?,?,?,?,?)",
                      (order_id, signature, o.user_id, total, n, now, lid))
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")
        log.info("order %s paid: %s credits", order_id, n)
        return PayEvent("paid", self.get(order_id), signature, total, credits=n, late=late)

    def match_manual(self, lamports: int, block_time: float | None, recipient: str,
                     now: float | None = None) -> Order | None:
        """A transfer without a reference (address + amount copied by hand): the single unpaid recent
        order for this recipient with exactly this amount, or exactly its missing rest (± tolerance),
        created before the tx. Zero or several candidates -> None (recorded as unmatched)."""
        bt = block_time or (time.time() if now is None else now)
        cands = [o for o in self.unpaid_recent(now) if o.recipient == recipient and o.created_at <= bt + 120
                 and (abs(o.lamports - lamports) <= self.tol
                      or (o.received_lamports and abs(o.lamports - o.received_lamports - lamports) <= self.tol))]
        return cands[0] if len(cands) == 1 else None

    def state_get(self, k: str) -> str | None:
        r = self.db.conn.execute("SELECT v FROM pay_state WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def state_set(self, k: str, v: str) -> None:
        self.db.conn.execute("INSERT OR REPLACE INTO pay_state(k,v) VALUES(?,?)", (k, v))

    def recent(self, limit: int = 15, user_id: int | None = None) -> list[Order]:
        if user_id is None:
            rows = self.db.conn.execute(f"SELECT {ORDER_COLS} FROM pay_orders ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self.db.conn.execute(f"SELECT {ORDER_COLS} FROM pay_orders WHERE user_id=? ORDER BY id DESC LIMIT ?",
                                        (user_id, limit))
        return [Order(*r) for r in rows.fetchall()]

    def unmatched(self, limit: int = 5) -> list[dict]:
        rows = self.db.conn.execute("SELECT signature, lamports, block_time, note FROM pay_txs WHERE order_id IS NULL "
                                    "AND lamports>0 ORDER BY seen_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(("signature", "lamports", "block_time", "note"), r)) for r in rows]


# ---------------------------------------------------------------- RPC (read-only)
class PayRpc:
    """Read-only JSON-RPC: getSignaturesForAddress + getTransaction at finalized commitment.
    Backoff on 429/5xx is done by Net. The RPC URL is never logged."""

    def __init__(self, net: Net):
        self.net = net
        self.max_version = 0
        self.endpoints = ([("custom", config.CUSTOM_RPC)] if config.CUSTOM_RPC else []) + [
            ("publicnode", config.PUBLICNODE_RPC), ("mainnet", config.MAINNET_RPC)]

    async def _call(self, method: str, params: list):
        """(ok, result). Tries each endpoint; a JSON-RPC error body is returned to the caller."""
        last_err = None
        for bucket, url in self.endpoints:
            j = await self.net.request(bucket, "POST", url, retries=3,
                                       json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if isinstance(j, dict) and "result" in j:
                return True, j["result"]
            if isinstance(j, dict) and "error" in j:
                last_err = j["error"]
                if VERSION_RE.search(str((last_err or {}).get("message", ""))):
                    return False, last_err
        return False, last_err

    async def signatures(self, address: str, limit: int = 25, until: str | None = None,
                         before: str | None = None) -> list[dict] | None:
        p: dict = {"limit": limit, "commitment": "finalized"}
        if until:
            p["until"] = until
        if before:
            p["before"] = before
        ok, res = await self._call("getSignaturesForAddress", [address, p])
        return res if ok and isinstance(res, list) else None

    async def transaction(self, sig: str) -> dict | None:
        """maxSupportedTransactionVersion 0; if the RPC says the tx uses a newer version, retry with
        the version it names (and remember it)."""
        for _ in range(2):
            ok, res = await self._call("getTransaction", [sig, {"encoding": "json", "commitment": "finalized",
                                                                "maxSupportedTransactionVersion": self.max_version}])
            if ok:
                return res
            m = VERSION_RE.search(str((res or {}).get("message", ""))) if isinstance(res, dict) else None
            if not m or int(m.group(1)) <= self.max_version or int(m.group(1)) > 9:
                return None
            self.max_version = int(m.group(1))
        return None


# ---------------------------------------------------------------- background checker
class PaymentWatcher:
    """One background loop. Only makes RPC calls while unpaid orders exist (open: every ~10s;
    expired/cancelled: every ~60s for 24h). `notify(event)` is awaited for each event."""

    def __init__(self, pay: Payments, rpc: PayRpc, notify, tick_s: float = 5.0):
        self.pay, self.rpc, self.notify, self.tick_s = pay, rpc, notify, tick_s
        self._locks: dict[int, asyncio.Lock] = {}
        self._addr_next = 0.0
        self.scan_addr = True     # also match reference-less exact-amount transfers to the receiving address

    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("payment poll failed")
            await asyncio.sleep(self.tick_s)

    async def poll_once(self, now: float | None = None) -> list[PayEvent]:
        now = time.time() if now is None else now
        self.pay.expire(now)
        events: list[PayEvent] = []
        for o in self.pay.due(now):
            events += await self.check_order(o, now=now)
        if self.scan_addr and config.PAY_ADDRESS and time.time() >= self._addr_next:
            unpaid = self.pay.unpaid_recent(now)
            if unpaid:
                any_open = any(o.status == "open" for o in unpaid)
                self._addr_next = time.time() + (2 * config.PAY_CHECK_S if any_open else config.PAY_LATE_CHECK_S)
                events += await self.scan_address(config.PAY_ADDRESS, now=now)
        return events

    async def check_order(self, o: Order, now: float | None = None) -> list[PayEvent]:
        lock = self._locks.setdefault(o.id, asyncio.Lock())
        async with lock:
            fresh = self.pay.get(o.id)
            if fresh is None or fresh.status == "paid":
                return []
            return await self._check_order(fresh, now)

    async def _check_order(self, o: Order, now: float | None) -> list[PayEvent]:
        self.pay.mark_checked(o.id, now)
        sigs = await self.rpc.signatures(o.reference, limit=25)
        if not sigs:
            return []
        events = []
        for s in reversed(sigs):                       # oldest first
            sig = s.get("signature")
            if not sig or self.pay.seen(sig):
                continue
            if s.get("err") is not None:
                ev = self.pay.apply_tx(o.id, sig, TxCheck(False, 0, "failed"), s.get("blockTime"), now)
            else:
                tx = await self.rpc.transaction(sig)
                if tx is None:
                    continue                           # not readable yet: try again next round
                ev = self.pay.apply_tx(o.id, sig, verify_tx(tx, o.recipient, o.reference), tx.get("blockTime"), now)
            if ev:
                events.append(ev)
                await self._notify(ev)
                if ev.kind == "paid":
                    break
        return events

    async def scan_address(self, address: str, now: float | None = None) -> list[PayEvent]:
        """New finalized txs of the receiving address that carry no order reference: exact-amount
        transfers (address + amount copied by hand) are matched to a single unpaid order."""
        key = "cursor:" + address
        cursor = self.pay.state_get(key)
        if cursor is None:                             # first run: start from now, ignore history
            page = await self.rpc.signatures(address, limit=1)
            if page is not None:
                self.pay.state_set(key, page[0]["signature"] if page else "")
            return []
        sigs = await self.rpc.signatures(address, limit=100, until=cursor or None)
        if not sigs:
            return []
        events = []
        for s in reversed(sigs):                       # oldest first; stop at the first unreadable tx
            sig = s["signature"]
            if s.get("err") is None and not self.pay.seen(sig):
                tx = await self.rpc.transaction(sig)
                if tx is None:
                    break
                keys = tx_keys(tx)
                if not self.pay.known_reference(keys):
                    chk = verify_tx(tx, address, None)
                    if chk.ok:
                        o = self.pay.match_manual(chk.lamports, tx.get("blockTime"), address, now)
                        if o is None:
                            self.pay.record_unmatched(sig, chk.lamports, tx.get("blockTime"),
                                                      "no reference, no unique exact-amount order", now)
                            log.info("unmatched incoming payment recorded (see /orders)")
                        else:
                            ev = self.pay.apply_tx(o.id, sig, chk, tx.get("blockTime"), now, note="matched by amount")
                            if ev:
                                events.append(ev)
                                await self._notify(ev)
            self.pay.state_set(key, sig)
        return events

    async def _notify(self, ev: PayEvent) -> None:
        if self.notify is None:
            return
        try:
            await self.notify(ev)
        except Exception:  # noqa: BLE001
            log.exception("payment notification failed (order %s)", ev.order.id)
