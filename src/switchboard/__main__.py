"""`python -m switchboard` launches the session manager."""

from __future__ import annotations

import sys

from switchboard.app import main


def run() -> int:
    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(run())
