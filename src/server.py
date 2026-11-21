#!/usr/bin/env python3
"""
server.py - Web GUI backend for the Ericsson BLE demo on this LANforge unit.

Thin control plane over bluforge_cli.py: reads capabilities, starts/stops
`adv` / `interfere` / `sniff` jobs as separate managed child processes (one
per BE200 Bluetooth controller, hci0-7), and streams their console output to
the browser over Server-Sent Events (SSE).

Unlike a single-radio lab box, this unit has 8 independent BE200 Bluetooth
controllers, so MULTIPLE jobs run concurrently (one per controller) rather
than one global job. Stopping one job only ever touches its own controller
process (SIGINT, falling back to SIGTERM) -- never SIGKILL from here, since
an abrupt kill was found to leave the kernel's own HCI_Reset-on-close timing
out (see docs/INSTALL_LOG.md, 2026-10-05). The server itself holds no HCI
sockets and talks to no Bluetooth hardware directly; only bluforge_cli.py
subprocesses do, each in its own process so one job's crash can't affect
another's controller.

Pure Python stdlib (http.server + threading): nothing to install, nothing to
replicate on another unit beyond copying this project folder. Serves the
LANforge-styled gui.html from the same directory.

Clustered LANforge: when this server runs on a LANforge *manager* it asks the manager's JSON API which resources are
clustered with it, finds the BluForge on each (the same server.py on the same port), and shows their radios beside its own
(1.1.hci0 ... 1.2.hci1). Jobs for a resource radio are forwarded to that resource. See cluster.py and docs/CLUSTER.md.

There is no login: anyone who can reach the port can use the page and the API (see docs/ACCESS_CONTROL.md).

Run as root (bluforge_cli.py needs raw HCI):  python3 server.py [--port 5480]
"""
import argparse
import json
import secrets
from version import __version__, RESULT_SCHEMA
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from hci_raw import detect_hci_indices
import bt_traffic
import cluster as clustermod
from cluster import LOCAL_HEADER

CLUSTER = None         # set in main(): a cluster.Cluster when this server should also show its resources' radios

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "bluforge_cli.py")
CLASSIC = os.path.join(HERE, "bluforge_classic.py")
RESULTS_DIR = os.path.join(os.environ.get("BLUFORGE_LOGS") or os.path.join(os.path.dirname(HERE), "logs"), "classic")
CAPTURE_DIR = os.path.join(os.environ.get("BLUFORGE_LOGS") or os.path.join(os.path.dirname(HERE), "logs"), "captures")
BLE_KINDS = ("adv", "interfere", "sniff")
CLASSIC_JOB_KINDS = ("c_a2dp", "c_load", "c_traffic", "c_avrcp_watch", "c_cycle", "c_hfp")
ADDR_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
STATIC_DIR = os.path.join(HERE, "static")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# survey output lines start with {"m": ...}; tolerate any JSON spacing
SURVEY_LINE = re.compile(r'^\{\s*"m"\s*:\s*"')

def all_hci():
    """The real controllers present on THIS machine, detected fresh each
    call (cheap: one sysfs directory listing) rather than a fixed count.
    Confirmed live as a real portability bug otherwise: a hardcoded
    range(8) (this unit's own controller count) offered hci4-hci7 as
    selectable radios on a second physical unit that only had 4."""
    return detect_hci_indices()


# ---------------------------------------------------------------- job manager
class Job:
    def __init__(self, job_id, kind, dev, argv):
        self.id = job_id
        self.kind = kind
        self.dev = dev
        self.argv = argv
        self.proc = None
        self.started = time.time()
        self.stopped = None
        self.exit_code = None
        self.devices = {}       # sniff --survey: addr -> latest device record
        self.stats = None

    def status(self):
        return {
            "id": self.id, "kind": self.kind, "dev": self.dev,
            "hci": "hci%d" % self.dev,
            "running": self.proc is not None and self.proc.poll() is None,
            "started": self.started, "stopped": self.stopped,
            "uptime": round(time.time() - self.started, 1) if self.stopped is None else None,
            "exit_code": self.exit_code,
            # BLE jobs: the arguments after the sub-command ("wifi 6 --bw 20", "nonconn -i 20") for the
            # Plan / PDU columns. argv is [python, -u, cli, --dev, N, <sub-command>, ...].
            "args": " ".join(str(x) for x in self.argv[6:]) if self.kind in BLE_KINDS else "",
            # Classic jobs: the device they talk to (the one Bluetooth address on the command line)
            "addr": next((str(x) for x in self.argv if ADDR_RE.match(str(x))), None) if self.kind in CLASSIC_JOB_KINDS else None,
        }


