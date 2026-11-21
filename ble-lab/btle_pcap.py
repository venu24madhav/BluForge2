#!/usr/bin/env python3
"""
btle_pcap.py - Convert a BluForge .btsnoop (HCI-transport capture) into a
genuine BLE Link-Layer .pcap, so it opens in Wireshark exactly like a capture
from a dedicated over-the-air sniffer (Ubertooth/nRF52/Ellisys): Protocol
column shows "LE LL", Info shows "ADV_IND"/"SCAN_RSP"/... directly, and
Source is the advertiser's address (Wireshark resolves the OUI to a vendor
name automatically, the same way it does for any Ethernet/BLE MAC).

The .btsnoop we produce is an HCI-transport capture (btmon's "Linux Monitor"
format) - Wireshark decodes it fully and correctly, it's just framed as
HCI_EVT -> LE Meta -> LE (Extended) Advertising Report rather than as a raw
over-the-air Link-Layer PDU. This script re-derives the actual LL PDU bytes
(AdvA, PDU type, AdvData) from each HCI Advertising Report and re-wraps them
as LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR (256) frames - the format dedicated
sniffers emit:

    [10-byte BLE RF pseudo-header] [4-byte Access Address]
    [2-byte PDU header] [payload] [3-byte CRC placeholder]

The RF pseudo-header is what carries per-packet RSSI into Wireshark
("Signal dBm"), which the plain LINKTYPE_BLUETOOTH_LE_LL (251) framing has
no room for. It also lets us set CRC-Checked/CRC-Valid, which is
semantically accurate - the controller validates the CRC in hardware and
only delivers packets that passed, it just strips the CRC bytes before the
host sees them, so the 3 trailing bytes are a zero placeholder rather than
the genuine value.

HONEST LIMITATION - RF Channel: the HCI Advertising Report does NOT tell us
which of the three primary advertising channels (37/38/39) a packet arrived
on, so the pseudo-header's RF channel field is a fixed placeholder (index 0
= 2402 MHz = advertising channel 37). Every other field in the header is
real. Do not read the channel column as ground truth; a dedicated sniffer
with its own radio is what reports that accurately.

Every byte-layout decision here (pseudo-header layout and flag bits, Access
Address placement, PDU header bit positions, epoch offset) was verified
empirically against tshark's own decode on real captures, not taken from
memory - see git history / dev notes for the hypothesis testing that pinned
each one down. RSSI values in the output were cross-checked against the
source HCI reports and match exactly.

Usage:
  python3 btle_pcap.py capture.btsnoop out.pcap
"""
import sys, struct, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btlab import parse_ext_adv_reports, parse_adv_reports

LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR = 256
BLE_ADV_ACCESS_ADDRESS = 0x8E89BED6           # fixed AA used on all 3 primary adv channels
AA_BYTES = struct.pack("<I", BLE_ADV_ACCESS_ADDRESS)

# ---- BLE RF pseudo-header (Wireshark packet-btle.c "btle_rf", 10 bytes) ----
#   rf_channel(1) signal_dbm(1,signed) noise_dbm(1,signed)
#   access_address_offenses(1) reference_access_address(4,LE) flags(2,LE)
# Flag bits confirmed against tshark's own field breakdown on a real capture.
RF_DEWHITENED        = 0x0001
RF_SIGNAL_POWER_VALID= 0x0002
RF_REF_AA_VALID      = 0x0010
RF_CRC_CHECKED       = 0x0400
RF_CRC_VALID         = 0x0800
# flags bits 7-9 = PDU type; 1 = "Advertising". Without this the dissector
# infers adv-vs-data from the RF channel number and mis-parses our frames as
# data-channel PDUs (Info column shows "Unknown") - verified empirically.
RF_PDU_TYPE_ADVERTISING = 1 << 7
# HCI does not report the arrival advertising channel - see module docstring.
RF_CHANNEL_PLACEHOLDER = 0            # index 0 = 2402 MHz = adv channel 37
RF_UNKNOWN_DBM = 0x7F                 # "not available" per the header convention


