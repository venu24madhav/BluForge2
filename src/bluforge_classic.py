#!/usr/bin/env python3
"""
bluforge_classic.py - Bluetooth Classic (BR/EDR) test engine: device
management, A2DP source streaming, AVRCP and link metrics.

Unlike bluforge_cli.py (which takes a controller away from BlueZ with
HCI_CHANNEL_USER to drive raw LE test commands), everything here runs on a
controller that stays UNDER BlueZ/bluetoothd, because Classic profiles need
the real stack: pairing, SDP, A2DP endpoints, AVRCP. So a radio is either a
BLE-engine radio or a Classic radio, never both at once (server.py enforces
that with a reservation).

Role: this host is the A2DP SOURCE / AVRCP TARGET / HFP AUDIO GATEWAY and the
DUT is the headset/speaker. Audio goes through the PipeWire session of the
desktop user (WirePlumber owns the A2DP endpoints); every PipeWire tool is
run as that user via runuser when this process is root.

Output protocol (consumed by server.py / the GUI):
  [*] [+] [!] [x]   human progress lines (same convention as bluforge_cli.py)
  {"m": "...", ...}  machine metric/event lines (long-running jobs)
  last line          {"ok": true|false, ...} result of a one-shot action

Dependencies: Python 3 stdlib + PyGObject (gi.repository.Gio, preinstalled on
Fedora/RHEL/Debian desktops). Optional, degrade gracefully: hcitool/hciconfig
(bluez-deprecated) for RSSI/TX-power/link-quality/throughput, pactl/pw-cat/
pw-top (PipeWire) for A2DP audio.
"""
import argparse
import csv
import datetime
import io
import json
import math
import os
import pwd
import random
import re
import select
import shutil
import socket
import signal
import struct
import subprocess
import sys
import threading
import time

try:
    from gi.repository import Gio, GLib
    HAVE_GI = True
except Exception as _e:          # reported by `preflight`, never a traceback
    HAVE_GI = False
    _GI_ERR = str(_e)

HERE = os.path.dirname(os.path.abspath(__file__))
import ble_decode
import bt_traffic
import durable
from version import __version__, RESULT_SCHEMA
LOG_DIR = os.environ.get("BLUFORGE_LOGS") or os.path.join(os.path.dirname(HERE), "logs")
ADDR_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

BLUEZ = "org.bluez"
IF_ADAPTER = "org.bluez.Adapter1"
IF_DEVICE = "org.bluez.Device1"
IF_TRANSPORT = "org.bluez.MediaTransport1"
IF_PLAYER = "org.bluez.MediaPlayer1"
IF_PROPS = "org.freedesktop.DBus.Properties"
IF_OM = "org.freedesktop.DBus.ObjectManager"

STOP = threading.Event()


def _on_signal(*_):
    STOP.set()


def say(tag, msg):
    print("[%s] %s" % (tag, msg), flush=True)


def emit(**kw):
    print(json.dumps(kw, separators=(",", ":"), default=str), flush=True)


class ClassicError(Exception):
    pass


def die(msg):
    raise ClassicError(msg)


def norm_addr(a):
    if not a or not ADDR_RE.match(a):
        die("bad Bluetooth address %r (expected AA:BB:CC:DD:EE:FF)" % a)
    return a.upper()


# ============================================================ BlueZ D-Bus
def _bz_error_text(e):
    m = re.search(r"GDBus\.Error:([\w.]+): (.*?)(?: \(\d+\))?$", str(e), re.S)
    if m:
        return "%s: %s" % (m.group(1).replace("org.bluez.Error.", ""), m.group(2).strip())
    return str(e)


class BZ:
    """Thin synchronous wrapper over the system bus for one adapter."""

    def __init__(self, hci):
        if not HAVE_GI:
            die("python3 PyGObject (gi) is not installed: %s" % _GI_ERR)
        self.hci = hci
        self.adapter = "/org/bluez/hci%d" % hci
        try:
            self.conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as e:
            die("cannot reach the system D-Bus: %s" % e)
        if not self.exists(self.adapter):
            die("hci%d is not managed by bluetoothd (is bluetooth.service running, "
                "and is this radio free of a BLE job?)" % hci)

    def dev_path(self, addr):
        return "%s/dev_%s" % (self.adapter, addr.replace(":", "_"))

    def call(self, path, iface, method, params=None, timeout_ms=30000):
        try:
            r = self.conn.call_sync(BLUEZ, path, iface, method, params, None,
                                    Gio.DBusCallFlags.NONE, timeout_ms, None)
        except GLib.Error as e:
            raise ClassicError(_bz_error_text(e))
        return r.unpack() if r is not None else None

    def call_loop(self, path, iface, method, params, timeout_ms):
        """Async call driven by a GLib loop so org.bluez.Agent1 callbacks
        (PIN / confirmation) can be served on this thread while it waits --
        a plain call_sync would deadlock pairing against our own agent."""
        loop = GLib.MainLoop()
        res = {}

        def done(src, task):
            try:
                res["v"] = src.call_finish(task)
            except GLib.Error as e:
                res["e"] = e
            loop.quit()
        self.conn.call(BLUEZ, path, iface, method, params, None,
                       Gio.DBusCallFlags.NONE, timeout_ms, None, done)
        GLib.timeout_add(timeout_ms + 3000, lambda: (loop.quit(), False)[1])
        loop.run()
        if "e" in res:
            raise ClassicError(_bz_error_text(res["e"]))
        if "v" not in res:
            die("timed out waiting for %s" % method)
        return res["v"].unpack()

    def exists(self, path):
        """True if `path` (an adapter or device object) is currently exported."""
        iface = IF_ADAPTER if path == self.adapter else IF_DEVICE
        try:
            self.call(path, IF_PROPS, "Get", GLib.Variant("(ss)", (iface, "Address")))
            return True
        except ClassicError:
            return False

    def props(self, path, iface):
        try:
            return self.call(path, IF_PROPS, "GetAll", GLib.Variant("(s)", (iface,)))[0]
        except ClassicError:
            return None

    def get(self, path, iface, name, default=None):
        try:
            return self.call(path, IF_PROPS, "Get", GLib.Variant("(ss)", (iface, name)))[0]
        except ClassicError:
            return default

    def set(self, path, iface, name, variant):
        self.call(path, IF_PROPS, "Set", GLib.Variant("(ssv)", (iface, name, variant)))

    def managed(self):
        return self.call("/", IF_OM, "GetManagedObjects")[0]

    def device_objects(self, addr):
        """{path: {iface: props}} for the device object and all its children
        (MediaTransport1, MediaPlayer1, MediaControl1, Battery1 ...)."""
        dp = self.dev_path(addr)
        return {p: i for p, i in self.managed().items() if p == dp or p.startswith(dp + "/")}

    def transport(self, addr):
        for p, ifs in self.device_objects(addr).items():
            if IF_TRANSPORT in ifs:
                return p, ifs[IF_TRANSPORT]
        return None, None

    def player(self, addr):
        for p, ifs in self.device_objects(addr).items():
            if IF_PLAYER in ifs:
                return p, ifs[IF_PLAYER]
        return None, None

    def subscribe(self, handler):
        """Subscribe to PropertiesChanged + InterfacesAdded/Removed under
        org.bluez; handler(kind, path, a, b). Pump with pump()."""
        def props_cb(conn, sender, path, iface, sig, params, *_):
            i, changed, _inv = params.unpack()
            handler("props", path, i, changed)

        def add_cb(conn, sender, path, iface, sig, params, *_):
            p, ifs = params.unpack()
            handler("added", p, list(ifs.keys()), ifs)

        def rem_cb(conn, sender, path, iface, sig, params, *_):
            p, ifs = params.unpack()
            handler("removed", p, list(ifs), None)
        self.conn.signal_subscribe(BLUEZ, IF_PROPS, "PropertiesChanged", None, None,
                                   Gio.DBusSignalFlags.NONE, props_cb)
        self.conn.signal_subscribe(BLUEZ, IF_OM, "InterfacesAdded", None, None,
                                   Gio.DBusSignalFlags.NONE, add_cb)
        self.conn.signal_subscribe(BLUEZ, IF_OM, "InterfacesRemoved", None, None,
                                   Gio.DBusSignalFlags.NONE, rem_cb)


def pump(seconds):
    """Sleep `seconds` while dispatching D-Bus signal callbacks; wakes early
    when a stop was requested."""
    ctx = GLib.MainContext.default()
    end = time.time() + seconds
    while time.time() < end and not STOP.is_set():
        while ctx.iteration(False):
            pass
        time.sleep(0.05)


# ============================================================ pairing agent
AGENT_PATH = "/org/bluforge/agent"
AGENT_XML = """<node><interface name='org.bluez.Agent1'>
<method name='Release'/>
<method name='RequestPinCode'><arg type='o' direction='in'/><arg type='s' direction='out'/></method>
<method name='DisplayPinCode'><arg type='o' direction='in'/><arg type='s' direction='in'/></method>
<method name='RequestPasskey'><arg type='o' direction='in'/><arg type='u' direction='out'/></method>
<method name='DisplayPasskey'><arg type='o' direction='in'/><arg type='u' direction='in'/><arg type='q' direction='in'/></method>
<method name='RequestConfirmation'><arg type='o' direction='in'/><arg type='u' direction='in'/></method>
<method name='RequestAuthorization'><arg type='o' direction='in'/></method>
<method name='AuthorizeService'><arg type='o' direction='in'/><arg type='s' direction='in'/></method>
<method name='Cancel'/></interface></node>"""


class Agent:
    """NoInputNoOutput BlueZ agent: SSP 'just works' devices pair silently,
    legacy-PIN devices get `pin`, confirmations/authorisations are accepted.
    This is a LAB tool for a controlled testbed: it deliberately auto-accepts."""

    def __init__(self, bz, pin="0000"):
        self.bz = bz
        self.pin = pin
        self.reg_id = None

    def _method(self, conn, sender, path, iface, method, params, inv):
        a = params.unpack()
        say("*", "agent: %s %s" % (method, " ".join(str(x) for x in a[1:]) if len(a) > 1 else ""))
        if method == "RequestPinCode":
            inv.return_value(GLib.Variant("(s)", (self.pin,)))
        elif method == "RequestPasskey":
            inv.return_value(GLib.Variant("(u)", (int(self.pin) if self.pin.isdigit() else 0,)))
        else:
            inv.return_value(None)

    def __enter__(self):
        info = Gio.DBusNodeInfo.new_for_xml(AGENT_XML).interfaces[0]
        self.reg_id = self.bz.conn.register_object(AGENT_PATH, info, self._method, None, None)
        self.bz.call("/org/bluez", "org.bluez.AgentManager1", "RegisterAgent",
                     GLib.Variant("(os)", (AGENT_PATH, "NoInputNoOutput")))
        try:
            self.bz.call("/org/bluez", "org.bluez.AgentManager1", "RequestDefaultAgent",
                         GLib.Variant("(o)", (AGENT_PATH,)))
        except ClassicError as e:
            say("!", "could not become default agent (%s); continuing" % e)
        return self

    def __exit__(self, *a):
        try:
            self.bz.call("/org/bluez", "org.bluez.AgentManager1", "UnregisterAgent",
                         GLib.Variant("(o)", (AGENT_PATH,)))
        except ClassicError:
            pass
        if self.reg_id:
            self.bz.conn.unregister_object(self.reg_id)


# ============================================================ helpers
def human_dev(p):
    return {
        "addr": p.get("Address"), "name": p.get("Alias") or p.get("Name") or "",
        "class": "0x%06x" % p["Class"] if "Class" in p else None,
        "icon": p.get("Icon"), "rssi": p.get("RSSI"),
        "paired": bool(p.get("Paired")), "trusted": bool(p.get("Trusted")),
        "connected": bool(p.get("Connected")),
        "uuids": [uuid_name(u) for u in p.get("UUIDs", [])],
        "modalias": p.get("Modalias"),
    }


UUID_NAMES = {
    "0000110a": "A2DP Source", "0000110b": "A2DP Sink", "0000110c": "AVRCP Target",
    "0000110d": "A2DP", "0000110e": "AVRCP Controller", "0000110f": "AVRCP",
    "00001108": "Headset (HSP)", "00001112": "Headset AG (HSP)", "0000111e": "Handsfree (HFP)",
    "0000111f": "Handsfree AG (HFP)", "00001200": "PnP", "00001124": "HID", "00001115": "PAN",
    "00001116": "NAP", "0000180f": "Battery Service", "00001800": "GAP", "00001801": "GATT",
}


def uuid_name(u):
    n = UUID_NAMES.get(u[:8].lower())
    return "%s (%s)" % (n, u[:8]) if n else u


def classic_devices(bz, only_bredr=True):
    out = []
    for p, ifs in bz.managed().items():
        if IF_DEVICE in ifs and p.startswith(bz.adapter + "/dev_"):
            pr = ifs[IF_DEVICE]
            if only_bredr and pr.get("AddressType") == "random":
                continue
            out.append(human_dev(pr))
    return sorted(out, key=lambda d: (not d["connected"], not d["paired"], d["name"]))


def adapter_info(bz):
    p = bz.props(bz.adapter, IF_ADAPTER) or {}
    return {"hci": bz.hci, "addr": p.get("Address"), "name": p.get("Alias"),
            "powered": p.get("Powered"), "pairable": p.get("Pairable"),
            "discoverable": p.get("Discoverable"), "discovering": p.get("Discovering"),
            "class": "0x%06x" % p["Class"] if "Class" in p else None,
            "modalias": p.get("Modalias")}


def ensure_powered(bz):
    if not bz.get(bz.adapter, IF_ADAPTER, "Powered"):
        say("*", "powering on hci%d" % bz.hci)
        bz.set(bz.adapter, IF_ADAPTER, "Powered", GLib.Variant("b", True))
        for _ in range(20):
            if bz.get(bz.adapter, IF_ADAPTER, "Powered"):
                break
            time.sleep(0.25)
        else:
            die("hci%d would not power on" % bz.hci)


def discover(bz, seconds, transport="bredr", want=None):
    """Run inquiry for `seconds` (or until `want` shows up). Returns devices."""
    ensure_powered(bz)
    flt = {"Transport": GLib.Variant("s", transport), "DuplicateData": GLib.Variant("b", False)}
    try:
        bz.call(bz.adapter, IF_ADAPTER, "SetDiscoveryFilter", GLib.Variant("(a{sv})", (flt,)))
    except ClassicError as e:
        say("!", "discovery filter rejected (%s); using unfiltered inquiry" % e)
    started = False
    try:
        try:
            bz.call(bz.adapter, IF_ADAPTER, "StartDiscovery")
            started = True
        except ClassicError as e:
            if "InProgress" in str(e):
                say("!", "discovery already running on hci%d; joining it" % bz.hci)
            else:
                raise
        end = time.time() + seconds
        while time.time() < end and not STOP.is_set():
            time.sleep(0.5)
            if want and bz.exists(bz.dev_path(want)):
                break
    finally:
        if started:
            try:
                bz.call(bz.adapter, IF_ADAPTER, "StopDiscovery")
            except ClassicError:
                pass
    return classic_devices(bz, only_bredr=(transport == "bredr"))


def ensure_device(bz, addr, scan_s=20):
    if not bz.exists(bz.dev_path(addr)):
        say("*", "%s not known to hci%d yet - running inquiry (up to %ds)" % (addr, bz.hci, scan_s))
        discover(bz, scan_s, want=addr)
        if not bz.exists(bz.dev_path(addr)):
            die("%s was not found by hci%d (powered on? in range? in pairing mode?)" % (addr, bz.hci))


