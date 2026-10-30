#!/usr/bin/env python3
"""Generate a Markdown report from an nginx-style access log.

Demonstrates the {output} placeholder: the workbench fills in a path inside
the job's output directory and serves whatever the script writes there.
"""
import argparse
import re
import sys
from collections import Counter
from datetime import datetime, timezone

LINE_RE = re.compile(
    r'(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] '
    r'"(?P<method>\S+) (?P<path>\S+)[^"]*" (?P<status>\d{3}) (?P<bytes>\S+)'
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", help="Path to the access log")
    parser.add_argument("--top", type=int, default=10, help="Rows per top-list")
    parser.add_argument("--output", required=True, help="Where to write the Markdown report")
    args = parser.parse_args()

    total = 0
    bytes_total = 0
    status_counter: Counter = Counter()
    path_counter: Counter = Counter()
    ip_counter: Counter = Counter()

    try:
        with open(args.log, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = LINE_RE.match(line)
                if not m:
                    continue
                total += 1
                status_counter[m.group("status")] += 1
                path_counter[m.group("path")] += 1
                ip_counter[m.group("ip")] += 1
                try:
                    bytes_total += int(m.group("bytes"))
                except ValueError:
                    pass
    except OSError as exc:
        print(f"error: cannot read log file: {exc}", file=sys.stderr)
        return 2

    def bucket(status: str) -> str:
        s = int(status)
        if s < 300:
            return "2xx success"
        if s < 400:
            return "3xx redirect"
        if s < 500:
            return "4xx client error"
        return "5xx server error"

    buckets: Counter = Counter()
    for status, n in status_counter.items():
        buckets[bucket(status)] += n

    def md_table(rows, headers):
        out = ["| " + " | ".join(headers) + " |",
               "|" + "|".join("---" for _ in headers) + "|"]
        for row in rows:
            out.append("| " + " | ".join(str(c) for c in row) + " |")
        return out

    lines = [
        "# Access log report",
        "",
        f"- **Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"- **Source:** `{args.log}`",
        f"- **Requests analyzed:** {total}",
        f"- **Bytes transferred:** {bytes_total:,}",
        "",
        "## Response classes",
        "",
        *md_table([(k, v) for k, v in buckets.most_common()], ["class", "requests"]),
        "",
        "## Status codes",
        "",
        *md_table([(f"`{s}`", n) for s, n in status_counter.most_common()], ["status", "count"]),
        "",
        f"## Top {args.top} paths",
        "",
        *md_table([(i, p, n) for i, (p, n) in enumerate(path_counter.most_common(args.top), 1)],
                  ["#", "path", "requests"]),
        "",
        f"## Top {args.top} client IPs",
        "",
        *md_table([(i, ip, n) for i, (ip, n) in enumerate(ip_counter.most_common(args.top), 1)],
                  ["#", "ip", "requests"]),
        "",
    ]

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    print(f"Report written to {args.output}")
    print(f"Analyzed {total} requests, {len(status_counter)} distinct status codes, "
          f"{len(ip_counter)} unique clients.")
    print(f"Top path: {path_counter.most_common(1)[0][0] if path_counter else '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
