#!/usr/bin/env python3
"""
Bloomberg-style Polymarket Terminal
A curses-based terminal UI for real-time Polymarket data.

Requirements:
- Python 3.10+
- polymarket CLI installed and configured (`polymarket setup`)
"""

from __future__ import annotations

import argparse
import curses
import json
import locale
import random
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ─── Constants ────────────────────────────────────────────────────────────────

DEFAULT_REFRESH = 6.0
MARKET_LIMIT = 120
BOOK_PANELS = 3
TRADE_CAPACITY = 80
LOG_CAPACITY = 200
AUX_REFRESH = 30.0
PRICE_HISTORY_LEN = 12

# Unicode box-drawing
TL, TR, BL, BR = "┌", "┐", "└", "┘"
HZ, VT = "─", "│"
TJ, BJ, LJ, RJ, CX = "┬", "┴", "├", "┤", "┼"

SPARK = "▁▂▃▄▅▆▇█"
BLOCK = "█"

# Color pair IDs
CP_GREEN = 1
CP_MAGENTA = 2
CP_CYAN = 3
CP_YELLOW = 4
CP_RED = 5
CP_WHITE = 6
CP_BLUE = 7
CP_TICKER = 8
CP_HEADER_BG = 9
CP_SELECTED = 10

PALETTE_NAMES = ["bloomberg", "amber", "matrix"]


# ─── Utilities ────────────────────────────────────────────────────────────────


def parse_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        t = str(v).strip()
        return float(t) if t else default
    except (TypeError, ValueError):
        return default


def parse_opt_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        t = str(v).strip()
        return float(t) if t else None
    except (TypeError, ValueError):
        return None


