#!/usr/bin/env python3
"""Validate a sanitized trace or replay it inside the prepared, isolated HA lab.

Prepare the exact release first: python scripts/lab.py prepare --ha-version 2026.9.3.
This script never connects to an external Home Assistant or accepts its credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lab import SUPPORTED, run_lab  # noqa: E402
from tests.lab.shadow_trace import load_trace  # noqa: E402


def main():
    if Path.cwd().resolve() != ROOT:
        raise SystemExit(f"Run this command from {ROOT}")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "run"))
    parser.add_argument("trace", type=Path, help="Sanitized export or {schema:1,pages:[exports]}")
    parser.add_argument("--ha-version", choices=SUPPORTED, default=SUPPORTED[0])
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    trace = load_trace(args.trace)
    if args.command == "validate":
        print(json.dumps(trace.report, indent=2, sort_keys=True))
        return 0 if trace.report["replay_complete"] else 2
    args.scenario, args.keep = "replay", False
    published = run_lab(args)
    print(f"Replay receipt: {published / 'shadow-replay.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
