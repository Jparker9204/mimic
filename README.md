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
  - **Purchased credits:** a second balance with no cap that never expires. Free credits are spent first, then purchased. Bought with SOL (see **💎 Buy credits** below) or granted by an admin.
  - **Balance** shows on the main menu and in `/credits`, e.g. `💳 Credits: 120 (free 80/150, bought 40). Next +40 in 3h 12m`. Each finished check shows what it cost and what's left.
  - **Refunds:** if a check fails (data source busy, rate limits, errors, or the bot restarts mid-check), its credits go back to the same balances automatically. Cache hits (repeat within 45 min) still cost credits, so the price is predictable.
  - **Ledger:** every change (signup, migrate, refill, spend, refund, grant) is stored in the `credit_ledger` table with the balance after, check ID and admin ID, for refund disputes.
  - **Admins** (`MIMIC_ADMIN_IDS`) are unlimited: no cost, no cooldown. Admin-only commands:
    - `/grant <telegram_id> <amount> [note]` adds purchased credits (negative removes them, never below 0), for testing and refunds.
    - `/ledger <telegram_id>` shows the user's latest 20 credit changes.
    - `/orders [telegram_id]` shows the latest SOL orders (status, SOL received, credits, tx link) and unmatched incoming payments.
  - Users find their Telegram ID under 💳 Credits.
  - All of these numbers can be changed in `/etc/mimic.env` (see Settings).
- **💎 Buy credits with SOL** (main menu, `/buy`, the out-of-credits message and `/credits`). Uses [Solana Pay](https://docs.solanapay.com/spec) transfer requests.
  - **Safety:** Mimic only *watches* a public receiving address (`MIMIC_PAY_ADDRESS`). It never generates, stores or uses a private key or seed, never connects to a wallet and never sends SOL. The user sends a normal SOL transfer from their own wallet.
  - **Price:** credits = floor(SOL ÷ `MIMIC_PRICE_PER_CREDIT_SOL`), default 0.001 SOL per credit (1 SOL = 1000 credits). Minimum 0.02 SOL, maximum 5 SOL per purchase. Quick buttons 0.02 / 0.05 / 0.1 / 0.25 SOL, or ✏️ Custom amount. The user sees `0.05 SOL = 50 credits` and taps ✅ Confirm.
  - **Order:** a random 32-byte *reference* public key (base58; no private key exists for it), the exact amount in lamports, the price, user and chat, created and expiry time (15 min). One open order per user; a new one replaces the old.
  - **Payment message:** a QR code of the `solana:` link, then the amount, credits, the receiving address in tap-to-copy `<code>`, the full Solana Pay link (`solana:<address>?amount=0.05&reference=<ref>&label=Mimic&message=Mimic%20credits`), 📋 copy buttons for address, amount and link, ✅ I've paid (an immediate check) and ✖️ Cancel. Amounts are exact decimals (no float artifacts).
  - **solana: button:** Telegram only accepts `http(s)://` and `tg://` URL buttons, so the bot tries a 💳 Pay with wallet button once and, when Telegram rejects it, sends the same message without it for the rest of the process (logged once). Phone users copy the address and amount into their wallet's Send screen; desktop users scan the QR with their phone wallet.
  - **Verification** (background task, only calls the RPC while unpaid orders exist): every ~10s for open orders, `getSignaturesForAddress(reference)` at `finalized`, then `getTransaction` (`maxSupportedTransactionVersion` 0, retried with the version the RPC names). A payment counts when the tx has no error, the reference is among its account keys (incl. lookup-table ones), and the receiving address's balance went up (post − pre). It's credited when the total received reaches the amount (tolerance 0.000001 SOL).
  - **Exactly once:** each signature is stored once (`pay_txs` primary key), and the credit is one DB transaction: order → paid, `pay_credits` row (UNIQUE order and UNIQUE signature), balance, and a `purchase` ledger entry with order, SOL and signature.
  - **Overpayment** is credited on what actually arrived. **Underpayment** keeps the order open and tells the user what's missing, with a link for the rest. **Late payments** (after expiry or cancel) are still checked every ~60s for 24h and credited automatically.
  - **Extra payments** for an order that is already paid (same reference) are recorded and listed in `/orders` as unmatched, not auto-credited.
  - **Copied address + amount (no reference):** the bot also reads new finalized txs of the receiving address. A transfer with no order reference is matched when exactly one unpaid order (last 24h) has exactly that amount (or exactly its missing rest). Otherwise it's listed as *unmatched* in `/orders` so an admin can `/grant`.
  - The user is told `✅ Payment received: 0.05 SOL → 50 credits. Tx: <solscan link>` (link previews off).
  - `/terms`: credits are non-refundable except failed checks (automatic) or by admin decision. Not financial advice.
  - The order and payment tables (`pay_orders`, `pay_txs`, `pay_credits`) plus `credit_ledger` are the refund-dispute record; nothing is deleted.
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
| `MIMIC_PAY_ADDRESS` | (none) | **public** Solana address that receives payments (never a private key). Unset or invalid = "payments aren't available yet" |
| `MIMIC_PRICE_PER_CREDIT_SOL` | 0.001 | SOL per credit; credits = floor(SOL ÷ price) |
| `MIMIC_MIN_PURCHASE_SOL` | 0.02 | smallest purchase |
| `MIMIC_MAX_PURCHASE_SOL` | 5 | largest purchase (sanity cap) |
| `MIMIC_BUY_AMOUNTS` | `0.02,0.05,0.1,0.25` | quick-amount buttons (only those within min..max are shown) |
| `MIMIC_ORDER_MINUTES` | 15 | how long a payment request is valid |
| `MIMIC_LATE_PAYMENT_HOURS` | 24 | late payments are still credited this long after the order |
| `MIMIC_PAY_URL_BUTTON` | 1 | set 0 to skip trying the `solana:` "Pay with wallet" button |
| `MIMIC_COOLDOWN_S` | 25 | seconds between checks |
| `MIMIC_ADMIN_IDS` | (none) | comma-separated Telegram IDs: no cost, no cooldown, can use `/grant`, `/ledger` and `/orders` |

Changing costs or refill settings never changes balances people already have; it only applies to future spends and refills. Changing the price only affects new orders (each order stores its price).

**Use a dedicated receiving wallet** for `MIMIC_PAY_ADDRESS` (a fresh Phantom account is fine). Other incoming transfers to it that happen to match an open order's exact amount could be credited to that order.

### Testing payments safely
1. Put your Telegram ID in `MIMIC_ADMIN_IDS`, set `MIMIC_PAY_ADDRESS`, and temporarily `MIMIC_MIN_PURCHASE_SOL=0.001` (keep the price at 0.001 so 0.001 SOL = 1 credit). `sudo systemctl restart mimic-bot`.
2. In the bot: 💎 Buy credits → ✏️ Custom amount → `0.001` → ✅ Confirm. Pay from another wallet by scanning the QR (or copying address + amount).
3. About 30–60s later you should get `✅ Payment received: 0.001 SOL → 1 credit`. Check `/orders` and `/ledger <your id>`.
4. Remove the `MIMIC_MIN_PURCHASE_SOL` line and restart.

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
