"""Telegram UI (buttons first). Long polling, so no open ports and no web server needed."""
from __future__ import annotations
import asyncio, logging, time
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as KB, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import config, texts
from .coinscan import coin_scan
from .db import DB
from .jobqueue import JobQueue
from .limits import KIND_NAMES, Limits
from .market import Market
from .net import Net
from .scan import Services, wallet_scan
from .sim import SimParams, build_trades, simulate
from .solana import Rpc, is_address, short

log = logging.getLogger("mimic.bot")


class UserError(Exception):
    """A failure we can explain to the user (still doesn't count against limits)."""


def menu_kb() -> KB:
    return KB([[B("🔍 Wallet Scan", callback_data="m:wallet"), B("🪙 Coin Scan", callback_data="m:coin")],
               [B("📊 My Limits", callback_data="m:limits"), B("💳 Credits", callback_data="m:credits")]])


def home_row() -> list:
    return [B("🏠 Menu", callback_data="m:home")]


class MimicBot:
    def __init__(self, db: DB | None = None):
        self.db = db or DB(config.DB_PATH)
        self.db.fail_stale_running()
        self.db.prune()
        self.net = Net()
        self.svc = Services(Rpc(self.net, self.db), Market(self.net, self.db))
        self.limits = Limits(self.db)
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
        """Limits → queue → work → log. `work(progress)` returns (text, keyboard)."""
        v = self.limits.check(user_id, kind)
        if not v.ok:
            if v.reason == "cooldown":
                asyncio.create_task(self._countdown(chat_id, ctx, v.wait_s, (kind, target)))
            elif v.reason == "busy":
                await self._send(chat_id, ctx, "⏳ You already have a check running. One at a time 🙂")
            else:
                h, m = v.wait_s // 3600, v.wait_s % 3600 // 60
                await self._send(chat_id, ctx,
                                 f"🚫 Free limit reached: {v.used}/{v.limit} {KIND_NAMES[kind]} today.\n"
                                 f"Resets in {h}h {m}m.", KB([[B("💳 Credits", callback_data="m:credits")], home_row()]))
            return
        cid = self.db.start_check(user_id, kind, target)
        msg = await self._send(chat_id, ctx, f"Check #{cid} ⏳ starting…")
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
        except UserError as e:
            self.db.finish_check(cid, False, str(e))
            await self._edit(msg, f"⚠️ {e}\n<i>Check #{cid} didn't count.</i>", KB([home_row()]))
            return
        except Exception as e:  # noqa: BLE001
            log.exception("check %s failed", cid)
            self.db.finish_check(cid, False, repr(e))
            await self._edit(msg, f"⚠️ Check #{cid} failed (free data sources are busy). It didn't count — "
                                  "try again in a minute.", KB([home_row()]))
            return
        self.db.finish_check(cid, True)
        parts = texts.split_message(f"{text}\n\nCheck #{cid} ✅ · {time.monotonic() - t0:.0f}s")
        await self._edit(msg, parts[0], kb if len(parts) == 1 else None)
        for i, part in enumerate(parts[1:], 2):
            await self._send(chat_id, ctx, part, kb if i == len(parts) else None)

    # ---------------------------------------------------------------- jobs
    def _wallet_work(self, wallet: str, ctx):
        async def work(progress):
            res = await self._get_scan(wallet, progress)
            ctx.user_data["last_wallet"] = wallet
            kb = KB([[B("🧪 Copytrade Sim", callback_data="sim:new")],
                     [B("🔁 Rescan", callback_data="ws:" + wallet)] + home_row()])
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

    def _coin_work(self, mint: str):
        async def work(progress):
            key = "coin3:" + mint
            res = self.db.cache_get(key)
            if res is None:
                try:
                    res = await coin_scan(self.svc, mint, progress)
                except ValueError as e:
                    raise UserError(str(e)) from e
                self.db.cache_set(key, res, config.CACHE_TTL_S)
            rows, row = [], []
            for i, t in enumerate(res["traders"], 1):
                row.append(B(f"🔍 {i}. {short(t['wallet'])}", callback_data="ws:" + t["wallet"]))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            if res.get("flagged_count"):
                rows.append([B("🚩 Show flagged", callback_data="cf:" + mint)])
            rows.append(home_row())
            return texts.coin_scan_text(res), KB(rows)
        return work

    def _sim_work(self, wallet: str, p: SimParams):
        async def work(progress):
            scan = await self._get_scan(wallet, progress)
            trades, sol = await build_trades(self.svc.market, scan, progress)
            if not trades:
                raise UserError("No coins with price data to simulate for this wallet.")
            res = simulate(trades, p, sol.at, now=scan["scanned_at"])
            kb = KB([[B("🧪 New sim", callback_data="sim:new"), B("🔍 Wallet", callback_data="ws:" + wallet)], home_row()])
            return texts.sim_text(wallet, p.label(), res.summary, p.delay_s), kb
        return work

    async def start_kind(self, update: Update, ctx, kind: str, target: str) -> None:
        chat, uid = update.effective_chat.id, update.effective_user.id
        if kind == "wallet":
            work = self._wallet_work(target, ctx)
        elif kind == "coin":
            work = self._coin_work(target)
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
                KB([[B("▶️ Run sim", callback_data="sim:run")], [B("✏️ Change", callback_data="sim:new")], home_row()]))

    # ---------------------------------------------------------------- handlers
    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ctx.user_data.pop("await", None)
        await update.effective_message.reply_text(
            "👋 <b>Mimic</b> — check if a Solana wallet is worth copying.\nPick an option:",
            parse_mode=ParseMode.HTML, reply_markup=menu_kb())

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
            await q.message.reply_text("Pick an option:", reply_markup=menu_kb())
        elif data == "m:wallet":
            ud["await"] = "wallet"
            await q.message.reply_text("🔍 Paste a Solana <b>wallet</b> address 👇", parse_mode=ParseMode.HTML)
        elif data == "m:coin":
            ud["await"] = "coin"
            await q.message.reply_text("🪙 Paste a coin's <b>mint</b> address 👇", parse_mode=ParseMode.HTML)
        elif data == "m:limits":
            await q.message.reply_text(texts.limits_text(self.limits.usage(update.effective_user.id), update.effective_user.id),
                                       parse_mode=ParseMode.HTML, reply_markup=KB([home_row()]))
        elif data == "m:credits":
            await q.message.reply_text("💳 Credits: coming soon. Free limits reset daily at 00:00 UTC.",
                                       reply_markup=KB([home_row()]))
        elif data in ("as:w", "as:c"):
            addr = ud.pop("pending_addr", None)
            if addr:
                await self.start_kind(update, ctx, "wallet" if data == "as:w" else "coin", addr)
        elif data.startswith("ws:"):
            await self.start_kind(update, ctx, "wallet", data[3:])
        elif data.startswith("cf:"):
            res = self.db.cache_get("coin3:" + data[3:])
            if res is None:
                await q.message.reply_text("That scan expired — run the Coin Scan again.", reply_markup=KB([home_row()]))
            else:
                parts = texts.split_message(texts.flagged_text(res))
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
            reply_markup=KB([[B("🔍 Wallet", callback_data="as:w"), B("🪙 Coin", callback_data="as:c")]]))

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Unhandled error: %s", ctx.error, exc_info=ctx.error)

    def build_app(self, token: str) -> Application:
        app = Application.builder().token(token).concurrent_updates(True).build()
        app.add_handler(CommandHandler(["start", "menu", "help"], self.cmd_start))
        app.add_handler(CallbackQueryHandler(self.on_button))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_error_handler(self.on_error)
        return app
