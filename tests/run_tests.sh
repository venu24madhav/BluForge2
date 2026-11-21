#!/usr/bin/env bash
# Hardware-free regression + fuzz tests. Safe to run anywhere (no radios, no root, no network).
cd "$(dirname "$(readlink -f "$0")")" && exec python3 -W ignore::ResourceWarning -m unittest discover -s . -p 'test_*.py' "$@"
