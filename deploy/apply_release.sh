#!/usr/bin/env bash
# Apply a `git archive` tarball to the app directory.
#
# Unpacking over the old tree never deletes files a release dropped, so stale
# code and retired systemd units come back. This script remembers what each
# release shipped (.deploy-manifest) and removes, before unpacking, every file
# the previous release had and this one does not. Only paths from the previous
# manifest can be deleted; runtime state (data/, .venv/, .env, outputs/) is not
# in any manifest and is never touched.
#
# Usage: apply_release.sh <archive.tar.gz> [app_dir] [initial_manifest]
set -euo pipefail

# Absolute paths in the shell's own form (also keeps GNU tar from reading
# "C:/..." as a remote host when this runs under Git Bash).
abspath() { printf '%s/%s' "$(cd "$(dirname "$1")" && pwd)" "$(basename "$1")"; }
archive="$(abspath "$1")"
app_dir="${2:-/home/adrian/scorerecord}"
initial="${3:-}"
[ -n "$initial" ] && initial="$(abspath "$initial")"
manifest="$app_dir/.deploy-manifest"
export LC_ALL=C

cd "$app_dir"
next="$(mktemp)"
tar -tzf "$archive" | grep -v '/$' | sort -u > "$next"

previous="$manifest"
if [ ! -f "$manifest" ] && [ -n "$initial" ] && [ -f "$initial" ]; then
  previous="$initial"
fi

if [ -f "$previous" ]; then
  comm -23 <(tr -d '\r' < "$previous" | sort -u) "$next" | while IFS= read -r stale; do
    case "$stale" in
      ""|/*|*..*|data/*|.venv/*|.env|outputs/*) continue ;;
    esac
    if [ -e "$stale" ] || [ -L "$stale" ]; then
      rm -f -- "$stale"
      echo "removed $stale"
    fi
  done
fi

tar -xzf "$archive"
mv "$next" "$manifest"
for dir in src tests scripts deploy docs; do
  [ -d "$dir" ] && find "$dir" -type d -empty -delete
done
echo "release applied: $(wc -l < "$manifest") files"
