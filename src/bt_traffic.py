"""Bluetooth Classic data traffic for realistic interference: file transfer (OBEX Object Push), video download and
keyboard/mouse reports.

Each traffic type is a real Classic data link (RFCOMM or L2CAP over BR/EDR ACL) between two radios, shaped like the
application it stands for. The receiving end is a sink run by this module on the other radio of the same unit (or, for
file transfer only, a real device that offers OBEX Object Push). Nothing here touches BlueZ's D-Bus API: the radios must
already be paired (see `link-local`), because an unpaired link would ask for a pairing confirmation nobody gives.

No radio is needed to test it: Session takes a `transport` object, and the tests give it one that uses loopback TCP."""
import os
import socket
import struct
import threading
import time

RETRY_S = 5                  # pause before a lost link is reconnected (an interference source never gives up)
CONNECT_TIMEOUT_S = 15
SLOW_ANSWER_S = 10           # an OBEX receiver slower than this is most likely waiting for a user to accept the file
IO_TIMEOUT_S = 2             # socket timeout: how often a blocked send/recv notices a stop request
PAYLOAD = os.urandom(65536)  # incompressible filler; every send is a slice of this

# kind -> label, what it imitates, whether a real (non-BluForge) device can be the receiver
KIND_INFO = {
    "file":  {"label": "File transfer (OBEX Object Push)", "external": True,
              "about": "back-to-back files over RFCOMM with OBEX framing, like sending photos or documents"},
    "video": {"label": "Video download (streaming)", "external": False,
              "about": "segment bursts at a video bitrate over L2CAP, like a streamed video"},
    "hid":   {"label": "Keyboard / mouse (HID)", "external": False,
              "about": "tiny L2CAP reports at 125 per second, like a mouse in use"},
}
KINDS = tuple(KIND_INFO)

DEFAULTS = {"file_mb": 2, "video_kbps": 1000, "segment_s": 4, "startup_segments": 3, "file_gap_s": 2,
            "obex_pkt": 16384, "hid_bytes": 8, "hid_interval_ms": 8}


def plan(kind, p=None):
    """The streams a traffic type is made of: a list of dicts (name, proto, model, direction + shape parameters).
    `direction` is from the sending radio's point of view: up = it sends, down = it receives."""
    q = dict(DEFAULTS)
    q.update({k: v for k, v in (p or {}).items() if v is not None})
    if kind not in KIND_INFO:
        raise ValueError("unknown traffic type %r (known: %s)" % (kind, ", ".join(KINDS)))
    file_ = {"name": "file", "proto": "rfcomm", "model": "obex", "direction": "up",
             "file_bytes": int(q["file_mb"] * 1000000), "gap_s": q["file_gap_s"], "obex_pkt": q["obex_pkt"]}
    video = {"name": "video", "proto": "l2cap", "model": "video", "direction": "down",
             "bitrate_kbps": q["video_kbps"], "segment_s": q["segment_s"], "startup": q["startup_segments"]}
    hid = {"name": "hid", "proto": "l2cap", "model": "paced", "direction": "up",
           "size": q["hid_bytes"], "interval_s": q["hid_interval_ms"] / 1000.0}
    return [{"file": file_, "video": video, "hid": hid}[kind]]


