"""Solana RPC access and swap parsing.

Fix vs the old wallet watcher: transactions are requested with
maxSupportedTransactionVersion=1 so newer-format swaps are no longer skipped.
"""
from __future__ import annotations
import asyncio, time
from . import config
from .db import DB
from .net import Net

WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCh8BuRYi8M"}
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
TX_CACHE_TTL = 20 * 86400
MAX_TX_VERSION = 1


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        i = B58.find(ch)
        if i < 0:
            raise ValueError("not base58")
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def is_address(s: str) -> bool:
    s = (s or "").strip()
    if not 32 <= len(s) <= 44:
        return False
    try:
        return len(b58decode(s)) == 32
    except ValueError:
        return False


def short(addr: str) -> str:
    return f"{addr[:4]}…{addr[-4:]}" if len(addr) > 10 else addr


def reduce_tx(tx: dict) -> dict:
    """Shrink a getTransaction result to what we need: per-owner token deltas and SOL deltas."""
    meta = tx.get("meta") or {}
    msg = tx["transaction"]["message"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg["accountKeys"]]
    la = meta.get("loadedAddresses") or {}
    if la and not any(isinstance(k, dict) for k in msg["accountKeys"]):
        keys = keys + list(la.get("writable") or []) + list(la.get("readonly") or [])
    tok: dict[str, dict[str, float]] = {}
    for sign, field in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for b in meta.get(field) or []:
            owner = b.get("owner")
            if not owner:
                continue
            amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            d = tok.setdefault(owner, {})
            d[b["mint"]] = d.get(b["mint"], 0.0) + sign * amt
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    sol: dict[str, float] = {}
    for owner in set(tok) | {keys[0]}:
        if owner in keys:
            i = keys.index(owner)
            if i < len(pre) and i < len(post):
                sol[owner] = (post[i] - pre[i]) / 1e9
    return {"t": tx.get("blockTime"), "err": bool(meta.get("err")), "payer": keys[0], "tok": tok, "sol": sol}


def parse_swap(red: dict, wallet: str):
    """(side, mint, token_amount, sol_amount) for a simple SOL<->token swap by `wallet`, else None."""
    if not red or red.get("err"):
        return None
    d = dict(red["tok"].get(wallet) or {})
    sol = red["sol"].get(wallet, 0.0) + d.pop(WSOL, 0.0)
    toks = {k: v for k, v in d.items() if k not in STABLES and abs(v) > 1e-9}
    if len(toks) != 1:
        return None
    mint, q = next(iter(toks.items()))
    if q > 0 and sol < -0.001:
        return ("BUY", mint, q, -sol)
    if q < 0 and sol > 0.0005:
        return ("SELL", mint, -q, sol)
    return None


class Rpc:
    def __init__(self, net: Net, db: DB):
        self.net, self.db = net, db
        self.archive = ([("custom", config.CUSTOM_RPC)] if config.CUSTOM_RPC else []) + [("mainnet", config.MAINNET_RPC)]
        self.recent = ([("custom", config.CUSTOM_RPC)] if config.CUSTOM_RPC else []) + [("publicnode", config.PUBLICNODE_RPC)]

    async def _call(self, bucket: str, url: str, method: str, params: list):
        j = await self.net.request(bucket, "POST", url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if isinstance(j, dict) and "result" in j:
            return True, j["result"]
        return False, None

    async def signatures(self, address: str, since_ts: float, max_sigs: int) -> list[dict]:
        """Newest-first signatures down to `since_ts` (or max_sigs)."""
        out: list[dict] = []
        before = None
        while len(out) < max_sigs:
            p = {"limit": 1000}
            if before:
                p["before"] = before
            page = None
            for bucket, url in self.archive:
                ok, res = await self._call(bucket, url, "getSignaturesForAddress", [address, p])
                if ok:
                    page = res or []
                    break
            if page is None:
                if not out:
                    raise RuntimeError("Solana RPC unavailable")
                break
            for s in page:
                if (s.get("blockTime") or 0) < since_ts:
                    return out
                out.append(s)
            if len(page) < 1000:
                break
            before = page[-1]["signature"]
        return out[:max_sigs]

    async def tx(self, sig: str, block_time: float | None = None) -> dict | None:
        key = "tx:" + sig
        hit = self.db.cache_get(key)
        if hit is not None:
            return hit
        recent_ok = block_time is None or time.time() - block_time < config.RECENT_RPC_HISTORY_HOURS * 3600
        order: list[tuple[str, str]] = []
        if config.CUSTOM_RPC:
            order.append(("custom", config.CUSTOM_RPC))
        if recent_ok:
            order.append(("publicnode", config.PUBLICNODE_RPC))
        order.append(("mainnet", config.MAINNET_RPC))
        seen = set()
        params = [sig, {"encoding": "json", "maxSupportedTransactionVersion": MAX_TX_VERSION}]
        for bucket, url in order:
            if url in seen:
                continue
            seen.add(url)
            ok, res = await self._call(bucket, url, "getTransaction", params)
            if ok and res:
                red = reduce_tx(res)
                self.db.cache_set(key, red, TX_CACHE_TTL)
                return red
        return None

    async def many_tx(self, sigs: list[dict], budget_s: float, concurrency: int = 6, progress=None) -> list[tuple[dict, dict]]:
        """Fetch txs newest-first within a time budget. Returns [(sig_info, reduced_tx)]."""
        deadline = time.monotonic() + budget_s
        results: list[tuple[dict, dict] | None] = [None] * len(sigs)
        idx = 0
        done = 0
        lock = asyncio.Lock()

        async def worker():
            nonlocal idx, done
            while True:
                async with lock:
                    if idx >= len(sigs) or time.monotonic() > deadline:
                        return
                    i = idx
                    idx += 1
                s = sigs[i]
                left = deadline - time.monotonic()
                try:
                    red = await asyncio.wait_for(self.tx(s["signature"], s.get("blockTime")), timeout=max(2.0, left))
                except asyncio.TimeoutError:
                    red = None
                if red:
                    results[i] = (s, red)
                done += 1
                if progress and done % 25 == 0:
                    await progress(done, len(sigs))

        await asyncio.gather(*(worker() for _ in range(concurrency)))
        # keep only the contiguous newest prefix that was attempted, so coverage is a clean time window
        attempted = min(idx, len(sigs))
        return [r for r in results[:attempted] if r]
