"""Short, plain message formatting (Telegram HTML)."""
from __future__ import annotations
import html, time


def _p(v) -> str:
    return "–" if v is None else f"{v}%"


def ordinal(n: int) -> str:
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def addr(a: str) -> str:
    """Full address in monospace: tap-to-copy in Telegram."""
    return f"<code>{html.escape(a)}</code>"


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Split on line breaks so each part fits Telegram's 4096-char limit (never inside a line/tag)."""
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                       # pathological single line
            parts.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def wallet_scan_text(res: dict) -> str:
    s = res["stats"]
    cov_days = (res["scanned_at"] - res["covered_from"]) / 86400
    period = f"Last {res['days']} days"
    if res["capped"]:
        period += f" (could only read the newest {cov_days:.1f} days)"
    head = [f"🔍 <b>Wallet Scan</b>", addr(res["wallet"]), f"📅 {period}"]
    if s["tokens"] == 0:
        return "\n".join(head + ["", "No coin buys found in this period."])
    net = s["sol_out"] - s["sol_in"]
    hold = "unknown (no sells yet)" if s["median_hold_min"] is None else f"{s['median_hold_min']:g} min"
    charted = f" (of {s['priced']} coins with charts)" if s["priced"] < s["tokens"] else ""
    lines = head + [
        "",
        f"🪙 Coins bought: <b>{s['tokens']}</b> ({s['buys']} buys, about {s['buys_per_token']:g} per coin)",
        f"🎯 Coins that went up after the buy{charted}:",
        f"   +25%: <b>{_p(s['hit25'])}</b> · +50%: <b>{_p(s['hit50'])}</b> · 2x: <b>{_p(s['hit2x'])}</b>",
        f"📊 Today: 🟢 up {_p(s['up_now'])} · 🔴 down {_p(s['down_now'])} · 💀 dead {_p(s['dead'])}",
        f"💰 SOL spent: {s['sol_in']:.2f} · Got back from sells: {s['sol_out']:.2f} ({net:+.2f})",
        f"⏱ Typical hold time: <b>{hold}</b>",
        "",
        s["copyability"],
    ]
    return "\n".join(lines)


def sim_text(wallet: str, label: str, summ: dict, delay_s: int = 15) -> str:
    sign = "🟢" if summ["net_sol"] > 0 else "🔴"
    lines = [
        "🧪 <b>Copytrade Sim</b>",
        addr(wallet),
        html.escape(label),
        "",
        f"{sign} Result: <b>{summ['net_sol']:+.3f} SOL ({summ['net_pct']:+.1f}%)</b>",
        f"Trades: {summ['trades']} (✅ {summ['wins']} wins · ❌ {summ['losses']} losses)",
        f"Put in: {summ['sol_in']:.3f} SOL · Got back: {summ['sol_out']:.3f} SOL",
        f"Worst trade: {summ['worst']:+.3f} SOL · Best trade: {summ['best']:+.3f} SOL",
    ]
    if summ.get("skipped"):
        lines.append(f"<i>{summ['skipped']} coins skipped (no price data)</i>")
    lines.append(f"<i>Assumes ~{delay_s}s copy delay, 2% slippage, 1% fee each way, 0.0005 SOL per trade. "
                 "Copies each coin's first buy only. Past results ≠ future.</i>")
    return "\n".join(lines)


def ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def coin_scan_text(res: dict, flt=None) -> str:
    from .filters import Filter, apply_filter, describe
    flt = flt or Filter()
    rows_data, total = apply_filter(res.get("candidates") or [], flt, res.get("price_sol"))
    head = (f"🪙 <b>Coin Scan · {html.escape(res['sym'])}</b>\n{addr(res['mint'])}\n"
            f"<i>{html.escape(describe(flt))}</i>\n<b>Top traders</b>")
    rows = []
    launch = res.get("launch_ts")
    for i, t in enumerate(rows_data, 1):
        if launch and t.get("first_t"):
            bought = f"{ago(t['first_t'] - launch)} after launch"
        elif t.get("first_t"):
            bought = time.strftime("%b %d %H:%M UTC", time.gmtime(t["first_t"]))
        else:
            bought = "unknown"
        rows += [f"{i}. Profit: <b>{t['profit_sol']:+.2f} SOL ({t['profit_pct']:+d}%)</b>",
                 f"   Put in: {t['sol_in']:.2f} SOL · Still holding: {t['holding_pct']}%",
                 f"   Bought: {bought}",
                 f"   {addr(t['wallet'])}", ""]
    default = flt.preset == "any"
    if total == 0:
        rows = ["No checked wallet made real profit on this coin (at least 0.1 SOL and 10%)." if default
                else "No checked wallets match this filter. Try ⚙️ Filters.", ""]
    elif total < 10:
        rows.append(f"Only {total} wallet{'s' if total != 1 else ''} "
                    + ("made real profit on this coin." if default else ("match" if total != 1 else "matches")
                       + " this filter."))
    elif total > 10:
        rows.append(f"Showing top 10 of {total} matching wallets.")
    extra = [f"🔎 Checked {res.get('checked', 0)} of {res.get('wanted', 0)} wallets "
             f"(top holders + recent big traders), each with its full history on this coin."]
    if res.get("flagged_count"):
        extra.append(f"🚩 Hid {res['flagged_count']} bundle/transfer wallet{'s' if res['flagged_count'] != 1 else ''}")
    skipped = []
    if res.get("very_active"):
        skipped.append(f"{res['very_active']} very active (bots?)")
    if res.get("unchecked") or res.get("history_failed"):
        skipped.append(f"{res.get('unchecked', 0) + res.get('history_failed', 0)} not read in time")
    if res.get("unvalued"):
        skipped.append(f"{res['unvalued']} paid with tokens we can't price")
    if skipped:
        extra.append("⏭ Skipped: " + ", ".join(skipped))
    if res.get("holders_failed"):
        extra.append("<i>Top-holder list was unavailable, so only recent traders were checked.</i>")
    if not res.get("priced", True):
        extra.append("<i>No current price, so profit counts sells only.</i>")
    extra.append("<i>Wallets that sold out before today's trades may be missed.</i>")
    return "\n".join([head, ""] + rows + extra)


def flagged_text(res: dict) -> str:
    fl = res.get("flagged") or []
    if not fl:
        return "🚩 No flagged wallets for this coin."
    rows = [f"🚩 <b>Flagged wallets · {html.escape(res['sym'])}</b>", ""]
    for i, t in enumerate(fl, 1):
        rows += [f"{i}. 🚩 {html.escape(', '.join(t.get('reasons') or []))}",
                 f"   Put in: {t['sol_in']:.2f} SOL · Got back: {t['sol_out']:.2f} SOL",
                 f"   {addr(t['wallet'])}", ""]
    if res.get("flagged_count", 0) > len(fl):
        rows.append(f"<i>…and {res['flagged_count'] - len(fl)} more</i>")
    return "\n".join(rows)


# ---------------------------------------------------------------- credits
KIND_LABELS = {"wallet": "Wallet Scan", "coin": "Coin Scan", "sim": "Copytrade Sim"}
KIND_EMOJI = {"wallet": "🔍", "coin": "🪙", "sim": "🧪"}


def wait_text(seconds: int) -> str:
    s = max(0, int(seconds))
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60}m"
    if s >= 60:
        return f"{s // 60}m"
    return f"{s}s"


def cr(n: int) -> str:
    return f"{n} credit{'' if n == 1 else 's'}"


def cost_label(kind: str, costs: dict | None = None) -> str:
    """'3 cr' style suffix for buttons."""
    from . import config
    c = (costs or config.COSTS).get(kind, 0)
    return "free" if c == 0 else f"{c} cr"


def balance_line(b, credits, now: float | None = None, admin: bool = False) -> str:
    if admin:
        return "💳 Credits: <b>unlimited</b> (admin)"
    line = f"💳 Credits: <b>{b.total}</b> (free {b.free}/{credits.cap}, bought {b.bought})."
    nxt = credits.next_refill_in(b, now)
    if nxt is None:
        line += " Free credits are full."
    else:
        line += f" Next +{credits.refill} in {wait_text(nxt)}"
    return line


def buy_note() -> str:
    """One line for /credits and the out-of-credits message."""
    from . import config
    from .payments import fmt_sol
    if not config.PAY_ADDRESS:
        return "💎 Buying credits with SOL isn't available yet."
    return (f"💎 Buy credits with SOL: 1 credit = {fmt_sol(config.PRICE_LAMPORTS)} SOL "
            f"(minimum {fmt_sol(config.MIN_PURCHASE_LAMPORTS)} SOL). Tap 💎 Buy credits.")


TERMS_SHORT = ("<i>Credits are non-refundable, except failed checks (refunded automatically) or by admin decision. "
               "Not financial advice. /terms</i>")


