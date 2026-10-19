"""Per-user Coin Scan filters: presets plus custom values. Pure functions, no API calls."""
from __future__ import annotations
from dataclasses import asdict, dataclass, fields, replace

MIN_IN_FLOOR = 0.05          # the scan only keeps real traders who put in at least this


@dataclass
class Filter:
    preset: str = "any"                 # any | small | mid | whale | custom
    min_in: float = 0.05                # SOL put in, at least
    max_in: float | None = None         # SOL put in, at most (None = no max)
    min_profit: float = 0.1             # profit in SOL, at least
    min_pct: float = 10.0               # profit as % of SOL put in, at least
    max_rank: int | None = None         # only the first N buyers
    holding: str = "any"                # any | holding | sold
    sort: str = "profit"                # profit | pct | early

    @classmethod
    def from_dict(cls, d: dict | None) -> "Filter":
        if not d:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)

    def set(self, key: str, value) -> "Filter":
        """Change one custom value (switches to the custom preset)."""
        if key == "min_in":
            value = max(MIN_IN_FLOOR, float(value))
        elif key == "max_in":
            value = None if not value else float(value)
        elif key in ("min_profit", "min_pct"):
            value = max(0.0, float(value))
        elif key == "max_rank":
            value = None if not value else max(1, int(value))
        return replace(self, **{key: value}, preset="custom")


PRESETS: dict[str, Filter] = {
    "any": Filter("any", 0.05, None, 0.1),
    "small": Filter("small", 0.05, 1.0, 0.1),
    "mid": Filter("mid", 1.0, 10.0, 1.0),
    "whale": Filter("whale", 10.0, None, 5.0),
}
PRESET_NAMES = {"any": "Any", "small": "🐟 Small", "mid": "🐬 Mid", "whale": "🐳 Whale", "custom": "⚙️ Custom"}
HOLDING_NAMES = {"any": "any", "holding": "still holding", "sold": "fully sold"}
SORT_NAMES = {"profit": "profit SOL", "pct": "profit %", "early": "earliest buyer"}
CYCLE = {"holding": ["any", "holding", "sold"], "sort": ["profit", "pct", "early"]}


def _n(v: float) -> str:
    return f"{v:g}"


def describe(f: Filter) -> str:
    """One line, e.g. 'Filter: 🐳 Whale · put in ≥10 SOL · profit ≥5 SOL'."""
    if f.max_in is not None:
        put = f"put in {_n(f.min_in)}–{_n(f.max_in)} SOL"
    else:
        put = f"put in ≥{_n(f.min_in)} SOL"
    parts = [f"Filter: {PRESET_NAMES.get(f.preset, 'Custom')}", put, f"profit ≥{_n(f.min_profit)} SOL"]
    if f.min_pct != 10:
        parts.append(f"profit ≥{_n(f.min_pct)}%")
    if f.max_rank:
        parts.append(f"first {f.max_rank} buyers checked")
    if f.holding != "any":
        parts.append(HOLDING_NAMES[f.holding])
    if f.sort != "profit":
        parts.append(f"sorted by {SORT_NAMES[f.sort]}")
    return " · ".join(parts)


def profit_of(x: dict, price_sol: float | None) -> tuple[float, int]:
    """(profit SOL, profit %) = realized + value of on-chain-verified holdings. Unverified holdings count 0."""
    unreal = 0.0
    if price_sol and x.get("held_tokens", 0) > 0 and x.get("balance_ok"):
        unreal = x["held_tokens"] * price_sol - x.get("held_cost_sol", 0.0)
    p = x["realized_sol"] + unreal
    return round(p, 4), (round(100 * p / x["sol_in"]) if x.get("sol_in") else 0)


def apply_filter(cands: list[dict], f: Filter, price_sol: float | None, limit: int = 10) -> tuple[list[dict], int]:
    """Filter cached candidates. Returns (top `limit` rows, total matches). Never returns losers."""
    out = []
    for x in cands:
        if x["sol_in"] < f.min_in or (f.max_in is not None and x["sol_in"] > f.max_in):
            continue
        if f.max_rank and (x.get("entry_rank") or 10**9) > f.max_rank:
            continue
        if f.holding == "holding" and x.get("holding_pct", 0) <= 0:
            continue
        if f.holding == "sold" and x.get("holding_pct", 0) > 0:
            continue
        p, pct = profit_of(x, price_sol)
        if p <= 0 or p < f.min_profit or pct < f.min_pct:
            continue
        out.append(dict(x, profit_sol=p, profit_pct=pct))
    key = {"profit": lambda r: -r["profit_sol"], "pct": lambda r: -r["profit_pct"],
           "early": lambda r: r.get("entry_rank") or 10**9}[f.sort]
    out.sort(key=key)
    return out[:limit], len(out)