# ============================================================ link metrics
def _run(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except Exception as e:
        return 127, str(e)


def link_stats(hci, addr):
    """RSSI / TX power / link quality of the ACL link via hcitool (legacy
    bluez-deprecated tool; returns None fields when it is absent)."""
    out = {"rssi": None, "tx_power": None, "lq": None}
    if not shutil.which("hcitool"):
        return out
    for key, sub, pat in (("rssi", "rssi", r"RSSI return value:\s*(-?\d+)"),
                          ("tx_power", "tpl", r"transmit power level:\s*(-?\d+)"),
                          ("lq", "lq", r"Link quality:\s*(\d+)")):
        rc, txt = _run(["hcitool", "-i", "hci%d" % hci, sub, addr], 3)
        m = re.search(pat, txt)
        if m:
            out[key] = int(m.group(1))
    return out


AFH_MAP_RE = re.compile(r"AFH map:\s*0x([0-9a-fA-F]{20})")


def parse_afh(text):
    """Parse `hcitool afh <addr>` output into {"enabled", "channels", "map_hex", "unused"}, or None.
    The controller reports a 79-bit map (10 octets): bit b of octet i is Classic channel 8*i+b, 1 = in use."""
    if "AFH disabled" in text:
        return {"enabled": False, "channels": 79, "map_hex": None, "unused": []}
    m = AFH_MAP_RE.search(text)
    if not m:
        return None
    raw = bytes.fromhex(m.group(1))
    used = [8 * i + b for i in range(10) for b in range(8) if 8 * i + b < 79 and raw[i] >> b & 1]
    return {"enabled": True, "channels": len(used), "map_hex": m.group(1),
            "unused": [c for c in range(79) if c not in used]}


def afh_info(hci, addr):
    """Which of the 79 Classic hop channels this ACL link is using right now (AFH), via hcitool; None when unknown."""
    if not shutil.which("hcitool"):
        return None
    rc, txt = _run(["hcitool", "-i", "hci%d" % hci, "afh", addr], 4)
    return parse_afh(txt)


def acl_links(hci):
    """Links currently up on this controller: [{"type": "ACL"|"SCO"|"ESCO", "addr", "handle", "role"}]."""
    if not shutil.which("hcitool"):
        return []
    rc, txt = _run(["hcitool", "-i", "hci%d" % hci, "con"], 3)
    links = []
    for m in re.finditer(r"([<>])\s+(ACL|SCO|ESCO)\s+([0-9A-F:]{17})\s+handle\s+(\d+)\s+state\s+\d+\s+lm\s+(\w+)", txt, re.I):
        links.append({"type": m.group(2).upper(), "addr": m.group(3).upper(), "handle": int(m.group(4)), "role": m.group(5)})
    return links


def hci_counters(hci):
    """(tx_bytes, rx_bytes, tx_acl_pkts, rx_acl_pkts) from hciconfig, or None."""
    if not shutil.which("hciconfig"):
        return None
    rc, txt = _run(["hciconfig", "hci%d" % hci], 3)
    rx = re.search(r"RX bytes:(\d+) acl:(\d+)", txt)
    tx = re.search(r"TX bytes:(\d+) acl:(\d+)", txt)
    if not (rx and tx):
        return None
    return int(tx.group(1)), int(rx.group(1)), int(tx.group(2)), int(rx.group(2))


# ============================================================ codec decode
VENDOR_CODECS = {(0x004F, 0x0001): "aptX", (0x00D7, 0x0024): "aptX-HD", (0x012D, 0x00AA): "LDAC",
                 (0x000A, 0x0002): "aptX-LL"}


def codec_name(codec, conf):
    if codec == 0:
        return "SBC"
    if codec == 1:
        return "MPEG-1,2 (MP3)"
    if codec == 2:
        return "AAC"
    if codec == 0xFF and conf and len(conf) >= 6:
        vid = conf[0] | (conf[1] << 8)
        cid = conf[4] | (conf[5] << 8)
        return VENDOR_CODECS.get((vid, cid), "vendor 0x%04x/0x%04x" % (vid, cid))
    return "codec 0x%02x" % codec


def decode_sbc(conf):
    """Negotiated SBC config -> dict incl. an upper-bound bitrate estimate."""
    if not conf or len(conf) < 4:
        return {}
    b0, b1, minbp, maxbp = conf[0], conf[1], conf[2], conf[3]
    freq = next((f for bit, f in ((0x80, 16000), (0x40, 32000), (0x20, 44100), (0x10, 48000)) if b0 & bit), None)
    mode = next((m for bit, m in ((0x08, "mono"), (0x04, "dual"), (0x02, "stereo"), (0x01, "joint")) if b0 & bit), None)
    blocks = next((n for bit, n in ((0x80, 16), (0x40, 12), (0x20, 8), (0x10, 4)) if b1 & bit), None)
    sub = next((n for bit, n in ((0x08, 8), (0x04, 4)) if b1 & bit), None)
    out = {"freq": freq, "mode": mode, "blocks": blocks, "subbands": sub,
           "bitpool_min": minbp, "bitpool_max": maxbp}
    if freq and mode and blocks and sub:
        ch = 1 if mode == "mono" else 2
        base = 4 + (4 * sub * ch) / 8.0
        if mode in ("mono", "dual"):
            flen = base + math.ceil(blocks * ch * maxbp / 8.0)
        elif mode == "stereo":
            flen = base + math.ceil(blocks * maxbp / 8.0)
        else:
            flen = base + math.ceil((sub + blocks * maxbp) / 8.0)
        out["bitrate_kbps_max"] = round(8 * flen * freq / (sub * blocks) / 1000.0, 1)
    return out


def transport_info(bz, addr):
    path, p = bz.transport(addr)
    if not p:
        return None
    conf = bytes(p.get("Configuration", b""))
    codec = p.get("Codec")
    t = {"path": path, "state": p.get("State"), "codec": codec_name(codec, conf),
         "volume": p.get("Volume"), "delay_ms": (p["Delay"] / 10.0) if "Delay" in p else None,
         "uuid": p.get("UUID")}
    if codec == 0:
        t["sbc"] = decode_sbc(conf)
    else:
        t["config_hex"] = conf.hex()
    return t


# ============================================================ PipeWire access
class Audio:
    """Runs PipeWire/Pulse tools inside the desktop user's session."""

    def __init__(self, user=None):
        self.user = user
        self.uid = None
        self.backend = None             # "pipewire" or "pulse": whichever sound server owns the DUT's Bluetooth audio
        self.drain_module = None        # PulseAudio null sink we created to park loopbacks (see pa_break_local_loops)
        self._pa_pw = None              # is the "pulse" socket just PipeWire's compatibility server? (see pa_is_pipewire)
        self._resolve()

    def _resolve(self):
        cands = []
        if self.user:
            try:
                cands.append(pwd.getpwnam(self.user).pw_uid)
            except KeyError:
                die("audio user %r does not exist" % self.user)
        else:
            if os.geteuid() != 0:
                cands.append(os.geteuid())
            if os.path.isdir("/run/user"):
                for d in sorted(os.listdir("/run/user")):
                    if d.isdigit() and int(d) >= 1000:
                        cands.append(int(d))
        for uid in cands:
            if os.path.exists("/run/user/%d/pipewire-0" % uid):
                self.uid = uid
                self.user = pwd.getpwuid(uid).pw_name
                return
        self.uid = None

    @property
    def ok(self):
        return self.uid is not None

    def env(self):
        return {"XDG_RUNTIME_DIR": "/run/user/%d" % self.uid,
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/%d/bus" % self.uid,
                "PIPEWIRE_REMOTE": "pipewire-0"}

    def wrap(self, cmd):
        if not self.ok:
            die("no PipeWire session found (is the desktop user logged in with pipewire running?)")
        env = ["%s=%s" % kv for kv in self.env().items()]
        if os.geteuid() == 0:
            return ["runuser", "-u", self.user, "--", "env"] + env + cmd
        return ["env"] + env + cmd

    def run(self, cmd, timeout=8):
        return _run(self.wrap(cmd), timeout)

    def popen(self, cmd, **kw):
        return subprocess.Popen(self.wrap(cmd), **kw)

    def dump(self):
        rc, txt = self.run(["pw-dump"], 10)
        try:
            return json.loads(txt) if rc == 0 else []
        except ValueError:
            return []

    def card_for(self, addr):
        """The PipeWire bluez device for `addr` as {id, name, active_profile,
        profiles{name:{index,available}}}. Native pw-dump is used (not pactl):
        a machine can run real PulseAudio on the pulse socket, which cannot
        see PipeWire's Bluetooth cards."""
        tag = addr.replace(":", "_").lower()
        for o in self.dump():
            p = o.get("info", {}).get("props", {}) if isinstance(o, dict) else {}
            if o.get("type", "").endswith("Interface:Device") and tag in p.get("device.name", "").lower():
                pr = o["info"].get("params", {})
                cur = (pr.get("Profile") or [{}])[0]
                self.backend = "pipewire"
                return {"id": o["id"], "name": p["device.name"], "active_profile": cur.get("name"),
                        "backend": "pipewire",
                        "profiles": {e["name"]: {"index": e["index"], "available": e.get("available") != "no"}
                                     for e in pr.get("EnumProfile", [])}}
        # Not in PipeWire: some units run real PulseAudio, which then owns the Bluetooth audio instead. But when the "pulse"
        # socket is only PipeWire's compatibility server, the card is simply not in the graph YET (it appears a couple of
        # seconds after the link comes up): asking pactl would race pw-dump, see the card that appeared in between and wrongly
        # pick the PulseAudio engine for the whole run.
        if self.pa_is_pipewire():
            return None
        card = self.pa_card_for(addr)
        if card:
            self.backend = "pulse"
        return card

    def sink_for(self, addr):
        tag = addr.replace(":", "_").lower()
        for o in self.dump():
            p = o.get("info", {}).get("props", {}) if isinstance(o, dict) else {}
            if o.get("type", "").endswith("Interface:Node") and p.get("media.class") == "Audio/Sink" \
                    and tag in p.get("node.name", "").lower():
                return {"id": o["id"], "name": p["node.name"], "state": o["info"].get("state"), "backend": "pipewire"}
        if self.backend == "pulse":
            return self.pa_sink_for(addr)
        return None

    def set_profile(self, card, profile):
        if card.get("backend") == "pulse":
            return self.pa_run(["set-card-profile", card["name"], card["profiles"][profile]["pa"]])
        idx = card["profiles"][profile]["index"]
        return self.run(["pw-cli", "set-param", str(card["id"]), "Profile", "{ index: %d }" % idx])

    def playback_cmd(self, target, rate, channels, latency_ms, name):
        """Command that plays raw s16le PCM from stdin into the Bluetooth sink `target`."""
        if self.backend == "pulse":
            return ["pacat", "-p", "--device", target, "--rate", str(rate), "--channels", str(channels),
                    "--format=s16le", "--latency-msec", str(latency_ms), "--stream-name", name]
        return ["pw-cat", "-p", "--target", target, "--rate", str(rate), "--channels", str(channels),
                "--format", "s16", "--latency", "%dms" % latency_ms,
                "-P", "{ node.name=%s media.name=%s }" % (name, name), "-"]

    # ---- PulseAudio ---------------------------------------------------------------------------
    PA_DRAIN = "bluforge_drain"

    def pa_run(self, args, timeout=8):
        return self.run(["pactl"] + args, timeout)

    def pa_is_pipewire(self):
        """True when the pulse socket is served by PipeWire (`Server Name: PulseAudio (on PipeWire x.y)`); False for real
        PulseAudio and when it cannot be told (the older behaviour: ask pactl)."""
        if self._pa_pw is None:
            rc, txt = self.pa_run(["info"], 5)
            m = re.search(r"^Server Name:\s*(.+)$", txt, flags=re.M) if rc == 0 else None
            if not m:
                return False
            self._pa_pw = "pipewire" in m.group(1).lower()
        return self._pa_pw

    def pa_card_for(self, addr):
        rc, txt = self.pa_run(["list", "cards"])
        return parse_pa_card(txt, addr.replace(":", "_").lower()) if rc == 0 else None

    def pa_sink_for(self, addr):
        rc, txt = self.pa_run(["list", "short", "sinks"])
        return parse_pa_sinks(txt, addr.replace(":", "_").lower()) if rc == 0 else None

    def pa_sink_state(self, name):
        """RUNNING / IDLE / SUSPENDED of the PulseAudio sink `name`, or None."""
        rc, txt = self.pa_run(["list", "short", "sinks"])
        return parse_pa_sink_state(txt, name) if rc == 0 else None

    def pa_resume_sink(self, name):
        """Ask PulseAudio to resume a suspended sink (0 = done)."""
        return self.pa_run(["suspend-sink", name, "0"])[0]

    def pa_json(self, what):
        rc, txt = self.pa_run(["--format=json", "list", what])
        if rc != 0:
            return None
        try:
            v = json.loads(txt)
        except ValueError:
            return None
        return v if isinstance(v, list) else None

    def pa_break_local_loops(self, tags):
        """PulseAudio loops every Bluetooth source it sees into the default sink. When the receiver is one of
        OUR OWN radios, that default sink is the one we stream into, so audio would circle between the two
        radios forever. Park those loopback streams on a silent sink instead. Returns how many were moved,
        or None when it cannot be done (the caller must then refuse to stream)."""
        srcs, outs, sinks, ins = (self.pa_json(w) for w in ("sources", "source-outputs", "sinks", "sink-inputs"))
        if None in (srcs, outs, sinks, ins):
            return None
        moves = loopback_moves(srcs, outs, sinks, ins, tags, self.PA_DRAIN)
        if not moves:
            return 0
        if not any(x.get("name") == self.PA_DRAIN for x in sinks):
            rc, txt = self.pa_run(["load-module", "module-null-sink", "sink_name=" + self.PA_DRAIN,
                                   "sink_properties=device.description=BluForge-drain"])
            if rc != 0:
                return None
            self.drain_module = txt.strip()
        moved = 0
        for idx in moves:
            rc, _ = self.pa_run(["move-sink-input", str(idx), self.PA_DRAIN])
            moved += rc == 0
        return moved if moved == len(moves) else None

    def pa_drain_cleanup(self):
        """Remove the silent sink we created, but only once nothing is playing into it."""
        if not self.drain_module:
            return
        ins = self.pa_json("sink-inputs")
        sinks = self.pa_json("sinks") or []
        drain = next((x.get("index") for x in sinks if x.get("name") == self.PA_DRAIN), None)
        if ins is None or any(i.get("sink") == drain for i in ins):
            return
        self.pa_run(["unload-module", self.drain_module])
        self.drain_module = None


def parse_pa_card(text, tag):
    """Bluetooth card for `tag` (address as 'aa_bb_..' lowercase) from `pactl list cards`, shaped like the PipeWire
    card: {name, active_profile, backend, profiles{name: {available, pa}}}. Profile names are normalised to the
    PipeWire spelling (a2dp_sink -> a2dp-sink); "pa" keeps PulseAudio's own name."""
    for block in re.split(r"^Card #\d+\s*$", text, flags=re.M):
        m = re.search(r"^\s*Name:\s*(bluez_card\.\S+)", block, flags=re.M)
        if not m or tag not in m.group(1).lower():
            continue
        prof = {}
        sect = re.search(r"^\s*Profiles:\s*$(.*?)^\s*Active Profile:\s*(\S+)", block, flags=re.M | re.S)
        if not sect:
            return None
        for line in sect.group(1).splitlines():
            pm = re.match(r"^\s+([A-Za-z0-9_+\-]+):\s.*\bavailable:\s*(\w+)\)", line)
            if pm:
                prof[pm.group(1).replace("_", "-")] = {"index": None, "available": pm.group(2) != "no", "pa": pm.group(1)}
        return {"id": None, "name": m.group(1), "active_profile": sect.group(2).replace("_", "-"),
                "backend": "pulse", "profiles": prof}
    return None


def parse_pa_sink_state(text, name):
    """State column of sink `name` in `pactl list short sinks` (index, name, driver, sample spec, state), else None."""
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) >= 5 and cols[1] == name:
            return cols[4].strip().upper()
    return None


def parse_pa_sinks(text, tag):
    """The A2DP sink for `tag` from `pactl list short sinks` (index, name, driver, spec, state per line)."""
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2 and cols[1].startswith("bluez_sink.") and tag in cols[1].lower() and "a2dp" in cols[1].lower():
            return {"id": cols[0], "name": cols[1], "state": cols[4] if len(cols) > 4 else None, "backend": "pulse"}
    return None


def loopback_moves(sources, outputs, sinks, inputs, tags, drain):
    """Indexes of sink-inputs that are module-loopback streams fed by a Bluetooth source belonging to one of this
    unit's own radios (tags = lowercase 'aa_bb_..' addresses) and not already playing into `drain`."""
    src_name = {s.get("index"): str(s.get("name", "")).lower() for s in sources}
    sink_name = {s.get("index"): str(s.get("name", "")) for s in sinks}
    mods = set()
    for o in outputs:
        name = src_name.get(o.get("source"), "")
        owner = o.get("owner_module")
        if name.startswith("bluez_source.") and any(t in name for t in tags) and owner not in (None, "", "n/a"):
            mods.add(str(owner))
    return [i["index"] for i in inputs if str(i.get("owner_module")) in mods and sink_name.get(i.get("sink")) != drain]


def local_adapter_tags(bz):
    """This unit's own Bluetooth addresses (every radio BlueZ manages) as lowercase 'aa_bb_cc_dd_ee_ff' strings."""
    tags = set()
    try:
        for path, ifaces in bz.managed().items():
            p = ifaces.get(IF_ADAPTER)
            if p and p.get("Address"):
                tags.add(str(p["Address"]).replace(":", "_").lower())
    except Exception:
        pass
    return tags


def pick_a2dp_profile(card, codec=None):
    names = list((card.get("profiles") or {}).keys())
    if codec:
        for n in names:
            if n.startswith("a2dp-sink") and codec.lower() in n.lower():
                return n
        if card.get("backend") == "pulse" and "a2dp-sink" in names:
            # PulseAudio has one A2DP profile and negotiates the codec with the receiver itself
            say("!", "PulseAudio cannot force the %s codec: using whatever it negotiates with the receiver "
                     "(the codec actually used is reported in the result)" % codec)
            return "a2dp-sink"
        die("this DUT/stack offers no A2DP profile for codec %r (available: %s)"
            % (codec, ", ".join(n for n in names if n.startswith("a2dp")) or "none"))
    if "a2dp-sink" in names:
        return "a2dp-sink"
    for n in names:
        if n.startswith("a2dp-sink"):
            return n
    return None


