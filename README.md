# Polymarket Bloomberg Terminal

Bloomberg-style Polymarket terminal in the terminal. Uses the official [Polymarket CLI](https://github.com/Polymarket/polymarket-cli) with `-o json` and polls on an interval.

(near real-time; the CLI does not stream)

## Prerequisites

- Python 3.11+
- `polymarket` on your `PATH` (see upstream install: Homebrew tap or install script).

## Install

```bash
cd PolyMarket_BT
python3 -m venv .venv && source .venv/bin/activate  # optional
pip install -e .
```

## Run

```bash
pm-term
# or: python -m pm_terminal
```

### Options / environment


| Flag           | Env                | Default      | Meaning                                   |
| -------------- | ------------------ | ------------ | ----------------------------------------- |
| `--polymarket` | `POLYMARKET_BIN`   | `polymarket` | CLI binary path                           |
| `--interval`   | `PM_TERM_INTERVAL` | `4`          | Seconds between market list refreshes     |
| `--limit`      | `PM_TERM_LIMIT`    | `40`         | `markets list --limit`                    |
| `--order`      | `PM_TERM_ORDER`    | `volumeNum`  | `markets list --order` (camelCase fields) |
| `--timeout`    | `PM_TERM_TIMEOUT`  | `60`         | Subprocess timeout (seconds)              |


## Keys

- `q` — quit  
- `r` — refresh list and selection  
- `j` / `k` or arrows — move selection  
- `/` — focus filter (substring on question, client-side)  
- `Escape` — clear filter when filter is focused

Updates are polling-based; lower `--interval` refreshes more often but loads the API more heavily.