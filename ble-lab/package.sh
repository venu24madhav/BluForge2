#!/usr/bin/env bash
# package.sh - Build a single distributable tarball of the BluForge toolkit.
# Run this on a machine that has the toolkit (e.g. this one) to produce a
# file you copy to any fresh system and install there with install.sh.
set -euo pipefail
SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
OUT="${1:-/tmp/bluforge-$(date +%Y%m%d).tar.gz}"

FILES=(install.sh package.sh btlab.py btlab_server.py gui.html
       bt-adv.sh bt-interfere.sh bt-sniff.sh bt-coex.sh README.md)

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bluforge"
for f in "${FILES[@]}"; do
  [ -f "$SRC/$f" ] && cp "$SRC/$f" "$TMP/bluforge/"
done
tar -czf "$OUT" -C "$TMP" bluforge
echo "[+] package written: $OUT"
echo "[+] to install on a fresh machine:"
echo "      scp $OUT user@newhost:/tmp/"
echo "      ssh user@newhost 'tar xzf /tmp/$(basename "$OUT") -C /tmp && sudo /tmp/bluforge/install.sh'"
