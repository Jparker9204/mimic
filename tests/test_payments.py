"""SOL credit purchases: math, amounts, orders, Solana Pay URL, tx verification, crediting exactly once,
late payments, the background checker with a fake RPC, and the bot's payment message fallbacks."""
import importlib
import pytest
from telegram.error import BadRequest
from mimic import config, texts
from mimic import payments as pm
from mimic.credits import Credits
from mimic.db import DB
from mimic.payments import (PaymentWatcher, Payments, PayRpc, TxCheck, check_amount, credits_for, fmt_sol,
                            new_reference, parse_sol, pay_url, qr_png, verify_tx)
from mimic.solana import b58decode, b58encode

ADDR = b58encode(bytes([7] * 32))      # a made-up receiving address (not a real wallet)
OTHER = b58encode(bytes([9] * 32))
PAYER = b58encode(bytes([3] * 32))
SYS = "11111111111111111111111111111111"
T0 = 1_800_000_000.0
SOL = 10 ** 9


@pytest.fixture(autouse=True)
def pay_cfg(monkeypatch):
    monkeypatch.setattr(config, "PAY_ADDRESS", ADDR)
    monkeypatch.setattr(config, "PRICE_LAMPORTS", 1_000_000)
    monkeypatch.setattr(config, "MIN_PURCHASE_LAMPORTS", 20_000_000)
    monkeypatch.setattr(config, "MAX_PURCHASE_LAMPORTS", 5 * SOL)
    monkeypatch.setattr(config, "ORDER_TTL_S", 900)
    monkeypatch.setattr(config, "LATE_PAYMENT_S", 86400)


@pytest.fixture
def pay():
    db = DB(":memory:")
    return Payments(db, Credits(db, refill=40, interval_h=6, cap=150))


def mktx(recipient=ADDR, reference=None, delta=50_000_000, err=None, v0_ref=False, parsed=False):
    """A getTransaction(json) result for a SOL transfer PAYER -> recipient, optionally tagged with a reference."""
    keys = [PAYER, recipient, SYS]
    pre, post = [SOL, 5 * SOL, 1], [SOL - delta - 5000, 5 * SOL + delta, 1]
    loaded = {"writable": [], "readonly": []}
    if reference and v0_ref:
        loaded["readonly"].append(reference)
        pre.append(0)
        post.append(0)
    elif reference:
        keys.append(reference)
        pre.append(0)
        post.append(0)
    if parsed:
        keys = [{"pubkey": k, "signer": i == 0, "writable": i < 2} for i, k in enumerate(keys)]
    return {"blockTime": int(T0) + 30, "slot": 1,
            "meta": {"err": err, "fee": 5000, "preBalances": pre, "postBalances": post, "loadedAddresses": loaded},
            "transaction": {"message": {"accountKeys": keys}, "signatures": ["x"]}}


# ---------------------------------------------------------------- amounts and math
def test_credit_math():
    assert credits_for(20_000_000) == 20
    assert credits_for(55_500_000) == 55           # floor
    assert credits_for(999_999) == 0
    assert credits_for(SOL, 2_000_000) == 500


def test_parse_and_limits():
    assert parse_sol("0.05") == 50_000_000
    assert parse_sol("0,05") == 50_000_000
    assert parse_sol(" 0.1 SOL ") == 100_000_000
    assert parse_sol("1") == SOL
    for bad in ("", "abc", "-1", "0", "nan", "inf", "0.0000000001", "1.2.3"):
        assert parse_sol(bad) is None, bad
    assert check_amount(20_000_000) is None
    assert check_amount(5 * SOL) is None
    assert "minimum is 0.02" in check_amount(19_999_999)
    assert "maximum per purchase is 5" in check_amount(5 * SOL + 1)
    assert check_amount(None) and check_amount(parse_sol("1e30"))


