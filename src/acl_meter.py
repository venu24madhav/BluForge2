"""Exact ACL throughput, and per-packet delivery delay, from the kernel's HCI monitor channel.

Every ACL packet the host hands to a controller (and every one it receives) is
mirrored on HCI_CHANNEL_MONITOR, together with the controller's own
"Number Of Completed Packets" events. Counting those gives byte-exact rates, with
no polling of deprecated tools and no sampling error:

  tx   ACL bytes the host handed to the controller (L2CAP header + payload)
  done ACL bytes the controller reports as completed (sent and acknowledged, or
       flushed); matched to sent packets in FIFO order per connection handle
  rx   ACL bytes received from the remote

Rates are taken between two snapshots over their exact elapsed time. Over-the-air
retransmissions and baseband overhead are not visible to the host, so this is
ACL-level throughput, not raw radio bit rate.

Delay. The kernel stamps every monitor frame (SO_TIMESTAMPNS, nanoseconds, one clock for every radio of the host). The meter
reassembles L2CAP frames from their ACL fragments, and for each frame measures

  delay = (time the controller reported its LAST fragment completed) - (time its FIRST fragment was handed to the controller)

per connection handle and L2CAP channel. For an A2DP stream the busiest channel is the media channel, so its delay is the time
one audio packet needed to cross the link as the host sees it, and the variation between packets is the link's jitter.
It is not the audio latency a listener experiences: it leaves out the host's audio buffer and the sink's own buffer and decoder.
On the Intel BE200 the completion report was measured to agree with the receiving host's own timestamp of the same packet to
within 0.5 ms for 95 % of packets (loopback between two radios, see docs/A2DP_DELAY.md); LinkPair reproduces that measurement.
"""
import socket
import struct
import threading
import time
from collections import OrderedDict, deque

from hci_raw import _hci_bind, AF_BLUETOOTH, BTPROTO_HCI
import delay_stats

HCI_CHANNEL_MONITOR = 2
HCI_DEV_NONE = 0xFFFF
MON_EVENT_PKT, MON_ACL_TX, MON_ACL_RX, MON_SCO_TX, MON_SCO_RX = 3, 4, 5, 6, 7
EVT_DISCONN_COMPLETE, EVT_CONN_COMPLETE, EVT_NUM_COMPLETED, EVT_LE_META = 0x05, 0x03, 0x13, 0x3E
EVT_FLUSH_OCCURRED = 0x11
PB_START_NOFLUSH, PB_CONT, PB_START_FLUSH, PB_COMPLETE = 0, 1, 2, 3
MON_COMMAND_PKT = 2
EVT_CMD_COMPLETE, EVT_CMD_STATUS, EVT_INQUIRY_COMPLETE = 0x0E, 0x0F, 0x01
# Host commands that put the radio to other uses (the Classic link shares it): opcode -> (activity, turns it on?  None = read the
# enable byte). "le_connect" is an LE connection attempt (the controller listens continuously while it is pending).
OP_ACTIVITY = {0x200C: ("le_scan", None), 0x2042: ("le_scan", None), 0x200A: ("le_adv", None), 0x2039: ("le_adv", None),
               0x200D: ("le_connect", True), 0x2043: ("le_connect", True), 0x200E: ("le_connect", False),
               0x0401: ("inquiry", True), 0x0402: ("inquiry", False)}
# AVDTP signalling (the control channel of an A2DP link). Only the commands that start, stop or reshape the stream are kept: the
# sink suspending a stream the host is sending is the most telling, and a DELAYREPORT carries the sink's own delay claim.
AVDTP_SIGNALS = {1: "DISCOVER", 2: "GET_CAPABILITIES", 3: "SET_CONFIGURATION", 4: "GET_CONFIGURATION", 5: "RECONFIGURE",
                 6: "OPEN", 7: "START", 8: "CLOSE", 9: "SUSPEND", 10: "ABORT", 11: "SECURITY_CONTROL",
                 12: "GET_ALL_CAPABILITIES", 13: "DELAYREPORT"}
