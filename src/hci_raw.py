"""Minimal raw-HCI control of a Bluetooth controller (stdlib only, root required).

Talks directly to a BE200 Bluetooth controller (hciN) over AF_BLUETOOTH /
BTPROTO_HCI, bypassing BlueZ's bluetoothd for the specific commands this
project needs. What is IMPLEMENTED (methods on HciSocket):

  - LE Enhanced Transmitter Test (v2), or LE Transmitter Test [v4] when a TX power is requested
    (tx_test_start), LE Enhanced Receiver Test (rx_test_start) and LE Test End (tx_test_end)
                                                                -> BLE "interference" generator (DTM)
  - LE Set Advertising Parameters/Data/Scan Response/Enable (legacy, <=31B) -> ADV emulation
  - LE Set Scan Parameters/Enable and LE Set Extended Scan Parameters/Enable, plus parsing of
    legacy and Extended Advertising Reports (with fragment reassembly)    -> scanner / survey

NOT implemented (only the opcode numbers exist in OCF below, unused): extended
*advertising* (LE Set Extended Adv Parameters/Data/Enable) and the original (v1) LE Transmitter Test.

Exclusive access: while a controller is doing a TX test or custom
advertising, we bind HCI_CHANNEL_USER on it. The kernel enforces exclusivity:
as soon as a user-channel socket is bound, bluetoothd is locked out of that
one controller until the socket is closed, and the other controllers /
bluetoothd itself are completely unaffected. `close()` (restore_to_bluez=True)
hands the controller back to BlueZ through force_recover() - a mgmt "power on",
retried for a few seconds - so `bluetoothctl`/the rest of the system sees it come
back to normal. (There is no method named release().)

This module never touches the HackRF or LANforge's RF generator (project
policy: BE200 radios only).

Implementation note -- verified against the running Fedora 39 kernel headers
(/usr/src/kernels/*/include/net/bluetooth/{hci_sock,bluetooth}.h) and tested
live against real BE200 controllers before being relied on:

  - The Python stdlib `socket.bind()` for AF_BLUETOOTH/BTPROTO_HCI only
    accepts a 1-tuple `(dev_id,)` on this CPython build (confirmed: a
    2-tuple raises "getsockaddrarg: wrong format") and always binds
    HCI_CHANNEL_RAW -- there is no stdlib way to request HCI_CHANNEL_USER or
    HCI_CHANNEL_CONTROL. So this module builds `struct sockaddr_hci` itself
    (3x u16: family, dev, channel -- confirmed from hci_sock.h) and calls the
    libc bind(2) syscall through ctypes, with explicit argtypes (calling it
    without argtypes was tried and silently corrupts the fd argument,
    producing an unrelated-looking EBADFD).
  - struct hci_filter for SOL_HCI/HCI_FILTER setsockopt is the 32-bit
    "hci_ufilter" userspace shape (type_mask/event_mask as u32, padded to 16
    bytes) -- confirmed from the same header (the kernel-internal
    `hci_filter` with `unsigned long` fields is a different, in-kernel-only
    struct of the same name).
  - HCI_CHANNEL_USER bind requires the device to be powered off first (the
    kernel returns EBUSY otherwise, confirmed live); we power off/on through
    the kernel's "mgmt" protocol (HCI_CHANNEL_CONTROL, documented in
    net/bluetooth/mgmt.h) rather than shelling out to the `btmgmt` CLI, whose
    one-shot ("btmgmt --index N power off") invocation was found to hang
    indefinitely in this environment (it appears to need a real TTY).
"""
import ctypes
import ctypes.util
import os
import socket
import struct
import threading
import time

AF_BLUETOOTH = 31
BTPROTO_HCI = 1
SOL_HCI = 0
HCI_FILTER = 2

HCI_CHANNEL_RAW = 0
HCI_CHANNEL_USER = 1
HCI_CHANNEL_MONITOR = 2
HCI_CHANNEL_CONTROL = 3
HCI_DEV_NONE = 0xFFFF

OGF_LE = 0x08
OGF_HOST_CTL = 0x03
OGF_INFO_PARAM = 0x04


def detect_hci_indices():
    """The real list of Bluetooth controllers present on THIS machine, read
    from /sys/class/bluetooth/hciN at call time. This project was first
    built and tested on a unit with exactly 8 identical BE200 controllers
    (hci0-hci7), and both server.py and static/app.js originally hardcoded
    that as a fixed range(8) -- confirmed live, on a second physical unit
    with only 4 controllers, to be a real portability bug: the GUI offered
    hci4-hci7 as selectable radios that didn't exist there at all. This is
    the single source of truth both the backend and the GUI now use instead
    of a hardcoded count, so the engine adapts to whatever hardware it's
    actually running on. Returns a sorted list of ints, e.g. [0,1,2,3]."""
    try:
        names = os.listdir("/sys/class/bluetooth")
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith("hci") and n[3:].isdigit():
            out.append(int(n[3:]))
    return sorted(out)

HCI_EVENT_PKT = 0x04

EVT_CMD_COMPLETE = 0x0E
EVT_CMD_STATUS = 0x0F
EVT_LE_META = 0x3E

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.bind.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
_libc.bind.restype = ctypes.c_int


def _hci_bind(sock: socket.socket, dev_id: int, channel: int):
    """bind() an AF_BLUETOOTH/BTPROTO_HCI socket to a specific channel
    (RAW/USER/MONITOR/CONTROL) -- see module docstring for why this can't go
    through socket.socket.bind()."""
    buf = struct.pack("<HHH", AF_BLUETOOTH, dev_id, channel)
    cbuf = ctypes.create_string_buffer(buf, len(buf))
    rc = _libc.bind(sock.fileno(), ctypes.cast(cbuf, ctypes.c_void_p), len(buf))
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