def test_fmt_sol_no_float_artifacts():
    assert fmt_sol(20_000_000) == "0.02"
    assert fmt_sol(100_000_000) == "0.1"
    assert fmt_sol(300_000_000) == "0.3"            # float 0.1*3 would be 0.30000000000000004
    assert fmt_sol(SOL) == "1"
    assert fmt_sol(5 * SOL) == "5"
    assert fmt_sol(10 * SOL) == "10"                # not 1E+1
    assert fmt_sol(1) == "0.000000001"              # not 1E-9
    assert fmt_sol(12_345_678_901) == "12.345678901"


def test_solana_pay_url():
    ref = new_reference()
    u = pay_url(ADDR, 50_000_000, ref)
    assert u == f"solana:{ADDR}?amount=0.05&reference={ref}&label=Mimic&message=Mimic%20credits"
    assert pay_url(ADDR, 70_000_000, ref).split("amount=")[1].startswith("0.07&")
    assert "e-" not in pay_url(ADDR, 1, ref).lower()


def test_references_are_random_32_byte_keys():
    refs = {new_reference() for _ in range(500)}
    assert len(refs) == 500
    assert all(len(b58decode(r)) == 32 for r in refs)


def test_qr_png():
    png = qr_png(pay_url(ADDR, 50_000_000, new_reference()))
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 300


# ---------------------------------------------------------------- orders
def test_order_create_one_open_per_user(pay):
    a = pay.create(1, 1, 50_000_000, now=T0)
    assert (a.status, a.credits, a.lamports, a.recipient) == ("open", 50, 50_000_000, ADDR)
    assert a.expires_at == T0 + 900 and len(b58decode(a.reference)) == 32
    b = pay.create(1, 1, 100_000_000, now=T0 + 5)
    assert pay.get(a.id).status == "replaced" and pay.open_order(1).id == b.id
    c = pay.create(2, 2, 20_000_000, now=T0)
    assert len({a.reference, b.reference, c.reference}) == 3
    assert pay.db.conn.execute("SELECT COUNT(*) FROM pay_orders WHERE status='open'").fetchone()[0] == 2


def test_order_limits_and_missing_address(pay, monkeypatch):
    with pytest.raises(ValueError):
        pay.create(1, 1, 10_000_000)
    with pytest.raises(ValueError):
        pay.create(1, 1, 6 * SOL)
    monkeypatch.setattr(config, "PAY_ADDRESS", "")
    with pytest.raises(ValueError):
        pay.create(1, 1, 50_000_000)
    assert "aren't available yet" in texts.buy_text()
    assert "isn't available yet" in texts.buy_note()


def test_expire_and_cancel(pay):
    o = pay.create(1, 1, 50_000_000, now=T0)
    assert pay.expire(now=T0 + 899) == 0
    assert pay.expire(now=T0 + 901) == 1 and pay.get(o.id).status == "expired"
    assert not pay.cancel(o.id, 2)                  # someone else's order
    assert pay.cancel(o.id, 1) and pay.get(o.id).status == "cancelled"


# ---------------------------------------------------------------- tx verification
def test_verify_tx_cases():
    ref = new_reference()
    assert verify_tx(mktx(reference=ref), ADDR, ref) == TxCheck(True, 50_000_000)
    assert verify_tx(mktx(reference=ref, v0_ref=True), ADDR, ref).ok          # reference via lookup table
    assert verify_tx(mktx(reference=ref, parsed=True), ADDR, ref).ok          # jsonParsed keys
    assert verify_tx(mktx(reference=ref, err={"InstructionError": [0, "x"]}), ADDR, ref).reason == "failed"
    assert verify_tx(mktx(recipient=OTHER, reference=ref), ADDR, ref).reason == "wrong_recipient"
    assert verify_tx(mktx(reference=None), ADDR, ref).reason == "no_reference"
    assert verify_tx(mktx(reference=ref, delta=-1000), ADDR, ref).reason == "not_incoming"
    assert verify_tx(None, ADDR, ref).reason == "missing"
    assert verify_tx(mktx(), ADDR, None).ok                                    # address scan: no reference


def bal(pay, uid=1):
    return pay.credits.balance(uid, now=T0 + 60)


