"""Realistic Classic data traffic (file transfer, video, HID): traffic shapes, OBEX framing, reconnects,
the job code on fake hardware, and argument validation. No radios: the engine's Session runs over loopback TCP."""
import contextlib
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import _path  # noqa: F401
import bluforge_classic as bc
import bt_traffic as bt
import server

ADDR = "AA:BB:CC:DD:EE:FF"
FAST = {"file_mb": 0.2, "file_gap_s": 0.1, "segment_s": 0.4, "startup_segments": 2, "video_kbps": 800}


class Tcp:
    """Stands in for the Bluetooth sockets: the same Session code over loopback TCP."""
    def __init__(self):
        self.connect_error = None           # exception (or callable returning one) raised by connect()
        self.connects = 0

    def listen(self, proto, addr):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(4)
        return s, s.getsockname()[1]

    def connect(self, proto, src, dst, chan, timeout):
        self.connects += 1
        err = self.connect_error() if callable(self.connect_error) else self.connect_error
        if err:
            raise err
        return socket.create_connection(("127.0.0.1", chan), timeout)

    def mtu(self, proto, sock):
        return 672 if proto == "l2cap" else 32768


def wait_for(pred, secs):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class Shapes(unittest.TestCase):
    def test_every_kind_has_a_plan(self):
        for k in bt.KINDS:
            self.assertTrue(bt.plan(k), k)

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            bt.plan("torrent")

    def test_the_kinds_are_file_video_and_hid(self):
        self.assertEqual(bt.KINDS, ("file", "video", "hid"))
        self.assertEqual([[x["name"] for x in bt.plan(k)] for k in bt.KINDS], [["file"], ["video"], ["hid"]])

    def test_removed_kinds_are_refused(self):
        for k in ("web", "spp", "mixed"):
            with self.assertRaises(ValueError):
                bt.plan(k)

    def test_protocols_and_directions(self):
        p = {k: bt.plan(k)[0] for k in bt.KINDS}
        self.assertEqual((p["file"]["proto"], p["file"]["direction"]), ("rfcomm", "up"))
        self.assertEqual((p["video"]["proto"], p["video"]["direction"]), ("l2cap", "down"))
        self.assertEqual((p["hid"]["proto"], p["hid"]["direction"]), ("l2cap", "up"))

    def test_parameters_override_defaults_and_none_keeps_them(self):
        s = bt.plan("video", {"video_kbps": 600, "file_mb": None})[0]
        self.assertEqual(s["bitrate_kbps"], 600)
        self.assertEqual(bt.plan("file", {"file_mb": None})[0]["file_bytes"], bt.DEFAULTS["file_mb"] * 1000000)
        self.assertEqual(bt.plan("file", {"file_mb": 0.5})[0]["file_bytes"], 500000)

    def test_kinds_offered_by_the_page_match_the_engine(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "src", "static", "index.html")) as f:
            html = f.read()
        block = re.search(r'id="cl_kind">(.*?)</select>', html, re.S).group(1)
        self.assertEqual(re.findall(r'value="(\w+)"', block), ["audio"] + list(bt.KINDS))


