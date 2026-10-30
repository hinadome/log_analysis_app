#!/usr/bin/env python3
"""Regenerate the sample logs in logs/ (deterministic)."""
import random
from datetime import datetime, timedelta
from pathlib import Path

LOGS = Path(__file__).resolve().parent.parent / "logs"
random.seed(2026)

USERS = ["alice", "bob", "carol", "dave", "eve", "frank", "grace", "heidi"]
IPS = [f"10.0.{i}.{j}" for i in range(1, 6) for j in range(1, 21)] + [
    "203.0.113.7", "198.51.100.24", "192.0.2.88", "203.0.113.99",
]
PATHS = ["/api/users", "/api/orders", "/api/search", "/login", "/logout",
         "/health", "/static/app.js", "/static/style.css", "/api/items", "/report"]
METHODS = ["GET", "GET", "GET", "POST", "POST", "PUT", "DELETE"]

BASE = datetime(2026, 10, 28, 14, 0, 0)


def app_log(n: int) -> list[str]:
    levels = ["INFO"] * 55 + ["DEBUG"] * 25 + ["WARN"] * 12 + ["ERROR"] * 7 + ["FATAL"]
    services = ["auth", "db", "api", "cache", "worker", "scheduler"]
    messages = {
        "INFO": ["user login succeeded (user={u})", "request handled in {ms}ms",
                 "cache warmed, {n} entries", "job picked up by worker-{w}",
                 "response sent to {u}", "session refreshed for {u}"],
        "DEBUG": ["heap usage {p}%", "connection pool: {n} active",
                  "query plan: index scan on idx_users", "gc pause {ms}ms",
                  "retry policy: next backoff {ms}ms"],
        "WARN": ["slow query {ms}ms: SELECT * FROM events",
                 "retrying request after transient error (attempt {n})",
                 "disk usage at {p}%", "connection reset by peer",
                 "rate limit approaching for {u} (90%)"],
        "ERROR": ["database connection lost: timeout after 5000ms",
                  "failed to process message: invalid payload",
                  "upstream service returned 503 (attempt {n})",
                  "unable to acquire lock on resource {n}"],
        "FATAL": ["out of memory: worker terminated", "unrecoverable state: aborting task {n}"],
    }
    lines = []
    for i in range(n):
        level = random.choice(levels)
        tmpl = random.choice(messages[level])
        msg = tmpl.format(
            u=random.choice(USERS), ms=random.randint(1, 900),
            n=random.randint(1, 500), w=random.randint(1, 8), p=random.randint(50, 99),
        )
        ts = (BASE + timedelta(seconds=i * random.randint(1, 9))).strftime("%Y-%m-%d %H:%M:%S")
        svc = random.choice(services)
        lines.append(f"{ts} {level:<7} [{svc}] {msg}")
    return lines


def access_log(n: int) -> list[str]:
    lines = []
    for i in range(n):
        ip = random.choice(IPS)
        ts = (BASE + timedelta(seconds=i * random.randint(1, 5))).strftime("%d/%b/%Y:%H:%M:%S +0000")
        method = random.choice(METHODS)
        path = random.choice(PATHS)
        roll = random.random()
        if roll < 0.83:
            status = 200
        elif roll < 0.88:
            status = 301
        elif roll < 0.93:
            status = 404
        elif roll < 0.97:
            status = 400
        else:
            status = random.choice([500, 502, 503])
        size = random.randint(120, 45000)
        lines.append(f'{ip} - - [{ts}] "{method} {path} HTTP/1.1" {status} {size}')
    return lines


def error_log(n: int) -> list[str]:
    lines = []
    for i in range(n):
        ts = (BASE + timedelta(seconds=i * 3)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        exc = random.choice([
            ("java.sql.SQLException", "Connection is not available, request timed out"),
            ("redis.exceptions.TimeoutError", "Timeout connecting to redis://cache:6379"),
            ("kafka.errors.NotLeaderError", "partition leader changed mid-request"),
            ("ValueError", "invalid literal for int() with base 10: 'NaN'"),
            ("httpx.ConnectError", "upstream api-gateway:443 refused connection"),
        ])
        lines.append(f"{ts} ERROR [worker-{i % 4 + 1}] Unhandled exception in task-{1000 + i}")
        lines.append(f"Traceback (most recent call last):")
        lines.append(f'  File "/app/jobs/task_{1000 + i}.py", line {random.randint(10, 200)}, in run')
        lines.append(f"    result = handler(payload)")
        lines.append(f"{exc[0]}: {exc[1]}")
        if i % 5 == 0:
            lines.append(f"{ts} FATAL [supervisor] worker-{i % 4 + 1} exited unexpectedly, restarting")
    return lines


def write(name: str, lines: list[str]) -> None:
    path = LOGS / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(LOGS.parent)} ({len(lines)} lines)")


if __name__ == "__main__":
    write("app.log", app_log(400))
    write("access.log", access_log(600))
    write("errors.log", error_log(40))
    write("rotated/app.log.1", app_log(120))