# ============================================================ audio source
def gen_pcm(kind, rate=48000, seconds=10):
    """Return interleaved s16le stereo bytes for one loop of the test signal."""
    n = rate * seconds
    out = bytearray()
    rng = random.Random(1234)
    amp = 0.25 * 32767            # -12 dBFS
    for i in range(n):
        t = i / float(rate)
        if kind == "tone":
            v = amp * math.sin(2 * math.pi * 440.0 * t)
        elif kind == "sweep":
            f0, f1 = 100.0, 8000.0
            k = math.log(f1 / f0) / seconds
            v = amp * math.sin(2 * math.pi * f0 * (math.exp(k * t) - 1) / k)
        elif kind == "noise":
            v = amp * rng.uniform(-1, 1)
        else:
            die("unknown source %r" % kind)
        s = struct.pack("<hh", int(v), int(v))
        out += s
    return bytes(out)


def read_wav(path):
    import wave
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            die("WAV must be 16-bit PCM (got %d-bit)" % (8 * w.getsampwidth()))
        return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()


class Streamer(threading.Thread):
    """Loops a PCM buffer into `pw-cat -p -` at real-time pace (the pipe blocks
    when the sink is back-pressured, so no timing code is needed)."""

    def __init__(self, audio, target, pcm, rate, channels, latency_ms, name):
        super().__init__(daemon=True)
        cmd = audio.playback_cmd(target, rate, channels, latency_ms, name)
        self.proc = audio.popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
        self.pcm = pcm
        self.error = None

    def run(self):
        chunk = 48000 * 4 // 10                # 100 ms of 48k stereo s16
        view = memoryview(self.pcm)
        try:
            while not STOP.is_set():
                for i in range(0, len(view), chunk):
                    if STOP.is_set():
                        return
                    self.proc.stdin.write(view[i:i + chunk])
                    self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as e:
            self.error = "audio writer stopped: %s" % e

    def stop(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()

    def alive(self):
        return self.proc.poll() is None


AUDIO_EXTS = (".wav", ".mp3", ".mp2", ".mpga", ".mpeg", ".mpg", ".m4a", ".aac", ".flac", ".ogg", ".oga",
              ".opus", ".wma", ".aif", ".aiff", ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".3gp", ".amr")


def probe_media(path):
    """Validate a user-supplied audio/video file with ffprobe; returns
    {codec, duration_s}. Rejects files with no audio stream up front."""
    if not os.path.isfile(path):
        die("audio file not found: %s" % path)
    if not path.lower().endswith(AUDIO_EXTS):
        die("unsupported file type; allowed: %s" % " ".join(AUDIO_EXTS))
    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        if path.lower().endswith(".wav"):
            return {"codec": "pcm (native wav reader)", "duration_s": None}
        die("ffmpeg/ffprobe are not installed: only 16-bit .wav files can be played (install ffmpeg for "
            "mp3/mpeg/aac/flac/ogg/...)")
    rc, txt = _run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                    "stream=codec_name,sample_rate,channels:format=duration", "-of", "json", path], 20)
    try:
        j = json.loads(txt)
        st = j["streams"][0]
    except (ValueError, KeyError, IndexError):
        die("no playable audio stream in %s (%s)" % (path, txt.strip()[:200] or "ffprobe found nothing"))
    dur = j.get("format", {}).get("duration")
    return {"codec": st.get("codec_name"), "rate": st.get("sample_rate"), "channels": st.get("channels"),
            "duration_s": round(float(dur), 1) if dur else None}


