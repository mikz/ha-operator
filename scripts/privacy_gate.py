"""Reject publishable artifacts that still require privacy redaction."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.lab.redaction import sanitize_artifacts  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", type=Path)
    args = parser.parse_args()
    for directory in args.directories:
        if not directory.is_dir():
            raise ValueError("Publication directory does not exist")
        sanitize_artifacts(directory, [], check=True)
    print("Publication privacy checks passed")


if __name__ == "__main__":
    main()