def test_exact_payment_credits_once(pay):
    o = pay.create(1, 1, 50_000_000, now=T0)
    chk = verify_tx(mktx(reference=o.reference), ADDR, o.reference)
    ev = pay.apply_tx(o.id, "SIG1", chk, now=T0 + 40)
    assert ev.kind == "paid" and ev.credits == 50 and ev.received == 50_000_000 and not ev.late
    assert bal(pay).bought == 50
    assert pay.apply_tx(o.id, "SIG1", chk, now=T0 + 50) is None              # same signature again
    assert bal(pay).bought == 50
    row = pay.db.conn.execute("SELECT order_id, signature, lamports, credits FROM pay_credits").fetchall()
    assert row == [(o.id, "SIG1", 50_000_000, 50)]
    led = pay.credits.ledger(1, 1)[0]
    assert led["kind"] == "purchase" and led["delta_bought"] == 50 and "SIG1" in led["note"] and "0.05 SOL" in led["note"]
    o2 = pay.get(o.id)
    assert (o2.status, o2.credited, o2.signature) == ("paid", 50, "SIG1")
    # a second, different tx for the paid order is recorded but never credited again
    assert pay.apply_tx(o.id, "SIG2", chk, now=T0 + 60) is None
    assert bal(pay).bought == 50


def test_unique_constraints_in_db(pay):
    import sqlite3
    o = pay.create(1, 1, 50_000_000, now=T0)
    pay.apply_tx(o.id, "SIG1", TxCheck(True, 50_000_000), now=T0)
    with pytest.raises(sqlite3.IntegrityError):
        pay.db.conn.execute("INSERT INTO pay_credits(order_id,signature,user_id,lamports,credits,ts) "
                            "VALUES(?,?,?,?,?,?)", (o.id, "OTHER", 1, 1, 1, T0))
    with pytest.raises(sqlite3.IntegrityError):
        pay.db.conn.execute("INSERT INTO pay_credits(order_id,signature,user_id,lamports,credits,ts) "
                            "VALUES(?,?,?,?,?,?)", (999, "SIG1", 1, 1, 1, T0))


def test_overpayment_credits_actual_sol(pay):
    o = pay.create(1, 1, 50_000_000, now=T0)
    ev = pay.apply_tx(o.id, "S", TxCheck(True, 77_700_000), now=T0)
    assert ev.kind == "paid" and ev.credits == 77 and bal(pay).bought == 77


def test_tiny_shortfall_within_tolerance_gets_quoted_credits(pay):
    o = pay.create(1, 1, 50_000_000, now=T0)
    ev = pay.apply_tx(o.id, "S", TxCheck(True, 50_000_000 - 1000), now=T0)
    assert ev.kind == "paid" and ev.credits == 50


def test_underpayment_keeps_order_open_then_completes(pay):
    o = pay.create(1, 1, 50_000_000, now=T0)
    ev = pay.apply_tx(o.id, "S1", TxCheck(True, 30_000_000), now=T0)
    assert ev.kind == "short" and ev.missing == 20_000_000 and ev.received == 30_000_000
    assert pay.get(o.id).status == "open" and bal(pay).bought == 0
    msg = texts.short_text(ev)
    assert "0.02 SOL</b> is still missing" in msg and "amount=0.02&" in msg and o.reference in msg
    ev = pay.apply_tx(o.id, "S2", TxCheck(True, 20_000_000), now=T0 + 60)
    assert ev.kind == "paid" and ev.credits == 50 and ev.received == 50_000_000 and bal(pay).bought == 50


def test_wrong_recipient_and_failed_tx_not_credited(pay):
    o = pay.create(1, 1, 50_000_000, now=T0)
    assert pay.apply_tx(o.id, "W", verify_tx(mktx(OTHER, o.reference), ADDR, o.reference), now=T0) is None
    assert pay.apply_tx(o.id, "F", verify_tx(mktx(reference=o.reference, err={"x": 1}), ADDR, o.reference), now=T0) is None
    assert bal(pay).bought == 0 and pay.get(o.id).status == "open"
    assert pay.seen("W") and pay.seen("F")


