"""Packet capture of the BLE sniffer: the pcap file format, the HCI tap, the argv, and the download API.
No radios, no root, no network."""
import http.client
import os
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

import _path  # noqa: F401
import hci_raw
import pcap_writer
import server


def read_pcap(path):
    """Parse a pcap the way a reader would: (link type, [(sec, usec, direction, h4 packet)])."""
    d = open(path, "rb").read()
    magic, vmaj, vmin, _tz, _sig, snap, link = struct.unpack("<IHHiIII", d[:24])
    assert magic == 0xA1B2C3D4 and (vmaj, vmin) == (2, 4), "not a little-endian microsecond pcap"
    off, out = 24, []
    while off + 16 <= len(d):
        sec, usec, incl, orig = struct.unpack("<IIII", d[off:off + 16])
        assert incl == orig, "record length fields disagree"
        body = d[off + 16:off + 16 + incl]
        assert len(body) == incl, "truncated record"
        out.append((sec, usec, struct.unpack(">I", body[:4])[0], body[4:]))
        off += 16 + incl
    assert off == len(d), "bytes left over after the last record"
    return link, out


ADV_REPORT_EVT = bytes.fromhex("043e1302010000a1b2c3d4e5f60702010603030ff0c4")   # LE Advertising Report, one ADV_IND


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "x.pcap")

    def test_header_and_records_round_trip(self):
        with pcap_writer.PcapWriter(self.path) as w:
            self.assertTrue(w.write(0, bytes.fromhex("01030c00"), ts=1700000000.25))
            self.assertTrue(w.write(1, ADV_REPORT_EVT, ts=1700000000.5))
            self.assertEqual(w.packets, 2)
        link, recs = read_pcap(self.path)
        self.assertEqual(link, 201)                                  # Bluetooth HCI H4 with pseudo-header
        self.assertEqual([(r[0], r[1], r[2]) for r in recs], [(1700000000, 250000, 0), (1700000000, 500000, 1)])
        self.assertEqual(recs[1][3], ADV_REPORT_EVT)                 # the HCI event is stored byte for byte
        self.assertEqual(os.path.getsize(self.path), w.bytes)

    def test_a_file_that_is_still_being_written_is_always_whole_records(self):
        w = pcap_writer.PcapWriter(self.path)
        for i in range(50):
            w.write(1, ADV_REPORT_EVT)
            link, recs = read_pcap(self.path)                        # read_pcap asserts: no partial or leftover bytes
            self.assertEqual(len(recs), i + 1)
        w.close()

    def test_never_overwrites_an_existing_capture(self):
        pcap_writer.PcapWriter(self.path).close()
        with self.assertRaises(FileExistsError):
            pcap_writer.PcapWriter(self.path)

    def test_size_limit_stops_recording_but_keeps_the_file_valid(self):
        rec = 16 + 4 + len(ADV_REPORT_EVT)
        w = pcap_writer.PcapWriter(self.path, max_bytes=24 + 3 * rec + 5)        # room for three records and a bit
        results = [w.write(1, ADV_REPORT_EVT) for _ in range(6)]
        w.close()
        self.assertEqual(results, [True, True, True, False, False, False])
        self.assertTrue(w.full)
        self.assertEqual(len(read_pcap(self.path)[1]), 3)

    def test_empty_packet_and_closed_writer_are_ignored(self):
        w = pcap_writer.PcapWriter(self.path)
        self.assertFalse(w.write(1, b""))
        w.close()
        w.close()                                                    # closing twice is harmless
        self.assertFalse(w.write(1, ADV_REPORT_EVT))
        self.assertEqual(read_pcap(self.path)[1], [])

    def test_concurrent_writers_do_not_interleave_records(self):
        w = pcap_writer.PcapWriter(self.path)

        def spam():
            for _ in range(200):
                w.write(1, ADV_REPORT_EVT)
        ts = [threading.Thread(target=spam) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        w.close()
        self.assertEqual(len(read_pcap(self.path)[1]), 800)


class FakeSock:
    """Stands in for the HCI user-channel socket: answers each command with Command Complete, then feeds queued events."""
    def __init__(self, events=()):
        self.sent, self.queue = [], list(events)

    def settimeout(self, t):
        pass

    def send(self, pkt):
        self.sent.append(bytes(pkt))
        op = struct.unpack("<H", pkt[1:3])[0]
        self.queue.insert(0, struct.pack("<BBBBHB", 0x04, 0x0E, 4, 1, op, 0))     # Command Complete, status 0

    def recv(self, n):
        if not self.queue:
            raise __import__("socket").timeout()
        return self.queue.pop(0)


class TapTests(unittest.TestCase):
    def test_commands_and_events_reach_the_tap_in_order(self):
        h = hci_raw.HciSocket(0)
        h.sock = FakeSock(events=[ADV_REPORT_EVT])
        seen = []
        h.tap = lambda d, p: seen.append((d, p))
        h.send_cmd(hci_raw.OGF_LE, hci_raw.OCF["LE_SET_SCAN_ENABLE"], b"\x01\x00")
        self.assertEqual([d for d, _ in seen], [0, 1])
        self.assertEqual(seen[0][1][0], 0x01)                        # a command packet
        self.assertEqual(seen[1][1][1], 0x0E)                        # its Command Complete

    def test_every_event_read_is_tapped_and_the_report_is_still_parsed(self):
        h = hci_raw.HciSocket(0)
        h.sock = FakeSock(events=[ADV_REPORT_EVT, ADV_REPORT_EVT])
        seen, reports = [], []
        h.tap = lambda d, p: seen.append((d, p))
        h.read_events(0.2, on_adv_report=reports.append)
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(d == 1 and p == ADV_REPORT_EVT for d, p in seen))
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0]["rssi"], -60)

    def test_a_broken_tap_never_disturbs_the_radio(self):
        h = hci_raw.HciSocket(0)
        h.sock = FakeSock(events=[ADV_REPORT_EVT])

        def boom(d, p):
            raise RuntimeError("disk full")
        h.tap = boom
        reports = []
        h.read_events(0.2, on_adv_report=reports.append)
        self.assertEqual(len(reports), 1)
        self.assertEqual(h.tap_errors, 1)

    def test_no_tap_is_the_default_and_costs_nothing(self):
        h = hci_raw.HciSocket(0)
        h.sock = FakeSock(events=[ADV_REPORT_EVT])
        reports = []
        h.read_events(0.2, on_adv_report=reports.append)
        self.assertEqual(len(reports), 1)
        self.assertEqual(h.tap_errors, 0)


class ArgvTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.patch = mock.patch.object(server, "CAPTURE_DIR", os.path.join(self.dir, "captures"))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_recording_is_opt_in_and_names_a_file_in_the_capture_folder(self):
        self.assertNotIn("--pcap", server.build_argv("sniff", 3, {"active": True}))
        argv = server.build_argv("sniff", 3, {"active": True, "pcap": True})
        path = argv[argv.index("--pcap") + 1]
        self.assertEqual(os.path.dirname(path), server.CAPTURE_DIR)
        self.assertRegex(os.path.basename(path), r"^\d{8}-\d{6}_blesniff_hci3\.pcap$")
        self.assertTrue(os.path.isdir(server.CAPTURE_DIR))

    def test_two_jobs_in_the_same_second_get_different_files(self):
        with mock.patch.object(server.time, "strftime", return_value="20261121-120000"):
            a = server.build_argv("sniff", 3, {"pcap": True})
            open(a[a.index("--pcap") + 1], "w").close()              # the first job has created its file
            b = server.build_argv("sniff", 3, {"pcap": True})
        self.assertNotEqual(a[a.index("--pcap") + 1], b[b.index("--pcap") + 1])

    def test_size_limit_is_checked(self):
        argv = server.build_argv("sniff", 3, {"pcap": True, "pcap_max_mb": "64"})
        self.assertEqual(argv[argv.index("--pcap-max-mb") + 1], "64")
        with self.assertRaises(ValueError):
            server.build_argv("sniff", 3, {"pcap": True, "pcap_max_mb": "-1"})
        with self.assertRaises(ValueError):
            server.build_argv("sniff", 3, {"pcap": True, "pcap_max_mb": "lots"})

    def test_the_cli_knows_the_options_the_server_passes(self):
        import subprocess
        import sys
        out = subprocess.run([sys.executable, os.path.join(server.HERE, "bluforge_cli.py"), "sniff", "--help"],
                             capture_output=True, text=True, timeout=30).stdout
        self.assertIn("--pcap ", out)
        self.assertIn("--pcap-max-mb", out)


class DownloadApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.patch = mock.patch.object(server, "CAPTURE_DIR", cls.dir)
        cls.patch.start()
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.patch.stop()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), body

    def make(self, name, n=5):
        w = pcap_writer.PcapWriter(os.path.join(self.dir, name))
        for _ in range(n):
            w.write(1, ADV_REPORT_EVT)
        w.close()
        return os.path.join(self.dir, name)

    def test_list_and_download_a_capture(self):
        p = self.make("20261121-101500_blesniff_hci2.pcap")
        st, _, body = self.get("/api/sniff/captures")
        self.assertEqual(st, 200)
        import json
        caps = json.loads(body)["captures"]
        c = next(x for x in caps if x["file"] == "20261121-101500_blesniff_hci2.pcap")
        self.assertEqual((c["hci"], c["size"], c["running"]), (2, os.path.getsize(p), False))
        st, hdr, data = self.get("/api/sniff/capture/20261121-101500_blesniff_hci2.pcap")
        self.assertEqual(st, 200)
        self.assertEqual(hdr["Content-Type"], "application/vnd.tcpdump.pcap")
        self.assertIn('attachment; filename="20261121-101500_blesniff_hci2.pcap"', hdr["Content-Disposition"])
        self.assertEqual(data, open(p, "rb").read())
        self.assertEqual(len(read_pcap(p)[1]), 5)

    def test_only_pcap_files_inside_the_capture_folder_can_be_fetched(self):
        open(os.path.join(self.dir, "secret.json"), "w").write("{}")
        for path in ("/api/sniff/capture/..%2f..%2fetc%2fpasswd", "/api/sniff/capture/%2e%2e%2fserver.py",
                     "/api/sniff/capture/secret.json", "/api/sniff/capture/missing.pcap", "/api/sniff/capture/.pcap"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_a_capture_that_a_running_job_is_writing_is_marked_recording(self):
        self.make("20261121-102000_blesniff_hci4.pcap")

        class P:
            def poll(self): return None
        job = server.Job("jobX", "sniff", 4, ["py", "-u", "cli", "--dev", "4", "sniff", "--pcap",
                                              os.path.join(self.dir, "20261121-102000_blesniff_hci4.pcap")])
        job.proc = P()
        with server.JOBS.lock:
            server.JOBS.jobs["jobX"] = job
        try:
            caps = server.sniff_captures()
            self.assertTrue(next(c for c in caps if c["file"].endswith("hci4.pcap"))["running"])
        finally:
            with server.JOBS.lock:
                del server.JOBS.jobs["jobX"]


if __name__ == "__main__":
    unittest.main()
