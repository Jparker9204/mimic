"""Prices: DexScreener (current price/liquidity) and GeckoTerminal (candles, SOL/USD)."""
from __future__ import annotations
import bisect
from . import config
from .db import DB
from .net import Net

SOL_USDC_POOL = "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE"
GT = "https://api.geckoterminal.com/api/v2/networks/solana"


class Market:
    def __init__(self, net: Net, db: DB):
        self.net, self.db = net, db

    async def dex_info(self, mints: list[str]) -> dict[str, dict | None]:
        """mint -> {pair, liq, price, mcap, sym} for its most liquid pair, or None if no pair."""
        out: dict[str, dict | None] = {}
        todo = []
        for m in dict.fromkeys(mints):
            hit = self.db.cache_get("ds:" + m)
            if hit is not None:
                out[m] = hit.get("v")
            else:
                todo.append(m)
        for i in range(0, len(todo), 30):
            batch = todo[i:i + 30]
            pairs = await self.net.request("dex", "GET", "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(batch))
            if pairs is None:          # API failed: don't cache, leave unknown
                for m in batch:
                    out.setdefault(m, None)
                continue
            best: dict[str, dict] = {}
            for p in pairs or []:
                m = (p.get("baseToken") or {}).get("address")
                if m not in batch:
                    continue
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                if m not in best or liq > best[m]["liq"]:
                    best[m] = {"pair": p.get("pairAddress"), "liq": liq, "price": float(p.get("priceUsd") or 0),
                               "mcap": p.get("marketCap") or p.get("fdv"), "sym": (p.get("baseToken") or {}).get("symbol") or "?"}
            for m in batch:
                out[m] = best.get(m)
                self.db.cache_set("ds:" + m, {"v": out[m]}, 30 * 60)
        return out

    async def pool_for(self, mint: str, dex: dict | None) -> str | None:
        if dex and dex.get("pair"):
            return dex["pair"]
        hit = self.db.cache_get("gtpool:" + mint)
        if hit is not None:
            return hit.get("v")
        j = await self.net.request("gecko", "GET", f"{GT}/tokens/{mint}/pools?page=1")
        try:
            pool = j["data"][0]["attributes"]["address"]
        except (TypeError, KeyError, IndexError):
            pool = None
        if j is not None:
            self.db.cache_set("gtpool:" + mint, {"v": pool}, 6 * 3600)
        return pool

    async def candles(self, pool: str, mint: str, minutes: int, before_ts: int, ttl_s: int) -> list[list[float]]:
        """Up to 1000 candles [ts, o, h, l, c, v] ending at before_ts, oldest first."""
        key = f"gt:{pool}:{mint}:{minutes}:{before_ts}"
        hit = self.db.cache_get(key)
        if hit is not None:
            return hit
        j = await self.net.request("gecko", "GET",
                                   f"{GT}/pools/{pool}/ohlcv/minute?aggregate={minutes}&limit=1000"
                                   f"&before_timestamp={before_ts}&token={mint}")
        if j is None:
            return []
        try:
            rows = sorted(j["data"]["attributes"]["ohlcv_list"])
        except (TypeError, KeyError):
            rows = []
        self.db.cache_set(key, rows, ttl_s)
        return rows

    async def candles_since(self, pool: str, mint: str, since_ts: float, now_ts: float) -> list[list[float]]:
        """15-minute candles from since_ts to now (1-2 calls)."""
        before = int(now_ts // 900 * 900)          # bucket so the cache key is stable for 15 min
        rows = await self.candles(pool, mint, 15, before, config.CACHE_TTL_S)
        if rows and rows[0][0] > since_ts and len(rows) >= 1000:
            older = await self.candles(pool, mint, 15, int(rows[0][0]), 6 * 3600)
            rows = sorted({r[0]: r for r in older + rows}.values())
        return rows

    async def candles_1m_after(self, pool: str, mint: str, t0: float) -> list[list[float]]:
        """1-minute candles covering ~16 hours after t0 (one call). Past data, so cache long."""
        return await self.candles(pool, mint, 1, int(t0) + 59000, 7 * 86400)

    async def sol_usd_series(self) -> list[list[float]]:
        hit = self.db.cache_get("solusd")
        if hit is not None:
            return hit
        j = await self.net.request("gecko", "GET", f"{GT}/pools/{SOL_USDC_POOL}/ohlcv/hour?limit=1000")
        try:
            rows = sorted(j["data"]["attributes"]["ohlcv_list"])
        except (TypeError, KeyError):
            rows = []
        if rows:
            self.db.cache_set("solusd", rows, 30 * 60)
        return rows


class SolPrice:
    """Hourly SOL/USD lookup by timestamp."""
    def __init__(self, rows: list[list[float]], fallback: float = 150.0):
        self.rows = rows
        self.ts = [r[0] for r in rows]
        self.fallback = fallback

    def at(self, t: float) -> float:
        if not self.rows:
            return self.fallback
        i = max(0, bisect.bisect_right(self.ts, t) - 1)
        return float(self.rows[i][4])