AVDTP_TRACKED = (3, 5, 6, 7, 8, 9, 10, 13)
AVDTP_KEEP = 200                                  # newest signalling events kept
AVDTP_MAX_FRAME = 64                              # signalling frames are tiny; anything larger is not one
LE_CONNECTION_COMPLETE = (0x01, 0x0A, 0x29)       # LE Meta subevents that end a pending LE connection attempt
LE_ADV_REPORTS = (0x02, 0x0D)                     # LE Meta subevents carrying advertising reports
FIFO_MAX = 4096
RCVBUF_BYTES = 8 * 1024 * 1024        # room for a burst: the kernel does not report dropped monitor frames
SO_RCVBUFFORCE = 33
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)   # the control message type has the same number
STARTUP_GRACE_PKTS = 64               # completions for packets sent just before we started are normal
MAX_BACKLOG_PKTS = 128                # a controller buffers far fewer; more means completion events were missed
MAX_CHANNELS = 32                     # (handle, L2CAP channel) delay accumulators kept at most
RTP_KEEP = 20000                      # newest RTP identities kept per direction for the loopback matcher
MAX_DELAY_MS = 60000.0                # a packet "delivered" after a minute is a bookkeeping error, not a measurement
MIN_MEDIA_FRAMES = 20                 # a channel with fewer completed frames is not reported as the media channel
MEDIA_SHARE = 0.05                    # ... nor one that carried less than this share of the busiest channel's bytes
KERNEL_CLOCK_MIN = 0.99               # share of frames that must carry a kernel timestamp for the delay to be trusted


class _Frame:
    """One L2CAP frame while its ACL fragments arrive."""
    __slots__ = ("h", "cid", "total", "got", "first_ts", "last_ts", "nfrag", "head", "rec")

    def __init__(self, h, cid, total, got, ts, head):
        self.h, self.cid, self.total, self.got = h, cid, total, got
        self.first_ts = self.last_ts = ts
        self.nfrag = 1
        self.head = head
        self.rec = None


class _Activity:
    """On/off history of one radio use, as a running total of the time it was on."""
    __slots__ = ("on", "since", "total", "starts", "seen")

    def __init__(self):
        self.on, self.since, self.total, self.starts, self.seen = False, 0, 0, 0, False

    def set(self, on, ts, window_start):
        if not self.seen:
            self.seen = True
            if not on:                    # the first thing we see is "off": it was on before the meter started
                self.total += max(0, ts - window_start)
                return
        if on and not self.on:
            self.on, self.since = True, ts
            self.starts += 1
        elif not on and self.on:
            self.on = False
            self.total += max(0, ts - self.since)


def rtp_identity(cid, head):
    """(ssrc, sequence number) when the first 12 bytes after the L2CAP header look like an RTP header of an A2DP media packet
    on a dynamic channel, else None. Signalling (AVDTP) and control (AVRCP) frames do not pass the payload-type test."""
    if cid < 0x40 or head is None or len(head) < 12:
        return None
    b0, b1, seq, _ts, ssrc = struct.unpack_from(">BBHII", head, 0)
    if (b0 & 0xC0) != 0x80 or (b1 & 0x7F) < 96:
        return None
    return ssrc, seq


