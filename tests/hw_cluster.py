#!/usr/bin/env python3
"""Hardware check of cluster mode on ONE machine (run as root; not part of run_tests.sh).

Two real BluForge servers run here and talk over loopback, exactly as a LANforge manager and a resource would:
  * the "resource"  on 127.0.0.2:PORT_R   (cluster mode off)
  * the "manager"   on 127.0.0.1:PORT_M   (cluster mode on, with the resource named as unit 9.9 and the real manager API for the rest)
A short advertisement is started on a radio of the "resource" THROUGH the manager, its console lines are followed on the
manager's event stream, and it is stopped again. Then the same for a radio of the manager itself.

It uses two radios that are idle: pass them with --resource-radio and --manager-radio. A radio that already has a BluForge job
is refused. Both servers are stopped (SIGINT, so they stop their jobs cleanly) at the end.

usage: sudo python3 hw_cluster.py [--resource-radio 7] [--manager-radio 6]"""
import argparse
import http.client
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "src", "server.py")
PORT_M, PORT_R = 5490, 5491
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  - " + str(detail)) if detail else ""), flush=True)


def call(host, port, method, path, body=None, timeout=30):
    c = http.client.HTTPConnection(host, port, timeout=timeout)
    h = {"Content-Type": "application/json"} if body is not None else {}
    c.request(method, path, json.dumps(body).encode() if body is not None else None, h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, (json.loads(raw) if raw else {})


def busy(dev):
    out = subprocess.run(["pgrep", "-af", r"bluforge_(cli|classic)\.py.*--(dev|hci) %d( |$)" % dev], capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if "pgrep" not in l]


def start(host, port, extra, logs):
    env = dict(os.environ, BLUFORGE_LOGS=logs, PYTHONUNBUFFERED="1")
    return subprocess.Popen([sys.executable, "-u", SERVER, "--host", host, "--port", str(port)] + extra, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def follow(port, sink, stop):
    """Collect events from the manager's /api/stream into sink."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        c.request("GET", "/api/stream")
        r = c.getresponse()
        while not stop.is_set():
            line = r.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                sink.append(json.loads(line[6:]))
    except Exception:
        pass


def wait(pred, secs, step=0.5):
    end = time.time() + secs
    while time.time() < end:
        try:
            v = pred()
        except (OSError, ValueError, KeyError):      # a server that is still starting up is "not yet", not a failure
            v = None
        if v:
            return v
        time.sleep(step)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resource-radio", type=int, default=7)
    ap.add_argument("--manager-radio", type=int, default=6)
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("run as root (raw HCI)")
    for d in (a.resource_radio, a.manager_radio):
        if busy(d):
            sys.exit("hci%d already has a BluForge job (%s) - pick another radio" % (d, busy(d)[0][:100]))
    tmp = tempfile.mkdtemp(prefix="bf-cluster-")
    procs = []
    try:
        print("starting the resource (127.0.0.2:%d) and the manager (127.0.0.1:%d)" % (PORT_R, PORT_M))
        procs.append(start("127.0.0.2", PORT_R, ["--cluster", "off"], tmp + "/res"))
        time.sleep(1.5)
        procs.append(start("127.0.0.1", PORT_M, ["--cluster", "auto", "--scan-interval", "2", "--peer-port", str(PORT_R),
                                                 "--peer", "9.9=127.0.0.2:%d" % PORT_R], tmp + "/mgr"))
        unit = wait(lambda: next((u for u in call("127.0.0.1", PORT_M, "GET", "/api/units")[1].get("units", [])
                                  if u["eid"] == "9.9" and u["state"] == "online"), None), 40)
        check("the manager finds the resource and reports it online", unit is not None)
        if not unit:
            return
        st, s = call("127.0.0.1", PORT_M, "GET", "/api/status")
        mine = [h for h in s["all_hci"] if h < 100]
        theirs = [h for h in s["all_hci"] if h >= 100]
        print("  manager radios:", [s["labels"][str(h)] for h in mine])
        print("  resource radios:", [s["labels"][str(h)] for h in theirs])
        check("radios of both units are in one list with unit names", bool(mine) and bool(theirs) and s["labels"][str(theirs[0])].startswith("9.9.hci"))
        check("the manager's own radios carry the manager's resource id from the real LANforge manager API",
              s["labels"][str(mine[0])] == "1.1.hci%d" % mine[0], s["labels"][str(mine[0])])
        others = [o["eid"] for o in s.get("other_resources", [])]
        print("  other resources in the real cluster without BluForge:", others)

        events, stop = [], threading.Event()
        threading.Thread(target=follow, args=(PORT_M, events, stop), daemon=True).start()
        time.sleep(1)

        base = unit["base"]
        dev = base + a.resource_radio
        print("advertisement on the RESOURCE's %s, started from the manager" % s["labels"].get(str(dev), dev))
        st, r = call("127.0.0.1", PORT_M, "POST", "/api/start", {"tool": "adv", "dev": dev, "params": {
            "type": "nonconn", "interval": 100, "name": "BF-cluster", "duration": 5}})
        check("start on a resource radio is accepted and its job id carries the unit", st == 200 and r.get("job_id", "").startswith("9.9:job"), r)
        jid = r.get("job_id")
        run = wait(lambda: next((j for j in call("127.0.0.1", PORT_M, "GET", "/api/status")[1]["running"] if j["id"] == jid), None), 10)
        check("the manager's status lists that job as running on the resource's radio", bool(run) and run["dev"] == dev and run["unit"] == "9.9", run)
        done = wait(lambda: next((j for j in call("127.0.0.1", PORT_M, "GET", "/api/status")[1]["finished"] if j["id"] == jid), None), 25)
        check("the job finishes by itself and exits cleanly", bool(done) and done["exit_code"] == 0, done and done.get("exit_code"))
        lines = [e for e in events if e.get("type") == "line" and e.get("job_id") == jid]
        check("its console output reached the manager's event stream, renumbered", len(lines) > 0 and all(e["dev"] == dev for e in lines), "%d lines" % len(lines))
        seen = lambda ev: any(e.get("event") == ev and e.get("job", {}).get("id") == jid for e in events)   # noqa: E731
        check("started and finished events arrived", bool(wait(lambda: seen("started") and seen("finished"), 6)))

        print("advertisement on the resource's radio, then stopped from the manager")
        st, r = call("127.0.0.1", PORT_M, "POST", "/api/start", {"tool": "adv", "dev": dev, "params": {"type": "nonconn", "interval": 100, "name": "BF-stop"}})
        jid2 = r.get("job_id")
        wait(lambda: next((j for j in call("127.0.0.1", PORT_M, "GET", "/api/status")[1]["running"] if j["id"] == jid2), None), 10)
        st, r = call("127.0.0.1", PORT_M, "POST", "/api/stop", {"job_id": jid2})
        check("stop reaches the resource and the job ends", st == 200 and r.get("ok") is True, r)
        gone = wait(lambda: not any(j["id"] == jid2 for j in call("127.0.0.1", PORT_M, "GET", "/api/status")[1]["running"]), 15)
        check("it is no longer running", bool(gone))

        print("advertisement on the MANAGER's own hci%d" % a.manager_radio)
        st, r = call("127.0.0.1", PORT_M, "POST", "/api/start", {"tool": "adv", "dev": a.manager_radio, "params": {
            "type": "nonconn", "interval": 100, "name": "BF-local", "duration": 3}})
        check("a radio of the manager itself still works", st == 200 and r.get("job_id", "").startswith("job"), r)
        jid3 = r.get("job_id")
        done = wait(lambda: next((j for j in call("127.0.0.1", PORT_M, "GET", "/api/status")[1]["finished"] if j["id"] == jid3), None), 25)
        check("and finishes cleanly", bool(done) and done["exit_code"] == 0)

        st, r = call("127.0.0.1", PORT_M, "POST", "/api/start", {"tool": "adv", "dev": 500, "params": {}})
        check("a radio number no unit owns is refused by the manager", st == 404, r)
        st, r = call("127.0.0.1", PORT_M, "POST", "/api/start", {"tool": "adv", "dev": base + 50, "params": {}})
        check("a radio number inside a unit's block that unit does not have is refused by that unit", st == 409 and "not a controller" in r.get("msg", ""), r.get("msg"))
        stop.set()
    finally:
        for p in procs:
            try:
                p.send_signal(signal.SIGINT)
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=15)
            except Exception:
                p.kill()
        print("servers stopped")
    bad = [n for n, ok in results if not ok]
    print("\n%d checks, %d failed%s" % (len(results), len(bad), (": " + "; ".join(bad)) if bad else ""))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
