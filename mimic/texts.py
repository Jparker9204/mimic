"""Short, plain message formatting (Telegram HTML)."""
from __future__ import annotations
import html, time
from .solana import short


def _p(v) -> str:
    return "–" if v is None else f"{v}%"


def wallet_scan_text(res: dict) -> str:
    s = res["stats"]
    cov_days = (res["scanned_at"] - res["covered_from"]) / 86400
    cover = f"last {res['days']} days"
    if res["capped"]:
        cover += f" (only the newest {cov_days:.1f} days could be read: {res['txs']} txs)"
    if s["tokens"] == 0:
        return (f"🔍 <b>Wallet {short(res['wallet'])}</b>\n📅 {cover}\n\n"
                "No token buys found in this window.")
    net = s["sol_out"] - s["sol_in"]
    hold = "–" if s["median_hold_min"] is None else f"{s['median_hold_min']:g} min"
    lines = [
        f"🔍 <b>Wallet {short(res['wallet'])}</b>",
        f"📅 {cover}",
        f"🪙 Coins bought: <b>{s['tokens']}</b> · {s['buys']} buys ({s['buys_per_token']:g} per coin)",
        f"🎯 Hit at peak: +25% <b>{_p(s['hit25'])}</b> · +50% <b>{_p(s['hit50'])}</b> · 2x <b>{_p(s['hit2x'])}</b>"
        + (f" <i>({s['priced']} coins with charts)</i>" if s["priced"] < s["tokens"] else ""),
        f"📊 Now: 🟢 up {_p(s['up_now'])} · 🔴 down {_p(s['down_now'])} · 💀 dead {_p(s['dead'])}",
        f"💰 SOL in {s['sol_in']:.2f} → out {s['sol_out']:.2f} ({net:+.2f})",
        f"⏱ Median hold: <b>{hold}</b>",
        "",
        s["copyability"],
    ]
    return "\n".join(lines)


def sim_text(wallet: str, label: str, summ: dict) -> str:
    sign = "🟢" if summ["net_sol"] > 0 else "🔴"
    lines = [
        f"🧪 <b>Copytrade sim · {short(wallet)}</b>",
        html.escape(label),
        "",
        f"{sign} Net: <b>{summ['net_sol']:+.3f} SOL ({summ['net_pct']:+.1f}%)</b>",
        f"Trades {summ['trades']} · ✅ {summ['wins']} wins · ❌ {summ['losses']} losses",
        f"SOL in {summ['sol_in']:.3f} → out {summ['sol_out']:.3f}",
        f"Worst trade: {summ['worst']:+.3f} SOL · Best: {summ['best']:+.3f} SOL",
    ]
    if summ.get("skipped"):
        lines.append(f"<i>{summ['skipped']} coins skipped (no price data)</i>")
    lines.append("<i>Copies each coin's first buy only. Includes 2% slippage, 1% fees, 0.0005 SOL/tx. "
                 "Past results ≠ future.</i>")
    return "\n".join(lines)


def coin_scan_text(res: dict) -> str:
    head = f"🪙 <b>{html.escape(res['sym'])}</b> ({short(res['mint'])}) · top real traders by profit"
    rows = []
    for i, t in enumerate(res["traders"], 1):
        pnl = f"{t['realized_sol']:+.2f} SOL" if t["sells"] else "no sells yet"
        rows.append(f"{i}. <code>{short(t['wallet'])}</code> {pnl} · in {t['sol_in']:.2f} · "
                    f"entry #{t['entry_rank']} · holds {t['holding_pct']}%")
    if not rows:
        rows = ["No real traders (0.05+ SOL in) found."]
    extra = []
    if res.get("flagged_count"):
        extra.append(f"🚩 Hid {res['flagged_count']} bundle/transfer wallet{'s' if res['flagged_count'] != 1 else ''}")
    if res.get("small"):
        extra.append(f"🤏 {res['small']} tiny buyers (under 0.05 SOL) not ranked")
    if not res.get("launch_checked", True):
        extra.append("<i>Too many trades to see the launch — launch-block check skipped.</i>")
    note = (f"<i>Based on {res['txs']} of {res['sigs']}{'+' if res['sigs'] >= res.get('sig_cap', 5000) else ''} trades "
            f"(earliest + newest) — approximate.</i>" if res["sampled"] else f"<i>Based on all {res['txs']} trades.</i>")
    return "\n".join([head, ""] + rows + [""] + extra + [note])


def flagged_text(res: dict) -> str:
    fl = res.get("flagged") or []
    if not fl:
        return "🚩 No flagged wallets for this coin."
    rows = [f"🚩 <b>Flagged wallets · {html.escape(res['sym'])}</b>"]
    for t in fl:
        rows.append(f"🚩 <code>{short(t['wallet'])}</code> in {t['sol_in']:.2f} → out {t['sol_out']:.2f} · "
                    + html.escape(", ".join(t.get("reasons") or [])))
    if res.get("flagged_count", 0) > len(fl):
        rows.append(f"<i>…and {res['flagged_count'] - len(fl)} more</i>")
    return "\n".join(rows)


def limits_text(usage: dict, user_id: int, now: float | None = None) -> str:
    now = time.time() if now is None else now
    left = int(86400 - now % 86400)
    w, c, s = usage["wallet"], usage["coin"], usage["sim"]
    return (f"📊 <b>Today</b>\n🔍 Wallet scans {w[0]}/{w[1]}\n🪙 Coin scans {c[0]}/{c[1]}\n🧪 Sims {s[0]}/{s[1]}\n"
            f"Resets in {left // 3600}h {left % 3600 // 60}m (00:00 UTC)\nYour ID: <code>{user_id}</code>")
