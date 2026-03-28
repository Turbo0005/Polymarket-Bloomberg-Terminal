"""CLI entry: `python -m pm_terminal` or `pm-term`."""

from __future__ import annotations

import argparse
import os
import sys

from pm_terminal.app import PolymarketTerminalApp


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bloomberg-style Polymarket terminal (polymarket CLI + JSON).",
    )
    p.add_argument(
        "--polymarket",
        default=os.environ.get("POLYMARKET_BIN", "polymarket"),
        help="Path to polymarket executable (default: polymarket or $POLYMARKET_BIN).",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("PM_TERM_INTERVAL", "4")),
        metavar="SEC",
        help="Seconds between list refreshes (default: 4 or $PM_TERM_INTERVAL).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=int(os.environ.get("PM_TERM_LIMIT", "40")),
        metavar="N",
        help="markets list --limit (default: 40 or $PM_TERM_LIMIT).",
    )
    p.add_argument(
        "--order",
        default=os.environ.get("PM_TERM_ORDER", "volumeNum"),
        help="markets list --order field (default: volumeNum; API uses camelCase).",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("PM_TERM_TIMEOUT", "60")),
        metavar="SEC",
        help="Subprocess timeout per CLI invocation (default: 60).",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.interval < 0.5:
        print("--interval must be >= 0.5", file=sys.stderr)
        sys.exit(2)
    app = PolymarketTerminalApp(
        polymarket_bin=args.polymarket,
        refresh_interval=args.interval,
        list_limit=args.limit,
        order_field=args.order,
        subprocess_timeout=args.timeout,
    )
    app.run()


if __name__ == "__main__":
    main()
