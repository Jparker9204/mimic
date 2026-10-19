# Mimic (@Mimic_TG_Bot)

A Telegram bot that tells you whether a Solana wallet is worth copy-trading. Everything is buttons; you only ever paste an address.

**Status:** first test version (v0.1). It's read-only: it never trades and never asks for keys.

## Features
- **🔍 Wallet Scan.** Covers the last 14 days of a wallet's coin buys:
  - how many coins it bought, and buys per coin;
  - how often the price later hit +25% / +50% / 2x (peak after the buy);
  - how many of those coins are up, down, or dead now (liquidity < $1k or no pair);
  - SOL spent vs SOL back from sells;
  - median hold time, plus a plain "copyability" note (e.g. *flips in under 2 min = hard to copy*).
- **🧪 Copytrade Sim.** Runs after a scan. It has three steps: size (0.1/0.2/0.5/1/custom SOL), take-profit (+25%/+50%/2x/none) and exit (sell when the wallet sells / -30% stop-loss / custom stop-loss). It assumes a realistic ~15s copy delay. You get net SOL, %, wins/losses, and the worst trade.
  - Assumptions are conservative: 2% entry slippage, 1% fee each way, and 0.0005 SOL per transaction.
  - If one candle touches both the stop-loss and the take-profit, the stop-loss wins. Checks start at the candle after entry.
  - Only each coin's first buy is copied.
- **🪙 Coin Scan.** Paste a coin's mint to see up to 10 *real* traders who made meaningful profit on that coin: at least 0.1 SOL and at least 10% of what they put in. Profit counts sells plus still-held tokens at today's price, holdings are verified on-chain, and only tokens they actually bought count. Losers are never listed. Full addresses are tap-to-copy, with their entry order and how much they still hold. One tap runs a Wallet Scan on any of them. Bundle and transfer wallets are hidden ("🚩 Hid N bundle/transfer wallets"); tap **🚩 Show flagged** to see them with the reason:
  - **dev wallet:** the coin's creator;
  - **bought in launch block:** bought in the creation block or the first trading block;
  - **same-block bundle:** 3+ wallets bought in one block with SOL amounts within ~10% of each other;
  - **got tokens by transfer / from dev:** at least half its tokens arrived by transfer, or it received tokens from the dev;
  - **no real buy:** spent under 0.01 SOL in total.
  
  Only wallets that put in at least 0.05 SOL are ranked.
- **🏆 Best & worst trades** (Wallet Scan button, free, from the cached scan, no credits): the wallet's 5 best and 5 worst coins by profit in SOL (sells plus coins still held at today's price), with put in, held time, still-holding %, and a tap-to-copy mint. A luck check shows how much of the total comes from the best coin and the total without it.
- **📊 Sim trades** (Copytrade Sim button, free): the sim's 5 best and 5 worst copied trades with result and exit reason, plus the total without the best trade.
- **⚙️ Filters** (Coin Scan; saved per user, all buttons). Open them from the main menu or from a Coin Scan result.
  - **Presets:** 🐟 Small (put in 0.05–1 SOL, profit ≥0.1), 🐬 Mid (put in 1–10, profit ≥1), 🐳 Whale (put in ≥10, profit ≥5), and Any (the default).
  - **Custom (tap, then type a number):** min/max SOL put in, min profit SOL, min profit % (default 10), first N buyers.
  - **Toggles:** still holding (any / still holding / fully sold), sort by (profit SOL / profit % / earliest buyer).
  - Applying a filter to a cached scan costs no credits and makes no API calls.