class FfmpegStreamer:
    """Any audio/video file -> ffmpeg (decode, 48 kHz stereo s16, looped forever)
    -> pw-cat. Streamed through a pipe, so file length is irrelevant and nothing
    is held in memory. Same interface as Streamer."""

    def __init__(self, audio, target, path, latency_ms, name):
        self.ff = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-stream_loop", "-1", "-i", path, "-vn",
             "-map", "0:a:0", "-f", "s16le", "-ar", "48000", "-ac", "2", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cmd = audio.playback_cmd(target, 48000, 2, latency_ms, name)
        self.proc = audio.popen(cmd, stdin=self.ff.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.ff.stdout.close()
        self._err = None

    @property
    def error(self):
        if self._err is None and self.ff.poll() not in (None, 0) and not STOP.is_set():
            try:
                tail = self.ff.stderr.read().decode(errors="replace").strip().splitlines()[-1:]
            except Exception:
                tail = []
            self._err = "ffmpeg decoder failed: %s" % (tail[0] if tail else "exit %s" % self.ff.returncode)
        return self._err

    def start(self):
        pass

    def alive(self):
        return self.proc.poll() is None and self.ff.poll() is None

    def stop(self):
        for pr in (self.ff, self.proc):
            try:
                pr.terminate()
                pr.wait(timeout=3)
            except Exception:
                pr.kill()


PA_KEEP_PERIOD_S = 0.5           # how often PulseSinkKeeper looks at the sink


class PulseSinkKeeper(threading.Thread):
    """PulseAudio only, interference stream only: resume the Bluetooth sink whenever PulseAudio suspends it.

    With an internal receiver on PulseAudio the receiving radio's device is not an audio device for PulseAudio (its A2DP profiles
    are "available: no"), so nothing takes the incoming stream and BlueZ on the receiving radio sends AVDTP SUSPEND about 5 s after
    every START (seen on resource 1.2: the stream was active 0-25 % of the run). The suspension also suspends the sender's sink;
    resuming it makes PulseAudio start the stream again within about half a second, which keeps the transport active about 90 %
    of the time. Every resume is counted and reported, so the run does not pretend nothing happened."""

    def __init__(self, audio, period=None):
        super().__init__(daemon=True, name="pa-sink-keeper")
        self.audio, self.period = audio, period or PA_KEEP_PERIOD_S
        self.sink = None                 # the sink to keep alive; set by the job once the stream is up (and after a reconnect)
        self.stop_ev = threading.Event()
        self.resumes = []                # time.time() of each resume
        self.errors = 0

    def run(self):
        while not self.stop_ev.wait(self.period):
            name = self.sink
            if not name:
                continue
            try:
                if self.audio.pa_sink_state(name) == "SUSPENDED":
                    if self.audio.pa_resume_sink(name) == 0:
                        self.resumes.append(time.time())
                    else:
                        self.errors += 1
            except Exception:            # a failed look must never end the stream: try again next time
                self.errors += 1

    def stop(self):
        self.stop_ev.set()
        if self.is_alive():
            self.join(3)


def remote_suspend_text(sc):
    """' (the receiving device sent AVDTP SUSPEND N time(s), first at T s ...)' when the link's stream-control record shows the
    receiver suspending the stream, else ''. It names the cause of an interrupted stream."""
    n = ((sc or {}).get("counts") or {}).get("remote_suspend", 0)
    if not n:
        return ""
    first = next((e["t_s"] for e in sc.get("events", []) if e["from"] == "remote" and e["signal"] == "SUSPEND"), None)
    return " (the receiving device sent AVDTP SUSPEND %d time(s)%s, which is why the stream stopped)" % (
        n, (", first %.1f s after monitoring began" % first) if first is not None else "")


class NullXrunMonitor:
    """Stand-in on PulseAudio, where audio underruns cannot be counted (pw-top is PipeWire only)."""
    err = 0
    seen = False
    last = None

    def start(self):
        pass

    def stop(self):
        pass


class XrunMonitor(threading.Thread):
    """Samples `pw-top -b -n 2` repeatedly and keeps the ERR (xrun) counter of
    one node. (A single long-running `pw-top -b` is block-buffered when piped,
    so its lines arrive minutes late; a short run flushes on exit.)"""

    def __init__(self, audio, node_name):
        super().__init__(daemon=True)
        self.audio = audio
        self.node = node_name
        self.err = 0
        self.seen = False
        self.last = {}
        self.stop_evt = threading.Event()

    def parse(self, text):
        row = None
        for line in text.splitlines():
            cols = line.split()
            # S ID QUANT RATE WAIT BUSY W/Q B/Q ERR [FORMAT...] NAME
            if len(cols) >= 10 and cols[-1] == self.node:
                row = cols
        if row:
            try:
                self.err = int(row[8])
                self.seen = True
                self.last = {"quant": row[2], "rate": row[3], "wait": row[4], "busy": row[5],
                             "w_q": row[6], "b_q": row[7]}
            except ValueError:
                pass

    def run(self):
        while not self.stop_evt.is_set():
            rc, txt = self.audio.run(["pw-top", "-b", "-n", "2"], 8)
            self.parse(txt)
            self.stop_evt.wait(1.0)

    def stop(self):
        self.stop_evt.set()


# ============================================================ one-shot actions
def act_preflight(a):
    res = {"ok": True, "gi": HAVE_GI, "root": os.geteuid() == 0}
    res["tools"] = {t: bool(shutil.which(t)) for t in
                    ("bluetoothctl", "hcitool", "hciconfig", "pw-dump", "pw-cli", "pw-cat", "pw-top", "runuser", "ffmpeg", "ffprobe")}
    res["bluetoothd"] = _run(["systemctl", "is-active", "bluetooth"])[1].strip()
    au = Audio(a.audio_user)
    res["audio"] = {"session": au.ok, "user": au.user if au.ok else None}
    if au.ok:
        rc, txt = au.run(["pw-cli", "info", "0"])
        res["audio"]["pipewire_ok"] = rc == 0 and "PipeWire" in txt
        rc, txt = au.run(["systemctl", "--user", "is-active", "wireplumber"])
        res["audio"]["wireplumber"] = txt.strip()
    notes = []
    if not HAVE_GI:
        notes.append("PyGObject missing: install python3-gobject (Fedora/RHEL) or python3-gi (Debian/Ubuntu)")
    if not res["tools"]["hcitool"]:
        notes.append("hcitool missing (bluez-deprecated): RSSI/TX power/link quality unavailable")
    if not res["tools"]["hciconfig"]:
        notes.append("hciconfig missing (bluez-deprecated): adapter throughput unavailable")
    if not (res["tools"]["ffmpeg"] and res["tools"]["ffprobe"]):
        notes.append("ffmpeg/ffprobe missing: custom audio limited to 16-bit .wav (mp3/mpeg/aac/flac/ogg need ffmpeg)")
    if not au.ok:
        notes.append("no PipeWire user session: A2DP streaming unavailable (log the desktop user in)")
    if res["bluetoothd"] != "active":
        notes.append("bluetooth.service is not active")
    res["notes"] = notes
    return res


def act_adapter(a, bz):
    if a.power is not None:
        bz.set(bz.adapter, IF_ADAPTER, "Powered", GLib.Variant("b", a.power == "on"))
        time.sleep(0.5)
    if a.pairable is not None:
        bz.set(bz.adapter, IF_ADAPTER, "Pairable", GLib.Variant("b", a.pairable == "on"))
    if a.discoverable is not None:
        bz.set(bz.adapter, IF_ADAPTER, "Discoverable", GLib.Variant("b", a.discoverable == "on"))
    return {"ok": True, "adapter": adapter_info(bz)}


def act_scan(a, bz):
    say("*", "hci%d: %s inquiry for %ds" % (bz.hci, "BR/EDR" if a.transport == "bredr" else "dual-mode", a.duration))
    devs = discover(bz, a.duration, transport=a.transport)
    say("+", "%d device(s) known on hci%d" % (len(devs), bz.hci))
    return {"ok": True, "devices": devs, "adapter": adapter_info(bz)}


def act_devices(a, bz):
    return {"ok": True, "devices": classic_devices(bz, only_bredr=False), "adapter": adapter_info(bz)}


def device_summary(bz, addr):
    dp = bz.dev_path(addr)
    p = bz.props(dp, IF_DEVICE)
    if p is None:
        die("%s is not known to hci%d (scan first)" % (addr, bz.hci))
    d = human_dev(p)
    objs = bz.device_objects(addr)
    d["has_avrcp_control"] = any("org.bluez.MediaControl1" in i for i in objs.values())
    d["has_player"] = any(IF_PLAYER in i for i in objs.values())
    d["battery"] = next((i["org.bluez.Battery1"].get("Percentage") for i in objs.values()
                         if "org.bluez.Battery1" in i), None)
    d["services_resolved"] = p.get("ServicesResolved")
    d["transport"] = transport_info(bz, addr)
    return d


def act_info(a, bz):
    addr = norm_addr(a.addr)
    d = device_summary(bz, addr)
    d["link"] = link_stats(bz.hci, addr) if d["connected"] else None
    au = Audio(a.audio_user)
    if au.ok and d["connected"]:
        c = au.card_for(addr)
        if c:
            d["audio_card"] = {"name": c.get("name"), "active_profile": c.get("active_profile"),
                               "profiles": [p for p, v in (c.get("profiles") or {}).items() if v.get("available", True)]}
    return {"ok": True, "device": d}


def act_pair(a, bz):
    addr = norm_addr(a.addr)
    ensure_powered(bz)
    ensure_device(bz, addr, a.scan)
    dp = bz.dev_path(addr)
    if bz.get(dp, IF_DEVICE, "Paired"):
        say("+", "%s is already paired" % addr)
    else:
        say("*", "pairing with %s (agent: NoInputNoOutput, legacy PIN %s)" % (addr, a.pin))
        t0 = time.time()
        with Agent(bz, a.pin):
            bz.call_loop(dp, IF_DEVICE, "Pair", None, int(a.timeout * 1000))
        say("+", "paired in %.1fs" % (time.time() - t0))
    bz.set(dp, IF_DEVICE, "Trusted", GLib.Variant("b", True))
    return {"ok": True, "device": device_summary(bz, addr)}


def wait_for(pred, timeout, step=0.25):
    end = time.time() + timeout
    while time.time() < end and not STOP.is_set():
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"


def offers_a2dp_sink(bz, dp):
    try:
        uuids = bz.get(dp, IF_DEVICE, "UUIDs")
    except Exception:
        return False
    return isinstance(uuids, (list, tuple)) and A2DP_SINK_UUID in [str(u).lower() for u in uuids]


def do_connect(bz, addr, timeout, prefer_bredr=False):
    """Connect all profiles; returns the time it took in milliseconds (0.0 if already connected).

    prefer_bredr: the caller is about to use a Classic audio profile. A dual-mode device that BlueZ last heard over LE gets a plain
    Device.Connect over the LE bearer, which fails only after about 15 s (measured), so every such run would start with that wait
    and its connect time would measure BlueZ's choice, not the device. Naming the A2DP sink profile forces BR/EDR at once. A plain
    Connect must NOT follow it to "bring up the other profiles": on a dual-mode device that makes BlueZ start an LE connection scan
    (passive, 60 ms interval and window = the radio is listening all the time) which stays on for the whole stream and degrades the
    Classic link (measured on an idle radio pair: jitter 2.3 -> 3.7 ms, worst audio packet 18 -> 40-100 ms). AVRCP follows the A2DP
    connection by itself. Devices that do not list the A2DP sink, and any refusal, fall through to the generic path below."""
    dp = bz.dev_path(addr)
    if bz.get(dp, IF_DEVICE, "Connected"):
        return 0.0
    t0 = time.time()
    first = max(10.0, timeout / 2.0)
    if prefer_bredr and offers_a2dp_sink(bz, dp):
        try:
            bz.call_loop(dp, IF_DEVICE, "ConnectProfile", GLib.Variant("(s)", (A2DP_SINK_UUID,)), int(first * 1000))
        except ClassicError:
            pass                                              # refused or timed out: the generic path decides
        if bz.get(dp, IF_DEVICE, "Connected"):
            return (time.time() - t0) * 1000.0
    try:
        bz.call_loop(dp, IF_DEVICE, "Connect", None, int(first * 1000))
    except ClassicError as e:
        # A dual-mode device that BlueZ last heard over LE (the internal receiver, after a spell idle) gets a
        # Device.Connect over the LE bearer, which times out. ConnectProfile for a Classic UUID forces a BR/EDR link,
        # even when the profile itself is then refused; the plain Connect after it finds the link up.
        say("!", "connect failed (%s) - forcing a BR/EDR link and retrying" % e)
        # The LE attempt is still pending inside bluetoothd after our own call timed out, and any further connect is
        # refused with "Operation already in progress" until it is cancelled. Disconnect cancels it (measured: Connect
        # timed out, ConnectProfile alone failed, Disconnect + ConnectProfile connected in 0.65 s).
        try:
            bz.call_loop(dp, IF_DEVICE, "Disconnect", None, 10000)
        except ClassicError:
            pass
        time.sleep(0.5)
        try:
            bz.call_loop(dp, IF_DEVICE, "ConnectProfile", GLib.Variant("(s)", ("0000110b-0000-1000-8000-00805f9b34fb",)),
                         int(first * 1000))
        except ClassicError:
            pass
        if not bz.get(dp, IF_DEVICE, "Connected"):
            bz.call_loop(dp, IF_DEVICE, "Connect", None, int(max(10.0, timeout - first) * 1000))
    return (time.time() - t0) * 1000.0


def act_connect(a, bz):
    addr = norm_addr(a.addr)
    ensure_powered(bz)
    ensure_device(bz, addr, a.scan)
    dp = bz.dev_path(addr)
    if not bz.get(dp, IF_DEVICE, "Paired"):
        die("%s is not paired on hci%d - pair it first" % (addr, bz.hci))
    say("*", "connecting %s via hci%d" % (addr, bz.hci))
    ms = do_connect(bz, addr, a.timeout)
    say("+", "connected in %.0f ms" % ms)
    res = {"ok": True, "connect_ms": round(ms, 1)}
    if a.wait_audio:
        t0 = time.time()
        t = wait_for(lambda: transport_info(bz, addr), a.wait_audio)
        res["transport_ms"] = round((time.time() - t0) * 1000, 1) if t else None
        if not t:
            say("!", "connected, but no A2DP transport appeared within %ds" % a.wait_audio)
    res["device"] = device_summary(bz, addr)
    return res


def act_disconnect(a, bz):
    addr = norm_addr(a.addr)
    dp = bz.dev_path(addr)
    if not bz.exists(dp):
        die("%s is not known to hci%d" % (addr, bz.hci))
    if not bz.get(dp, IF_DEVICE, "Connected"):
        return {"ok": True, "msg": "already disconnected"}
    t0 = time.time()
    bz.call(dp, IF_DEVICE, "Disconnect", None, 20000)
    ms = (time.time() - t0) * 1000
    say("+", "disconnected in %.0f ms" % ms)
    return {"ok": True, "disconnect_ms": round(ms, 1)}


def act_remove(a, bz):
    addr = norm_addr(a.addr)
    if bz.exists(bz.dev_path(addr)):
        bz.call(bz.adapter, IF_ADAPTER, "RemoveDevice", GLib.Variant("(o)", (bz.dev_path(addr),)))
    say("+", "%s removed from hci%d (pairing deleted)" % (addr, bz.hci))
    return {"ok": True}


# ---------------------------------------------------------------- AVRCP
PLAYER_CMDS = {"play": "Play", "pause": "Pause", "stop": "Stop", "next": "Next",
               "previous": "Previous", "fastforward": "FastForward", "rewind": "Rewind"}


def act_avrcp_player(a, bz):
    """Drive the DUT's media player (only exists when the DUT is a player, e.g.
    a phone). A headset/speaker is AVRCP *controller* only - it has no player
    for us to command, and says so explicitly instead of failing obscurely."""
    addr = norm_addr(a.addr)
    pp, props = bz.player(addr)
    if not pp:
        die("%s exposes no AVRCP media player (it is an AVRCP controller-only device such as a "
            "headset); use the AVRCP volume test or the button-watch test instead" % addr)
    if a.pcmd != "status":
        bz.call(pp, IF_PLAYER, PLAYER_CMDS[a.pcmd], None)
        say("+", "AVRCP %s sent" % a.pcmd)
        time.sleep(0.5)
    props = bz.props(pp, IF_PLAYER) or props
    return {"ok": True, "player": {"name": props.get("Name"), "status": props.get("Status"),
                                   "track": props.get("Track"), "position_ms": props.get("Position")}}


def act_avrcp_volume(a, bz):
    """AVRCP absolute-volume test: sends SetAbsoluteVolume steps to the DUT via
    BlueZ and checks BlueZ accepts each one and reports it back. NOTE: BlueZ
    updates its own Volume property when the command is sent, so this proves the
    command path (no error, value stuck), NOT that the DUT's amplifier changed -
    confirm audibly on the DUT."""
    addr = norm_addr(a.addr)
    tp, p = bz.transport(addr)
    if not tp:
        die("no A2DP transport for %s - connect it first (AVRCP volume rides on the A2DP transport)" % addr)
    if "Volume" not in p:
        die("transport has no Volume property: the DUT (or this BlueZ/PipeWire config) does not do "
            "AVRCP absolute volume")
    steps = [int(x) for x in a.steps.split(",")]
    for s in steps:
        if not 0 <= s <= 127:
            die("volume steps must be 0-127 (AVRCP absolute volume range)")
    events = []

    def on_sig(kind, path, i, changed):
        if kind == "props" and path == tp and "Volume" in changed:
            events.append((time.time(), changed["Volume"]))
    bz.subscribe(on_sig)
    results = []
    original = p.get("Volume")
    for s in steps:
        events.clear()
        t0 = time.time()
        bz.set(tp, IF_TRANSPORT, "Volume", GLib.Variant("q", s))
        pump(0.05)
        got = None
        while time.time() - t0 < a.timeout and not STOP.is_set():
            readback = bz.get(tp, IF_TRANSPORT, "Volume")
            if readback == s or any(v == s for _, v in events):
                got = readback if readback is not None else s
                break
            pump(0.1)
        ms = round((time.time() - t0) * 1000)
        ok = got is not None
        results.append({"set": s, "readback": bz.get(tp, IF_TRANSPORT, "Volume"), "ok": ok, "ms": ms})
        say("+" if ok else "x", "volume %3d -> %s (%d ms)" % (s, "sent, accepted by BlueZ" if ok else "NOT accepted", ms))
    if original is not None:
        try:
            bz.set(tp, IF_TRANSPORT, "Volume", GLib.Variant("q", original))
        except ClassicError:
            pass
    passed = all(r["ok"] for r in results)
    say("+" if passed else "x", "AVRCP absolute volume: %s (command path only; confirm the level change on the DUT)" % ("PASS" if passed else "FAIL"))
    return {"ok": True, "test": "avrcp-volume", "pass": passed, "steps": results}


# ============================================================ interrogate (GATT + SDP)
IF_GATT_SVC = "org.bluez.GattService1"
IF_GATT_CHR = "org.bluez.GattCharacteristic1"
IF_GATT_DSC = "org.bluez.GattDescriptor1"
SECURE_READ_FLAGS = ("encrypt-read", "encrypt-authenticated-read", "secure-read")
NO_READ_SERVICES = {0x1812}          # HID: reports can carry keystrokes - list it, never read it


def _short_uuid(u):
    """16-bit value of a Bluetooth-base UUID, else None."""
    u = u.lower()
    if u.endswith(ble_decode.BASE_UUID_TAIL) and u.startswith("0000"):
        return int(u[4:8], 16)
    return None


def _gatt_tree(bz, addr, do_read, budget_s):
    """Enumerate the device's resolved GATT database (read-only). Only characteristics
    that allow a plain read are read; ones needing encryption/authentication are listed
    but not read, so this never triggers pairing."""
    objs = bz.device_objects(addr)
    svcs = {p: i[IF_GATT_SVC] for p, i in objs.items() if IF_GATT_SVC in i}
    chrs = {p: i[IF_GATT_CHR] for p, i in objs.items() if IF_GATT_CHR in i}
    dscs = {p: i[IF_GATT_DSC] for p, i in objs.items() if IF_GATT_DSC in i}
    deadline = time.time() + budget_s
    out, stats = [], {"read_ok": 0, "read_failed": 0, "skipped_secure": 0, "skipped_budget": 0, "needs_security": 0}
    for sp in sorted(svcs):
        sv = svcs[sp]
        ss = _short_uuid(sv["UUID"])
        sname = ble_decode.SERVICES.get(ss) if ss is not None else None
        node = {"uuid": sv["UUID"], "name": sname, "primary": bool(sv.get("Primary")), "characteristics": []}
        for cp in sorted(c for c in chrs if c.startswith(sp + "/")):
            ch = chrs[cp]
            cs = _short_uuid(ch["UUID"])
            flags = list(ch.get("Flags", []))
            c = {"uuid": ch["UUID"], "name": ble_decode.CHARACTERISTICS.get(cs) if cs is not None else None,
                 "flags": flags, "descriptors": []}
            for dp in sorted(d for d in dscs if d.startswith(cp + "/")):
                du = dscs[dp]["UUID"]
                ds = _short_uuid(du)
                c["descriptors"].append({"uuid": du, "name": ble_decode.DESCRIPTORS.get(ds) if ds is not None else None})
            if "read" in flags and do_read and ss not in NO_READ_SERVICES:
                if time.time() > deadline or STOP.is_set():
                    stats["skipped_budget"] += 1
                else:
                    try:
                        r = bz.call(cp, IF_GATT_CHR, "ReadValue", GLib.Variant("(a{sv})", ({},)), timeout_ms=6000)
                        b = bytes(r[0])[:256]
                        c["value_hex"], c["value"] = ble_decode.value_text(cs, b)
                        stats["read_ok"] += 1
                    except ClassicError as e:
                        c["read_error"] = str(e)[:120]
                        stats["read_failed"] += 1
                        if re.search(r"authenticat|encrypt|insufficient|not permitted|security", str(e), re.I):
                            c["needs_security"] = True
                            stats["needs_security"] += 1
            elif "read" not in flags and any(f in flags for f in SECURE_READ_FLAGS):
                stats["skipped_secure"] += 1
            node["characteristics"].append(c)
        out.append(node)
    return out, stats


def _sdp_records(hci, addr):
    if not shutil.which("sdptool"):
        return None, "sdptool not installed"
    rc, txt = _run(["sdptool", "-i", "hci%d" % hci, "records", addr], 40)
    recs = ble_decode.parse_sdp_records(txt)
    if not recs:
        return [], (txt.strip().splitlines()[-1] if txt.strip() else "no SDP records returned")
    return recs, None


def act_interrogate(a, bz):
    """Connect to a device and list everything it will tell an unpaired, unencrypted
    client: GATT services/characteristics (+ readable values) for LE, and profiles
    (UUIDs + SDP records) for BR/EDR. Read-only: nothing is written to the device, and
    the device is restored to its prior connected/known state afterwards. It does not request pairing,
    but a device that demands security for a plain read may make BlueZ pair on its own; that is detected,
    reported (paired_during_interrogation) and undone."""
    addr = norm_addr(a.addr)
    ensure_powered(bz)
    dp = bz.dev_path(addr)
    existed = bz.exists(dp)
    if not existed:
        say("*", "%s not known to hci%d yet - discovering (up to %ds)" % (addr, bz.hci, a.scan))
        discover(bz, a.scan, transport=a.transport, want=addr)
        if not bz.exists(dp):
            die("%s was not found by hci%d (powered on? in range? advertising?)" % (addr, bz.hci))
    was_connected = bool(bz.get(dp, IF_DEVICE, "Connected"))
    was_paired = bool(bz.get(dp, IF_DEVICE, "Paired"))
    res = {"ok": True, "addr": addr, "hci": bz.hci, "was_connected": was_connected}
    try:
        if not was_connected:
            say("*", "connecting to %s (read-only interrogation)" % addr)
            t0 = time.time()
            try:
                bz.call_loop(dp, IF_DEVICE, "Connect", None, int(a.timeout * 1000))
            except ClassicError as e:
                die("could not connect to %s: %s" % (addr, e))
            res["connect_ms"] = round((time.time() - t0) * 1000)
            say("+", "connected in %d ms" % res["connect_ms"])
        resolved = wait_for(lambda: bz.get(dp, IF_DEVICE, "ServicesResolved"), a.resolve_timeout)
        p = bz.props(dp, IF_DEVICE) or {}
        res["device"] = {
            "name": p.get("Alias") or p.get("Name"), "address_type": p.get("AddressType"),
            "class": "0x%06x" % p["Class"] if "Class" in p else None, "icon": p.get("Icon"),
            "appearance": ble_decode.APPEARANCE.get(p["Appearance"] >> 6) if "Appearance" in p else None,
            "modalias": p.get("Modalias"), "paired": was_paired, "rssi": p.get("RSSI"),
            "tx_power": p.get("TxPower"),
        }
        uuids = []
        for u in p.get("UUIDs", []):
            sh = _short_uuid(u)
            nm = ble_decode.CLASSIC_PROFILES.get(sh) or ble_decode.SERVICES.get(sh) if sh is not None else None
            uuids.append({"uuid": u, "name": nm})
        res["uuids"] = uuids
        if resolved:
            say("*", "services resolved - reading GATT database")
            res["gatt"], res["gatt_stats"] = _gatt_tree(bz, addr, a.read, a.read_budget)
            say("+", "%d GATT service(s); %d value(s) read" % (len(res["gatt"]), res["gatt_stats"]["read_ok"]))
        else:
            res["gatt"], res["gatt_stats"] = [], None
            say("!", "GATT services did not resolve within %ds (BR/EDR-only device, or it needs pairing)" % a.resolve_timeout)
        classic = any((_short_uuid(u["uuid"]) or 0) in ble_decode.CLASSIC_PROFILES and (_short_uuid(u["uuid"]) or 0) >= 0x1100
                      and (_short_uuid(u["uuid"]) or 0) < 0x1800 for u in uuids)
        if classic and not a.no_sdp:
            say("*", "reading SDP records")
            res["sdp"], res["sdp_note"] = _sdp_records(bz.hci, addr)
            if res["sdp"]:
                say("+", "%d SDP record(s)" % len(res["sdp"]))
        else:
            res["sdp"], res["sdp_note"] = None, ("no BR/EDR profiles reported" if not classic else "skipped")
    finally:
        # Leave the device as we found it. Interrogate only issues plain reads, but a device that answers a
        # read with "authentication required" can make BlueZ raise the link security and start pairing on
        # its own. Detect that, say so in the result, and undo it.
        try:
            became_paired = (not was_paired) and bz.exists(dp) and bool(bz.get(dp, IF_DEVICE, "Paired"))
            res["paired_during_interrogation"] = became_paired
            if became_paired:
                say("!", "the device paired during interrogation (it asked for security); the pairing is being removed")
            if not was_connected and bz.exists(dp) and bz.get(dp, IF_DEVICE, "Connected"):
                bz.call(dp, IF_DEVICE, "Disconnect", None, 15000)
            if (not existed or became_paired) and not was_paired and bz.exists(dp):
                bz.call(bz.adapter, IF_ADAPTER, "RemoveDevice", GLib.Variant("(o)", (dp,)))
        except ClassicError:
            pass
    return res


def act_link_local(a, bz):
    """Prepare an INTERNAL receiver: another radio of this unit that will play the A2DP sink for `bz`'s radio.
    Pairs the two radios with each other if needed (first use only) and marks each side trusted, so the stream
    can connect without anyone confirming anything. The receiver is discoverable only while pairing."""
    if a.peer == bz.hci:
        die("the receiver must be a different radio than the sender (hci%d)" % bz.hci)
    pbz = BZ(a.peer)
    ensure_powered(bz)
    ensure_powered(pbz)
    my_addr = adapter_info(bz)["addr"]
    peer_addr = adapter_info(pbz)["addr"]
    if not my_addr or not peer_addr:
        die("could not read the radios' addresses")

    def paired(b, addr):
        dp = b.dev_path(addr)
        return b.exists(dp) and bool(b.get(dp, IF_DEVICE, "Paired"))
    # BlueZ can list both radios as paired while the kernel holds no link key for one of them (raw data links are then
    # refused); `force` re-pairs from scratch
    both = paired(bz, peer_addr) and paired(pbz, my_addr) and not getattr(a, "force", False)
    if not both:
        for b, addr in ((bz, peer_addr), (pbz, my_addr)):       # half-paired keys never work: start clean
            if b.exists(b.dev_path(addr)):
                b.call(b.adapter, IF_ADAPTER, "RemoveDevice", GLib.Variant("(o)", (b.dev_path(addr),)))
        say("*", "pairing hci%d with hci%d (first use of this pair of radios)" % (bz.hci, pbz.hci))
        pbz.set(pbz.adapter, IF_ADAPTER, "Pairable", GLib.Variant("b", True))
        pbz.set(pbz.adapter, IF_ADAPTER, "Discoverable", GLib.Variant("b", True))
        try:
            ensure_device(bz, peer_addr, a.scan)
            with Agent(bz, a.pin):
                bz.call_loop(bz.dev_path(peer_addr), IF_DEVICE, "Pair", None, int(a.timeout * 1000))
        finally:
            try:
                pbz.set(pbz.adapter, IF_ADAPTER, "Discoverable", GLib.Variant("b", False))
            except ClassicError:
                pass
        say("+", "paired")
    for b, addr in ((bz, peer_addr), (pbz, my_addr)):
        dp = b.dev_path(addr)
        if not b.exists(dp):
            die("hci%d does not know hci%d after pairing" % (b.hci, (pbz if b is bz else bz).hci))
        b.set(dp, IF_DEVICE, "Trusted", GLib.Variant("b", True))
    # Hand over a DISCONNECTED pair. Right after pairing, BlueZ's own auto-connect of the audio profile loses a
    # race ("Device or resource busy"): the link is up but the sound server never sees an A2DP profile. A clean
    # connect from the streaming job works every time.
    dp = bz.dev_path(peer_addr)
    if bz.get(dp, IF_DEVICE, "Connected"):
        try:
            bz.call(dp, IF_DEVICE, "Disconnect", None, 15000)
        except ClassicError:
            pass
        wait_for(lambda: not bz.get(dp, IF_DEVICE, "Connected"), 8)
    say("+", "hci%d (sender) and hci%d (receiver %s) are linked and trusted" % (bz.hci, pbz.hci, peer_addr))
    return {"ok": True, "addr": peer_addr, "sender": bz.hci, "peer": pbz.hci, "newly_paired": not both}


ACTIONS = {
    "link-local": act_link_local,
    "adapter": act_adapter, "scan": act_scan, "devices": act_devices, "info": act_info,
    "pair": act_pair, "connect": act_connect, "disconnect": act_disconnect, "remove": act_remove,
    "avrcp-player": act_avrcp_player, "avrcp-volume": act_avrcp_volume,
    "interrogate": act_interrogate,
}


# ============================================================ AVRCP key watcher
KEYMAP = {200: "PLAY", 207: "PLAY", 201: "PAUSE", 164: "PLAYPAUSE", 166: "STOP", 163: "NEXT",
          165: "PREVIOUS", 208: "FASTFORWARD", 168: "REWIND", 115: "VOLUMEUP", 114: "VOLUMEDOWN",
          113: "MUTE"}
EV_FMT = "llHHi"
EV_SIZE = struct.calcsize(EV_FMT)


def find_avrcp_evdev(addr, alias=None, local=None):
    """BlueZ exposes remote AVRCP passthrough commands (headset buttons) as a
    uinput device named '<remote name> (AVRCP)' (older BlueZ: '<BDADDR> (AVRCP)')
    with Phys = the LOCAL adapter address. Match on either name form plus the
    local adapter so two radios/devices with the same name stay distinct."""
    try:
        txt = open("/proc/bus/input/devices").read()
    except OSError:
        return None
    for block in txt.split("\n\n"):
        m = re.search(r'Name="([^"]*)"', block)
        h = re.search(r"Handlers=.*?\b(event\d+)", block)
        ph = re.search(r"Phys=(\S*)", block)
        if not (m and h) or "(avrcp)" not in m.group(1).lower():
            continue
        name = m.group(1).lower()
        if local and ph and ph.group(1) and ph.group(1).lower() != local.lower():
            continue
        if addr.lower() in name or (alias and name.startswith(alias.lower())):
            return "/dev/input/" + h.group(1)
    return None


class KeyWatcher(threading.Thread):
    def __init__(self, addr, on_key, alias=None, local=None):
        super().__init__(daemon=True)
        self.addr = addr
        self.alias = alias
        self.local = local
        self.on_key = on_key
        self.dev = None
        self.stop_evt = threading.Event()

    def run(self):
        fd = None
        while not self.stop_evt.is_set():
            if fd is None:
                path = find_avrcp_evdev(self.addr, self.alias, self.local)
                if not path:
                    self.stop_evt.wait(1.0)
                    continue
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    self.dev = path
                except OSError:
                    self.stop_evt.wait(1.0)
                    continue
            r, _, _ = select.select([fd], [], [], 0.5)
            if not r:
                continue
            try:
                data = os.read(fd, EV_SIZE * 16)
            except OSError:
                os.close(fd)
                fd = None
                continue
            for off in range(0, len(data) - EV_SIZE + 1, EV_SIZE):
                _s, _u, etype, code, value = struct.unpack_from(EV_FMT, data, off)
                if etype == 1 and value == 1 and code in KEYMAP:
                    self.on_key(KEYMAP[code])
        if fd is not None:
            os.close(fd)

    def stop(self):
        self.stop_evt.set()


# ============================================================ run bookkeeping
class Recorder:
    def __init__(self, kind, addr, hci):
        self.kind, self.addr, self.hci = kind, addr, hci
        self.t0 = time.time()
        self.samples = []
        self.events = []
        self.stamp = time.strftime("%Y%m%d-%H%M%S")

    def rel(self):
        return round(time.time() - self.t0, 2)

    def event(self, name, **kw):
        e = {"t": self.rel(), "event": name}
        e.update(kw)
        self.events.append(e)
        emit(m="event", **e)

    def sample(self, **kw):
        s = {"t": self.rel()}
        s.update(kw)
        self.samples.append(s)
        emit(m="sample", **s)

    def save(self, summary):
        """Write <base>.csv then <base>.json, each crash-safely (temp file + fsync + atomic rename). The JSON goes
        last: its existence means the CSV it describes is complete, and it carries that CSV's SHA-256, the file
        layout version, the tool version and where/when it was produced."""
        d = os.path.join(LOG_DIR, "classic")
        try:
            os.makedirs(d, exist_ok=True)
            durable.sweep_temp_files(d)
            base = os.path.join(d, "%s_%s_%s" % (self.stamp, self.kind, self.addr.replace(":", "")))
            csv_bytes = None
            if self.samples:
                buf = io.StringIO()
                w = csv.DictWriter(buf, fieldnames=sorted({k for s in self.samples for k in s}))
                w.writeheader()
                w.writerows(self.samples)
                csv_bytes = buf.getvalue().encode("utf-8")
                summary["csv_sha256"] = durable.sha256_hex(csv_bytes)
                summary["csv_rows"] = len(self.samples)
            now = time.time()
            summary.update({
                "saved_as": base,          # set before dumping/emitting so the file and the live summary both carry it
                "schema": RESULT_SCHEMA, "tool_version": __version__,
                "started_at": datetime.datetime.fromtimestamp(self.t0, datetime.timezone.utc).isoformat(timespec="seconds"),
                "finished_at": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(timespec="seconds"),
                "host": socket.gethostname(), "kernel": os.uname().release,
            })
            doc = {"schema": RESULT_SCHEMA, "tool": {"name": "BluForge", "version": __version__},
                   "summary": summary, "events": self.events, "samples": self.samples}
            body = json.dumps(doc, indent=1, default=str).encode("utf-8")
            if csv_bytes is not None:
                durable.atomic_write_bytes(base + ".csv", csv_bytes)
            durable.atomic_write_bytes(base + ".json", body)
            say("*", "results saved: %s.json / .csv" % base)
        except (OSError, ValueError, TypeError) as e:
            summary.pop("saved_as", None)
            say("!", "could not save results (%s)" % e)


def stats(vals):
    v = [x for x in vals if x is not None]
    if not v:
        return None
    v.sort()
    return {"min": v[0], "avg": round(sum(v) / len(v), 2), "max": v[-1],
            "p95": v[min(len(v) - 1, int(0.95 * len(v)))], "n": len(v)}


def verdict(rec, summary, failures):
    summary["pass"] = not failures
    summary["fail_reasons"] = failures
    rec.save(summary)                       # first, so summary["saved_as"] exists when it is emitted
    emit(m="summary", **summary)
    if failures:
        say("x", "RESULT: FAIL - " + "; ".join(failures))
    else:
        say("+", "RESULT: PASS")
    return 0 if not failures else 2


# ============================================================ A2DP stream job
def sink_reported_delay(polled, stream_control):
    """What the sink itself claims its audio delay is: the BlueZ transport's `Delay` as polled during the run, else the AVDTP
    DELAYREPORT seen on the air. Reported, not measured; None when the sink never said."""
    if polled:
        return {"min": min(polled), "max": max(polled), "last": polled[-1],
                "basis": "reported by the sink (AVDTP delay report, via BlueZ); not measured by BluForge"}
    dr = (stream_control or {}).get("delay_reports_ms")
    if dr:
        return {"min": dr["min"], "max": dr["max"], "last": dr["last"],
                "basis": "reported by the sink (AVDTP DELAYREPORT seen on the air); not measured by BluForge"}
    return None


# Other uses of the radio that carries the Classic link, and the share of the run above which they are reported as a warning.
COEX_WARN_PCT = {"le_scan": 10.0, "le_connect": 10.0, "inquiry": 5.0}
COEX_LABEL = {"le_scan": "an LE scan", "le_connect": "an LE connection attempt (the controller listens continuously)",
              "inquiry": "a Bluetooth inquiry"}


def coexistence_block(act):
    """What else the radio did during the run (acl_meter.activity_report) plus warnings for what disturbs a Classic link. On an
    idle radio pair BlueZ's LE connection scan (60 ms interval and window) raised the mean jitter from 2.3 to 3.7 ms and the worst
    audio packet from 18 to 40-100 ms, without any change of the link itself."""
    if not act:
        return None
    out = dict(act)
    out["warnings"] = ["%s was on for %.0f %% of the run (%.1f s) on the radio that carries the Classic link; the Classic link shares "
                       "that radio, so delay and jitter in this result include its effect" % (COEX_LABEL[k][0].upper() + COEX_LABEL[k][1:], act[k]["enabled_pct"], act[k]["enabled_s"])
                       for k, lim in COEX_WARN_PCT.items() if act.get(k) and act[k]["enabled_pct"] >= lim]
    return out


def link_delay_block(rep, integrity, activity=None):
    """The `link_delay` part of an A2DP result: what was measured, how, and whether it can be trusted."""
    basis = ("host-observed delivery time of each A2DP media packet: first ACL fragment handed to the controller -> controller "
             "reports the last fragment completed (kernel timestamps). Not the audio latency a listener hears: the host's "
             "audio buffer and the sink's own buffer and decoder are not included.")
    timing = (integrity or {}).get("timing") or {}
    coex = coexistence_block(activity)
    if rep is None:
        return {"available": False, "basis": basis, "coexistence": coex,
                "reason": "no A2DP media packet was completed while the meter was running" if integrity else
                          "the HCI monitor was unavailable"}
    out = dict(rep)
    out.update({"available": True, "basis": basis, "clock": timing.get("clock"), "coexistence": coex,
                "valid": bool(timing.get("ok", False)), "notes": list(timing.get("notes", []))})
    return out


def delay_failures(block, max_delay_ms, max_jitter_ms):
    """Reasons the delay limits failed. A limit that cannot be checked is a failure, never a silent pass."""
    out = []
    if max_delay_ms is None and max_jitter_ms is None:
        return out
    if not block or not block.get("available"):
        return ["a delay/jitter limit was set but no media packet delay could be measured (%s)" %
                ((block or {}).get("reason") or "no data")]
    if not block.get("valid"):
        return ["a delay/jitter limit was set but the delay measurement is not trustworthy: " + "; ".join(block.get("notes") or ["?"])]
    d, j = block["delay_ms"], block.get("jitter_ms")
    if max_delay_ms is not None and d["p99"] > max_delay_ms:
        out.append("link delay p99 %.1f ms exceeds the limit of %.1f ms (p50 %.1f, max %.1f)" % (d["p99"], max_delay_ms, d["p50"], d["max"]))
    if max_jitter_ms is not None:
        if not j:
            out.append("a jitter limit was set but too few packets were measured to compute jitter")
        elif j["avg"] > max_jitter_ms:
            out.append("link jitter %.2f ms exceeds the limit of %.2f ms" % (j["avg"], max_jitter_ms))
    if out:                       # a failure the device may not be responsible for: say what else was using the radio
        out.extend("note: " + w for w in (block.get("coexistence") or {}).get("warnings", []))
    return out


LOAD_RETRY_S = 5            # load mode: pause between reconnect attempts
AFH_EVERY_S = 15            # how often the link's hop-channel map is read


def job_a2dp(a, bz):
    addr = norm_addr(a.addr)
    # "load" mode = the same streamer used as a realistic Bluetooth Classic INTERFERENCE source: it never
    # gives up on the link (retries instead of failing), does not judge the DUT, and reports how much
    # interference it actually delivered.
    load = bool(getattr(a, "load", False))
    au = Audio(a.audio_user)
    if not au.ok:
        die("A2DP needs the desktop user's PipeWire session (none found)")
    for t in ("pw-cat", "pw-top", "pw-dump", "pw-cli"):
        if not shutil.which(t):
            die("%s not found (install pipewire-utils)" % t)
    ensure_powered(bz)
    ensure_device(bz, addr, a.scan)
    dp = bz.dev_path(addr)
    if not bz.get(dp, IF_DEVICE, "Paired"):
        die("%s is not paired on hci%d - pair it first" % (addr, bz.hci))

    rec = Recorder("load" if load else "a2dp", addr, bz.hci)
    state = {"connected": bool(bz.get(dp, IF_DEVICE, "Connected")), "tstate": None, "volume": None,
             "disconnects": 0, "keys": {}, "vol_changes": 0, "state_changes": 0}
    reconnects = []
    # Exact ACL throughput comes from the kernel HCI monitor channel; started before the
    # stream so every packet is counted. Falls back to the (inexact) hciconfig counters.
    meter = None
    try:
        import acl_meter                  # imported here, not at the top: only this job needs it, and a
    except Exception as e:                # problem in the radio layer must not break `preflight` and friends
        acl_meter = None
        say("!", "exact throughput meter unavailable (%s) - falling back to hciconfig counters (not exact)" % e)
    if acl_meter is not None:
        try:
            meter = acl_meter.AclMeter(bz.hci, late_limit_ms=getattr(a, "max_delay_ms", None), intervals=True).open()
            meter.start()
        except OSError as e:
            say("!", "HCI monitor unavailable (%s) - throughput falls back to hciconfig counters (not exact)" % e)

    seen_handles = set()                  # every connection handle the DUT had during the run (a reconnect gets a new one)

    def dut_handles():
        if meter is None:
            return None
        hs = meter.handles_for(addr)
        if not hs:
            hs = [l["handle"] for l in acl_links(bz.hci) if l["addr"].upper() == addr and l["type"] == "ACL"]
        seen_handles.update(hs or [])
        return hs or None

    def on_sig(kind, path, i, changed):
        if kind == "props" and path == dp and "Connected" in changed:
            state["connected"] = bool(changed["Connected"])
            if not changed["Connected"]:
                state["disconnects"] += 1
                rec.event("disconnect")
            else:
                rec.event("connected")
        elif kind == "props" and "/fd" in path and path.startswith(dp):
            if "State" in changed:
                state["tstate"] = changed["State"]
                state["state_changes"] += 1
                rec.event("transport_state", state=changed["State"])
            if "Volume" in changed:
                state["volume"] = changed["Volume"]
                state["vol_changes"] += 1
                rec.event("volume", value=changed["Volume"])
        elif kind == "removed" and path.startswith(dp) and "/fd" in path:
            rec.event("transport_removed")
    bz.subscribe(on_sig)

    def on_key(k):
        state["keys"][k] = state["keys"].get(k, 0) + 1
        rec.event("avrcp_key", key=k)
        say("*", "DUT sent AVRCP key: %s" % k)
    kw = KeyWatcher(addr, on_key, bz.get(dp, IF_DEVICE, "Alias"), bz.get(bz.adapter, IF_ADAPTER, "Address"))
    kw.start()

    def drop_internal():
        """An internal receiver left connected makes PulseAudio loop its audio back into the sender: never leave one."""
        if not internal:
            return
        try:
            bz.call(dp, IF_DEVICE, "Disconnect", None, 15000)
        except ClassicError:
            pass

    def bring_up():
        try:
            return bring_up_inner()
        except ClassicError:
            drop_internal()
            raise

    def bring_up_inner():
        """connect -> audio card -> A2DP profile -> sink. Returns info dict."""
        info = {}
        t0 = time.time()
        if not bz.get(dp, IF_DEVICE, "Connected"):
            say("*", "connecting %s via hci%d" % (addr, bz.hci))
            info["connect_ms"] = round(do_connect(bz, addr, a.connect_timeout, prefer_bredr=True), 1)
            say("+", "connected in %.0f ms" % info["connect_ms"])
        card = wait_for(lambda: au.card_for(addr), 20)
        if not card and not STOP.is_set():
            # the audio profile can lose a race with the link right after pairing: one clean reconnect fixes it
            say("!", "no audio card yet - reconnecting once")
            try:
                bz.call(dp, IF_DEVICE, "Disconnect", None, 15000)
            except ClassicError:
                pass
            wait_for(lambda: not bz.get(dp, IF_DEVICE, "Connected"), 8)
            info["connect_ms"] = round(do_connect(bz, addr, a.connect_timeout, prefer_bredr=True), 1)
            card = wait_for(lambda: au.card_for(addr), 20)
        if not card:
            die("connected, but the sound server created no audio card for %s within 20 s (does the DUT "
                "offer A2DP? check `info`)" % addr)
        prof = pick_a2dp_profile(card, a.codec)
        if not prof:
            die("PipeWire card %s has no A2DP profile (profiles: %s)"
                % (card["name"], ", ".join((card.get("profiles") or {}).keys())))
        if card.get("active_profile") != prof:
            say("*", "setting card profile %s" % prof)
            rc, txt = au.set_profile(card, prof)
            if rc != 0:
                die("could not set profile %s: %s" % (prof, txt.strip()))
        sink = wait_for(lambda: au.sink_for(addr), 20)
        if not sink:
            die("A2DP sink node did not appear within 20 s of selecting %s" % prof)
        if au.backend == "pulse" and not load:
            die("the A2DP test counts audio underruns with PipeWire, but this unit's Bluetooth audio is handled by "
                "PulseAudio, which cannot report them. Use the realistic-interference stream (load) here, or "
                "switch the unit to PipeWire for A2DP tests.")
        tp = wait_for(lambda: transport_info(bz, addr), 10)
        info.update({"profile": prof, "sink": sink["name"], "ready_ms": round((time.time() - t0) * 1000),
                     "transport": tp})
        return info

    local_tags = local_adapter_tags(bz)
    internal = addr.replace(":", "_").lower() in local_tags          # the receiver is another radio of THIS unit
    keeper = None                                                     # PulseSinkKeeper (PulseAudio + interference stream)

    # An internal receiver is another radio of this host, so the same media packet can be watched leaving one controller and
    # arriving at the other with one clock: that gives the true one-way delay and loss of the radio pair, and the reference
    # the sender-side delay is checked against (acl_meter.LinkPair).
    rx_meter = pair = None
    if internal and meter is not None:
        rx_idx = hci_of_addr(bz, addr)
        if rx_idx is not None and rx_idx != bz.hci:
            try:
                rx_meter = acl_meter.AclMeter(rx_idx, track_rtp=True).open()
                rx_meter.start()
                meter.track_rtp = True
                pair = acl_meter.LinkPair(meter, rx_meter, late_limit_ms=getattr(a, "max_delay_ms", None))
            except OSError as e:
                rx_meter = None
                say("!", "receiver-side monitor unavailable (%s) - no loopback delay/loss measurement" % e)

    def guard_loops(first=False):
        """With an internal receiver on PulseAudio, park the loopbacks that would feed the stream back (see
        Audio.pa_break_local_loops). Refuses to stream if that cannot be done: an endless audio loop between
        two radios is worse than no interference."""
        if not (internal and au.backend == "pulse"):
            return
        n = au.pa_break_local_loops(local_tags)
        if n is None:
            if first:
                try:                                  # leave nothing connected that could start looping
                    bz.call(dp, IF_DEVICE, "Disconnect", None, 15000)
                except ClassicError:
                    pass
                die("cannot protect against an audio feedback loop between this unit's two radios (this needs "
                    "PulseAudio 15 or newer, `pactl --format=json`). Not streaming to an internal receiver.")
            return
        if n:
            say("*", "parked %d PulseAudio loopback stream(s) from this unit's own radios on a silent sink" % n)
        return n

    def new_xrun():
        return NullXrunMonitor() if au.backend == "pulse" else XrunMonitor(au, node_name)

    try:
        info = bring_up()
        if internal and au.backend == "pulse":
            # the loopback appears a moment after the link comes up; wait for it so nothing streams before it is parked
            for _ in range(12):
                if guard_loops(first=True):
                    break
                pump(0.5)
        tp = info["transport"] or {}
        say("+", "A2DP ready: sink=%s codec=%s%s" % (info["sink"], tp.get("codec", "?"),
            (" %s kbit/s max" % tp["sbc"]["bitrate_kbps_max"]) if tp.get("sbc", {}).get("bitrate_kbps_max") else ""))
        state["tstate"] = tp.get("state")
        state["volume"] = tp.get("volume")
        rec.event("ready", **{k: v for k, v in info.items() if k != "transport"}, codec=tp.get("codec"))

        node_name = "bluforge-a2dp-%d" % os.getpid()
        media = None
        if a.source.startswith("file:"):
            path = a.source[5:]
            media = probe_media(path)
            say("+", "audio file: %s codec=%s%s" % (os.path.basename(path), media["codec"],
                (" %.0fs, looped" % media["duration_s"]) if media.get("duration_s") else ""))
            use_ffmpeg = bool(shutil.which("ffmpeg"))
            if not use_ffmpeg:
                pcm, rate, ch = read_wav(path)
                if not pcm:
                    die("WAV file is empty")
        else:
            say("*", "generating %s test signal" % a.source)
            pcm, rate, ch = gen_pcm(a.source), 48000, 2

        def make_streamer(sink):
            if media is not None and use_ffmpeg:
                return FfmpegStreamer(au, sink, a.source[5:], a.latency, node_name)
            return Streamer(au, sink, pcm, rate, ch, a.latency, node_name)
        streamer = make_streamer(info["sink"])
        streamer.start()
        xr = new_xrun()
        xr.start()
        if load and au.backend == "pulse":           # see PulseSinkKeeper: an internal receiver on PulseAudio suspends the stream
            keeper = PulseSinkKeeper(au)
            keeper.sink = info["sink"]
            keeper.start()
        say("+", "streaming %s to %s for %s" % (a.source, addr, ("%ds" % a.duration) if a.duration else "until stopped"))
    except BaseException:
        # any failure between connecting and the main loop (bad audio file, missing tool, stop request ...)
        # must not leave an internal receiver connected
        drop_internal()
        if internal:
            au.pa_drain_cleanup()
        if rx_meter is not None:
            rx_meter.close()
        if keeper is not None:
            keeper.stop()
        raise

    prev_c = hci_counters(bz.hci)
    prev_s = meter.snapshot_all() if meter else None
    first_s = prev_s
    prev_t = t_run0 = time.time()
    active_s = 0.0
    total_s = 0.0
    stream_restarts = 0
    reconnect_failures = 0
    afh_last = 0.0
    afh_seen = []
    reported_delays = []            # the sink's own claim (AVDTP delay report, via BlueZ): reported, not measured
    suspends_seen = 0
    failures = []
    try:
        while not STOP.is_set():
            if a.duration and time.time() - t_run0 >= a.duration:
                break
            pump(a.interval)
            now = time.time()
            dt = now - prev_t
            prev_t = now
            if state["connected"]:
                guard_loops()
            if (not streamer.alive() or streamer.error) and state["connected"]:
                rec.event("stream_stopped", reason=streamer.error or "pw-cat exited")
                if not load:
                    failures.append("audio stream stopped unexpectedly: %s" % (streamer.error or "pw-cat exited"))
                    break
                say("!", "audio stream stopped - restarting it")
                state["connected"] = False          # falls into the bring-up path below
            if not state["connected"]:
                if not (a.reconnect or load):
                    failures.append("link lost (DUT disconnected) and reconnect is off")
                    break
                streamer.stop()
                xr.stop()
                t0 = time.time()
                try:
                    say("!", "DUT disconnected - reconnecting")
                    info = bring_up()
                except ClassicError as e:
                    if not load:
                        failures.append("reconnect failed: %s" % e)
                        break
                    # an interference source keeps trying: the air must not go quiet for good because
                    # one attempt failed (earbuds out of range for a moment, a slow audio setup, ...)
                    reconnect_failures += 1
                    rec.event("reconnect_failed", error=str(e))
                    say("!", "reconnect failed (%s) - trying again in %ds" % (e, LOAD_RETRY_S))
                    state["connected"] = False
                    pump(LOAD_RETRY_S)
                    continue
                state["connected"] = bool(bz.get(dp, IF_DEVICE, "Connected"))
                ms = round((time.time() - t0) * 1000)
                reconnects.append(ms)
                rec.event("reconnected", ms=ms)
                streamer = make_streamer(info["sink"])
                streamer.start()
                if keeper is not None:
                    keeper.sink = info["sink"]
                guard_loops()
                xr = new_xrun()
                xr.start()
                stream_restarts += 1
                prev_t = time.time()
                prev_c = hci_counters(bz.hci)
                prev_s = meter.snapshot_all() if meter else None
                continue
            kbps = done_kbps = rx_kbps = None
            if meter is not None and prev_s is not None:
                cur_s = meter.snapshot_all()
                rt = acl_meter.rates_for(prev_s, cur_s, dut_handles())
                prev_s = cur_s
                if rt:
                    kbps, done_kbps, rx_kbps = rt["tx_kbps"], rt["done_kbps"], rt["rx_kbps"]
            else:
                cur = hci_counters(bz.hci)
                if cur and prev_c and dt > 0:
                    kbps = round((cur[0] - prev_c[0]) * 8 / dt / 1000.0, 1)
                prev_c = cur
            dly = meter.interval_report(dut_handles()) if meter is not None else None
            lb, lb_lost = pair.interval() if pair is not None else (None, 0)
            sc_now = meter.avdtp_report(dut_handles()) if meter is not None else None
            n_susp = ((sc_now or {}).get("counts") or {}).get("remote_suspend", 0)
            if n_susp > suspends_seen:                      # the receiving device ended the stream: say so when it happens
                for ev in [e for e in sc_now["events"] if e["from"] == "remote" and e["signal"] == "SUSPEND"][suspends_seen:n_susp]:
                    rec.event("remote_suspend", at=ev["t_s"])
                say("!", "the receiving device suspended the stream (AVDTP SUSPEND), %d time(s) so far%s" % (
                    n_susp, "; the sink is resumed each time" if keeper is not None else ""))
                suspends_seen = n_susp
            tinfo = transport_info(bz, addr)
            tstate = tinfo["state"] if tinfo else "none"
            state["tstate"] = tstate
            if tinfo and tinfo.get("delay_ms") is not None:
                reported_delays.append(tinfo["delay_ms"])
            ls = link_stats(bz.hci, addr)
            total_s += dt
            if tstate == "active":
                active_s += dt
                if time.time() - afh_last >= AFH_EVERY_S:
                    afh_last = time.time()
                    af = afh_info(bz.hci, addr)
                    if af:
                        afh_seen.append(af)
                        rec.event("afh", channels=af["channels"], enabled=af["enabled"])
            kpresent = kw.dev is not None
            extra = {"delay_avg_ms": dly["avg"] if dly else None, "delay_p95_ms": dly["p95"] if dly else None,
                     "delay_max_ms": dly["max"] if dly else None, "jitter_ms": dly["jitter_avg"] if dly else None,
                     "delay_n": dly["n"] if dly else 0}
            if pair is not None:
                extra.update(lb_delay_avg_ms=lb["avg"] if lb else None, lb_delay_p95_ms=lb["p95"] if lb else None,
                             lb_lost=lb_lost)
            rec.sample(state=tstate, rssi=ls["rssi"], tx_power=ls["tx_power"], lq=ls["lq"],
                       tx_kbps=kbps, done_kbps=done_kbps, rx_kbps=rx_kbps, xruns=xr.err, volume=state["volume"], **extra)
            if int(rec.rel()) % 10 == 0:
                say("*", "t=%4ds state=%s rssi_offset=%s lq=%s acl_tx=%s done=%s kbit/s xruns=%d delay avg/p95/max=%s/%s/%s ms "
                         "jitter=%s ms keys=%s" % (
                    rec.rel(), tstate, ls["rssi"], ls["lq"], kbps, done_kbps, xr.err,
                    extra["delay_avg_ms"], extra["delay_p95_ms"], extra["delay_max_ms"], extra["jitter_ms"],
                    sum(state["keys"].values()) if kpresent else "n/a"))
    finally:
        t_run_end = time.time()            # before teardown: disconnecting etc. must not count against the run
        end_s = meter.snapshot_all() if meter else None
        hs_end = dut_handles()
        meter_integrity = meter.integrity() if meter else None
        delay_rep = meter.delay_report(sorted(seen_handles) or None) if meter else None
        activity = meter.activity_report() if meter else None
        stream_control = meter.avdtp_report(sorted(seen_handles) or None) if meter else None
        if meter:
            meter.close()
        pulse_resumes = None
        if keeper is not None:
            keeper.stop()
            pulse_resumes = len(keeper.resumes)
        lb_rep = None
        if pair is not None:
            rx_meter.close()
            pair.finish()
            lb_rep = pair.report()
            if rx_meter.truncated or rx_meter.error:
                lb_rep["notes"] = ["receiver-side monitor: %s" % (rx_meter.error or "%d truncated frame(s)" % rx_meter.truncated)]
        kw.stop()
        streamer.stop()
        xr.stop()
        if a.disconnect_after or internal:
            # an internal receiver is always disconnected at the end: while it stays connected, PulseAudio
            # keeps looping its audio back into the sender's sink
            try:
                bz.call(dp, IF_DEVICE, "Disconnect", None, 15000)
            except ClassicError:
                pass
        if internal:
            time.sleep(1.0)
            au.pa_drain_cleanup()

    dur = round(time.time() - rec.t0, 1)
    denom = total_s
    if load:                      # an outage is time with no interference, so it counts against the run
        denom = max(total_s, t_run_end - t_run0)
    active_pct = round(100.0 * active_s / denom, 1) if denom else 0.0
    ss = rec.samples
    summary = {
        "test": "load" if load else "a2dp", "addr": addr, "hci": bz.hci, "duration_s": dur, "source": a.source, "media": media,
        "codec": tp.get("codec"), "codec_detail": tp.get("sbc") or tp.get("config_hex"),
        "profile": info.get("profile"), "setup_ms": info.get("ready_ms"),
        "active_pct": active_pct, "transport_state_changes": state["state_changes"],
        "disconnects": state["disconnects"], "reconnect_ms": reconnects, "stream_restarts": stream_restarts,
        "xruns": xr.err, "xrun_seen": xr.seen, "xruns_measured": au.backend != "pulse",
        "sound_server": au.backend, "internal_receiver": internal,
        "rssi": stats([s.get("rssi") for s in ss]), "link_quality": stats([s.get("lq") for s in ss]),
        "tx_power": stats([s.get("tx_power") for s in ss]), "tx_kbps": stats([s.get("tx_kbps") for s in ss]),
        "done_kbps": stats([s.get("done_kbps") for s in ss]), "rx_kbps": stats([s.get("rx_kbps") for s in ss]),
        "throughput_src": (("hci-monitor (exact ACL bytes)" if meter_integrity["ok"] else
                            "hci-monitor (INCOMPLETE - see acl_integrity)") if meter else "hciconfig counters (not exact)"),
        "acl_integrity": meter_integrity,
        "link_delay": link_delay_block(delay_rep, meter_integrity, activity),
        "loopback": lb_rep,
        "sink_reported_delay_ms": sink_reported_delay(reported_delays, stream_control),
        "stream_control": stream_control,
        "pulse_sink_resumes": pulse_resumes,
        "stream_latency_setting_ms": a.latency,
        "rssi_unit": "dB offset from the golden receive power range (BR/EDR Read RSSI; 0 = in range, not dBm)",
        "avrcp_keys": state["keys"], "avrcp_volume_changes": state["vol_changes"],
        "avrcp_key_device": kw.dev, "reconnect_failures": reconnect_failures,
        "afh": ({"reads": len(afh_seen), "enabled": afh_seen[-1]["enabled"],
                 "channels_min": min(x["channels"] for x in afh_seen),
                 "channels_max": max(x["channels"] for x in afh_seen),
                 "last_map_hex": afh_seen[-1]["map_hex"], "last_unused": afh_seen[-1]["unused"]} if afh_seen else None),
        "role": "interference source" if load else "test",
        "thresholds": {
            "min_active_pct": a.min_active_pct, "max_xruns": a.max_xruns,
            "max_disconnects": a.max_disconnects, "min_rssi": a.min_rssi,
            "max_delay_ms": getattr(a, "max_delay_ms", None), "max_jitter_ms": getattr(a, "max_jitter_ms", None)},
    }
    if meter_integrity and not meter_integrity["ok"]:
        for note in meter_integrity["notes"]:
            say("!", "throughput data may be incomplete: " + note)
    if first_s is not None and end_s is not None:
        tr = acl_meter.rates_for(first_s, end_s, hs_end)
        if tr:
            summary["acl_total"] = {"seconds": tr["dt"], "tx_bytes": tr["tx_bytes"], "done_bytes": tr["done_bytes"],
                                    "rx_bytes": tr["rx_bytes"], "avg_tx_kbps": tr["tx_kbps"],
                                    "avg_done_kbps": tr["done_kbps"], "avg_rx_kbps": tr["rx_kbps"]}
    # The transport state is sampled every few seconds, which can alias with a regular stop/start cycle (the receiver suspending
    # every ~5 s): the exact time the other end held the stream suspended, from the signalling timestamps, counts too, and the
    # worse of the two decides.
    exact = (stream_control or {}).get("suspended_pct")
    effective_pct = min(active_pct, round(100.0 - exact, 1)) if exact is not None else active_pct
    summary["active_pct_sampled"] = active_pct
    summary["active_pct"] = effective_pct
    if total_s and effective_pct < a.min_active_pct:
        failures.append((("the interference stream was active only %.1f%% of the run (< %.1f%%): the effect on "
                          "your Wi-Fi measurement is understated" if load else
                          "transport active only %.1f%% of the time (< %.1f%%)") % (effective_pct, a.min_active_pct))
                        + remote_suspend_text(stream_control))
    if not load:
        if xr.err > a.max_xruns:
            failures.append("%d audio xruns/underruns (> %d)" % (xr.err, a.max_xruns))
        if state["disconnects"] > a.max_disconnects:
            failures.append("%d disconnect(s) (> %d)" % (state["disconnects"], a.max_disconnects))
        if a.min_rssi is not None and summary["rssi"] and summary["rssi"]["min"] < a.min_rssi:
            failures.append("RSSI offset dipped to %d dB (< %d)" % (summary["rssi"]["min"], a.min_rssi))
        failures.extend(delay_failures(summary["link_delay"], getattr(a, "max_delay_ms", None),
                                       getattr(a, "max_jitter_ms", None)))
    if not ss:
        failures.append("no samples collected")
    return verdict(rec, summary, failures)


# ============================================================ Classic data traffic (file / video / HID)
def sdp_rfcomm_channel(hci, addr, uuid16):
    """RFCOMM channel of the service with this 16-bit UUID on the device (SDP), or None."""
    if not shutil.which("sdptool"):
        return None
    rc, txt = _run(["sdptool", "-i", "hci%d" % hci, "records", addr], 40)
    for block in re.split(r"\n(?=Service Name:|Service RecHandle:)", txt):
        if re.search(r"\(0x%04x\)" % uuid16, block, re.I):
            m = re.search(r"RFCOMM[^\n]*\n\s*Channel:\s*(\d+)", block)
            if m:
                return int(m.group(1))
    return None


def hci_of_addr(bz, addr):
    """Index of this unit's radio that has Bluetooth address `addr`, or None."""
    try:
        for path, ifaces in bz.managed().items():
            p = ifaces.get(IF_ADAPTER)
            m = re.search(r"/hci(\d+)$", path)
            if p and m and str(p.get("Address", "")).upper() == addr.upper():
                return int(m.group(1))
    except Exception:
        pass
    return None


def job_traffic(a, bz):
    """Realistic Bluetooth Classic interference from DATA traffic: file transfer, video download or HID
    reports. Real RFCOMM/L2CAP links between this radio and another radio of this unit (the
    internal receiver), or, for file transfer, a real device that offers OBEX Object Push. Like `load` it never gives
    up on the link, does not judge the receiver and reports how much traffic it actually delivered."""
    addr = norm_addr(a.addr)
    info = bt_traffic.KIND_INFO.get(a.kind)
    if info is None:
        die("unknown traffic type %r (known: %s)" % (a.kind, ", ".join(bt_traffic.KINDS)))
    ensure_powered(bz)
    internal = addr.replace(":", "_").lower() in local_adapter_tags(bz)
    my_addr = adapter_info(bz)["addr"]
    peer_hci = hci_of_addr(bz, addr) if internal else None
    remote_channel = None
    if internal:
        dp = bz.dev_path(addr)
        if not (bz.exists(dp) and bz.get(dp, IF_DEVICE, "Paired")):
            die("hci%d is not paired with hci%s - prepare the internal receiver first (link-local)"
                % (bz.hci, peer_hci if peer_hci is not None else "?"))
    else:
        if not info["external"]:
            die("%s traffic needs a receiver that runs BluForge's sink: choose \"This unit: hciN (internal receiver)\". "
                "A real device can only receive file transfer (OBEX Object Push)." % info["label"])
        ensure_device(bz, addr, a.scan)
        dp = bz.dev_path(addr)
        if not bz.get(dp, IF_DEVICE, "Paired"):
            die("%s is not paired on hci%d - pair it first" % (addr, bz.hci))
        remote_channel = sdp_rfcomm_channel(bz.hci, addr, 0x1105)
        if remote_channel is None:
            die("%s offers no OBEX Object Push service over SDP, so it cannot receive a file transfer" % addr)
        say("+", "OBEX Object Push on RFCOMM channel %d" % remote_channel)

    rec = Recorder("traffic", addr, bz.hci)
    meter, acl_meter = None, None
    try:
        import acl_meter
        meter = acl_meter.AclMeter(bz.hci).open()
        meter.start()
    except Exception as e:
        say("!", "exact throughput meter unavailable (%s) - ACL kbit/s will be missing" % e)
        meter = None

    def dut_handles():
        if meter is None:
            return None
        hs = meter.handles_for(addr)
        if not hs:
            hs = [l["handle"] for l in acl_links(bz.hci) if l["addr"].upper() == addr and l["type"] == "ACL"]
        return hs or None

    params = {"file_mb": a.file_mb, "video_kbps": a.video_kbps}
    sess = bt_traffic.Session(a.kind, my_addr, addr, params=params, remote_channel=remote_channel)
    try:
        sess.start()
    except OSError as e:
        if meter:
            meter.close()
        die("cannot open Bluetooth data sockets on %s: %s" % (my_addr, e))
    say("+", "%s: %d stream(s) %s -> %s for %s" % (info["label"], len(sess.streams), my_addr, addr,
                                                  ("%ds" % a.duration) if a.duration else "until stopped"))
    rec.event("started", kind=a.kind, streams=[s.name for s in sess.streams])

    prev_s = meter.snapshot_all() if meter else None
    first_s = prev_s
    prev_tot = sess.totals()
    prev_t = t_run0 = time.time()
    active_s = total_s = 0.0
    afh_last = 0.0
    afh_seen = []
    failures = []
    noted = set()
    repaired, repaired_at = False, 0.0
    t_first_up = None
    try:
        while not STOP.is_set():
            if a.duration and time.time() - t_run0 >= a.duration:
                break
            pump(a.interval)
            now = time.time()
            dt = now - prev_t
            prev_t = now
            if sess.auth_denied():
                if internal and not repaired:
                    repaired, repaired_at = True, time.time()
                    say("!", "the kernel refused the link (stale pairing between the two radios) - pairing them afresh")
                    rec.event("repair_pairing")
                    try:
                        act_link_local(argparse.Namespace(peer=peer_hci, pin="0000", timeout=40, scan=20, force=True), bz)
                    except ClassicError as e:
                        failures.append("could not repair the pairing between the radios: %s" % e)
                        break
                elif repaired and time.time() - repaired_at < 3 * bt_traffic.RETRY_S:
                    pass                    # the streams retry every RETRY_S: give them time to use the new pairing
                else:
                    failures.append("the link was still refused after re-pairing the radios" if internal else
                                    "the link was refused: pair %s again on the Bluetooth Classic tab" % addr)
                    break
            for st_ in sess.streams:
                if st_.note and st_.name not in noted:
                    noted.add(st_.name)
                    say("!", "%s: %s" % (st_.name, st_.note))
                    rec.event("note", stream=st_.name, text=st_.note)
            tot = sess.totals()
            up = sess.all_up()
            app_kbps = round((tot["rx"] - prev_tot["rx"]) * 8 / dt / 1000.0, 1) if dt > 0 else None
            prev_tot = tot
            kbps = done_kbps = rx_kbps = None
            if meter is not None and prev_s is not None:
                cur_s = meter.snapshot_all()
                rt = acl_meter.rates_for(prev_s, cur_s, dut_handles())
                prev_s = cur_s
                if rt:
                    kbps, done_kbps, rx_kbps = rt["tx_kbps"], rt["done_kbps"], rt["rx_kbps"]
            ls = link_stats(bz.hci, addr)
            total_s += dt
            if up:
                if t_first_up is None:
                    t_first_up = now - dt
                    rec.event("links_up", setup_s=round(t_first_up - t_run0, 1))
                active_s += dt
                if time.time() - afh_last >= AFH_EVERY_S:
                    afh_last = time.time()
                    af = afh_info(bz.hci, addr)
                    if af:
                        afh_seen.append(af)
                        rec.event("afh", channels=af["channels"], enabled=af["enabled"])
            # airtime used is both directions: a download arrives at this radio (rx), an upload leaves it (tx)
            acl_kbps = round((kbps or 0) + (rx_kbps or 0), 1) if (kbps is not None or rx_kbps is not None) else None
            rec.sample(state="active" if up else "down", rssi=ls["rssi"], tx_power=ls["tx_power"], lq=ls["lq"],
                       tx_kbps=kbps, done_kbps=done_kbps, rx_kbps=rx_kbps, acl_kbps=acl_kbps, app_kbps=app_kbps,
                       streams_up=tot["up"])
            if int(rec.rel()) % 10 == 0:
                say("*", "t=%4ds streams up %d/%d  delivered=%s kbit/s  acl=%s kbit/s  drops=%d" % (
                    rec.rel(), tot["up"], tot["streams"], app_kbps, acl_kbps, tot["drops"]))
    finally:
        t_run_end = time.time()
        end_s = meter.snapshot_all() if meter else None
        hs_end = dut_handles()
        meter_integrity = meter.integrity() if meter else None
        if meter:
            meter.close()
        sess.stop()
        if internal:                       # leave no ACL link between this unit's own radios behind
            try:
                bz.call(bz.dev_path(addr), IF_DEVICE, "Disconnect", None, 15000)
            except ClassicError:
                pass

    tot = sess.totals()
    ss = rec.samples
    dur = round(time.time() - rec.t0, 1)
    # setup is not an outage: the share is measured from the moment the links first came up (never up = 0 %); after
    # that an outage is time with no interference
    denom = (t_run_end - t_first_up) if t_first_up else (t_run_end - t_run0)
    active_pct = min(100.0, round(100.0 * active_s / denom, 1)) if denom > 0 else 0.0
    busy = [s for s in ss if (s.get("app_kbps") or 0) > 0]
    summary = {
        "test": "traffic", "kind": a.kind, "kind_label": info["label"], "addr": addr, "hci": bz.hci,
        "duration_s": dur, "internal_receiver": internal, "role": "interference source",
        "streams": sess.report(), "active_pct": active_pct,
        "busy_pct": round(100.0 * len(busy) / len(ss), 1) if ss else 0.0,
        "delivered_bytes": tot["rx"], "sent_bytes": tot["tx"],
        "disconnects": tot["drops"], "reconnect_failures": tot["connect_failures"],
        "reconnects": max(0, tot["connects"] - tot["streams"]), "pairing_repaired": repaired,
        "app_kbps": stats([s.get("app_kbps") for s in ss]), "acl_kbps": stats([s.get("acl_kbps") for s in ss]),
        "setup_s": round(t_first_up - t_run0, 1) if t_first_up else None,
        "rssi": stats([s.get("rssi") for s in ss]), "link_quality": stats([s.get("lq") for s in ss]),
        "tx_power": stats([s.get("tx_power") for s in ss]), "tx_kbps": stats([s.get("tx_kbps") for s in ss]),
        "done_kbps": stats([s.get("done_kbps") for s in ss]), "rx_kbps": stats([s.get("rx_kbps") for s in ss]),
        "throughput_src": (("hci-monitor (exact ACL bytes)" if meter_integrity["ok"] else
                            "hci-monitor (INCOMPLETE - see acl_integrity)") if meter else "unavailable"),
        "acl_integrity": meter_integrity,
        "rssi_unit": "dB offset from the golden receive power range (BR/EDR Read RSSI; 0 = in range, not dBm)",
        "afh": ({"reads": len(afh_seen), "enabled": afh_seen[-1]["enabled"],
                 "channels_min": min(x["channels"] for x in afh_seen),
                 "channels_max": max(x["channels"] for x in afh_seen),
                 "last_map_hex": afh_seen[-1]["map_hex"], "last_unused": afh_seen[-1]["unused"]} if afh_seen else None),
        "thresholds": {"min_active_pct": a.min_active_pct},
    }
    if meter_integrity and not meter_integrity["ok"]:
        for note in meter_integrity["notes"]:
            say("!", "throughput data may be incomplete: " + note)
    if first_s is not None and end_s is not None:
        tr = acl_meter.rates_for(first_s, end_s, hs_end)
        if tr:
            summary["acl_total"] = {"seconds": tr["dt"], "tx_bytes": tr["tx_bytes"], "done_bytes": tr["done_bytes"],
                                    "rx_bytes": tr["rx_bytes"], "avg_tx_kbps": tr["tx_kbps"],
                                    "avg_done_kbps": tr["done_kbps"], "avg_rx_kbps": tr["rx_kbps"]}
    if total_s and active_pct < a.min_active_pct:
        failures.append("the traffic links were up only %.1f%% of the run (< %.1f%%): the effect on your Wi-Fi "
                        "measurement is understated" % (active_pct, a.min_active_pct))
    if not ss:
        failures.append("no samples collected")
    return verdict(rec, summary, failures)


# ============================================================ AVRCP button watch
def job_avrcp_watch(a, bz):
    addr = norm_addr(a.addr)
    dp = bz.dev_path(addr)
    if not bz.exists(dp) or not bz.get(dp, IF_DEVICE, "Connected"):
        die("%s is not connected on hci%d - connect it first" % (addr, bz.hci))
    expect = [x.strip().upper() for x in a.expect.split(",") if x.strip()]
    rec = Recorder("avrcp-watch", addr, bz.hci)
    keys, vols = {}, []
    tinfo = transport_info(bz, addr)
    last_vol = [tinfo.get("volume") if tinfo else None]

    def count(k):
        keys[k] = keys.get(k, 0) + 1

    def on_key(k):
        count(k)
        rec.event("avrcp_key", key=k)
        say("+", "DUT sent AVRCP key: %s" % k)

    def on_sig(kind, path, i, changed):
        if kind == "props" and path.startswith(dp) and "Volume" in changed:
            v = changed["Volume"]
            vols.append(v)
            # absolute volume carries a level, not a direction: infer it from the previous level
            d = None if last_vol[0] is None else ("VOLUMEUP" if v > last_vol[0] else "VOLUMEDOWN" if v < last_vol[0] else None)
            last_vol[0] = v
            if d:
                count(d)
            rec.event("volume", value=v, direction=d)
            say("+", "DUT changed absolute volume -> %s%s" % (v, " (%s)" % d if d else ""))
        elif kind == "props" and path.startswith(dp) and path.endswith("player0") and changed:
            rec.event("player", **{k: str(v) for k, v in changed.items()})
        elif kind == "props" and path == dp and changed.get("Connected") is False:
            rec.event("disconnect")
            say("x", "DUT disconnected")
    bz.subscribe(on_sig)
    kw = KeyWatcher(addr, on_key, bz.get(dp, IF_DEVICE, "Alias"), bz.get(bz.adapter, IF_ADAPTER, "Address"))
    kw.start()

    def seen(k):
        # many earbuds send discrete PLAY and PAUSE instead of one toggle key
        if k == "PLAYPAUSE":
            return any(x in keys for x in ("PLAYPAUSE", "PLAY", "PAUSE"))
        return k in keys
    say("*", "watching AVRCP from %s - press the DUT's buttons%s" % (
        addr, (" (expecting: %s)" % ", ".join(expect)) if expect else ""))
    try:
        while not STOP.is_set():
            if a.duration and time.time() - rec.t0 >= a.duration:
                break
            pump(0.5)
            if expect and all(seen(k) for k in expect):
                break
            if kw.dev is None and int(rec.rel()) == 5:
                say("!", "no '(AVRCP)' input device for this DUT yet (it may not send passthrough keys)")
    finally:
        kw.stop()
    missing = [k for k in expect if not seen(k)]
    summary = {"test": "avrcp-watch", "addr": addr, "hci": bz.hci, "duration_s": round(time.time() - rec.t0, 1),
               "keys": keys, "volume_changes": vols, "expected": expect, "missing": missing,
               "key_device": kw.dev}
    failures = ["never saw: %s" % ", ".join(missing)] if missing else []
    if not keys and not vols and not expect:
        failures.append("no AVRCP activity observed")
    return verdict(rec, summary, failures)


# ============================================================ connect/disconnect cycling
def job_cycle(a, bz):
    addr = norm_addr(a.addr)
    ensure_powered(bz)
    ensure_device(bz, addr, a.scan)
    dp = bz.dev_path(addr)
    if not bz.get(dp, IF_DEVICE, "Paired"):
        die("%s is not paired on hci%d - pair it first" % (addr, bz.hci))
    au = Audio(a.audio_user)
    rec = Recorder("cycle", addr, bz.hci)
    ok_n = 0
    say("*", "connect/disconnect x%d on hci%d" % (a.count, bz.hci))
    for i in range(1, a.count + 1):
        if STOP.is_set():
            break
        row = {"n": i}
        try:
            if bz.get(dp, IF_DEVICE, "Connected"):
                t0 = time.time()
                bz.call(dp, IF_DEVICE, "Disconnect", None, 20000)
                row["disconnect_ms"] = round((time.time() - t0) * 1000)
                pump(a.gap)
            t0 = time.time()
            do_connect(bz, addr, a.connect_timeout, prefer_bredr=True)      # this test waits for the A2DP transport
            row["connect_ms"] = round((time.time() - t0) * 1000)
            tp = wait_for(lambda: transport_info(bz, addr), a.profile_timeout)
            row["transport_ms"] = round((time.time() - t0) * 1000) if tp else None
            if au.ok and tp:
                sk = wait_for(lambda: au.sink_for(addr), a.profile_timeout)
                row["sink_ms"] = round((time.time() - t0) * 1000) if sk else None
            row["rssi"] = link_stats(bz.hci, addr)["rssi"]
            row["ok"] = tp is not None
            if not tp:
                row["error"] = "connected but no A2DP transport within %ds" % a.profile_timeout
        except ClassicError as e:
            row["ok"] = False
            row["error"] = str(e)
        ok_n += 1 if row["ok"] else 0
        rec.sample(**row)
        say("+" if row["ok"] else "x", "cycle %d/%d: %s" % (i, a.count, json.dumps(row)))
        pump(a.gap)
    n = len(rec.samples)
    rate = round(100.0 * ok_n / n, 1) if n else 0.0
    summary = {"test": "cycle", "addr": addr, "hci": bz.hci, "duration_s": round(time.time() - rec.t0, 1),
               "cycles": n, "ok": ok_n, "success_pct": rate,
               "connect_ms": stats([s.get("connect_ms") for s in rec.samples if s.get("ok")]),
               "transport_ms": stats([s.get("transport_ms") for s in rec.samples]),
               "sink_ms": stats([s.get("sink_ms") for s in rec.samples]),
               "errors": [s["error"] for s in rec.samples if s.get("error")][:10]}
    failures = []
    if rate < a.min_success:
        failures.append("success rate %.1f%% (< %.1f%%)" % (rate, a.min_success))
    return verdict(rec, summary, failures)


# ============================================================ HFP (host = Audio Gateway)
def sdp_hfp_channel(hci, addr):
    """RFCOMM channel of the DUT's Hands-Free service (0x111e) via SDP."""
    if not shutil.which("sdptool"):
        die("sdptool missing (bluez-deprecated): pass --channel N explicitly")
    # `records` (not `browse`): many earbuds do not publish a browse group, so
    # `browse` returns an empty list even though the records exist.
    rc, txt = _run(["sdptool", "-i", "hci%d" % hci, "records", addr], 40)
    for block in re.split(r"\n(?=Service Name:|Service RecHandle:)", txt):
        if re.search(r"\(0x111e\)", block, re.I):
            m = re.search(r"RFCOMM[^\n]*\n\s*Channel:\s*(\d+)", block)
            if m:
                return int(m.group(1))
    die("%s advertises no Hands-Free (HFP HF) service over SDP - the DUT does not support HFP "
        "(or is not in range). SDP said: %s" % (addr, txt.strip().splitlines()[-1] if txt.strip() else "nothing"))


def tone8k(seconds=2.0):
    n = int(8000 * seconds)
    return b"".join(struct.pack("<h", int(0.3 * 32767 * math.sin(2 * math.pi * 440 * i / 8000.0))) for i in range(n))


def job_hfp(a, bz):
    """HFP test: RFCOMM service-level connection, optional simulated call and a
    SCO audio link carrying a tone, with link metrics. CVSD air coding only
    (no mSBC wide-band codec negotiation is offered to the DUT)."""
    import hfp_ag
    addr = norm_addr(a.addr)
    ensure_powered(bz)
    ensure_device(bz, addr, a.scan)
    dp = bz.dev_path(addr)
    if not bz.get(dp, IF_DEVICE, "Paired"):
        die("%s is not paired on hci%d - pair it first" % (addr, bz.hci))
    local = bz.get(bz.adapter, IF_ADAPTER, "Address")
    rec = Recorder("hfp", addr, bz.hci)
    failures = []
    ch = a.channel or sdp_hfp_channel(bz.hci, addr)
    say("*", "HFP: connecting RFCOMM channel %d on %s from hci%d" % (ch, addr, bz.hci))
    rf = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    t0 = time.time()
    try:
        rf.bind((local, 0))
        rf.settimeout(a.connect_timeout)
        rf.connect((addr, ch))
    except OSError as e:
        rf.close()
        die("RFCOMM connect to %s ch %d failed: %s" % (addr, ch, e))
    rfcomm_ms = round((time.time() - t0) * 1000)
    rec.event("rfcomm_connected", ms=rfcomm_ms, channel=ch)
    ag = hfp_ag.AudioGateway(rf, log=lambda m: say("*", m) if a.verbose else None)
    sco = None
    sco_ms = None
    tx_bytes = rx_bytes = 0
    mtu = 48
    try:
        t1 = time.time()
        slc = ag.establish_slc(a.slc_timeout)
        slc_ms = round((time.time() - t1) * 1000) if slc else None
        rec.event("slc", ok=slc, ms=slc_ms, hf_features=ag.hf_features, hf_codecs=ag.hf_codecs)
        if not slc:
            failures.append("service-level connection did not complete within %ds" % a.slc_timeout)
        elif a.scenario != "slc":
            call_active = False
            if a.scenario == "call":
                ag.ring(a.number)
                say("*", "ringing the DUT (up to %ds for it to answer)" % a.ring_timeout)
                end = time.time() + a.ring_timeout
                while time.time() < end and not ag.answered and not ag.hung_up and not STOP.is_set():
                    ag.pump(0.2)
                if ag.answered:
                    say("+", "DUT answered")
                    rec.event("answered")
                else:
                    say("!", "no answer from the DUT - activating the call from the AG side")
                    ag.activate_call()
                call_active = True
            t2 = time.time()
            sco = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_SCO)
            try:
                sco.bind(local.encode())
                sco.settimeout(a.connect_timeout)
                sco.connect(addr.encode())
                sco_ms = round((time.time() - t2) * 1000)
                try:
                    mtu = struct.unpack("H", sco.getsockopt(17, 1, 2))[0] or 48     # SOL_SCO / SCO_OPTIONS
                except OSError:
                    pass
                rec.event("sco_up", ms=sco_ms, mtu=mtu)
                say("+", "SCO audio link up in %d ms (MTU %d)" % (sco_ms, mtu))
            except OSError as e:
                failures.append("SCO audio link failed: %s" % e)
                sco.close()
                sco = None
            if sco:
                sco.setblocking(False)
                pcm = tone8k()
                pos = 0
                period = mtu / 16000.0                      # 8 kHz * 16 bit = 16000 B/s
                nxt = time.time()
                t_run0 = last = time.time()
                prev = (0, 0)
                while not STOP.is_set() and (not a.duration or time.time() - t_run0 < a.duration):
                    now = time.time()
                    if now >= nxt:
                        frame = pcm[pos:pos + mtu]
                        pos = (pos + mtu) % (len(pcm) - mtu)
                        try:
                            tx_bytes += sco.send(frame)
                        except BlockingIOError:
                            pass
                        except OSError as e:
                            failures.append("SCO write failed: %s" % e)
                            break
                        nxt += period
                    try:
                        rx_bytes += len(sco.recv(512))
                    except BlockingIOError:
                        pass
                    except OSError as e:
                        failures.append("SCO read failed: %s" % e)
                        break
                    try:
                        ag.pump(0.001)
                    except hfp_ag.HfpError as e:
                        failures.append("RFCOMM control link lost: %s" % e)
                        rec.event("disconnect")
                        break
                    if ag.hung_up:
                        rec.event("dut_hangup")
                        break
                    if now - last >= a.interval:
                        dt = now - last
                        last = now
                        ls = link_stats(bz.hci, addr)
                        rec.sample(sco_tx_Bps=round((tx_bytes - prev[0]) / dt), sco_rx_Bps=round((rx_bytes - prev[1]) / dt),
                                   rssi=ls["rssi"], lq=ls["lq"], tx_power=ls["tx_power"])
                        prev = (tx_bytes, rx_bytes)
                    time.sleep(0.0005)
            if call_active:
                ag.end_call()
    except hfp_ag.HfpError as e:
        failures.append(str(e))
    except Exception as e:
        # e.g. an OSError from ag.end_call()/ring() on a link the DUT just dropped. Before this, anything
        # but HfpError escaped the job and no summary or result file was written at all.
        failures.append("HFP test aborted: %s: %s" % (type(e).__name__, e))
        rec.event("error", error="%s: %s" % (type(e).__name__, e))
    finally:
        for sk in (sco, rf):
            try:
                if sk:
                    sk.close()
            except OSError:
                pass
    dur = round(time.time() - rec.t0, 1)
    ss = rec.samples
    summary = {"test": "hfp", "addr": addr, "hci": bz.hci, "duration_s": dur, "scenario": a.scenario,
               "rfcomm_channel": ch, "rfcomm_ms": rfcomm_ms, "slc_ok": ag.slc,
               "hf_features": ag.hf_features, "hf_codecs": ag.hf_codecs, "codec": "CVSD",
               "dut_answered": ag.answered, "sco_ms": sco_ms, "sco_mtu": mtu,
               "sco_tx_bytes": tx_bytes, "sco_rx_bytes": rx_bytes,
               "speaker_gain": ag.speaker, "mic_gain": ag.mic, "protocol_timeline": ag.events[-40:],
               "rssi": stats([x.get("rssi") for x in ss]), "link_quality": stats([x.get("lq") for x in ss])}
    if a.scenario != "slc" and ag.slc and sco_ms is not None:
        run_s = max((ss[-1]["t"] - ss[0]["t"]) if len(ss) > 1 else 0, 0.001)
        expect = 16000 * (a.duration or run_s)
        summary["sco_tx_pct_of_expected"] = round(100.0 * tx_bytes / expect, 1) if expect else None
        if summary["sco_tx_pct_of_expected"] is not None and a.duration and summary["sco_tx_pct_of_expected"] < 90:
            failures.append("SCO sent only %.0f%% of the expected audio" % summary["sco_tx_pct_of_expected"])
        if rx_bytes == 0:
            failures.append("no microphone audio received from the DUT over SCO")
    return verdict(rec, summary, failures)


