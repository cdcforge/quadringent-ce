#!/usr/bin/env python3
"""Read-only Popsink target Statistics parser.

Never talks to the Popsink API. Callers feed `kubectl logs` text on stdin
or via --log-file. No secrets, no mutations.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quadringent_research.popsink_readonly_lag import (  # noqa: E402
    DEFAULT_MAX_LAG,
    evaluate_tail,
    parse_statistics_text,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-file")
    parser.add_argument("--max-lag", type=int, default=DEFAULT_MAX_LAG)
    args = parser.parse_args()
    text = open(args.log_file, errors="replace").read() if args.log_file else sys.stdin.read()
    result = evaluate_tail(parse_statistics_text(text), max_lag=args.max_lag)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result["calm"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
