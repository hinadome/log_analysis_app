#!/usr/bin/env python3
"""A log-free sample: a host + filesystem usage snapshot.

The command has no {log} placeholder, so LogSmith runs it without a
selected log — handy for scripts that generate rather than analyze
input. Output is a JSON object, so the UI renders it as a table.
"""
import json
import platform
import shutil
import socket
import sys
from datetime import datetime, timezone


def main() -> int:
    usage = shutil.disk_usage(".")
    report = {
        "host": socket.gethostname(),
        "system": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "disk_total_gb": round(usage.total / 2**30, 1),
        "disk_used_gb": round(usage.used / 2**30, 1),
        "disk_free_gb": round(usage.free / 2**30, 1),
        "disk_free_pct": round(100 * usage.free / usage.total, 1),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