class AclMeter(threading.Thread):
    def __init__(self, hci_index, track_rtp=False, late_limit_ms=None, intervals=False):
        super().__init__(daemon=True, name="acl-meter")
        self.hci = hci_index
        self.track_rtp = track_rtp          # keep the identity of every RTP-like frame (needed by LinkPair only)
        self.late_limit_ms = late_limit_ms
        self.intervals = intervals          # keep per-interval delays for interval_report() (else constant memory)
        self.sock = None
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.c = {}                 # handle -> counters
        self.addr_of = {}           # handle -> BD_ADDR (from connection-complete events seen)
        self.fifo = {}              # handle -> deque of (ACL data length, frame or None, last fragment of that frame)
        self.error = None
        self.frames = 0
        self.truncated = 0          # frames the kernel cut short (would make byte counts wrong)
        # --- delay
        self.accs = {}              # (handle, cid) -> DelayAcc
        self.cur = {}               # (is_tx, handle) -> _Frame still receiving fragments
        self.flushes = {}           # handle -> "Flush Occurred" events
        self.ts_kernel = 0          # frames that carried a kernel timestamp
        self.ts_user = 0            # frames stamped by this process on arrival (older kernels: less precise)
        self.orphans = 0            # continuation fragments with no start fragment (the meter started mid-frame)
        self.bad_delays = 0         # completions that gave a negative or absurd delay (FIFO matching went wrong)
        self.rtp_tx = deque(maxlen=RTP_KEEP)
        self.rtp_rx = deque(maxlen=RTP_KEEP)
        # --- what else the radio did while the Classic link ran
        self.act = {k: _Activity() for k in ("le_scan", "le_adv", "le_connect", "inquiry")}
        self.pend = {}              # command opcode -> (activity, on, ts) until the controller accepts it
        self.le_reports = 0         # LE advertising reports received (proof that an LE scan is running)
        self.first_ts = self.last_ts = None
        self.avdtp = deque(maxlen=AVDTP_KEEP)   # (ts, handle, sent by the host?, signal id, delay ms or None)

    # ------------------------------------------------------------- lifecycle
    def open(self):
        s = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_CLOEXEC, BTPROTO_HCI)
        try:
            _hci_bind(s, HCI_DEV_NONE, HCI_CHANNEL_MONITOR)
        except OSError:
            s.close()
            raise
        s.settimeout(0.5)
        for opt in (SO_RCVBUFFORCE, socket.SO_RCVBUF):       # root may exceed rmem_max; fall back if refused
            try:
                s.setsockopt(socket.SOL_SOCKET, opt, RCVBUF_BYTES)
                break
            except OSError:
                continue
        try:
            s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)   # without it delays fall back to arrival time in this process
        except OSError:
            pass
        self.sock = s
        return self

    def close(self):
        self.stop_ev.set()
        if self.is_alive():
            self.join(2)
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # ------------------------------------------------------------- parsing
    def _ctr(self, h):
        return self.c.setdefault(h, {"tx_b": 0, "tx_p": 0, "done_b": 0, "done_p": 0, "done_unmatched": 0,
                                     "rx_b": 0, "rx_p": 0, "sco_tx_b": 0, "sco_rx_b": 0})

    def _acc(self, h, cid):
        a = self.accs.get((h, cid))
        if a is None and len(self.accs) < MAX_CHANNELS:
            a = self.accs[(h, cid)] = delay_stats.DelayAcc(self.late_limit_ms, self.intervals)
        return a

    def _fragment(self, is_tx, h, pb, dlen, data, ts):
        """Account one ACL fragment into its L2CAP frame. Returns (frame, completes_the_frame) or (None, False)."""
        key = (is_tx, h)
        if pb in (PB_START_NOFLUSH, PB_START_FLUSH, PB_COMPLETE):
            if len(data) < 4:
                self.cur.pop(key, None)
                return None, False
            l2len, cid = struct.unpack_from("<HH", data, 0)
            fr = _Frame(h, cid, 4 + l2len, dlen, ts, bytes(data[4:16]))
            self.cur[key] = fr
            if is_tx and self.track_rtp:
                ident = rtp_identity(cid, fr.head)
                if ident is not None:             # the receive side records a packet when its last fragment arrives (feed)
                    fr.rec = [ident[0], ident[1], ts, None]
                    self.rtp_tx.append(fr.rec)
        elif pb == PB_CONT:
            fr = self.cur.get(key)
            if fr is None:
                self.orphans += 1
                return None, False
            fr.got += dlen
            fr.last_ts = ts
            fr.nfrag += 1
            if len(fr.head) < 12:                          # a short first fragment: the rest of the header is in this one
                fr.head += bytes(data[:12 - len(fr.head)])
        else:
            return None, False
        done = fr.got >= fr.total
        if done:
            self.cur.pop(key, None)
            if fr.cid >= 0x40 and fr.total <= AVDTP_MAX_FRAME:
                self._avdtp(is_tx, fr, ts)
        return fr, done

    def _avdtp(self, is_tx, fr, ts):
        """Remember an AVDTP command that starts, stops or reshapes a stream. A frame is taken for one only if it is a
        single-packet command whose signal id is one we track: media (RTP: 0x80 0x60/0xE0) and AVRCP (0x11 0x0E) never are."""
        h = fr.head
        if len(h) < 2:
            return
        b0, sig = h[0], h[1] & 0x3F
        if (b0 & 0x0F) != 0 or sig not in AVDTP_TRACKED or fr.total < 6:       # packet type single, message type command
            return
        delay = None
        if sig == 13 and fr.total >= 9 and len(h) >= 5:                          # DELAYREPORT: ACP SEID, delay in 1/10 ms
            delay = ((h[3] << 8) | h[4]) / 10.0
        self.avdtp.append((ts, fr.h, is_tx, sig, delay))

    def _complete(self, fr, ts):
        """A transmitted frame's last fragment was reported completed at `ts`."""
        ms = (ts - fr.first_ts) / 1e6
        if ms < 0 or ms > MAX_DELAY_MS:
            self.bad_delays += 1
            return
        acc = self._acc(fr.h, fr.cid)
        if acc is not None:
            acc.add(ms, fr.total)
        if fr.rec is not None:
            fr.rec[3] = ts

    def feed(self, opcode, payload, ts_ns=None):
        """Account one monitor frame's payload (separate from I/O so it can be unit-tested). `ts_ns` is the kernel's
        timestamp of the frame in nanoseconds; without it the time of this call is used."""
        ts = time.time_ns() if ts_ns is None else ts_ns
        with self.lock:
            if self.first_ts is None:
                self.first_ts = ts
            self.last_ts = ts
            if opcode == MON_COMMAND_PKT and len(payload) >= 3:
                opc = struct.unpack_from("<H", payload, 0)[0]
                spec = OP_ACTIVITY.get(opc)
                if spec is not None:
                    kind, on = spec
                    if on is None:
                        on = len(payload) >= 4 and payload[3] != 0
                    self.pend[opc] = (kind, on, ts)
            elif opcode in (MON_ACL_TX, MON_ACL_RX) and len(payload) >= 4:
                hf = struct.unpack_from("<H", payload, 0)[0]
                h, pb = hf & 0x0FFF, (hf >> 12) & 3
                dlen = struct.unpack_from("<H", payload, 2)[0]
                c = self._ctr(h)
                is_tx = opcode == MON_ACL_TX
                fr, last = self._fragment(is_tx, h, pb, dlen, payload[4:4 + dlen], ts)
                if is_tx:
                    c["tx_b"] += dlen
                    c["tx_p"] += 1
                    q = self.fifo.setdefault(h, deque(maxlen=FIFO_MAX))
                    q.append((dlen, fr, last))
                else:
                    c["rx_b"] += dlen
                    c["rx_p"] += 1
                    if last and self.track_rtp:
                        ident = rtp_identity(fr.cid, fr.head)
                        if ident is not None:
                            self.rtp_rx.append((ident[0], ident[1], fr.first_ts, ts))
            elif opcode in (MON_SCO_TX, MON_SCO_RX) and len(payload) >= 3:
                h = struct.unpack_from("<H", payload, 0)[0] & 0x0FFF
                c = self._ctr(h)
                c["sco_tx_b" if opcode == MON_SCO_TX else "sco_rx_b"] += payload[2]
            elif opcode == MON_EVENT_PKT and len(payload) >= 2:
                evt, plen = payload[0], payload[1]
                body = payload[2:2 + plen]
                if evt == EVT_NUM_COMPLETED and body:
                    n = body[0]
                    for i in range(n):
                        if len(body) < 1 + 2 * n + 2 * (i + 1):
                            break
                        h = struct.unpack_from("<H", body, 1 + 2 * i)[0] & 0x0FFF
                        cnt = struct.unpack_from("<H", body, 1 + 2 * n + 2 * i)[0]
                        c = self._ctr(h)
                        q = self.fifo.setdefault(h, deque(maxlen=FIFO_MAX))
                        for _ in range(cnt):
                            c["done_p"] += 1
                            if q:
                                dlen, fr, last = q.popleft()
                                c["done_b"] += dlen
                                if last and fr is not None:
                                    self._complete(fr, ts)
                            else:
                                c["done_unmatched"] += 1      # sent before the meter started
                elif evt in (EVT_CMD_COMPLETE, EVT_CMD_STATUS) and len(body) >= 4:
                    # Command Complete: ncmd, opcode(2), status...   Command Status: status, ncmd, opcode(2)
                    opc = struct.unpack_from("<H", body, 1 if evt == EVT_CMD_COMPLETE else 2)[0]
                    status = body[3] if evt == EVT_CMD_COMPLETE else body[0]
                    cmd = self.pend.pop(opc, None)
                    if cmd is not None and status == 0:
                        self.act[cmd[0]].set(cmd[1], cmd[2], self.first_ts)
                elif evt == EVT_INQUIRY_COMPLETE:
                    self.act["inquiry"].set(False, ts, self.first_ts)
                elif evt == EVT_FLUSH_OCCURRED and len(body) >= 2:
                    h = struct.unpack_from("<H", body, 0)[0] & 0x0FFF
                    self.flushes[h] = self.flushes.get(h, 0) + 1
                elif evt == EVT_DISCONN_COMPLETE and len(body) >= 3:
                    h = struct.unpack_from("<H", body, 1)[0] & 0x0FFF
                    self.fifo.pop(h, None)
                    self.addr_of.pop(h, None)
                    self.cur.pop((True, h), None)
                    self.cur.pop((False, h), None)
                elif evt == EVT_CONN_COMPLETE and len(body) >= 9 and body[0] == 0:
                    h = struct.unpack_from("<H", body, 1)[0] & 0x0FFF
                    self.addr_of[h] = ":".join("%02X" % b for b in reversed(body[3:9]))
                elif evt == EVT_LE_META and body:
                    if body[0] in LE_ADV_REPORTS:
                        self.le_reports += 1
                    elif body[0] in LE_CONNECTION_COMPLETE:
                        self.act["le_connect"].set(False, ts, self.first_ts)      # the attempt ended, successfully or not
                        if len(body) >= 12 and body[1] == 0:
                            h = struct.unpack_from("<H", body, 2)[0] & 0x0FFF
                            self.addr_of[h] = ":".join("%02X" % b for b in reversed(body[6:12]))

    def run(self):
        while not self.stop_ev.is_set():
            try:
                data, anc, flags, _addr = self.sock.recvmsg(65536, socket.CMSG_SPACE(16) + 32)
            except socket.timeout:
                continue
            except OSError as e:
                self.error = str(e)
                return
            self.frames += 1
            if flags & socket.MSG_TRUNC:
                self.truncated += 1
            if len(data) < 6:
                continue
            opcode, index, ln = struct.unpack_from("<HHH", data, 0)
            if index != self.hci:
                continue
            ts = None
            for level, typ, cdata in anc:
                if level == socket.SOL_SOCKET and typ == SO_TIMESTAMPNS and len(cdata) >= 16:
                    sec, nsec = struct.unpack_from("qq", cdata, 0)
                    ts = sec * 1000000000 + nsec
            if ts is None:
                ts = time.time_ns()
                self.ts_user += 1
            else:
                self.ts_kernel += 1
            self.feed(opcode, data[6:6 + ln], ts)

    # ------------------------------------------------------------- integrity
    def integrity(self):
        """Can the numbers be trusted? The kernel does not report dropped monitor frames, so loss is detected from
        invariants of the data itself: a dropped transmit frame shows up as a completion with nothing to match, and a
        dropped completion event as a send backlog no controller could hold."""
        with self.lock:
            unmatched = sum(c["done_unmatched"] for c in self.c.values())
            backlog = max((len(q) for q in self.fifo.values()), default=0)
            ts_k, ts_u, orphans, bad = self.ts_kernel, self.ts_user, self.orphans, self.bad_delays
            done = sum(a.n for a in self.accs.values())
        notes = []
        if self.error:
            notes.append("monitor socket error: %s" % self.error)
        if self.truncated:
            notes.append("%d frame(s) truncated by the kernel" % self.truncated)
        if unmatched > STARTUP_GRACE_PKTS:
            notes.append("%d completion(s) matched no sent packet (frames were probably dropped)" % unmatched)
        if backlog > MAX_BACKLOG_PKTS:
            notes.append("send backlog of %d packets (completion events were probably missed)" % backlog)
        out = {"ok": not notes, "frames": self.frames, "truncated": self.truncated,
               "unmatched_completions": unmatched, "max_backlog_pkts": backlog, "notes": notes}
        # what the DELAY figures additionally need: kernel timestamps, and completions that pair up sensibly
        dnotes = list(notes)
        stamped = ts_k + ts_u
        if stamped and ts_k < KERNEL_CLOCK_MIN * stamped:
            dnotes.append("only %.0f%% of monitor frames carried a kernel timestamp: delays include this process's scheduling"
                          % (100.0 * ts_k / stamped))
        if bad and bad > 0.001 * max(1, done + bad):
            dnotes.append("%d completion(s) gave an impossible delay (completion before send)" % bad)
        out["timing"] = {"clock": ("not recorded" if not stamped else
                                   "kernel" if ts_k >= KERNEL_CLOCK_MIN * stamped else "arrival-in-process"),
                         "kernel_stamped": ts_k, "process_stamped": ts_u, "orphan_fragments": orphans,
                         "impossible_delays": bad, "ok": not dnotes, "notes": dnotes}
        return out

    # ------------------------------------------------------------- readout
    def handles_for(self, addr):
        with self.lock:
            return [h for h, a in self.addr_of.items() if a == addr]

    def snapshot(self, handles=None):
        """Cumulative counters (summed over `handles`, or all) with a monotonic timestamp."""
        with self.lock:
            keys = self.c.keys() if handles is None else [h for h in handles if h in self.c]
            tot = {k: 0 for k in ("tx_b", "tx_p", "done_b", "done_p", "done_unmatched", "rx_b", "rx_p")}
            for h in keys:
                for k in tot:
                    tot[k] += self.c[h][k]
            tot["t"] = time.monotonic()
            return tot

    def snapshot_all(self):
        """Per-handle copy of the cumulative counters + one monotonic timestamp."""
        with self.lock:
            return {"t": time.monotonic(), "per": {h: dict(c) for h, c in self.c.items()}}

    def _channels(self, handles):
        """Accumulators of `handles` (None = all) that look like media channels: the busiest, and any other that carried a
        comparable share (the same stream on a new connection handle after a reconnect)."""
        accs = [(k, a) for k, a in self.accs.items() if a.n and (handles is None or k[0] in handles)]
        if not accs:
            return []
        top = max(a.bytes for _k, a in accs)
        return [(k, a) for k, a in accs if a.n >= MIN_MEDIA_FRAMES and a.bytes >= MEDIA_SHARE * top]

    def delay_report(self, handles=None):
        """Whole-run delay and jitter of the media channel(s) of `handles` (None = every handle), or None when no media
        packet was completed. Channels of reconnects are merged: the same stream, one histogram."""
        with self.lock:
            chans = self._channels(handles)
            if not chans:
                return None
            merged = delay_stats.DelayAcc(self.late_limit_ms, intervals=False)
            for _k, a in chans:
                for key, cnt in a.delay.bins.items():
                    merged.delay.bins[key] = merged.delay.bins.get(key, 0) + cnt
                merged.delay.n += a.delay.n
                merged.delay.total += a.delay.total
                for src, dst in ((a.delay, merged.delay), (a.diff, merged.diff)):
                    if src.lo is not None and (dst.lo is None or src.lo < dst.lo):
                        dst.lo = src.lo
                    if src.hi is not None and (dst.hi is None or src.hi > dst.hi):
                        dst.hi = src.hi
                for key, cnt in a.diff.bins.items():
                    merged.diff.bins[key] = merged.diff.bins.get(key, 0) + cnt
                merged.diff.n += a.diff.n
                merged.diff.total += a.diff.total
                merged.late += a.late
                merged.bytes += a.bytes
            merged.rfc = max(chans, key=lambda ka: ka[1].n)[1].rfc
            rep = merged.summary()
            if rep is None:
                return None
            rep["channels"] = [{"handle": k[0], "cid": k[1], "frames": a.n} for k, a in chans]
            rep["bytes"] = merged.bytes
            rep["flush_events"] = sum(self.flushes.get(k[0], 0) for k, _a in chans)
            return rep

    def interval_report(self, handles=None):
        """Delay and jitter of the media frames completed since the previous call (None if none), then start a new
        interval. The channel is the one that carried the most bytes in this interval."""
        with self.lock:
            media = {k for k, _a in self._channels(handles)}
            best = max(((a.ibytes, k) for k, a in self.accs.items() if k in media and a.ival), default=None)
            out = None
            for k, a in self.accs.items():          # every channel starts a new interval, whether reported or not
                r = a.take_interval()
                if best is not None and k == best[1]:
                    out = r
            return out

    def avdtp_report(self, handles=None):
        """AVDTP stream-control commands seen on the link(s) of `handles` (None = all) since the meter started, or None:
        `counts` per origin and signal (host_start, remote_suspend ...), the first events with their time in seconds from the
        first frame the meter saw, and the sink's own delay reports (DELAYREPORT, milliseconds)."""
        with self.lock:
            ev = [e for e in self.avdtp if handles is None or e[1] in handles]
            t0, t_end = self.first_ts, self.last_ts
        if not ev:
            return None
        counts, events, delays = {}, [], []
        for ts, _h, tx, sig, delay in ev:
            who = "host" if tx else "remote"
            key = "%s_%s" % (who, AVDTP_SIGNALS[sig].lower())
            counts[key] = counts.get(key, 0) + 1
            e = {"t_s": round((ts - t0) / 1e9, 2), "from": who, "signal": AVDTP_SIGNALS[sig]}
            if delay is not None:
                e["delay_ms"] = delay
                if not tx:
                    delays.append(delay)
            events.append(e)
        out = {"counts": counts, "events": events[:50], "events_total": len(events),
               "delay_reports_ms": ({"last": delays[-1], "min": min(delays), "max": max(delays), "n": len(delays)}
                                    if delays else None)}
        # How long the stream was actually stopped by the other end: from each remote SUSPEND to the host's next START (or to the
        # end of monitoring), within the window that begins at the first host START. Exact, unlike a state sampled every few seconds.
        starts = [e[0] for e in ev if e[2] and e[3] == 7]
        if starts and t_end is not None and t_end > starts[0]:
            window = t_end - starts[0]
            stopped = 0
            for ts in (e[0] for e in ev if not e[2] and e[3] == 9 and e[0] >= starts[0]):
                nxt = next((st for st in starts if st > ts), t_end)
                stopped += max(0, nxt - ts)
            out["stream_window_s"] = round(window / 1e9, 2)
            out["suspended_s"] = round(stopped / 1e9, 2)
            out["suspended_pct"] = round(100.0 * min(stopped, window) / window, 1)
        return out

    def activity_report(self):
        """What else this radio did during the frames the meter saw, or None if it saw none: for LE scanning, LE advertising,
        a pending LE connection attempt and BR/EDR inquiry, the seconds and the share of the window they were switched on,
        plus the number of LE advertising reports received (which proves an LE scan is running even when its enable command
        was issued before the meter started)."""
        with self.lock:
            t0, t1 = self.first_ts, self.last_ts
            if t0 is None or t1 <= t0:
                return None
            span = t1 - t0
            out = {"window_s": round(span / 1e9, 2), "le_reports": self.le_reports}
            for k, a in self.act.items():
                tot = a.total + ((t1 - a.since) if a.on else 0)
                out[k] = {"enabled_s": round(tot / 1e9, 2), "enabled_pct": round(100.0 * min(tot, span) / span, 1), "starts": a.starts}
            return out

    def drain_rtp(self):
        """(transmitted, received) RTP identities seen since the previous call, for LinkPair."""
        with self.lock:
            tx, rx = list(self.rtp_tx), list(self.rtp_rx)
            self.rtp_tx.clear()
            self.rtp_rx.clear()
            return tx, rx