def buy_text() -> str:
    from . import config
    from .payments import credits_for, fmt_sol
    if not config.PAY_ADDRESS:
        return ("💎 <b>Buy credits</b>\nPayments aren't available yet. You still get free credits every "
                f"{config.REFILL_HOURS:g}h.")
    p = config.PRICE_LAMPORTS
    return "\n".join([
        "💎 <b>Buy credits with SOL</b>",
        f"Rate: <b>1 credit = {fmt_sol(p)} SOL</b> (1 SOL = {credits_for(10 ** 9, p)} credits)",
        f"Minimum {fmt_sol(config.MIN_PURCHASE_LAMPORTS)} SOL · maximum {fmt_sol(config.MAX_PURCHASE_LAMPORTS)} SOL",
        "Bought credits never expire. Pick an amount:",
        "",
        TERMS_SHORT,
    ])


def buy_confirm_text(lamports: int) -> str:
    from . import config
    from .payments import credits_for, fmt_sol
    return "\n".join([
        f"💎 <b>{fmt_sol(lamports)} SOL = {credits_for(lamports)} credits</b>",
        f"Next you'll get a payment request for exactly {fmt_sol(lamports)} SOL, valid for "
        f"{config.ORDER_TTL_S // 60} minutes. Nothing is charged until you send it from your wallet.",
    ])


def payment_text(o, button: bool) -> str:
    """The payment request (sent right after the QR code)."""
    from . import config
    from .payments import fmt_sol
    amt = fmt_sol(o.lamports)
    how = ("📱 <b>Phone:</b> tap <b>Pay with wallet</b> (opens Phantom/Solflare), or copy the address and amount "
           "into your wallet's Send screen." if button else
           "📱 <b>Phone:</b> copy the address and the amount into your wallet's Send screen (Phantom, Solflare…).")
    return "\n".join([
        f"💎 <b>Pay {amt} SOL → get {o.credits} credits</b>",
        "",
        f"Send exactly <b>{amt} SOL</b> to Mimic's wallet:",
        f"<code>{o.recipient}</code>",
        "",
        how,
        "💻 <b>Computer:</b> scan the QR code above with your phone's wallet app.",
        "Solana Pay link (paste into a wallet that supports it):",
        f"<code>{html.escape(o.url)}</code>",
        "",
        f"⏱ Valid for {config.ORDER_TTL_S // 60} minutes. Credits arrive automatically about 30–60 seconds after "
        "you pay, and I'll message you.",
        "",
        "🔒 This is a normal SOL transfer to Mimic's wallet. Mimic never asks for your seed phrase or to "
        "connect your wallet.",
        f"<i>Order #{o.id} · Non-refundable except failed checks or by admin decision. Not financial advice.</i>",
    ])


def qr_caption(o) -> str:
    from .payments import fmt_sol
    return f"Scan with your wallet app to pay {fmt_sol(o.lamports)} SOL (order #{o.id})"


def tx_link(sig: str) -> str:
    return f'<a href="https://solscan.io/tx/{sig}">{sig[:4]}…{sig[-4:]}</a>'


def paid_text(ev) -> str:
    from .payments import fmt_sol
    line = f"✅ Payment received: {fmt_sol(ev.received)} SOL → {cr(ev.credits)}. Tx: {tx_link(ev.signature)}"
    if ev.late:
        line += "\n<i>It arrived after the payment request expired; it still counts.</i>"
    return line


def short_text(ev) -> str:
    from .payments import fmt_sol, pay_url
    o = ev.order
    rest = pay_url(o.recipient, ev.missing, o.reference)
    return "\n".join([
        f"⚠️ Received <b>{fmt_sol(ev.received)} SOL</b> of {fmt_sol(o.lamports)} SOL for order #{o.id} "
        f"(tx {tx_link(ev.signature)}). <b>{fmt_sol(ev.missing)} SOL</b> is still missing.",
        f"Send the remaining {fmt_sol(ev.missing)} SOL to <code>{o.recipient}</code>, or use this link:",
        f"<code>{html.escape(rest)}</code>",
        "Your credits are added as soon as the full amount has arrived.",
    ])


def terms_text() -> str:
    return "\n".join([
        "📄 <b>Terms</b>",
        "• Credits pay for Mimic checks. Bought credits never expire.",
        "• Credits are non-refundable, except: a failed check refunds its credits automatically, and an admin "
        "can decide on a refund.",
        "• Payments are normal SOL transfers on Solana and can't be reversed. Check the amount before you send.",
        "• Mimic never asks for your seed phrase or private key and never connects to your wallet.",
        "• Mimic shows past on-chain data. Not financial advice.",
    ])