LE_ADVERTISING_REPORT = 0x02
LE_EXT_ADVERTISING_REPORT = 0x0D
LE_EXTENDED_ADVERTISING_REPORT = 0x0D

# LE Transmitter/Receiver Test Packet_Payload values (Core Spec Vol 4 Part E,
# Table 7.8.29). Named here so a CLI/GUI can offer them by name instead of
# a bare magic number.
PAYLOAD_TYPES = {
    "prbs9": 0x00, "11110000": 0x01, "10101010": 0x02, "prbs15": 0x03,
    "all1": 0x04, "all0": 0x05, "00001111": 0x06, "01010101": 0x07, "0x55": 0x07,
}

# Legacy advertising PDU types (Advertising_Type field, Core Spec 7.8.5
# Table). Directed types (DIRECT_IND, high/low duty) carry a mandatory peer
# (target) address and NO AdvData field at the link layer -- the HCI command
# can still technically carry Set Advertising Data, but it has no effect on
# what goes over the air for a directed PDU, so legacy_adv_configure()
# refuses non-empty adv_data for these two types rather than silently
# ignoring it.
ADV_TYPE_IND = 0x00            # ADV_IND: connectable, scannable, undirected
ADV_TYPE_DIRECT_IND_HIGH = 0x01  # ADV_DIRECT_IND, high duty cycle (fixed ~3.75ms interval)
ADV_TYPE_SCAN_IND = 0x02       # ADV_SCAN_IND: scannable, undirected
ADV_TYPE_NONCONN_IND = 0x03    # ADV_NONCONN_IND: non-connectable beacon
ADV_TYPE_DIRECT_IND_LOW = 0x04  # ADV_DIRECT_IND, low duty cycle (user interval)
DIRECTED_ADV_TYPES = (ADV_TYPE_DIRECT_IND_HIGH, ADV_TYPE_DIRECT_IND_LOW)


def opcode(ogf, ocf):
    return (ogf << 10) | ocf


# a few opcodes this module issues directly (full table lives in ble_spec docs)
OCF = dict(
    SET_EVENT_MASK=0x0001,
    RESET=0x0003,
    LE_SET_EVENT_MASK=0x0001,
    LE_SET_RANDOM_ADDRESS=0x0005,
    LE_SET_ADV_PARAMETERS=0x0006,
    LE_SET_ADV_DATA=0x0008,
    LE_SET_SCAN_RESPONSE_DATA=0x0009,
    LE_SET_ADV_ENABLE=0x000A,
    LE_SET_SCAN_PARAMETERS=0x000B,
    LE_SET_SCAN_ENABLE=0x000C,
    LE_RECEIVER_TEST=0x001D,
    LE_TRANSMITTER_TEST=0x001E,
    LE_TEST_END=0x001F,
    LE_ENHANCED_RECEIVER_TEST=0x0033,
    LE_ENHANCED_TRANSMITTER_TEST=0x0034,
    LE_SET_ADV_SET_RANDOM_ADDRESS=0x0035,
    LE_SET_EXT_ADV_PARAMETERS=0x0036,
    LE_SET_EXT_ADV_DATA=0x0037,
    LE_SET_EXT_SCAN_RESPONSE_DATA=0x0038,
    LE_SET_EXT_ADV_ENABLE=0x0039,
    LE_REMOVE_ADV_SET=0x003C,
    LE_CLEAR_ADV_SETS=0x003D,
    LE_SET_EXT_SCAN_PARAMETERS=0x0041,
    LE_SET_EXT_SCAN_ENABLE=0x0042,
    LE_TRANSMITTER_TEST_V4=0x007B,
)


class HciError(Exception):
    pass


MGMT_OP_SET_POWERED = 0x0005
MGMT_EV_CMD_COMPLETE = 0x0001
MGMT_EV_CMD_STATUS = 0x0002


def btmgmt_power(index, on, timeout=5.0):
    """Power a controller off/on via the kernel mgmt socket directly (same
    ABI 'btmgmt'/bluetoothd use -- include/net/bluetooth/mgmt.h). We talk to
    it ourselves instead of shelling out to the `btmgmt` CLI: that tool's
    one-shot ("btmgmt --index N power off") invocation was found to hang
    indefinitely in this environment (it appears to need a real TTY);
    driving the documented kernel protocol directly is more robust and still
    pure stdlib (verified live: power off -> bind USER channel succeeds ->
    power on -> bluetoothctl sees the controller fully normal again).

    power off -> required before we can bind HCI_CHANNEL_USER exclusively
                 (releases the controller from bluetoothd).
    power on  -> hands the controller back to bluetoothd when we're done."""
    s = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_CLOEXEC, BTPROTO_HCI)
    try:
        _hci_bind(s, HCI_DEV_NONE, HCI_CHANNEL_CONTROL)
        s.settimeout(timeout)
        hdr = struct.pack("<HHH", MGMT_OP_SET_POWERED, index, 1)
        s.send(hdr + bytes([1 if on else 0]))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = s.recv(256)
            except socket.timeout:
                break
            if len(data) < 6:
                continue
            ev_code, ev_index, ev_len = struct.unpack("<HHH", data[:6])
            body = data[6:6 + ev_len]
            if ev_index != index:
                continue
            if ev_code == MGMT_EV_CMD_COMPLETE and len(body) >= 3:
                rop = struct.unpack("<H", body[0:2])[0]
                if rop == MGMT_OP_SET_POWERED:
                    status = body[2]
                    return status == 0, "mgmt status 0x%02x" % status
            elif ev_code == MGMT_EV_CMD_STATUS and len(body) >= 3:
                rop = struct.unpack("<H", body[0:2])[0]
                if rop == MGMT_OP_SET_POWERED:
                    status = body[2]
                    return status == 0, "mgmt status 0x%02x (status event)" % status
        return False, "timeout waiting for mgmt reply (index %d)" % index
    finally:
        s.close()