def test_late_payment_after_expiry_or_cancel(pay):
    o = pay.create(1, 1, 50_000_000, now=T0)
    pay.expire(now=T0 + 1000)
    ev = pay.apply_tx(o.id, "L", TxCheck(True, 50_000_000), now=T0 + 3600)
    assert ev.kind == "paid" and ev.late and bal(pay).bought == 50
    assert "still counts" in texts.paid_text(ev)
    o2 = pay.create(1, 1, 20_000_000, now=T0)
    assert pay.cancel(o2.id, 1)
    assert pay.apply_tx(o2.id, "L2", TxCheck(True, 20_000_000), now=T0 + 60).late


def test_paid_text():
    o = pm.Order(1, 1, 1, "r", ADDR, 50_000_000, 50, 1_000_000, "paid", T0, T0, 0, T0, 50_000_000, 50, "5" * 88)
    t = texts.paid_text(pm.PayEvent("paid", o, "5" * 88, 50_000_000, credits=50))
    assert t.startswith("✅ Payment received: 0.05 SOL → 50 credits. Tx: <a href=\"https://solscan.io/tx/")
    assert ">5555…5555</a>" in t


# ---------------------------------------------------------------- background checker with a fake RPC
class FakeRpc:
    def __init__(self):
        self.sigs: dict[str, list[dict]] = {}
        self.txs: dict[str, dict] = {}
        self.calls = []

    async def signatures(self, address, limit=25, until=None, before=None):
        self.calls.append(("sigs", address))
        out = self.sigs.get(address, [])
        if until:
            idx = [s["signature"] for s in out].index(until) if until in [s["signature"] for s in out] else len(out)
            out = out[:idx]
        return out[:limit]

    async def transaction(self, sig):
        self.calls.append(("tx", sig))
        return self.txs.get(sig)


@pytest.fixture
def watcher(pay, monkeypatch):
    monkeypatch.setattr(config, "PAY_ADDRESS", ADDR)
    got = []

    async def notify(ev):
        got.append(ev)
    w = PaymentWatcher(pay, FakeRpc(), notify)
    w.got = got
    return w


async def test_watcher_credits_once_over_repeated_polls(watcher, monkeypatch):
    watcher.scan_addr = False                            # reference path only
    pay, rpc = watcher.pay, watcher.rpc
    o = pay.create(1, 1, 50_000_000, now=T0)
    assert await watcher.poll_once(now=T0 + 1) == [] and rpc.calls == [("sigs", o.reference)]
    rpc.sigs[o.reference] = [{"signature": "SIGA", "err": None, "blockTime": T0 + 30}]
    rpc.txs["SIGA"] = mktx(reference=o.reference)
    evs = await watcher.poll_once(now=T0 + 20)
    assert [e.kind for e in evs] == ["paid"] and watcher.got[0].credits == 50
    for t in (T0 + 40, T0 + 60, T0 + 100):
        assert await watcher.poll_once(now=t) == []
    assert bal(pay).bought == 50 and len(watcher.got) == 1
    # a paid order is no longer polled
    n = len(rpc.calls)
    await watcher.poll_once(now=T0 + 200)
    assert len(rpc.calls) == n


async def test_watcher_rechecks_unreadable_tx_and_late_window(watcher, monkeypatch):
    watcher.scan_addr = False
    pay, rpc = watcher.pay, watcher.rpc
    o = pay.create(1, 1, 50_000_000, now=T0)
    rpc.sigs[o.reference] = [{"signature": "S", "err": None}]
    await watcher.poll_once(now=T0 + 20)                 # tx not readable yet
    assert not pay.seen("S")
    rpc.txs["S"] = mktx(reference=o.reference)
    assert await watcher.poll_once(now=T0 + 25) == []     # checked again only after PAY_CHECK_S
    assert [e.kind for e in await watcher.poll_once(now=T0 + 31)] == ["paid"]
    # long after expiry (but within 24h) orders are still checked, at the slow rate
    o2 = pay.create(2, 2, 20_000_000, now=T0)
    pay.expire(now=T0 + 2000)
    rpc.sigs[o2.reference] = [{"signature": "S2", "err": None}]
    rpc.txs["S2"] = mktx(reference=o2.reference, delta=20_000_000)
    evs = await watcher.poll_once(now=T0 + 5 * 3600)
    assert any(e.kind == "paid" and e.late for e in evs)
    # after the late window nothing is checked
    o3 = pay.create(3, 3, 20_000_000, now=T0)
    pay.expire(now=T0 + 2000)
    rpc.calls.clear()
    await watcher.poll_once(now=T0 + 25 * 3600)
    assert ("sigs", o3.reference) not in rpc.calls