ORDER_ICONS = {"open": "🕐", "paid": "✅", "expired": "⌛", "cancelled": "✖️", "replaced": "↪️"}


def orders_text(orders: list, unmatched: list[dict]) -> str:
    from .payments import fmt_sol
    lines = ["🧾 <b>Recent orders</b> (newest first, UTC)"]
    if not orders:
        lines.append("No orders.")
    for o in orders:
        when = time.strftime("%b %d %H:%M", time.gmtime(o.created_at))
        line = (f"#{o.id} {when} · <code>{o.user_id}</code> · {fmt_sol(o.lamports)} SOL → {o.credits} cr · "
                f"{ORDER_ICONS.get(o.status, '')} {o.status}")
        if o.status == "paid":
            line += f" {fmt_sol(o.received_lamports)} SOL, +{o.credited} cr, tx {tx_link(o.signature)}"
        elif o.received_lamports:
            line += f" · got {fmt_sol(o.received_lamports)} SOL (short)"
        lines.append(line)
    if unmatched:
        lines += ["", "<b>Unmatched incoming payments</b> (no reference; credit with /grant if you can tell whose):"]
        for u in unmatched:
            when = time.strftime("%b %d %H:%M", time.gmtime(u["block_time"])) if u.get("block_time") else "?"
            lines.append(f"{when} · {fmt_sol(u['lamports'])} SOL · tx {tx_link(u['signature'])}")
    return "\n".join(lines)


def costs_lines() -> list[str]:
    from . import config
    return [f"{KIND_EMOJI[k]} {KIND_LABELS[k]}: {cr(config.COSTS[k])}" for k in ("coin", "wallet", "sim")]


def credits_text(b, credits, user_id: int, now: float | None = None, admin: bool = False) -> str:
    hours = credits.interval_s / 3600
    lines = [balance_line(b, credits, now, admin), "", "<b>Costs</b>"] + costs_lines() + [
        "🏆 Best &amp; worst trades, 📊 Sim trades, ⚙️ Filters: free",
        "",
        f"🎁 You get <b>{credits.refill}</b> free credits every {hours:g}h, up to {credits.cap}.",
        "Free credits are used first, then bought ones (bought credits never expire).",
        "If a check fails, its credits come back automatically.",
        buy_note(),
        f"Your ID: <code>{user_id}</code>",
    ]
    return "\n".join(lines)


def no_credits_text(kind: str, cost: int, b, credits, enough_in: int | None, now: float | None = None) -> str:
    lines = [f"😅 Not enough credits for a {KIND_LABELS[kind]}: it costs <b>{cost}</b>, you have <b>{b.total}</b>.",
             balance_line(b, credits, now)]
    nxt = credits.next_refill_in(b, now)
    if enough_in and nxt is not None and enough_in > nxt:
        lines.append(f"You'll have enough in about {wait_text(enough_in)}.")
    lines += ["", buy_note()]
    return "\n".join(lines)


def ledger_text(user_id: int, rows: list[dict]) -> str:
    lines = [f"📒 <b>Credit ledger</b> · <code>{user_id}</code> (newest first, UTC)"]
    if not rows:
        lines.append("No entries.")
    for r in rows:
        when = time.strftime("%b %d %H:%M", time.gmtime(r["ts"]))
        parts = []
        if r["delta_free"]:
            parts.append(f"free {r['delta_free']:+d}")
        if r["delta_bought"]:
            parts.append(f"bought {r['delta_bought']:+d}")
        extra = ""
        if r.get("check_id"):
            extra += f" · check #{r['check_id']}"
        if r.get("note"):
            extra += " · " + html.escape(str(r["note"]))[:80]
        lines.append(f"#{r['id']} {when} <b>{r['kind']}</b> {', '.join(parts) or '0'} → "
                     f"{r['free_after']}/{r['bought_after']}{extra}")
    return "\n".join(lines)


def help_text() -> str:
    from . import config
    hours = config.REFILL_HOURS
    return "\n".join([
        "👋 <b>Mimic</b> checks if a Solana wallet is worth copying. Everything is buttons; just paste an address.",
        "",
        f"🔍 <b>Wallet Scan</b>: the wallet's last {config.SCAN_DAYS} days of coin buys, hit rates and hold times.",
        "🪙 <b>Coin Scan</b>: real traders who made profit on a coin.",
        "🧪 <b>Copytrade Sim</b>: what copying the wallet would have made.",
        "🏆 Best &amp; worst trades and 📊 Sim trades are free extras on a result.",
        "",
        "<b>💳 Credits</b>",
    ] + costs_lines() + [
        f"🎁 {config.FREE_REFILL} free credits every {hours:g}h (up to {config.FREE_CAP}).",
        f"Failed checks are refunded automatically. Repeats within {config.CACHE_TTL_S // 60} min are fast but still cost credits.",
        f"There's a {config.COOLDOWN_S}s cooldown between checks.",
        "",
        "/credits: your balance · /buy: buy credits with SOL · /terms · /menu: main menu",
        "<i>Read-only: Mimic never trades and never asks for keys. Not financial advice.</i>",
    ])