class Obex(unittest.TestCase):
    def test_headers_round_trip(self):
        buf = bt.hdr_name("a.bin") + bt.hdr_length(1234) + bt.hdr_body(False, 10) + bt.hdr_body(True, 3)
        hs = list(bt.iter_headers(buf))
        self.assertEqual([h for h, _ in hs], [bt.H_NAME, bt.H_LENGTH, bt.H_BODY, bt.H_END_BODY])
        self.assertEqual(hs[0][1], "a.bin".encode("utf-16-be") + b"\0\0")
        self.assertEqual(int.from_bytes(hs[1][1], "big"), 1234)
        self.assertEqual((len(hs[2][1]), len(hs[3][1])), (10, 3))

    def test_created_is_a_successful_answer_to_the_last_packet(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        got = []

        def accept_all():
            c, _ = srv.accept()
            stop = threading.Event()
            while True:
                try:
                    op, pl = bt.read_obex(c, stop)
                except OSError:
                    return
                if op == bt.OBEX_CONNECT:
                    c.sendall(bt.obex_packet(bt.OBEX_OK, b"\x10\x00\x40\x00"))
                elif op in (bt.OBEX_PUT, bt.OBEX_PUT_FINAL):
                    got.append(op)
                    c.sendall(bt.obex_packet(bt.OBEX_CREATED if op == bt.OBEX_PUT_FINAL else bt.OBEX_CONTINUE))
                else:
                    c.sendall(bt.obex_packet(bt.OBEX_OK))
        threading.Thread(target=accept_all, daemon=True).start()
        s = bt.Session("file", "a", "b", Tcp(), FAST, remote_channel=srv.getsockname()[1]).start()
        try:
            self.assertTrue(wait_for(lambda: s.streams[0].n.get("files", 0) >= 2, 8))
            self.assertEqual(s.streams[0].drops, 0)
        finally:
            s.stop()
            srv.close()

    def test_a_receiver_that_is_slow_to_answer_is_noted(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)

        def slow():
            c, _ = srv.accept()
            stop = threading.Event()
            while True:
                try:
                    op, pl = bt.read_obex(c, stop)
                except OSError:
                    return
                if op == bt.OBEX_CONNECT:
                    c.sendall(bt.obex_packet(bt.OBEX_OK, b"\x10\x00\x40\x00"))
                else:
                    time.sleep(0.6)                         # a person deciding whether to accept the file
                    c.sendall(bt.obex_packet(bt.OBEX_OK if op == bt.OBEX_PUT_FINAL else bt.OBEX_CONTINUE))
        threading.Thread(target=slow, daemon=True).start()
        with mock.patch.object(bt, "SLOW_ANSWER_S", 0.3):
            s = bt.Session("file", "a", "b", Tcp(), FAST, remote_channel=srv.getsockname()[1]).start()
            try:
                self.assertTrue(wait_for(lambda: s.streams[0].note, 8))
                self.assertIn("accept", s.report()[0]["note"])
                self.assertIsNone(s.streams[0].err)
            finally:
                s.stop()
                srv.close()

    def test_a_receiver_that_refuses_the_file_is_reported_not_hung(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)

        def refuse():
            c, _ = srv.accept()
            stop = threading.Event()
            bt.read_obex(c, stop)
            c.sendall(bt.obex_packet(bt.OBEX_OK, b"\x10\x00\x40\x00"))
            bt.read_obex(c, stop)
            c.sendall(bt.obex_packet(0xC3))                 # Forbidden
            time.sleep(0.5)
            c.close()
        threading.Thread(target=refuse, daemon=True).start()
        t = Tcp()
        s = bt.Session("file", "a", "b", t, FAST, remote_channel=srv.getsockname()[1]).start()
        try:
            self.assertTrue(wait_for(lambda: s.streams[0].drops >= 1, 5))
            self.assertIn("refused", s.streams[0].err)
        finally:
            s.stop()
            srv.close()


class SessionOverLoopback(unittest.TestCase):
    def run_kind(self, kind, secs=1.5, params=None):
        s = bt.Session(kind, "a", "b", Tcp(), dict(FAST, **(params or {}))).start()
        wait_for(s.all_up, 3)
        time.sleep(secs)
        up = s.all_up()
        s.stop()
        return s, up

    def test_every_kind_moves_data_and_every_sent_byte_is_received(self):
        for kind in bt.KINDS:
            s, up = self.run_kind(kind)
            t = s.totals()
            self.assertTrue(up, kind)
            self.assertGreater(t["rx"], 0, kind)
            self.assertLessEqual(t["tx"] - t["rx"], 100000, kind)      # only what was in flight at stop
            self.assertEqual(t["drops"], 0, kind)

    def test_file_transfer_completes_files_and_reports_their_speed(self):
        s, _ = self.run_kind("file", 2.0)
        r = s.report()[0]
        self.assertGreaterEqual(r["files"], 2)
        self.assertIn("file_kbps_avg", r)

    def test_video_fetches_segments_at_roughly_the_bitrate(self):
        s, _ = self.run_kind("video", 2.5)
        r = s.report()[0]
        self.assertGreaterEqual(r["segments"], 3)
        seg = int(800 * 1000 / 8 * 0.4)
        self.assertEqual(r["tx_bytes"] // seg, r["segments"])          # whole segments, none partial but the last

    def test_hid_keeps_its_rate(self):
        s, _ = self.run_kind("hid", 1.5)
        r = s.report()[0]
        self.assertTrue(80 <= r["tx_bytes"] // 8 <= 220, r)           # 125 reports/s for ~1.5 s

    def test_a_lost_link_is_reconnected(self):
        t = Tcp()
        s = bt.Session("hid", "a", "b", t, FAST).start()
        try:
            self.assertTrue(wait_for(s.all_up, 3))
            with s.lock:
                socks = list(s.socks)
            for c in socks:
                c.shutdown(socket.SHUT_RDWR)
            with mock.patch.object(bt, "RETRY_S", 0.1):
                self.assertTrue(wait_for(lambda: s.streams[0].connects >= 2 and s.all_up(), 6))
            self.assertGreaterEqual(s.streams[0].drops, 1)
        finally:
            s.stop()

    def test_denied_link_is_flagged_and_retried(self):
        t = Tcp()
        t.connect_error = PermissionError(13, "Permission denied")
        with mock.patch.object(bt, "RETRY_S", 0.05):
            s = bt.Session("file", "a", "b", t, FAST).start()
            try:
                self.assertTrue(wait_for(s.auth_denied, 3))
                self.assertFalse(s.all_up())
                t.connect_error = None                       # pairing fixed: the next retry succeeds
                self.assertTrue(wait_for(lambda: s.all_up() and not s.auth_denied(), 3))
            finally:
                s.stop()

    def test_stop_returns_promptly_and_leaves_no_thread(self):
        s = bt.Session("video", "a", "b", Tcp(), FAST).start()
        wait_for(s.all_up, 3)
        t0 = time.time()
        s.stop()
        self.assertLess(time.time() - t0, 4)
        self.assertFalse([th for th in s.threads if th.is_alive()])


class FakeBZ:
    hci = 0
    adapter = "/org/bluez/hci0"

    def __init__(self):
        self.calls = []

    def dev_path(self, addr):
        return "/org/bluez/hci0/dev_" + addr.replace(":", "_")

    def exists(self, path):
        return True

    def get(self, path, iface, prop):
        return {"Paired": True, "Connected": True}.get(prop)

    def call(self, path, iface, method, params=None, timeout_ms=0):
        self.calls.append(method)


def run_traffic(argv_tail, local=True, tcp=None, repair=None, paired=True, channel=None):
    """Run job_traffic on fake hardware with the engine on loopback TCP. Returns (rc or exception, summary, context)."""
    bz = FakeBZ()
    if not paired:
        bz.get = lambda path, iface, prop: False
    tcp = tcp or Tcp()
    repairs = []

    def act_link_local(a, b):
        repairs.append(a)
        if repair:
            repair(a)
        return {"ok": True}

    def pump(sec):
        time.sleep(0.05)

    sessions = []
    real_session = bt.Session

    def session(*a, **kw):
        s = real_session(*a, **dict(kw, transport=tcp, params=dict(kw.get("params") or {}, **FAST)))
        sessions.append(s)
        return s

    a = bc.build_parser().parse_args(["--hci", "0"] + argv_tail)
    out = io.StringIO()
    tmp = tempfile.mkdtemp()
    with contextlib.ExitStack() as st:
        st.enter_context(mock.patch.dict(sys.modules, {"acl_meter": None}))
        st.enter_context(mock.patch.object(bc, "local_adapter_tags", lambda b: {ADDR.replace(":", "_").lower()} if local else set()))
        st.enter_context(mock.patch.object(bc.bt_traffic, "Session", session))
        st.enter_context(mock.patch.object(bc.bt_traffic, "RETRY_S", 0.1))
        for name, val in (("ensure_powered", lambda *x: None), ("ensure_device", lambda *x: None),
                          ("adapter_info", lambda b: {"addr": "11:22:33:44:55:66"}), ("hci_of_addr", lambda b, ad: 1),
                          ("act_link_local", act_link_local), ("pump", pump), ("acl_links", lambda h: []),
                          ("sdp_rfcomm_channel", lambda h, ad, u: channel),
                          ("link_stats", lambda h, ad: {"rssi": 0, "tx_power": None, "lq": 255}),
                          ("afh_info", lambda h, ad: {"enabled": True, "channels": 22, "map_hex": "ff" * 10, "unused": [1]}),
                          ("LOG_DIR", tmp), ("AFH_EVERY_S", 0.05)):
            st.enter_context(mock.patch.object(bc, name, val))
        bc.STOP.clear()
        st.enter_context(contextlib.redirect_stdout(out))
        try:
            rc = bc.job_traffic(a, bz)
        except bc.ClassicError as e:
            rc = e
    summary = None
    for line in out.getvalue().splitlines():
        if line.startswith('{"m":"summary"'):
            summary = json.loads(line)
    return rc, summary, {"bz": bz, "repairs": repairs, "sessions": sessions, "out": out.getvalue()}


class JobOnFakeHardware(unittest.TestCase):
    def test_internal_receiver_run_passes_and_reports_delivered_traffic(self):
        rc, sm, ctx = run_traffic(["traffic", ADDR, "--kind", "video", "-t", "2"])
        self.assertEqual(rc, 0)
        self.assertEqual((sm["test"], sm["kind"], sm["pass"], sm["internal_receiver"]), ("traffic", "video", True, True))
        self.assertEqual(len(sm["streams"]), 1)
        self.assertGreater(sm["delivered_bytes"], 0)
        self.assertGreaterEqual(sm["active_pct"], 95)
        self.assertGreater(sm["app_kbps"]["avg"], 0)
        self.assertEqual(sm["afh"]["channels_max"], 22)
        self.assertEqual(sm["role"], "interference source")
        self.assertIn("Disconnect", ctx["bz"].calls)             # an internal receiver link is never left behind
        self.assertTrue(all(not th.is_alive() for th in ctx["sessions"][0].threads))

    def test_a_stale_pairing_is_repaired_once_and_the_run_carries_on(self):
        tcp = Tcp()
        tcp.connect_error = lambda: PermissionError(13, "denied") if not ctx_repaired else None
        ctx_repaired = []
        rc, sm, ctx = run_traffic(["traffic", ADDR, "--kind", "hid", "-t", "3"], tcp=tcp, repair=lambda a: ctx_repaired.append(1))
        self.assertEqual(rc, 0, sm and sm["fail_reasons"])
        self.assertEqual(len(ctx["repairs"]), 1)
        self.assertTrue(ctx["repairs"][0].force)
        self.assertEqual(ctx["repairs"][0].peer, 1)
        self.assertTrue(sm["pairing_repaired"])

    def test_a_pairing_that_cannot_be_repaired_fails_the_run(self):
        tcp = Tcp()
        tcp.connect_error = PermissionError(13, "denied")
        rc, sm, ctx = run_traffic(["traffic", ADDR, "--kind", "hid", "-t", "30"], tcp=tcp)
        self.assertEqual(rc, 2)
        self.assertFalse(sm["pass"])
        self.assertTrue(any("re-pairing" in r for r in sm["fail_reasons"]), sm["fail_reasons"])
        self.assertEqual(len(ctx["repairs"]), 1)

    def test_links_that_never_come_up_are_flagged_not_passed(self):
        tcp = Tcp()
        tcp.connect_error = ConnectionRefusedError(111, "refused")
        rc, sm, _ = run_traffic(["traffic", ADDR, "--kind", "video", "-t", "1"], tcp=tcp)
        self.assertEqual(rc, 2)
        self.assertEqual(sm["active_pct"], 0.0)
        self.assertGreaterEqual(sm["reconnect_failures"], 1)

    def test_an_unpaired_internal_receiver_is_refused_with_the_fix(self):
        rc, sm, _ = run_traffic(["traffic", ADDR, "--kind", "file", "-t", "1"], paired=False)
        self.assertIsInstance(rc, bc.ClassicError)
        self.assertIn("link-local", str(rc))

    def test_only_file_transfer_can_go_to_a_real_device(self):
        for kind in ("video", "hid"):
            rc, _, _ = run_traffic(["traffic", ADDR, "--kind", kind, "-t", "1"], local=False, channel=9)
            self.assertIsInstance(rc, bc.ClassicError, kind)
            self.assertIn("internal receiver", str(rc))

    def test_a_real_device_without_object_push_is_refused(self):
        rc, _, _ = run_traffic(["traffic", ADDR, "--kind", "file", "-t", "1"], local=False, channel=None)
        self.assertIsInstance(rc, bc.ClassicError)
        self.assertIn("Object Push", str(rc))

    def test_file_transfer_to_a_real_device_uses_its_obex_channel(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(2)

        def serve():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                st = bt.Stream(bt.plan("file", FAST)[0])
                threading.Thread(target=lambda: _quiet(bt.obex_server, c, st, threading.Event()), daemon=True).start()
        threading.Thread(target=serve, daemon=True).start()
        try:
            rc, sm, ctx = run_traffic(["traffic", ADDR, "--kind", "file", "-t", "2"], local=False, channel=srv.getsockname()[1])
            self.assertEqual(rc, 0, sm and sm["fail_reasons"])
            self.assertEqual(ctx["sessions"][0].remote_channel, srv.getsockname()[1])
            self.assertFalse(sm["internal_receiver"])
            self.assertNotIn("Disconnect", ctx["bz"].calls)       # only an internal receiver link is torn down by the job
        finally:
            srv.close()


def _quiet(fn, *a):
    try:
        fn(*a)
    except OSError:
        pass


class Arguments(unittest.TestCase):
    def test_defaults(self):
        a = server.build_classic_argv("c_traffic", 1, {"addr": ADDR})
        self.assertEqual(a[a.index("traffic") + 1], ADDR)
        self.assertEqual(a[a.index("--kind") + 1], "file")
        self.assertEqual(a[a.index("-t") + 1], "0")

    def test_values_are_passed_through(self):
        a = server.build_classic_argv("c_traffic", 1, {"addr": ADDR.lower(), "kind": "video", "duration": "600", "file_mb": "3.5",
                                                       "video_kbps": "1200", "min_active_pct": "90"})
        self.assertIn(ADDR, a)
        self.assertEqual(a[a.index("--kind") + 1], "video")
        self.assertEqual(a[a.index("--file-mb") + 1], "3.5")
        self.assertEqual(a[a.index("--video-kbps") + 1], "1200")
        self.assertEqual(a[a.index("--min-active-pct") + 1], "90.0")

    def test_blank_fields_are_left_to_the_engine_defaults(self):
        a = server.build_classic_argv("c_traffic", 1, {"addr": ADDR, "file_mb": "", "video_kbps": " "})
        self.assertNotIn("--file-mb", a)
        self.assertNotIn("--video-kbps", a)

    def test_bad_input_is_refused(self):
        for bad in ({"addr": "nope"}, {"addr": ADDR, "kind": "torrent"}, {"addr": ADDR, "kind": "file; rm"},
                    {"addr": ADDR, "duration": "-1"}, {"addr": ADDR, "file_mb": "0"}, {"addr": ADDR, "file_mb": "99999"},
                    {"addr": ADDR, "video_kbps": "10"}, {"addr": ADDR, "video_kbps": "x"}, {"addr": ADDR, "min_active_pct": "101"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                server.build_classic_argv("c_traffic", 1, bad)

    def test_it_is_a_classic_job_that_needs_a_reserved_radio(self):
        self.assertIn("c_traffic", server.CLASSIC_JOB_KINDS)

    def test_cli_defaults(self):
        a = bc.build_parser().parse_args(["--hci", "0", "traffic", ADDR])
        self.assertEqual((a.kind, a.duration, a.min_active_pct), ("file", 0, 80.0))
        self.assertIs(bc.JOBS["traffic"], bc.job_traffic)


if __name__ == "__main__":
    unittest.main()