def rf_phdr(rssi):
    """Build the 10-byte BLE RF pseudo-header for one advertising packet."""
    flags = (RF_DEWHITENED | RF_REF_AA_VALID | RF_CRC_CHECKED | RF_CRC_VALID
             | RF_PDU_TYPE_ADVERTISING)
    signal = RF_UNKNOWN_DBM
    if rssi is not None:
        signal = max(-127, min(127, int(rssi)))
        flags |= RF_SIGNAL_POWER_VALID
    return struct.pack("<BbbBIH", RF_CHANNEL_PLACEHOLDER, signal, RF_UNKNOWN_DBM,
                       0, BLE_ADV_ACCESS_ADDRESS, flags)
# Wireshark's btle dissector on this linktype always expects a trailing 3-byte
# CRC field to be present (confirmed empirically - omitting it truncates the
# last 3 bytes of the actual AD payload and reports "Malformed Packet"). The
# controller already validates and strips the real CRC before HCI delivers
# the report to the host, so we don't have the genuine value - zero-pad it;
# the dissector doesn't validate CRC correctness, only that 3 bytes exist.
CRC_PLACEHOLDER = b"\x00\x00\x00"

# btsnoop timestamps are microseconds since 0000-01-01 (per the btsnoop spec), not
# Unix epoch. Offset verified empirically against tshark's own frame.time_epoch on
# a real capture (matched across two independent frames, both exact match).
BTSNOOP_EPOCH_OFFSET_US = 62168256000000000

# Advertising-channel PDU header (Core Spec Vol 6 Part B Sec 2.3), byte0 bit
# positions confirmed via tshark's own field breakdown: bits[3:0]=Type,
# bit4=RFU, bit5=ChSel/RFU, bit6=TxAdd, bit7=RxAdd.
PDU_ADV_IND         = 0x0
PDU_ADV_DIRECT_IND  = 0x1
PDU_ADV_NONCONN_IND = 0x2
PDU_SCAN_RSP        = 0x4
PDU_ADV_SCAN_IND    = 0x6
PDU_ADV_EXT_IND     = 0x7


def read_btsnoop_hci_events(path):
    """Yield (event_code, params_bytes) for every HCI Event Pkt record in a
    BlueZ btmon .btsnoop (datalink type 2001, 'Linux Monitor' framing).
    Record framing: standard btsnoop record header, but the 32-bit `flags`
    field is repurposed to carry opcode (low 16 bits) + adapter index (high
    16 bits) - confirmed empirically: opcode 12 = System Note (ASCII text),
    opcode 3 = HCI Event Pkt, whose payload is event_code(1)+plen(1)+
    params(plen) with no leading HCI packet-type byte."""
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
            if opcode != 3 or len(payload) < 2:
                continue
            event_code = payload[0]
            params = payload[2:]
            yield ts, event_code, params


def legacy_ll_pdu(report):
    """Access Address(4) + advertising-channel PDU header(2) + payload."""
    addr_bytes = bytes(int(x, 16) for x in reversed(report["addr"].split(":")))
    tx_add = 1 if report["addr_type"] == "random" else 0
    pdu = report["pdu"]
    ad_hex = report.get("adv_hex", "")
    ad_bytes = bytes.fromhex(ad_hex) if ad_hex else b""
    rx_add = 0

    if pdu == "ADV_IND":            ptype = PDU_ADV_IND;         payload = addr_bytes + ad_bytes
    elif pdu == "ADV_NONCONN_IND":  ptype = PDU_ADV_NONCONN_IND; payload = addr_bytes + ad_bytes
    elif pdu == "ADV_SCAN_IND":     ptype = PDU_ADV_SCAN_IND;    payload = addr_bytes + ad_bytes
    elif pdu == "SCAN_RSP":         ptype = PDU_SCAN_RSP;        payload = addr_bytes + ad_bytes
    elif pdu == "ADV_DIRECT_IND":
        ptype = PDU_ADV_DIRECT_IND
        payload = addr_bytes + b"\x00" * 6   # TargetA not carried by the HCI report shape
    else:
        return None

    if len(payload) > 63:                 # 6-bit length field limit
        payload = payload[:63]
    byte0 = (ptype & 0x0F) | ((tx_add & 1) << 6) | ((rx_add & 1) << 7)
    byte1 = len(payload) & 0x3F
    return AA_BYTES + bytes([byte0, byte1]) + payload + CRC_PLACEHOLDER


