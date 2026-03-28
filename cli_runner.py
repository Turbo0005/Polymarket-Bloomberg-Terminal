"""Subprocess wrapper for `polymarket -o json` with JSON parsing and errors."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any


class CliError(Exception):
    def __init__(self, message: str, stderr: str | None = None):
        super().__init__(message)
        self.stderr = stderr


def parse_outcome_prices(raw: Any) -> list[float]:
    """Normalize outcomePrices: may be JSON string or list of strings."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[float] = []
    for x in raw:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            continue
    return out


class PolymarketClient:
    def __init__(
        self,
        binary: str = "polymarket",
        timeout: float = 60.0,
    ):
        self.binary = binary
        self.timeout = timeout

    def _run(self, *args: str) -> Any:
        if not shutil.which(self.binary) and "/" not in self.binary:
            raise CliError(f"CLI not found: {self.binary!r} (not on PATH)")
        cmd = (self.binary, "-o", "json", *args)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise CliError(f"Command timed out: {' '.join(cmd)}") from e
        except OSError as e:
            raise CliError(f"Failed to run {self.binary!r}: {e}") from e

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        if not out:
            if proc.returncode != 0:
                raise CliError(err or "Empty stdout and non-zero exit", stderr=err)
            return None

        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise CliError(f"Invalid JSON from CLI: {e!s}\n{out[:500]}", stderr=err) from e

        if isinstance(data, dict) and "error" in data:
            msg = str(data.get("error") or "unknown error")
            raise CliError(msg, stderr=err)

        if proc.returncode != 0:
            raise CliError(err or f"CLI exited {proc.returncode}", stderr=err)

        return data

    def markets_list(
        self,
        limit: int = 40,
        order: str = "volumeNum",
        closed: str = "false",
    ) -> list[dict[str, Any]]:
        data = self._run(
            "markets",
            "list",
            "--limit",
            str(limit),
            "--order",
            order,
            "--closed",
            closed,
        )
        if data is None:
            return []
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    def markets_get(self, id_or_slug: str) -> dict[str, Any]:
        data = self._run("markets", "get", id_or_slug)
        if not isinstance(data, dict):
            return {}
        return data

    def clob_market(self, condition_id: str) -> dict[str, Any]:
        data = self._run("clob", "market", condition_id)
        return data if isinstance(data, dict) else {}

    def clob_batch_midpoints(self, token_ids: list[str]) -> dict[str, Any]:
        if not token_ids:
            return {}
        joined = ",".join(token_ids)
        data = self._run("clob", "midpoints", joined)
        return data if isinstance(data, dict) else {}

    def clob_book(self, token_id: str) -> dict[str, Any]:
        data = self._run("clob", "book", token_id)
        return data if isinstance(data, dict) else {}
