import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
import _path  # noqa: F401
import server


class LiveServer:
    """The real Handler on an ephemeral loopback port. There is no login, so nothing else is needed to talk to it."""
    def __init__(self):
        self.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, data, h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, dict(r.getheaders()), raw

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class RequestChecks(unittest.TestCase):
    """There is no login (by request). What must still hold: no leftover token machinery, and the same-site / Host rules."""
    @classmethod
    def setUpClass(cls):
        cls.s = LiveServer()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_there_is_no_login_or_token(self):
        for name in ("Auth", "AUTH", "LOGIN_PAGE", "COOKIE_NAME"):
            self.assertFalse(hasattr(server, name), name)
        for path in ("/", "/app.js", "/classic.js", "/style.css", "/api/status", "/api/sniff/devices", "/api/classic/results",
                     "/api/bluez/status", "/api/units"):
            self.assertEqual(self.s.req("GET", path)[0], 200, path)
        self.assertEqual(self.s.req("POST", "/api/stop_all", {})[0], 200)

    def test_old_login_links_still_open_the_page(self):
        self.assertEqual(self.s.req("GET", "/?token=anything-at-all")[0], 200)       # a bookmark from the token days
        st, hdr, _ = self.s.req("GET", "/login")
        self.assertEqual((st, hdr["Location"]), (302, "/"))

    def test_no_session_cookie_is_ever_set(self):
        for path in ("/", "/api/status"):
            self.assertNotIn("Set-Cookie", self.s.req("GET", path)[1])

    def test_cross_site_and_malformed_state_changes_are_refused(self):
        post = lambda extra: self.s.req("POST", "/api/stop_all", {}, extra)[0]   # noqa: E731
        self.assertEqual(post({}), 200)
        self.assertEqual(post({"Origin": "http://127.0.0.1:%d" % self.s.port}), 200)
        self.assertEqual(post({"Origin": "http://evil.example"}), 403)
        self.assertEqual(post({"Origin": "null"}), 403)
        self.assertEqual(post({"Sec-Fetch-Site": "cross-site"}), 403)
        self.assertEqual(self.s.req("POST", "/api/stop_all", b"{}", {"Content-Type": "text/plain"})[0], 415)

    def test_cross_site_reads_are_refused(self):
        st = self.s.req("GET", "/api/status", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"})[0]
        self.assertEqual(st, 403)

    def test_dns_rebinding_host_is_refused_on_loopback(self):
        self.assertEqual(self.s.req("GET", "/api/status", headers={"Host": "evil.example"})[0], 403)

    def test_bluez_install_requires_acceptance(self):
        self.assertEqual(self.s.req("POST", "/api/bluez/install", {})[0] in (400, 200), True)
        old = server.bluez_installed
        server.bluez_installed = lambda: False
        try:
            self.assertEqual(self.s.req("POST", "/api/bluez/install", {})[0], 400)
        finally:
            server.bluez_installed = old

    def test_bluetooth_actions_are_gated_while_bluez_is_missing(self):
        old = server.bluez_installed
        server.bluez_installed = lambda: False
        try:
            for path in ("/api/start", "/api/classic/action", "/api/classic/reserve"):
                st, _, body = self.s.req("POST", path, {"dev": 1})
                self.assertEqual(st, 409, path)
                self.assertTrue(json.loads(body).get("bluez_missing"))
        finally:
            server.bluez_installed = old

    def test_a_non_object_body_is_a_clean_refusal_not_a_crash(self):
        st, _, body = self.s.req("POST", "/api/start", b"[1, 2, 3]")
        self.assertIn(st, (400, 409))


class HealthTests(unittest.TestCase):
    def test_health_report_shape_and_clean_state(self):
        r = server.health_report(detail=True)
        self.assertTrue(r["ok"] or r["warnings"])
        for k in ("version", "result_schema", "uptime_s", "checks", "instance"):
            self.assertIn(k, r)
        self.assertTrue(r["checks"]["install_ok"])
        self.assertIn("health", server.JOBS.snapshot())

    def test_moved_install_folder_is_reported_not_silent(self):
        old = server.STATIC_DIR
        server.STATIC_DIR = "/nonexistent/static"
        try:
            r = server.health_report()
            self.assertFalse(r["ok"])
            self.assertIn("moved", r["warnings"][0])
        finally:
            server.STATIC_DIR = old

    def test_unwritable_and_full_results_disk_are_reported(self):
        old = server.RESULTS_DIR
        server.RESULTS_DIR = "/proc/nope/classic"
        try:
            self.assertTrue(any("cannot be saved" in w for w in server.health_report()["warnings"]))
        finally:
            server.RESULTS_DIR = old
        with mock.patch("server.shutil.disk_usage", return_value=mock.Mock(free=10 * 1048576)):
            self.assertTrue(any("Low disk space" in w for w in server.health_report()["warnings"]))

    def test_page_request_says_why_when_the_folder_is_gone(self):
        s = LiveServer()
        old = server.STATIC_DIR
        server.STATIC_DIR = "/nonexistent/static"
        try:
            st, _, body = s.req("GET", "/app.js")
            self.assertEqual(st, 503)
            self.assertIn(b"moved or removed", body)
        finally:
            server.STATIC_DIR = old
            s.close()

    def test_health_endpoint_is_open_and_names_the_server_process(self):
        s = LiveServer()
        try:
            st, _, body = s.req("GET", "/api/health")
            self.assertEqual(st, 200)
            h = json.loads(body)
            self.assertIn("checks", h)
            self.assertEqual(h["instance"], server.INSTANCE)
        finally:
            s.close()


class ArgumentValidation(unittest.TestCase):
    def ok(self, tool, p):
        return server.build_argv(tool, 1, p)

    def bad(self, tool, p):
        with self.assertRaises(ValueError):
            server.build_argv(tool, 1, p)

    def test_accepts_what_the_gui_sends(self):
        self.ok("adv", {"type": "ind", "interval": "100", "channels": "37,38,39", "name": "Lab Tag", "profile": "ibeacon",
                        "profile_cfg": "uuid=00112233-4455-6677-8899-aabbccddeeff,major=1,minor=2", "flags": "06", "duration": "0"})
        self.ok("adv", {"profile": "eddystone_url", "profile_cfg": "url=https://example.com/x"})
        self.assertIn("--power", self.ok("adv", {"type": "nonconn", "power": "-20"}))
        self.assertNotIn("--power", self.ok("adv", {"type": "nonconn", "power": ""}))
        self.ok("interfere", {"mode": "single", "arg": "19", "bw": "20", "len": "37", "payload": "prbs9", "phy": "1M",
                              "dwell": "800", "power": "max", "duration": "0", "map": False})
        self.ok("interfere", {"mode": "wifi", "arg": "6", "bw": "40"})
        self.ok("interfere", {"mode": "sweep", "arg": "0-39"})
        self.ok("interfere", {"mode": "hop", "arg": "1,2,3"})
        self.ok("interfere", {"mode": "band"})
        self.ok("sniff", {"active": True, "filter": "HTWS", "duration": "30"})

    def test_rejects_bad_values(self):
        self.bad("adv", {"channels": "37,40"})
        self.bad("adv", {"type": "x; rm -rf /"})
        self.bad("adv", {"interval": "10"})
        self.bad("adv", {"interval": "99999"})
        self.bad("adv", {"name": "x" * 40})
        self.bad("adv", {"name": "a\x00b"})
        self.bad("adv", {"type": "direct", "peer": "zz"})
        self.bad("adv", {"data": "xyz"})
        self.bad("adv", {"duration": "-1"})
        self.bad("adv", {"power": "21"})
        self.bad("adv", {"power": "-128"})
        self.bad("adv", {"power": "loud"})
        self.bad("interfere", {"mode": "single", "arg": "40"})
        self.bad("interfere", {"mode": "single"})
        self.bad("interfere", {"mode": "wifi", "arg": "14"})
        self.bad("interfere", {"mode": "sweep", "arg": "5"})
        self.bad("interfere", {"mode": "band", "power": "99"})
        self.bad("interfere", {"mode": "band", "len": "300"})
        self.bad("interfere", {"mode": "nonsense"})
        self.bad("sniff", {"interval": "50", "window": "100"})
        self.bad("sniff", {"filter": "a\x00b"})
        self.bad("nonsense", {})

    def test_values_stay_separate_argv_items_never_a_shell_string(self):
        argv = self.ok("adv", {"name": "a b; $(touch /tmp/x)"})
        self.assertIn("a b; $(touch /tmp/x)", argv)                   # one element, passed without a shell


class JobBookkeeping(unittest.TestCase):
    class P:
        def __init__(self, rc): self.rc = rc
        def poll(self): return self.rc

    def test_finished_jobs_and_old_sniff_tables_are_pruned(self):
        J = server.Jobs()
        for i in range(80):
            j = server.Job("job%d" % i, "sniff" if i % 2 == 0 else "adv", i % 2, ["x"] * 7)
            j.proc, j.started, j.devices = self.P(0), i, {"a": 1}
            J.jobs[j.id] = j
        run = server.Job("run", "adv", 5, ["x"] * 7)
        run.proc, run.started = self.P(None), 999
        J.jobs["run"], J.by_dev[5] = run, "run"
        with J.lock:
            J._prune()
        self.assertEqual(sum(1 for v in J.jobs.values() if v.proc.poll() is not None), J.MAX_FINISHED_KEPT)
        self.assertIn("run", J.jobs)
        self.assertEqual(sum(1 for v in J.jobs.values() if v.kind == "sniff" and v.devices), 1)

    def test_stop_all_reports_what_did_not_stop(self):
        J = server.Jobs()
        j = server.Job("stuck", "adv", 0, ["x"] * 7)
        j.proc = self.P(None)
        J.jobs["stuck"] = j
        J.stop = lambda jid, timeout=5.0: (False, "did not stop")
        self.assertEqual(J.stop_all(), [("stuck", "did not stop")])

    def test_a_failed_capabilities_probe_is_not_cached(self):
        old = server.CLI
        server.CLI, server._CAPS["data"] = "/nonexistent/cli.py", None
        try:
            self.assertIn("error", server.get_caps())
            self.assertIsNone(server._CAPS["data"])
        finally:
            server.CLI = old

    def test_survey_line_matcher_accepts_any_json_spacing(self):
        for line in ('{"m":"dev","a":1}', '{"m": "warn", "msg": "x"}', '{ "m" : "stats"}'):
            self.assertTrue(server.SURVEY_LINE.match(line))
        self.assertFalse(server.SURVEY_LINE.match('{"addr":1}'))

    def test_job_status_carries_args_for_ble_jobs_only(self):
        self.assertEqual(server.Job("j", "adv", 1, ["p", "-u", "c", "--dev", "1", "adv", "nonconn", "-i", "20"]).status()["args"], "nonconn -i 20")
        self.assertEqual(server.Job("j", "c_a2dp", 0, ["p", "c", "--hci", "0", "a2dp", "AA"]).status()["args"], "")


class ResultListing(unittest.TestCase):
    def test_corrupt_oversized_and_good_files(self):
        with tempfile.TemporaryDirectory() as d:
            old = server.RESULTS_DIR
            server.RESULTS_DIR = d
            try:
                open(os.path.join(d, "bad.json"), "w").write("{not json")
                open(os.path.join(d, "empty.json"), "w").write("")
                open(os.path.join(d, "good.json"), "w").write(json.dumps({"summary": {"test": "a2dp", "pass": True, "addr": "AA"}}))
                with open(os.path.join(d, "huge.json"), "wb") as f:
                    f.seek(9 * 1024 * 1024)
                    f.write(b"0")
                r = server.classic_results()
                self.assertEqual([x["file"] for x in r], ["good.json"])
            finally:
                server.RESULTS_DIR = old


if __name__ == "__main__":
    unittest.main()