def _ad_structures_fitting(ad_bytes, budget):
    """Return as many WHOLE AD structures from the front of ad_bytes as fit
    within `budget` bytes. A real extended advertisement's reassembled
    AdvData can legitimately exceed what a single PDU can carry (63-byte
    header-length-field cap) - it's meant to span chained AUX_ADV_IND PDUs,
    which we don't reconstruct here. Truncating mid-structure corrupts the
    AD-structure's own internal length prefix and Wireshark reports the
    frame as malformed - so we only ever cut between whole structures."""
    i = 0
    while i < len(ad_bytes):
        ln = ad_bytes[i]
        if ln == 0 or i + 1 + ln > len(ad_bytes):
            break
        if i + 1 + ln > budget:
            break
        i += 1 + ln
    return ad_bytes[:i]


def ext_ll_pdu(report):
    """Best-effort ADV_EXT_IND reconstruction: Extended Header carrying just
    AdvA (flag bit0), then AdvData. Enough for Wireshark to show Source=AdvA
    and Info=ADV_EXT_IND; optional sub-fields (TargetA/ADI/AuxPtr/TxPower)
    are omitted since the HCI report doesn't carry their raw bytes. AdvData
    that doesn't fit a single PDU is trimmed at a whole-AD-structure
    boundary (see _ad_structures_fitting) rather than corrupted."""
    addr_bytes = bytes(int(x, 16) for x in reversed(report["addr"].split(":")))
    tx_add = 1 if report["addr_type"] == "random" else 0
    ad_hex = report.get("adv_hex", "")
    ad_bytes = bytes.fromhex(ad_hex) if ad_hex else b""

    ext_hdr_flags = 0x01              # bit0 = AdvA present
    ext_hdr_content = addr_bytes
    ext_hdr_len = 1 + len(ext_hdr_content)          # flags byte + AdvA
    if ext_hdr_len > 63: return None

    ad_budget = 63 - ext_hdr_len
    ad_bytes = _ad_structures_fitting(ad_bytes, ad_budget)

    payload = bytes([ext_hdr_len & 0x3F, ext_hdr_flags]) + ext_hdr_content + ad_bytes
    byte0 = (PDU_ADV_EXT_IND & 0x0F) | ((tx_add & 1) << 6)
    byte1 = len(payload) & 0x3F
    return AA_BYTES + bytes([byte0, byte1]) + payload + CRC_PLACEHOLDER


def convert(in_path, out_path):
    packets = []
    reasm = {}
    for ts, event_code, params in read_btsnoop_hci_events(in_path):
        if event_code != 0x3E:
            continue
        reports = []
        if params and params[0] == 0x0D:
            reports = list(parse_ext_adv_reports(params, reasm))
        elif params and params[0] == 0x02:
            reports = list(parse_adv_reports(params))
        for r in reports:
            pdu_bytes = legacy_ll_pdu(r) if r.get("legacy", True) else ext_ll_pdu(r)
            if pdu_bytes is None:
                continue
            # prepend the RF pseudo-header so per-packet RSSI survives into the
            # capture file (the plain LE_LL framing has nowhere to put it)
            packets.append((ts, rf_phdr(r.get("rssi")) + pdu_bytes))

    with open(out_path, "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535,
                             LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR))
        for raw_ts, data in packets:
            unix_us = raw_ts - BTSNOOP_EPOCH_OFFSET_US
            sec = unix_us // 1_000_000
            usec = unix_us % 1_000_000
            f.write(struct.pack("<IIII", sec & 0xFFFFFFFF, usec, len(data), len(data)))
            f.write(data)
    return len(packets)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: btle_pcap.py capture.btsnoop out.pcap", file=sys.stderr)
        sys.exit(1)
    n = convert(sys.argv[1], sys.argv[2])
    print(f"wrote {n} LE LL packets -> {sys.argv[2]}")
