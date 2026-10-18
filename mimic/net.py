"""HTTP helper: one shared client, a minimum gap per API host, and retry with backoff."""
from __future__ import annotations
import asyncio, logging, random, time
import httpx

log = logging.getLogger("mimic.net")

# Minimum seconds between requests per host bucket (free-tier friendly).
INTERVALS = {
    "gecko": 2.1,       # GeckoTerminal free: ~30 calls/min
    "dex": 0.35,        # DexScreener tokens endpoint: 300/min
    "publicnode": 0.06,
    "mainnet": 0.3,     # api.mainnet-beta: ~40 calls/10s per method
    "custom": 0.08,
}


class RateLimiter:
    def __init__(self, intervals: dict[str, float] | None = None):
        self.intervals = dict(INTERVALS if intervals is None else intervals)
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, bucket: str) -> None:
        lock = self._locks.setdefault(bucket, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            t = self._next.get(bucket, 0.0)
            if t > now:
                await asyncio.sleep(t - now)
            self._next[bucket] = max(now, t) + self.intervals.get(bucket, 0.2)

    def penalize(self, bucket: str, seconds: float) -> None:
        self._next[bucket] = max(self._next.get(bucket, 0.0), time.monotonic() + seconds)


class Net:
    def __init__(self, client: httpx.AsyncClient | None = None, limiter: RateLimiter | None = None):
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True,
                                                  headers={"User-Agent": "mimic-bot/0.1"})
        self.limiter = limiter or RateLimiter()

    async def request(self, bucket: str, method: str, url: str, *, json=None, retries: int = 4):
        """Returns parsed JSON, or None after retries are exhausted."""
        for attempt in range(retries + 1):
            await self.limiter.wait(bucket)
            try:
                r = await self.client.request(method, url, json=json)
            except (httpx.HTTPError, OSError) as exc:
                log.debug("%s %s error: %s", bucket, url[:80], exc)
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (429, 413) or r.status_code >= 500:
                back = min(30.0, 2.0 * (2 ** attempt)) + random.random()
                self.limiter.penalize(bucket, back)
                log.debug("%s %s -> %s, backoff %.1fs", bucket, url[:60], r.status_code, back)
                continue
            if r.status_code >= 400:
                return None
            try:
                return r.json()
            except ValueError:
                return None
        return None

    async def aclose(self) -> None:
        await self.client.aclose()
