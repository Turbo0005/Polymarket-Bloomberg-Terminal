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
import subprocess
import urllib.request
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ─── Constants ────────────────────────────────────────────────────────────────

DEFAULT_REFRESH = 6.0
MARKET_LIMIT = 120
TRADE_CAPACITY = 80
LOG_CAPACITY = 200
PRICE_HISTORY_LEN = 12

TICKER_MARKET_LIMIT = 16
TRADE_API_URL = "https://data-api.polymarket.com/trades"
TRADE_FETCH_LIMIT = 25
TRADE_API_TIMEOUT = 10.0
TRADE_SEEN_CAP = 500

CLI_TIMEOUT_DEFAULT = 20.0
CLI_TIMEOUT_MARKETS = 30.0
CLI_TIMEOUT_BOOKS = 20.0

# Unicode box-drawing
TL, TR, BL, BR = "┌", "┐", "└", "┘"
HZ, VT = "─", "│"
TJ, BJ, LJ, RJ, CX = "┬", "┴", "├", "┤", "┼"

SPARK = "▁▂▃▄▅▆▇█"

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


def word_wrap(text: str, w: int) -> list[str]:
    """Simple word-wrap that respects width *w*."""
    if w <= 0:
        return []
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        cur = words[0]
        for word in words[1:]:
            if len(cur) + 1 + len(word) <= w:
                cur += " " + word
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


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


def fmt_notional(v: float | None) -> str:
    """Format USD order-book notionals (price × size); keeps decimals when < $1."""
    if v is None:
        return "  --"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1_000_000:
        return f"{sign}${a / 1_000_000:,.1f}M"
    if a >= 1_000:
        return f"{sign}${a / 1_000:,.1f}K"
    if a >= 100:
        return f"{sign}${a:,.0f}"
    if a >= 1:
        return f"{sign}${a:.1f}"
    if a >= 0.01:
        return f"{sign}${a:.2f}"
    if a > 1e-9:
        return f"{sign}${a:.3f}"
    return f"{sign}$0"


def fmt_shares(v: float | None) -> str:
    """Format order-book level size (share / contract count)."""
    if v is None:
        return "  --"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1_000_000:
        return f"{sign}{a / 1_000_000:,.1f}M"
    if a >= 1_000:
        return f"{sign}{a / 1_000:,.1f}K"
    if a >= 100:
        return f"{sign}{a:,.0f}"
    if a >= 1:
        t = f"{sign}{a:,.2f}"
        return t.rstrip("0").rstrip(".") if "." in t else t
    if a > 1e-9:
        return f"{sign}{a:.3g}"
    return f"{sign}0"