class LinkPair:
    """Loopback measurement for an INTERNAL receiver: the same A2DP media packet is seen leaving the sender's controller
    (first fragment handed to it) and arriving at the receiver's host (last fragment received), both stamped by the one kernel
    clock of this host. Matching is by RTP (SSRC, sequence number), so a packet that never arrives is counted as lost, and a
    packet that arrives twice or out of order is seen. The result is the true one-way delay and loss of the radio pair, and
    the reference the sender-side completion delay is checked against (completion minus arrival).

    An identity is not unique over a whole run: PulseAudio and PipeWire both use SSRC 1 and start the sequence at 0 again every
    time the stream restarts. Sends and arrivals are therefore kept as ordered queues per identity and paired oldest with oldest;
    a send too old to belong to an arrival is a lost packet, an arrival that precedes the oldest send belongs to a send that
    is no longer held.

    Both meters must have been created with track_rtp=True."""

    DUP_WINDOW_NS = 2 * 10 ** 9          # a second arrival of a matched identity within this time is a duplicate

    def __init__(self, tx_meter, rx_meter, timeout_s=5.0, late_limit_ms=None):
        self.tx_meter, self.rx_meter, self.timeout_ns = tx_meter, rx_meter, int(timeout_s * 1e9)
        self.tx = {}                     # (ssrc, seq) -> deque of [ssrc, seq, first_ts, completion_ts or None]
        self.rx = {}                     # (ssrc, seq) -> deque of arrival ts of the last fragment
        self.done = OrderedDict()        # (ssrc, seq) -> arrival ts of its last match, to recognise a packet received twice
        self.acc = delay_stats.DelayAcc(late_limit_ms, intervals=True)
        self.matched = self.lost = self.dup_rx = self.unmatched_rx = self.reordered = 0
        self.sent = 0
        self.calib = delay_stats.SignedHist()   # completion - arrival, ms, for packets where both are known
        self.newest_sent = 0             # send time of the newest packet matched so far (for reordering)
        self.newest = 0
        self.in_flight_excluded = 0
        self.ilost = 0

    def _lose(self, n=1):
        self.lost += n
        self.ilost += n

    def poll(self):
        tx = self.tx_meter.drain_rtp()[0]
        rx = self.rx_meter.drain_rtp()[1]
        for rec in tx:
            self.sent += 1
            self.tx.setdefault((rec[0], rec[1]), deque()).append(rec)
            self.newest = max(self.newest, rec[2])
        for ssrc, seq, _first, last in rx:
            key = (ssrc, seq)
            self.newest = max(self.newest, last)
            seen = self.done.get(key)
            txq, rxq = self.tx.get(key), self.rx.get(key)
            while txq and rxq and rxq[0] < txq[0][2]:     # waiting arrivals that precede the oldest send cannot be its pair
                rxq.popleft()
                self.unmatched_rx += 1
            waiting_tx, waiting_rx = len(txq or ()), len(rxq or ())
            if waiting_rx >= waiting_tx and (waiting_rx > 0 or (seen is not None and 0 <= last - seen <= self.DUP_WINDOW_NS)):
                self.dup_rx += 1                      # no send is waiting for it, and one just arrived or was matched: a copy
                continue
            self.rx.setdefault(key, deque()).append(last)
        batch = []                                    # (arrival, key, send record): applied in arrival order below
        for key in [k for k in self.rx if k in self.tx]:
            txq, rxq = self.tx[key], self.rx[key]
            while txq and rxq:
                rec, arrival = txq[0], rxq[0]
                if arrival < rec[2]:                  # it precedes the oldest send we hold: its send is gone
                    rxq.popleft()
                    self.unmatched_rx += 1
                elif arrival - rec[2] > self.timeout_ns:      # that send is too old to be this arrival's: it was lost
                    txq.popleft()
                    self._lose()
                else:
                    txq.popleft()
                    rxq.popleft()
                    batch.append((arrival, key, rec))
            if not txq:
                del self.tx[key]
            if not rxq:
                del self.rx[key]
        # Keys are visited in the order they first appeared, not in time: when an identity repeats inside one batch (a restarted
        # stream) its pairs must still reach the statistics in arrival order, or jitter and reordering would be computed across
        # the wrong neighbours.
        for arrival, key, rec in sorted(batch, key=lambda b: b[0]):
            self._matched(key, rec, arrival)
        horizon = self.newest - self.timeout_ns
        for key in list(self.tx):
            q = self.tx[key]
            while q and q[0][2] < horizon:
                q.popleft()
                self._lose()
            if not q:
                del self.tx[key]
        for key in list(self.rx):
            q = self.rx[key]
            while q and q[0] < horizon:
                q.popleft()
                self.unmatched_rx += 1
            if not q:
                del self.rx[key]

    def _matched(self, key, rec, arrival):
        self.acc.add((arrival - rec[2]) / 1e6)
        self.matched += 1
        self.done[key] = arrival
        self.done.move_to_end(key)
        while len(self.done) > RTP_KEEP:
            self.done.popitem(last=False)
        # Out of order = this packet was SENT before one that has already ARRIVED. Judged by send time, not by sequence number:
        # a restarted stream (sequence numbers back to 0, even after a very short session) is not reordering.
        if rec[2] < self.newest_sent:
            self.reordered += 1
        else:
            self.newest_sent = rec[2]
        if rec[3] is not None:
            self.calib.add((rec[3] - arrival) / 1e6)

    def interval(self):
        """Delay of the packets matched since the previous call, and the packets declared lost since then."""
        self.poll()
        r = self.acc.take_interval()
        lost, self.ilost = self.ilost, 0
        return r, lost

    def finish(self, settle_s=1.0):
        """Final poll. Packets still in flight when the run stopped (sent within `settle_s` of the newest one) are
        left out of both counts instead of being called lost."""
        self.poll()
        cutoff = self.newest - int(settle_s * 1e9)
        for key in list(self.tx):
            q = self.tx.pop(key)
            for rec in q:
                if rec[2] >= cutoff:
                    self.in_flight_excluded += 1
                else:
                    self._lose()

    def report(self):
        s = self.acc.summary()
        total = self.matched + self.lost
        return {
            "what": "true one-way delay of the radio pair: first fragment handed to the sender's controller -> last fragment "
                    "received by the receiver's host, matched per RTP packet, one kernel clock",
            "packets_sent": self.sent, "matched": self.matched, "lost": self.lost, "in_flight_excluded": self.in_flight_excluded,
            "loss_pct": round(100.0 * self.lost / total, 3) if total else None,
            "duplicates": self.dup_rx, "reordered": self.reordered, "unmatched_arrivals": self.unmatched_rx,
            "delay_ms": s["delay_ms"] if s else None, "jitter_ms": s["jitter_ms"] if s else None,
            "late": s["late"] if s else None,
            "completion_minus_arrival_ms": self.calib.summary(),
        }


