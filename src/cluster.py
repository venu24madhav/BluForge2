"""cluster.py - one BluForge page for a whole LANforge cluster.

A LANforge *manager* knows every resource clustered with it (resource 1.1 is the manager itself, 1.2 is the next
unit, and so on) through its JSON API. BluForge on the manager asks that API which resources exist, finds the BluForge
service running on each of them (the same server.py, same port) and merges their Bluetooth radios into its own lists:

    1.1.hci0 ... 1.1.hci7   radios of the manager itself
    1.2.hci0, 1.2.hci1      radios of resource 1.2

Starting an advertisement, an interference job or a Classic action on a resource radio from the manager's page simply
forwards the request to the BluForge on that resource, which does exactly what it would do for its own page. Each unit
keeps working on its own: a resource's own BluForge page is unchanged, and a resource that goes away only loses its
radios from the manager's lists.

How a radio is numbered. The page identifies a radio by one integer. Radios of the unit the page is served from keep
their plain hci number (0..31). Every other unit gets a block of 100: its radios are base + hci number (resource 1.2 might
get 100, so its hci1 is 101). The block is handed out the first time BluForge answers on a resource and then kept, so a
running job never changes number. Job ids from a resource are prefixed with its id ("1.2:job3") for the same reason (two units both
have a "job1").

Nothing here runs on a resource: a resource has no manager JSON API, so its BluForge never looks for peers. Requests the
manager forwards carry the header X-BluForge-Local so a BluForge that did find peers would still answer for itself only."""
import copy
import http.client
import json
import re
import socket
import subprocess
import threading
import time

UNIT_STEP = 100                       # radios of unit N are base + hci index; an hci index is always below this
LOCAL_HEADER = "X-BluForge-Local"     # set on every request one BluForge sends to another: "answer for yourself only"
DEFAULT_MANAGER_API = "http://127.0.0.1:8080"
FILE_PREFIX = re.compile(r"^(\d+)__(.+)$")   # a result file of a resource is listed as "<base>__<name>"

# how long each kind of forwarded request may take (Classic actions can legitimately run for minutes)
TIMEOUTS = {"/api/classic/action": 200.0, "/api/stop_all": 60.0, "/api/stop": 25.0}
DEFAULT_TIMEOUT = 15.0


# ---------------------------------------------------------------- small helpers
_IPS = {"at": 0.0, "ips": set()}


def local_ips():
    """IPv4 addresses of this machine (cached for a minute). `ip` sees addresses inside a VRF, which a bind() test does not."""
    now = time.time()
    if now - _IPS["at"] < 60 and _IPS["ips"]:
        return _IPS["ips"]
    ips = {"127.0.0.1"}
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=3).stdout
        ips |= set(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out))
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        ips |= set(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    _IPS.update(at=now, ips=ips)
    return ips


def parse_resources(data):
    """The manager's /resource/all answer -> [{"eid", "host", "ip"}] for every real (non-phantom) resource."""
    if not isinstance(data, dict):
        return []
    items = data.get("resources")
    if items is None and isinstance(data.get("resource"), dict):
        items = [data["resource"]]
    if isinstance(items, dict):
        items = [items]
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        recs = [it] if "eid" in it else [v for v in it.values() if isinstance(v, dict)]     # {"1.2": {...}} or the record itself
        for r in recs:
            eid = str(r.get("eid") or "").strip()
            if not eid or r.get("phantom") in (True, "true", "TRUE"):
                continue
            out.append({"eid": eid, "host": str(r.get("hostname") or ""), "ip": str(r.get("ctrl-ip") or "").strip()})
    return out


def eid_key(eid):
    """Sort key so that 1.2 comes before 1.10."""
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in str(eid).split(".")]


def qualify_job(unit, job_id):
    """A resource's job id, made unique across the cluster ('action' is the shared name of one-shot Classic lines)."""
    return job_id if unit.local or job_id in (None, "action") else "%s:%s" % (unit.eid, job_id)


def qualify_file(unit, name):
    return name if unit.local or not name else "%d__%s" % (unit.base, name)


