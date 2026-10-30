#!/usr/bin/env bash
# Search a log file with an extended regular expression, with optional context.
set -eo pipefail

LOG=""
PATTERN=""
IGNORE_CASE=0
CONTEXT=0
MAX_COUNT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pattern)     PATTERN="${2:-}"; shift 2 ;;
    --ignore-case) IGNORE_CASE=1; shift ;;
    --context)     CONTEXT="${2:-0}"; shift 2 ;;
    --max-count)   MAX_COUNT="${2:-0}"; shift 2 ;;
    -*) echo "error: unknown option: $1" >&2; exit 2 ;;
    *)  LOG="$1"; shift ;;
  esac
done

if [[ -z "$PATTERN" ]]; then
  echo "error: --pattern is required" >&2
  exit 2
fi
if [[ -z "$LOG" ]]; then
  echo "error: no log file given" >&2
  exit 2
fi
if [[ ! -f "$LOG" ]]; then
  echo "error: log file not found: $LOG" >&2
  exit 2
fi

opts=()
(( IGNORE_CASE )) && opts+=(-i)
(( CONTEXT > 0 )) && opts+=(-C "$CONTEXT")
(( MAX_COUNT > 0 )) && opts+=(-m "$MAX_COUNT")

matches="$(grep -E ${opts[@]+"${opts[@]}"} -- "$PATTERN" "$LOG")" || rc=$?
rc="${rc:-0}"
if [[ "$rc" -eq 2 ]]; then
  echo "error: grep failed (invalid regex?): $PATTERN" >&2
  exit 2
fi

if [[ -z "$matches" ]]; then
  echo "No lines matching pattern: $PATTERN"
  exit 0
fi

printf '%s\n' "$matches"
count="$(printf '%s' "$matches" | grep -c '' || true)"
echo
echo "-- ${count} matching line(s) for pattern '${PATTERN}' --"
exit 0
