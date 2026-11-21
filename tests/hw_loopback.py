#!/usr/bin/env python3
"""Two-radio loopback test on real hardware (NOT part of the automatic suite: it transmits and takes both radios).

Radio A advertises a known iBeacon; radio B surveys; the decoded result must match what was sent byte for byte.
Also checks the radios come back UP afterwards. Usage:  sudo python3 hw_loopback.py [ADV_HCI SCAN_HCI]   (default 0 1)
Exit 0 = pass."""
import json
import os
import re
import subprocess
import sys
import time

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
CLI = os.path.join(SRC, "bluforge_cli.py")
adv_hci, scan_hci = (int(x) for x in sys.argv[1:3]) if len(sys.argv) >= 3 else (0, 1)
UUID, MAJOR, MINOR = "00112233-4455-6677-8899-aabbccddeeff", 7, 9


def hci_state(n):
    t = subprocess.run(["hciconfig", "hci%d" % n], capture_output=True, text=True).stdout
    addr = re.search(r"BD Address: ([0-9A-F:]{17})", t)
    return ("UP RUNNING" in t), (addr.group(1) if addr else None)


def main():
    _, adv_addr = hci_state(adv_hci)
    cfg = "uuid=%s,major=%d,minor=%d" % (UUID, MAJOR, MINOR)
    adv = subprocess.Popen([sys.executable, CLI, "--dev", str(adv_hci), "adv", "ind", "-i", "30", "--profile", "ibeacon",
                            "--profile-cfg", cfg, "-t", "14"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    r = subprocess.run([sys.executable, CLI, "--dev", str(scan_hci), "sniff", "--json", "--survey", "--active", "-t", "8"],
                       capture_output=True, text=True, timeout=60)
    adv.wait(timeout=30)
    devs, bad = {}, 0
    for line in r.stdout.splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if o.get("m") == "dev":
            devs[o["addr"]] = o
    tx = devs.get(adv_addr)
    checks = {
        "survey exited cleanly": r.returncode == 0,
        "no malformed output lines": bad == 0,
        "our advertiser was heard": tx is not None,
    }
    if tx:
        b = tx.get("beacon") or {}
        checks.update({
            "beacon decoded as iBeacon": b.get("type") == "iBeacon",
            "UUID matches": b.get("uuid") == UUID,
            "major/minor match": (b.get("major"), b.get("minor")) == (MAJOR, MINOR),
            "both PDU types seen (advert + scan response)": {"ADV_IND", "SCAN_RSP"} <= set(tx["pdus"]),
            "reports counted consistently": tx["count"] == sum(tx["pdus"].values()),
        })
    checks["advertising radio restored"] = hci_state(adv_hci)[0]
    checks["scanning radio restored"] = hci_state(scan_hci)[0]
    for k, v in checks.items():
        print(("PASS " if v else "FAIL ") + k)
    return all(checks.values())


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
