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


def limits_text(usage: dict, user_id: int, now: float | None = None) -> str:
    now = time.time() if now is None else now
    left = int(86400 - now % 86400)
    w, c, s = usage["wallet"], usage["coin"], usage["sim"]
    return (f"📊 <b>Today</b>\n🔍 Wallet scans {w[0]}/{w[1]}\n🪙 Coin scans {c[0]}/{c[1]}\n🧪 Sims {s[0]}/{s[1]}\n"
            f"Resets in {left // 3600}h {left % 3600 // 60}m (00:00 UTC)\nYour ID: <code>{user_id}</code>")