_KEYS = ("tx_b", "tx_p", "done_b", "done_p", "rx_b", "rx_p")


def totals(snap, handles=None):
    per = snap["per"]
    hs = per.keys() if handles is None else [h for h in handles if h in per]
    out = {k: 0 for k in _KEYS}
    for h in hs:
        for k in _KEYS:
            out[k] += per[h][k]
    return out


def rates_for(prev, cur, handles=None):
    """Exact kbit/s between two snapshot_all() results, for `handles` (None = every connection
    on the controller). Counters are cumulative per handle, so a reconnect (new handle) is safe."""
    dt = cur["t"] - prev["t"]
    if dt <= 0:
        return None
    zero = {k: 0 for k in _KEYS}
    hs = list(cur["per"].keys()) if handles is None else [h for h in handles if h in cur["per"]]
    d = {k: 0 for k in _KEYS}
    for h in hs:
        p = prev["per"].get(h, zero)
        for k in _KEYS:
            d[k] += cur["per"][h][k] - p[k]
    return {"dt": round(dt, 3), "tx_kbps": round(d["tx_b"] * 8.0 / dt / 1000.0, 2),
            "done_kbps": round(d["done_b"] * 8.0 / dt / 1000.0, 2),
            "rx_kbps": round(d["rx_b"] * 8.0 / dt / 1000.0, 2), "tx_pps": round(d["tx_p"] / dt, 1),
            "tx_bytes": d["tx_b"], "done_bytes": d["done_b"], "rx_bytes": d["rx_b"]}


def rates(prev, cur):
    """kbit/s between two snapshots over their exact elapsed time (None if no time passed)."""
    dt = cur["t"] - prev["t"]
    if dt <= 0:
        return None
    def r(k):
        return round((cur[k] - prev[k]) * 8.0 / dt / 1000.0, 2)
    return {"dt": round(dt, 3), "tx_kbps": r("tx_b"), "done_kbps": r("done_b"), "rx_kbps": r("rx_b"),
            "tx_pps": round((cur["tx_p"] - prev["tx_p"]) / dt, 1)}
