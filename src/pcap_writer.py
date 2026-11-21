"""pcap_writer.py - records HCI packets in a file that Wireshark opens directly.

Format: classic pcap, link type 201 (LINKTYPE_BLUETOOTH_HCI_H4_WITH_PHDR). Each record is
    4 bytes, big-endian: direction (0 = sent to the controller, 1 = received from it)
    then the H4 packet itself: one indicator byte (1 command, 2 ACL, 4 event) and the HCI packet.
Wireshark, tshark and the other pcap tools decode it as Bluetooth HCI, including every LE Advertising Report
(address, RSSI, advertising data) the controller handed to the host.

This is what the controller REPORTED, not the radio packets themselves: there is no channel number, no CRC and no
link-layer framing, because the controller does not pass those to the host. Standard library only.

Every record goes out in one write() call, so a file that is being written is always readable up to its last whole
record (Wireshark simply stops there). The file is created exclusively and is never overwritten.
"""
import os
import struct
import threading
import time

LINKTYPE_BLUETOOTH_HCI_H4_WITH_PHDR = 201
DIR_SENT, DIR_RECV = 0, 1
SNAPLEN = 65535
# pcap global header, little-endian, microsecond timestamps: magic, version 2.4, tz 0, sigfigs 0, snaplen, link type
GLOBAL_HEADER = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, SNAPLEN, LINKTYPE_BLUETOOTH_HCI_H4_WITH_PHDR)


class PcapWriter:
    """Append-only pcap writer. max_bytes=0 means no limit; past the limit write() returns False and `full` is set."""

    def __init__(self, path, max_bytes=0):
        self.path = path
        self.max_bytes = int(max_bytes or 0)
        self.packets = 0
        self.bytes = len(GLOBAL_HEADER)
        self.full = False
        self._lock = threading.Lock()
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644)
        try:
            self._put(GLOBAL_HEADER)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise

    def _put(self, data):
        view = memoryview(data)
        while view:
            n = os.write(self._fd, view)
            view = view[n:]

    def write(self, direction, packet, ts=None):
        """Add one packet. Returns True if it was written."""
        if not packet:
            return False
        ts = time.time() if ts is None else ts
        sec = int(ts)
        usec = int((ts - sec) * 1_000_000)
        n = len(packet) + 4
        rec = struct.pack("<IIII", sec, usec, n, n) + struct.pack(">I", direction & 1) + packet
        with self._lock:
            if self._fd is None or self.full:
                return False
            if self.max_bytes and self.bytes + len(rec) > self.max_bytes:
                self.full = True
                return False
            self._put(rec)
            self.packets += 1
            self.bytes += len(rec)
            return True

    def tap(self, direction, packet):
        """Signature of HciSocket.tap."""
        self.write(direction, packet)

    def close(self):
        with self._lock:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                finally:
                    self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
