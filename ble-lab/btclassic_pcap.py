#!/usr/bin/env python3
"""
btclassic_pcap.py - Convert a BluForge Classic-BR/EDR .btsnoop into a genuine
.pcap using LINKTYPE_BLUETOOTH_LINUX_MONITOR (254).

Unlike btle_pcap.py (which RECONSTRUCTS synthetic BLE Link-Layer PDUs from
higher-level HCI Advertising Report fields - a lossy, best-effort
transformation, because the real over-the-air PDU bytes for BLE happen to be
directly present in the HCI report), Classic Inquiry Results are a much
higher-level abstraction over what's actually a complex multi-packet RF
procedure (frequency-hopping ID/FHS packet exchanges) - there is no way to
losslessly, or even plausibly, reconstruct genuine over-the-air Classic
baseband PDUs from an Inquiry Result event. Attempting that would be
fabrication, not reconstruction.

So for Classic this script does something different and safer: a LOSSLESS
container swap. It re-wraps the exact same HCI-transport bytes BlueZ's btmon
already writes (which Wireshark already decodes perfectly - Inquiry Result/
Extended Inquiry Result with full field breakdown) into a plain .pcap file
instead of the .btsnoop container, using the officially-registered
"Bluetooth Linux Monitor" pcap linktype (254). Nothing is invented; every
byte of every HCI record is preserved.

The exact per-packet payload layout for this linktype - 2 bytes adapter
index (big-endian) + 2 bytes opcode (big-endian) + the original HCI payload
- was verified empirically: `editcap -F pcap` (part of Wireshark, used only
at development time to derive this, NOT a runtime dependency of this script)
converted a real capture, and the resulting bytes were compared field-by-
field against the source .btsnoop records to confirm the exact transform.
This script has no new runtime dependency - pure Python stdlib.

Usage:
  python3 btclassic_pcap.py capture.btsnoop out.pcap
"""
import sys, struct

LINKTYPE_BLUETOOTH_LINUX_MONITOR = 254
SNAPLEN = 262144   # matches what editcap produces for this linktype

# btsnoop timestamps are microseconds since 0000-01-01 (per the btsnoop spec),
# not Unix epoch. Offset verified empirically against tshark's own
# frame.time_epoch on a real capture (matched across two independent frames,
# both exact) - see btle_pcap.py for the original derivation.
BTSNOOP_EPOCH_OFFSET_US = 62168256000000000


def read_btsnoop_records(path):
    """Yield (raw_ts, opcode, index, payload) for every record in a BlueZ
    btmon .btsnoop (datalink type 2001, 'Linux Monitor' framing). The 32-bit
    `flags` field is repurposed to carry opcode (low 16 bits) + adapter index
    (high 16 bits) - this is BlueZ's own convention, not ours."""
    with open(path, "rb") as f:
        hdr = f.read(16)
        if len(hdr) < 16 or hdr[:8] != b"btsnoop\x00":
            raise ValueError("not a btsnoop file")
        while True:
            rh = f.read(24)
            if len(rh) < 24: break
            orig_len, incl_len, flags, drops, ts = struct.unpack(">IIIIq", rh)
            payload = f.read(incl_len)
            if len(payload) < incl_len: break
            opcode = flags & 0xFFFF
            index = (flags >> 16) & 0xFFFF
            yield ts, opcode, index, payload


def convert(in_path, out_path):
    n = 0
    with open(out_path, "wb") as out:
        out.write(struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, SNAPLEN,
                               LINKTYPE_BLUETOOTH_LINUX_MONITOR))
        for raw_ts, opcode, index, payload in read_btsnoop_records(in_path):
            data = struct.pack(">HH", index, opcode) + payload
            unix_us = raw_ts - BTSNOOP_EPOCH_OFFSET_US
            sec = unix_us // 1_000_000
            usec = unix_us % 1_000_000
            out.write(struct.pack("<IIII", sec & 0xFFFFFFFF, usec, len(data), len(data)))
            out.write(data)
            n += 1
    return n


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: btclassic_pcap.py capture.btsnoop out.pcap", file=sys.stderr)
        sys.exit(1)
    n = convert(sys.argv[1], sys.argv[2])
    print(f"wrote {n} records -> {sys.argv[2]}")