class HciSocket:
    """One exclusive raw-HCI session on a single controller (hciN)."""

    def __init__(self, hci_index, label=""):
        self.index = hci_index
        self.label = label or ("hci%d" % hci_index)
        self.sock = None
        # RLock, not Lock: cleanup() is installed as the SIGINT/SIGTERM
        # handler and calls back into send_cmd() (via tx_test_end()/close()).
        # If a signal arrives on the same thread while send_cmd() already
        # holds this lock (e.g. mid-retry, blocked in recv()), a plain Lock
        # deadlocks permanently -- confirmed by code review as a real path
        # (server.py's Jobs.stop() SIGINT-then-SIGTERM timing can line up
        # with exactly this window). RLock lets the same thread reacquire it.
        self._lock = threading.RLock()
        self._opened_user_channel = False
        self._ext_reassembler = _ExtReassembler()
        self.dead = False
        self.tap = None                   # optional callback(direction, packet): every HCI packet sent (0) or received (1); see _tap()
        self.tap_errors = 0
        self._adv_high_duty = False
        self._adv_ext = False             # advertising set 0 is live via the extended commands (TX power path)
        self.bad_events = 0               # malformed advertising events skipped (should stay 0)

    # -------------------------------------------------------------- lifecycle
    def open(self):
        """Power the controller off (releases it from bluetoothd), then bind
        an exclusive HCI_CHANNEL_USER socket. The kernel brings the device
        back up for us as part of this bind."""
        ok, msg = btmgmt_power(self.index, False)
        if not ok:
            # proceed anyway -- "already powered off" also reports a non-zero
            # mgmt status on some firmware; the bind below is the real check.
            pass
        # mgmt's Command Complete for "power off" was confirmed live to
        # arrive BEFORE the controller is actually bindable (bluetoothctl
        # shows "Powered: no" / "PowerState: on" for up to ~1-2s afterward --
        # either the driver's own shutdown sequence is asynchronous, or
        # desktop BT managers like blueman race to re-power it). Retry the
        # bind with a bounded budget instead of guessing a fixed sleep.
        last_err = None
        s = None
        for _attempt in range(30):  # 30 x 100ms = 3s budget
            s = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_CLOEXEC, BTPROTO_HCI)
            try:
                _hci_bind(s, self.index, HCI_CHANNEL_USER)
                last_err = None
                break
            except OSError as e:
                last_err = e
                s.close()
                s = None
                time.sleep(0.1)
        if last_err is not None:
            # Leave the controller no worse off than before this attempt: we
            # already told bluetoothd to power it off above, so if we can't
            # actually claim it, hand it back rather than abandoning it
            # mid-handoff (confirmed by code review as a real dead-controller
            # path -- this is exactly what force_recover() exists for, so use
            # it here instead of leaving it as unreachable dead code).
            force_recover(self.index, timeout=5.0)
            raise HciError(
                "bind HCI_CHANNEL_USER on hci%d failed after retrying for 3s (%s, mgmt "
                "power-off said: %s). Another process may be holding this controller "
                "(e.g. a desktop Bluetooth manager re-powering it). The controller has "
                "been handed back to bluetoothd." % (self.index, last_err, msg))
        s.settimeout(5.0)
        self.sock = s
        # NOTE: HCI_CHANNEL_USER sockets do NOT accept SOL_HCI/HCI_FILTER
        # (confirmed live: setsockopt fails EBADFD) -- by design, a user
        # channel already delivers every HCI event/ACL/SCO packet to its one
        # owner unfiltered, which is the entire point of exclusive access.
        # The filter method below exists only for a HCI_CHANNEL_RAW socket.
        self._opened_user_channel = True
        self._reset_and_enable_event_masks()
        return self

    def _reset_and_enable_event_masks(self):
        """A controller taken over through HCI_CHANNEL_USER gets none of the
        adapter-init sequence bluetoothd normally runs (Reset / Set Event
        Mask / LE Set Event Mask). We do the same two steps here:

        1. HCI Reset first, for a clean slate (clears any state left over
           from a prior ungraceful session on this controller -- e.g. an
           aborted test mode -- before we touch it).
        2. Command Complete/Status are hard-wired and always fire regardless
           of any mask (confirmed live), but LE Meta Event (needed for every
           Advertising Report) is a MASKABLE bit in the standard Event_Mask
           and was confirmed live to be absent otherwise: with no event mask
           set, a fully-successful LE Set Scan Enable produced zero
           Advertising Report events over several seconds against a
           verified-live advertiser. Enable every bit in both masks, exactly
           as BlueZ's own adapter-init does (and as confirmed correct against
           a second, independent implementation's --help text referencing
           this exact gotcha)."""
        try:
            self._check("Reset", OGF_HOST_CTL, OCF["RESET"])
        except HciError:
            pass  # non-fatal: proceed even if a freshly-bound controller briefly declines
        try:
            self._check("Set Event Mask", OGF_HOST_CTL, OCF["SET_EVENT_MASK"],
                         struct.pack("<Q", 0xFFFFFFFFFFFFFFFF))
            self._check("LE Set Event Mask", OGF_LE, OCF["LE_SET_EVENT_MASK"],
                         struct.pack("<Q", 0xFFFFFFFFFFFFFFFF))
        except HciError:
            pass  # non-fatal: some controllers may reject unknown bits; scan/adv still attempted

    def _set_default_filter(self):
        """Only valid on a HCI_CHANNEL_RAW socket (not used by open() above,
        which uses HCI_CHANNEL_USER). A fresh RAW socket's kernel-default
        filter does NOT pass LE Meta Events (0x3E, needed for advertising
        reports) and may not reliably pass Command Complete/Status for every
        opcode either. struct hci_filter { u32 type_mask; u32 event_mask[2];
        u16 opcode; } (padded to 16 bytes on x86_64). type_mask bit4 =
        HCI_EVENT_PKT. event_mask word0 bits 14/15 = Command
        Complete(0x0E)/Command Status(0x0F); word1 bit30 = event 62 = LE Meta
        Event(0x3E). opcode=0 means 'any opcode' (kernel only restricts by
        opcode when this field is non-zero)."""
        type_mask = 1 << HCI_EVENT_PKT
        event_mask0 = (1 << EVT_CMD_COMPLETE) | (1 << EVT_CMD_STATUS)
        event_mask1 = 1 << (EVT_LE_META - 32)
        flt = struct.pack("<III H 2x", type_mask, event_mask0, event_mask1, 0)
        self.sock.setsockopt(SOL_HCI, HCI_FILTER, flt)

    def close(self, restore_to_bluez=True):
        """Returns (ok, msg) describing whether the controller was actually
        handed back to bluetoothd. A single best-effort btmgmt_power() call
        here was found (by code review) to silently discard its result even
        though a single attempt is known-unreliable (see open()'s own
        comments on the mgmt power-off race) -- every caller then told the
        operator the radio was 'restored' regardless of whether it was.
        force_recover() retries, and the result is returned so callers can
        surface a real failure instead of asserting success blindly."""
        with self._lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
            self._opened_user_channel = False
        if restore_to_bluez:
            time.sleep(0.1)
            return force_recover(self.index, timeout=5.0)
        return True, "not requested"

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------- packet tap
    def _tap(self, direction, pkt):
        """Hand a copy of one HCI packet to the tap (a packet-capture writer), if one is set. direction: 0 = sent to
        the controller, 1 = received from it. A failing tap must never disturb the radio, so errors are only counted."""
        cb = self.tap
        if cb is None:
            return
        try:
            cb(direction, bytes(pkt))
        except Exception:                                   # noqa: BLE001
            self.tap_errors += 1

    # ------------------------------------------------------------- HCI command
    def send_cmd(self, ogf, ocf, params=b"", expect_complete=True, timeout=5.0):
        if self.sock is None:
            raise HciError("socket not open")
        op = opcode(ogf, ocf)
        pkt = struct.pack("<BHB", 0x01, op, len(params)) + params  # HCI_COMMAND_PKT
        with self._lock:
            self.sock.settimeout(timeout)
            self.sock.send(pkt)
            self._tap(0, pkt)
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data = self.sock.recv(1024)
                except socket.timeout:
                    break
                if not data or data[0] != 0x04:  # HCI_EVENT_PKT
                    continue
                self._tap(1, data)
                evt, plen = data[1], data[2]
                body = data[3:3 + plen]
                if evt == EVT_CMD_COMPLETE and len(body) >= 3:
                    rop = struct.unpack("<H", body[1:3])[0]
                    if rop == op:
                        status = body[3] if len(body) > 3 else None
                        return {"event": "complete", "status": status, "ret": body[3:]}
                elif evt == EVT_CMD_STATUS and len(body) >= 3:
                    rop = struct.unpack("<H", body[2:4])[0] if len(body) >= 4 else None
                    if rop == op:
                        return {"event": "status", "status": body[0], "ret": b""}
            raise HciError("timeout waiting for response to opcode 0x%04x on %s" % (op, self.label))

    def _check(self, name, ogf, ocf, params=b"", timeout=5.0):
        r = self.send_cmd(ogf, ocf, params, timeout=timeout)
        if r["status"] not in (0, None):
            raise HciError("%s on %s failed, HCI status 0x%02x" % (name, self.label, r["status"]))
        return r

    # --------------------------------------------------------- LE TX test API
    def tx_test_start(self, rf_channel, payload_len=37, payload_type=0x00, phy="1M", power_level=None):
        """Start LE Transmitter Test. payload_type: 0x00 PRBS9, 0x01
        11110000, 0x02 10101010, 0x03 PRBS15, 0x04 11111111, 0x05 00000000,
        0x06 00001111, 0x07 01010101 (Core spec Vol 4 Part E Table 7.8.29 --
        see PAYLOAD_TYPES for the name->code mapping). Ends any TX/RX test
        already running on this controller first (defensive: reissuing the
        test command directly, e.g. to hop channel, is not guaranteed by
        spec to behave the same as a clean start -- a prior, independently
        developed implementation of this exact engine ends-before-starting
        for every hop, and this was adopted to match).

        Uses the Enhanced v2 command (0x2034) normally, so PHY (1M/2M) is
        explicit. If power_level is given (dBm int, or 0x7E 'minimum'/0x7F
        'maximum' per spec), uses v4 (0x207B) instead, which adds TX power
        control -- confirmed supported on these BE200 controllers. Stops
        with tx_test_end()."""
        if not (0 <= rf_channel <= 39):
            raise ValueError("rf_channel must be 0..39")
        if not (0 <= payload_len <= 255):
            raise ValueError("payload_len must be 0..255, got %r" % (payload_len,))
        try:
            self.tx_test_end()
        except HciError:
            pass  # no test was running -- expected the first time
        phy_code = {"1M": 1, "2M": 2}[phy]
        if power_level is not None:
            # Core Spec 7.8.108 Transmit_Power_Level: a signed octet valid
            # ONLY -127..+20 dBm, plus the two sentinels 0x7E ("set minimum")
            # and 0x7F ("set maximum") -- NOT simply "any signed byte". The
            # earlier check here (-128..127) was a Python-side sanity check
            # that happened to re-open the exact bug class just fixed: e.g.
            # --power 30 would pass it, then the firmware would reject the
            # command with the same HCI status 0x12 "Invalid HCI Command
            # Parameters" as the Switching_Pattern_Length bug (confirmed by
            # code review). Reject it here instead, with a clear message.
            if power_level not in (0x7E, 0x7F) and not (-127 <= power_level <= 20):
                raise ValueError(
                    "power_level must be -127..20 (dBm), or 0x7E ('minimum')/0x7F "
                    "('maximum') per Core Spec 7.8.108 -- got %r" % (power_level,))
            # v4 params: TX_Channel, Length, Payload, PHY, CTE_Length(0),
            # CTE_Type(0), Switching_Pattern_Length, Antenna_IDs[pattern_len],
            # Transmit_Power_Level. BUG FIXED HERE (confirmed live: every
            # radio rejected this with HCI status 0x12 "Invalid HCI Command
            # Parameters"): Switching_Pattern_Length=0 looks reasonable since
            # no CTE is requested, but Core Spec 7.8.108 defines its valid
            # range as 0x02-0x4B UNCONDITIONALLY -- 0 is out of range and the
            # firmware correctly rejects it regardless of CTE_Length. Fix:
            # always supply a valid (if unused) 2-entry antenna pattern
            # [0x00, 0x00], matching a second, independently developed
            # implementation of this exact command that was confirmed
            # working. struct's signed-byte "b" format handles negative dBm.
            params = struct.pack("<BBBBBBBBBb", rf_channel, payload_len, payload_type,
                                  phy_code, 0, 0, 2, 0, 0, power_level)
            return self._check("LE Transmitter Test [v4]", OGF_LE,
                                 OCF["LE_TRANSMITTER_TEST_V4"], params)
        params = struct.pack("<BBBB", rf_channel, payload_len, payload_type, phy_code)
        return self._check("LE Enhanced Transmitter Test", OGF_LE, OCF["LE_ENHANCED_TRANSMITTER_TEST"], params)

    def rx_test_start(self, rf_channel, phy="1M", modulation_index="standard"):
        """Start LE Receiver Test (Enhanced v2, opcode 0x2033). Used to prove
        a tx_test_start() on another controller is really putting energy on
        a channel: Core Spec 7.8.30 defines LE Test End's Number_Of_Packets
        return value as the count of packets RECEIVED -- meaningful only
        here, not for the transmitter side (where it is always reported as
        0x0000 by spec, not a failure signal)."""
        if not (0 <= rf_channel <= 39):
            raise ValueError("rf_channel must be 0..39")
        phy_code = {"1M": 1, "2M": 2, "Coded": 3}[phy]
        mod_code = {"standard": 0, "stable": 1}[modulation_index]
        params = struct.pack("<BBB", rf_channel, phy_code, mod_code)
        return self._check("LE Enhanced Receiver Test", OGF_LE, OCF["LE_ENHANCED_RECEIVER_TEST"], params)

    def tx_test_end(self):
        """LE Test End -- stops an active TX or RX test. Returns the
        firmware's reported Number_Of_Packets (meaningful for RX test only;
        per spec always 0 when ending a TX test -- that is correct, not an
        error)."""
        r = self.send_cmd(OGF_LE, OCF["LE_TEST_END"])
        if r["status"] not in (0, None):
            raise HciError("LE Test End failed, status 0x%02x" % r["status"])
        n = struct.unpack("<H", r["ret"][1:3])[0] if len(r["ret"]) >= 3 else None
        return n

    # ------------------------------------------------------ legacy ADV (<=31B)
    def legacy_adv_configure(self, adv_data: bytes = b"", own_addr_type=0x00,
                              adv_type=ADV_TYPE_NONCONN_IND, channel_map=0x07,
                              interval_ms=100, scan_rsp: bytes = b"",
                              peer_addr: str = None, peer_addr_type=0x00,
                              tx_power_dbm=None):
        """tx_power_dbm: None keeps the plain legacy commands (controller's own
        default power, nothing reported). An int -127..+20 switches to the
        extended-advertising commands with the legacy-PDU bit set -- the
        over-the-air PDU is still a legacy one every scanner sees, but the
        command carries Advertising_TX_Power, and the controller answers with
        the power it actually SELECTED (it clamps: measured on the BE200 as
        ~+8 dBm). Returns that selected dBm, or None on the legacy path.

        adv_type: one of ADV_TYPE_IND (0x00), ADV_TYPE_DIRECT_IND_HIGH
        (0x01), ADV_TYPE_SCAN_IND (0x02), ADV_TYPE_NONCONN_IND (0x03),
        ADV_TYPE_DIRECT_IND_LOW (0x04) -- see Core Spec 7.8.5.
        channel_map bit0/1/2 = chan 37/38/39. interval units are 0.625 ms.
        Directed types (DIRECT_IND, high/low duty) REQUIRE peer_addr
        ("AA:BB:CC:DD:EE:FF") and must not be given adv_data (the directed
        PDU has no AdvData field at the link layer -- see PAYLOAD_TYPES /
        DIRECTED_ADV_TYPES docs above for why this is enforced, not just
        silently ignored)."""
        directed = adv_type in DIRECTED_ADV_TYPES
        if directed and not peer_addr:
            raise ValueError("adv_type 0x%02x is a directed PDU and requires peer_addr" % adv_type)
        if directed and adv_data:
            raise ValueError("adv_type 0x%02x (directed) carries no AdvData at the link layer -- "
                              "omit adv_data for this type" % adv_type)
        if len(adv_data) > 31:
            raise ValueError("legacy adv_data must be <=31 bytes, got %d" % len(adv_data))
        # Core Spec 7.8.5: Advertising_Interval_Min/Max range is 0x0020-0x4000
        # (20-10240ms in 0.625ms units). Before Bluetooth 5.0 non-connectable and
        # scannable advertising needed >=100ms; since 5.0 the minimum is 20ms for every
        # type, and these controllers are 5.4. An interval below the range used to be
        # raised to 100ms silently (so a 20ms request, the adv-flood default and the
        # advertised minimum, was sent at 100ms); it is now rejected, and a controller
        # that still refuses a short interval says so (see the check below). The upper bound was previously
        # unenforced: an interval above ~40959ms overflows struct.pack's
        # unsigned-short "H" field and raises struct.error -- a different
        # exception type than the HciError/ValueError callers already expect
        # (confirmed by code review), so reject it here with a clear message
        # instead of letting it reach struct.pack unguarded.
        if interval_ms > 10240:
            raise ValueError("interval_ms must be <=10240 (Core Spec 7.8.5 max), got %r" % (interval_ms,))
        if interval_ms < 20:
            raise ValueError("interval_ms must be >=20 (Core Spec 7.8.5 min), got %r" % (interval_ms,))
        iv = int(round(interval_ms / 0.625))           # 0.625ms units; 20ms = 0x20
        if peer_addr:
            peer_bytes = bytes(int(b, 16) for b in reversed(peer_addr.split(":")))
            if len(peer_bytes) != 6:
                raise ValueError("peer_addr must be AA:BB:CC:DD:EE:FF")
        else:
            peer_bytes = b"\x00" * 6
        if tx_power_dbm is not None:
            return self._ext_adv_configure(adv_data, own_addr_type, adv_type, channel_map, iv,
                                           scan_rsp, peer_bytes, peer_addr_type, tx_power_dbm)
        # HCI LE Set Advertising Parameters (Core spec 7.8.5), 15 bytes total:
        # Interval_Min(2) Interval_Max(2) Adv_Type(1) Own_Addr_Type(1)
        # Peer_Addr_Type(1) Peer_Addr(6) Channel_Map(1) Filter_Policy(1)
        params = struct.pack("<HHBBB6sBB", iv, iv, adv_type, own_addr_type, peer_addr_type,
                             peer_bytes, channel_map, 0x00)
        try:
            self._check("LE Set Advertising Parameters", OGF_LE, OCF["LE_SET_ADV_PARAMETERS"], params)
        except HciError as e:
            if interval_ms < 100 and adv_type in (ADV_TYPE_SCAN_IND, ADV_TYPE_NONCONN_IND) and "0x12" in str(e):
                raise HciError("%s -- this controller may only allow >=100ms for scannable/non-connectable "
                               "advertising (Bluetooth older than 5.0); use --interval 100 or higher" % e)
            raise
        if not directed:
            data = adv_data + b"\x00" * (31 - len(adv_data))
            self._check("LE Set Advertising Data", OGF_LE, OCF["LE_SET_ADV_DATA"],
                         struct.pack("<B31s", len(adv_data), data))
        scannable = adv_type in (ADV_TYPE_IND, ADV_TYPE_SCAN_IND)
        if scan_rsp and not scannable:
            raise ValueError("scan_rsp only applies to scannable PDUs (ADV_IND/ADV_SCAN_IND)")
        if scan_rsp:
            if len(scan_rsp) > 31:
                raise ValueError("scan_rsp must be <=31 bytes")
            sdata = scan_rsp + b"\x00" * (31 - len(scan_rsp))
            self._check("LE Set Scan Response Data", OGF_LE, OCF["LE_SET_SCAN_RESPONSE_DATA"],
                         struct.pack("<B31s", len(scan_rsp), sdata))

    # Event_Properties for a LEGACY PDU carried by the extended commands
    # (Core Spec 7.8.53 table): connectable 0x01, scannable 0x02, directed
    # 0x04, high duty 0x08, legacy 0x10.
    _EXT_ADV_PROPS = {ADV_TYPE_IND: 0x13, ADV_TYPE_DIRECT_IND_HIGH: 0x1D, ADV_TYPE_SCAN_IND: 0x12,
                      ADV_TYPE_NONCONN_IND: 0x10, ADV_TYPE_DIRECT_IND_LOW: 0x15}

    def _ext_adv_configure(self, adv_data, own_addr_type, adv_type, channel_map, iv, scan_rsp,
                           peer_bytes, peer_addr_type, tx_power_dbm):
        if not (-127 <= tx_power_dbm <= 20):
            raise ValueError("tx_power_dbm must be -127..20 (Core Spec 7.8.53), got %r" % (tx_power_dbm,))
        directed = adv_type in DIRECTED_ADV_TYPES
        # Handle(1) Props(2) PrimMin(3) PrimMax(3) PrimChMap(1) OwnAddrType(1) PeerAddrType(1)
        # PeerAddr(6) FilterPolicy(1) TxPower(1) PrimPHY(1) SecMaxSkip(1) SecPHY(1) SID(1) ScanReqNotif(1)
        params = (bytes([0]) + struct.pack("<H", self._EXT_ADV_PROPS[adv_type])
                  + iv.to_bytes(3, "little") * 2
                  + bytes([channel_map, own_addr_type, peer_addr_type]) + peer_bytes
                  + bytes([0x00]) + struct.pack("<b", tx_power_dbm) + bytes([0x01, 0x00, 0x01, 0x00, 0x00]))
        r = self.send_cmd(OGF_LE, OCF["LE_SET_EXT_ADV_PARAMETERS"], params)
        if r["status"] not in (0, None):
            raise HciError("LE Set Extended Advertising Parameters failed, status 0x%02x" % r["status"])
        self._adv_ext = True
        self._adv_high_duty = adv_type == ADV_TYPE_DIRECT_IND_HIGH
        selected = struct.unpack("<b", r["ret"][1:2])[0] if len(r["ret"]) >= 2 else None
        if not directed:
            self._check("LE Set Extended Advertising Data", OGF_LE, OCF["LE_SET_EXT_ADV_DATA"],
                         bytes([0, 0x03, 0x01, len(adv_data)]) + adv_data)
        if scan_rsp and adv_type in (ADV_TYPE_IND, ADV_TYPE_SCAN_IND):
            self._check("LE Set Extended Scan Response Data", OGF_LE, OCF["LE_SET_EXT_SCAN_RESPONSE_DATA"],
                         bytes([0, 0x03, 0x01, len(scan_rsp)]) + scan_rsp)
        return selected

    def legacy_adv_enable(self, on: bool):
        if self._adv_ext:
            if on:
                # high duty directed MUST carry a non-zero Duration <= 1.28 s (10 ms units), else 0x0c
                dur = 128 if self._adv_high_duty else 0
                self._check("LE Set Extended Advertising Enable", OGF_LE, OCF["LE_SET_EXT_ADV_ENABLE"],
                             bytes([1, 1, 0]) + struct.pack("<H", dur) + bytes([0]))
                return
            # disable all sets, then drop set 0 so the controller is back to a clean state
            try:
                self._check("LE Set Extended Advertising Enable", OGF_LE, OCF["LE_SET_EXT_ADV_ENABLE"], bytes([0, 0]))
            finally:
                self._adv_ext = False
                try:
                    self.send_cmd(OGF_LE, OCF["LE_REMOVE_ADV_SET"], bytes([0]))
                except HciError:
                    pass
            return
        self._check("LE Set Advertising Enable", OGF_LE, OCF["LE_SET_ADV_ENABLE"],
                     struct.pack("<B", 1 if on else 0))

    # ----------------------------------------------------------------- scanner
    def scan_configure(self, interval_ms=100, window_ms=100, active=True, own_addr_type=0x00):
        # Core Spec 7.8.10: LE_Scan_Interval/Window are both 0x0004-0x4000
        # (2.5-10240ms) and Window MUST be <= Interval; violating either
        # raises an HCI error (or, for a too-large ms value, overflows
        # struct.pack's unsigned-short field with struct.error) -- reject
        # both up front with a clear message instead (confirmed by code
        # review: neither was previously checked here).
        if interval_ms > 10240 or window_ms > 10240:
            raise ValueError("interval_ms/window_ms must be <=10240 (Core Spec 7.8.10 max)")
        if window_ms > interval_ms:
            raise ValueError("window_ms (%r) must be <= interval_ms (%r)" % (window_ms, interval_ms))
        iv = max(int(round(interval_ms / 0.625)), 4)
        wn = max(int(round(window_ms / 0.625)), 4)
        params = struct.pack("<BHHBB", 0x01 if active else 0x00, iv, wn, own_addr_type, 0x00)
        self._check("LE Set Scan Parameters", OGF_LE, OCF["LE_SET_SCAN_PARAMETERS"], params)

    def scan_enable(self, on: bool, filter_dup=False):
        self._check("LE Set Scan Enable", OGF_LE, OCF["LE_SET_SCAN_ENABLE"],
                     struct.pack("<BB", 1 if on else 0, 1 if filter_dup else 0))

    def ext_scan_configure(self, interval_ms=100, window_ms=100, active=False, coded=False,
                            own_addr_type=0x00):
        """LE Set Extended Scan Parameters (Core 7.8.64). Needed to see Bluetooth 5
        extended advertising; legacy scanning cannot receive it. Also reports
        legacy advertisers, so one path covers everything. coded=True adds the
        LE Coded PHY (long range) as a second scanning PHY."""
        if window_ms > interval_ms:
            raise ValueError("window_ms (%r) must be <= interval_ms (%r)" % (window_ms, interval_ms))
        iv = max(int(round(interval_ms / 0.625)), 4)
        wn = max(int(round(window_ms / 0.625)), 4)
        phys = 0x01 | (0x04 if coded else 0x00)
        params = struct.pack("<BBB", own_addr_type, 0x00, phys)
        for _ in range(2 if coded else 1):
            params += struct.pack("<BHH", 0x01 if active else 0x00, iv, wn)
        self._check("LE Set Extended Scan Parameters", OGF_LE, OCF["LE_SET_EXT_SCAN_PARAMETERS"], params)

    def ext_scan_enable(self, on: bool, filter_dup=False):
        # duration=0 / period=0: scan continuously until disabled
        self._check("LE Set Extended Scan Enable", OGF_LE, OCF["LE_SET_EXT_SCAN_ENABLE"],
                     struct.pack("<BBHH", 1 if on else 0, 1 if filter_dup else 0, 0, 0))

    def read_events(self, duration_s, on_adv_report=None):
        """Block reading raw HCI events for duration_s seconds, calling
        on_adv_report(parsed_dict) for each LE Advertising Report encountered.
        Used both by the scanner and (with on_adv_report=None) as a plain
        drain when waiting out a TX-test burst."""
        end = time.time() + duration_s
        self.sock.settimeout(0.5)
        count = 0
        while time.time() < end:
            try:
                data = self.sock.recv(2048)
            except socket.timeout:
                continue
            except OSError:
                self.dead = True        # controller vanished / socket broken: let the caller bail out
                break
            if not data or data[0] != 0x04:
                continue
            self._tap(1, data)
            evt, plen = data[1], data[2]
            body = data[3:3 + plen]
            if evt == EVT_LE_META and body and on_adv_report:
                try:
                    if body[0] == LE_ADVERTISING_REPORT:
                        reps = _parse_legacy_adv_reports(body[1:])
                    elif body[0] == LE_EXT_ADVERTISING_REPORT:
                        reps = self._ext_reassembler.feed(_parse_ext_adv_reports(body[1:]))
                    else:
                        reps = []
                except Exception:                  # malformed event: count it, keep scanning
                    self.bad_events += 1
                    continue
                for rep in reps:
                    on_adv_report(rep)
                    count += 1
        return count


