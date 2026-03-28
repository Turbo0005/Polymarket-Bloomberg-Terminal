"""Textual Bloomberg-style Polymarket terminal."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Input, Static

from pm_terminal.cli_runner import CliError, PolymarketClient
from pm_terminal.models import MarketRow, parse_book_side


def _truncate(s: str, max_len: int = 56) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _fmt_num(x: float | None) -> str:
    if x is None:
        return "—"
    if x >= 1_000_000:
        return f"{x / 1_000_000:.2f}M"
    if x >= 1_000:
        return f"{x / 1_000:.1f}K"
    return f"{x:.2f}"


def _fmt_price(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.1f}¢"


def _condition_id(m: dict[str, Any]) -> str | None:
    cid = m.get("conditionId") or m.get("condition_id")
    if cid:
        return str(cid)
    return None


def _extract_yes_no_tokens(market: dict[str, Any], clob_market: dict[str, Any]) -> tuple[str | None, str | None]:
    ids = market.get("clobTokenIds")
    if isinstance(ids, list) and len(ids) >= 2:
        return str(ids[0]), str(ids[1])
    if isinstance(ids, list) and len(ids) == 1:
        return str(ids[0]), None

    tokens = clob_market.get("tokens")
    if isinstance(tokens, list) and tokens:
        yes_id: str | None = None
        no_id: str | None = None
        for t in tokens:
            if not isinstance(t, dict):
                continue
            tid = t.get("token_id") or t.get("tokenId")
            if tid is None:
                continue
            oc = t.get("outcome")
            if oc == "Yes":
                yes_id = str(tid)
            elif oc == "No":
                no_id = str(tid)
        if yes_id or no_id:
            return yes_id, no_id
        extracted: list[tuple[int, str]] = []
        for i, t in enumerate(tokens):
            if not isinstance(t, dict):
                continue
            tid = t.get("token_id") or t.get("tokenId")
            if tid is None:
                continue
            idx = t.get("outcomeIndex")
            if isinstance(idx, int):
                extracted.append((idx, str(tid)))
            else:
                extracted.append((i, str(tid)))
        extracted.sort(key=lambda x: x[0])
        if len(extracted) >= 2:
            return extracted[0][1], extracted[1][1]
        if len(extracted) == 1:
            return extracted[0][1], None

    nested = clob_market.get("clobTokenIds")
    if isinstance(nested, list) and len(nested) >= 2:
        return str(nested[0]), str(nested[1])
    return None, None


class PolymarketTerminalApp(App[None]):
    CSS_PATH = "app.tcss"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("slash", "focus_filter", "Filter"),
        ("escape", "clear_filter", "Clear filter"),
        ("j", "market_down", ""),
        ("k", "market_up", ""),
    ]

    def __init__(
        self,
        polymarket_bin: str = "polymarket",
        refresh_interval: float = 4.0,
        list_limit: int = 40,
        order_field: str = "volumeNum",
        subprocess_timeout: float = 60.0,
    ) -> None:
        super().__init__()
        self.client = PolymarketClient(binary=polymarket_bin, timeout=subprocess_timeout)
        self.refresh_interval = refresh_interval
        self.list_limit = list_limit
        self.order_field = order_field
        self._markets: list[MarketRow] = []
        self._filter_substr: str = ""
        self._selected_key: str | None = None
        self._detail_raw: dict[str, Any] = {}
        self._last_list_error: str | None = None
        self._last_detail_error: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(" POLYMARKET  │  loading…", id="header")
        with Horizontal(id="main"):
            with Vertical(id="left-pane"):
                yield DataTable(id="markets", zebra_stripes=True)
            with Vertical(id="right-pane"):
                yield Static("", id="detail")
                with Horizontal(id="book-row"):
                    yield DataTable(id="bids", zebra_stripes=True)
                    yield DataTable(id="asks", zebra_stripes=True)
        with Horizontal(id="footer"):
            yield Static(" / filter  │  r refresh  │  j/k move  │  q quit ", id="footer-hint")
            yield Input(placeholder=" filter question… ", id="filter-input")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#markets", DataTable)
        table.cursor_type = "row"
        table.add_columns("Question", "Yes", "Volume", "Liq")

        bids = self.query_one("#bids", DataTable)
        asks = self.query_one("#asks", DataTable)
        bids.cursor_type = "row"
        asks.cursor_type = "row"
        bids.add_columns("Bid px", "Size")
        asks.add_columns("Ask px", "Size")

        table.focus()

        self.set_interval(self.refresh_interval, self._schedule_list_refresh)
        self.run_worker(self._worker_initial_list, thread=True, exclusive=True)

    def _schedule_list_refresh(self) -> None:
        self.run_worker(self._worker_list_refresh, thread=True, exclusive=True)

    def _worker_initial_list(self) -> None:
        self._run_list_sync()
        self.call_from_thread(self._after_list, True)

    def _worker_list_refresh(self) -> None:
        self._run_list_sync()
        self.call_from_thread(self._after_list, False)

    def _run_list_sync(self) -> None:
        try:
            raw = self.client.markets_list(
                limit=self.list_limit,
                order=self.order_field,
                closed="false",
            )
            self._last_list_error = None
            self._markets = [MarketRow.from_api(x) for x in raw]
        except CliError as e:
            self._last_list_error = str(e)
            self._markets = []

    def _after_list(self, refresh_selection: bool) -> None:
        self._render_header()
        self._populate_market_table()
        if refresh_selection or not self._selected_key:
            self._select_first_row()
        else:
            self._restore_selection()

    def _render_header(self) -> None:
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        err = self._last_list_error or ""
        line = (
            f" POLYMARKET  │  {now}  │  interval {self.refresh_interval:g}s  │  n={len(self._filtered_rows())} "
        )
        if err:
            line += f" │  ERR: {err[:80]}"
        self.query_one("#header", Static).update(line)

    def _filtered_rows(self) -> list[MarketRow]:
        q = self._filter_substr.strip().lower()
        if not q:
            return self._markets
        return [m for m in self._markets if q in m.question.lower()]

    def _populate_market_table(self) -> None:
        table = self.query_one("#markets", DataTable)
        table.clear(columns=False)
        rows = self._filtered_rows()
        for m in rows:
            key = m.id or m.slug
            table.add_row(
                _truncate(m.question, 52),
                _fmt_price(m.yes_price),
                _fmt_num(m.volume),
                _fmt_num(m.liquidity),
                key=key,
            )

    def _select_first_row(self) -> None:
        table = self.query_one("#markets", DataTable)
        rows = self._filtered_rows()
        if not rows:
            self._selected_key = None
            self._detail_raw = {}
            self._update_detail_static()
            self._clear_books()
            return
        if table.row_count:
            table.move_cursor(row=0)

    def _restore_selection(self) -> None:
        table = self.query_one("#markets", DataTable)
        if not self._selected_key or not table.row_count:
            self._select_first_row()
            return
        for i, m in enumerate(self._filtered_rows()):
            k = m.id or m.slug
            if k == self._selected_key:
                table.move_cursor(row=i)
                return
        self._select_first_row()

    def _schedule_detail_refresh(self) -> None:
        self.run_worker(self._worker_detail, thread=True, exclusive=True)

    def _row_raw_for_key(self, key: str | None) -> dict[str, Any]:
        if not key:
            return {}
        for m in self._markets:
            if (m.id or m.slug) == key:
                return m.raw
        return {}

    def _worker_detail(self) -> None:
        key = self._selected_key
        if not key:
            self.call_from_thread(self._apply_detail, {}, None, None, None, None, "")
            return
        detail: dict[str, Any] = {}
        mids: dict[str, Any] | None = None
        book: dict[str, Any] | None = None
        err_parts: list[str] = []
        try:
            detail = self.client.markets_get(key)
        except CliError as e:
            err_parts.append(f"get: {e}")
            detail = {}
        list_raw = self._row_raw_for_key(key)
        cid = _condition_id(detail) or _condition_id(list_raw)
        yes_t: str | None = None
        no_t: str | None = None
        clob_m: dict[str, Any] = {}
        if cid:
            try:
                clob_m = self.client.clob_market(cid)
            except CliError as e:
                err_parts.append(f"clob market: {e}")
        merged = {**list_raw, **detail}
        yes_t, no_t = _extract_yes_no_tokens(merged, clob_m)
        tokens_for_mid = [t for t in (yes_t, no_t) if t]
        if tokens_for_mid:
            try:
                mids = self.client.clob_batch_midpoints(tokens_for_mid)
            except CliError as e:
                err_parts.append(f"midpoints: {e}")
        if yes_t:
            try:
                book = self.client.clob_book(yes_t)
            except CliError as e:
                err_parts.append(f"book: {e}")
        err = "; ".join(err_parts) if err_parts else ""
        self.call_from_thread(self._apply_detail, detail, mids, book, yes_t, no_t, err)

    def _apply_detail(
        self,
        detail: dict[str, Any],
        mids: dict[str, Any] | None,
        book: dict[str, Any] | None,
        yes_t: str | None,
        no_t: str | None,
        err: str,
    ) -> None:
        list_raw = self._row_raw_for_key(self._selected_key)
        self._detail_raw = {**list_raw, **detail}
        self._last_detail_error = err or None
        self._update_detail_static(mids, yes_t, no_t)
        self._update_book_tables(book)

    def _update_detail_static(
        self,
        mids: dict[str, Any] | None = None,
        yes_t: str | None = None,
        no_t: str | None = None,
    ) -> None:
        d = self._detail_raw
        q = str(d.get("question") or d.get("title") or "—")
        lines = [f"[bold]{q}[/bold]"]
        row = MarketRow.from_api(d) if d else None
        if row:
            lines.append(
                f"Yes [green]{_fmt_price(row.yes_price)}[/]   "
                f"No [red]{_fmt_price(row.no_price)}[/]   "
                f"Vol {_fmt_num(row.volume)}   Liq {_fmt_num(row.liquidity)}"
            )
        cid = _condition_id(d)
        if cid:
            lines.append(f"conditionId [cyan]{_truncate(cid, 72)}[/]")
        if yes_t or no_t:
            lines.append(f"YES token [yellow]{yes_t or '—'}[/]   NO token [yellow]{no_t or '—'}[/]")
        if mids:
            lines.append(f"midpoints [magenta]{_truncate(repr(mids), 100)}[/]")
        if self._last_detail_error:
            lines.append(f"[red]{self._last_detail_error}[/]")
        self.query_one("#detail", Static).update("\n".join(lines))

    def _update_book_tables(self, book: dict[str, Any] | None) -> None:
        bids_t = self.query_one("#bids", DataTable)
        asks_t = self.query_one("#asks", DataTable)
        bids_t.clear(columns=False)
        asks_t.clear(columns=False)
        if not book:
            return
        bids_raw = book.get("bids") or book.get("buys") or []
        asks_raw = book.get("asks") or book.get("sells") or []
        bs = parse_book_side(bids_raw)
        ar = parse_book_side(asks_raw)
        for lv in bs.levels[:16]:
            bids_t.add_row(f"{lv.price:.4f}", f"{lv.size:.4f}")
        for lv in ar.levels[:16]:
            asks_t.add_row(f"{lv.price:.4f}", f"{lv.size:.4f}")

    def _clear_books(self) -> None:
        self.query_one("#bids", DataTable).clear(columns=False)
        self.query_one("#asks", DataTable).clear(columns=False)

    @on(DataTable.RowHighlighted, "#markets")
    def on_markets_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        rk = event.row_key.value
        self._selected_key = str(rk) if rk is not None else None
        self._schedule_detail_refresh()

    def action_quit(self) -> None:
        self.exit()

    def action_refresh(self) -> None:
        self.run_worker(self._worker_list_refresh, thread=True, exclusive=True)
        self.run_worker(self._worker_detail, thread=True, exclusive=True)

    def action_focus_filter(self) -> None:
        self.query_one("#filter-input", Input).focus()

    def action_clear_filter(self) -> None:
        fi = self.query_one("#filter-input", Input)
        if fi.has_focus:
            fi.value = ""
            self._filter_substr = ""
            fi.blur()
            self.query_one("#markets", DataTable).focus()
            self._populate_market_table()
            self._select_first_row()

    def action_market_down(self) -> None:
        if self.query_one("#filter-input", Input).has_focus:
            return
        self.query_one("#markets", DataTable).action_cursor_down()

    def action_market_up(self) -> None:
        if self.query_one("#filter-input", Input).has_focus:
            return
        self.query_one("#markets", DataTable).action_cursor_up()

    @on(Input.Changed, "#filter-input")
    def filter_changed(self, event: Input.Changed) -> None:
        self._filter_substr = event.value
        self._populate_market_table()
        self._select_first_row()
