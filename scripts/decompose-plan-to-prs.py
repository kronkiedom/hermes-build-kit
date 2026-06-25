#!/usr/bin/env python3
"""Decompose an approved plan contract into PR-sized durable task packets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from plan_automation_lib import decompose_plan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-id", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = decompose_plan(Path.cwd(), args.plan_id)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