def _parse_legacy_adv_reports(payload):
    """Parse the LE Advertising Report sub-event body (can contain several
    reports back to back). Returns a list of dicts. A payload that is shorter than its own header claims
    returns [] instead of raising: one malformed event must never end a whole sniff job."""
    if not payload:
        return []
    num = payload[0]
    # fixed part: event types (1) + address types (1) + addresses (6) + data lengths (1) per report, then data, then RSSI
    if len(payload) < 1 + num * 9:
        return []
    off = 1
    evt_types = list(payload[off:off + num]); off += num
    addr_types = list(payload[off:off + num]); off += num
    addrs = [payload[off + 6 * i:off + 6 * i + 6] for i in range(num)]; off += 6 * num
    lens = list(payload[off:off + num]); off += num
    if len(payload) < off + sum(lens) + num:
        return []
    datas = []
    for L in lens:
        datas.append(bytes(payload[off:off + L])); off += L
    rssis = [struct.unpack("<b", payload[off + i:off + i + 1])[0] for i in range(num)]
    return [{
        "event_type": evt_types[i],
        "addr_type": addr_types[i],
        "addr": ":".join("%02X" % b for b in reversed(addrs[i])),
        "data": datas[i],
        "rssi": rssis[i],
    } for i in range(num)]


# Extended-report event_type bits (Core 7.7.65.13)
EXT_CONNECTABLE, EXT_SCANNABLE, EXT_DIRECTED, EXT_SCAN_RSP, EXT_LEGACY = 0x01, 0x02, 0x04, 0x08, 0x10


