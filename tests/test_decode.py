import random
import unittest
import _path  # noqa: F401
import ble_decode as d


class DecodeVectors(unittest.TestCase):
    def test_real_device_advert(self):
        ad = d.decode_adv(bytes.fromhex("0201060303f0ff10094c3236303132384642313030313539"))
        self.assertEqual(ad["name"], "L260128FB100159")
        self.assertTrue(ad["name_complete"])
        self.assertEqual([s["uuid"] for s in ad["services"]], ["0xFFF0"])
        self.assertIn("BR/EDR not supported", ad["flags"])

    def test_ibeacon(self):
        u = "00112233445566778899aabbccddeeff"
        ad = d.decode_adv(bytes.fromhex("1aff4c000215" + u + "0001000250"))
        b = ad["beacon"]
        self.assertEqual((b["type"], b["major"], b["minor"]), ("iBeacon", 1, 2))
        self.assertEqual(b["uuid"], "00112233-4455-6677-8899-aabbccddeeff")
        self.assertEqual(ad["mfg"][0]["company"], "Apple")

    def test_eddystone_url(self):
        ad = d.decode_adv(bytes.fromhex("0201060e16aafe1000" + "03" + "6578616d706c65" + "07"))
        self.assertEqual(ad["beacon"]["url"], "https://example.com")
        self.assertEqual(d.classify(ad), "Eddystone-URL")

    def test_services_16_32_128(self):
        ad = d.decode_adv(bytes([3, 0x03, 0x0F, 0x18]) + bytes([5, 0x05, 1, 0, 0, 0]) + bytes([17, 0x07]) + bytes(range(16)))
        self.assertEqual(ad["services"][0], {"uuid": "0x180F", "name": "Battery"})
        self.assertEqual(len(ad["services"]), 3)

    def test_truncated_and_zero_length_are_tolerated(self):
        self.assertEqual(d.parse_ad(b"\x05\x09ab"), [])           # length runs past the end
        self.assertEqual(d.parse_ad(b"\x00\x09abc"), [])          # zero length stops the walk
        self.assertEqual(d.parse_ad(b""), [])
        self.assertEqual(d.parse_ad(b"\x02\x01\x06\x09"), [(1, b"\x06")])   # good block then garbage tail

    def test_address_kinds(self):
        self.assertEqual(d.addr_kind("AA:BB:CC:DD:EE:FF", "public"), "public")
        self.assertEqual(d.addr_kind("F3:00:00:00:00:01", "random"), "random-static")
        self.assertEqual(d.addr_kind("4A:00:00:00:00:01", "random"), "random-resolvable (RPA)")
        self.assertEqual(d.addr_kind("0A:00:00:00:00:01", "random"), "random-non-resolvable")

    def test_supports_on_minimal_record(self):
        self.assertIsInstance(d.supports({}), list)
        s = d.supports({"connectable": True, "pdus": {"ADV_IND": 1}, "flags": ["BR/EDR not supported"], "services": []})
        self.assertIn("LE-only device", s)

    def test_characteristic_values(self):
        self.assertEqual(d.value_text(0x2A19, b"\x5a"), ("5a", "90 %"))
        self.assertEqual(d.value_text(0x2A29, b"Acme")[1], "Acme")
        hexs, txt = d.value_text(0x2B2A, bytes([1, 2, 3]))
        self.assertEqual((hexs, txt), ("010203", None))

    def test_sdp_parse(self):
        t = ('Service Name: Handsfree\nService RecHandle: 0x10002\nService Class ID List:\n  "Handsfree" (0x111e)\n'
             'Protocol Descriptor List:\n  "L2CAP" (0x0100)\n  "RFCOMM" (0x0003)\n    Channel: 1\n'
             'Profile Descriptor List:\n  "Handsfree" (0x111e)\n    Version: 0x0108\n\n'
             'Service Name: A2DP\nService RecHandle: 0x10003\nProtocol Descriptor List:\n  "L2CAP" (0x0100)\n    PSM: 0x0019\n')
        r = d.parse_sdp_records(t)
        self.assertEqual(len(r), 2)
        self.assertEqual((r[0]["rfcomm_channel"], r[0]["profiles"][0]["version"]), (1, "0x0108"))
        self.assertEqual(r[1]["l2cap_psm"], "0x0019")
        self.assertEqual(d.parse_sdp_records("garbage\n\nmore garbage"), [])


class DecodeFuzz(unittest.TestCase):
    def test_random_bytes_never_raise(self):
        rng = random.Random(20261027)
        for i in range(30000):
            data = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 80)))
            ad = d.decode_adv(data)
            self.assertIsInstance(ad, dict)
            d.classify(ad)

    def test_structured_garbage_never_raises(self):
        """Valid length bytes with hostile contents for every block type we decode."""
        rng = random.Random(7)
        types = [0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x14, 0x15, 0x16, 0x19, 0x1A, 0x1F, 0x20, 0x21, 0x24, 0xFF]
        for _ in range(20000):
            blocks = b""
            for _ in range(rng.randint(1, 4)):
                t = rng.choice(types)
                body = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 30)))
                blocks += bytes([len(body) + 1, t]) + body
            d.decode_adv(blocks)

    def test_sdp_and_char_fuzz(self):
        rng = random.Random(3)
        for _ in range(3000):
            txt = "".join(rng.choice("abc :\n()0x1\"") for _ in range(rng.randint(0, 200)))
            d.parse_sdp_records(txt)
        for _ in range(5000):
            d.value_text(rng.choice([0x2A00, 0x2A01, 0x2A04, 0x2A07, 0x2A19, 0x2A23, 0x2A4A, 0x2A50, None]),
                         bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 12))))


if __name__ == "__main__":
    unittest.main()
