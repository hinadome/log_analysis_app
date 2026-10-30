#!/usr/bin/env python3
"""Simulated long-running scan.

Demonstrates: async execution, live streaming of stdout while the job runs,
artifact files written to $JOB_OUTPUT_DIR, and deliberate failure via --fail.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", help="Path to the log file")
    parser.add_argument("--seconds", type=int, default=15,
                        help="Total run time in seconds")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="Seconds between progress updates")
    parser.add_argument("--fail", action="store_true",
                        help="Exit with code 1 at the end (failure demo)")
    args = parser.parse_args()

    lines = 0
    try:
        with open(args.log, encoding="utf-8", errors="replace") as fh:
            for _ in fh:
                lines += 1
    except OSError as exc:
        print(f"error: cannot read log file: {exc}", file=sys.stderr)
        return 2

    interval = max(args.interval, 0.05)
    steps = max(1, int(round(args.seconds / interval)))
    for i in range(1, steps + 1):
        time.sleep(interval)
        pct = 100 * i // steps
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{stamp}] {pct:3d}%  scanned {lines * i // steps}/{lines} lines", flush=True)

    out_dir = os.environ.get("JOB_OUTPUT_DIR")
    if out_dir:
        summary_path = os.path.join(out_dir, "summary.txt")
        with open(summary_path, "w", encoding="utf-8") as fh:
            fh.write(f"scan of     : {args.log}\n")
            fh.write(f"lines       : {lines}\n")
            fh.write(f"duration    : {args.seconds}s\n")
            fh.write(f"status      : {'FAILED (as requested)' if args.fail else 'OK'}\n")
        print(f"summary written to {summary_path}")

    if args.fail:
        print("scan finished with an error (as requested)", file=sys.stderr)
        return 1
    print("scan finished successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
