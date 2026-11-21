#!/usr/bin/env bash
# bt-adv.sh - BLE advertisement generator (wrapper around btlab.py adv)
# Types: ind | direct | direct-l | scan | nonconn.  Run:  bt-adv.sh --help
exec python3 "$(dirname "$(readlink -f "$0")")/btlab.py" adv "$@"
