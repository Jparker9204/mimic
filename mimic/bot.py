"""Telegram UI (buttons first). Long polling, so no open ports and no web server needed."""
from __future__ import annotations
import asyncio
import secrets, logging, time
from telegram import BotCommand, InlineKeyboardButton as B, InlineKeyboardMarkup as KB, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import config, texts
from .coinscan import coin_scan
from .db import DB
from .filters import CYCLE, HOLDING_NAMES, PRESET_NAMES, PRESETS, SORT_NAMES, Filter, apply_filter
from .jobqueue import JobQueue
from .credits import InsufficientCredits
from .limits import Limits
from .market import Market
from .net import Net
from .scan import Services, wallet_scan
from .sim import SimParams, build_trades, simulate
from .solana import DataBusy, Rpc, is_address, short

log = logging.getLogger("mimic.bot")


class UserError(Exception):
    """A failure we can explain to the user (its credits are refunded)."""


def menu_kb() -> KB:
    cl = texts.cost_label
    return KB([[B(f"🔍 Wallet Scan · {cl('wallet')}", callback_data="m:wallet"),
                B(f"🪙 Coin Scan · {cl('coin')}", callback_data="m:coin")],
               [B("⚙️ Filters", callback_data="m:filters"), B("💳 Credits", callback_data="m:credits")]])


FILTER_PROMPTS = {
    "min_in": "✏️ Type the minimum SOL put in (e.g. 2)",
    "max_in": "✏️ Type the maximum SOL put in (0 = no max)",
    "min_profit": "✏️ Type the minimum profit in SOL (e.g. 1)",
    "min_pct": "✏️ Type the minimum profit in % (e.g. 50)",
    "max_rank": "✏️ Only the first N buyers: type N (e.g. 50, 0 = all)",
}


def home_row() -> list:
    return [B("🏠 Menu", callback_data="m:home")]


