#!/usr/bin/env python3
"""
btcapture.py - BluForge HCI capture recorder (drop-in replacement for `btmon -w`).

WHY THIS EXISTS
BlueZ 5.77's `btmon` crashes with a glibc "*** buffer overflow detected ***"
while decoding live advertising traffic. Reproduced on this box: btmon -w
survives an idle adapter (writes ~223 B and exits cleanly) but dies within
seconds once a scan is running, leaving a truncated ~3 KB capture containing
only the adapter-open bookkeeping and zero advertising reports. That silently
produced empty captures for every sniff session.

btmon decodes every packet for its pretty-printer even in -w mode, so the
decoder defect takes the writer down with it. This recorder does NOT decode
anything - it copies the raw monitor stream straight to disk, so a malformed
or unusual packet can never crash it.

HOW IT WORKS
Binds AF_BLUETOOTH/SOCK_RAW/BTPROTO_HCI to HCI_CHANNEL_MONITOR (2) - the same
kernel interface btmon reads - and writes the standard BTSnoop container that
Wireshark and BluForge's own converters already understand:

  global header : "btsnoop\\0" + version 1 + datalink 2001 (HCI Monitor)
  per record    : orig_len, incl_len, flags, drops, timestamp   (all big-endian)
                  followed by the raw packet payload

`flags` carries BlueZ's own convention: opcode in the low 16 bits, adapter
index in the high 16 bits. The monitor socket hands us those in a 6-byte
little-endian header (opcode, index, length) ahead of each payload - verified
empirically against a live capture, and byte-compatible with the files btmon
used to produce (BluForge's btle_pcap.py / btclassic_pcap.py read both
identically).

Usage:
  python3 btcapture.py -w out.btsnoop [--duration S]
  (SIGINT/SIGTERM stop cleanly and flush, same as btmon)
"""
import argparse, ctypes, os, signal, socket, struct, sys, time

HCI_CHANNEL_MONITOR = 2
HCI_DEV_NONE        = 0xFFFF
BTSNOOP_MAGIC       = b"btsnoop\x00"
BTSNOOP_VERSION     = 1
BTSNOOP_DATALINK_MONITOR = 2001
# microseconds between 0000-01-01 (btsnoop epoch) and 1970-01-01 (unix epoch);
# verified empirically against tshark's frame.time_epoch on a real capture.
BTSNOOP_EPOCH_OFFSET_US = 62168256000000000
RECV_BUF = 65535


def open_monitor_socket():
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_CLOEXEC,
                      socket.BTPROTO_HCI)
    addr = struct.pack("<HHH", socket.AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_MONITOR)
    if libc.bind(s.fileno(), addr, len(addr)) != 0:
        err = ctypes.get_errno()
        s.close()
        raise OSError(err, f"bind(HCI monitor channel) failed: {os.strerror(err)}")
    return s


def main():
    ap = argparse.ArgumentParser(description="BluForge HCI monitor recorder")
    ap.add_argument("-w", "--write", required=True, help="output .btsnoop path")
    ap.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = until signalled)")
    a = ap.parse_args()

    try:
        sock = open_monitor_socket()
    except OSError as e:
        print(f"btcapture: {e}", file=sys.stderr)
        return 1

    stop = {"v": False}
    def _stop(*_):
        stop["v"] = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    written = 0
    f = open(a.write, "wb")
    try:
        f.write(struct.pack(">8sII", BTSNOOP_MAGIC, BTSNOOP_VERSION,
                            BTSNOOP_DATALINK_MONITOR))
        f.flush()
        sock.settimeout(0.5)
        start = time.time()
        while not stop["v"]:
            if a.duration and (time.time() - start) >= a.duration:
                break
            try:
                pkt = sock.recv(RECV_BUF)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(pkt) < 6:
                continue
            opcode, index, plen = struct.unpack_from("<HHH", pkt, 0)
            body = pkt[6:6 + plen]
            # BlueZ packs opcode/index into the btsnoop `flags` field
            flags = (opcode & 0xFFFF) | ((index & 0xFFFF) << 16)
            ts = int(time.time() * 1_000_000) + BTSNOOP_EPOCH_OFFSET_US
            f.write(struct.pack(">IIIIq", len(body), len(body), flags, 0, ts))
            f.write(body)
            # flush as we go so the GUI's live "size" readout actually moves
            # and an abrupt kill still leaves a valid, readable capture
            f.flush()
            written += 1
    finally:
        try: f.close()
        except Exception: pass
        try: sock.close()
        except Exception: pass

    print(f"btcapture: wrote {written} records -> {a.write}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