def _parse_ext_adv_reports(payload):
    """LE Extended Advertising Report sub-event body -> list of dicts. Unlike the
    legacy report the fields are interleaved per report, and data may arrive in
    fragments (data_status 0x01 = more to come, 0x02 = truncated)."""
    out = []
    if not payload:
        return out
    num, off = payload[0], 1
    for _ in range(num):
        if off + 24 > len(payload):
            break
        (etype, atype, addr, pphy, sphy, sid, txp, rssi, per_iv, datype, daddr,
         dlen) = struct.unpack_from("<HB6sBBBbbHB6sB", payload, off)
        off += 24
        data = payload[off:off + dlen]
        off += dlen
        out.append({
            "ext": True, "event_type": etype, "addr_type": atype,
            "addr": ":".join("%02X" % b for b in reversed(addr)),
            "primary_phy": pphy, "secondary_phy": sphy, "sid": sid,
            "tx_power": None if txp == 127 else txp, "rssi": None if rssi == 127 else rssi,
            "periodic_interval": per_iv, "data": bytes(data),
            "status": (etype >> 5) & 0x03,
        })
    return out


class _ExtReassembler:
    """Joins fragmented extended reports (status 0x01 'more data') per
    (addr, sid, scan-rsp) and releases only complete or truncated ones."""
    MAX_PENDING = 64

    def __init__(self):
        self.pending = {}

    def feed(self, reports):
        done = []
        for r in reports:
            key = (r["addr"], r["sid"], bool(r["event_type"] & EXT_SCAN_RSP))
            if key in self.pending:
                prev = self.pending.pop(key)
                r = dict(r, data=prev["data"] + r["data"])
            if r["status"] == 0x01:
                if len(self.pending) >= self.MAX_PENDING:
                    self.pending.pop(next(iter(self.pending)))
                self.pending[key] = r
            else:
                done.append(r)
        return done


