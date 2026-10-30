#!/usr/bin/env bash
# LogSmith uploader — copy the app to a remote machine via scp.
#
#   tools/upload.sh user@host                 unpacks into ~/logsmith on the host
#   tools/upload.sh user@host:apps/logsmith   custom target dir (absolute paths ok)
#   tools/upload.sh -p 2222 user@host         non-standard ssh port
#   tools/upload.sh --start user@host         also runs ./run.sh start there
#
# What is uploaded: exactly the files git tracks. The local runtime state
# (.venv, node_modules, frontend/dist, data/, certs/, .run/, .env —
# everything .gitignore keeps out) stays behind; the remote builds its own
# on its first ./run.sh start. Re-uploads refresh the tracked files in place
# and leave remote state (job history, generated token, TLS certs) untouched.
#
#   --tar-only   just build the tarball and print its path (no upload)
#   --dirty      include uncommitted tracked changes; a dirty tree is refused
#                by default — otherwise you would upload something you
#                cannot rebuild from the repo
#   --start      after unpacking, start the app on the remote (background
#                mode: the banner prints, ssh returns, server keeps running)
set -euo pipefail
cd "$(dirname "$0")/.."

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

die() { echo "Error: $1" >&2; exit 1; }

PORT=22
TAR_ONLY=0
DIRTY=0
START=0
TARGET=""

while [ $# -gt 0 ]; do
  case "$1" in
    -p)
      [ $# -ge 2 ] || die "-p needs a port number"
      PORT="$2"; shift 2 ;;
    --tar-only) TAR_ONLY=1; shift ;;
    --dirty)    DIRTY=1;    shift ;;
    --start)    START=1;    shift ;;
    -h|--help)  usage; exit 0 ;;
    -*)         die "unknown flag '$1' (see --help)" ;;
    *)
      [ -z "$TARGET" ] || die "unexpected extra argument '$1'"
      TARGET="$1"; shift ;;
  esac
done
[ -n "$TARGET" ] || [ "$TAR_ONLY" = "1" ] || { usage >&2; exit 1; }

for tool in git scp ssh; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed or not on PATH"
done

# Package exactly the committed tree. A dirty working tree is refused: the
# tarball would silently lack the uncommitted edits. --dirty snapshots them
# without touching anything (git stash create makes a commit object but no
# stash entry; new untracked files are still not included — commit those).
TREE="HEAD"
if [ -n "$(git status --porcelain)" ]; then
  if [ "$DIRTY" = "0" ]; then
    echo "Error: the working tree has uncommitted changes — they would NOT" >&2
    echo "       be uploaded. Commit first, or pass --dirty to snapshot them." >&2
    exit 1
  fi
  TREE="$(git stash create)"
  [ -n "$TREE" ] || TREE="HEAD"
fi
SHA="$(git rev-parse --short "$TREE")"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
TARBALL="$WORKDIR/logsmith-$SHA.tar.gz"
git archive --format=tar.gz -o "$TARBALL" "$TREE"

FILES="$(git ls-tree -r --name-only "$TREE" | wc -l | tr -d ' ')"
SIZE="$(du -h "$TARBALL" | cut -f1)"
echo "Packed   ●  logsmith @ $SHA ($FILES files, $SIZE) — git-tracked files only"

if [ "$TAR_ONLY" = "1" ]; then
  trap - EXIT
  echo "Tarball  ●  $TARBALL"
  exit 0
fi

# Target spec: user@host[:dir] — dir defaults to ~/logsmith on the remote.
[ -n "$TARGET" ] || { usage >&2; exit 1; }
[ "$TARGET" = "${TARGET#*:}" ] && DIR="" || DIR="${TARGET#*:}"  # host[:dir]
REMOTE="${TARGET%%:*}"
[ -n "$REMOTE" ] || die "no host in '$TARGET' (want user@host[:dir])"
[ -n "$DIR" ] || DIR="logsmith"

# Single-quote the remote paths (a dir like "my apps/logsmith" must survive).
qdir() { printf '%s' "$1" | sed "s/'/'\\\\''/g"; }
QDIR="$(qdir "$DIR")"
QTMP="$(qdir "/tmp/logsmith-upload-$$.tar.gz")"

echo "Upload   ●  $REMOTE:$DIR (port $PORT)"
scp -P "$PORT" "$TARBALL" "$REMOTE:$QTMP"

echo "Extract  ●  refreshing tracked files in \$HOME-relative '$DIR'"
ssh -p "$PORT" "$REMOTE" \
  "mkdir -p '$QDIR' && tar -xzf '$QTMP' -C '$QDIR' && rm -f '$QTMP'"

if [ "$START" = "1" ]; then
  echo "Start    ●  ./run.sh start on $REMOTE"
  ssh -p "$PORT" "$REMOTE" "cd '$QDIR' && ./run.sh start"
else
  echo "Done     ●  $REMOTE:$DIR"
  echo "           start it there:  ssh -t $REMOTE \"cd '$DIR' && ./run.sh\""
fi