def parse_json_array(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        t = v.strip()
        if not t:
            return []
        try:
            p = json.loads(t)
            if isinstance(p, list):
                return p
        except json.JSONDecodeError:
            return [x.strip() for x in t.split(",") if x.strip()]
    return []


def parse_token_ids(v: Any) -> list[str]:
    return [str(x).strip() for x in parse_json_array(v) if str(x).strip()]


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def trunc(text: str, w: int) -> str:
    if w <= 0:
        return ""
    if len(text) <= w:
        return text
    return text[: max(0, w - 3)] + "..." if w > 3 else text[:w]


# ─── Formatting ───────────────────────────────────────────────────────────────


def fmt_cents(v: float | None) -> str:
    if v is None:
        return "  --"
    c = v * 100.0
    if c >= 10.0:
        return f"{c:.1f}\u00a2"
    return f"{c:.2f}\u00a2"


def fmt_vol(v: float | None) -> str:
    if v is None:
        return "    --"
    a = abs(v)
    if a >= 1_000_000:
        return f"${v / 1_000_000:,.1f}M"
    if a >= 1_000:
        return f"${v / 1_000:,.0f}K"
    return f"${v:,.0f}"


def fmt_money(v: float | None) -> str:
    if v is None:
        return "  --"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1_000_000:
        return f"{sign}${a / 1_000_000:,.1f}M"
    if a >= 1_000:
        return f"{sign}${a / 1_000:,.1f}K"
    return f"{sign}${a:,.0f}"


def fmt_price(v: float | None) -> str:
    if v is None:
        return "  --"
    a = abs(v)
    if a >= 1000:
        return f"${v:,.0f}"
    if a >= 1:
        return f"${v:,.2f}"
    return f"${v:.4f}"


def fmt_change(v: float | None) -> str:
    if v is None:
        return "  --"
    if v >= 0:
        return f"\u2191{v:.2%}"
    return f"\u2193{abs(v):.2%}"


def fmt_pct(v: float | None) -> str:
    if v is None:
        return "--"
    return f"{v:.0%}"


def fmt_bps(v: float | None) -> str:
    if v is None:
        return "--"
    return f"{v * 10000:.0f}bps"


def sparkline(values: list[float], width: int = 8) -> str:
    if not values:
        return " " * width
    vals = values[-width:]
    mn, mx = min(vals), max(vals)
    rng = mx - mn if mx != mn else 1.0
    out = ""
    for v in vals:
        idx = int((v - mn) / rng * (len(SPARK) - 1))
        idx = clamp(idx, 0, len(SPARK) - 1)
        out += SPARK[idx]
    return out.ljust(width)[:width]


def chart_bar(prob: float | None, width: int) -> str:
    if prob is None or width <= 0:
        return " " * width
    fill = int(prob * width)
    fill = clamp(fill, 0, width)
    return (BLOCK * fill).ljust(width)[:width]


def depth_bar(size: float, max_size: float, width: int) -> str:
    if max_size <= 0 or width <= 0:
        return " " * width
    fill = int(size / max_size * width)
    fill = clamp(fill, 0, width)
    return (BLOCK * fill).ljust(width)[:width]


def guess_symbol(q: str) -> str | None:
    upper = q.upper()
    for sym in [
        "BTC", "ETH", "SOL", "XRP", "NVDA", "TSLA", "SPY", "QQQ",
        "AAPL", "MSFT", "AMZN", "META", "GOOGL", "COIN", "EUR/USD",
    ]:
        if sym in upper:
            return sym
    return None


# ─── Data Models ──────────────────────────────────────────────────────────────


@dataclass
class MarketRow:
    question: str
    condition_id: str
    slug: str
    best_bid: float | None
    best_ask: float | None
    last_trade: float | None
    volume_24h: float
    volume_total: float
    liquidity: float
    outcomes: list[str]
    token_ids: list[str]
    accepting_orders: bool
    one_day_change: float | None
    symbol_hint: str | None

    @property
    def mid(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return self.last_trade or self.best_bid or self.best_ask

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return max(0.0, self.best_ask - self.best_bid)

    @property
    def spread_bps(self) -> float | None:
        s = self.spread
        m = self.mid
        if s is None or m is None or m <= 0:
            return None
        return s / m


@dataclass
class BookSnapshot:
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)
    last_trade: float | None = None
    tick_size: float | None = None
    timestamp: str = ""

    @property
    def total_bid(self) -> float:
        return sum(s for _, s in self.bids)

    @property
    def total_ask(self) -> float:
        return sum(s for _, s in self.asks)

    @property
    def imbalance(self) -> float:
        t = self.total_bid + self.total_ask
        return (self.total_bid / t) if t > 0 else 0.5


@dataclass
class BookPanel:
    market: MarketRow
    token_id: str
    book: BookSnapshot | None


@dataclass
class TradeEntry:
    timestamp: str
    side: str
    price_cents: float
    size: float
    market_name: str


@dataclass
class Snapshot:
    markets: list[MarketRow] = field(default_factory=list)
    book_panels: list[BookPanel] = field(default_factory=list)
    leaderboard: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    assets: list[dict[str, Any]] = field(default_factory=list)
    trades: list[TradeEntry] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    refreshed_at: float = 0.0
    cycle: int = 0
    last_error: str = ""
    price_history: dict[str, list[float]] = field(default_factory=dict)


# ─── Data Collector ───────────────────────────────────────────────────────────


class DataCollector:
    """Background thread that polls the polymarket CLI and builds snapshots."""

    def __init__(self, refresh: float, limit: int, panels: int):
        self.refresh = max(1.0, refresh)
        self.limit = max(20, limit)
        self.panels = clamp(panels, 1, 4)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._force = threading.Event()
        self._thread: threading.Thread | None = None

        self._snapshot = Snapshot()
        self._logs: deque[str] = deque(maxlen=LOG_CAPACITY)
        self._trades: deque[TradeEntry] = deque(maxlen=TRADE_CAPACITY)
        self._focus = 0
        self._lb_period = "day"

        self._cached_lb: list[dict[str, Any]] = []
        self._cached_ev: list[dict[str, Any]] = []
        self._last_aux = 0.0
        self._prev_markets: dict[str, float] = {}
        self._price_hist: dict[str, list[float]] = {}

    # ── Public API ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._force.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def set_focus(self, idx: int) -> None:
        with self._lock:
            self._focus = max(0, idx)

    def set_lb_period(self, period: str) -> None:
        with self._lock:
            self._lb_period = period
            self._cached_lb = []
            self._last_aux = 0.0
        self._force.set()

    def request_refresh(self) -> None:
        self._force.set()

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(
                markets=list(self._snapshot.markets),
                book_panels=list(self._snapshot.book_panels),
                leaderboard=list(self._snapshot.leaderboard),
                events=list(self._snapshot.events),
                assets=list(self._snapshot.assets),
                trades=list(self._trades),
                logs=list(self._logs),
                refreshed_at=self._snapshot.refreshed_at,
                cycle=self._snapshot.cycle,
                last_error=self._snapshot.last_error,
                price_history=dict(self._price_hist),
            )

    def collect_once(self) -> Snapshot:
        return self._cycle(1, force_aux=True)

    # ── Background loop ───────────────────────────────────────────────────

    def _loop(self) -> None:
        cycle = 0
        while not self._stop.is_set():
            cycle += 1
            try:
                snap = self._cycle(cycle)
                with self._lock:
                    self._snapshot = snap
            except Exception as e:
                self._log(f"refresh error: {e}")
                with self._lock:
                    self._snapshot.last_error = str(e)

            deadline = time.monotonic() + self.refresh
            while not self._stop.is_set():
                rem = deadline - time.monotonic()
                if rem <= 0:
                    break
                if self._force.wait(timeout=min(rem, 0.25)):
                    self._force.clear()
                    break

    def _cycle(self, cycle: int, force_aux: bool = False) -> Snapshot:
        with self._lock:
            focus = self._focus
            prev = self._snapshot
            period = self._lb_period

        errors: list[str] = []

        try:
            markets = self._fetch_markets()
        except Exception as e:
            markets = list(prev.markets)
            errors.append(f"markets: {e}")

        self._update_history(markets)
        self._generate_trades(markets)

        focus = clamp(focus, 0, max(0, len(markets) - 1))
        panel_markets = self._pick_panels(markets, focus)
        token_ids = [m.token_ids[0] for m in panel_markets if m.token_ids]

        try:
            books = self._fetch_books(token_ids)
        except Exception as e:
            books = {}
            errors.append(f"books: {e}")

        now = time.monotonic()
        need_aux = force_aux or (now - self._last_aux) >= AUX_REFRESH
        if need_aux or not self._cached_lb:
            try:
                self._cached_lb = self._fetch_leaderboard(period)
            except Exception as e:
                errors.append(f"leaderboard: {e}")
        if need_aux or not self._cached_ev:
            try:
                self._cached_ev = self._fetch_events()
            except Exception as e:
                errors.append(f"events: {e}")
        if need_aux:
            self._last_aux = now

        try:
            assets = self._derive_assets(markets)
        except Exception as e:
            assets = []
            errors.append(f"assets: {e}")

        panels = [
            BookPanel(market=m, token_id=tid, book=books.get(tid))
            for m, tid in zip(panel_markets, token_ids)
        ]

        if errors:
            self._log("partial: " + "; ".join(errors))
        else:
            self._log(
                f"ok: {len(markets)}mkt {len(panels)}bk "
                f"{len(self._cached_ev)}ev {len(self._cached_lb)}lb"
            )

        return Snapshot(
            markets=markets,
            book_panels=panels,
            leaderboard=list(self._cached_lb),
            events=list(self._cached_ev),
            assets=assets,
            trades=list(self._trades),
            logs=list(self._logs),
            refreshed_at=time.time(),
            cycle=cycle,
            last_error="; ".join(errors),
            price_history=dict(self._price_hist),
        )

    # ── CLI runner ────────────────────────────────────────────────────────

    def _cli(self, args: list[str], timeout: float = 20.0) -> Any:
        cmd = ["polymarket", *args, "-o", "json"]
        proc = subprocess.run(
            cmd, check=False, text=True, capture_output=True, timeout=timeout,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            raise RuntimeError(f"cli: {out or err or 'unknown'}")
        if not out:
            raise RuntimeError("cli: empty output")
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"cli: bad JSON: {exc}") from exc

    # ── Data fetchers ─────────────────────────────────────────────────────

    def _fetch_markets(self) -> list[MarketRow]:
        raw = None
        try:
            raw = self._cli(
                ["markets", "list", "--active", "true", "--closed", "false",
                 "--limit", str(self.limit), "--order", "volume"],
                timeout=30.0,
            )
        except RuntimeError:
            raw = self._cli(
                ["markets", "list", "--active", "true", "--closed", "false",
                 "--limit", str(self.limit)],
                timeout=30.0,
            )
        if not isinstance(raw, list):
            return []

        rows: list[MarketRow] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            q = str(item.get("question") or item.get("slug") or "unknown")
            rows.append(MarketRow(
                question=q,
                condition_id=str(item.get("conditionId") or ""),
                slug=str(item.get("slug") or ""),
                best_bid=parse_opt_float(item.get("bestBid")),
                best_ask=parse_opt_float(item.get("bestAsk")),
                last_trade=parse_opt_float(item.get("lastTradePrice")),
                volume_24h=parse_float(item.get("volume24hr")),
                volume_total=parse_float(item.get("volume")),
                liquidity=parse_float(
                    item.get("liquidityClob"),
                    parse_float(item.get("liquidity")),
                ),
                outcomes=[str(x) for x in parse_json_array(item.get("outcomes"))],
                token_ids=parse_token_ids(item.get("clobTokenIds")),
                accepting_orders=bool(item.get("acceptingOrders")),
                one_day_change=parse_opt_float(item.get("oneDayPriceChange")),
                symbol_hint=guess_symbol(q),
            ))
        rows.sort(
            key=lambda m: (1 if m.accepting_orders else 0, m.volume_24h, m.volume_total),
            reverse=True,
        )
        return rows

    def _normalize_book(self, item: Any) -> BookSnapshot | None:
        if not isinstance(item, dict):
            return None
        bids = [
            (parse_float(b.get("price")), parse_float(b.get("size")))
            for b in item.get("bids", []) if isinstance(b, dict)
        ]
        asks = [
            (parse_float(a.get("price")), parse_float(a.get("size")))
            for a in item.get("asks", []) if isinstance(a, dict)
        ]
        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])
        return BookSnapshot(
            bids=bids, asks=asks,
            last_trade=parse_opt_float(item.get("last_trade_price")),
            tick_size=parse_opt_float(item.get("tick_size")),
            timestamp=str(item.get("timestamp") or ""),
        )

    def _fetch_books(self, token_ids: list[str]) -> dict[str, BookSnapshot]:
        if not token_ids:
            return {}
        raw = self._cli(["clob", "books", ",".join(token_ids)], timeout=20.0)
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            return {}
        out: dict[str, BookSnapshot] = {}
        for i, item in enumerate(raw):
            if i >= len(token_ids):
                break
            b = self._normalize_book(item)
            if b:
                out[token_ids[i]] = b
        return out

    def _fetch_leaderboard(self, period: str = "day") -> list[dict[str, Any]]:
        raw = self._cli(
            ["data", "leaderboard", "--period", period,
             "--order-by", "pnl", "--limit", "10"],
            timeout=16.0,
        )
        if not isinstance(raw, list):
            return []
        return [
            {
                "rank": int(item.get("rank") or 0),
                "name": str(
                    item.get("user_name")
                    or item.get("proxy_wallet")
                    or "anon"
                ),
                "pnl": parse_opt_float(item.get("pnl")),
                "volume": parse_opt_float(item.get("volume")),
            }
            for item in raw if isinstance(item, dict)
        ]

    def _fetch_events(self) -> list[dict[str, Any]]:
        raw = self._cli(
            ["events", "list", "--active", "true", "--closed", "false",
             "--limit", "8", "--order", "volume"],
            timeout=20.0,
        )
        if not isinstance(raw, list):
            return []
        evs = [
            {
                "title": str(item.get("title") or item.get("slug") or "unknown"),
                "volume": parse_float(
                    item.get("volume24hr"), parse_float(item.get("volume"))
                ),
            }
            for item in raw if isinstance(item, dict)
        ]
        evs.sort(key=lambda e: e["volume"], reverse=True)
        return evs[:10]

    def _derive_assets(self, markets: list[MarketRow]) -> list[dict[str, Any]]:
        syms = [
            "BTC", "ETH", "SOL", "XRP", "NVDA", "TSLA", "SPY", "QQQ",
            "AAPL", "MSFT", "AMZN", "META", "GOOGL", "COIN", "EUR/USD",
        ]
        out: list[dict[str, Any]] = []
        for sym in syms:
            m = next(
                (x for x in markets
                 if x.accepting_orders and x.symbol_hint == sym),
                None,
            )
            if m is None:
                continue
            out.append({
                "symbol": sym,
                "mid": m.mid,
                "change": m.one_day_change,
                "spread": m.spread,
                "question": m.question,
            })
        return out[:16]

    def _pick_panels(
        self, markets: list[MarketRow], focus: int,
    ) -> list[MarketRow]:
        chosen: list[MarketRow] = []
        for i in range(focus, len(markets)):
            if len(chosen) >= self.panels:
                break
            m = markets[i]
            if m.accepting_orders and m.token_ids and m not in chosen:
                chosen.append(m)
        for i in range(0, focus):
            if len(chosen) >= self.panels:
                break
            m = markets[i]
            if m.accepting_orders and m.token_ids and m not in chosen:
                chosen.append(m)
        return chosen

    # ── Price history & synthetic trades ──────────────────────────────────

    def _update_history(self, markets: list[MarketRow]) -> None:
        for m in markets:
            if m.mid is not None:
                key = m.condition_id or m.slug
                hist = self._price_hist.get(key, [])
                hist.append(m.mid)
                if len(hist) > PRICE_HISTORY_LEN:
                    hist = hist[-PRICE_HISTORY_LEN:]
                self._price_hist[key] = hist

    def _generate_trades(self, markets: list[MarketRow]) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        for m in markets[:25]:
            if m.mid is None or not m.accepting_orders:
                continue
            key = m.condition_id or m.slug
            prev_mid = self._prev_markets.get(key)
            if prev_mid is not None and abs(prev_mid - m.mid) > 0.001:
                side = "BUY" if m.mid > prev_mid else "SELL"
                size = random.uniform(500, 200000)
                self._trades.appendleft(TradeEntry(
                    timestamp=now, side=side,
                    price_cents=m.mid * 100.0,
                    size=size, market_name=m.question,
                ))
            elif random.random() < 0.12:
                side = random.choice(["BUY", "SELL"])
                px = m.mid * 100.0 + random.uniform(-2, 2)
                size = random.uniform(100, 50000)
                self._trades.appendleft(TradeEntry(
                    timestamp=now, side=side,
                    price_cents=max(0.1, px),
                    size=size, market_name=m.question,
                ))
        for m in markets:
            if m.mid is not None:
                self._prev_markets[m.condition_id or m.slug] = m.mid

    def _log(self, msg: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        self._logs.appendleft(f"{now}  {msg}")


# ─── Terminal UI ──────────────────────────────────────────────────────────────


class TerminalUI:
    """Curses-based Bloomberg-style terminal renderer."""

    def __init__(self, stdscr: Any, collector: DataCollector):
        self.stdscr = stdscr
        self.collector = collector

        self.selected = 0
        self.tape_off = 0
        self.tape_cache = ""
        self.palette = 0
        self.lb_period = "day"
        self.lb_label = "DAY"
        self.colors_ok = False
        self.frame = 0

        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.stdscr.nodelay(True)
        self.stdscr.timeout(100)
        self._init_colors()

    # ── Color management ──────────────────────────────────────────────────

    def _init_colors(self) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        self._apply_palette()
        self.colors_ok = True

    def _apply_palette(self) -> None:
        try:
            if self.palette == 0:
                curses.init_pair(CP_GREEN, curses.COLOR_GREEN, -1)
                curses.init_pair(CP_MAGENTA, curses.COLOR_MAGENTA, -1)
                curses.init_pair(CP_CYAN, curses.COLOR_CYAN, -1)
                curses.init_pair(CP_YELLOW, curses.COLOR_YELLOW, -1)
                curses.init_pair(CP_RED, curses.COLOR_RED, -1)
                curses.init_pair(CP_WHITE, curses.COLOR_WHITE, -1)
                curses.init_pair(CP_BLUE, curses.COLOR_BLUE, -1)
                curses.init_pair(CP_TICKER, curses.COLOR_GREEN, -1)
                curses.init_pair(CP_HEADER_BG, curses.COLOR_CYAN, -1)
                curses.init_pair(CP_SELECTED, curses.COLOR_BLACK, curses.COLOR_GREEN)
            elif self.palette == 1:
                curses.init_pair(CP_GREEN, curses.COLOR_YELLOW, -1)
                curses.init_pair(CP_MAGENTA, curses.COLOR_RED, -1)
                curses.init_pair(CP_CYAN, curses.COLOR_YELLOW, -1)
                curses.init_pair(CP_YELLOW, curses.COLOR_YELLOW, -1)
                curses.init_pair(CP_RED, curses.COLOR_RED, -1)
                curses.init_pair(CP_WHITE, curses.COLOR_YELLOW, -1)
                curses.init_pair(CP_BLUE, curses.COLOR_YELLOW, -1)
                curses.init_pair(CP_TICKER, curses.COLOR_YELLOW, -1)
                curses.init_pair(CP_HEADER_BG, curses.COLOR_YELLOW, -1)
                curses.init_pair(CP_SELECTED, curses.COLOR_BLACK, curses.COLOR_YELLOW)
            elif self.palette == 2:
                curses.init_pair(CP_GREEN, curses.COLOR_GREEN, -1)
                curses.init_pair(CP_MAGENTA, curses.COLOR_GREEN, -1)
                curses.init_pair(CP_CYAN, curses.COLOR_GREEN, -1)
                curses.init_pair(CP_YELLOW, curses.COLOR_GREEN, -1)
                curses.init_pair(CP_RED, curses.COLOR_GREEN, -1)
                curses.init_pair(CP_WHITE, curses.COLOR_GREEN, -1)
                curses.init_pair(CP_BLUE, curses.COLOR_GREEN, -1)
                curses.init_pair(CP_TICKER, curses.COLOR_GREEN, -1)
                curses.init_pair(CP_HEADER_BG, curses.COLOR_GREEN, -1)
                curses.init_pair(CP_SELECTED, curses.COLOR_BLACK, curses.COLOR_GREEN)
        except (curses.error, ValueError):
            pass

    # ── Main loop ─────────────────────────────────────────────────────────

    def run(self) -> None:
        while True:
            snap = self.collector.snapshot()
            if snap.markets:
                self.selected = clamp(self.selected, 0, len(snap.markets) - 1)
                self.collector.set_focus(self.selected)
            else:
                self.selected = 0
            if not self._input(snap):
                return
            self._draw(snap)
            self.frame += 1

    def _input(self, snap: Snapshot) -> bool:
        ch = self.stdscr.getch()
        if ch == -1:
            return True
        if ch in (ord("q"), ord("Q")):
            return False
        if ch in (ord("r"), ord("R")):
            self.collector.request_refresh()
        elif ch in (curses.KEY_UP, ord("k"), ord("K")):
            if snap.markets:
                self.selected = max(0, self.selected - 1)
                self.collector.set_focus(self.selected)
                self.collector.request_refresh()
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            if snap.markets:
                self.selected = min(len(snap.markets) - 1, self.selected + 1)
                self.collector.set_focus(self.selected)
                self.collector.request_refresh()
        elif ch == ord("1"):
            self.lb_period, self.lb_label = "day", "DAY"
            self.collector.set_lb_period("day")
        elif ch == ord("2"):
            self.lb_period, self.lb_label = "week", "WEEK"
            self.collector.set_lb_period("week")
        elif ch == ord("3"):
            self.lb_period, self.lb_label = "month", "MONTH"
            self.collector.set_lb_period("month")
        elif ch in (ord("p"), ord("P")):
            self.palette = (self.palette + 1) % len(PALETTE_NAMES)
            self._apply_palette()
        return True

    # ── Drawing primitives ────────────────────────────────────────────────

    def _put(
        self, y: int, x: int, text: str,
        cp: int = 0, attr: int = 0, w: int | None = None,
    ) -> None:
        my, mx = self.stdscr.getmaxyx()
        if y < 0 or y >= my or x >= mx:
            return
        if x < 0:
            text = text[-x:]
            x = 0
        if w is None:
            w = mx - x
        if w <= 0:
            return
        s = text[:w]
        try:
            if cp and self.colors_ok:
                self.stdscr.addnstr(y, x, s, w, curses.color_pair(cp) | attr)
            else:
                self.stdscr.addnstr(y, x, s, w, attr)
        except curses.error:
            pass

    def _box(
        self, y: int, x: int, h: int, w: int,
        title: str = "", cp: int = 0,
    ) -> None:
        if h < 2 or w < 2:
            return
        self._put(y, x, TL + HZ * (w - 2) + TR, cp)
        for r in range(y + 1, y + h - 1):
            self._put(r, x, VT, cp)
            self._put(r, x + w - 1, VT, cp)
        self._put(y + h - 1, x, BL + HZ * (w - 2) + BR, cp)
        if title:
            lbl = f" \u2605 {title} "
            self._put(y, x + 2, trunc(lbl, w - 4), cp, curses.A_BOLD)

    def _fill(
        self, y: int, x: int, w: int,
        text: str = "", cp: int = 0, attr: int = 0,
    ) -> None:
        padded = text.ljust(w)[:w]
        self._put(y, x, padded, cp, attr, w)

    # ── Main draw dispatcher ──────────────────────────────────────────────

    def _draw(self, snap: Snapshot) -> None:
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        if h < 24 or w < 110:
            self._put(
                1, 2,
                f"Terminal too small ({w}x{h}). Need at least 110x24.",
                CP_RED, curses.A_BOLD,
            )
            self._put(3, 2, "Press q to quit.", CP_WHITE)
            self.stdscr.refresh()
            return

        hdr_h = 4
        ftr_h = 1
        body_h = h - hdr_h - ftr_h

        left_w = max(44, int(w * 0.35))
        right_w = max(38, int(w * 0.28))
        center_w = w - left_w - right_w
        if center_w < 38:
            center_w = 38
            over = left_w + right_w + center_w - w
            if over > 0:
                left_w = max(36, left_w - over // 2)
                right_w = max(32, right_w - (over - over // 2))
                center_w = w - left_w - right_w

        mkt_h = int(body_h * 0.62)
        trade_h = body_h - mkt_h


        self._draw_header(snap, 0, 0, hdr_h, w)
        self._draw_markets(snap, hdr_h, 0, mkt_h, left_w)
        self._draw_trades(snap, hdr_h + mkt_h, 0, trade_h, left_w)
        self._draw_books(snap, hdr_h, left_w, body_h, center_w)
        self._draw_right(snap, hdr_h, left_w + center_w, body_h, right_w)
        self._draw_footer(snap, h - ftr_h, 0, ftr_h, w)

        self.stdscr.refresh()

    # ── Header with scrolling ticker ──────────────────────────────────────

    def _draw_header(
        self, snap: Snapshot, y: int, x: int, h: int, w: int,
    ) -> None:
        self._box(y, x, h, w, "", CP_CYAN)
        iw = w - 4

        # Row 1: scrolling ticker tape
        tape = self._build_tape(snap)
        if tape != self.tape_cache:
            self.tape_cache = tape
            self.tape_off = 0
        if not tape:
            tape = "loading market data..."
        doubled = tape + "   " + tape
        off = self.tape_off % max(1, len(tape) + 3)
        seg = doubled[off: off + iw]
        self._put(y + 1, x + 2, seg, CP_TICKER, curses.A_BOLD, iw)
        self.tape_off += 1

        # Row 2: POLY badge + status + time
        ts = datetime.now().strftime("%H:%M:%S")
        n_mkt = len(snap.markets)
        n_bk = len(snap.book_panels)
        self._put(y + 2, x + 2, " POLY ", CP_YELLOW, curses.A_BOLD | curses.A_REVERSE)
        status = (
            f" {ts}  MKT:{n_mkt}  EXEC:{snap.cycle}  "
            f"MDG:{n_bk}  MKT:LIVE  HTDS:LIVE"
        )
        self._put(y + 2, x + 8, status, CP_WHITE, 0, w - 20)

        # Center: terminal title + commands
        cmd = "POLYMARKET TERMINAL ~ r:refresh ~ 1/2/3:leaderboard ~ q:quit"
        cx = max(x + 40, x + (w - len(cmd)) // 2)
        self._put(y + 2, cx, cmd, CP_CYAN, 0, w - cx + x - 2)

        # Right timestamp
        self._put(y + 2, x + w - len(ts) - 3, ts, CP_WHITE)

    def _build_tape(self, snap: Snapshot) -> str:
        pieces: list[str] = []
        for m in snap.markets[:16]:
            if m.mid is None:
                continue
            q = trunc(m.question, 30)
            yes_str = fmt_cents(m.mid)
            pieces.append(f"{q}  YES:{yes_str}")
        return "  \u2605  ".join(pieces)

    # ── Markets panel ─────────────────────────────────────────────────────

    def _draw_markets(
        self, snap: Snapshot, y: int, x: int, h: int, w: int,
    ) -> None:
        self._box(y, x, h, w, "MARKETS", CP_CYAN)
        iw = w - 2
        if iw < 20 or h < 4:
            return

        name_w = max(10, iw - 34)
        hdr = (
            f"{'MARKET':<{name_w}}"
            f"{'YES':>6} {'NO':>6} {'CHART':>8} {'VOL':>10}"
        )
        self._put(y + 1, x + 1, trunc(hdr, iw), CP_YELLOW, curses.A_BOLD, iw)

        rows = h - 3
        if rows <= 0 or not snap.markets:
            self._put(
                y + 2, x + 2,
                "Loading markets from polymarket CLI...",
                CP_WHITE, 0, iw - 2,
            )
            return

        scroll = max(0, self.selected - rows // 2)
        scroll = min(scroll, max(0, len(snap.markets) - rows))
        visible = snap.markets[scroll: scroll + rows]

        for i, m in enumerate(visible):
            ai = scroll + i
            is_sel = ai == self.selected
            yes = m.mid if m.mid is not None else m.best_bid
            no = (1.0 - yes) if yes is not None else None

            key = m.condition_id or m.slug
            hist = snap.price_history.get(key, [])
            cbar = chart_bar(yes, 6) if yes is not None else "      "

            name = trunc(m.question, name_w - 1)
            line = (
                f"{name:<{name_w}}"
                f"{fmt_cents(yes):>6} "
                f"{fmt_cents(no):>5} "
                f"{cbar:>6} "
                f"{fmt_vol(m.volume_24h):>10}"
            )

            if is_sel:
                self._fill(y + 2 + i, x + 1, iw, line, CP_SELECTED, curses.A_BOLD)
            else:
                self._put(y + 2 + i, x + 1, trunc(line, iw), CP_GREEN, 0, iw)
                # Overlay chart in appropriate color
                chart_x = x + 1 + name_w + 13
                chart_cp = CP_GREEN if (m.one_day_change is None or m.one_day_change >= 0) else CP_RED
                self._put(y + 2 + i, chart_x, cbar, chart_cp, 0, 6)

    # ── Trade feed ────────────────────────────────────────────────────────

    def _draw_trades(
        self, snap: Snapshot, y: int, x: int, h: int, w: int,
    ) -> None:
        self._box(y, x, h, w, "TRADE FEED", CP_CYAN)
        iw = w - 2
        ih = h - 2
        if ih < 1 or iw < 20:
            return

        trades = snap.trades[:ih]
        if not trades:
            self._put(
                y + 1, x + 2, "Waiting for trade activity...",
                CP_WHITE, 0, iw - 2,
            )
            return

        for i, t in enumerate(trades):
            if i >= ih:
                break
            cp = CP_GREEN if t.side == "BUY" else CP_MAGENTA
            name = trunc(t.market_name, max(10, iw - 40))
            line = (
                f"{t.timestamp} & {t.side:<4} "
                f"{t.price_cents:>5.1f}\u00a2"
                f"+{t.size:>9,.1f} "
                f"{VT} {name}"
            )
            self._put(y + 1 + i, x + 1, trunc(line, iw), cp, 0, iw)

    # ── Orderbook panel ───────────────────────────────────────────────────

    def _draw_books(
        self, snap: Snapshot, y: int, x: int, h: int, w: int,
    ) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._box(y, x, h, w, f"LIVE ORDERBOOKS  {ts}", CP_CYAN)
        iw = w - 2
        ih = h - 2
        if ih < 6 or iw < 30:
            return

        if not snap.book_panels:
            self._put(
                y + 2, x + 2, "No active order books available.",
                CP_WHITE, 0, iw - 2,
            )
            return

        n = len(snap.book_panels)
        base_h = ih // n
        extra = ih % n
        cy = y + 1

        for pi, panel in enumerate(snap.book_panels):
            bh = base_h + (1 if pi < extra else 0)
            if bh < 5:
                cy += bh
                continue

            # Panel separator
            if pi > 0:
                self._put(cy, x + 1, HZ * iw, CP_CYAN)
                cy += 1
                bh -= 1

            # Market title
            self._put(
                cy, x + 2,
                trunc(panel.market.question, iw - 2),
                CP_WHITE, curses.A_BOLD, iw - 2,
            )
            cy += 1

            if panel.book is None:
                self._put(cy, x + 2, "book unavailable", CP_RED, 0, iw - 2)
                cy += bh - 1
                continue

            book = panel.book
            mid = panel.market.mid
            spread = panel.market.spread
            spr_bps = panel.market.spread_bps
            imbal = book.imbalance

            # Price stats line
            stats = (
                f"MID:{fmt_cents(mid)}  "
                f"SPRD:{fmt_cents(spread)} ({fmt_bps(spr_bps)})  "
                f"IMBAL:{fmt_pct(imbal)}"
            )
            self._put(cy, x + 2, trunc(stats, iw - 2), CP_YELLOW, 0, iw - 2)
            cy += 1

            # Depth stats line
            t_bid = book.total_bid
            t_ask = book.total_ask
            n_bid = len(book.bids)
            n_ask = len(book.asks)
            depth = (
                f"BID:${t_bid:,.0f} ({n_bid}lvl)  "
                f"ASK:${t_ask:,.0f} ({n_ask}lvl)"
            )
            self._put(cy, x + 2, trunc(depth, iw - 2), CP_WHITE, 0, iw - 2)
            cy += 1

            # Column header — adapt to available width
            half = (iw - 3) // 2
            bar_w = max(2, half - 22)
            col_hdr = f"{'SIZE':>7} {'BAR':>{bar_w}} {'BID':>6} {VT} {'ASK':<6} {'BAR':<{bar_w}} {'SIZE':<7}"
            self._put(
                cy, x + 2,
                trunc(col_hdr, iw - 2),
                CP_CYAN, curses.A_BOLD, iw - 2,
            )
            cy += 1

            # Bid/Ask rows
            data_rows = bh - 5
            if data_rows <= 0:
                continue

            bids = book.bids[:data_rows]
            asks = book.asks[:data_rows]
            max_bid_sz = max((s for _, s in bids), default=1.0) if bids else 1.0
            max_ask_sz = max((s for _, s in asks), default=1.0) if asks else 1.0
            cum_bid = 0.0
            cum_ask = 0.0

            for ri in range(data_rows):
                bid = bids[ri] if ri < len(bids) else None
                ask = asks[ri] if ri < len(asks) else None

                if bid:
                    cum_bid += bid[1]
                    b_sz = fmt_vol(bid[1])
                    b_bar = depth_bar(bid[1], max_bid_sz, bar_w)
                    b_px = fmt_cents(bid[0])
                else:
                    b_sz = "      "
                    b_bar = " " * bar_w
                    b_px = "    "

                if ask:
                    cum_ask += ask[1]
                    a_px = fmt_cents(ask[0])
                    a_bar = depth_bar(ask[1], max_ask_sz, bar_w)
                    a_sz = fmt_vol(ask[1])
                else:
                    a_px = "    "
                    a_bar = " " * bar_w
                    a_sz = "      "

                bid_half = f"{b_sz:>7} {b_bar} {b_px:>6}"
                ask_half = f"{a_px:<6} {a_bar} {a_sz:<7}"
                mid_sep = f" {VT} "

                self._put(cy, x + 2, bid_half, CP_GREEN, 0, half)
                self._put(cy, x + 2 + half, mid_sep, CP_CYAN, 0, 3)
                self._put(cy, x + 2 + half + 3, ask_half, CP_MAGENTA, 0, half)
                cy += 1

    # ── Right column ──────────────────────────────────────────────────────

    def _draw_right(
        self, snap: Snapshot, y: int, x: int, h: int, w: int,
    ) -> None:
        if h < 12:
            self._box(y, x, h, w, "DATA", CP_CYAN)
            return

        asset_h = max(6, h * 35 // 100)
        trader_h = max(5, h * 30 // 100)
        event_h = h - asset_h - trader_h

        self._draw_assets(snap, y, x, asset_h, w)
        self._draw_leaderboard(snap, y + asset_h, x, trader_h, w)
        self._draw_events(snap, y + asset_h + trader_h, x, event_h, w)

    # ── Assets panel ──────────────────────────────────────────────────────

    def _draw_assets(
        self, snap: Snapshot, y: int, x: int, h: int, w: int,
    ) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        n = len(snap.assets)
        title = f"LIVE ASSETS  {ts}  {n}/{n} feeds"
        self._box(y, x, h, w, title, CP_CYAN)

        iw = w - 2
        ih = h - 2
        if ih < 1 or iw < 20:
            return

        if not snap.assets:
            self._put(y + 1, x + 2, "No asset feeds.", CP_WHITE, 0, iw - 2)
            return

        col_w = iw // 2
        left_a = snap.assets[:8]
        right_a = snap.assets[8:16]

        for i, a in enumerate(left_a):
            if i >= ih:
                break
            sym = a.get("symbol", "---")
            mid = a.get("mid")
            chg = a.get("change")
            cp = CP_GREEN if (chg is None or chg >= 0) else CP_RED
            p_str = fmt_price(mid) if mid else "  --"
            c_str = fmt_change(chg) if chg is not None else ""
            line = f"{sym:<6}{p_str:>10} {c_str:>8}"
            self._put(y + 1 + i, x + 1, trunc(line, col_w), cp, 0, col_w)

        for i, a in enumerate(right_a):
            if i >= ih:
                break
            sym = a.get("symbol", "---")
            mid = a.get("mid")
            chg = a.get("change")
            cp = CP_GREEN if (chg is None or chg >= 0) else CP_RED
            p_str = fmt_price(mid) if mid else "  --"
            c_str = fmt_change(chg) if chg is not None else ""
            line = f"{sym:<6}{p_str:>10} {c_str:>8}"
            self._put(
                y + 1 + i, x + 1 + col_w,
                trunc(line, col_w), cp, 0, col_w,
            )

    # ── Leaderboard panel ─────────────────────────────────────────────────

    def _draw_leaderboard(
        self, snap: Snapshot, y: int, x: int, h: int, w: int,
    ) -> None:
        self._box(y, x, h, w, f"TRADERS ({self.lb_label})", CP_CYAN)
        iw = w - 2
        ih = h - 2
        if ih < 2:
            return

        hdr = f"{'#':>2} {'NAME':<16} {'PnL':>10}"
        self._put(y + 1, x + 1, trunc(hdr, iw), CP_YELLOW, curses.A_BOLD, iw)

        rows = ih - 1
        if not snap.leaderboard:
            self._put(
                y + 2, x + 2, "Loading traders...",
                CP_WHITE, 0, iw - 2,
            )
            return

        for i, t in enumerate(snap.leaderboard[:rows]):
            rank = t.get("rank", i + 1)
            name = trunc(str(t.get("name", "anon")), 14)
            pnl = t.get("pnl")
            pnl_str = fmt_money(pnl)
            cp = CP_GREEN if (pnl is not None and pnl >= 0) else CP_MAGENTA
            line = f"{rank:>2} {name:<16} {pnl_str:>10}"
            self._put(y + 2 + i, x + 1, trunc(line, iw), cp, 0, iw)

    # ── Events panel ──────────────────────────────────────────────────────

    def _draw_events(
        self, snap: Snapshot, y: int, x: int, h: int, w: int,
    ) -> None:
        self._box(y, x, h, w, "EVENTS", CP_CYAN)
        iw = w - 2
        ih = h - 2
        if ih < 2:
            return

        title_w = max(8, iw - 12)
        hdr = f"{'EVENT':<{title_w}} {'VOL':>10}"
        self._put(y + 1, x + 1, trunc(hdr, iw), CP_YELLOW, curses.A_BOLD, iw)

        rows = ih - 1
        if not snap.events:
            self._put(
                y + 2, x + 2, "Loading events...",
                CP_WHITE, 0, iw - 2,
            )
            return

        for i, ev in enumerate(snap.events[:rows]):
            title = trunc(str(ev.get("title", "")), title_w)
            vol = fmt_vol(parse_opt_float(ev.get("volume")))
            line = f"{title:<{title_w}} {vol:>10}"
            self._put(y + 2 + i, x + 1, trunc(line, iw), CP_WHITE, 0, iw)

    # ── Footer ────────────────────────────────────────────────────────────

    def _draw_footer(
        self, snap: Snapshot, y: int, x: int, h: int, w: int,
    ) -> None:
        pal = PALETTE_NAMES[self.palette]
        left = (
            " q:Quit  r:Refresh  1:LB:Day  2:LB:Week  3:LB:Month"
        )
        right = f"p:palette({pal}) "
        gap = w - len(left) - len(right)
        line = left + " " * max(1, gap) + right
        self._put(y, x, trunc(line, w), CP_YELLOW, curses.A_BOLD, w)


# ─── Entry Points ─────────────────────────────────────────────────────────────


def run_once(args: argparse.Namespace) -> int:
    c = DataCollector(args.refresh, args.market_limit, args.book_panels)
    snap = c.collect_once()
    print("Polymarket terminal probe")
    print(f"  markets:     {len(snap.markets)}")
    print(f"  book panels: {len(snap.book_panels)}")
    print(f"  leaderboard: {len(snap.leaderboard)}")
    print(f"  events:      {len(snap.events)}")
    print(f"  assets:      {len(snap.assets)}")
    if snap.markets:
        print("  top markets:")
        for m in snap.markets[:5]:
            print(f"    {m.question[:60]}  YES={fmt_cents(m.mid)}")
    return 0


def run_ui(args: argparse.Namespace) -> int:
    locale.setlocale(locale.LC_ALL, "")
    c = DataCollector(args.refresh, args.market_limit, args.book_panels)
    c.start()
    c.request_refresh()

    def app(stdscr: Any) -> None:
        TerminalUI(stdscr, c).run()

    try:
        curses.wrapper(app)
    finally:
        c.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bloomberg-style Polymarket Terminal (CLI-powered)",
    )
    p.add_argument(
        "--refresh", type=float, default=DEFAULT_REFRESH,
        help=f"refresh interval in seconds (default: {DEFAULT_REFRESH})",
    )
    p.add_argument(
        "--market-limit", type=int, default=MARKET_LIMIT,
        help=f"active markets to query (default: {MARKET_LIMIT})",
    )
    p.add_argument(
        "--book-panels", type=int, default=BOOK_PANELS,
        help=f"number of orderbook panels (default: {BOOK_PANELS})",
    )
    p.add_argument(
        "--once", action="store_true",
        help="single data probe, print summary (no UI)",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    return run_ui(args) if not args.once else run_once(args)


if __name__ == "__main__":
    raise SystemExit(main())
