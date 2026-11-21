import random
import struct
import unittest
import _path  # noqa: F401
import hci_raw as h
import acl_meter as m


def ext_report(addr=(1, 2, 3, 4, 5, 6), etype=0x13, data=b"\x02\x01\x06", rssi=-60, sid=0xFF, status=0):
    et = (etype & ~0x60) | (status << 5)
    return struct.pack("<HB6sBBBbbHB6sB", et, 0, bytes(addr), 1, 0, sid, 127, rssi, 0, 0, b"\0" * 6, len(data)) + data


class HciParsers(unittest.TestCase):
    def test_legacy_report(self):
        payload = bytes([1, 0, 0]) + bytes([1, 2, 3, 4, 5, 6]) + bytes([3]) + b"\x02\x01\x06" + struct.pack("<b", -55)
        r = h._parse_legacy_adv_reports(payload)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["addr"], r[0]["rssi"], r[0]["data"]), ("06:05:04:03:02:01", -55, b"\x02\x01\x06"))

    def test_extended_report_and_unavailable_fields(self):
        r = h._parse_ext_adv_reports(bytes([1]) + ext_report())
        self.assertEqual((r[0]["addr"], r[0]["rssi"], r[0]["tx_power"]), ("06:05:04:03:02:01", -60, None))
        self.assertTrue(r[0]["ext"])

    def test_fragments_are_reassembled_per_device(self):
        ra = h._ExtReassembler()
        first = h._parse_ext_adv_reports(bytes([1]) + ext_report(data=b"\x02\x01\x06", status=1))
        self.assertEqual(ra.feed(first), [])                                  # more data to come: held back
        second = h._parse_ext_adv_reports(bytes([1]) + ext_report(data=b"\x03\x09ab", status=0))
        out = ra.feed(second)
        self.assertEqual(out[0]["data"], b"\x02\x01\x06\x03\x09ab")
        other = h._parse_ext_adv_reports(bytes([1]) + ext_report(addr=(9, 9, 9, 9, 9, 9), data=b"\x01", status=0))
        self.assertEqual(len(ra.feed(other)), 1)                              # a different device is not mixed in

    def test_reassembler_memory_is_bounded(self):
        ra = h._ExtReassembler()
        for i in range(500):
            ra.feed(h._parse_ext_adv_reports(bytes([1]) + ext_report(addr=(i % 250, i // 250, 0, 0, 0, 1), status=1)))
        self.assertLessEqual(len(ra.pending), ra.MAX_PENDING)


class ParserFuzz(unittest.TestCase):
    """The controller should never send garbage, but a parser that raises would end a whole sniff job."""

    def _rand(self, rng, n):
        return bytes(rng.getrandbits(8) for _ in range(rng.randint(0, n)))

    def test_legacy_parser_never_raises(self):
        rng = random.Random(11)
        for _ in range(20000):
            h._parse_legacy_adv_reports(self._rand(rng, 120))

    def test_extended_parser_never_raises(self):
        rng = random.Random(12)
        for _ in range(20000):
            h._parse_ext_adv_reports(self._rand(rng, 200))

    def test_extended_parser_with_valid_header_and_hostile_lengths(self):
        rng = random.Random(13)
        for _ in range(5000):
            n = rng.randint(1, 4)
            body = bytes([n]) + b"".join(ext_report(data=self._rand(rng, 40)) for _ in range(n))
            cut = rng.randint(0, len(body))
            h._ExtReassembler().feed(h._parse_ext_adv_reports(body[:cut]))


class MeterTests(unittest.TestCase):
    @staticmethod
    def acl(handle, n):
        return struct.pack("<HH", handle, n) + b"\0" * n

    def test_exact_byte_accounting_and_fifo_matching(self):
        x = m.AclMeter(0)
        for n in (100, 200, 300):
            x.feed(m.MON_ACL_TX, self.acl(0x0B, n))
        x.feed(m.MON_EVENT_PKT, bytes([0x13, 5, 1]) + struct.pack("<HH", 0x0B, 2))
        x.feed(m.MON_ACL_RX, self.acl(0x0B, 50))
        s = x.snapshot([0x0B])
        self.assertEqual((s["tx_b"], s["tx_p"], s["done_b"], s["done_p"], s["rx_b"], s["done_unmatched"]), (600, 3, 300, 2, 50, 0))

    def test_unmatched_completions_are_counted_not_hidden(self):
        x = m.AclMeter(0)
        x.feed(m.MON_EVENT_PKT, bytes([0x13, 5, 1]) + struct.pack("<HH", 1, 3))      # completions for packets sent before we started
        s = x.snapshot([1])
        self.assertEqual((s["done_p"], s["done_b"], s["done_unmatched"]), (3, 0, 3))

    def test_two_handles_in_one_completion_event(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_TX, self.acl(1, 10))
        x.feed(m.MON_ACL_TX, self.acl(2, 20))
        x.feed(m.MON_EVENT_PKT, bytes([0x13, 9, 2]) + struct.pack("<HHHH", 1, 2, 1, 1))
        self.assertEqual(x.snapshot([1, 2])["done_b"], 30)

    def test_handle_mapping_and_cleanup(self):
        x = m.AclMeter(0)
        x.feed(m.MON_EVENT_PKT, bytes([0x03, 11, 0]) + struct.pack("<H", 0x0C) + bytes([6, 5, 4, 3, 2, 1, 1, 0]))
        self.assertEqual(x.handles_for("01:02:03:04:05:06"), [12])
        x.feed(m.MON_EVENT_PKT, bytes([0x05, 4, 0]) + struct.pack("<H", 0x0C) + bytes([0x13]))
        self.assertEqual(x.handles_for("01:02:03:04:05:06"), [])

    def test_rates_over_exact_elapsed_time_and_reconnect_safety(self):
        a = {"t": 10.0, "per": {1: {k: 0 for k in ("tx_b", "tx_p", "done_b", "done_p", "rx_b", "rx_p")}}}
        zero = {k: 0 for k in ("tx_b", "tx_p", "done_b", "done_p", "rx_b", "rx_p")}
        b = {"t": 12.0, "per": {1: dict(zero, tx_b=25000, tx_p=50, done_b=25000, done_p=50),
                                 2: dict(zero, tx_b=2500, tx_p=5, done_b=2500, done_p=5)}}   # handle 2 appeared (a reconnect)
        r = m.rates_for(a, b, [1, 2])
        self.assertEqual(r["tx_kbps"], round(27500 * 8 / 2.0 / 1000, 2))
        self.assertGreaterEqual(r["tx_kbps"], 0)
        self.assertIsNone(m.rates_for(b, b))                     # zero elapsed time -> no rate, no divide by zero

    def test_meter_fuzz_never_raises(self):
        rng = random.Random(5)
        x = m.AclMeter(0)
        for _ in range(30000):
            op = rng.choice([2, 3, 4, 5, 6, 7, 8, 99])
            x.feed(op, bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 40))))
        x.snapshot_all()


if __name__ == "__main__":
    unittest.main()


class MeterIntegrity(unittest.TestCase):
    @staticmethod
    def acl(handle, n):
        return struct.pack("<HH", handle, n) + b"\0" * n

    def test_clean_stream_is_trusted(self):
        x = m.AclMeter(0)
        for _ in range(50):
            x.feed(m.MON_ACL_TX, self.acl(1, 100))
            x.feed(m.MON_EVENT_PKT, bytes([0x13, 5, 1]) + struct.pack("<HH", 1, 1))
        i = x.integrity()
        self.assertTrue(i["ok"])
        self.assertEqual((i["unmatched_completions"], i["max_backlog_pkts"], i["notes"]), (0, 0, []))

    def test_startup_in_flight_packets_are_tolerated(self):
        x = m.AclMeter(0)
        x.feed(m.MON_EVENT_PKT, bytes([0x13, 5, 1]) + struct.pack("<HH", 1, m.STARTUP_GRACE_PKTS))
        self.assertTrue(x.integrity()["ok"])

    def test_many_unmatched_completions_mean_frames_were_lost(self):
        x = m.AclMeter(0)
        x.feed(m.MON_EVENT_PKT, bytes([0x13, 5, 1]) + struct.pack("<HH", 1, m.STARTUP_GRACE_PKTS + 1))
        i = x.integrity()
        self.assertFalse(i["ok"])
        self.assertIn("matched no sent packet", " ".join(i["notes"]))

    def test_unbounded_backlog_means_completion_events_were_missed(self):
        x = m.AclMeter(0)
        for _ in range(m.MAX_BACKLOG_PKTS + 1):
            x.feed(m.MON_ACL_TX, self.acl(1, 10))
        self.assertFalse(x.integrity()["ok"])

    def test_truncated_frames_and_socket_errors_are_reported(self):
        x = m.AclMeter(0)
        x.truncated, x.error = 2, "boom"
        notes = " ".join(x.integrity()["notes"])
        self.assertIn("truncated", notes)
        self.assertIn("socket error", notes)
