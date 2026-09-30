"""Thin wrapper kept for muscle memory: the featured paths and their renders are the pipeline
stage ``paths`` (musichistory/paths/, contract data/audio/renders/paths.json v2).

Usage: python tools/render_paths.py [--pick-only] [--force] [--auto] ...
   ==  python -m musichistory paths [same options]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def argv_for(args: list[str]) -> list[str]:
    return ["paths", *args]


def main(argv: list[str] | None = None) -> int:
    from musichistory.cli import main as cli_main

    return cli_main(argv_for(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
