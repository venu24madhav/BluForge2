#!/usr/bin/env python3
"""
btlab_server.py - Web GUI backend for the BE200 BLE lab.

Thin control plane over btlab.py: it reads capabilities, starts/stops the
adv / interfere / sniff / coex commands as managed child processes, and streams
their output to the browser over Server-Sent Events (SSE). The RF engine
(btlab.py) is unchanged - this only wraps it.

It also manages a full-HCI capture (btmon -w) that runs concurrently with any
job; on stop it's converted (btle_pcap.py) into a genuine BLE Link-Layer
.pcap and that's what's listed/downloadable from the GUI (/api/capture/*).

Because the box has ONE Bluetooth radio, only one BT job runs at a time; the
server enforces that. Run as root:   python3 btlab_server.py [--port 8090]
"""
import os, re, sys, json, signal, socket, argparse, threading, subprocess, queue, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE  = os.path.dirname(os.path.abspath(__file__))
BTLAB = os.path.join(HERE, "btlab.py")
BTLE_PCAP = os.path.join(HERE, "btle_pcap.py")
BTCLASSIC_PCAP = os.path.join(HERE, "btclassic_pcap.py")
BTINSIGHT = os.path.join(HERE, "btinsight.py")
BTCAPTURE = os.path.join(HERE, "btcapture.py")
COEX  = os.path.join(HERE, "bt-coex.sh")
GUI   = os.path.join(HERE, "gui.html")
CAPTURE_DIR = os.path.join(HERE, "captures")
ANSI  = re.compile(r"\x1b\[[0-9;]*m")

# ---------------------------------------------------------------- job manager
class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.kind = None
        self.argv = None
        self.started = 0
        self.subs = []            # SSE subscriber queues
        self.subs_lock = threading.Lock()
        self.backlog = []         # recent lines (capped)

    def publish(self, evt):
        with self.subs_lock:
            if evt.get("type") == "line":
                self.backlog.append(evt)
                if len(self.backlog) > 400: self.backlog = self.backlog[-400:]
            dead = []
            for q in self.subs:
                try: q.put_nowait(evt)
                except Exception: dead.append(q)
            for q in dead:
                if q in self.subs: self.subs.remove(q)

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self.subs_lock:
            self.subs.append(q)
            backlog = list(self.backlog)
        return q, backlog

    def unsubscribe(self, q):
        with self.subs_lock:
            if q in self.subs: self.subs.remove(q)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def status(self):
        return {
            "running": self.running(),
            "kind": self.kind if self.running() else None,
            "argv": self.argv if self.running() else None,
            "started": self.started if self.running() else None,
            "uptime": round(time.time()-self.started,1) if self.running() else None,
        }

    def start(self, kind, argv):
        with self.lock:
            if self.running():
                return False, f"a '{self.kind}' job is already running - stop it first"
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            self.proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, start_new_session=True, env=env)
            self.kind, self.argv, self.started = kind, argv, time.time()
        with self.subs_lock:
            self.backlog = []          # fresh console per job (no stale replay)
        self.publish({"type":"status","event":"started","kind":kind,
                      "argv":argv,"ts":time.time()})
        threading.Thread(target=self._reader, args=(self.proc,kind), daemon=True).start()
        return True, "started"

    def _reader(self, proc, kind):
        try:
            for line in proc.stdout:
                self.publish({"type":"line","kind":kind,"text":ANSI.sub("", line.rstrip("\n"))})
        except Exception:
            pass
        code = proc.wait()
        self.publish({"type":"status","event":"finished","kind":kind,"code":code,"ts":time.time()})

    def stop(self):
        with self.lock:
            if not self.running():
                return False, "no job running"
            p = self.proc
        # SIGINT to the whole process group so btlab.py's cleanup restores the radio
        try: os.killpg(os.getpgid(p.pid), signal.SIGINT)
        except Exception:
            try: p.send_signal(signal.SIGINT)
            except Exception: pass
        for _ in range(50):
            if p.poll() is not None: break
            time.sleep(0.1)
        if p.poll() is None:
            try: os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception: p.terminate()
        return True, "stopped"

JOBS = Jobs()

# ---------------------------------------------------------------- capture conversion
_CONVERT_FAILED = set()     # paths whose conversion errored - don't retry endlessly