# ---------------------------------------------------------------- best & worst trades
def _luck_lines(lk: dict, what: str) -> list[str]:
    if lk["n"] == 0:
        return []
    if lk["share"] is None:
        out = [f"🎲 Luck check: no overall profit ({lk['total']:+.2f} SOL)."]
    elif lk["share"] > 100:
        out = [f"🎲 Luck check: best {what} made {lk['best']:+.2f} SOL, more than the whole total "
               f"({lk['total']:+.2f} SOL). The rest lost."]
    else:
        out = [f"🎲 Luck check: best {what} = {lk['share']}% of total profit"]
    if lk["n"] >= 2:
        out.append(f"Without its best trade: <b>{lk['without_best']:+.2f} SOL</b>")
        if lk["total"] > 0 and lk["without_best"] <= 0:
            out.append(f"⚠️ The profit comes from one {what}. Could be luck.")
        elif lk["share"] is not None and lk["share"] <= 50 and lk["without_best"] > 0:
            out.append(f"✅ Profit is spread over several {what}s.")
    return out


def best_worst_text(scan: dict) -> str:
    from .trades import best_worst, coin_profits, luck
    rows, unpriced = coin_profits(scan)
    best, worst = best_worst(rows, "profit_sol")
    lines = ["🏆 <b>Best &amp; worst trades</b>", addr(scan["wallet"]),
             f"<i>Last {scan.get('days', 14)} days · profit = sells + coins still held at today's price</i>", ""]

    def block(title, items):
        lines.append(f"<b>{title}</b>")
        if not items:
            lines.append("None")
        for i, r in enumerate(items, 1):
            held = ago(r["held_s"]) + (" so far" if r["still_in"] else "") if r["held_s"] is not None else "?"
            dead = " · 💀 dead" if r["dead"] else ""
            lines.extend([f"{i}. <b>{html.escape(r['sym'])}</b> · <b>{r['profit_sol']:+.2f} SOL ({r['profit_pct']:+d}%)</b>",
                          f"   Put in: {r['sol_in']:.2f} SOL · Held: {held} · Still holding: {r['holding_pct']}%{dead}",
                          f"   {addr(r['mint'])}"])
        lines.append("")
    if not rows:
        lines.append("No coins with a price to rank in this scan.")
    else:
        block("🟢 Best 5", best)
        block("🔴 Worst 5", worst)
        lines.extend(_luck_lines(luck(rows, "profit_sol"), "coin"))
        lines.append(f"<i>{len(rows)} coins counted.</i>")
    if unpriced:
        lines.append(f"<i>{unpriced} coin{'s' if unpriced != 1 else ''} still held with no current price left out.</i>")
    return "\n".join(lines)


def sim_trades_text(sim: dict) -> str:
    from .trades import best_worst, luck
    rows = sim.get("rows") or []
    best, worst = best_worst(rows, "pnl_sol")
    lines = ["📊 <b>Sim trades</b>", addr(sim["wallet"]), html.escape(sim.get("label", "")), ""]

    def block(title, items):
        lines.append(f"<b>{title}</b>")
        if not items:
            lines.append("None")
        for i, r in enumerate(items, 1):
            pct = round(100 * r["pnl_sol"] / r["sol_in"]) if r.get("sol_in") else 0
            lines.append(f"{i}. <b>{html.escape(r.get('sym') or '?')}</b> · <b>{r['pnl_sol']:+.3f} SOL ({pct:+d}%)</b> · "
                         f"Exit: {html.escape(r.get('exit') or '?')}")
        lines.append("")
    if not rows:
        lines.append("No copied trades in this sim.")
    else:
        block("🟢 Best 5", best)
        block("🔴 Worst 5", worst)
        lk = luck(rows, "pnl_sol")
        lines.extend(_luck_lines(lk, "trade"))
        lines.append(f"<i>{len(rows)} copied trades · total {lk['total']:+.3f} SOL</i>")
    return "\n".join(lines)