def force_recover(index, timeout=10.0):
    """Explicitly ask bluetoothd to re-power a controller, retrying for up
    to `timeout` seconds. Exists because an abruptly-killed process holding
    a controller's HCI_CHANNEL_USER socket (e.g. SIGKILL, which skips
    HciSocket.close()'s cleanup) was observed live to leave the kernel's own
    close-time HCI_Reset timing out (dmesg: "HCI reset during shutdown
    failed", "Opcode 0x0c03 failed: -110"), leaving the controller reporting
    Powered=no / PowerState=on until explicitly re-powered. A single
    `btmgmt_power(index, True)` call was sufficient to fully recover all 3
    affected controllers in that incident; this wraps it with retries for
    robustness. Safe to call on a controller that is already fine."""
    deadline = time.time() + timeout
    last = (False, "not attempted")
    while time.time() < deadline:
        last = btmgmt_power(index, True)
        if last[0]:
            return last
        time.sleep(0.5)
    return last


if __name__ == "__main__":
    # read-only smoke test: open+immediately close one controller (the first one present unless an index is
    # given). Does not transmit anything.
    import sys
    present = detect_hci_indices()
    if len(sys.argv) > 1:
        idx = int(sys.argv[1])
    elif present:
        idx = present[0]
    else:
        print("no Bluetooth controllers found under /sys/class/bluetooth")
        sys.exit(1)
    print("opening user channel on hci%d ..." % idx)
    h = HciSocket(idx).open()
    print("opened OK, closing (restoring to BlueZ) ...")
    h.close()
    print("hci_raw smoke test OK")
