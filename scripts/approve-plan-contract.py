#!/usr/bin/env python3
"""Record an operator decision for a shaped plan contract."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from plan_automation_lib import record_contract_approval


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--decision", choices=["APPROVE", "REJECT", "CANCEL", "approve", "reject", "cancel"], default="APPROVE")
    parser.add_argument("--source", default="manual")
    parser.add_argument("--message-id", default=None)
    args = parser.parse_args()
    result = record_contract_approval(
        Path.cwd(),
        args.plan_id,
        decision=args.decision,
        source=args.source,
        message_id=args.message_id,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
