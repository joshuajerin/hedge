#!/usr/bin/env python3
"""Run Hedge's file-only paper backtest sandbox.

This command reads local JSON and CSV files only. It imports no Hedge broker,
does not read environment variables, and has no code path that submits orders.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# Allow a checkout to run this script without an editable installation.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hedge.backtest import BacktestBar, BacktestConfig, ScheduledDecision, run_backtest  # noqa: E402
from hedge.contracts import Decision  # noqa: E402


CONFIG_KEYS = {"mode", "initial_cash", "commission_per_order", "slippage_bps", "fill_delay_bars"}


def _load_config(path: Path) -> BacktestConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config must be a JSON object")
    unknown = set(raw) - CONFIG_KEYS
    if unknown:
        raise ValueError(f"config has unsupported keys: {', '.join(sorted(unknown))}")
    if raw.get("mode") != "paper":
        raise ValueError("sandbox config must set mode to exactly 'paper'")
    return BacktestConfig(**{key: value for key, value in raw.items() if key != "mode"})


def _load_bars(path: Path) -> list[BacktestBar]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    required = {"timestamp", "symbol", "open", "high", "low", "close"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("bars CSV must include timestamp,symbol,open,high,low,close headers")
    return [
        BacktestBar(
            symbol=row["symbol"], timestamp=row["timestamp"], open=row["open"],
            high=row["high"], low=row["low"], close=row["close"],
            volume=row.get("volume") or None,
        )
        for row in rows
    ]


def _load_decisions(path: Path) -> list[ScheduledDecision]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("scheduled_decisions") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError("decisions must be an object with a scheduled_decisions list")
    result: list[ScheduledDecision] = []
    for entry in entries:
        if not isinstance(entry, dict) or "timestamp" not in entry or "decision" not in entry:
            raise ValueError("each scheduled decision needs timestamp and decision")
        if not isinstance(entry["decision"], dict):
            raise ValueError("scheduled decision must be an object")
        result.append(ScheduledDecision(entry["timestamp"], Decision.from_dict(entry["decision"])))
    return result


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _result_document(result: Any) -> dict[str, Any]:
    return {
        "mode": "paper",
        "metrics": _json_value(dict(result.metrics)),
        "positions": dict(result.positions),
        "ledger": _json_value(result.ledger),
        "equity_curve": _json_value(result.equity_curve),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a deterministic local Hedge paper backtest.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Optional local JSON output path.")
    args = parser.parse_args()

    result = run_backtest(_load_decisions(args.decisions), _load_bars(args.bars), config=_load_config(args.config))
    output = json.dumps(_result_document(result), indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