def orderbook_triple_widths(half: int) -> tuple[int, int, int]:
    """Balanced column widths for PRICE / SHARE / TOTAL; two spaces between; sum = *half*."""
    sp = 2
    rem = half - sp
    if rem < 3:
        return 1, 1, max(1, rem - 2)

    # Ideal widths: PRICE 6, SHARE 6, TOTAL 6 (needs rem=18 / half=20).
    # Minimums: PRICE 5 ("99.0¢"), SHARE 5 ("SHARE"), TOTAL 5 ("$1.2K").
    ideal_px, ideal_sh, ideal_tot = 6, 6, 6
    min_px, min_sh, min_tot = 5, 5, 5

    if rem >= ideal_px + ideal_sh + ideal_tot:
        extra = rem - ideal_px - ideal_sh - ideal_tot
        # Distribute surplus evenly: TOT first, then SHARE, then PR.
        tot_w = ideal_tot + extra // 3
        sh_w = ideal_sh + (extra - extra // 3) // 2
        px_w = rem - sh_w - tot_w
        return px_w, sh_w, tot_w

    if rem >= min_px + min_sh + min_tot:
        short = (min_px + min_sh + min_tot) - rem
        px_w = min_px
        sh_w = min_sh
        tot_w = rem - px_w - sh_w
        if tot_w < min_tot:
            sh_w -= min_tot - tot_w
            tot_w = min_tot
        return px_w, max(3, sh_w), tot_w

    # Very narrow: split as evenly as possible.
    px_w = max(2, rem // 3)
    sh_w = max(2, (rem - px_w) // 2)
    tot_w = rem - px_w - sh_w
    return px_w, sh_w, max(2, tot_w)


def pad_orderbook_cell(text: str, width: int, *, right: bool = True) -> str:
    """Truncate then pad so each row stays a fixed character width."""
    if len(text) > width:
        text = trunc(text, width)
    return text.rjust(width) if right else text.ljust(width)


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
    event_id: str
    event_title: str
    group_item_title: str
    end_date: str
    description: str

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
    def total_bid_usd(self) -> float:
        return sum(p * s for p, s in self.bids)

    @property
    def total_ask_usd(self) -> float:
        return sum(p * s for p, s in self.asks)


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
    outcome: str = ""


@dataclass
class Snapshot:
    markets: list[MarketRow] = field(default_factory=list)
    book_panels: list[BookPanel] = field(default_factory=list)
    trades: list[TradeEntry] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    refreshed_at: float = 0.0
    cycle: int = 0
    last_error: str = ""
    price_history: dict[str, list[float]] = field(default_factory=dict)


# ─── Data Collector ───────────────────────────────────────────────────────────


class DataCollector:
    """Background thread that polls the polymarket CLI and builds snapshots."""

    def __init__(self, refresh: float, limit: int):
        self.refresh = max(1.0, refresh)
        self.limit = max(20, limit)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._force = threading.Event()
        self._thread: threading.Thread | None = None

        self._snapshot = Snapshot()
        self._logs: deque[str] = deque(maxlen=LOG_CAPACITY)
        self._trades: deque[TradeEntry] = deque(maxlen=TRADE_CAPACITY)
        self._focus = 0

        self._seen_tx: set[str] = set()
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

    def request_refresh(self) -> None:
        self._force.set()

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(
                markets=list(self._snapshot.markets),
                book_panels=list(self._snapshot.book_panels),
                trades=list(self._trades),
                logs=list(self._logs),
                refreshed_at=self._snapshot.refreshed_at,
                cycle=self._snapshot.cycle,
                last_error=self._snapshot.last_error,
                price_history=dict(self._price_hist),
            )

    def collect_once(self) -> Snapshot:
        return self._cycle(1)

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

    def _cycle(self, cycle: int) -> Snapshot:
        with self._lock:
            focus = self._focus
            prev = self._snapshot

        errors: list[str] = []

        try:
            markets = self._fetch_markets()
        except Exception as e:
            markets = list(prev.markets)
            errors.append(f"markets: {e}")

        self._update_history(markets)

        try:
            self._fetch_trades()
        except Exception as e:
            self._log(f"trades: {e}")

        focus = clamp(focus, 0, max(0, len(markets) - 1))
        panel_market = self._pick_focus_market(markets, focus)
        token_ids = [panel_market.token_ids[0]] if (panel_market and panel_market.token_ids) else []

        try:
            books = self._fetch_books(token_ids)
        except Exception as e:
            books = {}
            errors.append(f"books: {e}")

        panels = []
        if panel_market and token_ids:
            tid = token_ids[0]
            panels.append(BookPanel(market=panel_market, token_id=tid, book=books.get(tid)))

        if errors:
            self._log("partial: " + "; ".join(errors))
        else:
            self._log(f"ok: {len(markets)}mkt {len(panels)}bk")

        return Snapshot(
            markets=markets,
            book_panels=panels,
            trades=list(self._trades),
            logs=list(self._logs),
            refreshed_at=time.time(),
            cycle=cycle,
            last_error="; ".join(errors),
            price_history=dict(self._price_hist),
        )

    # ── CLI runner ────────────────────────────────────────────────────────

    def _cli(self, args: list[str], timeout: float = CLI_TIMEOUT_DEFAULT) -> Any:
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
        # NOTE: Don't pass `--order volume`.
        # In some polymarket-cli versions, ordering by `volume` causes the
        # returned volume/liquidity fields to be zero, which then makes
        # the UI show incorrect constant values.
        raw = self._cli(
            ["markets", "list", "--active", "true", "--closed", "false",
             "--limit", str(self.limit)],
            timeout=CLI_TIMEOUT_MARKETS,
        )
        if not isinstance(raw, list):
            return []

        rows: list[MarketRow] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            q = str(item.get("question") or item.get("slug") or "unknown")
            event_data = item.get("events") or []
            ev0 = event_data[0] if isinstance(event_data, list) and event_data else {}
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
                event_id=str(ev0.get("id") or ""),
                event_title=str(ev0.get("title") or ""),
                group_item_title=str(item.get("groupItemTitle") or ""),
                end_date=str(item.get("endDateIso") or ""),
                description=str(item.get("description") or ""),
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
        raw = self._cli(
            ["clob", "books", ",".join(token_ids)],
            timeout=CLI_TIMEOUT_BOOKS,
        )
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

    def _pick_focus_market(
        self, markets: list[MarketRow], focus: int,
    ) -> MarketRow | None:
        if not markets:
            return None
        idx = clamp(focus, 0, len(markets) - 1)
        m = markets[idx]
        if m.accepting_orders and m.token_ids:
            return m
        for i in range(idx, len(markets)):
            if markets[i].accepting_orders and markets[i].token_ids:
                return markets[i]
        for i in range(0, idx):
            if markets[i].accepting_orders and markets[i].token_ids:
                return markets[i]
        return m

    # ── Price history & live trades ───────────────────────────────────────

    def _update_history(self, markets: list[MarketRow]) -> None:
        for m in markets:
            if m.mid is not None:
                key = m.condition_id or m.slug
                hist = self._price_hist.get(key, [])
                hist.append(m.mid)
                if len(hist) > PRICE_HISTORY_LEN:
                    hist = hist[-PRICE_HISTORY_LEN:]
                self._price_hist[key] = hist

    def _fetch_trades(self) -> None:
        url = f"{TRADE_API_URL}?limit={TRADE_FETCH_LIMIT}&takerOnly=true"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "PolymarketTerminal/3.1",
        })
        with urllib.request.urlopen(req, timeout=TRADE_API_TIMEOUT) as resp:
            raw = json.loads(resp.read())
        if not isinstance(raw, list):
            return
        new_entries: list[TradeEntry] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            tx = str(item.get("transactionHash") or "")
            if tx and tx in self._seen_tx:
                continue
            if tx:
                self._seen_tx.add(tx)
            ts_unix = item.get("timestamp")
            if ts_unix is not None:
                ts_str = datetime.fromtimestamp(int(ts_unix)).strftime("%H:%M:%S")
            else:
                ts_str = datetime.now().strftime("%H:%M:%S")
            price = parse_float(item.get("price"))
            size = parse_float(item.get("size"))
            new_entries.append(TradeEntry(
                timestamp=ts_str,
                side=str(item.get("side") or "BUY").upper(),
                price_cents=price * 100.0,
                size=price * size,
                market_name=str(item.get("title") or "unknown"),
                outcome=str(item.get("outcome") or ""),
            ))
        for entry in reversed(new_entries):
            self._trades.appendleft(entry)
        if len(self._seen_tx) > TRADE_SEEN_CAP:
            self._seen_tx = set(list(self._seen_tx)[-TRADE_SEEN_CAP // 2:])

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

        # Wider center column for orderbook (PRICE / SHARE / TOTAL); narrower side panels.
        left_w = max(36, int(w * 0.27))
        right_w = max(34, int(w * 0.25))
        center_w = w - left_w - right_w
        min_center = 46
        for _ in range(40):
            if center_w >= min_center:
                break
            if left_w > 30:
                left_w -= 1
            elif right_w > 28:
                right_w -= 1
            else:
                break
            center_w = w - left_w - right_w

        self._draw_header(snap, 0, 0, hdr_h, w)
        self._draw_markets(snap, hdr_h, 0, body_h, left_w)
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

        ts = datetime.now().strftime("%H:%M:%S")
        n_mkt = len(snap.markets)
        status_tag = "ERR" if snap.last_error else "LIVE"
        self._put(y + 2, x + 2, " POLY ", CP_YELLOW, curses.A_BOLD | curses.A_REVERSE)
        status = f" {ts}  MKT:{n_mkt}  EXEC:{snap.cycle}  MKT:{status_tag}"
        self._put(y + 2, x + 8, status, CP_WHITE, 0, w - 20)

        cmd = "POLYMARKET TERMINAL"
        cx = max(x + 40, x + (w - len(cmd)) // 2)
        self._put(y + 2, cx, cmd, CP_CYAN, curses.A_BOLD, w - cx + x - 2)

        self._put(y + 2, x + w - len(ts) - 3, ts, CP_WHITE)

    def _build_tape(self, snap: Snapshot) -> str:
        pieces: list[str] = []
        for m in snap.markets[:TICKER_MARKET_LIMIT]:
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

        yes_w, no_w, vol_w = 6, 6, 8
        fixed_w = yes_w + 1 + no_w + 1 + vol_w
        name_w = max(8, iw - fixed_w)
        hdr = (
            f"{'MARKET':<{name_w}}"
            f"{'YES':>{yes_w}} {'NO':>{no_w}} {'VOL':>{vol_w}}"
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

            name = trunc(m.question, name_w - 1)
            line = (
                f"{name:<{name_w}}"
                f"{fmt_cents(yes):>{yes_w}} "
                f"{fmt_cents(no):>{no_w}} "
                f"{fmt_vol(m.volume_24h):>{vol_w}}"
            )

            if is_sel:
                self._fill(y + 2 + i, x + 1, iw, line, CP_SELECTED, curses.A_BOLD)
            else:
                self._put(y + 2 + i, x + 1, trunc(line, iw), CP_GREEN, 0, iw)

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
            label = t.market_name
            if t.outcome:
                label = f"{t.market_name} [{t.outcome}]"
            name = trunc(label, max(8, iw - 32))
            usd = fmt_money(t.size) if t.size else "$0"
            line = (
                f"{t.timestamp}  {t.side:<4} "
                f"{t.price_cents:>5.1f}\u00a2 "
                f"{usd:>6} "
                f"{VT} {name}"
            )
            self._put(y + 1 + i, x + 1, trunc(line, iw), cp, 0, iw)

    # ── Orderbook panel (single market, full height) ─────────────────────

    def _draw_books(
        self, snap: Snapshot, y: int, x: int, h: int, w: int,
    ) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._box(y, x, h, w, f"LIVE ORDERBOOK  {ts}", CP_CYAN)
        iw = w - 2
        ih = h - 2
        if ih < 6 or iw < 30:
            return

        if not snap.book_panels:
            self._put(
                y + 2, x + 2, "No active order book available.",
                CP_WHITE, 0, iw - 2,
            )
            return

        panel = snap.book_panels[0]
        cy = y + 1

        self._put(
            cy, x + 2,
            trunc(panel.market.question, iw - 2),
            CP_WHITE, curses.A_BOLD, iw - 2,
        )
        cy += 1

        if panel.book is None:
            self._put(cy, x + 2, "book unavailable", CP_RED, 0, iw - 2)
            return

        book = panel.book
        mid = panel.market.mid
        spread = panel.market.spread
        spr_bps = panel.market.spread_bps

        stats = (
            f"MID:{fmt_cents(mid)}  "
            f"SPRD:{fmt_cents(spread)} ({fmt_bps(spr_bps)})"
        )
        self._put(cy, x + 2, trunc(stats, iw - 2), CP_YELLOW, 0, iw - 2)
        cy += 1

        t_bid = book.total_bid_usd
        t_ask = book.total_ask_usd
        depth = (
            f"BIDS:{fmt_notional(t_bid)}  "
            f"ASKS:{fmt_notional(t_ask)}"
        )
        self._put(cy, x + 2, trunc(depth, iw - 2), CP_WHITE, 0, iw - 2)
        cy += 1

        bid_half = (iw - 3) // 2
        ask_half_w = iw - 2 - bid_half - 3  # remaining interior after left-pad + bid + separator
        half = min(bid_half, ask_half_w)     # use the smaller side for column-width calc
        px_w, sh_w, tot_w = orderbook_triple_widths(half)
        mid_sep = f" {VT} "

        def _ob_hcell(label: str, wcol: int, *, right: bool) -> str:
            return pad_orderbook_cell(trunc(label, wcol), wcol, right=right)

        band_bid = "BID".center(bid_half)[:bid_half]
        band_ask = "ASK".center(ask_half_w)[:ask_half_w]
        self._put(
            cy, x + 2, band_bid, CP_CYAN, curses.A_BOLD, bid_half,
        )
        self._put(cy, x + 2 + bid_half, mid_sep, CP_CYAN, curses.A_BOLD, 3)
        self._put(
            cy, x + 2 + bid_half + 3, band_ask, CP_CYAN, curses.A_BOLD, ask_half_w,
        )
        cy += 1

        hdr_bid = (
            f"{_ob_hcell('PRICE', px_w, right=True)} "
            f"{_ob_hcell('SHARE', sh_w, right=True)} "
            f"{_ob_hcell('TOTAL', tot_w, right=True)}"
        ).ljust(bid_half)[:bid_half]
        hdr_ask = (
            f"{_ob_hcell('PRICE', px_w, right=False)} "
            f"{_ob_hcell('SHARE', sh_w, right=False)} "
            f"{_ob_hcell('TOTAL', tot_w, right=False)}"
        ).ljust(ask_half_w)[:ask_half_w]
        self._put(cy, x + 2, hdr_bid, CP_CYAN, curses.A_BOLD, bid_half)
        self._put(cy, x + 2 + bid_half, mid_sep, CP_CYAN, curses.A_BOLD, 3)
        self._put(cy, x + 2 + bid_half + 3, hdr_ask, CP_CYAN, curses.A_BOLD, ask_half_w)
        cy += 1

        data_rows = (y + h - 1) - cy
        if data_rows <= 0:
            return

        bids = book.bids[:data_rows]
        asks = book.asks[:data_rows]

        for ri in range(data_rows):
            bid = bids[ri] if ri < len(bids) else None
            ask = asks[ri] if ri < len(asks) else None

            if bid:
                px_b, sh_b = bid[0], bid[1]
                usd_b = px_b * sh_b
                b_p = pad_orderbook_cell(fmt_cents(px_b), px_w)
                b_s = pad_orderbook_cell(fmt_shares(sh_b), sh_w)
                b_t = pad_orderbook_cell(fmt_notional(usd_b), tot_w)
            else:
                b_p = " " * px_w
                b_s = " " * sh_w
                b_t = " " * tot_w

            if ask:
                px_a, sh_a = ask[0], ask[1]
                usd_a = px_a * sh_a
                a_p = pad_orderbook_cell(fmt_cents(px_a), px_w, right=False)
                a_s = pad_orderbook_cell(fmt_shares(sh_a), sh_w, right=False)
                a_t = pad_orderbook_cell(fmt_notional(usd_a), tot_w, right=False)
            else:
                a_p = " " * px_w
                a_s = " " * sh_w
                a_t = " " * tot_w

            row_bid = f"{b_p} {b_s} {b_t}".ljust(bid_half)[:bid_half]
            row_ask = f"{a_p} {a_s} {a_t}".ljust(ask_half_w)[:ask_half_w]

            self._put(cy, x + 2, row_bid, CP_GREEN, 0, bid_half)
            self._put(cy, x + 2 + bid_half, mid_sep, CP_CYAN, 0, 3)
            self._put(cy, x + 2 + bid_half + 3, row_ask, CP_MAGENTA, 0, ask_half_w)
            cy += 1

    # ── Right column: Market Detail + Related Markets ────────────────────

    def _draw_right(
        self, snap: Snapshot, y: int, x: int, h: int, w: int,
    ) -> None:
        if h < 8:
            self._box(y, x, h, w, "DATA", CP_CYAN)
            return

        detail_h = max(8, h * 60 // 100)
        related_h = h - detail_h

        self._draw_market_detail(snap, y, x, detail_h, w)
        self._draw_related_markets(snap, y + detail_h, x, related_h, w)

    def _draw_market_detail(
        self, snap: Snapshot, y: int, x: int, h: int, w: int,
    ) -> None:
        self._box(y, x, h, w, "MARKET DETAIL", CP_CYAN)
        iw = w - 4
        ih = h - 2
        if ih < 2 or iw < 16:
            return

        if not snap.markets:
            self._put(y + 1, x + 2, "No market selected.", CP_WHITE, 0, iw)
            return

        sel = clamp(self.selected, 0, len(snap.markets) - 1)
        m = snap.markets[sel]
        cy = y + 1

        # Question (word-wrapped)
        q_lines = word_wrap(m.question, iw)
        for ql in q_lines[:3]:
            if cy >= y + h - 1:
                break
            self._put(cy, x + 2, ql, CP_WHITE, curses.A_BOLD, iw)
            cy += 1

        if cy < y + h - 1:
            cy += 1

        # End date
        if m.end_date and cy < y + h - 1:
            self._put(cy, x + 2, f"Ends:  {m.end_date}", CP_CYAN, 0, iw)
            cy += 1

        if cy < y + h - 1:
            cy += 1

        # Stats block
        stats = [
            ("YES", fmt_cents(m.mid)),
            ("NO", fmt_cents(1.0 - m.mid if m.mid is not None else None)),
            ("Spread", f"{fmt_cents(m.spread)} ({fmt_bps(m.spread_bps)})"),
            ("Vol 24h", fmt_vol(m.volume_24h)),
            ("Vol Total", fmt_vol(m.volume_total)),
            ("Liquidity", fmt_vol(m.liquidity)),
        ]
        for label, val in stats:
            if cy >= y + h - 1:
                break
            line = f"{label + ':':<12}{val}"
            self._put(cy, x + 2, trunc(line, iw), CP_YELLOW, 0, iw)
            cy += 1

        # Intentionally stop after core stats; trend and description are hidden.

    def _draw_related_markets(
        self, snap: Snapshot, y: int, x: int, h: int, w: int,
    ) -> None:
        self._box(y, x, h, w, "RELATED MARKETS", CP_CYAN)
        iw = w - 4
        ih = h - 2
        if ih < 2 or iw < 16:
            return

        if not snap.markets:
            self._put(y + 1, x + 2, "No data.", CP_WHITE, 0, iw)
            return

        sel = clamp(self.selected, 0, len(snap.markets) - 1)
        m = snap.markets[sel]

        siblings: list[MarketRow] = []
        if m.event_id:
            siblings = [
                s for s in snap.markets
                if s.event_id == m.event_id and s.condition_id != m.condition_id
            ]

        if not siblings:
            self._put(y + 1, x + 2, "No related markets for this event.", CP_WHITE, 0, iw)
            return

        name_w = max(8, iw - 18)
        hdr = f"{'MARKET':<{name_w}} {'YES':>6} {'VOL':>10}"
        self._put(y + 1, x + 2, trunc(hdr, iw), CP_YELLOW, curses.A_BOLD, iw)

        rows = ih - 1
        for i, s in enumerate(siblings[:rows]):
            if i + 2 > ih:
                break
            label = s.group_item_title if s.group_item_title else s.question
            name = trunc(label, name_w - 1)
            yes = fmt_cents(s.mid)
            vol = fmt_vol(s.volume_24h)
            line = f"{name:<{name_w}} {yes:>6} {vol:>10}"
            cp = CP_GREEN if (s.one_day_change is None or s.one_day_change >= 0) else CP_RED
            self._put(y + 2 + i, x + 2, trunc(line, iw), cp, 0, iw)

    # ── Footer ────────────────────────────────────────────────────────────

    def _draw_footer(
        self, snap: Snapshot, y: int, x: int, h: int, w: int,
    ) -> None:
        pal = PALETTE_NAMES[self.palette]
        left = " q:Quit  r:Refresh  j/k:Navigate  p:Palette"
        right = f"palette({pal}) "
        gap = w - len(left) - len(right)
        line = left + " " * max(1, gap) + right
        self._put(y, x, trunc(line, w), CP_YELLOW, curses.A_BOLD, w)


# ─── Entry Points ─────────────────────────────────────────────────────────────


def run_once(args: argparse.Namespace) -> int:
    c = DataCollector(args.refresh, args.market_limit)
    snap = c.collect_once()
    print("Polymarket terminal probe")
    print(f"  markets:     {len(snap.markets)}")
    print(f"  book panels: {len(snap.book_panels)}")
    if snap.markets:
        print("  top markets:")
        for m in snap.markets[:5]:
            print(f"    {m.question[:60]}  YES={fmt_cents(m.mid)}")
    return 0


def run_ui(args: argparse.Namespace) -> int:
    locale.setlocale(locale.LC_ALL, "")
    c = DataCollector(args.refresh, args.market_limit)
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
        "--once", action="store_true",
        help="single data probe, print summary (no UI)",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    return run_ui(args) if not args.once else run_once(args)


if __name__ == "__main__":
    raise SystemExit(main())
