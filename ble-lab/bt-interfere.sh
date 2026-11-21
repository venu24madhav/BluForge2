#!/usr/bin/env bash
# bt-interfere.sh - 2.4GHz interference generator (wrapper around btlab.py interfere)
# Modes: wifi | single | sweep | hop | band | adv-flood.  Run: bt-interfere.sh --help
exec python3 "$(dirname "$(readlink -f "$0")")/btlab.py" interfere "$@"