# ---------------------------------------------------------------- one unit
class Unit:
    def __init__(self, eid, base, local=False, host="", ip="", port=0):
        self.eid = eid
        self.base = base
        self.local = local
        self.host = host
        self.ip = ip
        self.port = port
        self.state = "local" if local else "probing"      # local | online | offline | no-bluforge | probing
        self.error = ""
        self.version = None
        self.instance = None
        self.fails = 0
        self.last_ok = None
        self.next_probe = 0.0
        self.status = None              # last /api/status body, in the unit's own (unshifted) numbering
        self.status_at = 0.0
        self.caps = None
        self.caps_at = 0.0
        self.stream_gen = 0             # a new number retires the previous event-stream thread
        self._stream_alive = False
        self._announced = None

    @property
    def online(self):
        return self.local or (self.state == "online" and self.base is not None)

    def info(self):
        st = self.status or {}
        return {"eid": self.eid, "host": self.host, "ip": self.ip, "port": self.port, "base": self.base, "local": self.local,
                "state": self.state, "error": self.error, "version": self.version,
                "radios": len(st.get("all_hci") or []) if (self.online and st) else None,
                "bluez": (st.get("bluez") or {}).get("installed") if st else None}


# ---------------------------------------------------------------- the cluster
class Cluster:
    def __init__(self, publish, hostname, instance, port, manager_api=DEFAULT_MANAGER_API,
                 static_peers=(), peer_port=None, scan_interval=10.0, probe_timeout=1.5, call_timeout=DEFAULT_TIMEOUT,
                 ips_fn=local_ips):
        self.publish = publish                  # JOBS.publish: events from a resource join the page's own event stream
        self.hostname = hostname
        self.instance = instance                # random id of this server process: a probe that finds it is finding itself
        self.port = port
        self.peer_port = peer_port or port
        self.manager_api = (manager_api or "").rstrip("/")
        self.static = list(static_peers)        # [(eid, host, port)]
        self.scan_interval = scan_interval
        self.probe_timeout = probe_timeout
        self.call_timeout = call_timeout
        self.ips_fn = ips_fn
        self.lock = threading.RLock()
        self.local = Unit("", 0, local=True, host=hostname)
        self.units = {}                         # eid -> Unit (resources only)
        self._bases = {}                        # eid -> block base, kept for the life of the process
        self._self_eids = set()                 # resource ids that turned out to be this very server (found by instance id)
        self.manager_ok = None
        self.manager_error = ""
        self.last_scan = 0.0
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------ lifecycle
    def start(self):
        self._thread = threading.Thread(target=self._loop, name="bluforge-cluster", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self.lock:
            for u in self.units.values():
                u.stream_gen += 1

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as e:              # discovery must never take the page down
                self.manager_error = "scan failed: %s" % e
            self._stop.wait(self.scan_interval)

    # ------------------------------------------------------------ discovery
    def base_for(self, eid):
        with self.lock:
            if eid not in self._bases:
                used = set(self._bases.values())
                b = UNIT_STEP
                while b in used:
                    b += UNIT_STEP
                self._bases[eid] = b
            return self._bases[eid]

    def _read_resources(self):
        """The manager's resource list, or None when its JSON API cannot be reached (static peers still work)."""
        if not self.manager_api:
            return None
        try:
            u = self.manager_api.split("://", 1)[-1]
            host, _, port = u.partition("/")[0].partition(":")
            c = http.client.HTTPConnection(host, int(port or 80), timeout=3.0)
            # the LANforge JSON API answers HTTP 500 to a request that has no Accept header
            c.request("GET", "/resource/all?fields=eid,hostname,ctrl-ip,phantom", headers={"Accept": "application/json"})
            r = c.getresponse()
            raw = r.read()
            c.close()
            if r.status != 200:
                raise RuntimeError("HTTP %d" % r.status)
            self.manager_ok, self.manager_error = True, ""
            return parse_resources(json.loads(raw))
        except Exception as e:
            self.manager_ok, self.manager_error = False, "%s: %s" % (self.manager_api, e)
            return None

    def scan_once(self):
        """One pass: refresh the resource list, probe the resources, start/stop event streams. Safe to call from tests."""
        res = self._read_resources()
        if res is None:
            res = []
        known = {r["eid"] for r in res}
        for eid, host, port in self.static:
            if eid not in known:
                res.append({"eid": eid, "host": host, "ip": host, "port": port})
        ips = self.ips_fn()
        changed = False
        local_found = False
        peers = []
        for r in res:
            if not local_found and (r["eid"] in self._self_eids or (r["ip"] and r["ip"] in ips) or (r["host"] and r["host"] == self.hostname)):
                local_found = True
                if (self.local.eid, self.local.ip) != (r["eid"], r["ip"]):
                    self.local.eid, self.local.ip = r["eid"], r["ip"]
                    changed = True
                continue
            if r["eid"] in self._self_eids:
                continue
            peers.append(r)
        with self.lock:
            live = {r["eid"] for r in peers}
            for eid in [e for e in self.units if e not in live]:     # a resource the manager no longer lists
                self.units[eid].stream_gen += 1
                del self.units[eid]
                changed = True
            for r in peers:
                u = self.units.get(r["eid"])
                if u is None:
                    u = self.units[r["eid"]] = Unit(r["eid"], None)      # its block of radio numbers comes when BluForge first answers there
                u.host = r["host"] or u.host
                u.ip = r["ip"]
                u.port = int(r.get("port") or self.peer_port)
        now = time.time()
        due = [u for u in list(self.units.values()) if now >= u.next_probe]
        found = {}

        def run(u):
            found[u.eid] = self._probe_request(u)
        threads = [threading.Thread(target=run, args=(u,), daemon=True) for u in due]
        for t in threads:
            t.start()
        for t in threads:
            t.join(self.probe_timeout + 2.0)
        for u in sorted(due, key=lambda x: eid_key(x.eid)):
            if u.eid in found:
                self._apply_probe(u, *found[u.eid])
            else:                                               # the probe did not even return in time
                self._apply_probe(u, "err", "no answer within %.0f s" % (self.probe_timeout + 2.0))
        for u in list(self.units.values()):
            if u.online:
                self._ensure_stream(u)
        self.last_scan = time.time()
        if changed or any(u.state != u._announced for u in self.units.values()):
            for u in self.units.values():
                u._announced = u.state
            self.publish({"type": "status", "event": "units", "ts": time.time()})

    def _probe_request(self, u):
        """Ask a resource whether it runs BluForge. ("ok", health) or ("err", message). Runs in a thread per resource."""
        try:
            st, _, raw = self._get(u, "/api/health", self.probe_timeout)
            h = json.loads(raw)
            if st != 200 or not isinstance(h, dict) or "version" not in h:
                raise RuntimeError("not a BluForge answer (HTTP %d)" % st)
            return "ok", h
        except Exception as e:
            return "err", str(e)

    def _apply_probe(self, u, kind, data):
        """Record what a probe found. Called one resource at a time, in resource order, so radio-number blocks are handed out
        in a fixed order and a resource is never seen as online before it has its block."""
        was = u.state
        if kind == "err":
            u.fails += 1
            u.error = data
            u.status = None
            u.state = "offline" if u.last_ok else "no-bluforge"
            if u.state == "no-bluforge" and u.fails > 3:
                u.next_probe = time.time() + 60.0               # a PC that will never run BluForge: stop knocking every pass
            else:
                u.next_probe = time.time() + self.scan_interval
            if was == "online":
                u.stream_gen += 1
            return
        h = data
        if h.get("instance") and h.get("instance") == self.instance:
            # this very server, reached through another address of the manager: it is the local unit, not a peer
            with self.lock:
                self.local.eid, self.local.ip = u.eid, u.ip
                self._self_eids.add(u.eid)
                self.units.pop(u.eid, None)
            return
        if u.base is None:
            u.base = self.base_for(u.eid)
        u.fails = 0
        u.error = ""
        u.version = h.get("version")
        u.instance = h.get("instance")
        u.host = h.get("host") or u.host
        u.last_ok = time.time()
        u.next_probe = time.time() + self.scan_interval
        u.state = "online"                                      # last: readers that see "online" also see the base

    # ------------------------------------------------------------ talking to a unit
    def _get(self, u, path, timeout):
        return self.call(u, "GET", path, None, timeout)

    def call(self, u, method, path, body=None, timeout=None):
        """One request to a resource's BluForge. Returns (status, headers, raw). Raises OSError-family errors if it cannot be reached."""
        headers = {LOCAL_HEADER: "1"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        c = http.client.HTTPConnection(u.ip, u.port, timeout=timeout or self.call_timeout)
        try:
            c.request(method, path, data, headers)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def _json_call(self, u, method, path, body=None, timeout=None):
        st, _, raw = self.call(u, method, path, body, timeout)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {"ok": False, "error": "resource %s sent an unreadable answer" % u.eid}
        return st, data

    def mark_offline(self, u, err):
        if u.state == "online":
            u.state = "offline"
            u.error = str(err)
            u.status = None
            u.stream_gen += 1
            u.next_probe = 0.0
            self.publish({"type": "status", "event": "units", "ts": time.time()})

    def peer_status(self, u, max_age=1.0):
        if u.status is not None and time.time() - u.status_at < max_age:
            return u.status
        try:
            st, data = self._json_call(u, "GET", "/api/status", None, 2.5)
            if st != 200 or not isinstance(data, dict):
                raise RuntimeError("HTTP %d" % st)
        except Exception as e:
            self.mark_offline(u, e)
            return None
        u.status, u.status_at = data, time.time()
        return data

    def peer_caps(self, u):
        if u.caps is not None and time.time() - u.caps_at < 60:
            return u.caps
        try:
            st, data = self._json_call(u, "GET", "/api/caps", None, 8.0)
            if st != 200 or not isinstance(data, dict) or "error" in data:
                return u.caps
        except Exception as e:
            self.mark_offline(u, e)
            return None
        u.caps, u.caps_at = data, time.time()
        return data

    def _gather(self, fn, units):
        """fn(unit) for every unit at once (a slow resource must not hold up the others); {eid: result}."""
        out = {}

        def run(u):
            try:
                out[u.eid] = fn(u)
            except Exception:
                out[u.eid] = None
        threads = [threading.Thread(target=run, args=(u,), daemon=True) for u in units]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10.0)
        return out

    # ------------------------------------------------------------ the event stream of a resource
    def _ensure_stream(self, u):
        with self.lock:
            if u._stream_alive:
                return
            u.stream_gen += 1
            gen = u.stream_gen
            u._stream_alive = True
        threading.Thread(target=self._stream_loop, args=(u, gen), name="bluforge-stream-%s" % u.eid, daemon=True).start()

    def _stream_loop(self, u, gen):
        backoff = 1.0
        try:
            while not self._stop.is_set() and u.stream_gen == gen:
                c = None
                try:
                    c = http.client.HTTPConnection(u.ip, u.port, timeout=45)
                    c.request("GET", "/api/stream?live=1", headers={LOCAL_HEADER: "1", "Accept": "text/event-stream"})
                    r = c.getresponse()
                    if r.status != 200:
                        raise RuntimeError("HTTP %d" % r.status)
                    backoff = 1.0
                    u.status_at = 0.0
                    self.publish({"type": "status", "event": "units", "ts": time.time()})
                    while u.stream_gen == gen and not self._stop.is_set():
                        line = r.readline()
                        if not line:
                            raise EOFError("event stream closed")
                        if not line.startswith(b"data: "):
                            continue                              # keep-alive comment or blank line
                        try:
                            evt = json.loads(line[6:])
                        except ValueError:
                            continue
                        if evt.get("type") == "status":
                            u.status_at = 0.0                     # the next status request must ask the resource again
                        out = self.rw_event(evt, u)
                        if out is not None:
                            self.publish(out)
                except Exception as e:
                    if u.stream_gen == gen:
                        self.mark_offline(u, e)
                finally:
                    if c is not None:
                        c.close()
                if u.stream_gen != gen:
                    break
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 10.0)
        finally:
            u._stream_alive = False

    # ------------------------------------------------------------ numbering and names
    @property
    def on(self):
        """True once at least one resource runs BluForge (then radio names carry the unit id)."""
        return any(u.state in ("online", "offline") for u in self.units.values())

    def unit_label(self, u):
        return u.eid or "local"

    def label(self, u, dev):
        return "%s.hci%d" % (self.unit_label(u), dev) if self.on else "hci%d" % dev

    def unit_of(self, gdev):
        """(unit, local hci index) for a radio number as the page uses it; (None, None) if no such unit."""
        try:
            gdev = int(gdev)
        except (TypeError, ValueError):
            return None, None
        base = (gdev // UNIT_STEP) * UNIT_STEP
        if base == 0:
            return self.local, gdev
        with self.lock:
            for u in self.units.values():
                if u.base == base:
                    return u, gdev - base
        return None, None

    def rw_job(self, j, u):
        o = dict(j)
        if isinstance(j.get("dev"), int):
            o["dev"] = u.base + j["dev"]
            o["hci"] = "hci%d" % j["dev"]
            o["label"] = self.label(u, j["dev"])
        o["id"] = qualify_job(u, j.get("id"))
        o["unit"] = u.eid
        return o

    def rw_event(self, evt, u):
        """An event from a resource's stream, renumbered for the page; None = do not forward."""
        t = evt.get("type")
        if t in ("line", "sniff"):
            o = dict(evt)
            o["job_id"] = qualify_job(u, evt.get("job_id"))
            if isinstance(evt.get("dev"), int):
                o["dev"] = u.base + evt["dev"]
            return o
        if t == "status":
            if evt.get("event") == "hello":
                return None                                       # the resource's own snapshot; the page asks for the merged one
            o = dict(evt)
            if isinstance(evt.get("job"), dict):
                o["job"] = self.rw_job(evt["job"], u)
            return o
        return None                                               # bluez install chatter etc. stays on its own unit

    # ------------------------------------------------------------ merging what the page asks for
    def _peers_online(self):
        with self.lock:
            return sorted((u for u in self.units.values() if u.online), key=lambda u: u.base)

    def merge_status(self, snap):
        """The page's /api/status: this unit's snapshot plus every online resource's, in the page's numbering."""
        out = dict(snap)
        peers = self._peers_online()
        got = self._gather(self.peer_status, peers)
        on = self.on
        local_job = lambda j: dict(j, unit=self.local.eid, label=self.label(self.local, j["dev"]), id=j["id"])   # noqa: E731
        running = [local_job(j) for j in snap.get("running", [])]
        finished = [local_job(j) for j in snap.get("finished", [])]
        all_hci = list(snap.get("all_hci", []))
        available = list(snap.get("available_hci", []))
        reserved = list(snap.get("classic_reserved", []))
        labels = {str(d): self.label(self.local, d) for d in all_hci}
        warnings = list((snap.get("health") or {}).get("warnings", []))
        for u in peers:
            st = got.get(u.eid)
            if not st:
                continue
            running += [self.rw_job(j, u) for j in st.get("running", [])]
            finished += [self.rw_job(j, u) for j in st.get("finished", [])]
            all_hci += [u.base + d for d in st.get("all_hci", [])]
            available += [u.base + d for d in st.get("available_hci", [])]
            reserved += [u.base + d for d in st.get("classic_reserved", [])]
            labels.update({str(u.base + d): self.label(u, d) for d in st.get("all_hci", [])})
            for w in (st.get("health") or {}).get("warnings", []):
                warnings.append("%s: %s" % (u.eid, w))
            if st.get("bluez") and not st["bluez"].get("installed"):
                warnings.append("%s: BlueZ is not installed on that unit, so its radios are listed but cannot run jobs." % u.eid)
        with self.lock:
            offline = [u for u in self.units.values() if u.state == "offline"]
        for u in offline:
            warnings.append("Resource %s (%s) stopped answering (%s). Its radios are hidden until it is back." % (u.eid, u.host, u.error))
        finished.sort(key=lambda j: j.get("started") or 0)
        out.update(running=running, finished=finished[-40:], all_hci=all_hci, available_hci=available,
                   classic_reserved=reserved, labels=labels, cluster=self.summary())
        out["units"] = [self.local_info(snap)] + [u.info() for u in self._known_units()]
        out["other_resources"] = self.others()
        if warnings:
            out["health"] = dict(snap.get("health") or {}, ok=False, warnings=warnings)
        return out

    def local_info(self, snap):
        """This unit's row for the units list; snap is this unit's own status snapshot."""
        return {"eid": self.local.eid, "host": self.hostname, "ip": self.local.ip, "port": self.port, "base": 0, "local": True,
                "state": "local", "error": "", "version": None, "radios": len(snap.get("all_hci") or []),
                "bluez": (snap.get("bluez") or {}).get("installed")}

    def _known_units(self):
        with self.lock:
            return sorted((u for u in self.units.values() if u.state in ("online", "offline")), key=lambda u: u.base)

    def others(self):
        with self.lock:
            return [{"eid": u.eid, "host": u.host, "ip": u.ip, "port": u.port,
                     "note": "BluForge did not answer on port %d" % u.port}
                    for u in sorted(self.units.values(), key=lambda u: eid_key(u.eid)) if u.state in ("no-bluforge", "probing")]

    def summary(self):
        return {"on": self.on, "manager_api": self.manager_api, "manager_ok": self.manager_ok, "manager_error": self.manager_error,
                "local": self.local.eid, "last_scan": self.last_scan}

    def units_payload(self, local_snap):
        return {"cluster": self.summary(), "units": [self.local_info(local_snap)] + [u.info() for u in self._known_units()],
                "other_resources": self.others()}

    def merge_caps(self, caps):
        if not isinstance(caps, dict) or "error" in caps or not self.on:
            return caps
        out = copy.deepcopy(caps)
        hw = out.setdefault("hardware", {})
        local_hci = list(hw.get("all_hci", []))
        all_hci = list(local_hci)
        units = [{"eid": self.local.eid, "host": self.hostname, "base": 0, "all_hci": local_hci}]
        peers = self._peers_online()
        got = self._gather(self.peer_caps, peers)
        for u in peers:
            pc = got.get(u.eid)
            if not pc:
                continue
            mine = (pc.get("hardware") or {}).get("all_hci", [])
            all_hci += [u.base + d for d in mine]
            units.append({"eid": u.eid, "host": u.host, "base": u.base, "all_hci": list(mine)})
        hw["all_hci"] = all_hci
        hw["units"] = units
        return out

    def merge_preflight(self, pf):
        out = dict(pf)
        peers = self._peers_online()
        got = self._gather(lambda u: self._json_call(u, "GET", "/api/classic/preflight", None, 20.0)[1], peers)
        notes = list(out.get("notes") or [])
        reserved = list(out.get("reserved") or [])
        all_hci = list(out.get("all_hci") or [])
        for u in peers:
            p = got.get(u.eid)
            if not isinstance(p, dict):
                continue
            reserved += [u.base + d for d in p.get("reserved") or []]
            all_hci += [u.base + d for d in p.get("all_hci") or []]
            if p.get("ok") is False and p.get("error"):
                notes.append("%s: %s" % (u.eid, p["error"]))
            notes += ["%s: %s" % (u.eid, n) for n in p.get("notes") or [] if isinstance(n, str)]
        out.update(notes=notes, reserved=reserved, all_hci=all_hci)
        return out

    def merge_results(self, results, limit=40):
        peers = self._peers_online()
        got = self._gather(lambda u: self._json_call(u, "GET", "/api/classic/results", None, 8.0)[1], peers)
        merged = list(results)
        for u in peers:
            r = got.get(u.eid)
            for x in (r or {}).get("results", []) if isinstance(r, dict) else []:
                y = dict(x)
                y["file"] = qualify_file(u, x.get("file"))
                if x.get("csv"):
                    y["csv"] = qualify_file(u, x["csv"])
                if isinstance(x.get("hci"), int):
                    y["hci"] = u.base + x["hci"]
                y["unit"] = u.eid
                merged.append(y)
        merged.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
        return merged[:limit]

    def merge_captures(self, captures, limit=100):
        """The packet captures of this unit plus those of every online resource ('<base>__<file>', radio numbers renumbered)."""
        peers = self._peers_online()
        got = self._gather(lambda u: self._json_call(u, "GET", "/api/sniff/captures", None, 8.0)[1], peers)
        merged = list(captures)
        for u in peers:
            r = got.get(u.eid)
            for x in (r or {}).get("captures", []) if isinstance(r, dict) else []:
                y = dict(x)
                y["file"] = qualify_file(u, x.get("file"))
                if isinstance(x.get("hci"), int):
                    y["hci"] = u.base + x["hci"]
                y["unit"] = u.eid
                merged.append(y)
        merged.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
        return merged[:limit]

    def merge_sniffs(self, snap):
        peers = self._peers_online()
        got = self._gather(lambda u: self._json_call(u, "GET", "/api/sniff/devices", None, 8.0)[1], peers)
        sniffs = list(snap.get("sniffs", []))
        for u in peers:
            r = got.get(u.eid)
            for s in (r or {}).get("sniffs", []) if isinstance(r, dict) else []:
                y = dict(s)
                y["job_id"] = qualify_job(u, s.get("job_id"))
                if isinstance(s.get("dev"), int):
                    y["dev"] = u.base + s["dev"]
                sniffs.append(y)
        return dict(snap, sniffs=sniffs)

    # ------------------------------------------------------------ sending the page's requests to the right unit
    def _unreachable(self, u, e):
        self.mark_offline(u, e)
        return 502, {"ok": False, "error": "resource %s (%s) did not answer: %s" % (u.eid, u.host, e)}

    def route_post(self, path, body):
        """None = this unit handles the request itself; otherwise (status, json) already produced by the owning resource."""
        if path in ("/api/start", "/api/classic/reserve", "/api/classic/action"):
            try:
                int(body.get("dev"))
            except (TypeError, ValueError):
                return None                                           # let the local handler refuse it exactly as before
            u, local = self.unit_of(body.get("dev"))
            if u is None:
                return 404, {"ok": False, "error": "no radio %r in this cluster" % (body.get("dev"),)}
            if u.local:
                if path == "/api/classic/action" and str(body.get("action")) == "link-local":
                    pu, _ = self.unit_of((body.get("params") or {}).get("peer"))
                    if pu is not self.local:
                        return 400, {"ok": False, "error": "the receiver must be a radio of the same unit as the sender"}
                return None
            if not u.online:
                return 503, {"ok": False, "error": "resource %s (%s) is offline" % (u.eid, u.host)}
            fwd = dict(body, dev=local)
            if path == "/api/classic/action" and str(body.get("action")) == "link-local":
                params = dict(body.get("params") or {})
                pu, plocal = self.unit_of(params.get("peer"))
                if pu is not u:
                    return 400, {"ok": False, "error": "the receiver must be a radio of the same unit as the sender (%s)" % u.eid}
                params["peer"] = plocal
                fwd["params"] = params
            try:
                st, data = self._json_call(u, "POST", path, fwd, TIMEOUTS.get(path, self.call_timeout))
            except Exception as e:
                return self._unreachable(u, e)
            if isinstance(data, dict):
                if data.get("job_id"):
                    data["job_id"] = qualify_job(u, data["job_id"])
                if isinstance(data.get("reserved"), list):
                    data["reserved"] = [u.base + d for d in data["reserved"] if isinstance(d, int)]
                u.status_at = 0.0
            return st, data
        if path == "/api/stop":
            jid = str(body.get("job_id") or "")
            if ":" not in jid:
                return None                                       # one of this unit's own jobs
            eid, _, local_id = jid.partition(":")
            with self.lock:
                u = self.units.get(eid)
            if u is None:
                return 404, {"ok": False, "error": "no such job %r" % jid}
            try:
                st, data = self._json_call(u, "POST", path, dict(body, job_id=local_id), TIMEOUTS[path])
            except Exception as e:
                return self._unreachable(u, e)
            u.status_at = 0.0
            return st, data
        return None

    def stop_all_peers(self):
        """Stop every job on every online resource. Returns [(unit id, message)] for the ones that did not stop."""
        peers = self._peers_online()

        def one(u):
            try:
                st, data = self._json_call(u, "POST", "/api/stop_all", {}, TIMEOUTS["/api/stop_all"])
            except Exception as e:
                return "did not answer: %s" % e
            u.status_at = 0.0
            return None if st == 200 else (data.get("error") if isinstance(data, dict) else None) or "HTTP %d" % st
        got = self._gather(one, peers)
        return [(u.eid, got.get(u.eid)) for u in peers if got.get(u.eid)]

    def fetch_capture(self, name):
        """A sniff capture of a resource, listed as '<base>__<file>'. (status, headers, body) or None if not a resource file."""
        m = FILE_PREFIX.match(name or "")
        if not m:
            return None
        base = int(m.group(1))
        with self.lock:
            u = next((x for x in self.units.values() if x.base == base), None)
        if u is None or not u.online:
            return 404, {}, b'{"error": "that resource is not available"}'
        try:
            return self.call(u, "GET", "/api/sniff/capture/" + m.group(2), None, 120.0)
        except Exception as e:
            self.mark_offline(u, e)
            return 502, {}, json.dumps({"error": "resource %s did not answer: %s" % (u.eid, e)}).encode()

    def fetch_result(self, name):
        """A saved Classic result of a resource, listed as '<base>__<file>'. (status, headers, body) or None if not a resource file."""
        m = FILE_PREFIX.match(name or "")
        if not m:
            return None
        base = int(m.group(1))
        with self.lock:
            u = next((x for x in self.units.values() if x.base == base), None)
        if u is None or not u.online:
            return 404, {}, b'{"error": "that resource is not available"}'
        try:
            return self.call(u, "GET", "/api/classic/result/" + m.group(2), None, 20.0)
        except Exception as e:
            self.mark_offline(u, e)
            return 502, {}, json.dumps({"error": "resource %s did not answer: %s" % (u.eid, e)}).encode()
