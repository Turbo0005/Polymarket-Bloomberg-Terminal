"""Minimal typed helpers for market and order-book rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _to_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@dataclass
class MarketRow:
    """One row from markets list / get."""

    id: str
    slug: str
    question: str
    volume: float | None
    liquidity: float | None
    yes_price: float | None
    no_price: float | None
    active: bool | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> MarketRow:
        from pm_terminal.cli_runner import parse_outcome_prices

        oid = str(d.get("id") or d.get("slug") or "")
        slug = str(d.get("slug") or "")
        q = str(d.get("question") or d.get("title") or "")
        vol = _to_float(d.get("volumeNum") or d.get("volume"))
        liq = _to_float(d.get("liquidityNum") or d.get("liquidity"))
        prices = parse_outcome_prices(d.get("outcomePrices"))
        yes_p = prices[0] if len(prices) > 0 else None
        no_p = prices[1] if len(prices) > 1 else None
        active = d.get("active")
        if isinstance(active, str):
            active = active.lower() in ("true", "1", "yes")
        return cls(
            id=oid,
            slug=slug,
            question=q,
            volume=vol,
            liquidity=liq,
            yes_price=yes_p,
            no_price=no_p,
            active=active if isinstance(active, bool) else None,
            raw=d,
        )


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class OrderBookSide:
    levels: list[BookLevel]


def parse_book_side(raw: Any) -> OrderBookSide:
    levels: list[BookLevel] = []
    if not isinstance(raw, list):
        return OrderBookSide(levels=levels)
    for item in raw:
        if not isinstance(item, dict):
            continue
        pr = _to_float(item.get("price"))
        sz = _to_float(item.get("size"))
        if pr is not None and sz is not None:
            levels.append(BookLevel(price=pr, size=sz))
    return OrderBookSide(levels=levels)
