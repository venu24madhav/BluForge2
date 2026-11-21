"""Live verification: cycle every ble_ad.py profile through a real BE200
advertiser and confirm a real BE200 scanner receives the exact bytes.

Run: sudo /usr/bin/python3 verify_adv_profiles.py
     sudo /usr/bin/python3 verify_adv_profiles.py 0 1   # pin specific hci indices
"""
import sys
import time

from hci_raw import HciSocket, detect_hci_indices
import ble_ad

if len(sys.argv) > 1:
    _devs = [int(x) for x in sys.argv[1:3]]
else:
    _devs = detect_hci_indices()[:2]
if len(_devs) < 2:
    print("[x] need 2 distinct Bluetooth controllers, found %d (%s)" % (len(_devs), detect_hci_indices()))
    sys.exit(1)
ADV_HCI, SCAN_HCI = _devs


def main():
    adv = HciSocket(ADV_HCI)
    scan = HciSocket(SCAN_HCI)
    adv.open()
    scan.open()
    scan.scan_configure(interval_ms=100, window_ms=100, active=False)
    scan.scan_enable(True)
    results = {}
    try:
        for name in ble_ad.PROFILES:
            prof = ble_ad.build_profile(name)
            adv.legacy_adv_configure(prof["adv_data"], adv_type=0x03, interval_ms=100)
            adv.legacy_adv_enable(True)
            seen = []
            scan.read_events(1.5, on_adv_report=lambda r: seen.append(r))
            adv.legacy_adv_enable(False)
            match = sum(1 for r in seen if r["data"] == prof["adv_data"])
            results[name] = (len(seen), match, prof["adv_data"].hex())
            print("%-15s sent=%2dB  reports_seen=%-3d exact_byte_matches=%-3d  %s"
                  % (name, len(prof["adv_data"]), len(seen), match,
                     "PASS" if match > 0 else "FAIL"))
    finally:
        scan.scan_enable(False)
        adv.close()
        scan.close()
    ok = all(m > 0 for _, m, _ in results.values())
    print()
    print("ALL PROFILES:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
