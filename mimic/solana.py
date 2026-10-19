"""Solana RPC access and swap parsing.

Fix vs the old wallet watcher: transactions are requested with
maxSupportedTransactionVersion=1 so newer-format swaps are no longer skipped.
"""
from __future__ import annotations
import asyncio, hashlib, time
from . import config
from .db import DB
from .net import Net

WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCh8BuRYi8M"}
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
TX_CACHE_TTL = 20 * 86400
MAX_TX_VERSION = 1


TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
# Public demo endpoint; used ONLY as a fallback for getTokenLargestAccounts, which the public
# mainnet RPC rate-limits almost permanently. Set MIMIC_RPC_URL to avoid relying on it.
DEMO_HOLDERS_RPC = "https://solana-mainnet.g.alchemy.com/v2/demo"


class DataBusy(Exception):
    """Free data sources rate-limited us: the result would be misleading, so don't show (or count) it."""


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


_P = 2 ** 255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P


def _on_curve(b: bytes) -> bool:
    """Is this 32-byte string a valid ed25519 point (i.e. NOT usable as a PDA)?"""
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    u, v = (y2 - 1) % _P, (_D * y2 + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return True
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * pow(2, (_P - 1) // 4, _P) % _P
    return (x * x - x2) % _P == 0


def find_pda(seeds: list[bytes], program: str) -> str:
    prog = b58decode(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h)
    raise ValueError("no PDA")


def ata_address(owner: str, mint: str, token_program: str = TOKEN_PROGRAM) -> str:
    """The wallet's associated token account for this mint (exists in history even after it's closed)."""
    return find_pda([b58decode(owner), b58decode(token_program), b58decode(mint)], ATA_PROGRAM)


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
        self.ok = self.fail = 0
        self.archive = ([("custom", config.CUSTOM_RPC)] if config.CUSTOM_RPC else []) + [("mainnet", config.MAINNET_RPC)]
        self.recent = ([("custom", config.CUSTOM_RPC)] if config.CUSTOM_RPC else []) + [("publicnode", config.PUBLICNODE_RPC)]

    async def _call(self, bucket: str, url: str, method: str, params: list, retries: int = 4):
        j = await self.net.request(bucket, "POST", url, retries=retries,
                                   json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if isinstance(j, dict) and "result" in j:
            self.ok += 1
            return True, j["result"]
        self.fail += 1
        return False, None

    async def call(self, method: str, params: list, order: list[tuple[str, str, int]] | None = None):
        """Try endpoints in order [(bucket, url, retries)]; default = full-history ones. Raises DataBusy."""
        if order is None:
            order = [(b, u, 4) for b, u in self.archive]
        for bucket, url, retries in order:
            ok, res = await self._call(bucket, url, method, params, retries=retries)
            if ok:
                return res
        raise DataBusy(method)

    def _state_order(self) -> list[tuple[str, str, int]]:
        """Current-state calls (balances, account owners): any healthy node works, PublicNode is fast."""
        return ([("custom", config.CUSTOM_RPC, 3)] if config.CUSTOM_RPC else []) + [
            ("publicnode", config.PUBLICNODE_RPC, 2), ("mainnet", config.MAINNET_RPC, 2)]

    async def account_info(self, address: str) -> dict | None:
        res = await self.call("getAccountInfo", [address, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}],
                              order=self._state_order())
        return (res or {}).get("value")

    async def largest_holders(self, mint: str) -> list[str]:
        """Token account addresses of the top ~20 holders. Public mainnet almost always 429s this call,
        so without a custom RPC we use Alchemy's public demo endpoint first and don't wait on mainnet."""
        order = ([("custom", config.CUSTOM_RPC, 3)] if config.CUSTOM_RPC else []) + [
            ("demo", DEMO_HOLDERS_RPC, 2), ("mainnet", config.MAINNET_RPC, 0)]
        res = await self.call("getTokenLargestAccounts", [mint], order=order)
        return [a["address"] for a in (res or {}).get("value") or []]

    async def accounts(self, addresses: list[str], parsed: bool = True) -> list[dict | None]:
        out: list[dict | None] = []
        for i in range(0, len(addresses), 100):
            cfg = {"encoding": "jsonParsed"} if parsed else {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}
            res = await self.call("getMultipleAccounts", [addresses[i:i + 100], cfg], order=self._state_order())
            out += (res or {}).get("value") or [None] * len(addresses[i:i + 100])
        return out

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
                # keep only what we use (coin scans can page through tens of thousands)
                out.append({"signature": s["signature"], "slot": s.get("slot"), "blockTime": s.get("blockTime"),
                            "err": s.get("err")})
            if len(page) < 1000:
                break
            before = page[-1]["signature"]
        return out[:max_sigs]

    async def token_balance(self, owner: str, mint: str) -> float | None:
        """Current balance of `mint` held by `owner` (None if the RPC call failed)."""
        params = [owner, {"mint": mint}, {"encoding": "jsonParsed"}]
        for bucket, url in self.archive:          # PublicNode refuses this call without a personal token
            ok, res = await self._call(bucket, url, "getTokenAccountsByOwner", params)
            if ok:
                total = 0.0
                for acc in (res or {}).get("value") or []:
                    info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                    total += float((info.get("tokenAmount") or {}).get("uiAmount") or 0)
                return total
        return None

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
        failed = 0
        lock = asyncio.Lock()

        async def worker():
            nonlocal idx, done, failed
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
                else:
                    failed += 1
                done += 1
                if progress and done % 25 == 0:
                    await progress(done, len(sigs))

        await asyncio.gather(*(worker() for _ in range(concurrency)))
        # keep only the contiguous newest prefix that was attempted, so coverage is a clean time window
        attempted = min(idx, len(sigs))
        self.last_batch = {"wanted": len(sigs), "attempted": attempted, "failed": failed}
        return [r for r in results[:attempted] if r]

    def check_batch(self, min_attempts: int = 10, max_fail: float = 0.5) -> None:
        """Raise DataBusy if the last many_tx mostly failed or barely got started (rate limited)."""
        b = getattr(self, "last_batch", None)
        if not b or not b["wanted"]:
            return
        if b["attempted"] >= min_attempts and b["failed"] > max_fail * b["attempted"]:
            raise DataBusy(f"{b['failed']} of {b['attempted']} transactions failed")
        if b["attempted"] - b["failed"] < min(min_attempts, b["wanted"]):
            raise DataBusy(f"only {b['attempted'] - b['failed']} of {b['wanted']} transactions read")