async def test_watcher_failed_signature_skipped(watcher, monkeypatch):
    watcher.scan_addr = False
    pay, rpc = watcher.pay, watcher.rpc
    o = pay.create(1, 1, 50_000_000, now=T0)
    rpc.sigs[o.reference] = [{"signature": "BAD", "err": {"InstructionError": [0, "x"]}}]
    assert await watcher.poll_once(now=T0 + 20) == []
    assert pay.seen("BAD") and ("tx", "BAD") not in rpc.calls and bal(pay).bought == 0


async def test_address_scan_matches_exact_amount_without_reference(watcher):
    pay, rpc = watcher.pay, watcher.rpc
    rpc.sigs[ADDR] = [{"signature": "OLD", "err": None}]
    pay.create(1, 1, 50_000_000, now=T0)
    await watcher.scan_address(ADDR, now=T0)             # first run: cursor = newest, history ignored
    assert pay.state_get("cursor:" + ADDR) == "OLD"
    rpc.sigs[ADDR] = [{"signature": "MAN", "err": None}, {"signature": "OLD", "err": None}]
    rpc.txs["MAN"] = mktx(reference=None, delta=50_000_000)
    evs = await watcher.scan_address(ADDR, now=T0 + 60)
    assert [e.kind for e in evs] == ["paid"] and bal(pay).bought == 50
    assert pay.state_get("cursor:" + ADDR) == "MAN"


async def test_address_scan_ambiguous_or_referenced(watcher):
    pay, rpc = watcher.pay, watcher.rpc
    pay.state_set("cursor:" + ADDR, "")
    a = pay.create(1, 1, 50_000_000, now=T0)
    pay.create(2, 2, 50_000_000, now=T0)
    rpc.sigs[ADDR] = [{"signature": "AMB", "err": None}, {"signature": "REF", "err": None}]
    rpc.txs["AMB"] = mktx(reference=None, delta=50_000_000)
    rpc.txs["REF"] = mktx(reference=a.reference)        # left for the reference check
    assert await watcher.scan_address(ADDR, now=T0 + 60) == []
    assert [u["signature"] for u in pay.unmatched()] == ["AMB"]
    assert not pay.seen("REF")
    assert "Unmatched incoming payments" in texts.orders_text(pay.recent(), pay.unmatched())


# ---------------------------------------------------------------- RPC: version retry, no URL in logs
class FakeNet:
    def __init__(self, replies):
        self.replies, self.bodies = list(replies), []

    async def request(self, bucket, method, url, json=None, retries=4):
        self.bodies.append(json)
        return self.replies.pop(0)


