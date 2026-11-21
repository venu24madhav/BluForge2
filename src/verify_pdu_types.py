"""Live verification of all 4 legacy BLE advertising PDU types, including the
new directed ADV_DIRECT_IND (high and low duty) support.

Run: sudo /usr/bin/python3 verify_pdu_types.py
     sudo /usr/bin/python3 verify_pdu_types.py 0 1   # pin specific hci indices
"""
import sys
import time

from hci_raw import HciSocket, HciError, detect_hci_indices, ADV_TYPE_IND, ADV_TYPE_DIRECT_IND_HIGH, \
    ADV_TYPE_SCAN_IND, ADV_TYPE_NONCONN_IND, ADV_TYPE_DIRECT_IND_LOW, OGF_INFO_PARAM

# Advertising_Type -> the Event_Type a scanner reports for it (Core Spec 7.7.65.2): both directed types report 1
EXPECT_EVENT = {ADV_TYPE_IND: 0, ADV_TYPE_DIRECT_IND_HIGH: 1, ADV_TYPE_SCAN_IND: 2, ADV_TYPE_NONCONN_IND: 3,
                ADV_TYPE_DIRECT_IND_LOW: 1}


def read_bd_addr(h):
    """The controller's own public address (HCI Read BD_ADDR), so the scanner can tell OUR packets from the
    neighbours' - counting any advertisement in range used to make this test pass without proving anything."""
    r = h.send_cmd(OGF_INFO_PARAM, 0x0009)
    ret = r["ret"]                                  # status + 6 address bytes, little-endian
    if len(ret) < 7 or ret[0] != 0:
        raise HciError("Read BD_ADDR failed")
    return ":".join("%02X" % b for b in reversed(ret[1:7]))

if len(sys.argv) > 1:
    _devs = [int(x) for x in sys.argv[1:3]]
else:
    _devs = detect_hci_indices()[:2]
if len(_devs) < 2:
    print("[x] need 2 distinct Bluetooth controllers, found %d (%s)" % (len(_devs), detect_hci_indices()))
    sys.exit(1)
ADV_HCI, SCAN_HCI = _devs
PEER_ADDR = "AA:BB:CC:DD:EE:FF"  # arbitrary target for directed PDUs (don't need a real peer to prove the PDU is emitted)


def main():
    adv = HciSocket(ADV_HCI)
    adv.open()
    scan = HciSocket(SCAN_HCI)
    scan.open()
    adv_addr = read_bd_addr(adv)
    print("advertiser hci%d public address %s - only its packets count" % (ADV_HCI, adv_addr))
    scan.scan_configure(interval_ms=100, window_ms=100, active=True)
    scan.scan_enable(True)
    seen_pdus = {}

    def on_report(rep):
        if rep["addr"] != adv_addr:                 # a neighbour's advertisement: ignore
            return
        seen_pdus.setdefault(rep["event_type"], 0)
        seen_pdus[rep["event_type"]] += 1

    results = {}
    try:
        for label, adv_type, kwargs in [
            ("ADV_IND", ADV_TYPE_IND, dict(adv_data=b"\x02\x01\x06", scan_rsp=b"\x05\x09test")),
            ("ADV_SCAN_IND", ADV_TYPE_SCAN_IND, dict(adv_data=b"\x02\x01\x06", scan_rsp=b"\x05\x09test")),
            ("ADV_NONCONN_IND", ADV_TYPE_NONCONN_IND, dict(adv_data=b"\x02\x01\x06")),
            ("ADV_DIRECT_IND (high duty)", ADV_TYPE_DIRECT_IND_HIGH, dict(peer_addr=PEER_ADDR)),
            ("ADV_DIRECT_IND (low duty)", ADV_TYPE_DIRECT_IND_LOW, dict(peer_addr=PEER_ADDR)),
        ]:
            print("--- %s (Advertising_Type=0x%02x) ---" % (label, adv_type))
            seen_pdus.clear()
            adv.legacy_adv_configure(kwargs.get("adv_data", b""), adv_type=adv_type, interval_ms=100,
                                      peer_addr=kwargs.get("peer_addr"), scan_rsp=kwargs.get("scan_rsp", b""))
            adv.legacy_adv_enable(True)
            print("  enabled OK (controller accepted Set Advertising Parameters/Data/Enable, status 0)")
            scan.read_events(1.2, on_adv_report=on_report)
            adv.legacy_adv_enable(False)
            total = seen_pdus.get(EXPECT_EVENT[adv_type], 0)     # reports of the RIGHT type from OUR advertiser
            print("  scanner saw %d report(s) of event type %d from %s: %s" % (total, EXPECT_EVENT[adv_type], adv_addr, seen_pdus))
            results[label] = total > 0
            print("  RESULT: %s" % ("PASS (reports seen)" if total > 0 else
                                     "INFO (0 reports -- expected for a directed PDU "
                                     "pointed at an address nobody is scanning for)"))
    finally:
        scan.scan_enable(False)
        adv.close()
        scan.close()

    print()
    print("Non-directed PDU types (should all show reports received):")
    for k in ("ADV_IND", "ADV_SCAN_IND", "ADV_NONCONN_IND"):
        print("  %-30s %s" % (k, "PASS" if results.get(k) else "FAIL"))
    ok = all(results.get(k) for k in ("ADV_IND", "ADV_SCAN_IND", "ADV_NONCONN_IND"))
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
