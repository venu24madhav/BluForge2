import json
import unittest
import _path  # noqa: F401
import ble_survey as sv


def legacy(addr="AA:00:00:00:00:01", et=0, data=b"", rssi=-60, at=0):
    return {"event_type": et, "addr_type": at, "addr": addr, "data": data, "rssi": rssi}


def ext(addr="AA:00:00:00:00:01", et=0x13, data=b"", rssi=-60, sphy=0, sid=0xFF, at=0):
    return {"ext": True, "event_type": et, "addr_type": at, "addr": addr, "data": data, "rssi": rssi,
            "primary_phy": 1, "secondary_phy": sphy, "sid": sid, "tx_power": None}


class SurveyTests(unittest.TestCase):
    def test_scan_response_merges_into_the_same_device(self):
        s = sv.Survey()
        s.add(legacy(et=0, data=bytes.fromhex("0201060303f0ff")))
        s.add(legacy(et=4, data=bytes.fromhex("0b094c32363031323846423130")))     # SCAN_RSP carries the name
        rec = s.record(s.devs["AA:00:00:00:00:01"])
        self.assertEqual(rec["name"], "L260128FB1")
        self.assertEqual(rec["pdus"], {"ADV_IND": 1, "SCAN_RSP": 1})
        self.assertEqual([x["uuid"] for x in rec["services"]], ["0xFFF0"])
        self.assertTrue(rec["connectable"] and rec["scannable"])

    def test_counters_and_rssi_statistics(self):
        s = sv.Survey()
        for r in (-70, -60, -80):
            s.add(legacy(rssi=r))
        rec = s.record(s.devs["AA:00:00:00:00:01"])
        self.assertEqual((rec["count"], rec["rssi"], rec["rssi_min"], rec["rssi_max"]), (3, -80, -80, -60))
        self.assertEqual(rec["rssi_avg"], -70.0)

    def test_missing_rssi_is_tolerated(self):
        s = sv.Survey()
        s.add(ext(rssi=None))
        rec = s.record(s.devs["AA:00:00:00:00:01"])
        self.assertIsNone(rec["rssi"])
        self.assertIsNone(rec["rssi_avg"])

    def test_record_is_routable_and_json_safe(self):
        s = sv.Survey()
        s.add(legacy(data=bytes.fromhex("0201060303f0ff")))
        line = json.dumps(s.record(s.devs["AA:00:00:00:00:01"]), separators=(",", ":"))
        self.assertTrue(line.startswith('{"m":"dev"'))           # the server routes survey lines by this prefix
        self.assertEqual(json.loads(line)["addr"], "AA:00:00:00:00:01")

    def test_drain_returns_only_changed_devices_once(self):
        s = sv.Survey()
        s.add(legacy("AA:00:00:00:00:01"))
        s.add(legacy("AA:00:00:00:00:02"))
        self.assertEqual(len(s.drain()), 2)
        self.assertEqual(s.drain(), [])
        s.add(legacy("AA:00:00:00:00:02"))
        self.assertEqual([r["addr"] for r in s.drain()], ["AA:00:00:00:00:02"])

    def test_capture_filter(self):
        s = sv.Survey("tag")
        s.add(legacy("AA:00:00:00:00:01", data=bytes.fromhex("05097461673100")[:6]))
        s.add(legacy("AA:00:00:00:00:02"))
        self.assertEqual([r["addr"] for r in s.drain()], ["AA:00:00:00:00:01"])

    def test_memory_is_bounded(self):
        old = sv.MAX_DEVICES
        sv.MAX_DEVICES = 5
        try:
            s = sv.Survey()
            for i in range(20):
                s.add(legacy("AA:00:00:00:00:%02X" % i))
            self.assertEqual(len(s.devs), 5)
            self.assertEqual(s.evicted, 15)
            self.assertEqual(s.stats()["devices"], 5)
        finally:
            sv.MAX_DEVICES = old

    def test_extended_report_flags(self):
        s = sv.Survey()
        s.add(ext(et=0x01, sphy=3, sid=4))                       # non-legacy, connectable, secondary PHY Coded
        rec = s.record(s.devs["AA:00:00:00:00:01"])
        self.assertTrue(rec["ext"] and rec["connectable"])
        self.assertIn("Coded", rec["phys"])
        self.assertEqual(rec["sid"], 4)
        self.assertIn("ADV_EXT_IND", rec["pdus"])
        self.assertIn("Bluetooth 5 extended advertising", rec["supports"])

    def test_address_type_classes(self):
        s = sv.Survey()
        s.add(legacy("4A:00:00:00:00:01", at=1))
        self.assertEqual(s.record(s.devs["4A:00:00:00:00:01"])["addr_kind"], "random-resolvable (RPA)")

    def test_stats_invariants(self):
        s = sv.Survey()
        for i in range(30):
            s.add(legacy("AA:00:00:00:00:%02X" % (i % 4)))
        st = s.stats()
        self.assertEqual(st["devices"], 4)
        self.assertEqual(st["reports"], 30)
        self.assertEqual(sum(d["count"] for d in s.devs.values()), 30)      # nothing lost or double counted


if __name__ == "__main__":
    unittest.main()