def convert_capture(btsnoop_path, cap_kind):
    """Convert a raw .btsnoop into the .pcap deliverable, picking the converter
    that is actually correct for the capture kind:

      - "ble":     btle_pcap.py RECONSTRUCTS BLE Link-Layer PDUs from the HCI
                   Advertising Report fields and wraps them with the RF
                   pseudo-header (LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR) so
                   per-packet RSSI survives into Wireshark - the format a
                   dedicated sniffer emits.
      - "classic": btclassic_pcap.py does a LOSSLESS container swap only
                   (LINKTYPE_BLUETOOTH_LINUX_MONITOR) - every HCI record
                   preserved byte-for-byte, nothing invented. Genuine Classic
                   baseband PDUs cannot be reconstructed from an Inquiry
                   Result, so that is deliberately not attempted.

    Returns the .pcap path, or None if the source is missing / conversion
    failed. Safe to call repeatedly: returns the existing .pcap if present.
    """
    if not btsnoop_path or not os.path.exists(btsnoop_path):
        return None
    pcap_path = (btsnoop_path[:-len(".btsnoop")] + ".pcap"
                 if btsnoop_path.endswith(".btsnoop") else btsnoop_path + ".pcap")
    if os.path.exists(pcap_path):
        return pcap_path
    if btsnoop_path in _CONVERT_FAILED:
        return None
    converter = BTCLASSIC_PCAP if cap_kind == "classic" else BTLE_PCAP
    try:
        r = subprocess.run([sys.executable, converter, btsnoop_path, pcap_path],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and os.path.exists(pcap_path):
            return pcap_path
        _CONVERT_FAILED.add(btsnoop_path)
    except Exception:
        _CONVERT_FAILED.add(btsnoop_path)
    return None


# ---------------------------------------------------------------- capture manager
class Capture:
    """Full HCI btsnoop capture via `btmon -w`.

    Independent of the single-job lock: btmon is a passive kernel monitor that
    also sees HCI-User-Channel traffic, so a capture can run CONCURRENTLY with
    any adv/interfere/sniff/profile job and record exactly what it does.

    Two capture kinds, each producing the artifact that's actually correct for
    it - not one format forced onto both:
      - "ble":     on stop, the raw .btsnoop is converted to a genuine BLE
                   Link-Layer .pcap (btle_pcap.py) - Wireshark shows it exactly
                   as a dedicated OTA sniffer would (Protocol=LE LL, Info=
                   ADV_IND/SCAN_RSP/... directly). The raw .btsnoop is kept on
                   disk as source data but not listed/served.
      - "classic": no LE-LL conversion applies (there's nothing BLE in it) -
                   the raw .btsnoop IS the deliverable. Wireshark decodes
                   Classic Inquiry Results/EIR natively with full field
                   breakdown; there is no dedicated-sniffer-hardware format to
                   reconstruct without real BR/EDR baseband capture equipment,
                   so this is offered as-is rather than faked.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.file = None
        self.started = 0
        self.cap_kind = "ble"      # "ble" or "classic" - set at start(), read at stop()
        self.linked_job = None     # set when capture was started together with a job (e.g. "sniff")
        try: os.makedirs(CAPTURE_DIR, exist_ok=True)
        except Exception: pass

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, cap_kind="ble"):
        with self.lock:
            if self.running():
                return False, "capture already running", os.path.basename(self.file)
            ts = time.strftime("%Y%m%d_%H%M%S")
            suffix = "_classic" if cap_kind == "classic" else ""
            fname = f"bt_{ts}{suffix}.btsnoop"
            path = os.path.join(CAPTURE_DIR, fname)
            try:
                # btcapture.py, NOT `btmon -w`: BlueZ 5.77's btmon crashes with a
                # glibc buffer overflow while decoding live advertising traffic,
                # which silently truncated every capture to ~3 KB of bookkeeping
                # with zero advertising reports. Our recorder copies the raw
                # monitor stream without decoding, so it cannot be crashed by a
                # malformed packet.
                self.proc = subprocess.Popen(
                    [sys.executable, BTCAPTURE, "-w", path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
            except Exception as e:
                return False, f"capture recorder failed to start: {e}", None
            self.file, self.started, self.cap_kind = path, time.time(), cap_kind
            return True, "capture started", fname

    def stop(self):
        with self.lock:
            p, f, cap_kind = self.proc, self.file, self.cap_kind
            alive = self.running()
        if not f:
            return False, "no capture running", None

        if alive:
            try: os.killpg(os.getpgid(p.pid), signal.SIGINT)
            except Exception:
                try: p.send_signal(signal.SIGINT)
                except Exception: pass
            for _ in range(30):
                if p.poll() is not None: break
                time.sleep(0.1)
            if p.poll() is None:
                try: os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except Exception: p.terminate()

        # IMPORTANT: conversion runs even when btmon already exited on its own.
        # The previous code returned early on "not running" and left the raw
        # .btsnoop orphaned with no .pcap - which is exactly the failure the
        # user hit (several captures on disk as .btsnoop only). The deliverable
        # is always a .pcap, so always attempt the conversion.
        pcap_file = convert_capture(f, cap_kind)
        with self.lock:
            self.proc = None
        # if conversion failed for any reason, fall back to the raw .btsnoop
        # so the capture is never silently lost
        deliverable = pcap_file or f
        size = os.path.getsize(deliverable) if deliverable and os.path.exists(deliverable) else 0
        return True, "capture stopped", {"file": os.path.basename(deliverable) if deliverable else None, "size": size}

    def status(self):
        r = self.running()
        size = os.path.getsize(self.file) if r and self.file and os.path.exists(self.file) else None
        return {"running": r,
                "file": os.path.basename(self.file) if r and self.file else None,
                "size": size,
                "cap_kind": self.cap_kind if r else None,
                "uptime": round(time.time()-self.started, 1) if r else None,
                "linked_job": self.linked_job if r else None}

    def start_with_job(self, cap_kind, job_kind, argv):
        """Start the capture (tagged "ble" or "classic"), then start a
        coordinated job (e.g. 'sniff') so the .btsnoop is guaranteed to
        contain real over-the-air activity instead of just btmon's
        adapter-open bookkeeping frames (System Note/New Index/...) - which
        is what an unpaired Start-Capture-with-nothing-scanning produces."""
        ok, msg, fname = self.start(cap_kind)
        if not ok:
            return False, msg, None
        time.sleep(0.3)   # let btmon attach to the HCI monitor before the job generates traffic
        ok2, msg2 = JOBS.start(job_kind, argv)
        if not ok2:
            self.stop()
            return False, msg2, None
        self.linked_job = job_kind
        return True, f"capture+{job_kind} started", fname

    def stop_linked(self):
        """Stop the coordinated job first (cleanly disables scanning - that
        disable command is worth capturing too), then stop the capture."""
        was_linked = self.linked_job
        if was_linked:
            JOBS.stop()
            time.sleep(0.3)
        ok, msg, info = self.stop()
        self.linked_job = None
        return ok, msg, info

    def list(self):
        try: names = set(os.listdir(CAPTURE_DIR))
        except OSError: names = set()

        # Self-heal: convert any raw .btsnoop that has no .pcap yet. This
        # recovers captures whose btmon exited unexpectedly (so stop() never
        # ran its conversion) and guarantees the GUI only ever offers .pcap.
        cur = os.path.basename(self.file) if (self.running() and self.file) else None
        for fn in sorted(n for n in names if n.endswith(".btsnoop")):
            if fn == cur:                                  # still being written
                continue
            if fn[:-len(".btsnoop")] + ".pcap" in names:   # already converted
                continue
            k = "classic" if fn.endswith("_classic.btsnoop") else "ble"
            if convert_capture(os.path.join(CAPTURE_DIR, fn), k):
                names.add(fn[:-len(".btsnoop")] + ".pcap")

        out = []
        for fn in sorted(names, reverse=True):
            # check the more specific classic patterns first - "_classic.pcap"
            # also ends with ".pcap" and would otherwise be mislabeled "ble"
            if fn.endswith("_classic.btsnoop"):
                # raw source for a classic capture - only surface it if the
                # .pcap conversion never happened (failed), so a successful
                # session shows ONE row, not the deliverable plus its leftover
                # source file
                if fn[:-len(".btsnoop")] + ".pcap" in names:
                    continue
                kind = "classic"
            elif fn.endswith("_classic.pcap"):
                kind = "classic"
            elif fn.endswith(".btsnoop"):
                # same fallback logic for the ble side
                if fn[:-len(".btsnoop")] + ".pcap" in names:
                    continue
                kind = "ble"
            elif fn.endswith(".pcap"):
                kind = "ble"
            else:
                continue
            fp = os.path.join(CAPTURE_DIR, fn)
            try: st = os.stat(fp)
            except OSError: continue
            out.append({"file": fn, "kind": kind, "size": st.st_size, "mtime": st.st_mtime})
        return out

CAP = Capture()

# ---------------------------------------------------------------- argv builders
def _opt(a, flag, v):
    if v is None: return
    if isinstance(v, str) and v.strip() == "": return
    a += [flag, str(v)]
def _flag(a, flag, v):
    if v: a += [flag]

def build_argv(tool, p):
    py = [sys.executable, "-u", BTLAB]
    if tool == "adv":
        t = p.get("type", "ind")
        a = py + ["adv", t]
        _opt(a, "-i", p.get("interval"))
        _opt(a, "--interval-min", p.get("interval_min"))
        _opt(a, "--interval-max", p.get("interval_max"))
        _opt(a, "-c", p.get("channels"))
        _opt(a, "--own", p.get("own"))
        _opt(a, "--random-addr", p.get("random_addr"))
        _opt(a, "-p", p.get("peer"))
        _opt(a, "--peer-type", p.get("peer_type"))
        _opt(a, "-n", p.get("name"))
        _opt(a, "-d", p.get("data"))
        _opt(a, "--scan-resp", p.get("scan_resp"))
        _opt(a, "--flags", p.get("flags"))
        _flag(a, "--no-flags", p.get("no_flags"))
        _opt(a, "-t", p.get("duration"))
        return a
    if tool == "interfere":
        mode = p.get("mode", "auto")
        a = py + ["interfere", mode]
        arg = p.get("arg")
        if mode in ("wifi","single","sweep","hop") and arg not in (None,""):
            a.append(str(arg))
        _opt(a, "-w", p.get("bw"))
        _opt(a, "-l", p.get("len"))
        _opt(a, "-P", p.get("payload"))
        _opt(a, "--phy", p.get("phy"))
        _opt(a, "-D", p.get("dwell"))
        _opt(a, "--power", p.get("power"))
        _opt(a, "--scan-iface", p.get("scan_iface"))
        _opt(a, "--retarget", p.get("retarget"))
        _opt(a, "-i", p.get("interval"))            # adv-flood
        _opt(a, "--adv-channels", p.get("adv_channels"))
        _opt(a, "-t", p.get("duration"))
        _flag(a, "--map", p.get("map"))
        return a
    if tool == "sniff":
        a = py + ["sniff", "--json"]
        _opt(a, "--mode", p.get("mode"))          # "le" (default) or "classic"
        _flag(a, "--active", p.get("active"))
        _opt(a, "--interval", p.get("interval"))
        _opt(a, "--window", p.get("window"))
        _flag(a, "--dedup", p.get("dedup"))       # default OFF (catch everything); opt-in to filter
        _opt(a, "--filter", p.get("filter"))
        _opt(a, "-t", p.get("duration"))
        return a
    if tool == "coex":
        a = ["bash", COEX, "--iface", str(p.get("iface","")),]
        _opt(a, "--server", p.get("server"))
        _opt(a, "--time", p.get("time"))
        _opt(a, "--bw", p.get("bw"))
        _flag(a, "--reverse", p.get("reverse"))
        _flag(a, "--udp", p.get("udp"))
        _flag(a, "--dry-run", p.get("dry_run"))
        extra = (p.get("extra") or "").split()
        if extra: a += ["--"] + extra
        return a
    raise ValueError(f"unknown tool {tool}")

# ---------------------------------------------------------------- capabilities cache
_CAPS = {"data": None}
def get_caps(refresh=False):
    if _CAPS["data"] is not None and not refresh:
        return _CAPS["data"]
    try:
        out = subprocess.run([sys.executable, BTLAB, "caps", "--compact"],
                             capture_output=True, text=True, timeout=15).stdout
        _CAPS["data"] = json.loads(out)
    except Exception as e:
        _CAPS["data"] = {"error": str(e)}
    return _CAPS["data"]

# ---------------------------------------------------------------- HTTP handler
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    def log_message(self, *a): pass    # quiet

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)): body = json.dumps(body).encode()
        elif isinstance(body, str): body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        # Never let a browser cache the GUI or API responses - this app changes
        # under active development and a stale cached gui.html silently running
        # old JS against the new backend is a real, hard-to-diagnose failure mode.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        try: self.wfile.write(body)
        except Exception: pass

    def _send_file(self, basename, download_name=None):
        # basename only - never trust a path from the query string
        safe = os.path.basename(basename or "")
        if not (safe.endswith(".pcap") or safe.endswith(".btsnoop")):
            return self._send(400, {"error": "only .pcap/.btsnoop files"})
        path = os.path.join(CAPTURE_DIR, safe)
        if not os.path.isfile(path):
            return self._send(404, {"error": "no such capture"})
        try:
            with open(path, "rb") as f: data = f.read()
        except Exception as e:
            return self._send(500, {"error": str(e)})
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{download_name or safe}"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try: self.wfile.write(data)
        except Exception: pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with open(GUI, "rb") as f: data = f.read()
                self._send(200, data.decode(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, f"gui.html missing: {e}", "text/plain")
        elif path == "/api/caps":
            refresh = "refresh=1" in self.path
            self._send(200, get_caps(refresh))
        elif path == "/api/status":
            self._send(200, JOBS.status())
        elif path == "/api/stream":
            self._stream()
        elif path == "/api/capture/status":
            self._send(200, CAP.status())
        elif path == "/api/capture/list":
            self._send(200, CAP.list())
        elif path == "/api/insight":
            # Offline insight engine (btinsight.py): device intelligence,
            # privacy/tracking analysis, RF & coexistence health. Runs against
            # the RAW .btsnoop (it carries RSSI + timing the LL pcap can't
            # fully express), so map the requested .pcap back to its source.
            qs = parse_qs(urlparse(self.path).query)
            name = os.path.basename((qs.get("file") or [""])[0])
            if not (name.endswith(".pcap") or name.endswith(".btsnoop")):
                return self._send(400, {"error": "only .pcap/.btsnoop files"})
            raw = name[:-len(".pcap")] + ".btsnoop" if name.endswith(".pcap") else name
            raw_path = os.path.join(CAPTURE_DIR, raw)
            if not os.path.isfile(raw_path):
                return self._send(404, {"error": f"no raw capture for {name}"})
            try:
                r = subprocess.run([sys.executable, BTINSIGHT, raw_path, "--json"],
                                   capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    return self._send(500, {"error": (r.stderr or "insight failed")[:400]})
                self._send(200, json.loads(r.stdout))
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/api/capture/download":
            qs = parse_qs(urlparse(self.path).query)
            self._send_file((qs.get("file") or [""])[0])
        else:
            self._send(404, {"error":"not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try: body = json.loads(raw or b"{}")
        except Exception: body = {}
        path = self.path.split("?")[0]
        if path == "/api/start":
            tool = body.get("tool"); params = body.get("params", {})
            if tool not in ("adv","interfere","sniff","coex"):
                return self._send(400, {"ok":False,"error":"bad tool"})
            try: argv = build_argv(tool, params)
            except Exception as e: return self._send(400, {"ok":False,"error":str(e)})
            ok, msg = JOBS.start(tool, argv)
            self._send(200 if ok else 409, {"ok":ok,"msg":msg,"argv":argv})
        elif path == "/api/stop":
            ok, msg = JOBS.stop()
            self._send(200, {"ok":ok,"msg":msg})
        elif path == "/api/capture/start":
            ok, msg, f = CAP.start()
            self._send(200 if ok else 409, {"ok":ok,"msg":msg,"file":f})
        elif path == "/api/capture/stop":
            ok, msg, info = CAP.stop()
            self._send(200 if ok else 409, {"ok":ok,"msg":msg,"info":info})
        elif path == "/api/capture/sniff/start":
            params = body.get("params", {})
            cap_kind = "classic" if params.get("mode") == "classic" else "ble"
            try: argv = build_argv("sniff", params)
            except Exception as e: return self._send(400, {"ok":False,"error":str(e)})
            ok, msg, f = CAP.start_with_job(cap_kind, "sniff", argv)
            self._send(200 if ok else 409, {"ok":ok,"msg":msg,"file":f})
        elif path == "/api/capture/sniff/stop":
            ok, msg, info = CAP.stop_linked()
            self._send(200 if ok else 409, {"ok":ok,"msg":msg,"info":info})
        else:
            self._send(404, {"error":"not found"})

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q, backlog = JOBS.subscribe()
        def write(evt):
            self.wfile.write(f"data: {json.dumps(evt)}\n\n".encode()); self.wfile.flush()
        try:
            write({"type":"status","event":"hello","status":JOBS.status()})
            for evt in backlog: write(evt)
            while True:
                try: evt = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n"); self.wfile.flush(); continue
                write(evt)
        except Exception:
            pass
        finally:
            JOBS.unsubscribe(q)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    if os.geteuid() != 0:
        print("[!] not root - RF jobs will fail. Start with sudo.", file=sys.stderr)
    # Bind the HTTP port FIRST so the GUI is reachable immediately on boot.
    srv = ThreadingHTTPServer((a.host, a.port), H)
    try: ip = socket.gethostbyname(socket.gethostname())
    except Exception: ip = a.host
    print(f"[+] BLE Lab GUI on http://{a.host}:{a.port}  (try http://{ip}:{a.port})", flush=True)
    # Warm the capability cache in the background (it briefly probes the radio).
    threading.Thread(target=get_caps, daemon=True).start()
    try: srv.serve_forever()
    except KeyboardInterrupt:
        JOBS.stop(); print("\n[+] bye")

if __name__ == "__main__":
    main()