JOBS = {"a2dp": job_a2dp, "load": job_a2dp, "traffic": job_traffic, "avrcp-watch": job_avrcp_watch, "cycle": job_cycle,
        "hfp": job_hfp}


# ============================================================ CLI
def build_parser():
    ap = argparse.ArgumentParser(description="Bluetooth Classic test engine (BlueZ + PipeWire)")
    ap.add_argument("--hci", type=int, default=None, help="controller index (hciN) to use")
    ap.add_argument("--audio-user", default=None, help="desktop user whose PipeWire session to use (default: auto)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("preflight", help="check dependencies on this machine")

    p = sub.add_parser("adapter", help="show / change adapter state")
    p.add_argument("--power", choices=["on", "off"])
    p.add_argument("--pairable", choices=["on", "off"])
    p.add_argument("--discoverable", choices=["on", "off"])

    p = sub.add_parser("scan", help="inquiry for nearby Classic devices")
    p.add_argument("-t", "--duration", type=int, default=10)
    p.add_argument("--transport", choices=["bredr", "auto"], default="bredr")

    sub.add_parser("devices", help="devices BlueZ already knows on this adapter")

    def addr_parser(name, help_, **extra):
        q = sub.add_parser(name, help=help_)
        q.add_argument("addr")
        return q

    addr_parser("info", "details, profiles, link stats")
    q = addr_parser("interrogate", "connect read-only; list GATT services/characteristics and BR/EDR profiles (SDP)")
    q.add_argument("--transport", choices=["auto", "le", "bredr"], default="auto",
                   help="discovery transport if the device is not known yet")
    q.add_argument("--scan", type=int, default=15, help="discovery seconds if the device is not known yet")
    q.add_argument("--timeout", type=float, default=25, help="connect timeout")
    q.add_argument("--resolve-timeout", type=int, default=15, help="seconds to wait for GATT services to resolve")
    q.add_argument("--read-budget", type=int, default=25, help="max seconds spent reading characteristic values")
    q.add_argument("--no-read", dest="read", action="store_false", help="list services only, read no values")
    q.add_argument("--no-sdp", action="store_true")
    q = sub.add_parser("link-local", help="prepare another radio of this unit as the A2DP receiver for --hci")
    q.add_argument("peer", type=int, help="hci index of the receiving radio")
    q.add_argument("--pin", default="0000")
    q.add_argument("--timeout", type=float, default=40)
    q.add_argument("--scan", type=int, default=20)

    q = addr_parser("pair", "pair + trust")
    q.add_argument("--pin", default="0000")
    q.add_argument("--timeout", type=float, default=40)
    q.add_argument("--scan", type=int, default=20, help="inquiry seconds if the device is not known yet")
    q = addr_parser("connect", "connect all profiles")
    q.add_argument("--timeout", type=float, default=30)
    q.add_argument("--scan", type=int, default=20)
    q.add_argument("--wait-audio", type=int, default=10, help="seconds to wait for the A2DP transport (0=skip)")
    addr_parser("disconnect", "disconnect")
    addr_parser("remove", "unpair and forget")
    q = addr_parser("avrcp-player", "control the DUT's media player (phones)")
    q.add_argument("pcmd", choices=["status"] + sorted(PLAYER_CMDS))
    q = addr_parser("avrcp-volume", "AVRCP absolute-volume test")
    q.add_argument("--steps", default="0,32,64,96,127,64")
    q.add_argument("--timeout", type=float, default=3.0)

    q = addr_parser("a2dp", "A2DP source stream test with metrics")
    q.add_argument("-t", "--duration", type=int, default=60, help="seconds (0 = until stopped)")
    q.add_argument("--source", default="sweep", help="tone | sweep | noise | file:/path/to.wav")
    q.add_argument("--codec", default=None, help="force a codec profile (sbc, aac, ldac, aptx...)")
    q.add_argument("--latency", type=int, default=100, help="stream latency ms")
    q.add_argument("--interval", type=float, default=1.0)
    q.add_argument("--reconnect", action="store_true", help="reconnect + resume if the DUT drops")
    q.add_argument("--disconnect-after", action="store_true")
    q.add_argument("--connect-timeout", type=float, default=30)
    q.add_argument("--scan", type=int, default=20)
    q.add_argument("--min-active-pct", type=float, default=95.0)
    q.add_argument("--max-xruns", type=int, default=5)
    q.add_argument("--max-disconnects", type=int, default=0)
    q.add_argument("--min-rssi", type=int, default=None)
    q.add_argument("--max-delay-ms", type=float, default=None,
                   help="fail if the link delay of the media packets (99th percentile, host-observed) exceeds this; not judged by default")
    q.add_argument("--max-jitter-ms", type=float, default=None,
                   help="fail if the mean delay variation between consecutive media packets exceeds this; not judged by default")
    q.set_defaults(load=False)

    q = addr_parser("load", "realistic Bluetooth Classic interference: stream A2DP audio to a real sink, unattended")
    q.add_argument("-t", "--duration", type=int, default=0, help="seconds (0 = until stopped)")
    q.add_argument("--source", default="noise", help="noise | sweep | tone | file:/path/to.wav")
    q.add_argument("--codec", default=None, help="force a codec profile (sbc, aac, ldac, aptx...)")
    q.add_argument("--latency", type=int, default=100, help="stream latency ms")
    q.add_argument("--interval", type=float, default=1.0)
    q.add_argument("--connect-timeout", type=float, default=30)
    q.add_argument("--scan", type=int, default=20)
    q.add_argument("--min-active-pct", type=float, default=80.0,
                   help="the run is flagged if the stream was active less than this share of the time")
    q.set_defaults(load=True, reconnect=True, disconnect_after=False, max_xruns=None, max_disconnects=None, min_rssi=None,
                   max_delay_ms=None, max_jitter_ms=None)

    q = addr_parser("traffic", "realistic Bluetooth Classic data traffic (file transfer, video, HID), unattended")
    q.add_argument("--kind", choices=bt_traffic.KINDS, default="file")
    q.add_argument("-t", "--duration", type=int, default=0, help="seconds (0 = until stopped)")
    q.add_argument("--file-mb", type=float, default=None, help="size of each transferred file (default %s)" % bt_traffic.DEFAULTS["file_mb"])
    q.add_argument("--video-kbps", type=int, default=None, help="video bitrate (default %s)" % bt_traffic.DEFAULTS["video_kbps"])
    q.add_argument("--interval", type=float, default=1.0)
    q.add_argument("--scan", type=int, default=20)
    q.add_argument("--min-active-pct", type=float, default=80.0,
                   help="the run is flagged if the links were up less than this share of the time")

    q = addr_parser("avrcp-watch", "observe DUT button presses / volume changes")
    q.add_argument("-t", "--duration", type=int, default=120)
    q.add_argument("--expect", default="", help="comma list, e.g. PLAYPAUSE,NEXT,PREVIOUS,VOLUMEUP")

    q = addr_parser("hfp", "HFP test: host = Audio Gateway, DUT = Hands-Free")
    q.add_argument("-t", "--duration", type=int, default=20, help="SCO audio seconds")
    q.add_argument("--scenario", choices=["slc", "sco", "call"], default="call",
                   help="slc = service-level connection only; sco = + audio link; call = + simulated incoming call")
    q.add_argument("--channel", type=int, default=0, help="RFCOMM channel (default: look up via SDP)")
    q.add_argument("--number", default="+15551234567", help="caller ID shown on the DUT")
    q.add_argument("--ring-timeout", type=int, default=15)
    q.add_argument("--slc-timeout", type=int, default=10)
    q.add_argument("--connect-timeout", type=float, default=20)
    q.add_argument("--interval", type=float, default=1.0)
    q.add_argument("--scan", type=int, default=20)
    q.add_argument("--verbose", action="store_true", help="log every AT command")

    q = addr_parser("cycle", "connect/disconnect reliability loop")
    q.add_argument("-n", "--count", type=int, default=10)
    q.add_argument("--gap", type=float, default=3.0)
    q.add_argument("--connect-timeout", type=float, default=30)
    q.add_argument("--profile-timeout", type=int, default=15)
    q.add_argument("--min-success", type=float, default=100.0)
    q.add_argument("--scan", type=int, default=20)
    return ap


def main():
    a = build_parser().parse_args()
    cmd = a.cmd
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    try:
        if cmd == "preflight":
            print(json.dumps(act_preflight(a)))
            return 0
        if a.hci is None:
            die("--hci N is required")
        bz = BZ(a.hci)
        if cmd in JOBS:
            return JOBS[cmd](a, bz)
        res = ACTIONS[cmd](a, bz)
        print(json.dumps(res, default=str))
        return 0
    except ClassicError as e:
        say("x", str(e))
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    except Exception as e:                                     # never a bare traceback to the GUI
        import traceback
        traceback.print_exc()
        print(json.dumps({"ok": False, "error": "internal error: %s: %s" % (type(e).__name__, e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
