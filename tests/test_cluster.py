"""Cluster mode, hardware-free: a real BluForge Handler (the "manager") talks to fake resources and a fake LANforge manager
JSON API on loopback addresses (127.0.0.2, 127.0.0.3 ... all belong to this machine). No radios, no root, no network."""
import http.client
import json
import queue
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import _path  # noqa: F401
import cluster
import server

LOCAL_IP = "10.9.9.9"        # what the fake manager calls this machine; never really used


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.2", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeManagerApi:
    """The LANforge JSON API's /resource/all, with a list the test can change. Like the real one it answers only the fields
    that the request names (fields=a,b,c), so a field BluForge forgets to ask for is missing from the answer here too."""
    def __init__(self):
        self.resources = []
        self.closed = False
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_GET(self):
                if not self.path.startswith("/resource/all"):
                    self.send_response(404)
                    self.end_headers()
                    return
                if "Accept" not in self.headers:            # the real LANforge JSON API answers 500 without an Accept header
                    self.send_response(500)
                    self.end_headers()
                    return
                want = [f for f in parse_qs(urlsplit(self.path).query).get("fields", [""])[0].split(",") if f]
                recs = [{k: v for k, v in r.items() if not want or k in want} for r in outer.resources]
                body = json.dumps({"handler": "x", "resources": [{r["eid"]: r} for r in recs]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        if not self.closed:                  # shutdown() waits for serve_forever, so it must only be asked once
            self.closed = True
            self.srv.shutdown()
            self.srv.server_close()


class FakePeer:
    """A resource's BluForge: the same API, canned data, and a log of every request it received."""
    def __init__(self, ip, port, host, radios, jobs=None, bluez=True):
        self.host, self.radios, self.bluez = host, radios, bluez
        self.jobs = list(jobs or [])
        self.requests = []
        self.events = queue.Queue()
        self.stream_clients = 0
        self.closing = False
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append(("GET", self.path, dict(self.headers), None))
                p = self.path.split("?")[0]
                if p == "/api/health":
                    return self._json(200, {"ok": True, "warnings": [], "version": "1.0.0", "instance": "inst-" + outer.host,
                                            "host": outer.host, "checks": {}})
                if p == "/api/status":
                    return self._json(200, {"running": [j for j in outer.jobs if j.get("running")], "finished": [j for j in outer.jobs if not j.get("running")],
                                            "available_hci": [r for r in outer.radios if r not in [j["dev"] for j in outer.jobs if j.get("running")]],
                                            "all_hci": outer.radios, "classic_reserved": [], "hostname": outer.host,
                                            "bluez": {"installed": outer.bluez}, "health": {"ok": True, "warnings": []}, "version": "1.0.0"})
                if p == "/api/caps":
                    return self._json(200, {"hardware": {"all_hci": outer.radios}, "adv": {"profiles": ["ibeacon"]}})
                if p == "/api/classic/preflight":
                    return self._json(200, {"ok": True, "notes": ["no PipeWire"], "reserved": [outer.radios[-1]], "all_hci": outer.radios})
                if p == "/api/classic/results":
                    return self._json(200, {"results": [{"file": "a2dp-1.json", "csv": "a2dp-1.csv", "mtime": 5000.0, "hci": outer.radios[-1],
                                                         "test": "a2dp", "pass": True}]})
                if p.startswith("/api/classic/result/"):
                    body = b'{"summary": {"from": "%s"}}' % outer.host.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Disposition", 'attachment; filename="%s"' % p.rsplit("/", 1)[1])
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
                if p == "/api/sniff/devices":
                    return self._json(200, {"sniffs": [{"job_id": "job1", "dev": outer.radios[0], "running": True, "stats": None,
                                                        "devices": [{"addr": "AA:BB:CC:DD:EE:01"}]}], "now": 1.0})
                if p == "/api/stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    outer.stream_clients += 1
                    try:
                        while not outer.closing:
                            try:
                                evt = outer.events.get(timeout=0.2)
                            except queue.Empty:
                                continue
                            self.wfile.write(("data: %s\n\n" % json.dumps(evt)).encode())
                            self.wfile.flush()
                    except Exception:
                        pass
                    finally:
                        outer.stream_clients -= 1
                    return
                self._json(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append(("POST", self.path, dict(self.headers), body))
                p = self.path
                if p == "/api/start":
                    return self._json(200, {"ok": True, "msg": "started", "job_id": "job7", "argv": ["fake"]})
                if p == "/api/stop":
                    return self._json(200, {"ok": True, "msg": "stopped"})
                if p == "/api/stop_all":
                    return self._json(200, {"ok": True})
                if p == "/api/classic/reserve":
                    return self._json(200, {"ok": True, "msg": "reserved", "reserved": [body.get("dev")]})
                if p == "/api/classic/action":
                    return self._json(200, {"ok": True, "echo": body, "log": []})
                self._json(404, {"error": "not found"})
        self.srv = ThreadingHTTPServer((ip, port), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def posts(self, path=None):
        return [r for r in self.requests if r[0] == "POST" and (path is None or r[1] == path)]

    def close(self):
        self.closing = True
        self.srv.shutdown()
        self.srv.server_close()


class Hub:
    """A manager's BluForge: the real Handler with a Cluster pointed at the fake manager API and the fake resources."""
    LOCAL_JOB = {"id": "job1", "kind": "adv", "dev": 0, "hci": "hci0", "running": True, "started": 100.0, "stopped": None,
                 "uptime": 3.0, "exit_code": None, "args": "", "addr": None}

    def __init__(self, resources, peer_port, ips=(LOCAL_IP,), local_radios=(0, 1, 2)):
        self.api = FakeManagerApi()
        self.api.resources = resources
        self.patches = [
            mock.patch.object(server, "all_hci", return_value=list(local_radios)),
            mock.patch.object(server, "get_caps", return_value={"hardware": {"all_hci": list(local_radios)}, "adv": {"profiles": ["ibeacon"]},
                                                                "interfere": {"payloads": ["prbs9"]}}),
            mock.patch.object(server, "classic_preflight", return_value={"ok": True, "notes": [], "reserved": [], "all_hci": list(local_radios)}),
            mock.patch.object(server, "classic_results", return_value=[{"file": "local.json", "csv": None, "mtime": 9000.0, "hci": 1, "test": "x"}]),
        ]
        for p in self.patches:
            p.start()
        self.cluster = cluster.Cluster(server.JOBS.publish, "hubhost", server.INSTANCE, 1, manager_api="http://127.0.0.1:%d" % self.api.port,
                                       peer_port=peer_port, scan_interval=0.3, probe_timeout=1.0, ips_fn=lambda: set(ips))
        server.CLUSTER = self.cluster
        server.JOBS.backlog = []
        self.live = None
        from test_server import LiveServer
        self.live = LiveServer()

    def get(self, path, headers=None):
        st, _, raw = self.live.req("GET", path, headers=headers)
        return st, json.loads(raw)

    def post(self, path, body):
        st, _, raw = self.live.req("POST", path, body)
        return st, json.loads(raw)

    def close(self):
        self.cluster.stop()
        server.CLUSTER = None
        server.JOBS.backlog = []
        self.live.close()
        self.api.close()
        for p in self.patches:
            p.stop()


def resource(eid, host, ip):
    return {"eid": eid, "hostname": host, "ctrl-ip": ip, "phantom": False}


class ClusterTestCase(unittest.TestCase):
    """Manager 1.1 (this machine, 3 radios), resource 1.2 (2 radios), resource 1.3 (1 radio), and 1.10, a PC with no BluForge."""
    def setUp(self):
        self.port = free_port()
        self.p12 = FakePeer("127.0.0.2", self.port, "ct523c-ab1a", [0, 1],
                            jobs=[{"id": "job1", "kind": "interfere", "dev": 1, "hci": "hci1", "running": True, "started": 50.0, "stopped": None,
                                   "uptime": 9.0, "exit_code": None, "args": "single 19", "addr": None}])
        self.p13 = FakePeer("127.0.0.3", self.port, "ct523c-cd02", [0])
        self.hub = Hub([resource("1.1", "hubhost", LOCAL_IP), resource("1.2", "ct523c-ab1a", "127.0.0.2"),
                        resource("1.3", "ct523c-cd02", "127.0.0.3"), resource("1.10", "21DODB05", "127.0.0.4")], self.port)
        self.hub.cluster.scan_once()

    def tearDown(self):
        self.hub.close()
        self.p12.close()
        self.p13.close()


class Discovery(ClusterTestCase):
    def test_resources_that_run_bluforge_become_units_and_the_pc_is_listed_separately(self):
        st, d = self.hub.get("/api/units")
        self.assertEqual(st, 200)
        eids = [u["eid"] for u in d["units"]]
        self.assertEqual(eids, ["1.1", "1.2", "1.3"])
        self.assertEqual([u["state"] for u in d["units"]], ["local", "online", "online"])
        self.assertEqual([u["radios"] for u in d["units"]][:1], [3])
        self.assertEqual([o["eid"] for o in d["other_resources"]], ["1.10"])
        self.assertTrue(d["cluster"]["on"])
        self.assertTrue(d["cluster"]["manager_ok"])
        self.assertEqual(d["cluster"]["local"], "1.1")

    def test_every_radio_of_every_unit_is_in_one_list_with_a_unit_name(self):
        st, s = self.hub.get("/api/status")
        self.assertEqual(sorted(s["all_hci"]), [0, 1, 2, 100, 101, 200])
        self.assertEqual(s["labels"]["0"], "1.1.hci0")
        self.assertEqual(s["labels"]["101"], "1.2.hci1")
        self.assertEqual(s["labels"]["200"], "1.3.hci0")
        self.assertEqual(sorted(s["available_hci"]), [0, 1, 2, 100, 200])      # 1.2's hci1 is busy with a job
        self.assertEqual(s["hostname"], socket.gethostname())

    def test_a_lone_manager_keeps_plain_hci_names(self):
        self.hub.api.resources = [resource("1.1", "hubhost", LOCAL_IP)]
        self.hub.cluster.scan_once()
        st, s = self.hub.get("/api/status")
        self.assertEqual(s["labels"]["0"], "hci0")
        self.assertEqual(sorted(s["all_hci"]), [0, 1, 2])
        self.assertFalse(s["cluster"]["on"])

    def test_numbers_do_not_change_when_resources_come_and_go(self):
        before = {u.eid: u.base for u in self.hub.cluster.units.values()}
        self.hub.api.resources = [resource("1.1", "hubhost", LOCAL_IP), resource("1.3", "ct523c-cd02", "127.0.0.3")]
        self.hub.cluster.scan_once()
        self.hub.api.resources.append(resource("1.2", "ct523c-ab1a", "127.0.0.2"))
        self.hub.cluster.scan_once()
        after = {u.eid: u.base for u in self.hub.cluster.units.values()}
        self.assertEqual(before["1.3"], after["1.3"])
        self.assertEqual(before["1.2"], after["1.2"])

    def test_only_resources_that_run_bluforge_get_a_block_of_radio_numbers(self):
        self.hub.api.resources = [resource("1.1", "hubhost", LOCAL_IP), resource("1.10", "21DODB05", "127.0.0.4"),
                                  resource("1.3", "ct523c-cd02", "127.0.0.3"), resource("1.2", "ct523c-ab1a", "127.0.0.2")]
        self.hub.cluster.scan_once()
        bases = {u.eid: u.base for u in self.hub.cluster.units.values()}
        self.assertIsNone(bases["1.10"])                                          # the PC never got one
        self.assertEqual(sorted(b for b in bases.values() if b is not None), [100, 200])
        st, s = self.hub.get("/api/status")
        self.assertEqual(sorted(s["all_hci"]), [0, 1, 2, 100, 101, 200])

    def test_caps_list_every_radio_and_keep_the_local_options(self):
        st, c = self.hub.get("/api/caps")
        self.assertEqual(sorted(c["hardware"]["all_hci"]), [0, 1, 2, 100, 101, 200])
        self.assertEqual(c["adv"]["profiles"], ["ibeacon"])
        self.assertEqual([u["eid"] for u in c["hardware"]["units"]], ["1.1", "1.2", "1.3"])

    def test_resource_with_no_bluforge_is_probed_gently(self):
        pc = next(u for u in self.hub.cluster.units.values() if u.eid == "1.10")
        self.assertEqual(pc.state, "no-bluforge")
        for _ in range(5):
            pc.next_probe = 0
            self.hub.cluster.scan_once()
        self.assertGreater(pc.next_probe, time.time() + 30)                     # after a few misses it is asked about once a minute


class Jobs(ClusterTestCase):
    def test_a_resources_job_is_renumbered_and_named_for_its_unit(self):
        st, s = self.hub.get("/api/status")
        remote = [j for j in s["running"] if j["unit"] == "1.2"]
        self.assertEqual(len(remote), 1)
        self.assertEqual((remote[0]["id"], remote[0]["dev"], remote[0]["label"]), ("1.2:job1", 101, "1.2.hci1"))
        self.assertEqual(remote[0]["args"], "single 19")

    def test_starting_on_a_resource_radio_goes_to_that_resource_with_its_own_numbering(self):
        st, r = self.hub.post("/api/start", {"tool": "adv", "dev": 101, "params": {"type": "ind"}})
        self.assertEqual(st, 200)
        self.assertEqual(r["job_id"], "1.2:job7")
        req = self.p12.posts("/api/start")[0]
        self.assertEqual(req[3]["dev"], 1)
        self.assertEqual(req[3]["tool"], "adv")
        self.assertEqual(req[2].get(cluster.LOCAL_HEADER), "1")                    # "answer for yourself only"
        self.assertEqual(self.p13.posts(), [])

    def test_starting_on_a_local_radio_is_not_forwarded(self):
        with mock.patch.object(server.JOBS, "start", return_value=(True, "started", "job9")) as m, \
                mock.patch.object(server, "bluez_gate", return_value=None):
            st, r = self.hub.post("/api/start", {"tool": "adv", "dev": "1", "params": {"type": "nonconn"}})
        self.assertEqual((st, r["job_id"]), (200, "job9"))
        self.assertEqual(m.call_args[0][1], 1)
        self.assertEqual(self.p12.posts(), [])
        self.assertEqual(self.p13.posts(), [])

    def test_a_radio_that_no_unit_owns_is_a_clear_404(self):
        st, r = self.hub.post("/api/start", {"tool": "adv", "dev": 500, "params": {}})
        self.assertEqual(st, 404)
        self.assertIn("no radio", r["error"])

    def test_a_bad_dev_is_refused_exactly_as_before(self):
        st, r = self.hub.post("/api/start", {"tool": "adv", "dev": "banana", "params": {}})
        self.assertIn(st, (400, 409))

    def test_stop_goes_to_the_unit_named_in_the_job_id(self):
        st, r = self.hub.post("/api/stop", {"job_id": "1.2:job1"})
        self.assertEqual((st, r["ok"]), (200, True))
        self.assertEqual(self.p12.posts("/api/stop")[0][3], {"job_id": "job1"})
        with mock.patch.object(server.JOBS, "stop", return_value=(True, "stopped")):
            self.hub.post("/api/stop", {"job_id": "job1"})
        self.assertEqual(len(self.p12.posts("/api/stop")), 1)                       # a local job id stays local
        st, r = self.hub.post("/api/stop", {"job_id": "9.9:job1"})
        self.assertEqual(st, 404)

    def test_stop_all_is_the_panic_button_for_the_whole_cluster(self):
        with mock.patch.object(server.JOBS, "stop_all", return_value=[]):
            st, r = self.hub.post("/api/stop_all", {})
        self.assertEqual(st, 200)
        self.assertEqual(len(self.p12.posts("/api/stop_all")), 1)
        self.assertEqual(len(self.p13.posts("/api/stop_all")), 1)

    def test_reserve_and_classic_actions_follow_the_radio(self):
        st, r = self.hub.post("/api/classic/reserve", {"dev": 100, "on": True})
        self.assertEqual((st, r["reserved"]), (200, [100]))                          # the resource said [0]; the page sees 100
        self.assertEqual(self.p12.posts("/api/classic/reserve")[0][3]["dev"], 0)
        st, r = self.hub.post("/api/classic/action", {"dev": 101, "action": "scan", "params": {"duration": 5}})
        self.assertEqual(r["echo"]["dev"], 1)

    def test_link_local_needs_both_radios_on_the_same_unit(self):
        post = lambda dev, peer: self.hub.post("/api/classic/action", {"dev": dev, "action": "link-local", "params": {"peer": peer}})   # noqa: E731
        st, r = post(100, 1)                       # 1.2.hci0 with 1.1.hci1
        self.assertEqual(st, 400)
        st, r = post(0, 100)                       # 1.1.hci0 with 1.2.hci0
        self.assertEqual(st, 400)
        st, r = post(100, 101)                     # both on 1.2
        self.assertEqual(st, 200)
        sent = self.p12.posts("/api/classic/action")[-1][3]
        self.assertEqual((sent["dev"], sent["params"]["peer"]), (0, 1))
        self.assertEqual(len(self.p12.posts("/api/classic/action")), 1)             # the two refusals never left the manager


class Offline(ClusterTestCase):
    def test_a_resource_that_stops_answering_is_hidden_and_reported_then_returns(self):
        self.p12.close()
        st, s = self.hub.get("/api/status")
        self.assertEqual(sorted(s["all_hci"]), [0, 1, 2, 200])
        self.assertTrue(any("1.2" in w and "stopped answering" in w for w in s["health"]["warnings"]), s["health"])
        self.assertEqual([u["state"] for u in s["units"] if u["eid"] == "1.2"], ["offline"])
        st, r = self.hub.post("/api/start", {"tool": "adv", "dev": 100, "params": {}})
        self.assertEqual(st, 503)
        self.assertFalse(r["ok"])
        # it comes back
        self.p12 = FakePeer("127.0.0.2", self.port, "ct523c-ab1a", [0, 1])
        self.hub.cluster.units["1.2"].next_probe = 0
        self.hub.cluster.scan_once()
        st, s = self.hub.get("/api/status")
        self.assertEqual(sorted(s["all_hci"]), [0, 1, 2, 100, 101, 200])

    def test_a_resource_without_bluez_is_listed_with_a_warning(self):
        self.p13.bluez = False
        self.hub.cluster.units["1.3"].status_at = 0
        st, s = self.hub.get("/api/status")
        self.assertTrue(any("1.3" in w and "BlueZ" in w for w in s["health"]["warnings"]))
        self.assertIn(200, s["all_hci"])

    def test_a_manager_api_that_is_down_leaves_a_lone_unit_not_an_error(self):
        self.hub.api.close()
        self.hub.cluster.scan_once()
        st, s = self.hub.get("/api/status")
        self.assertEqual(st, 200)
        self.assertFalse(s["cluster"]["manager_ok"])


class OnlyLanforgeUnits(unittest.TestCase):
    """The manager's resource list also holds phones (1.11 and 1.12 on the lab manager are Android phones, interop devices). They are
    not LANforge units: BluForge neither lists them nor looks for a service on them. A LANforge unit that merely has no BluForge yet
    stays on the page, with the note to install it."""
    def setUp(self):
        self.port = free_port()
        self.p12 = FakePeer("127.0.0.2", self.port, "ct523c-ab1a", [0, 1])

        def rec(eid, host, ip, hw):
            return {"eid": eid, "hostname": host, "ctrl-ip": ip, "phantom": False, "hw version": hw}
        self.hub = Hub([rec("1.1", "hubhost", LOCAL_IP, "Linux/x86-64"), rec("1.2", "ct523c-ab1a", "127.0.0.2", "Linux/x86-64"),
                        rec("1.3", "ct523c-cd02", "127.0.0.3", "Linux/x86-64"),
                        rec("1.11", "NTSV-J900XHX2", "127.0.0.4", "Nothing AIN065 r16 sdk: 36"),
                        rec("1.12", "kvm-slave-build-s-system-106341", "127.0.0.5", "OPPO CPH2219 r13 sdk: 33")], self.port)
        self.hub.cluster.scan_once()

    def tearDown(self):
        self.hub.close()
        self.p12.close()

    def test_phones_are_not_listed_and_never_probed(self):
        st, d = self.hub.get("/api/units")
        self.assertEqual([u["eid"] for u in d["units"]], ["1.1", "1.2"])
        self.assertEqual([o["eid"] for o in d["other_resources"]], ["1.3"])        # a LANforge unit without BluForge is still shown
        self.assertEqual(sorted(self.hub.cluster.units), ["1.2", "1.3"])           # the phones are no unit at all, so nothing probes them

    def test_the_request_really_asks_the_manager_for_the_hardware_version(self):
        # the fake manager answers only the fields that were asked for, like the real one: had BluForge not asked for "hw version",
        # the phones would arrive without it, count as "cannot be classified" and be listed
        self.hub.api.resources[3].pop("hw version")
        self.hub.cluster.scan_once()
        self.assertIn("1.11", self.hub.cluster.units)                               # without the field a record is kept...
        self.hub.api.resources[3]["hw version"] = "Nothing AIN065 r16 sdk: 36"
        self.hub.cluster.scan_once()
        self.assertNotIn("1.11", self.hub.cluster.units)                            # ...and with it the phone goes again


class MergedViews(ClusterTestCase):
    def test_classic_preflight_and_results_cover_the_cluster(self):
        st, pf = self.hub.get("/api/classic/preflight")
        self.assertEqual(sorted(pf["all_hci"]), [0, 1, 2, 100, 101, 200])
        self.assertIn(101, pf["reserved"])
        self.assertTrue(any(n.startswith("1.2: ") for n in pf["notes"]))
        st, res = self.hub.get("/api/classic/results")
        files = [x["file"] for x in res["results"]]
        self.assertIn("local.json", files)
        self.assertIn("100__a2dp-1.json", files)
        self.assertEqual(files[0], "local.json")                                    # newest first (9000 > 5000)
        remote = next(x for x in res["results"] if x["file"].startswith("100__"))
        self.assertEqual((remote["hci"], remote["csv"], remote["unit"]), (101, "100__a2dp-1.csv", "1.2"))

    def test_a_result_file_of_a_resource_downloads_through_the_manager(self):
        st, hdr, raw = self.hub.live.req("GET", "/api/classic/result/100__a2dp-1.json")
        self.assertEqual(st, 200)
        self.assertIn(b"ct523c-ab1a", raw)
        self.assertIn("a2dp-1.json", hdr["Content-Disposition"])
        st, _, raw = self.hub.live.req("GET", "/api/classic/result/900__nope.json")
        self.assertEqual(st, 404)

    def test_sniff_tables_of_resources_are_included(self):
        st, d = self.hub.get("/api/sniff/devices")
        ids = sorted((s["job_id"], s["dev"]) for s in d["sniffs"])
        self.assertIn(("1.2:job1", 100), ids)
        self.assertIn(("1.3:job1", 200), ids)


class EventStream(ClusterTestCase):
    def sse(self):
        c = http.client.HTTPConnection("127.0.0.1", self.hub.live.port, timeout=10)
        c.request("GET", "/api/stream")
        r = c.getresponse()
        return c, r

    def read_until(self, r, pred, limit=30):
        for _ in range(limit):
            line = r.readline()
            if line.startswith(b"data: "):
                evt = json.loads(line[6:])
                if pred(evt):
                    return evt
        self.fail("expected event never arrived")

    def wait_stream(self, peer):
        end = time.time() + 5
        while peer.stream_clients < 1 and time.time() < end:
            time.sleep(0.05)
        self.assertGreaterEqual(peer.stream_clients, 1, "the manager never followed the resource's event stream")

    def test_console_lines_and_status_events_of_a_resource_reach_the_page_renumbered(self):
        self.hub.cluster.scan_once()
        self.wait_stream(self.p12)
        c, r = self.sse()
        try:
            hello = self.read_until(r, lambda e: e.get("event") == "hello")
            self.assertEqual(sorted(hello["status"]["all_hci"]), [0, 1, 2, 100, 101, 200])
            self.p12.events.put({"type": "line", "job_id": "job1", "dev": 1, "text": "[*] hop 19"})
            evt = self.read_until(r, lambda e: e.get("type") == "line")
            self.assertEqual((evt["job_id"], evt["dev"], evt["text"]), ("1.2:job1", 101, "[*] hop 19"))
            self.p12.events.put({"type": "status", "event": "started", "ts": 1.0,
                                 "job": {"id": "job2", "kind": "adv", "dev": 0, "hci": "hci0", "running": True}})
            evt = self.read_until(r, lambda e: e.get("event") == "started")
            self.assertEqual((evt["job"]["id"], evt["job"]["dev"], evt["job"]["unit"]), ("1.2:job2", 100, "1.2"))
            self.p13.events.put({"type": "sniff", "job_id": "job1", "dev": 0, "devices": [], "stats": None, "now": 1.0})
            evt = self.read_until(r, lambda e: e.get("type") == "sniff")
            self.assertEqual((evt["job_id"], evt["dev"]), ("1.3:job1", 200))
        finally:
            c.close()

    def test_the_resources_own_snapshot_and_bluez_chatter_are_not_forwarded(self):
        self.wait_stream(self.p12)
        c, r = self.sse()
        try:
            self.read_until(r, lambda e: e.get("event") == "hello")
            self.p12.events.put({"type": "status", "event": "hello", "status": {"all_hci": [9]}})
            self.p12.events.put({"type": "bluez", "text": "installing"})
            self.p12.events.put({"type": "line", "job_id": "job1", "dev": 1, "text": "marker"})
            seen = []
            self.read_until(r, lambda e: (seen.append(e), e.get("text") == "marker")[1])
            self.assertEqual([e["type"] for e in seen], ["line"])
        finally:
            c.close()


class Plumbing(unittest.TestCase):
    def test_resource_list_shapes(self):
        one = {"resource": {"eid": "1.1", "hostname": "a", "ctrl-ip": "1.2.3.4"}}
        self.assertEqual(cluster.parse_resources(one), [{"eid": "1.1", "host": "a", "ip": "1.2.3.4"}])
        many = {"resources": [{"1.1": {"eid": "1.1", "hostname": "a", "ctrl-ip": "1.1.1.1"}},
                              {"1.2": {"eid": "1.2", "hostname": "b", "ctrl-ip": "2.2.2.2", "phantom": True}},
                              {"1.3": {"eid": "1.3", "hostname": "c", "ctrl-ip": "3.3.3.3", "phantom": False}}]}
        self.assertEqual([r["eid"] for r in cluster.parse_resources(many)], ["1.1", "1.3"])         # phantom resources are not real
        self.assertEqual(cluster.parse_resources({"resources": {"1.1": {"eid": "1.1", "ctrl-ip": "1.1.1.1"}}})[0]["eid"], "1.1")
        self.assertEqual(cluster.parse_resources("junk"), [])
        self.assertEqual(cluster.parse_resources({}), [])

    def test_phones_and_interop_devices_are_not_cluster_resources(self):
        """The lab manager's real list: two LANforge units, a phantom and two Android phones (interop devices)."""
        def rec(eid, host, ip, hw, phantom=False):
            return {eid: {"eid": eid, "hostname": host, "ctrl-ip": ip, "hw version": hw, "phantom": phantom}}
        answer = {"resources": [rec("1.1", "ct523c-VenuMadhav-PM-b821", "192.168.206.99", "Linux/x86-64"),
                                rec("1.2", "ct523c-ab1a", "192.168.204.24", "Linux/x86-64"),
                                rec("1.6", "", "0.0.0.0", "", phantom=True),
                                rec("1.11", "NTSV-J900XHX2", "192.168.4.55", "Nothing AIN065 r16 sdk: 36"),
                                rec("1.12", "kvm-slave-build-s-system-106341", "192.168.4.60", "OPPO CPH2219 r13 sdk: 33")]}
        self.assertEqual([r["eid"] for r in cluster.parse_resources(answer)], ["1.1", "1.2"])

    def test_which_hardware_versions_count_as_a_lanforge_unit(self):
        for hw in ("Linux/x86-64", "linux/x86-64", "  Linux/x86-64 "):
            self.assertTrue(cluster.is_lanforge_unit({"hw version": hw}), hw)
        for hw in ("Nothing AIN065 r16 sdk: 36", "OPPO CPH2219 r13 sdk: 33", "Windows 11"):
            self.assertFalse(cluster.is_lanforge_unit({"hw version": hw}), hw)
        # a record that does not say what it is (an older manager, a trimmed answer) is kept, never silently dropped
        for rec in ({}, {"hw version": ""}, {"hw version": None}, {"hostname": "x"}):
            self.assertTrue(cluster.is_lanforge_unit(rec), rec)

    def test_names_and_numbers(self):
        local = cluster.Unit("1.1", 0, local=True)
        peer = cluster.Unit("1.2", 100)
        self.assertEqual(cluster.qualify_job(local, "job3"), "job3")
        self.assertEqual(cluster.qualify_job(peer, "job3"), "1.2:job3")
        self.assertEqual(cluster.qualify_job(peer, "action"), "action")
        self.assertEqual(cluster.qualify_file(peer, "x.json"), "100__x.json")
        self.assertEqual(cluster.FILE_PREFIX.match("100__x.json").groups(), ("100", "x.json"))

    def test_a_server_that_reaches_itself_by_another_address_is_not_its_own_resource(self):
        from test_server import LiveServer
        me = LiveServer()
        api = FakeManagerApi()
        api.resources = [resource("1.1", "some-other-name", "127.0.0.1")]
        c = cluster.Cluster(lambda e: None, "hubhost", server.INSTANCE, me.port, manager_api="http://127.0.0.1:%d" % api.port,
                            peer_port=me.port, scan_interval=0.3, ips_fn=lambda: set())
        try:
            c.scan_once()
            self.assertEqual(c.units, {})
            self.assertEqual(c.local.eid, "1.1")
            c.scan_once()                                  # and it stays that way
            self.assertEqual(c.units, {})
        finally:
            c.stop()
            me.close()
            api.close()

    def test_a_request_marked_local_is_answered_for_this_unit_only(self):
        from test_server import LiveServer
        port = free_port()
        peer = FakePeer("127.0.0.2", port, "ct523c-ab1a", [0, 1])
        hub = Hub([resource("1.1", "hubhost", LOCAL_IP), resource("1.2", "ct523c-ab1a", "127.0.0.2")], port)
        try:
            hub.cluster.scan_once()
            st, merged = hub.get("/api/status")
            self.assertIn(100, merged["all_hci"])
            st, plain = hub.get("/api/status", headers={cluster.LOCAL_HEADER: "1"})
            self.assertEqual(sorted(plain["all_hci"]), [0, 1, 2])
            self.assertNotIn("units", plain)
        finally:
            hub.close()
            peer.close()

    def test_without_cluster_mode_the_server_is_the_plain_single_unit_it_always_was(self):
        from test_server import LiveServer
        s = LiveServer()
        try:
            self.assertIsNone(server.CLUSTER)
            st, _, raw = s.req("GET", "/api/status")
            self.assertEqual(st, 200)
            self.assertNotIn("units", json.loads(raw))
            st, _, raw = s.req("GET", "/api/units")
            d = json.loads(raw)
            self.assertEqual((d["cluster"]["on"], len(d["units"])), (False, 1))
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