# ------------------------------------------------------------------ transports
class BtTransport:
    """Real Bluetooth Classic sockets. Server channels/PSMs are chosen by the kernel: fixed dynamic PSMs (0x1001 ...)
    are already taken by other services on a typical BlueZ host."""
    AF = getattr(socket, "AF_BLUETOOTH", None)

    def _new(self, proto):
        if proto == "rfcomm":
            return socket.socket(self.AF, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        return socket.socket(self.AF, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)

    def listen(self, proto, addr):
        s = self._new(proto)
        try:
            s.bind((addr, 0))
            s.listen(1)
            return s, s.getsockname()[1]
        except OSError:
            s.close()
            raise

    def connect(self, proto, src, dst, chan, timeout):
        s = self._new(proto)
        try:
            s.bind((src, 0))                    # picks the sending radio
            s.settimeout(timeout)
            s.connect((dst, chan))
        except OSError:
            s.close()
            raise
        return s

    def mtu(self, proto, sock):
        if proto == "l2cap":                    # L2CAP_OPTIONS: omtu, imtu, ...
            return struct.unpack("HH", sock.getsockopt(6, 1, 12)[:4])[0]
        return 32768


# ------------------------------------------------------------------ OBEX (the file-transfer protocol)
OBEX_CONNECT, OBEX_DISCONNECT, OBEX_PUT, OBEX_PUT_FINAL, OBEX_GET, OBEX_GET_FINAL = 0x80, 0x81, 0x02, 0x82, 0x03, 0x83
OBEX_OK, OBEX_CREATED, OBEX_CONTINUE = 0xA0, 0xA1, 0x90
H_NAME, H_LENGTH, H_BODY, H_END_BODY = 0x01, 0xC3, 0x48, 0x49


class ObexRefused(OSError):
    pass


def obex_packet(op, body=b""):
    return struct.pack(">BH", op, 3 + len(body)) + body


def hdr_name(name):
    data = name.encode("utf-16-be") + b"\0\0"
    return struct.pack(">BH", H_NAME, 3 + len(data)) + data


def hdr_length(n):
    return struct.pack(">BI", H_LENGTH, n)


def hdr_body(final, n):
    return struct.pack(">BH", H_END_BODY if final else H_BODY, 3 + n) + PAYLOAD[:n]


def iter_headers(buf):
    """(id, value-bytes) for every OBEX header in `buf`."""
    i = 0
    while i < len(buf):
        hid = buf[i]
        enc = hid >> 6
        if enc in (0, 1):                       # unicode string / byte sequence: 2-byte length incl. the 3 prefix bytes
            ln = struct.unpack(">H", buf[i + 1:i + 3])[0]
            yield hid, buf[i + 3:i + ln]
            i += max(ln, 3)
        elif enc == 2:
            yield hid, buf[i + 1:i + 2]
            i += 2
        else:
            yield hid, buf[i + 1:i + 5]
            i += 5


def recv_exact(sock, n, stop):
    out = bytearray()
    while len(out) < n:
        if stop.is_set():
            raise ConnectionAbortedError("stopped")
        try:
            d = sock.recv(n - len(out))
        except socket.timeout:
            continue
        if not d:
            raise ConnectionResetError("link closed")
        out += d
    return bytes(out)


def read_obex(sock, stop):
    op, ln = struct.unpack(">BH", recv_exact(sock, 3, stop))
    return op, (recv_exact(sock, ln - 3, stop) if ln > 3 else b"")


def send_all(sock, data, stop):
    mv = memoryview(data)
    while len(mv) and not stop.is_set():
        try:
            k = sock.send(mv)
        except socket.timeout:
            continue
        mv = mv[k:]
    if len(mv):
        raise ConnectionAbortedError("stopped")


def obex_connect(sock, st, stop, want_pkt):
    send_all(sock, obex_packet(OBEX_CONNECT, struct.pack(">BBH", 0x10, 0, want_pkt)), stop)
    op, pl = read_obex(sock, stop)
    if op != OBEX_OK or len(pl) < 4:
        raise ObexRefused("OBEX connect refused (response 0x%02X)" % op)
    return min(want_pkt, struct.unpack(">H", pl[2:4])[0])


def obex_put(sock, st, stop, pkt, name, size):
    sent, first = 0, True
    while sent < size:
        h = (hdr_name(name) + hdr_length(size)) if first else b""
        n = min(pkt - 3 - len(h) - 3, size - sent)
        last = sent + n >= size
        send_all(sock, obex_packet(OBEX_PUT_FINAL if last else OBEX_PUT, h + hdr_body(last, n)), stop)
        st.tx += n
        sent += n
        first = False
        t_sent = time.monotonic()
        op, _ = read_obex(sock, stop)
        if time.monotonic() - t_sent > SLOW_ANSWER_S:
            st.note = ("the receiver took %.0f s to answer a file packet: it is probably waiting for someone to accept "
                       "the file, which holds the traffic back" % (time.monotonic() - t_sent))
        if op != OBEX_CONTINUE and not (last and op in (OBEX_OK, OBEX_CREATED)):
            raise ObexRefused("receiver refused the file (OBEX response 0x%02X)" % op)


def obex_get(sock, st, stop, name):
    req = obex_packet(OBEX_GET_FINAL, hdr_name(name))
    while True:
        send_all(sock, req, stop)
        op, pl = read_obex(sock, stop)
        if op not in (OBEX_OK, OBEX_CONTINUE):
            raise ObexRefused("receiver refused the download (OBEX response 0x%02X)" % op)
        st.rx += sum(len(v) for h, v in iter_headers(pl) if h in (H_BODY, H_END_BODY))
        if op == OBEX_OK:
            return
        req = obex_packet(OBEX_GET_FINAL)


def obex_client(sock, st, stop):
    pkt = obex_connect(sock, st, stop, st.p["obex_pkt"])
    i = 0
    while not stop.is_set():
        i += 1
        name = "bluforge_%d.bin" % i
        t0 = time.monotonic()
        if st.direction == "up":
            obex_put(sock, st, stop, pkt, name, st.p["file_bytes"])
        else:
            obex_get(sock, st, stop, name)
        dt = max(time.monotonic() - t0, 1e-6)
        st.n["files"] += 1
        st.rates.append(st.p["file_bytes"] * 8 / dt / 1000.0)
        stop.wait(st.p["gap_s"])
    send_all(sock, obex_packet(OBEX_DISCONNECT), threading.Event())


def obex_server(sock, st, stop):
    """Minimal OBEX Object Push / GET server: takes every file, serves `file_bytes` for every GET."""
    remaining = None
    while not stop.is_set():
        op, pl = read_obex(sock, stop)
        if op == OBEX_CONNECT:
            send_all(sock, obex_packet(OBEX_OK, struct.pack(">BBH", 0x10, 0, st.p["obex_pkt"])), stop)
        elif op == OBEX_DISCONNECT:
            send_all(sock, obex_packet(OBEX_OK), stop)
            return
        elif op in (OBEX_PUT, OBEX_PUT_FINAL):
            st.rx += sum(len(v) for h, v in iter_headers(pl) if h in (H_BODY, H_END_BODY))
            send_all(sock, obex_packet(OBEX_OK if op == OBEX_PUT_FINAL else OBEX_CONTINUE), stop)
        elif op in (OBEX_GET, OBEX_GET_FINAL):
            if remaining is None:
                remaining = st.p["file_bytes"]
            n = min(remaining, st.p["obex_pkt"] - 6)
            remaining -= n
            send_all(sock, obex_packet(OBEX_OK if remaining == 0 else OBEX_CONTINUE, hdr_body(remaining == 0, n)), stop)
            st.tx += n
            if remaining == 0:
                remaining = None
        else:
            send_all(sock, obex_packet(0xC0), stop)          # bad request


# ------------------------------------------------------------------ raw traffic shapes
def send_bytes(sock, st, stop, n):
    """Send n bytes as fast as the link takes them (one packet per send on L2CAP)."""
    sent = 0
    while sent < n and not stop.is_set():
        try:
            k = sock.send(PAYLOAD[:min(n - sent, st.mtu)])
        except socket.timeout:
            continue
        st.tx += k
        sent += k
    if sent < n:
        raise ConnectionAbortedError("stopped")


def sleep_until(t, stop):
    while not stop.is_set():
        d = t - time.monotonic()
        if d <= 0:
            return
        stop.wait(min(d, 0.5))


def video_sender(sock, st, stop):
    """Segment download: the player fills its buffer first (startup segments back to back), then fetches one segment
    per segment time. A segment that takes longer than its playback time would drain a real player's buffer."""
    seg = max(1, int(st.p["bitrate_kbps"] * 1000 / 8 * st.p["segment_s"]))
    i, grid = 0, None
    while not stop.is_set():
        t0 = time.monotonic()
        send_bytes(sock, st, stop, seg)
        st.n["segments"] += 1
        i += 1
        if i < st.p["startup"]:
            continue
        if grid is None:
            grid = time.monotonic()
        else:
            if time.monotonic() - t0 > st.p["segment_s"]:
                st.n["late_segments"] += 1
        grid += st.p["segment_s"]
        sleep_until(grid, stop)


def paced_sender(sock, st, stop):
    t = time.monotonic()
    while not stop.is_set():
        send_bytes(sock, st, stop, st.p["size"])
        t += st.p["interval_s"]
        if time.monotonic() - t > 1.0:          # fell far behind (link stall): do not burst to catch up
            t = time.monotonic()
        sleep_until(t, stop)


def drain(sock, st, stop):
    while not stop.is_set():
        try:
            d = sock.recv(65536)
        except socket.timeout:
            continue
        if not d:
            raise ConnectionResetError("link closed")
        st.rx += len(d)


SENDERS = {"video": video_sender, "paced": paced_sender}


# ------------------------------------------------------------------ session
class Stream:
    def __init__(self, spec):
        self.name, self.proto, self.model, self.direction = spec["name"], spec["proto"], spec["model"], spec["direction"]
        self.p = spec
        self.tx = self.rx = 0                   # application bytes sent / received (the receiver's count is the delivered load)
        self.up = False
        self.connects = self.drops = self.connect_failures = 0
        self.auth_denied = False
        self.err = None
        self.note = None                        # something worth telling the user that is not an error
        self.n = {"files": 0, "segments": 0, "late_segments": 0}
        self.rates = []                         # kbit/s of each completed file
        self.mtu = 32768
        self.listener = None
        self.chan = None

    def report(self):
        r = {"name": self.name, "proto": self.proto, "direction": self.direction, "tx_bytes": self.tx,
             "rx_bytes": self.rx, "connects": self.connects, "drops": self.drops,
             "connect_failures": self.connect_failures, "last_error": self.err}
        if self.note:
            r["note"] = self.note
        main = {"obex": "files", "video": "segments"}.get(self.model)
        r.update({k: v for k, v in self.n.items() if v or k == main})     # the stream's own count always; others only if used
        if self.rates:
            r["file_kbps_avg"] = round(sum(self.rates) / len(self.rates), 1)
            r["file_kbps_min"] = round(min(self.rates), 1)
        return r


class Session:
    """Runs the streams of one traffic type between `src` (sending radio) and `dst` (receiving radio, or a device).
    Never gives up: a lost link is reconnected every RETRY_S until stop()."""

    def __init__(self, kind, src, dst, transport=None, params=None, remote_channel=None):
        self.kind, self.src, self.dst = kind, src, dst
        self.t = transport or BtTransport()
        self.remote_channel = remote_channel    # set: the receiver is a real device with this OBEX channel
        self.stop_ev = threading.Event()
        self.streams = [Stream(s) for s in plan(kind, params)]
        self.threads = []
        self.socks = set()
        self.lock = threading.Lock()

    # -- lifecycle
    def start(self):
        try:
            for st in self.streams:
                if self.remote_channel is not None:
                    st.chan = self.remote_channel
                else:
                    st.listener, st.chan = self.t.listen(st.proto, self.dst)
                    st.listener.settimeout(1)
                    self._spawn(self._accept_loop, st)
                self._spawn(self._client_loop, st)
        except BaseException:
            self.stop()
            raise
        return self

    def _spawn(self, fn, *a):
        th = threading.Thread(target=fn, args=a, daemon=True, name="bt-traffic")
        th.start()
        self.threads.append(th)

    def stop(self):
        self.stop_ev.set()
        with self.lock:
            socks = list(self.socks)
        for s in socks + [st.listener for st in self.streams if st.listener]:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        for th in self.threads:
            th.join(3)

    def _track(self, s, add=True):
        with self.lock:
            (self.socks.add if add else self.socks.discard)(s)

    # -- the sending radio
    def _client_loop(self, st):
        stop = self.stop_ev
        while not stop.is_set():
            try:
                c = self.t.connect(st.proto, self.src, self.dst, st.chan, CONNECT_TIMEOUT_S)
            except PermissionError as e:        # the kernel's answer to a link that needs (re)pairing
                st.auth_denied, st.err = True, "connection refused - the radios are not (or no longer) paired: %s" % e
                stop.wait(RETRY_S)
                continue
            except OSError as e:
                st.connect_failures += 1
                st.err = "connect failed: %s" % e
                stop.wait(RETRY_S)
                continue
            st.auth_denied = False
            self._track(c)
            c.settimeout(IO_TIMEOUT_S)
            st.mtu = self.t.mtu(st.proto, c)
            st.connects += 1
            st.up = True
            try:
                if st.model == "obex":
                    obex_client(c, st, stop)
                elif st.direction == "up":
                    SENDERS[st.model](c, st, stop)
                else:
                    drain(c, st, stop)
            except OSError as e:
                if not stop.is_set():
                    st.err = str(e)
            finally:
                st.up = False
                self._track(c, False)
                try:
                    c.close()
                except OSError:
                    pass
            if not stop.is_set():
                st.drops += 1
                stop.wait(1.0)

    # -- the receiving radio
    def _accept_loop(self, st):
        while not self.stop_ev.is_set():
            try:
                c, _ = st.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._track(c)
            self._spawn(self._serve, st, c)

    def _serve(self, st, c):
        c.settimeout(IO_TIMEOUT_S)
        try:
            mtu = self.t.mtu(st.proto, c)
        except OSError:
            mtu = st.mtu
        st.mtu = min(st.mtu, mtu)
        try:
            if st.model == "obex":
                obex_server(c, st, self.stop_ev)
            elif st.direction == "down":
                SENDERS[st.model](c, st, self.stop_ev)
            else:
                drain(c, st, self.stop_ev)
        except OSError:
            pass
        finally:
            self._track(c, False)
            try:
                c.close()
            except OSError:
                pass

    # -- observation
    def all_up(self):
        return all(s.up for s in self.streams)

    def auth_denied(self):
        return any(s.auth_denied for s in self.streams)

    def first_error(self):
        return next((s.err for s in self.streams if s.err and not s.up), None)

    def totals(self):
        return {"tx": sum(s.tx for s in self.streams), "rx": sum(s.rx for s in self.streams),
                "up": sum(1 for s in self.streams if s.up), "streams": len(self.streams),
                "drops": sum(s.drops for s in self.streams), "connects": sum(s.connects for s in self.streams),
                "connect_failures": sum(s.connect_failures for s in self.streams)}

    def report(self):
        return [s.report() for s in self.streams]
