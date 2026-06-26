#!/usr/bin/env python3
"""Shape an accepted source plan into a durable execution contract."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from plan_automation_lib import shape_contract


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--auto-approve", action="store_true", help="Record contract approval without waiting for Discord")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = shape_contract(Path.cwd(), args.plan_id, auto_approve=args.auto_approve)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
