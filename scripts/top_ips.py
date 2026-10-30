#!/usr/bin/env python3
"""Rank the most active client IPs in an access log."""
import argparse
import json
import re
import sys
from collections import Counter

IP_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", help="Path to the log file")
    parser.add_argument("--top", type=int, default=10, help="How many IPs to show")
    parser.add_argument("--format", choices=["text", "json"], default="text",
                        help="Output format")
    args = parser.parse_args()

    counter: Counter = Counter()
    try:
        with open(args.log, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                match = IP_RE.search(line)
                if match:
                    counter[match.group(1)] += 1
    except OSError as exc:
        print(f"error: cannot read log file: {exc}", file=sys.stderr)
        return 2

    top = counter.most_common(max(args.top, 0))
    total = sum(counter.values())
    unique = len(counter)

    if args.format == "json":
        print(json.dumps({
            "log": args.log,
            "total_requests": total,
            "unique_ips": unique,
            "top": [{"rank": i, "ip": ip, "requests": n} for i, (ip, n) in enumerate(top, 1)],
        }, indent=2))
        return 0

    print(f"Access log    : {args.log}")
    print(f"Total requests: {total} from {unique} unique IPs")
    print()
    if not top:
        print("No IP addresses found in this file.")
        return 0
    width = max(len(str(n)) for _, n in top)
    max_n = top[0][1]
    for rank, (ip, n) in enumerate(top, 1):
        bar = "#" * max(1, round(30 * n / max_n))
        pct = 100 * n / total if total else 0
        print(f"{rank:>3}. {ip:<16} {n:>{width}}  {pct:5.1f}%  {bar}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
