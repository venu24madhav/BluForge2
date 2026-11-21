"""One-shot, real-hardware verification of the two core demo capabilities.
Uses whatever Bluetooth controllers are actually present on THIS machine
(auto-detected -- NOT hardcoded hci indices: confirmed live as a real gap on
a second unit with fewer controllers than the original 8-radio development
box, where fixed indices 4/5/6/7 didn't all exist). Prints a clear PASS/FAIL
for each. Every controller used is restored to normal BlueZ control
afterward.

Run: sudo /usr/bin/python3 verify_core.py
     sudo /usr/bin/python3 verify_core.py 0 1 2 3   # pin specific hci indices instead
"""
import sys
import time

from hci_raw import HciSocket, HciError, _parse_legacy_adv_reports, detect_hci_indices

_NEEDED = 4
if len(sys.argv) > 1:
    _devs = [int(x) for x in sys.argv[1:1 + _NEEDED]]
else:
    _devs = detect_hci_indices()[:_NEEDED]
if len(_devs) < _NEEDED:
    print("[x] need %d distinct Bluetooth controllers, found %d (%s). "
          "Pass specific hci indices as arguments if you have enough but this "
          "machine's detection differs, e.g.: verify_core.py 0 1 2 3"
          % (_NEEDED, len(_devs), detect_hci_indices()))
    sys.exit(1)

INTERFERER_HCI, RX_HCI, ADV_HCI, SCAN_HCI = _devs


def test_tx_interference():
    print("=== TEST 1: LE Transmitter Test (interference generator), hci%d -> witnessed by hci%d ==="
          % (INTERFERER_HCI, RX_HCI))
    rf_channel = 19   # 2440 MHz, a BLE data channel overlapping Wi-Fi ch6
    # Per Core Spec 7.8.30, LE Test End's Number_Of_Packets is defined as
    # "number of packets received" -- always 0x0000 when ending a
    # Transmitter Test (confirmed live: status 0 / success, 0 packets, on
    # every run). So the only spec-correct way to prove the interferer is
    # really putting energy on the air is to have a SECOND controller run an
    # LE Receiver Test on the same RF channel at the same time and read ITS
    # packet count.
    tx = HciSocket(INTERFERER_HCI, label="hci%d" % INTERFERER_HCI)
    rx = HciSocket(RX_HCI, label="hci%d" % RX_HCI)
    try:
        rx.open()
        print("  opened exclusive HCI_CHANNEL_USER on hci%d (RX witness)" % RX_HCI)
        rx.rx_test_start(rf_channel=rf_channel, phy="1M")
        print("  LE Enhanced Receiver Test listening on RF channel %d (%d MHz)"
              % (rf_channel, 2402 + 2 * rf_channel))

        tx.open()
        print("  opened exclusive HCI_CHANNEL_USER on hci%d (interferer)" % INTERFERER_HCI)
        tx.tx_test_start(rf_channel=rf_channel, payload_len=37, payload_type=0x00, phy="1M")
        print("  LE Enhanced Transmitter Test started: 37B PRBS9 payload, 1M PHY, continuous")

        time.sleep(2.0)

        tx.tx_test_end()
        tx.close()
        print("  interferer stopped, hci%d restored to BlueZ" % INTERFERER_HCI)

        n = rx.tx_test_end()  # same LE_Test_End opcode ends either TX or RX test
        print("  RX witness LE Test End -> firmware reports %s packets received" % n)
        ok = n is not None and n > 0
        print("  RESULT: %s" % ("PASS" if ok else "FAIL (RX witness heard 0 packets)"))
        return ok
    except HciError as e:
        print("  RESULT: FAIL (%s)" % e)
        return False
    finally:
        tx.close()
        rx.close()
        print("  hci%d and hci%d closed, restored to BlueZ" % (INTERFERER_HCI, RX_HCI))


def test_adv_and_scan():
    print()
    print("=== TEST 2: custom BLE advertisement, received over the air ===")
    print("    advertiser = hci%d, scanner = hci%d" % (ADV_HCI, SCAN_HCI))
    # Manufacturer Specific Data (AD type 0xFF), company ID 0xFFFF (reserved
    # for testing per the Bluetooth SIG assigned numbers document), payload
    # marks this as our own synthetic test beacon.
    company_id = b"\xff\xff"
    marker = b"BLUFORGE1"
    ad_mfg = bytes([1 + len(company_id) + len(marker), 0xFF]) + company_id + marker
    ad_flags = bytes([0x02, 0x01, 0x06])  # LE General Discoverable, BR/EDR not supported
    adv_data = ad_flags + ad_mfg

    adv = HciSocket(ADV_HCI, label="hci%d" % ADV_HCI)
    scan = HciSocket(SCAN_HCI, label="hci%d" % SCAN_HCI)
    seen = {"count": 0, "rssi": []}

    def on_report(rep):
        if marker in rep["data"]:
            seen["count"] += 1
            seen["rssi"].append(rep["rssi"])

    try:
        adv.open()
        print("  opened exclusive HCI_CHANNEL_USER on hci%d (advertiser)" % ADV_HCI)
        adv.legacy_adv_configure(adv_data, adv_type=0x03, interval_ms=100)  # ADV_NONCONN_IND
        adv.legacy_adv_enable(True)
        print("  advertising ADV_NONCONN_IND, %d bytes AD data, every ~100ms" % len(adv_data))

        scan.open()
        print("  opened exclusive HCI_CHANNEL_USER on hci%d (scanner)" % SCAN_HCI)
        scan.scan_configure(interval_ms=100, window_ms=100, active=False)
        scan.scan_enable(True)
        print("  scanning for 3s, looking for our marker %r ..." % marker)
        scan.read_events(3.0, on_adv_report=on_report)
        scan.scan_enable(False)
        adv.legacy_adv_enable(False)

        ok = seen["count"] > 0
        if ok:
            avg_rssi = sum(seen["rssi"]) / len(seen["rssi"])
            print("  RESULT: PASS - received %d copies of our own advertisement, avg RSSI %.1f dBm"
                  % (seen["count"], avg_rssi))
        else:
            print("  RESULT: FAIL - scanner never saw our advertisement")
        return ok
    except HciError as e:
        print("  RESULT: FAIL (%s)" % e)
        return False
    finally:
        adv.close()
        scan.close()
        print("  hci%d and hci%d closed, restored to BlueZ" % (ADV_HCI, SCAN_HCI))


if __name__ == "__main__":
    r1 = test_tx_interference()
    r2 = test_adv_and_scan()
    print()
    print("=== SUMMARY: interference=%s, adv+scan=%s ===" % (
        "PASS" if r1 else "FAIL", "PASS" if r2 else "FAIL"))
    sys.exit(0 if (r1 and r2) else 1)