- **💳 Credits** (per Telegram user). Every check costs credits; there are no separate daily limits.
  - **Costs:** 🪙 Coin Scan 10 · 🔍 Wallet Scan 3 · 🧪 Copytrade Sim 15. Free extras from cached results (🏆 Best & worst trades, 📊 Sim trades, ⚙️ Filters) cost nothing. Costs are shown on the buttons.
  - **Free credits:** +40 every 6 hours, stacking up to 150 (no more however long you're away). New accounts start with 40, and users from before credits get 40 on their first use after the update. Refills are computed lazily from a last-refill timestamp, so no background job runs.
  - **Purchased credits:** a second balance with no cap that never expires. Free credits are spent first, then purchased. Payments aren't live yet; the "out of credits" message says packs are coming soon (100 for 0.05 SOL, 250 for 0.1 SOL).
  - **Balance** shows on the main menu and in `/credits`, e.g. `💳 Credits: 120 (free 80/150, bought 40). Next +40 in 3h 12m`. Each finished check shows what it cost and what's left.
  - **Refunds:** if a check fails (data source busy, rate limits, errors, or the bot restarts mid-check), its credits go back to the same balances automatically. Cache hits (repeat within 45 min) still cost credits, so the price is predictable.
  - **Ledger:** every change (signup, migrate, refill, spend, refund, grant) is stored in the `credit_ledger` table with the balance after, check ID and admin ID, for refund disputes.
  - **Admins** (`MIMIC_ADMIN_IDS`) are unlimited: no cost, no cooldown. Admin-only commands:
    - `/grant <telegram_id> <amount> [note]` adds purchased credits (negative removes them, never below 0), for testing and refunds.
    - `/ledger <telegram_id>` shows the user's latest 20 credit changes.
  - Users find their Telegram ID under 💳 Credits.
  - All of these numbers can be changed in `/etc/mimic.env` (see Settings).
- **Fair use:** 25s cooldown between checks, with a live countdown. One check at a time per user. At most 2 heavy jobs run at once; others are told "you're #N in line". Every check is logged with an ID (`Check #123 ✅`).
- **Caching:** SQLite. Scans and prices are kept for 45 min, and parsed transactions for 20 days, so repeat checks are fast and free APIs aren't hammered.

## Data sources (all free)
| What | Source | Notes |
|---|---|---|
| Wallet/coin transactions | Solana RPC | PublicNode (fast, but only ~1.5 days of history), then `api.mainnet-beta` (full history, heavily rate-limited). Set `MIMIC_RPC_URL` to a free Helius/QuickNode key for much faster, fuller scans. |
| Current price, liquidity | DexScreener `tokens/v1` | batched 30 per call |
| Candles (peak, sim) | GeckoTerminal OHLCV | 15-min for peaks, 1-min for sims; ~30 calls/min |

Transactions are requested with `maxSupportedTransactionVersion: 1`, so newer-format swaps aren't silently skipped.

## Install on the Droplet (Ubuntu 22.04)
Paste these into the DigitalOcean web console:
```bash
curl -fsSL https://raw.githubusercontent.com/Jparker9204/mimic/main/deploy/install.sh | sudo bash
sudo mimic-set-token        # paste the @BotFather token (hidden); optional RPC URL
```
- **Update later:** `sudo mimic-update`
- **Logs:** `journalctl -u mimic-bot -f`

The bot runs as the `mimic` user under systemd (`mimic-bot`, `Restart=always`, memory capped at 300 MB). It uses long polling, so it opens no ports and doesn't touch Caddy or any other service.

## Settings
These live in `/etc/mimic.env` (root-only, chmod 600); see `deploy/mimic.env.example`. Secrets are never stored in the repo.

All credit numbers are optional. Leave a line out to keep the default. After editing, apply with `sudo systemctl restart mimic-bot`.

| Variable | Default | Meaning |
|---|---|---|
| `MIMIC_COST_COIN` | 10 | credits per Coin Scan |
| `MIMIC_COST_WALLET` | 3 | credits per Wallet Scan |
| `MIMIC_COST_SIM` | 15 | credits per Copytrade Sim |
| `MIMIC_FREE_REFILL` | 40 | free credits added each interval (also the starting balance) |
| `MIMIC_REFILL_HOURS` | 6 | refill interval in hours (decimals allowed, e.g. 0.5) |
| `MIMIC_FREE_CAP` | 150 | free credits never stack above this |
| `MIMIC_PACKS` | `100:0.05,250:0.1` | packs shown in the "coming soon" note, as `credits:SOL` pairs (display only; no payments) |
| `MIMIC_COOLDOWN_S` | 25 | seconds between checks |
| `MIMIC_ADMIN_IDS` | (none) | comma-separated Telegram IDs: no cost, no cooldown, can use `/grant` and `/ledger` |

Changing costs or refill settings never changes balances people already have; it only applies to future spends and refills.

## Development
```bash
python3.10 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python scripts/dry_run.py <wallet> [--coin <mint>]   # runs the logic without Telegram
```

## Limitations
- **Scan coverage:** the free RPCs are slow. A wallet scan reads at most 800 transactions within a time budget, newest first, and tells you when it could only cover part of the 14 days.
- **Peak hit rates:** these use 15-min candles that start after the buy, so the first minutes of a pump are skipped (this understates the hit rate). Only the 40 newest coins are charted per scan.
- **Coin Scan (v2, per-wallet):** it picks about 40 wallets (top 20 holders plus the biggest recent traders from GeckoTerminal) and reads each wallet's **own** token-account history for that coin. A wallet's profit is only shown after its whole history on the coin has been read. Wallets with more than 40 trades on the coin are skipped as bots. Wallets that sold out before the newest ~300 pool trades and aren't top holders can be missed. Trades paid in WBTC, USDC, or another pair's quote token are valued at today's price of that token.
- **Top holders** (`getTokenLargestAccounts`) are refused by every public RPC we tried. Without `MIMIC_RPC_URL` Mimic tries Alchemy's public demo endpoint, which is often rate-limited, and then says "Top-holder list was unavailable". **A free Helius key is strongly recommended**: it gives reliable top holders and faster history reads.
- **Rate limits are never hidden:** if most reads fail, the check stops with "data source busy, try again" and its credits are refunded.
- **Past results don't predict the future.** This is not financial advice.
