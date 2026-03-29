# Polymarket Bloomberg Terminal

A Bloomberg-style terminal interface for real-time Polymarket prediction market data, built entirely in Python using only the standard library (`curses`).

## Features

- **Scrolling ticker tape** with live market prices in cents
- **Markets list** with YES/NO prices, chart bars, and volume
- **3 stacked orderbook panels** with depth bars, spread in bps, imbalance %, cumulative size, and bid/ask level counts
- **Live assets panel** in two-column layout showing derived crypto/stock prices
- **Traders leaderboard** with switchable periods (Day/Week/Month)
- **Events panel** with 24h volume
- **Real-time trade feed** with color-coded BUY/SELL entries
- **3 color palettes** — Bloomberg (default), Amber, Matrix
- **Unicode box-drawing** characters for clean terminal borders
- **Keyboard navigation** — j/k or arrows to scroll markets, r to refresh, q to quit

## Prerequisites

- Python 3.10+
- [Polymarket CLI](https://github.com/Polymarket/polymarket-cli) installed and configured

```bash
# Install the polymarket CLI
npm install -g @polymarket/cli

# Configure it
polymarket setup
```

## Usage

```bash
# Run the terminal UI
python3 polymarket_terminal.py

# Custom refresh interval (seconds)
python3 polymarket_terminal.py --refresh 10

# Limit number of markets queried
python3 polymarket_terminal.py --market-limit 60

# Adjust number of orderbook panels (1-4)
python3 polymarket_terminal.py --book-panels 2

# Single data probe (no UI)
python3 polymarket_terminal.py --once
```

## Keyboard Controls

| Key | Action |
|-----|--------|
| `q` | Quit |
| `r` | Force refresh |
| `j` / `↓` | Select next market |
| `k` / `↑` | Select previous market |
| `1` | Leaderboard: Day |
| `2` | Leaderboard: Week |
| `3` | Leaderboard: Month |
| `p` | Cycle color palette |

## Terminal Requirements

Minimum terminal size: **110 columns x 24 rows**. For the best experience, use a full-screen terminal with 160+ columns.

## Architecture

```
polymarket CLI (subprocess + JSON)
        │
        ▼
DataCollector (background thread)
        │
        ▼
    Snapshot (dataclass)
        │
        ▼
  TerminalUI (curses)
   ┌────┼────────────────────────┐
   │    │    ┌─────┐ ┌─────────┐ │
   │ Markets │Books│ │ Assets  │ │
   │         │     │ │ Traders │ │
   │ Trades  │     │ │ Events  │ │
   └─────────┴─────┘ └─────────┘ │
   │         Footer               │
   └──────────────────────────────┘
```

## Dependencies

**None beyond Python's standard library.** The application uses `curses`, `subprocess`, `json`, `threading`, `argparse`, `dataclasses`, `collections`, `datetime`, `time`, `locale`, and `random`.

Data is sourced exclusively via the `polymarket` CLI tool.
