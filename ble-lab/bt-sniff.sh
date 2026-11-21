#!/usr/bin/env bash
# bt-sniff.sh - over-the-air BLE advertising sniffer (wrapper around btlab.py sniff)
# Passive by default; --active gets device names/scan responses.  bt-sniff.sh --help
exec python3 "$(dirname "$(readlink -f "$0")")/btlab.py" sniff "$@"
