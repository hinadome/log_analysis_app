#!/usr/bin/env python3
"""Count log lines by severity level.

Reads a log file, extracts the severity token from each line and reports how
many lines match the requested level. A quick "how bad is it?" check.
"""
import argparse
import json
import re
import sys
from collections import Counter

LEVEL_RE = re.compile(r"\b(ERROR|ERR|WARN(?:ING)?|INFO|DEBUG|FATAL|CRITICAL|TRACE)\b", re.IGNORECASE)
ALIAS = {"WARNING": "WARN", "ERR": "ERROR", "CRITICAL": "FATAL"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", help="Path to the log file")
    parser.add_argument("--level", default="ERROR",
                        choices=["ERROR", "WARN", "INFO", "DEBUG", "FATAL", "ALL"],
                        help="Severity level to count (default: ERROR)")
    parser.add_argument("--pattern", default="", help="Only count lines containing this substring")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    level = args.level.upper()
    alias = {"WARN": {"WARN"}, "ERROR": {"ERROR"}, "FATAL": {"FATAL"}}
    wanted = alias.get(level) if level != "ALL" else None

    counts: Counter = Counter()
    total = 0
    try:
        with open(args.log, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                total += 1
                if args.pattern and args.pattern not in line:
                    continue
                match = LEVEL_RE.search(line)
                token = ALIAS.get(match.group(1).upper(), match.group(1).upper()) if match else "OTHER"
                counts[token] += 1
    except OSError as exc:
        print(f"error: cannot read log file: {exc}", file=sys.stderr)
        return 2

    matched = sum(n for t, n in counts.items() if wanted is None or t in wanted)

    if args.json:
        print(json.dumps({
            "log": args.log,
            "level": level,
            "pattern": args.pattern or None,
            "total_lines": total,
            "matching_lines": matched,
            "breakdown": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        }, indent=2))
        return 0

    print(f"Log file       : {args.log}")
    print(f"Total lines    : {total}")
    print(f"Pattern filter : {args.pattern or '(none)'}")
    print()
    print(f"{'LEVEL':<10}{'COUNT':>10}   BAR")
    for token, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        bar = "#" * max(1, round(40 * n / max(total, 1)))
        marker = " <-" if wanted and token in wanted else ""
        print(f"{token:<10}{n:>10}   {bar}{marker}")
    print()
    print(f"=> {matched} lines match level={level} ({100 * matched / max(total, 1):.1f}% of file)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
