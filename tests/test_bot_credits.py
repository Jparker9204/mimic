"""run_check end to end with a fake Telegram: charging, refunds on failure, admin bypass, insufficient credits."""
import pytest
from mimic import bot as botmod
from mimic.credits import Credits
from mimic.db import DB
from mimic.limits import Limits
from mimic.solana import DataBusy


class FakeMsg:
    def __init__(self, log):
        self.log = log

    async def edit_text(self, text, **kw):
        self.log.append(text)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return FakeMsg(self.sent)


class FakeCtx:
    def __init__(self):
        self.bot = FakeBot()
        self.user_data = {}


@pytest.fixture
def mb(monkeypatch):
    db = DB(":memory:")
    b = botmod.MimicBot(db)
    b.limits = Limits(db, credits=Credits(db, refill=40, interval_h=6, cap=500),
                      costs={"wallet": 3, "coin": 10, "sim": 15}, cooldown_s=0, admins={999})
    return b


async def ok_work(progress):
    return "result", None


async def busy_work(progress):
    raise DataBusy("rate limited")


async def err_work(progress):
    raise RuntimeError("boom")


async def user_err_work(progress):
    raise botmod.UserError("No coins with price data")


async def test_success_charges_and_shows_balance(mb):
    ctx = FakeCtx()
    await mb.run_check(1, 1, ctx, "coin", "m", ok_work)
    assert mb.limits.credits.balance(1).total == 30
    assert "−10 credits · 💳 30 left" in ctx.bot.sent[-1]


@pytest.mark.parametrize("work", [busy_work, err_work, user_err_work])
async def test_failures_refund(mb, work):
    ctx = FakeCtx()
    await mb.run_check(1, 1, ctx, "sim", "w", work)
    assert mb.limits.credits.balance(1).total == 40
    assert "15 credits refunded" in ctx.bot.sent[-1]
    kinds = [r["kind"] for r in mb.limits.credits.ledger(1)]
    assert kinds[:2] == ["refund", "spend"]


async def test_insufficient_credits_friendly_message(mb):
    ctx = FakeCtx()
    await mb.run_check(1, 1, ctx, "sim", "w", ok_work)
    await mb.run_check(1, 1, ctx, "sim", "w", ok_work)   # 10 left
    ctx.bot.sent.clear()
    await mb.run_check(1, 1, ctx, "sim", "w", ok_work)
    msg = ctx.bot.sent[-1]
    assert "Not enough credits" in msg and "Next +40 in" in msg and "coming soon" in msg
    assert mb.limits.credits.balance(1).total == 10


async def test_admin_free(mb):
    ctx = FakeCtx()
    for _ in range(5):
        await mb.run_check(1, 999, ctx, "sim", "w", ok_work)
    assert mb.limits.credits.balance(999).total == 40
    assert "credits" not in ctx.bot.sent[-1].split("✅")[-1]
    assert all(r["kind"] != "spend" for r in mb.limits.credits.ledger(999))
