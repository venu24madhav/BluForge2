import struct
import unittest
import _path  # noqa: F401
import bluforge_cli as cli
import hci_raw


class FakeAdvHci(hci_raw.HciSocket):
    """HciSocket with the controller replaced by a recorder; selected TX power is capped like the BE200."""
    def __init__(self, cap=8):
        super().__init__(1)
        self.sent, self.cap = [], cap

    def send_cmd(self, ogf, ocf, params=b"", expect_complete=True, timeout=5.0):
        self.sent.append((ocf, params))
        ret = b"\x00"
        if ocf == hci_raw.OCF["LE_SET_EXT_ADV_PARAMETERS"]:
            ret += struct.pack("<b", min(struct.unpack("<b", params[19:20])[0], self.cap))
        return {"status": 0, "ret": ret}


class ParsePower(unittest.TestCase):
    def test_adv_values(self):
        self.assertIsNone(cli.parse_power(None))
        self.assertIsNone(cli.parse_power(""))
        self.assertEqual(cli.parse_power("max"), 20)
        self.assertEqual(cli.parse_power("min"), -127)
        self.assertEqual(cli.parse_power("-20"), -20)

    def test_dtm_keeps_the_spec_sentinels(self):
        self.assertEqual(cli.parse_power("max", dtm=True), 0x7F)
        self.assertEqual(cli.parse_power("min", dtm=True), 0x7E)
        self.assertEqual(cli.parse_power("8", dtm=True), 8)

    def test_out_of_range_is_refused(self):
        for bad in ("21", "-128", "loud"):
            with self.assertRaises(SystemExit):
                cli.parse_power(bad)


class ExtAdvPower(unittest.TestCase):
    def configure(self, adv_type, power, **kw):
        h = FakeAdvHci()
        sel = h.legacy_adv_configure(b"\x02\x01\x06", adv_type=adv_type, interval_ms=30, tx_power_dbm=power, **kw)
        return h, sel

    def test_no_power_keeps_the_plain_legacy_commands(self):
        h = FakeAdvHci()
        self.assertIsNone(h.legacy_adv_configure(b"\x02\x01\x06", interval_ms=30))
        self.assertEqual([o for o, _ in h.sent][0], hci_raw.OCF["LE_SET_ADV_PARAMETERS"])
        h.legacy_adv_enable(True)
        self.assertEqual(h.sent[-1][0], hci_raw.OCF["LE_SET_ADV_ENABLE"])

    def test_power_uses_ext_commands_with_the_legacy_bit_and_returns_what_the_controller_applied(self):
        h, sel = self.configure(hci_raw.ADV_TYPE_NONCONN_IND, 20)
        self.assertEqual(sel, 8)                                    # asked +20, controller clamps
        ocf, p = h.sent[0]
        self.assertEqual(ocf, hci_raw.OCF["LE_SET_EXT_ADV_PARAMETERS"])
        self.assertEqual(struct.unpack("<H", p[1:3])[0], 0x0010)    # legacy PDU, non-connectable
        self.assertEqual(len(p), 25)
        self.assertEqual(struct.unpack("<b", p[19:20])[0], 20)

    def test_event_properties_per_pdu_type(self):
        want = {hci_raw.ADV_TYPE_IND: 0x13, hci_raw.ADV_TYPE_SCAN_IND: 0x12, hci_raw.ADV_TYPE_NONCONN_IND: 0x10}
        for t, props in want.items():
            h, _ = self.configure(t, 0)
            self.assertEqual(struct.unpack("<H", h.sent[0][1][1:3])[0], props)
        for t, props in ((hci_raw.ADV_TYPE_DIRECT_IND_LOW, 0x15), (hci_raw.ADV_TYPE_DIRECT_IND_HIGH, 0x1D)):
            h = FakeAdvHci()
            h.legacy_adv_configure(b"", adv_type=t, interval_ms=30, tx_power_dbm=0, peer_addr="11:22:33:44:55:66")
            self.assertEqual(struct.unpack("<H", h.sent[0][1][1:3])[0], props)
            self.assertNotIn(hci_raw.OCF["LE_SET_EXT_ADV_DATA"], [o for o, _ in h.sent])   # directed PDU has no AdvData

    def test_high_duty_directed_gets_the_mandatory_128x10ms_duration(self):
        h = FakeAdvHci()
        h.legacy_adv_configure(b"", adv_type=hci_raw.ADV_TYPE_DIRECT_IND_HIGH, interval_ms=30, tx_power_dbm=0,
                               peer_addr="11:22:33:44:55:66")
        h.legacy_adv_enable(True)
        ocf, p = h.sent[-1]
        self.assertEqual(ocf, hci_raw.OCF["LE_SET_EXT_ADV_ENABLE"])
        self.assertEqual(struct.unpack("<H", p[3:5])[0], 128)

    def test_stop_disables_and_removes_the_set_and_returns_to_the_legacy_path(self):
        h, _ = self.configure(hci_raw.ADV_TYPE_NONCONN_IND, 0)
        h.legacy_adv_enable(True)
        h.legacy_adv_enable(False)
        self.assertEqual([o for o, _ in h.sent][-2:], [hci_raw.OCF["LE_SET_EXT_ADV_ENABLE"], hci_raw.OCF["LE_REMOVE_ADV_SET"]])
        self.assertFalse(h._adv_ext)

    def test_out_of_range_power_is_refused_before_anything_is_sent(self):
        h = FakeAdvHci()
        with self.assertRaises(ValueError):
            h.legacy_adv_configure(b"", interval_ms=30, tx_power_dbm=21)
        self.assertEqual(h.sent, [])


if __name__ == "__main__":
    unittest.main()