async def test_rpc_retries_with_newer_version():
    net = FakeNet([{"jsonrpc": "2.0", "id": 1, "error": {"code": -32015, "message":
                   "Transaction version (1) is not supported by the requesting client."}},
                   {"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}}])
    rpc = PayRpc(net)
    assert await rpc.transaction("S") == {"ok": 1}
    assert [b["params"][1]["maxSupportedTransactionVersion"] for b in net.bodies] == [0, 1]
    assert all(b["params"][1]["commitment"] == "finalized" for b in net.bodies)


async def test_rpc_signatures_finalized():
    net = FakeNet([{"result": [{"signature": "A"}]}])
    assert await PayRpc(net).signatures("REF", limit=5, until="X") == [{"signature": "A"}]
    assert net.bodies[0]["params"] == ["REF", {"limit": 5, "commitment": "finalized", "until": "X"}]


# ---------------------------------------------------------------- config
def test_env_pay_settings(monkeypatch):
    env = {"MIMIC_PAY_ADDRESS": ADDR, "MIMIC_PRICE_PER_CREDIT_SOL": "0.002", "MIMIC_MIN_PURCHASE_SOL": "0.05",
           "MIMIC_MAX_PURCHASE_SOL": "2", "MIMIC_BUY_AMOUNTS": "0.05, 0.1"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    try:
        importlib.reload(config)
        assert config.PAY_ADDRESS == ADDR and not config.PAY_ADDRESS_INVALID
        assert (config.PRICE_LAMPORTS, config.MIN_PURCHASE_LAMPORTS, config.MAX_PURCHASE_LAMPORTS) == \
            (2_000_000, 50_000_000, 2 * SOL)
        assert config.BUY_QUICK_LAMPORTS == [50_000_000, 100_000_000]
        for bad in ("not-an-address", ADDR + "1", "0" * 44, b58encode(bytes([1] * 31))):
            monkeypatch.setenv("MIMIC_PAY_ADDRESS", bad)
            importlib.reload(config)
            assert config.PAY_ADDRESS == "" and config.PAY_ADDRESS_INVALID, bad
        monkeypatch.setenv("MIMIC_PRICE_PER_CREDIT_SOL", "garbage")
        importlib.reload(config)
        assert config.PRICE_LAMPORTS == 1_000_000
    finally:
        for k in env:
            monkeypatch.delenv(k, raising=False)
        importlib.reload(config)
    assert config.PAY_ADDRESS == "" and not config.PAY_ADDRESS_INVALID
    assert (config.PRICE_LAMPORTS, config.MIN_PURCHASE_LAMPORTS, config.MAX_PURCHASE_LAMPORTS) == \
        (1_000_000, 20_000_000, 5 * SOL)


# ---------------------------------------------------------------- bot: screens and payment message
class FakeTgBot:
    def __init__(self, reject_url=True):
        self.reject_url, self.photos, self.msgs = reject_url, [], []

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.photos.append((photo, caption))

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        btns = [b for row in reply_markup.inline_keyboard for b in row] if reply_markup else []
        if self.reject_url and any(b.url and b.url.startswith("solana:") for b in btns):
            raise BadRequest("Button_url_invalid")
        self.msgs.append((text, btns, kw))
        return object()


@pytest.fixture
def mbot():
    from mimic import bot as botmod
    return botmod.MimicBot(DB(":memory:"))


async def test_payment_message_falls_back_without_url_button(mbot):
    o = mbot.pay.create(1, 1, 50_000_000, now=T0)
    tg = FakeTgBot(reject_url=True)
    await mbot.send_payment(tg, 1, o)
    assert mbot.pay_url_button is False
    text, btns, kw = tg.msgs[-1]
    assert kw.get("disable_web_page_preview") is True
    assert not any(b.url for b in btns)
    assert {b.text for b in btns} >= {"📋 Copy address", "📋 Copy amount", "✅ I've paid", "✖️ Cancel"}
    assert f"<code>{ADDR}</code>" in text and "0.05 SOL" in text and "50 credits" in text
    assert "never asks for your seed phrase" in text and f"reference={o.reference}" in text
    assert tg.photos[0][0][:4] == b"\x89PNG"
    # next time the URL button isn't tried again
    tg2 = FakeTgBot(reject_url=True)
    await mbot.send_payment(tg2, 1, o)
    assert len(tg2.msgs) == 1


async def test_payment_message_keeps_url_button_if_accepted(mbot):
    o = mbot.pay.create(1, 1, 50_000_000, now=T0)
    tg = FakeTgBot(reject_url=False)
    await mbot.send_payment(tg, 1, o)
    assert mbot.pay_url_button is True
    assert tg.msgs[-1][1][0].url == o.url and "Pay with wallet" in tg.msgs[-1][0]


def test_buy_screen(mbot, monkeypatch):
    t, kb = mbot.buy_screen()
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert labels[:4] == ["0.02 SOL · 20 cr", "0.05 SOL · 50 cr", "0.1 SOL · 100 cr", "0.25 SOL · 250 cr"]
    assert "✏️ Custom amount" in labels
    assert "1 credit = 0.001 SOL" in t and "Minimum 0.02 SOL" in t and "Not financial advice" in t
    t, kb = mbot.buy_confirm(50_000_000)
    assert "0.05 SOL = 50 credits" in t and kb.inline_keyboard[0][0].callback_data == "buy:ok:50000000"
    monkeypatch.setattr(config, "PAY_ADDRESS", "")
    t, kb = mbot.buy_screen()
    assert "aren't available yet" in t and len(kb.inline_keyboard) == 1


class _Msg:
    def __init__(self, log):
        self.log = log

    async def reply_text(self, text, **kw):
        self.log.append(("reply", text, kw))

    async def edit_text(self, text, **kw):
        self.log.append(("edit", text, kw))


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _update(data, log, uid=1):
    async def answer(*a, **k):
        return None
    q = _Obj(data=data, message=_Msg(log), answer=answer)
    return _Obj(callback_query=q, effective_user=_Obj(id=uid), effective_chat=_Obj(id=uid),
                effective_message=_Msg(log))


async def test_bot_buy_flow_confirm_check_cancel(mbot):
    log, tg = [], FakeTgBot(reject_url=True)
    ctx = _Obj(bot=tg, user_data={})
    mbot.watcher.rpc = FakeRpc()
    mbot.watcher.scan_addr = False
    await mbot.on_button(_update("buy:a:50000000", log), ctx)
    assert "0.05 SOL = 50 credits" in log[-1][1]
    await mbot.on_button(_update("buy:ok:50000000", log), ctx)
    o = mbot.pay.open_order(1)
    assert o and o.lamports == 50_000_000 and tg.photos and tg.msgs
    await mbot.on_button(_update(f"pay:chk:{o.id}", log), ctx)
    assert "Not on the blockchain yet" in log[-1][1]
    await mbot.on_button(_update(f"pay:x:{o.id}", log, uid=2), ctx)    # not their order: ignored
    assert mbot.pay.get(o.id).status == "open"
    await mbot.on_button(_update(f"pay:x:{o.id}", log), ctx)
    assert mbot.pay.get(o.id).status == "cancelled" and "cancelled" in log[-1][1]
    # forged amounts below the minimum are refused server-side
    await mbot.on_button(_update("buy:ok:1000", log), ctx)
    assert "minimum" in log[-1][1] and mbot.pay.open_order(1) is None


async def test_bot_custom_amount(mbot):
    log = []
    ctx = _Obj(bot=FakeTgBot(), user_data={})
    await mbot.on_button(_update("buy:c", log), ctx)
    assert ctx.user_data["await"] == "buy_amount"
    upd = _update("", log)
    upd.effective_message.text = "0.001"
    await mbot.on_text(upd, ctx)
    assert "minimum is 0.02" in log[-1][1] and ctx.user_data["await"] == "buy_amount"
    upd.effective_message.text = "0,07"
    await mbot.on_text(upd, ctx)
    assert "0.07 SOL = 70 credits" in log[-1][1] and "await" not in ctx.user_data


async def test_address_scan_records_extra_payment_for_paid_order(watcher):
    pay, rpc = watcher.pay, watcher.rpc
    pay.state_set("cursor:" + ADDR, "")
    o = pay.create(1, 1, 50_000_000, now=T0)
    pay.apply_tx(o.id, "FIRST", TxCheck(True, 50_000_000), now=T0 + 30)
    rpc.sigs[ADDR] = [{"signature": "AGAIN", "err": None}]
    rpc.txs["AGAIN"] = mktx(reference=o.reference)
    assert await watcher.scan_address(ADDR, now=T0 + 90) == []
    u = pay.unmatched()
    assert [x["signature"] for x in u] == ["AGAIN"] and f"order #{o.id}" in u[0]["note"]
    assert bal(pay).bought == 50
