"""Preview endpoint: head and tail modes, plain and compressed files."""
from __future__ import annotations

import gzip

from conftest import LOGS_TMP

# A dedicated file: other suites read the shared SAMPLE_LOG fixture, so
# these tests must not overwrite it.
PREVIEW_LOG = LOGS_TMP / "preview.log"

CONTENT = (
    "2026-10-29 10:00:01 INFO  line one\n"
    "2026-10-29 10:00:02 INFO  line two\n"
    "2026-10-29 10:00:03 INFO  line three\n"
    "2026-10-29 10:00:04 INFO  line four\n"
    "2026-10-29 10:00:05 INFO  line five\n"
)
ALL_LINES = CONTENT.splitlines()


def _url(path: str, **params) -> str:
    query = f"path={path}"
    for key, value in params.items():
        query += f"&{key}={value}"
    return f"/api/logs/preview?{query}"


def test_head_mode(client):
    PREVIEW_LOG.write_text(CONTENT, encoding="utf-8")
    res = client.get(_url(str(PREVIEW_LOG), lines=2)).json()
    assert res["lines"] == ALL_LINES[:2]
    assert res["truncated"] is True
    assert res["compressed"] is False

    res = client.get(_url(str(PREVIEW_LOG), lines=10)).json()
    assert res["lines"] == ALL_LINES
    assert res["truncated"] is False


def test_tail_mode(client):
    PREVIEW_LOG.write_text(CONTENT, encoding="utf-8")
    res = client.get(_url(str(PREVIEW_LOG), lines=2, tail=1)).json()
    assert res["lines"] == ALL_LINES[-2:]
    assert res["truncated"] is True

    res = client.get(_url(str(PREVIEW_LOG), lines=10, tail=1)).json()
    assert res["lines"] == ALL_LINES
    assert res["truncated"] is False


def test_tail_without_trailing_newline(client):
    # 5 lines but only 4 newlines — the boundary the newline counting can't see
    PREVIEW_LOG.write_text(CONTENT.rstrip("\n"), encoding="utf-8")
    res = client.get(_url(str(PREVIEW_LOG), lines=4, tail=1)).json()
    assert res["lines"] == ALL_LINES[1:]
    assert res["truncated"] is True


def test_tail_compressed(client):
    gz = LOGS_TMP / "tailcase.log.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        fh.write("".join(f"line {i}\n" for i in range(100)))
    res = client.get(_url(str(gz), lines=3, tail=1)).json()
    assert res["compressed"] is True
    assert res["lines"] == ["line 97", "line 98", "line 99"]
    assert res["truncated"] is True

    res = client.get(_url(str(gz), lines=200, tail=1)).json()
    assert len(res["lines"]) == 100
    assert res["truncated"] is False


def test_tail_large_file_is_cheap(client):
    """Backwards seek means a big file costs a couple of blocks, not a scan."""
    big = LOGS_TMP / "big.log"
    with big.open("w", encoding="utf-8") as fh:
        for i in range(50_000):
            fh.write(f"2026-10-29 12:00:00 INFO entry {i:06d} padded padding padding\n")
    res = client.get(_url(str(big), lines=2, tail=1)).json()
    assert res["lines"] == [
        "2026-10-29 12:00:00 INFO entry 049998 padded padding padding",
        "2026-10-29 12:00:00 INFO entry 049999 padded padding padding",
    ]
    assert res["truncated"] is True


def test_preview_errors(client):
    assert client.get(_url(str(LOGS_TMP / "nope.log"))).status_code == 404
    assert client.get(_url("/etc/passwd")).status_code == 400
    assert client.get(_url(str(PREVIEW_LOG), lines=0)).status_code == 422
