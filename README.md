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
- **Limits** (per Telegram user, per UTC day): 5 wallet scans, 3 coin scans, 2 sims.
  - 25s cooldown between checks, with a live countdown.
  - At most 2 heavy jobs run at once; others are told "you're #N in line".
  - Every check is logged with an ID (`Check #123 ✅`). Failed checks never count.
  - **💳 Credits:** coming soon (stub).
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

## Development
```bash
python3.10 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python scripts/dry_run.py <wallet> [--coin <mint>]   # runs the logic without Telegram
```

## Limitations
- **Scan coverage:** the free RPCs are slow. A wallet scan reads at most 800 transactions within a time budget, newest first, and tells you when it could only cover part of the 14 days.
- **Peak hit rates:** these use 15-min candles that start after the buy, so the first minutes of a pump are skipped (this understates the hit rate). Only the 40 newest coins are charted per scan.
- **Coin Scan:** for big coins it samples the earliest and newest trades, so the profits shown are approximate.
- **Past results don't predict the future.** This is not financial advice.
