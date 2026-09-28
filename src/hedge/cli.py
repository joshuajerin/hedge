"""Local Hedge commands.

IBKR paper submission is deliberately disabled while Slack and Brainbase use
Yahoo Finance research data. The retained adapter lives in ``broker.py`` so it
can be re-enabled later without rebuilding the integration.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contracts import Decision
from .market_data import latest_close
from .policy import PaperPolicy


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hedge")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="Validate a Brainbase decision without contacting a broker.")
    validate.add_argument("decision", type=Path)
    submit = subparsers.add_parser(
        "submit-paper",
        help="Disabled. The retained local IBKR paper adapter is not part of the Yahoo Finance flow.",
    )
    submit.add_argument("decision", type=Path)
    quote = subparsers.add_parser("quote", help="Read the latest daily close from Yahoo Finance.")
    quote.add_argument("symbol")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "quote":
        quote = latest_close(args.symbol)
        print(
            json.dumps(
                {
                    "symbol": quote.symbol,
                    "close": quote.close,
                    "as_of": quote.as_of.isoformat(),
                    "source_url": quote.source_url,
                }
            )
        )
        return

    decision = Decision.from_dict(json.loads(args.decision.read_text()))
    policy = PaperPolicy()
    policy.validate(decision)
    if args.command == "validate":
        print(json.dumps({"ok": True, "decision_id": decision.decision_id, "intents": len(decision.intents)}))
        return

    # The former submit-paper implementation intentionally remains in
    # broker.py and store.py but is not imported or invoked by this command.
    raise SystemExit(
        "IBKR paper submission is disabled. Hedge currently uses Yahoo Finance "
        "for research and produces reviewed proposals only."
    )


if __name__ == "__main__":
    main()
