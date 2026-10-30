#!/usr/bin/env node
// Endpoint statistics from a common-format access log: hits, transferred
// bytes and error rate per URL path, ranked by traffic.
//
// Demonstrates the Node.js script style: the log path is argv[2], CLI flags
// arrive after it, artifacts can be written to the current directory.
"use strict";
const fs = require("fs");

const logPath = process.argv[2];
const args = process.argv.slice(3);

function intFlag(name, fallback) {
  const i = args.indexOf(name);
  if (i === -1) return fallback;
  const v = Number(args[i + 1]);
  return Number.isFinite(v) ? v : fallback;
}
function boolFlag(name) {
  return args.includes(name);
}
const top = Math.max(1, intFlag("--top", 10));
const errorsOnly = boolFlag("--errors-only");

const LINE = /^(\S+) \S+ \S+ \[[^\]]+\] "(\S+) (\S+)(?: [^"]*)?" (\d{3}) (\d+|-)\s*$/;

function human(bytes) {
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MiB";
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + " KiB";
  return bytes + " B";
}

if (!logPath) {
  console.error("usage: node response_stats.js <access.log> [--top N] [--errors-only]");
  process.exit(2);
}

let text;
try {
  text = fs.readFileSync(logPath, "utf8");
} catch (e) {
  console.error(`cannot read ${logPath}: ${e.message}`);
  process.exit(2);
}

const stats = new Map(); // url -> {hits, bytes, errors}
let total = 0;
let errors = 0;

for (const line of text.split("\n")) {
  const m = LINE.exec(line);
  if (!m) continue;
  const url = m[3];
  const status = parseInt(m[4], 10);
  const bytes = m[5] === "-" ? 0 : parseInt(m[5], 10);
  total++;
  const isErr = status >= 400;
  if (isErr) errors++;
  const s = stats.get(url) || { hits: 0, bytes: 0, errors: 0 };
  s.hits++;
  s.bytes += bytes;
  if (isErr) s.errors++;
  stats.set(url, s);
}

if (total === 0) {
  console.log("no access-log lines found (expected common log format)");
  process.exit(0);
}

let rows = [...stats.entries()].map(([url, s]) => ({ url, ...s }));
if (errorsOnly) rows = rows.filter((r) => r.errors > 0);
rows.sort((a, b) => b.hits - a.hits || b.errors - a.errors);
const shown = rows.slice(0, top);

const errPct = (n) => (100 * n / total).toFixed(1).padStart(5) + "%";
console.log(`Endpoint stats — ${total} requests, ${errPct(errors)} errors, ${stats.size} endpoints`);
if (errorsOnly) console.log("(endpoints with 4xx/5xx responses only)");
console.log("");
console.log("  hits   err%      bytes  endpoint");
for (const r of shown) {
  const errRate = r.hits ? (100 * r.errors / r.hits).toFixed(1).padStart(5) + "%" : "  n/a";
  console.log(
    String(r.hits).padStart(5) + "  " + errRate + "  " +
    human(r.bytes).padStart(10) + "  " + r.url
  );
}
if (rows.length > shown.length) {
  console.log(`… showing top ${shown.length} of ${rows.length} endpoints`);
}
