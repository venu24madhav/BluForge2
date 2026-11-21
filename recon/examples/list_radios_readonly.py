#!/home/lanforge/scripts/venv-3.12/bin/python
"""Read-only LANforge query example (GET only). Lists radios and the HackRF RF generator.

Run:  /home/lanforge/scripts/venv-3.12/bin/python list_radios_readonly.py
"""
import sys
sys.path.insert(0, "/home/lanforge/scripts")          # makes `lanforge_client` importable
from lanforge_client.lanforge_api import LFSession    # also: LFJsonQuery / LFJsonCommand

session = LFSession(lfclient_url="http://localhost:8080",
                    connection_timeout_sec=5.0,
                    require_session=False,   # no session tracking -> pure GETs
                    exit_on_error=False)
query = session.get_query()                  # LFJsonQuery (GET wrappers, get_xxx())

# NOTE: query.get_radiostatus() looks for a "radio"/"radios" key that /radiostatus/all does NOT
# return (records are keyed by eid), so it returns []. Use the raw json_get() for /radiostatus.
data = query.json_get("/radiostatus/all")
for eid in sorted((k for k in data if k.startswith("1.1.wiphy")), key=lambda s: int(s.split("wiphy")[1])):
    r = data[eid]
    print("%-12s %-14s ch=%-3s sta_up=%s vap_up=%s mon_up=%s  %s" % (
        eid, r["capabilities"], r["channel"], r["stations_up"], r["vaps_up"], r["monitors_up"], r["driver"]))

# GET /rfgen/1/1 through the typed helper (this one works)
print("rfgen:", query.get_rfgen(eid_list=["1.1"], requested_col_names=["entity+id", "state", "status"]))