class MimicBot:
    def __init__(self, db: DB | None = None):
        self.db = db or DB(config.DB_PATH)
        self.limits = Limits(self.db)
        for cid in self.db.fail_stale_running():
            self.limits.refund(cid, note="interrupted by restart")
        self.db.prune()
        self.net = Net()
        self.svc = Services(Rpc(self.net, self.db), Market(self.net, self.db))
        self.queue = JobQueue(config.MAX_HEAVY_JOBS)

    # ---------------------------------------------------------------- helpers
    async def _edit(self, msg, text: str, kb: KB | None = None) -> None:
        try:
            await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.debug("edit failed: %s", e)
        except TelegramError as e:
            log.debug("edit failed: %s", e)

    async def _send(self, chat_id: int, ctx, text: str, kb: KB | None = None):
        return await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                          disable_web_page_preview=True)

    def balance_line(self, user_id: int) -> str:
        admin = self.limits.is_admin(user_id)
        b = None if admin else self.limits.credits.balance(user_id)
        return texts.balance_line(b, self.limits.credits, admin=admin)

    def menu_text(self, user_id: int, head: str = "Pick an option:") -> str:
        return f"{head}\n{self.balance_line(user_id)}"

    def credits_screen(self, user_id: int) -> str:
        admin = self.limits.is_admin(user_id)
        b = self.limits.credits.balance(user_id)
        return texts.credits_text(b, self.limits.credits, user_id, admin=admin)

    async def _countdown(self, chat_id: int, ctx, wait_s: int, retry: tuple) -> None:
        ctx.user_data["retry"] = retry
        msg = await self._send(chat_id, ctx, f"⏳ Cooldown: please wait <b>{wait_s}s</b>")
        end = time.monotonic() + wait_s
        while True:
            left = int(end - time.monotonic() + 0.999)
            if left <= 0:
                break
            await asyncio.sleep(min(3, left))
            left = int(end - time.monotonic() + 0.999)
            if left > 0:
                await self._edit(msg, f"⏳ Cooldown: please wait <b>{left}s</b>")
        await self._edit(msg, "✅ Ready!", KB([[B("▶️ Run it now", callback_data="retry")], home_row()]))

    async def run_check(self, chat_id: int, user_id: int, ctx, kind: str, target: str, work) -> None:
        """Credits/cooldown → charge → queue → work → log. `work(progress)` returns (text, keyboard).
        Any failure refunds the credits it took."""
        v = self.limits.check(user_id, kind)
        if not v.ok:
            if v.reason == "cooldown":
                asyncio.create_task(self._countdown(chat_id, ctx, v.wait_s, (kind, target)))
            elif v.reason == "busy":
                await self._send(chat_id, ctx, "⏳ You already have a check running. One at a time 🙂")
            else:
                await self._send(chat_id, ctx, texts.no_credits_text(kind, v.cost, v.balance, self.limits.credits,
                                                                     v.enough_in),
                                 KB([[B("💳 Credits", callback_data="m:credits")], home_row()]))
            return
        cid = self.db.start_check(user_id, kind, target)
        try:
            cost = self.limits.charge(user_id, kind, cid)
        except InsufficientCredits:
            self.db.finish_check(cid, False, "no credits")
            await self._send(chat_id, ctx, "😅 Not enough credits for that.", KB([[B("💳 Credits", callback_data="m:credits")]]))
            return

        def refund(note: str) -> str:
            n = self.limits.refund(cid, note=note)
            return f"Check #{cid} didn't count" + (f": {texts.cr(n)} refunded." if n else ".")

        paid = f" · {texts.cr(cost)}" if cost else ""
        try:
            msg = await self._send(chat_id, ctx, f"Check #{cid}{paid} ⏳ starting…")
        except Exception:
            self.db.finish_check(cid, False, "send failed")
            refund("message failed")
            raise
        last = [0.0]

        async def progress(text: str) -> None:
            if time.monotonic() - last[0] >= 3:
                last[0] = time.monotonic()
                await self._edit(msg, f"Check #{cid} · {text}")

        async def on_pos(pos: int) -> None:
            await self._edit(msg, f"Check #{cid} · 🕐 You're <b>#{pos}</b> in line…")

        t0 = time.monotonic()
        try:
            text, kb = await self.queue.run(lambda: work(progress), on_position=on_pos)
        except DataBusy as e:
            log.warning("check %s: data source busy (%s)", cid, e)
            self.db.finish_check(cid, False, "busy: " + str(e))
            note = refund("data source busy")
            await self._edit(msg, f"⚠️ The free data sources are busy right now, so I couldn't get complete data.\n"
                                  f"Please try again in a few minutes. <i>{note}</i>", KB([home_row()]))
            return
        except UserError as e:
            self.db.finish_check(cid, False, str(e))
            note = refund("no result: " + str(e))
            await self._edit(msg, f"⚠️ {e}\n<i>{note}</i>", KB([home_row()]))
            return
        except Exception as e:  # noqa: BLE001
            log.exception("check %s failed", cid)
            self.db.finish_check(cid, False, repr(e))
            note = refund("error")
            await self._edit(msg, f"⚠️ Check #{cid} failed (free data sources are busy). <i>{note}</i>\n"
                                  "Try again in a minute.", KB([home_row()]))
            return
        self.db.finish_check(cid, True)
        footer = f"Check #{cid} ✅ · {time.monotonic() - t0:.0f}s"
        if cost:
            footer += f" · −{texts.cr(cost)} · 💳 {self.limits.credits.balance(user_id).total} left"
        parts = texts.split_message(f"{text}\n\n{footer}")
        await self._edit(msg, parts[0], kb if len(parts) == 1 else None)
        for i, part in enumerate(parts[1:], 2):
            await self._send(chat_id, ctx, part, kb if i == len(parts) else None)

    # ---------------------------------------------------------------- jobs
    def _wallet_work(self, wallet: str, ctx):
        async def work(progress):
            res = await self._get_scan(wallet, progress)
            ctx.user_data["last_wallet"] = wallet
            kb = KB([[B("🏆 Best & worst trades", callback_data="wb:" + wallet)],
                     [B(f"🧪 Copytrade Sim · {texts.cost_label('sim')}", callback_data="sim:new")],
                     [B(f"🔁 Rescan · {texts.cost_label('wallet')}", callback_data="ws:" + wallet)] + home_row()])
            return texts.wallet_scan_text(res), kb
        return work

    async def _get_scan(self, wallet: str, progress) -> dict:
        key = "scan:" + wallet
        res = self.db.cache_get(key)
        if res is None:
            res = await wallet_scan(self.svc, wallet, progress)
            if not res["txs"]:
                raise UserError("Couldn't read this wallet's history right now (or it has no activity).")
            self.db.cache_set(key, res, config.CACHE_TTL_S)
        return res

    def get_filter(self, user_id: int) -> Filter:
        return Filter.from_dict(self.db.get_setting(user_id, "coin_filter"))

    def coin_view(self, res: dict, flt: Filter) -> tuple[str, KB]:
        """Render a cached coin scan with a filter. Pure: no API calls, no check used."""
        shown, _ = apply_filter(res.get("candidates") or [], flt, res.get("price_sol"))
        rows, row = [], []
        for i, t in enumerate(shown, 1):
            row.append(B(f"🔍 {i}. {short(t['wallet'])}", callback_data="ws:" + t["wallet"]))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        extra = [B("⚙️ Filters", callback_data="cflt:" + res["mint"])]
        if res.get("flagged_count"):
            extra.append(B("🚩 Show flagged", callback_data="cf:" + res["mint"]))
        rows.append(extra)
        rows.append(home_row())
        text = texts.coin_scan_text(res, flt)
        if shown:
            text += f"\n<i>Tap a wallet below to Wallet Scan it ({texts.cost_label('wallet')}).</i>"
        return text, KB(rows)

    def _coin_work(self, mint: str, user_id: int, ctx):
        async def work(progress):
            key = "coin5:" + mint
            res = self.db.cache_get(key)
            if res is None:
                try:
                    res = await coin_scan(self.svc, mint, progress)
                except ValueError as e:
                    raise UserError(str(e)) from e
                self.db.cache_set(key, res, config.CACHE_TTL_S)
            ctx.user_data["filter_mint"], ctx.user_data["filter_sym"] = mint, res.get("sym", "?")
            return self.coin_view(res, self.get_filter(user_id))
        return work

    def filters_screen(self, user_id: int, ud: dict) -> tuple[str, KB]:
        f = self.get_filter(user_id)
        from .filters import describe

        def pb(key):
            return B(("✅ " if f.preset == key else "") + PRESET_NAMES[key], callback_data="fp:" + key)
        mx = "no max" if f.max_in is None else f"{f.max_in:g} SOL"
        rows = [[pb("small"), pb("mid"), pb("whale"), pb("any")],
                [B(f"Min put in: {f.min_in:g} SOL", callback_data="fs:min_in"), B(f"Max put in: {mx}", callback_data="fs:max_in")],
                [B(f"Min profit: {f.min_profit:g} SOL", callback_data="fs:min_profit"),
                 B(f"Min profit: {f.min_pct:g}%", callback_data="fs:min_pct")],
                [B(f"Buyers: {'all' if not f.max_rank else f'first {f.max_rank} (of checked)'}", callback_data="fs:max_rank")],
                [B(f"Holding: {HOLDING_NAMES[f.holding]} 🔄", callback_data="fc:holding"),
                 B(f"Sort: {SORT_NAMES[f.sort]} 🔄", callback_data="fc:sort")]]
        if ud.get("filter_mint") and self.db.cache_get("coin5:" + ud["filter_mint"]) is not None:
            rows.append([B(f"✅ Show {ud.get('filter_sym', 'coin')} with this filter", callback_data="fa")])
        rows.append(home_row())
        text = (f"⚙️ <b>Coin Scan filters</b>\n{describe(f)}\n\nPick a preset, or tap a value to change it.\n"
                "<i>Saved for your next scans. Changing filters is free (no credits).</i>")
        return text, KB(rows)

    def _sim_work(self, wallet: str, p: SimParams):
        async def work(progress):
            scan = await self._get_scan(wallet, progress)
            trades, sol = await build_trades(self.svc.market, scan, progress)
            if not trades:
                raise UserError("No coins with price data to simulate for this wallet.")
            res = simulate(trades, p, sol.at, now=scan["scanned_at"])
            sim_id = secrets.token_hex(6)
            self.db.cache_set("simres:" + sim_id, {"wallet": wallet, "label": p.label(), "rows": [
                {k: r[k] for k in ("mint", "sym", "pnl_sol", "sol_in", "exit")} for r in res.rows]}, config.CACHE_TTL_S)
            kb = KB([[B("📊 Sim trades", callback_data="st:" + sim_id)],
                     [B("🧪 New sim", callback_data="sim:new"), B("🔍 Wallet", callback_data="ws:" + wallet)], home_row()])
            return texts.sim_text(wallet, p.label(), res.summary, p.delay_s), kb
        return work

    async def start_kind(self, update: Update, ctx, kind: str, target: str) -> None:
        chat, uid = update.effective_chat.id, update.effective_user.id
        if kind == "wallet":
            work = self._wallet_work(target, ctx)
        elif kind == "coin":
            work = self._coin_work(target, uid, ctx)
        else:
            p = SimParams(**ctx.user_data.get("sim_params", {}))
            work = self._sim_work(target, p)
        await self.run_check(chat, uid, ctx, kind, target, work)

    # ---------------------------------------------------------------- sim setup screens
    def _sim_screen(self, ctx) -> tuple[str, KB]:
        s = ctx.user_data.setdefault("sim_params", {})
        w = ctx.user_data.get("last_wallet", "")
        step = ctx.user_data.get("sim_step", "size")
        if step == "size":
            return (f"🧪 <b>Copytrade Sim</b> · {short(w)}\n<b>Step 1 of 3: how much SOL per trade?</b>",
                    KB([[B(x + " SOL", callback_data="sz:" + x) for x in ("0.1", "0.2", "0.5", "1")],
                        [B("✏️ Custom amount", callback_data="sz:c")], home_row()]))
        if step == "tp":
            return ("<b>Step 2 of 3: take profit at?</b>",
                    KB([[B("+25%", callback_data="tp:1.25"), B("+50%", callback_data="tp:1.5"),
                         B("2x", callback_data="tp:2"), B("No take profit", callback_data="tp:0")], home_row()]))
        if step == "exit":
            return ("<b>Step 3 of 3: otherwise, when to sell?</b>",
                    KB([[B("🪞 When the wallet sells", callback_data="ex:m")],
                        [B("🛑 Stop-loss -30%", callback_data="ex:sl30"), B("✏️ Custom stop-loss", callback_data="ex:c")],
                        home_row()]))
        p = SimParams(**s)
        return (f"🧪 <b>Ready to run</b>\n{p.label()}\n<i>Assumes ~{p.delay_s}s copy delay.</i>",
                KB([[B(f"▶️ Run sim · {texts.cost_label('sim')}", callback_data="sim:run")], [B("✏️ Change", callback_data="sim:new")], home_row()]))

    # ---------------------------------------------------------------- handlers
    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ctx.user_data.pop("await", None)
        await update.effective_message.reply_text(
            self.menu_text(update.effective_user.id, "👋 <b>Mimic</b> — check if a Solana wallet is worth copying.\n"
                                                     "Pick an option:"),
            parse_mode=ParseMode.HTML, reply_markup=menu_kb())

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ctx.user_data.pop("await", None)
        await update.effective_message.reply_text(texts.help_text(), parse_mode=ParseMode.HTML, reply_markup=menu_kb())

    async def cmd_credits(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(self.credits_screen(update.effective_user.id),
                                                  parse_mode=ParseMode.HTML, reply_markup=KB([home_row()]))

    async def cmd_grant(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Admin only: /grant <telegram_id> <amount> [note]. Adds (or removes, if negative) purchased credits."""
        uid = update.effective_user.id
        if not self.limits.is_admin(uid):
            await update.effective_message.reply_text("This command is for admins only.")
            return
        args = ctx.args or []
        try:
            target, amount = int(args[0]), int(args[1])
            if amount == 0:
                raise ValueError
        except (IndexError, ValueError):
            await update.effective_message.reply_text(
                "Usage: /grant <telegram_id> <amount> [note]\nAmount can be negative to take purchased credits back.")
            return
        note = " ".join(args[2:])[:200] or "admin grant"
        try:
            b = self.limits.credits.grant(target, amount, actor=uid, note=note)
        except InsufficientCredits as e:
            await update.effective_message.reply_text(f"Not done: {e}.")
            return
        log.info("admin %s granted %+d purchased credits to %s", uid, amount, target)
        await update.effective_message.reply_text(
            f"✅ {amount:+d} purchased credits for <code>{target}</code>.\n"
            + texts.balance_line(b, self.limits.credits), parse_mode=ParseMode.HTML)

    async def cmd_ledger(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Admin only: /ledger <telegram_id> shows the latest credit changes (for refund disputes)."""
        if not self.limits.is_admin(update.effective_user.id):
            await update.effective_message.reply_text("This command is for admins only.")
            return
        try:
            target = int((ctx.args or [])[0])
        except (IndexError, ValueError):
            await update.effective_message.reply_text("Usage: /ledger <telegram_id>")
            return
        rows = self.limits.credits.ledger(target, 20)
        await update.effective_message.reply_text(texts.ledger_text(target, rows), parse_mode=ParseMode.HTML)

    async def on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        data = q.data or ""
        try:
            await q.answer()
        except TelegramError:
            pass
        ud = ctx.user_data
        if data == "m:home":
            ud.pop("await", None)
            await q.message.reply_text(self.menu_text(update.effective_user.id), parse_mode=ParseMode.HTML,
                                       reply_markup=menu_kb())
        elif data == "m:wallet":
            ud["await"] = "wallet"
            await q.message.reply_text(f"🔍 Paste a Solana <b>wallet</b> address 👇\n<i>Costs {texts.cost_label('wallet')}"
                                       f" · {self.balance_line(update.effective_user.id)}</i>", parse_mode=ParseMode.HTML)
        elif data == "m:coin":
            ud["await"] = "coin"
            await q.message.reply_text(f"🪙 Paste a coin's <b>mint</b> address 👇\n<i>Costs {texts.cost_label('coin')}"
                                       f" · {self.balance_line(update.effective_user.id)}</i>", parse_mode=ParseMode.HTML)
        elif data in ("m:credits", "m:limits"):   # m:limits = old "My Limits" button on earlier messages
            await q.message.reply_text(self.credits_screen(update.effective_user.id),
                                       parse_mode=ParseMode.HTML, reply_markup=KB([home_row()]))
        elif data in ("as:w", "as:c"):
            addr = ud.pop("pending_addr", None)
            if addr:
                await self.start_kind(update, ctx, "wallet" if data == "as:w" else "coin", addr)
        elif data.startswith("ws:"):
            await self.start_kind(update, ctx, "wallet", data[3:])
        elif data == "m:filters" or data.startswith("cflt:"):
            if data.startswith("cflt:"):
                ud["filter_mint"] = data[5:]
            t, kb = self.filters_screen(update.effective_user.id, ud)
            await q.message.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=kb)
        elif data[:3] in ("fp:", "fc:", "fs:"):
            uid = update.effective_user.id
            f = self.get_filter(uid)
            k, v = data.split(":", 1)
            if k == "fs":
                if v in FILTER_PROMPTS:
                    ud["await"] = "flt:" + v
                    await q.message.reply_text(FILTER_PROMPTS[v])
                return
            if k == "fp" and v in PRESETS:
                f = PRESETS[v]
            elif k == "fc" and v in CYCLE:
                opts = CYCLE[v]
                f = f.set(v, opts[(opts.index(getattr(f, v)) + 1) % len(opts)])
            self.db.set_setting(uid, "coin_filter", f.to_dict())
            t, kb = self.filters_screen(uid, ud)
            await self._edit(q.message, t, kb)
        elif data == "fa":
            mint = ud.get("filter_mint")
            res = self.db.cache_get("coin5:" + mint) if mint else None
            if res is None:
                await q.message.reply_text("That scan expired — run the Coin Scan again.", reply_markup=KB([home_row()]))
                return
            t, kb = self.coin_view(res, self.get_filter(update.effective_user.id))
            parts = texts.split_message(t)
            for i, part in enumerate(parts, 1):
                await q.message.reply_text(part, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                           reply_markup=kb if i == len(parts) else None)
        elif data.startswith("cf:"):
            res = self.db.cache_get("coin5:" + data[3:])
            if res is None:
                await q.message.reply_text("That scan expired — run the Coin Scan again.", reply_markup=KB([home_row()]))
            else:
                parts = texts.split_message(texts.flagged_text(res))
                for i, part in enumerate(parts, 1):
                    await q.message.reply_text(part, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                               reply_markup=KB([home_row()]) if i == len(parts) else None)
        elif data.startswith("wb:") or data.startswith("st:"):
            # free views of cached results: no check, no API calls
            if data.startswith("wb:"):
                res = self.db.cache_get("scan:" + data[3:])
                t = texts.best_worst_text(res) if res else None
                gone = "That scan expired — run the Wallet Scan again."
            else:
                res = self.db.cache_get("simres:" + data[3:])
                t = texts.sim_trades_text(res) if res else None
                gone = "That sim expired — run it again."
            if t is None:
                await q.message.reply_text(gone, reply_markup=KB([home_row()]))
            else:
                parts = texts.split_message(t)
                for i, part in enumerate(parts, 1):
                    await q.message.reply_text(part, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                               reply_markup=KB([home_row()]) if i == len(parts) else None)
        elif data == "retry":
            r = ud.pop("retry", None)
            if r:
                await self.start_kind(update, ctx, r[0], r[1])
        elif data == "sim:new":
            if not ud.get("last_wallet"):
                await q.message.reply_text("Scan a wallet first 🔍", reply_markup=menu_kb())
                return
            ud["sim_params"], ud["sim_step"] = {}, "size"
            t, kb = self._sim_screen(ctx)
            await q.message.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=kb)
        elif data == "sim:run":
            if ud.get("last_wallet"):
                await self.start_kind(update, ctx, "sim", ud["last_wallet"])
        elif data[:3] in ("sz:", "tp:", "ex:"):
            await self._sim_choice(update, ctx, data)

    async def _sim_choice(self, update: Update, ctx, data: str) -> None:
        ud = ctx.user_data
        s = ud.setdefault("sim_params", {})
        k, v = data.split(":", 1)
        if k == "sz":
            if v == "c":
                ud["await"] = "custom_size"
                await update.callback_query.message.reply_text("✏️ Type a size in SOL (e.g. 0.3)")
                return
            s["size_sol"], ud["sim_step"] = float(v), "tp"
        elif k == "tp":
            s["tp"], ud["sim_step"] = (float(v) or None), "exit"
        elif k == "ex":
            if v == "c":
                ud["await"] = "custom_sl"
                await update.callback_query.message.reply_text("✏️ Type a stop-loss % (e.g. 20 for -20%)")
                return
            s["exit"] = "mirror" if v == "m" else "sl"
            s["sl"] = 0.30
            ud["sim_step"] = "ready"
        t, kb = self._sim_screen(ctx)
        await self._edit(update.callback_query.message, t, kb)

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ud = ctx.user_data
        txt = (update.effective_message.text or "").strip()
        mode = ud.get("await")
        if mode and mode.startswith("flt:"):
            key = mode[4:]
            try:
                val = float(txt.replace(",", ".").rstrip("%").replace("SOL", "").strip())
                if val < 0 or val > 1e7:
                    raise ValueError
            except ValueError:
                await update.effective_message.reply_text("Please type a number (e.g. 5).")
                return
            uid = update.effective_user.id
            self.db.set_setting(uid, "coin_filter", self.get_filter(uid).set(key, val).to_dict())
            ud.pop("await", None)
            t, kb = self.filters_screen(uid, ud)
            await update.effective_message.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        if mode in ("custom_size", "custom_sl"):
            try:
                val = float(txt.replace(",", ".").rstrip("%").lstrip("-"))
            except ValueError:
                val = -1
            s = ud.setdefault("sim_params", {})
            if mode == "custom_size":
                if not 0.01 <= val <= 100:
                    await update.effective_message.reply_text("Please type a number between 0.01 and 100.")
                    return
                s["size_sol"], ud["sim_step"] = val, "tp"
            else:
                if not 1 <= val <= 95:
                    await update.effective_message.reply_text("Please type a number between 1 and 95.")
                    return
                s["exit"], s["sl"], ud["sim_step"] = "sl", val / 100, "ready"
            ud.pop("await", None)
            t, kb = self._sim_screen(ctx)
            await update.effective_message.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        addr = txt.split()[0] if txt else ""
        if not is_address(addr):
            await update.effective_message.reply_text("That doesn't look like a Solana address. Pick an option:",
                                                      reply_markup=menu_kb())
            return
        if mode in ("wallet", "coin"):
            ud.pop("await", None)
            await self.start_kind(update, ctx, mode, addr)
            return
        ud["pending_addr"] = addr
        await update.effective_message.reply_text(
            f"Is <code>{short(addr)}</code> a wallet or a coin?", parse_mode=ParseMode.HTML,
            reply_markup=KB([[B(f"🔍 Wallet · {texts.cost_label('wallet')}", callback_data="as:w"),
                              B(f"🪙 Coin · {texts.cost_label('coin')}", callback_data="as:c")]]))

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Unhandled error: %s", ctx.error, exc_info=ctx.error)

    async def _post_init(self, app: Application) -> None:
        try:
            await app.bot.set_my_commands([BotCommand("menu", "Main menu"), BotCommand("credits", "Your credits"),
                                           BotCommand("help", "How Mimic works")])
        except TelegramError as e:
            log.warning("set_my_commands failed: %s", e)

    def build_app(self, token: str) -> Application:
        app = Application.builder().token(token).concurrent_updates(True).post_init(self._post_init).build()
        app.add_handler(CommandHandler(["start", "menu"], self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler(["credits", "balance"], self.cmd_credits))
        app.add_handler(CommandHandler("grant", self.cmd_grant))
        app.add_handler(CommandHandler("ledger", self.cmd_ledger))
        app.add_handler(CallbackQueryHandler(self.on_button))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_error_handler(self.on_error)
        return app