class Jobs:
    """Tracks multiple concurrent jobs, each bound to one controller. Only
    one job per controller at a time (refuses a second job on a busy hci)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}          # job_id -> Job
        self.by_dev = {}        # dev -> job_id
        self.next_id = 1
        self.subs = []
        self.subs_lock = threading.Lock()
        self.backlog = []
        # Controllers handed to BlueZ for Classic work. A reserved radio never
        # runs a BLE raw-HCI job (those power it off to BlueZ), and Classic
        # jobs/actions only run on a reserved radio.
        self.reserved = set()
        self.dev_locks = {}     # dev -> Lock serialising one-shot Classic actions

    def publish(self, evt):
        with self.subs_lock:
            if evt.get("type") == "line" and not evt.get("nobacklog"):
                self.backlog.append(evt)
                if len(self.backlog) > 500:
                    self.backlog = self.backlog[-500:]
            for q in list(self.subs):
                try:
                    q.put_nowait(evt)
                except queue.Full:
                    # A slow client must not be dropped silently (its page would
                    # look connected but stop updating). Flush its queue and tell
                    # its stream to close, so the browser reconnects and resyncs.
                    try:
                        while True:
                            q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait({"type": "overflow"})
                    except queue.Full:
                        pass
                    self.subs.remove(q)
                except Exception:
                    self.subs.remove(q)

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.subs_lock:
            self.subs.append(q)
            backlog = list(self.backlog)
        return q, backlog

    def unsubscribe(self, q):
        with self.subs_lock:
            if q in self.subs:
                self.subs.remove(q)

    def available_hci(self):
        """Radios free for a BLE raw-HCI job (not busy, not reserved for Classic)."""
        with self.lock:
            return [d for d in all_hci() if d not in self.by_dev and d not in self.reserved]

    def reserve(self, dev):
        if dev not in all_hci():
            return False, "hci%d is not a controller present on this unit" % dev
        with self.lock:
            jid = self.by_dev.get(dev)
            if jid and self.jobs[jid].kind in BLE_KINDS:
                return False, "hci%d is running BLE job %s - stop it first" % (dev, jid)
            self.reserved.add(dev)
        return True, "hci%d reserved for Bluetooth Classic" % dev

    def release(self, dev):
        with self.lock:
            jid = self.by_dev.get(dev)
            if jid and self.jobs[jid].kind in CLASSIC_JOB_KINDS:
                return False, "hci%d is running Classic job %s - stop it first" % (dev, jid)
            self.reserved.discard(dev)
        return True, "hci%d released to the BLE engine" % dev

    def dev_lock(self, dev):
        with self.lock:
            return self.dev_locks.setdefault(dev, threading.Lock())

    def classic_job_on(self, dev):
        with self.lock:
            jid = self.by_dev.get(dev)
            return jid if jid and self.jobs[jid].kind in CLASSIC_JOB_KINDS else None

    MAX_FINISHED_KEPT = 50          # finished job records kept for the status list; older ones are dropped
    MAX_SNIFF_DEVICES = 6000        # per sniff job (the sniffer itself caps at 5000)

    def _prune(self):
        """Called with self.lock held. Keeps memory bounded on a unit that runs for weeks."""
        active_ids = set(self.by_dev.values())
        done = sorted((j for j in self.jobs.values()
                       if j.proc is not None and j.proc.poll() is not None and j.id not in active_ids),
                      key=lambda j: j.started)
        for j in done[:-self.MAX_FINISHED_KEPT] if len(done) > self.MAX_FINISHED_KEPT else []:
            del self.jobs[j.id]
        # only the newest sniff job on each radio keeps its device table (that is what the page restores from)
        newest = {}
        for j in self.jobs.values():
            if j.kind == "sniff" and (j.dev not in newest or j.started > newest[j.dev].started):
                newest[j.dev] = j
        for j in self.jobs.values():
            if j.kind == "sniff" and newest.get(j.dev) is not j:
                j.devices = {}

    def start(self, kind, dev, argv):
        if dev not in all_hci():
            return False, "hci%d is not a controller present on this unit" % dev, None
        with self.lock:
            if kind in BLE_KINDS and dev in self.reserved:
                return False, "hci%d is reserved for Bluetooth Classic - release it on the Classic tab first" % dev, None
            if kind in CLASSIC_JOB_KINDS and dev not in self.reserved:
                return False, "hci%d is not reserved for Classic - reserve it on the Classic tab first" % dev, None
            if dev in self.by_dev:
                return False, "hci%d is already running job %s" % (dev, self.by_dev[dev]), None
            job_id = "job%d" % self.next_id
            self.next_id += 1
            job = Job(job_id, kind, dev, argv)
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            try:
                job.proc = subprocess.Popen(
                    argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, start_new_session=True, env=env)
            except Exception as e:
                return False, "failed to start: %s" % e, None
            self.jobs[job_id] = job
            self.by_dev[dev] = job_id
            self._prune()
        self.publish({"type": "status", "event": "started", "job": job.status(), "ts": time.time()})
        threading.Thread(target=self._reader, args=(job,), daemon=True).start()
        return True, "started", job_id

    def _reader(self, job):
        pending = []
        try:
            for line in job.proc.stdout:
                text = ANSI.sub("", line.rstrip("\n"))
                if job.kind == "sniff" and SURVEY_LINE.match(text):
                    # survey output: devices changed this second, then a stats line.
                    # One SSE event per second per job instead of one per packet.
                    try:
                        obj = json.loads(text)
                    except ValueError:
                        obj = None
                    if obj and obj.get("m") == "dev":
                        pending.append(obj)
                        if obj["addr"] not in job.devices and len(job.devices) >= self.MAX_SNIFF_DEVICES:
                            job.devices.pop(next(iter(job.devices)))      # drop the oldest-inserted device
                        job.devices[obj["addr"]] = obj
                        continue
                    if obj and obj.get("m") == "stats":
                        job.stats = obj
                        self.publish({"type": "sniff", "job_id": job.id, "dev": job.dev,
                                      "devices": pending, "stats": obj,
                                      "now": time.time()})
                        pending = []
                        continue
                    if obj and obj.get("m") == "warn":
                        text = "[!] " + str(obj.get("msg"))
                # 1 Hz Classic metric samples would evict real history from the
                # replay backlog within minutes; results are saved to disk anyway.
                self.publish({"type": "line", "job_id": job.id, "dev": job.dev, "text": text,
                               "nobacklog": text.startswith('{"m":"sample"')})
        except Exception:
            pass
        job.exit_code = job.proc.wait()
        job.stopped = time.time()
        with self.lock:
            if self.by_dev.get(job.dev) == job.id:
                del self.by_dev[job.dev]
        self.publish({"type": "status", "event": "finished", "job": job.status(), "ts": time.time()})

    def stop(self, job_id, timeout=5.0):
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            return False, "no such job %r" % job_id
        if job.proc.poll() is not None:
            return True, "already stopped"
        try:
            os.killpg(os.getpgid(job.proc.pid), signal.SIGINT)
        except Exception:
            try:
                job.proc.send_signal(signal.SIGINT)
            except Exception:
                pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if job.proc.poll() is not None:
                break
            time.sleep(0.1)
        if job.proc.poll() is None:
            try:
                os.killpg(os.getpgid(job.proc.pid), signal.SIGTERM)
            except Exception:
                job.proc.terminate()
            # Confirmed by code review: this previously returned success
            # unconditionally right after sending SIGTERM, with no check that
            # it actually worked -- a wedged job (holding its controller
            # exclusively the whole time) was reported to the operator as
            # "stopped" while still running. Give SIGTERM a short bounded
            # window too, and report the real outcome either way.
            term_deadline = time.time() + 3.0
            while time.time() < term_deadline:
                if job.proc.poll() is not None:
                    return True, "stopped"
                time.sleep(0.1)
            return False, ("hci%d job did not stop after SIGINT+SIGTERM -- it is likely wedged. "
                            "Manual recovery may be needed (see docs/INSTALL_LOG.md)." % job.dev)
        return True, "stopped"

    def stop_all(self):
        """Stop every job. Returns [(job_id, message)] for the ones that did NOT stop (empty = all stopped)."""
        with self.lock:
            ids = list(self.jobs.keys())
        failed = []
        for jid in ids:
            ok, msg = self.stop(jid)
            if not ok:
                failed.append((jid, msg))
        return failed

    def sniff_snapshot(self):
        """Devices from the most recent sniff job on each radio (running or finished),
        so a page load / reconnect can rebuild its table without replaying packets."""
        with self.lock:
            jobs = [j for j in self.jobs.values() if j.kind == "sniff"]
        latest = {}
        for j in jobs:
            if j.dev not in latest or j.started > latest[j.dev].started:
                latest[j.dev] = j
        return [{"job_id": j.id, "dev": j.dev, "running": j.proc.poll() is None,
                 "stats": j.stats, "devices": list(j.devices.values())} for j in latest.values()]

    def snapshot(self):
        with self.lock:
            running = [j.status() for j in self.jobs.values() if j.proc.poll() is None]
            finished = [j.status() for j in self.jobs.values() if j.proc.poll() is not None]
        return {"running": running, "finished": finished[-20:],
                "available_hci": self.available_hci(), "all_hci": all_hci(),
                "classic_reserved": sorted(self.reserved), "hostname": socket.gethostname(),
                "bluez": bluez_info(), "health": health_report(), "version": __version__}


JOBS = Jobs()


# ---------------------------------------------------------------- BlueZ presence + end-user install
# BluForge is sold without BlueZ: the end-user installs it (deploy/bluforge-bluez.sh install).
BLUEZ_NOTICE = ("BlueZ is third-party open-source software (the Linux Bluetooth stack, GPL/LGPL licensed). "
                "It is not supplied by Candela Technologies. Installing it downloads it from your operating "
                "system vendor's package repository onto this machine, under its own licence terms. "
                "BluForge needs it to run.")
BLUEZ_JOB = {"running": False, "log": [], "rc": None}
BLUEZ_LOCK = threading.Lock()


def bluez_installed():
    daemon = any(os.access(p, os.X_OK) for p in (
        "/usr/libexec/bluetooth/bluetoothd", "/usr/lib/bluetooth/bluetoothd", "/usr/sbin/bluetoothd"))
    return daemon and shutil.which("bluetoothctl") is not None


def bluez_script():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in ("/usr/local/sbin/bluforge-bluez",
              os.path.join(here, "..", "deploy", "bluforge-bluez.sh"),
              os.path.join(here, "bluforge-bluez.sh")):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return os.path.abspath(p)
    return None


def bluez_info():
    return {"installed": bluez_installed(), "installing": BLUEZ_JOB["running"],
            "can_install": bluez_script() is not None, "notice": BLUEZ_NOTICE}


def bluez_gate():
    """None when BlueZ is present, else the message every Bluetooth action is refused with."""
    if bluez_installed():
        return None
    return "BlueZ is not installed. Install it from the banner at the top of this page (or run: sudo bluforge-bluez install)."


def bluez_install_job():
    script = bluez_script()
    try:
        p = subprocess.Popen([script, "install", "--accept"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, env=dict(os.environ, TERM="dumb"))
        for line in p.stdout:
            t = ANSI.sub("", line.rstrip("\n"))
            BLUEZ_JOB["log"] = (BLUEZ_JOB["log"] + [t])[-200:]
            JOBS.publish({"type": "bluez", "text": t, "nobacklog": True})
        rc = p.wait()
    except Exception as e:
        rc = 1
        JOBS.publish({"type": "bluez", "text": "install failed to start: %s" % e, "nobacklog": True})
    BLUEZ_JOB["rc"] = rc
    BLUEZ_JOB["running"] = False
    JOBS.publish({"type": "bluez", "done": True, "ok": rc == 0 and bluez_installed(), "nobacklog": True})

# ---------------------------------------------------------------- argv builders
def _opt(argv, flag, v):
    if v is None:
        return
    if isinstance(v, str) and v.strip() == "":
        return
    argv += [flag, str(v)]


def _flag(argv, flag, v):
    if v:
        argv += [flag]


def _bad(msg):
    raise ValueError(msg)


def _text(v, name, pattern, maxlen=200):
    """Optional string that must match `pattern` (checked here so a bad value is a 400, not a crashed job)."""
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    v = str(v)
    if len(v) > maxlen or not re.fullmatch(pattern, v):
        _bad("%s has an invalid value" % name)
    return v


def _choice(v, name, allowed, default=None):
    v = default if v in (None, "") else v
    if v not in allowed:
        _bad("%s must be one of: %s" % (name, ", ".join(allowed)))
    return v


ADV_TYPES_OK = ("ind", "direct", "direct-l", "scan", "nonconn")
ADV_CHANNELS_RE = r"3[789](,3[789]){0,2}"
HEX_RE = r"[0-9A-Fa-f: ]{0,100}"
INTERFERE_MODES = ("wifi", "single", "sweep", "hop", "band", "adv-flood")


def new_capture_path(dev):
    """A fresh, unused file name in the capture folder for one sniff job: YYYYMMDD-HHMMSS_blesniff_hciN.pcap"""
    try:
        os.makedirs(CAPTURE_DIR, exist_ok=True)
    except OSError as e:
        _bad("cannot create the capture folder %s: %s" % (CAPTURE_DIR, e))
    base = "%s_blesniff_hci%d" % (time.strftime("%Y%m%d-%H%M%S"), dev)
    path, n = os.path.join(CAPTURE_DIR, base + ".pcap"), 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(CAPTURE_DIR, "%s_%d.pcap" % (base, n))
    return path


def build_argv(tool, dev, p):
    """Argv for a BLE job. Every value is typed and range-checked here (like the Classic builder), so a bad
    value is refused with a clear 400 instead of crashing the helper after it has claimed the radio."""
    py = [sys.executable, "-u", CLI, "--dev", str(dev)]
    if tool == "adv":
        t = _choice(p.get("type"), "type", ADV_TYPES_OK, "nonconn")
        a = py + ["adv", t]
        _opt(a, "-i", _num(p.get("interval"), "interval ms", 20, 10240, cast=float))
        _opt(a, "-c", _text(p.get("channels"), "channels", ADV_CHANNELS_RE, 20))
        peer = _text(p.get("peer"), "peer", r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", 17)
        _opt(a, "-p", peer)
        _opt(a, "--peer-type", _choice(p.get("peer_type"), "peer type", ("public", "random"), "public") if p.get("peer_type") else None)
        name = p.get("name")
        if name not in (None, ""):
            name = str(name)
            if len(name.encode("utf-8", "replace")) > 29 or any(ord(ch) < 32 for ch in name):
                _bad("name must be at most 29 bytes with no control characters")
            _opt(a, "-n", name)
        _opt(a, "-d", _text(p.get("data"), "data", HEX_RE, 100))
        _opt(a, "--profile", _text(p.get("profile"), "profile", r"[a-z0-9_]{1,32}", 32))
        _opt(a, "--profile-cfg", _text(p.get("profile_cfg"), "profile config", r"[A-Za-z0-9_=,.:/\- ]{0,200}", 200))
        _opt(a, "--scan-resp", _text(p.get("scan_resp"), "scan response", HEX_RE, 100))
        power = _text(p.get("power"), "power", r"(max|min|hi|lo|high|low|-?\d{1,3})", 6)
        if power is not None and power.lstrip("-").isdigit() and not -127 <= int(power) <= 20:
            _bad("power must be -127..20 dBm, or max/min")
        _opt(a, "--power", power)
        _opt(a, "--flags", _text(p.get("flags"), "flags", r"[0-9A-Fa-f]{1,2}", 2))
        _flag(a, "--no-flags", p.get("no_flags"))
        _opt(a, "-t", _num(p.get("duration"), "duration", 0, 86400))
        return a
    if tool == "interfere":
        mode = _choice(p.get("mode"), "mode", INTERFERE_MODES, "single")
        a = py + ["interfere", mode]
        arg = p.get("arg")
        if mode in ("wifi", "single", "sweep", "hop"):
            arg = str(arg).strip() if arg not in (None, "") else ""
            shape = {"single": r"\d{1,2}", "wifi": r"\d{1,2}", "sweep": r"\d{1,2}-\d{1,2}",
                     "hop": r"\d{1,2}(,\d{1,2}){0,39}"}[mode]
            if not re.fullmatch(shape, arg):
                _bad("the %s argument is missing or malformed" % mode)
            nums = [int(x) for x in re.split(r"[-,]", arg)]
            if mode == "wifi" and not 1 <= nums[0] <= 13:
                _bad("Wi-Fi channel must be 1..13")
            if mode != "wifi" and any(not 0 <= n <= 39 for n in nums):
                _bad("BLE channels must be 0..39")
            a.append(arg)
        _opt(a, "-w", _choice(p.get("bw"), "bandwidth", ("20", "40")) if p.get("bw") not in (None, "") else None)
        _opt(a, "-l", _num(p.get("len"), "payload length", 0, 255))
        _opt(a, "-P", _text(p.get("payload"), "payload", r"[a-z0-9]{2,12}", 12))
        _opt(a, "--phy", _choice(p.get("phy"), "phy", ("1M", "2M")) if p.get("phy") not in (None, "") else None)
        _opt(a, "-D", _num(p.get("dwell"), "dwell ms", 1, 600000, cast=float))
        power = _text(p.get("power"), "power", r"(max|min|hi|lo|high|low|-?\d{1,3})", 6)
        if power is not None and power.lstrip("-").isdigit() and not -127 <= int(power) <= 20:
            _bad("power must be -127..20 dBm, or max/min")
        _opt(a, "--power", power)
        _opt(a, "--interval", _num(p.get("interval"), "interval ms", 20, 10240, cast=float))
        _opt(a, "--adv-channels", _text(p.get("adv_channels"), "advertising channels", ADV_CHANNELS_RE, 20))
        _opt(a, "-t", _num(p.get("duration"), "duration", 0, 86400))
        _flag(a, "--map", p.get("map"))
        return a
    if tool == "sniff":
        a = py + ["sniff", "--json", "--survey"]
        _flag(a, "--active", p.get("active"))
        _flag(a, "--coded", p.get("coded"))
        if p.get("ext") in ("auto", "on", "off"):
            a += ["--ext", p["ext"]]
        interval = _num(p.get("interval"), "scan interval ms", 3, 10240, cast=float)
        window = _num(p.get("window"), "scan window ms", 3, 10240, cast=float)
        if interval is not None and window is not None and window > interval:
            _bad("scan window must not be larger than the interval")
        _opt(a, "--interval", interval)
        _opt(a, "--window", window)
        _flag(a, "--dedup", p.get("dedup"))
        f = p.get("filter")
        if f not in (None, ""):
            f = str(f)
            if len(f) > 64 or any(ord(ch) < 32 for ch in f):
                _bad("filter must be at most 64 characters with no control characters")
            a += ["--filter", f]
        _opt(a, "-t", _num(p.get("duration"), "duration", 0, 86400))
        if p.get("pcap"):                       # record every advertising report to a pcap for Wireshark (opt-in over the API)
            a += ["--pcap", new_capture_path(dev)]
            _opt(a, "--pcap-max-mb", _num(p.get("pcap_max_mb"), "capture size limit MB", 0, 100000))
        return a
    raise ValueError("unknown tool %r" % tool)


# ---------------------------------------------------------------- Bluetooth Classic
AUDIO_USER = {"name": None}     # set from --audio-user; None = auto-detect the desktop user


def _num(v, name, lo, hi, cast=int, default=None):
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return default
    try:
        x = cast(v)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % name)
    if not lo <= x <= hi:
        raise ValueError("%s must be between %s and %s" % (name, lo, hi))
    return x


def _addr(p):
    a = (p.get("addr") or "").strip()
    if not ADDR_RE.match(a):
        raise ValueError("a valid Bluetooth address (AA:BB:CC:DD:EE:FF) is required")
    return a.upper()


def _source(v):
    v = (v or "sweep").strip()
    if v in ("tone", "sweep", "noise"):
        return v
    if v.startswith("file:"):
        path = v[5:]
        exts = (".wav", ".mp3", ".mp2", ".mpga", ".mpeg", ".mpg", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus",
                ".wma", ".aif", ".aiff", ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".3gp", ".amr")
        if not (os.path.isabs(path) and path.lower().endswith(exts) and os.path.isfile(path)):
            raise ValueError("audio file must be an existing absolute path ending in " + " ".join(exts))
        return v
    raise ValueError("source must be tone, sweep, noise or file:/abs/path/audio")


def _classic_base(dev):
    a = [sys.executable, "-u", CLASSIC, "--hci", str(dev)]
    if AUDIO_USER["name"]:
        a += ["--audio-user", AUDIO_USER["name"]]
    return a


def build_classic_argv(name, dev, p):
    """Argv for a Classic one-shot action or long-running job. Every value is
    validated/typed here, so nothing from the browser reaches a shell or the
    CLI unchecked."""
    a = _classic_base(dev)
    if name == "adapter":
        a.append("adapter")
        for key in ("power", "pairable", "discoverable"):
            v = p.get(key)
            if v in ("on", "off"):
                a += ["--" + key, v]
        return a
    if name == "scan":
        a += ["scan", "-t", str(_num(p.get("duration"), "duration", 3, 120, default=10))]
        if p.get("transport") == "auto":
            a += ["--transport", "auto"]
        return a
    if name == "devices":
        return a + ["devices"]
    if name == "link-local":
        # prepare another radio of this unit as the A2DP receiver (pairs the two radios on first use)
        peer = _num(p.get("peer"), "receiver radio", 0, 31)
        if peer is None:
            raise ValueError("receiver radio (peer) is required")
        if peer == dev:
            raise ValueError("the receiver must be a different radio than the sender")
        return a + ["link-local", str(peer)]
    if name in ("info", "disconnect", "remove"):
        return a + [name, _addr(p)]
    if name == "pair":
        pin = str(p.get("pin") or "0000")
        if not re.fullmatch(r"[0-9]{1,16}", pin):
            raise ValueError("PIN must be 1-16 digits")
        return a + ["pair", _addr(p), "--pin", pin]
    if name == "connect":
        return a + ["connect", _addr(p)]
    if name == "interrogate":
        a += ["interrogate", _addr(p)]
        if p.get("transport") in ("auto", "le", "bredr"):
            a += ["--transport", p["transport"]]
        a += ["--scan", str(_num(p.get("scan"), "scan seconds", 3, 60, default=15))]
        if p.get("read") is False:
            a.append("--no-read")
        return a
    if name == "avrcp-player":
        cmd = p.get("cmd", "status")
        if cmd not in ("status", "play", "pause", "stop", "next", "previous", "fastforward", "rewind"):
            raise ValueError("bad AVRCP player command")
        return a + ["avrcp-player", _addr(p), cmd]
    if name == "avrcp-volume":
        steps = str(p.get("steps") or "0,32,64,96,127,64")
        if not re.fullmatch(r"\d{1,3}(,\d{1,3}){0,19}", steps):
            raise ValueError("steps must be a comma list of 0-127 values")
        return a + ["avrcp-volume", _addr(p), "--steps", steps]
    if name == "c_a2dp":
        a += ["a2dp", _addr(p), "-t", str(_num(p.get("duration"), "duration", 0, 86400, default=60)),
              "--source", _source(p.get("source"))]
        codec = (p.get("codec") or "").strip().lower()
        if codec:
            if not re.fullmatch(r"[a-z0-9_]{2,16}", codec):
                raise ValueError("bad codec name")
            a += ["--codec", codec]
        _opt(a, "--interval", _num(p.get("interval"), "interval", 1, 60, cast=float))
        _opt(a, "--latency", _num(p.get("latency"), "stream latency ms", 10, 2000))
        _opt(a, "--min-active-pct", _num(p.get("min_active_pct"), "min active %", 0, 100, cast=float))
        _opt(a, "--max-xruns", _num(p.get("max_xruns"), "max xruns", 0, 100000))
        _opt(a, "--max-disconnects", _num(p.get("max_disconnects"), "max disconnects", 0, 1000))
        _opt(a, "--min-rssi", _num(p.get("min_rssi"), "min RSSI", -127, 20))
        _opt(a, "--max-delay-ms", _num(p.get("max_delay_ms"), "max link delay ms", 1, 60000, cast=float))
        _opt(a, "--max-jitter-ms", _num(p.get("max_jitter_ms"), "max jitter ms", 0.1, 10000, cast=float))
        _flag(a, "--reconnect", p.get("reconnect"))
        _flag(a, "--disconnect-after", p.get("disconnect_after"))
        return a
    if name == "c_load":
        # realistic Bluetooth Classic interference: an unattended, never-give-up A2DP stream to a real sink
        a += ["load", _addr(p), "-t", str(_num(p.get("duration"), "duration", 0, 604800, default=0)),
              "--source", _source(p.get("source") or "noise")]
        codec = (p.get("codec") or "").strip().lower()
        if codec:
            if not re.fullmatch(r"[a-z0-9_]{2,16}", codec):
                raise ValueError("bad codec name")
            a += ["--codec", codec]
        _opt(a, "--interval", _num(p.get("interval"), "interval", 1, 60, cast=float))
        _opt(a, "--latency", _num(p.get("latency"), "stream latency ms", 10, 2000))
        _opt(a, "--min-active-pct", _num(p.get("min_active_pct"), "min active %", 0, 100, cast=float))
        return a
    if name == "c_traffic":
        # realistic Classic data traffic (file transfer, video, HID) between two radios of this unit
        kind = str(p.get("kind") or "file")
        if kind not in bt_traffic.KINDS:
            raise ValueError("traffic type must be one of: " + ", ".join(bt_traffic.KINDS))
        a += ["traffic", _addr(p), "--kind", kind, "-t", str(_num(p.get("duration"), "duration", 0, 604800, default=0))]
        _opt(a, "--file-mb", _num(p.get("file_mb"), "file size", 0.1, 500, cast=float))
        _opt(a, "--video-kbps", _num(p.get("video_kbps"), "video bitrate", 50, 3000))
        _opt(a, "--interval", _num(p.get("interval"), "interval", 1, 60, cast=float))
        _opt(a, "--min-active-pct", _num(p.get("min_active_pct"), "min active %", 0, 100, cast=float))
        return a
    if name == "c_avrcp_watch":
        exp = str(p.get("expect") or "")
        if exp and not re.fullmatch(r"[A-Za-z,]{1,120}", exp):
            raise ValueError("expect must be a comma list of key names")
        a += ["avrcp-watch", _addr(p), "-t", str(_num(p.get("duration"), "duration", 5, 3600, default=120))]
        _opt(a, "--expect", exp)
        return a
    if name == "c_hfp":
        scenario = p.get("scenario", "call")
        if scenario not in ("slc", "sco", "call"):
            raise ValueError("scenario must be slc, sco or call")
        a += ["hfp", _addr(p), "--scenario", scenario, "-t", str(_num(p.get("duration"), "duration", 5, 3600, default=20))]
        number = str(p.get("number") or "")
        if number:
            if not re.fullmatch(r"\+?[0-9]{3,20}", number):
                raise ValueError("caller number must be digits (optional leading +)")
            a += ["--number", number]
        _opt(a, "--ring-timeout", _num(p.get("ring_timeout"), "ring timeout", 3, 120))
        return a
    if name == "c_cycle":
        a += ["cycle", _addr(p), "-n", str(_num(p.get("count"), "cycles", 1, 1000, default=10))]
        _opt(a, "--gap", _num(p.get("gap"), "gap", 1, 60, cast=float))
        _opt(a, "--min-success", _num(p.get("min_success"), "min success %", 0, 100, cast=float))
        return a
    raise ValueError("unknown Classic action %r" % name)


# one-shot actions that cannot disturb a running stream/cycle/watch job
SAFE_DURING_JOB = ("info", "devices", "adapter", "avrcp-player", "avrcp-volume")
ACTION_TIMEOUT = {"interrogate": 150, "scan": 150, "pair": 120, "connect": 120, "disconnect": 40, "remove": 30,
                  "link-local": 150}


def run_classic_action(dev, action, params):
    """Run a one-shot Classic action synchronously and return its JSON result
    plus the progress log. Serialised per radio."""
    if dev not in all_hci():
        return 400, {"ok": False, "error": "hci%d is not a controller on this unit" % dev}
    if dev not in JOBS.reserved:
        return 409, {"ok": False, "error": "hci%d is not reserved for Classic - reserve it first" % dev}
    running = JOBS.classic_job_on(dev)
    if running and action not in SAFE_DURING_JOB:
        return 409, {"ok": False, "error": "%s is running on hci%d - stop it before '%s'" % (running, dev, action)}
    try:
        argv = build_classic_argv(action, dev, params)
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}
    if action == "link-local":
        peer = int(params.get("peer"))
        if peer not in all_hci():
            return 400, {"ok": False, "error": "hci%d is not a controller on this unit" % peer}
        if peer not in JOBS.reserved:
            return 409, {"ok": False, "error": "hci%d is not reserved for Classic - reserve it first" % peer}
        busy = JOBS.classic_job_on(peer)
        if busy:
            return 409, {"ok": False, "error": "%s is running on hci%d - stop it before using it as the receiver" % (busy, peer)}
    lock = JOBS.dev_lock(dev)
    # read-only polls (the GUI's 5 s auto-refresh) must neither block nor be
    # blocked by a pair/connect in progress
    readonly = action in ("info", "devices")
    if not readonly and not lock.acquire(blocking=False):
        return 409, {"ok": False, "error": "another Classic action is still running on hci%d" % dev}
    try:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=ACTION_TIMEOUT.get(action, 40),
                               env=dict(os.environ, PYTHONUNBUFFERED="1"))
            out, rc = r.stdout + r.stderr, r.returncode
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return 504, {"ok": False, "error": "'%s' timed out on hci%d" % (action, dev), "log": out.splitlines()[-20:]}
    finally:
        if not readonly:
            lock.release()
    lines = [ANSI.sub("", l) for l in out.splitlines() if l.strip()]
    result = None
    for l in reversed(lines):
        if l.startswith("{"):
            try:
                result = json.loads(l)
                break
            except ValueError:
                continue
    log = [l for l in lines if not l.startswith("{")]
    for l in log:
        JOBS.publish({"type": "line", "job_id": "action", "kind": "c_action", "dev": dev, "text": l})
    if result is None:
        result = {"ok": False, "error": "no result from the Classic engine (exit %s)" % rc}
    result["log"] = log[-30:]
    return (200 if result.get("ok") else 422), result


def classic_preflight():
    argv = [sys.executable, CLASSIC, "preflight"]
    if AUDIO_USER["name"]:
        argv = [sys.executable, CLASSIC, "--audio-user", AUDIO_USER["name"], "preflight"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        pf = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        pf = {"ok": False, "error": "preflight failed: %s" % e, "notes": []}
    pf["reserved"] = sorted(JOBS.reserved)
    pf["all_hci"] = all_hci()
    return pf


SAFE_FILE = re.compile(r"^[A-Za-z0-9_.-]+\.(json|csv)$")
SAFE_CAPTURE = re.compile(r"^[A-Za-z0-9_.-]+\.pcap$")
CAPTURE_HCI = re.compile(r"_blesniff_hci(\d+)")


def job_capture_name(job):
    """The capture file a sniff job records to (from its argv), or None."""
    argv = [str(x) for x in job.argv]
    if "--pcap" in argv and argv.index("--pcap") + 1 < len(argv):
        return os.path.basename(argv[argv.index("--pcap") + 1])
    return None


def sniff_captures(limit=100):
    """The packet captures of this unit, newest first. 'running' = a sniff job is still writing it."""
    with JOBS.lock:
        writing = {job_capture_name(j) for j in JOBS.jobs.values()
                   if j.kind == "sniff" and j.proc is not None and j.proc.poll() is None}
    out = []
    try:
        names = [n for n in os.listdir(CAPTURE_DIR) if SAFE_CAPTURE.match(n) and not n.startswith(".")]
    except OSError:
        return out
    for n in names:
        try:
            st = os.stat(os.path.join(CAPTURE_DIR, n))
        except OSError:
            continue
        m = CAPTURE_HCI.search(n)
        out.append({"file": n, "hci": int(m.group(1)) if m else None, "size": st.st_size, "mtime": st.st_mtime,
                    "running": n in writing})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:limit]


def classic_results(limit=40):
    out = []
    try:
        names = sorted((n for n in os.listdir(RESULTS_DIR) if n.endswith(".json") and not n.startswith(".")),
                       key=lambda n: os.path.getmtime(os.path.join(RESULTS_DIR, n)), reverse=True)[:limit]
    except OSError:
        return out
    for n in names:
        path = os.path.join(RESULTS_DIR, n)
        try:
            if os.path.getsize(path) > 8 * 1024 * 1024:
                continue
            with open(path) as f:
                sm = json.load(f).get("summary", {})
        except (OSError, ValueError):
            continue
        out.append({"file": n, "csv": n[:-5] + ".csv" if os.path.exists(path[:-5] + ".csv") else None,
                    "mtime": os.path.getmtime(path), "test": sm.get("test"), "addr": sm.get("addr"),
                    "hci": sm.get("hci"), "pass": sm.get("pass"), "duration_s": sm.get("duration_s"),
                    "fail_reasons": sm.get("fail_reasons", []), "summary": sm})
    return out


# ---------------------------------------------------------------- capabilities cache
_CAPS = {"data": None}


def cluster_safe(fn, fallback, *args):
    """The cluster layer only adds resources to the answer; if it ever fails, the page keeps working for this unit alone."""
    try:
        return fn(*args)
    except Exception as e:
        print("[!] cluster: %s (showing this unit only)" % e, file=sys.stderr, flush=True)
        return fallback


def get_caps(refresh=False):
    if _CAPS["data"] is not None and not refresh:
        return _CAPS["data"]
    try:
        out = subprocess.run([sys.executable, CLI, "caps", "--compact"],
                              capture_output=True, text=True, timeout=10).stdout
        _CAPS["data"] = json.loads(out)         # only a success is remembered
    except Exception as e:
        return {"error": str(e)}                # a failure is reported but retried on the next request
    return _CAPS["data"]


# ---------------------------------------------------------------- health
STARTED_AT = time.time()
INSTANCE = secrets.token_hex(6)       # names this server process; lets a cluster hub that reaches itself by another address notice
LOW_DISK_MB = 200


_WRITE_PROBE = {}      # directory -> (checked_at, result): the probe writes a file, so do it at most once a minute


def _really_writable(directory):
    """os.access() lies to root (and on read-only or special filesystems), so actually create and remove a file."""
    now = time.time()
    hit = _WRITE_PROBE.get(directory)
    if hit and now - hit[0] < 60:
        return hit[1]
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".health-")
        os.close(fd)
        os.unlink(tmp)
        ok = True
    except OSError:
        ok = False
    _WRITE_PROBE[directory] = (now, ok)
    return ok


def health_report(detail=False):
    """Self-checks that are cheap and side-effect free, so they can run on every status poll. Returns
    {"ok": bool, "warnings": [...]} (plus the raw check values when detail=True)."""
    warnings, checks = [], {}
    # the folder this server was started from must still exist (a rename/move while running breaks page and helper paths)
    checks["install_ok"] = os.path.isfile(os.path.join(STATIC_DIR, "index.html")) and os.path.isfile(CLI) and os.path.isfile(CLASSIC)
    if not checks["install_ok"]:
        warnings.append("The BluForge installation folder was moved, renamed or removed while the server was running. "
                        "Restart the service from the new location (pages and Bluetooth jobs cannot work until then).")
    # where results are saved: writable, and not nearly full
    logs = os.path.dirname(RESULTS_DIR)
    probe = RESULTS_DIR if os.path.isdir(RESULTS_DIR) else logs
    while probe and not os.path.isdir(probe) and probe != os.path.dirname(probe):
        probe = os.path.dirname(probe)
    checks["results_dir"] = RESULTS_DIR
    checks["results_writable"] = bool(probe) and _really_writable(probe)
    try:
        du = shutil.disk_usage(probe or "/")
        checks["disk_free_mb"] = int(du.free / 1048576)
    except OSError:
        checks["disk_free_mb"] = None
    if not checks["results_writable"]:
        warnings.append("Test results cannot be saved: %s is not writable." % RESULTS_DIR)
    elif checks["disk_free_mb"] is not None and checks["disk_free_mb"] < LOW_DISK_MB:
        warnings.append("Low disk space (%d MB free) - saved results may fail to write." % checks["disk_free_mb"])
    checks["bluez_installed"] = bluez_installed()
    checks["radios"] = all_hci()
    if checks["bluez_installed"] and not checks["radios"]:
        warnings.append("No Bluetooth radio is visible to the system (unplugged, or the driver did not load).")
    out = {"ok": not warnings, "warnings": warnings}
    if detail:
        out.update({"version": __version__, "result_schema": RESULT_SCHEMA, "uptime_s": int(time.time() - STARTED_AT), "instance": INSTANCE,
                    "host": socket.gethostname(), "python": sys.version.split()[0], "checks": checks})
    return out


# ---------------------------------------------------------------- HTTP handler

# ---------------------------------------------------------------- request checks (there is no login)
# The server runs as root and can start jobs and claim radios, and it has NO login: anyone who can reach the port can use
# it (see docs/ACCESS_CONTROL.md). What it still refuses: a wrong Host header when it listens on loopback only (DNS
# rebinding), and requests that change state unless they are same-site JSON, which stops another web page from driving
# the radios through a visitor's browser.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 deliberately: the SSE endpoint streams indefinitely by writing
    # directly to wfile with no Content-Length, which is invalid under
    # HTTP/1.1's keep-alive/chunking rules unless chunked encoding is used
    # explicitly. HTTP/1.0 has no such requirement (connection closes when
    # the response ends), which is exactly the simple behavior a streaming
    # endpoint needs here. Confirmed live: HTTP/1.1 silently produced zero
    # bytes on the SSE stream; HTTP/1.0 works correctly.
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _send_static(self, rel_path, ctype):
        path = os.path.join(STATIC_DIR, rel_path)
        if not os.path.isfile(path):
            if not os.path.isdir(STATIC_DIR):
                return self._send(503, {"error": "the BluForge installation folder was moved or removed while the server "
                                                  "was running - restart the service from its new location"})
            return self._send(404, {"error": "not found: %s" % rel_path})
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, ctype)

    # ------------------------------------------------------------ checks for every request
    def _local_only(self):
        """True for a request another BluForge sent (cluster mode): answer for this unit only, never forward or merge."""
        return self.headers.get(LOCAL_HEADER) == "1"

    def _host_ok(self):
        """DNS-rebinding guard: when listening on loopback only, the Host header must be a loopback name."""
        if self.server.server_address[0] not in LOOPBACK_HOSTS:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            name = host[1:host.find("]")] if "]" in host else host
        else:
            name = host.rsplit(":", 1)[0] if ":" in host else host
        return name in LOOPBACK_HOSTS

    def _csrf_error(self):
        """Extra checks for requests that change state. Returns (status, message) or None."""
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            host = (self.headers.get("Host") or "").lower()
            if urllib.parse.urlsplit(origin).netloc.lower() != host:
                return 403, "cross-site request refused (Origin does not match this server)"
        elif origin == "null":
            return 403, "cross-site request refused (opaque Origin)"
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return 403, "cross-site request refused"
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return 415, "requests must be application/json"
        return None

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "unexpected Host header"})
        if self.headers.get("Sec-Fetch-Site") == "cross-site" and self.headers.get("Sec-Fetch-Mode") != "navigate":
            return self._send(403, {"error": "cross-site request refused"})
        self._do_GET()

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "unexpected Host header"})
        err = self._csrf_error()
        if err:
            return self._send(err[0], {"ok": False, "error": err[1]})
        self._do_POST()

    def _do_GET(self):
        path = self.path.split("?")[0]
        merged = CLUSTER is not None and not self._local_only()      # the page asks for the whole cluster; a peer asks for this unit only
        if path == "/login":                                          # an old bookmark: there is no login any more
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path in ("/", "/index.html"):
            self._send_static("index.html", "text/html; charset=utf-8")
        elif path == "/style.css":
            self._send_static("style.css", "text/css; charset=utf-8")
        elif path == "/app.js":
            self._send_static("app.js", "application/javascript; charset=utf-8")
        elif path == "/classic.js":
            self._send_static("classic.js", "application/javascript; charset=utf-8")
        elif path.startswith("/assets/"):
            name = os.path.basename(path)
            ctype = "image/svg+xml" if name.endswith(".svg") else \
                "image/png" if name.endswith(".png") else \
                "image/x-icon" if name.endswith(".ico") else \
                "font/woff2" if name.endswith(".woff2") else "application/octet-stream"
            self._send_static(os.path.join("assets", name), ctype)
        elif path == "/api/caps":
            refresh = "refresh=1" in self.path
            caps = get_caps(refresh)
            self._send(200, cluster_safe(CLUSTER.merge_caps, caps, caps) if merged else caps)
        elif path == "/api/status":
            snap = JOBS.snapshot()
            self._send(200, cluster_safe(CLUSTER.merge_status, snap, snap) if merged else snap)
        elif path == "/api/units":
            snap = JOBS.snapshot()
            if CLUSTER is not None:
                self._send(200, cluster_safe(CLUSTER.units_payload, {"cluster": {"on": False}, "other_resources": [], "units": []}, snap))
            else:
                self._send(200, {"cluster": {"on": False}, "other_resources": [],
                                 "units": [{"eid": "", "host": socket.gethostname(), "local": True, "base": 0, "state": "local",
                                            "radios": len(snap.get("all_hci", []))}]})
        elif path == "/api/classic/preflight":
            pf = classic_preflight()
            self._send(200, cluster_safe(CLUSTER.merge_preflight, pf, pf) if merged else pf)
        elif path == "/api/classic/results":
            res = classic_results()
            self._send(200, {"results": cluster_safe(CLUSTER.merge_results, res, res) if merged else res})
        elif path.startswith("/api/classic/result/"):
            name = os.path.basename(path)
            if merged:
                got = cluster_safe(CLUSTER.fetch_result, None, name)       # a result file that lives on a resource
                if got is not None:
                    st, hdr, data = got
                    self.send_response(st)
                    for k in ("Content-Type", "Content-Disposition"):
                        if hdr.get(k):
                            self.send_header(k, hdr[k])
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            full = os.path.join(RESULTS_DIR, name)
            if not SAFE_FILE.match(name) or not os.path.isfile(full):
                return self._send(404, {"error": "no such result"})
            with open(full, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv" if name.endswith(".csv") else "application/json")
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/health":
            self._send(200, health_report(detail=True))
        elif path == "/api/bluez/status":
            self._send(200, dict(bluez_info(), log=BLUEZ_JOB["log"][-50:], rc=BLUEZ_JOB["rc"]))
        elif path == "/api/sniff/devices":
            snap = {"sniffs": JOBS.sniff_snapshot(), "now": time.time()}
            self._send(200, cluster_safe(CLUSTER.merge_sniffs, snap, snap) if merged else snap)
        elif path == "/api/sniff/captures":
            caps = sniff_captures()
            self._send(200, {"captures": cluster_safe(CLUSTER.merge_captures, caps, caps) if merged else caps})
        elif path.startswith("/api/sniff/capture/"):
            self._send_capture(os.path.basename(path), merged)
        elif path == "/api/stream":
            self._stream(live="live=1" in self.path.partition("?")[2].split("&"), merged=merged)
        else:
            self._send(404, {"error": "not found"})

    def _send_capture(self, name, merged):
        """Download one sniff capture (.pcap). Also fine while the job is still writing it: the file always ends on a whole record."""
        if merged:
            got = cluster_safe(CLUSTER.fetch_capture, None, name)         # a capture that lives on a resource
            if got is not None:
                st, hdr, data = got
                self.send_response(st)
                for k in ("Content-Type", "Content-Disposition"):
                    if hdr.get(k):
                        self.send_header(k, hdr[k])
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        full = os.path.join(CAPTURE_DIR, name)
        if not SAFE_CAPTURE.match(name) or not os.path.isfile(full):
            return self._send(404, {"error": "no such capture"})
        try:
            f = open(full, "rb")
            size = os.fstat(f.fileno()).st_size
        except OSError:
            return self._send(404, {"error": "no such capture"})
        with f:
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.tcpdump.pcap")
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                left = size
                while left > 0:                                      # streamed: a capture can be hundreds of MB
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except Exception:                                        # the browser went away
                pass

    def _do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        path = self.path.split("?")[0]
        merged = CLUSTER is not None and not self._local_only()
        if merged:
            try:
                routed = CLUSTER.route_post(path, body)               # a radio or job of a resource: that resource answers
            except Exception as e:
                return self._send(500, {"ok": False, "error": "cluster routing failed: %s" % e})
            if routed is not None:
                return self._send(routed[0], routed[1])
        if path in ("/api/start", "/api/classic/action", "/api/classic/reserve"):
            msg = bluez_gate()
            if msg:
                return self._send(409, {"ok": False, "error": msg, "bluez_missing": True})
        if path == "/api/bluez/install":
            if body.get("accept") is not True:
                return self._send(400, {"ok": False, "error": "the licence notice must be accepted (accept: true)"})
            if bluez_installed():
                return self._send(200, {"ok": True, "msg": "BlueZ is already installed"})
            if not bluez_script():
                return self._send(500, {"ok": False, "error": "bluforge-bluez installer script not found on this unit"})
            with BLUEZ_LOCK:
                if BLUEZ_JOB["running"]:
                    return self._send(409, {"ok": False, "error": "an install is already running"})
                BLUEZ_JOB.update(running=True, log=[], rc=None)
            threading.Thread(target=bluez_install_job, daemon=True).start()
            return self._send(200, {"ok": True, "msg": "install started"})
        if path == "/api/start":
            tool = body.get("tool")
            dev = body.get("dev")
            params = body.get("params", {})
            if tool not in BLE_KINDS + CLASSIC_JOB_KINDS:
                return self._send(400, {"ok": False, "error": "bad tool"})
            try:
                dev = int(dev)
            except (TypeError, ValueError):
                return self._send(400, {"ok": False, "error": "dev must be an hci index"})
            try:
                argv = (build_classic_argv(tool, dev, params) if tool in CLASSIC_JOB_KINDS
                        else build_argv(tool, dev, params))
            except Exception as e:
                return self._send(400, {"ok": False, "error": str(e)})
            ok, msg, job_id = JOBS.start(tool, dev, argv)
            self._send(200 if ok else 409, {"ok": ok, "msg": msg, "job_id": job_id, "argv": argv})
        elif path == "/api/classic/reserve":
            try:
                dev = int(body.get("dev"))
            except (TypeError, ValueError):
                return self._send(400, {"ok": False, "error": "dev must be an hci index"})
            ok, msg = JOBS.reserve(dev) if body.get("on", True) else JOBS.release(dev)
            JOBS.publish({"type": "status", "event": "reservation", "ts": time.time()})
            self._send(200 if ok else 409, {"ok": ok, "msg": msg, "reserved": sorted(JOBS.reserved)})
        elif path == "/api/classic/action":
            try:
                dev = int(body.get("dev"))
            except (TypeError, ValueError):
                return self._send(400, {"ok": False, "error": "dev must be an hci index"})
            code, res = run_classic_action(dev, str(body.get("action")), body.get("params") or {})
            self._send(code, res)
        elif path == "/api/stop":
            job_id = body.get("job_id")
            ok, msg = JOBS.stop(job_id)
            self._send(200, {"ok": ok, "msg": msg})
        elif path == "/api/stop_all":
            failed = JOBS.stop_all()
            if merged:
                failed += cluster_safe(CLUSTER.stop_all_peers, [])    # the panic button stops the whole cluster
            if failed:
                self._send(500, {"ok": False, "failed": [j for j, _ in failed],
                                 "error": "; ".join("%s: %s" % f for f in failed)})
            else:
                self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def _stream(self, live=False, merged=False):
        """live=True is a cluster hub following this unit: no hello snapshot and no replay of old console lines."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q, backlog = JOBS.subscribe()

        def write(evt):
            self.wfile.write(("data: %s\n\n" % json.dumps(evt)).encode())
            self.wfile.flush()

        try:
            if not live:
                snap = JOBS.snapshot()
                write({"type": "status", "event": "hello", "status": cluster_safe(CLUSTER.merge_status, snap, snap) if merged else snap})
                for evt in backlog:
                    write(evt)
            while True:
                try:
                    evt = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if evt.get("type") == "overflow":
                    return              # browser's EventSource reconnects and resyncs
                write(evt)
        except Exception:
            pass
        finally:
            JOBS.unsubscribe(q)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5480)
    ap.add_argument("--host", default="0.0.0.0",
                    help="address to listen on (default 0.0.0.0 = every interface, reachable by the unit's IP; use 127.0.0.1 for this machine only). There is no login: anyone who can reach the port can control the radios")
    ap.add_argument("--audio-user", default=None,
                    help="desktop user whose PipeWire session carries Classic audio (default: auto-detect)")
    ap.add_argument("--cluster", choices=("auto", "off"), default="auto",
                    help="auto (default): on a LANforge manager, find the clustered resources that run BluForge and show their radios; off: this unit only")
    ap.add_argument("--manager-api", default=clustermod.DEFAULT_MANAGER_API,
                    help="the LANforge manager's JSON API, where the list of resources comes from (default %(default)s)")
    ap.add_argument("--peer", action="append", default=[], metavar="EID=HOST[:PORT]",
                    help="a resource to include even when the manager's list does not show it, e.g. 1.2=192.168.204.24 (repeatable)")
    ap.add_argument("--peer-port", type=int, default=None, help="port BluForge listens on at the resources (default: this server's own port)")
    ap.add_argument("--scan-interval", type=float, default=10.0, help="seconds between looks for resources (default %(default)s)")
    a = ap.parse_args()
    peers = []
    for spec in a.peer:
        eid, sep, addr = spec.partition("=")
        host, _, port = addr.partition(":")
        if not sep or not eid or not host or (port and not port.isdigit()):
            ap.error("--peer wants EID=HOST[:PORT], for example 1.2=192.168.204.24 (got %r)" % spec)
        peers.append((eid, host, int(port) if port else (a.peer_port or a.port)))
    AUDIO_USER["name"] = a.audio_user
    if os.geteuid() != 0:
        print("[!] not root - jobs will fail to grab raw HCI. Start with sudo.", file=sys.stderr)

    def handle_sigterm(*_):
        JOBS.stop_all()
        # Belt-and-suspenders: confirmed live that with a real browser tab's
        # /api/stream connection open, sys.exit() from a signal handler did
        # NOT reliably terminate the process (some worker-thread/blocking-I/O
        # interaction kept it alive indefinitely -- daemon_threads=True below
        # addresses the common cause, but a live demo cannot depend on fully
        # diagnosing every such interaction). This timer guarantees the
        # process is actually gone within 2s no matter what: os._exit() ends
        # the process at the OS level immediately, ignoring any thread state.
        t = threading.Timer(2.0, os._exit, args=(0,))
        t.daemon = True
        t.start()
        sys.exit(0)
    signal.signal(signal.SIGTERM, handle_sigterm)
    # SIGHUP: fires when the launcher terminal this runs in gets closed (the
    # desktop launcher runs server.py in the foreground of an xterm so that
    # closing the window is the "manually stop the service" action). Without
    # this handler the default SIGHUP action just kills the process without
    # running JOBS.stop_all(), which would leave any active interference/ADV
    # job's subprocess running headless (orphaned, no way to stop it from the
    # GUI anymore) -- not acceptable for a live demo.
    signal.signal(signal.SIGHUP, handle_sigterm)

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    # ThreadingMixIn.daemon_threads defaults to False, so a long-lived
    # connection (an open browser tab's EventSource on /api/stream, which
    # blocks in its own worker thread for as long as the tab stays open) is a
    # non-daemon thread the whole PROCESS waits on before exiting. Confirmed
    # live: with a real SSE client connected, sys.exit() raised from the
    # SIGTERM/SIGHUP handler below left the process alive indefinitely (the
    # main thread exits, but the interpreter won't actually terminate until
    # every non-daemon thread does); plain SIGINT's built-in KeyboardInterrupt
    # path happened to still work, but relying on that asymmetry would be
    # fragile. Daemon worker threads fixes it at the root for every shutdown
    # path (SIGINT/SIGTERM/SIGHUP all exit promptly once this is set).
    srv.daemon_threads = True
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = a.host
    print("[+] BluForge GUI listening on %s:%d" % (a.host, a.port), flush=True)
    shown = "localhost" if a.host in LOOPBACK_HOSTS else (ip if a.host in ("0.0.0.0", "::") else a.host)
    print("[+] Open:  http://%s:%d/" % (shown, a.port), flush=True)
    if a.host not in LOOPBACK_HOSTS:
        print("[!] There is no login: anyone who can reach this port can control the radios. Use only on a trusted network, "
              "or listen on this machine only (--host 127.0.0.1) and tunnel (ssh -L %d:localhost:%d)." % (a.port, a.port), flush=True)
    global CLUSTER
    if a.cluster == "auto":
        CLUSTER = clustermod.Cluster(JOBS.publish, socket.gethostname(), INSTANCE, a.port, manager_api=a.manager_api,
                                     static_peers=peers, peer_port=a.peer_port, scan_interval=a.scan_interval)
        CLUSTER.start()
        print("[+] Cluster mode: looking for LANforge resources through %s%s (a unit with no manager API is simply a single unit)."
              % (a.manager_api, " and " + ", ".join("%s=%s:%d" % p for p in peers) if peers else ""), flush=True)
    threading.Thread(target=get_caps, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        JOBS.stop_all()
        if CLUSTER is not None:
            CLUSTER.stop()
        print("\n[+] bye")


if __name__ == "__main__":
    main()
