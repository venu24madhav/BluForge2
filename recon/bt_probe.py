#!/usr/bin/python3
"""
Read-only HCI capability probe (raw HCI channel, HCI_CHANNEL_RAW).

SAFETY: only opcodes in READ_ONLY_OPCODES can ever be sent (hard guard in hci_cmd()).
No reset / set / enable / test-mode / power commands are present in the allowlist.
The HCI_CHANNEL_USER (exclusive) channel is never opened.

Usage: bt_probe.py [out.json]
"""
import socket, struct, sys, json, select, time, os

SOL_HCI = 0
HCI_FILTER = 2
HCI_EVENT_PKT = 4
EVT_CMD_COMPLETE = 0x0E
EVT_CMD_STATUS = 0x0F

# (opcode, name) -- every one of these is a "Read ..." / informational command
READ_ONLY_OPCODES = {
    0x1001: "Read Local Version Information",
    0x1002: "Read Local Supported Commands",
    0x1003: "Read Local Supported Features",
    0x1009: "Read BD_ADDR",
    0x2002: "LE Read Buffer Size [v1]",
    0x2003: "LE Read Local Supported Features",
    0x2007: "LE Read Advertising Physical Channel TX Power",
    0x200F: "LE Read Filter Accept List Size",
    0x201C: "LE Read Supported States",
    0x202A: "LE Read Resolving List Size",
    0x202F: "LE Read Maximum Data Length",
    0x203A: "LE Read Maximum Advertising Data Length",
    0x203B: "LE Read Number of Supported Advertising Sets",
    0x204B: "LE Read Transmit Power",
}

# --- Supported Commands bitmap (Core Spec Vol 4 Part E 6.27) : (octet, bit) ---
CMDS = [
    # name, opcode, octet, bit
    ("LE Set Random Address", 0x2005, 25, 4),
    ("LE Set Advertising Parameters", 0x2006, 25, 5),
    ("LE Read Advertising Physical Channel TX Power", 0x2007, 25, 6),
    ("LE Set Advertising Data", 0x2008, 25, 7),
    ("LE Set Scan Response Data", 0x2009, 26, 0),
    ("LE Set Advertising Enable", 0x200A, 26, 1),
    ("LE Set Scan Parameters", 0x200B, 26, 2),
    ("LE Set Scan Enable", 0x200C, 26, 3),
    ("LE Read Filter Accept List Size", 0x200F, 26, 6),
    ("LE Read Supported States", 0x201C, 28, 3),
    ("LE Receiver Test [v1]", 0x201D, 28, 4),
    ("LE Transmitter Test [v1]", 0x201E, 28, 5),
    ("LE Test End", 0x201F, 28, 6),
    ("LE Read Resolving List Size", 0x202A, 34, 6),
    ("LE Read Maximum Data Length", 0x202F, 35, 3),
    ("LE Enhanced Receiver Test [v2]", 0x2033, 35, 7),
    ("LE Enhanced Transmitter Test [v2]", 0x2034, 36, 0),
    ("LE Set Advertising Set Random Address", 0x2035, 36, 1),
    ("LE Set Extended Advertising Parameters [v1]", 0x2036, 36, 2),
    ("LE Set Extended Advertising Data", 0x2037, 36, 3),
    ("LE Set Extended Scan Response Data", 0x2038, 36, 4),
    ("LE Set Extended Advertising Enable", 0x2039, 36, 5),
    ("LE Read Maximum Advertising Data Length", 0x203A, 36, 6),
    ("LE Read Number of Supported Advertising Sets", 0x203B, 36, 7),
    ("LE Remove Advertising Set", 0x203C, 37, 0),
    ("LE Clear Advertising Sets", 0x203D, 37, 1),
    ("LE Set Periodic Advertising Parameters [v1]", 0x203E, 37, 2),
    ("LE Set Periodic Advertising Data", 0x203F, 37, 3),
    ("LE Set Periodic Advertising Enable", 0x2040, 37, 4),
    ("LE Set Extended Scan Parameters", 0x2041, 37, 5),
    ("LE Set Extended Scan Enable", 0x2042, 37, 6),
    ("LE Extended Create Connection [v1]", 0x2043, 37, 7),
    ("LE Read Transmit Power", 0x204B, 38, 7),
    ("LE Read RF Path Compensation", 0x204C, 39, 0),
    ("LE Write RF Path Compensation", 0x204D, 39, 1),
    ("LE Receiver Test [v3]", 0x204F, 39, 3),
    ("LE Transmitter Test [v3]", 0x2050, 39, 4),
    ("LE Enhanced Read Transmit Power Level", 0x2076, 44, 3),
    ("LE Read Remote Transmit Power Level", 0x2077, 44, 4),
    ("LE Set Path Loss Reporting Parameters", 0x2078, 44, 5),
    ("LE Set Path Loss Reporting Enable", 0x2079, 44, 6),
    ("LE Set Transmit Power Reporting Enable", 0x207A, 44, 7),
    ("LE Transmitter Test [v4]", 0x207B, 45, 0),
    ("LE Set Data Related Address Changes", 0x207C, 45, 1),
    ("Read Transmit Power Level (BR/EDR)", 0x0C2D, 10, 1),
    ("Write Inquiry Transmit Power Level (BR/EDR)", 0x0C59, 18, 1),
    ("Enable Device Under Test Mode (BR/EDR)", 0x1803, 16, 2),
]

# LE features Link Layer FeatureSet (Core Spec Vol 6 Part B 4.6)
LE_FEATS = {
    (0, 0): "LE Encryption", (0, 1): "Connection Parameters Request Procedure",
    (0, 2): "Extended Reject Indication", (0, 3): "Peripheral-initiated Features Exchange",
    (0, 4): "LE Ping", (0, 5): "LE Data Packet Length Extension", (0, 6): "LL Privacy",
    (0, 7): "Extended Scanner Filter Policies",
    (1, 0): "LE 2M PHY", (1, 1): "Stable Modulation Index - Transmitter",
    (1, 2): "Stable Modulation Index - Receiver", (1, 3): "LE Coded PHY",
    (1, 4): "LE Extended Advertising", (1, 5): "LE Periodic Advertising",
    (1, 6): "Channel Selection Algorithm #2", (1, 7): "LE Power Class 1",
    (2, 0): "Minimum Number of Used Channels Procedure", (2, 1): "Connection CTE Request",
    (2, 2): "Connection CTE Response", (2, 3): "Connectionless CTE Transmitter",
    (2, 4): "Connectionless CTE Receiver", (2, 5): "Antenna Switching During CTE Tx (AoD)",
    (2, 6): "Antenna Switching During CTE Rx (AoA)", (2, 7): "Receiving CTE",
    (3, 0): "Periodic Advertising Sync Transfer - Sender",
    (3, 1): "Periodic Advertising Sync Transfer - Recipient",
    (3, 2): "Sleep Clock Accuracy Updates", (3, 3): "Remote Public Key Validation",
    (3, 4): "Connected Isochronous Stream - Central",
    (3, 5): "Connected Isochronous Stream - Peripheral",
    (3, 6): "Isochronous Broadcaster", (3, 7): "Synchronized Receiver",
    (4, 0): "Connected Isochronous Stream (Host Support)",
    (4, 1): "LE Power Control Request", (4, 2): "LE Power Change Indication",
    (4, 3): "LE Path Loss Monitoring", (4, 4): "Periodic Advertising ADI support",
    (4, 5): "Connection Subrating", (4, 6): "Connection Subrating (Host Support)",
    (4, 7): "Channel Classification",
}


def hci_cmd(sock, dev, opcode, params=b"", timeout=2.0):
    """Send ONE allowlisted read-only HCI command, return (status, return_params) or raise."""
    if opcode not in READ_ONLY_OPCODES:
        raise RuntimeError("REFUSING to send non-allowlisted opcode 0x%04x" % opcode)
    # filter: only HCI events, only Command Complete / Command Status, only this opcode
    evmask0 = (1 << EVT_CMD_COMPLETE) | (1 << EVT_CMD_STATUS)
    # struct hci_ufilter is 16 bytes (u32 type_mask, u32 event_mask[2], le16 opcode, +2 pad);
    # newer kernels require optlen >= sizeof(struct hci_ufilter)
    flt = struct.pack("<IIIH2x", 1 << HCI_EVENT_PKT, evmask0, 0, opcode)
    sock.setsockopt(SOL_HCI, HCI_FILTER, flt)
    pkt = b"\x01" + struct.pack("<HB", opcode, len(params)) + params
    sock.send(pkt)
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            return (None, b"")  # timeout
        r, _, _ = select.select([sock], [], [], left)
        if not r:
            return (None, b"")
        data = sock.recv(512)
        if len(data) < 3 or data[0] != 0x04:
            continue
        evt, plen = data[1], data[2]
        body = data[3:3 + plen]
        if evt == EVT_CMD_COMPLETE:
            ncmd, op = struct.unpack("<BH", body[:3])
            if op != opcode:
                continue
            return (body[3] if len(body) > 3 else None, body[4:])
        if evt == EVT_CMD_STATUS:
            st, ncmd, op = struct.unpack("<BBH", body[:4])
            if op != opcode:
                continue
            return (st, b"")


def bit(buf, octet, b):
    return bool(octet < len(buf) and (buf[octet] >> b) & 1)


def probe(dev):
    out = {"hci": "hci%d" % dev}
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    try:
        s.bind((dev,))  # HCI_CHANNEL_RAW (shared, non-exclusive)
        raw = {}

        def q(op, params=b""):
            st, rp = hci_cmd(s, dev, op, params)
            raw["0x%04x" % op] = {"name": READ_ONLY_OPCODES[op], "status": st, "ret": rp.hex()}
            return st, rp

        st, rp = q(0x1001)
        if st == 0 and len(rp) >= 8:
            hv, hr, lv, mfr, ls = struct.unpack("<BHBHH", rp[:8])
            out["version"] = {"hci_version": hv, "hci_revision": hr, "lmp_version": lv,
                              "manufacturer_id": mfr, "lmp_subversion": ls,
                              "hci_revision_hex": "0x%04x" % hr, "lmp_subversion_hex": "0x%04x" % ls}
        st, rp = q(0x1009)
        if st == 0 and len(rp) >= 6:
            out["bd_addr"] = ":".join("%02X" % b for b in rp[:6][::-1])
        st, rp = q(0x1003)
        if st == 0:
            out["lmp_features_page0"] = rp[:8].hex()
        st, rp = q(0x1002)
        cmds = rp[:64] if st == 0 else b""
        out["supported_commands_hex"] = cmds.hex()
        out["commands"] = {}
        for name, opc, octet, b in CMDS:
            out["commands"][name] = {"opcode": "0x%04x" % opc, "octet": octet, "bit": b,
                                     "supported": bit(cmds, octet, b)}
        st, rp = q(0x2003)
        lef = rp[:8] if st == 0 else b""
        out["le_features_hex"] = lef.hex()
        out["le_features"] = {nm: bit(lef, o, b) for (o, b), nm in LE_FEATS.items()}
        out["le_features_set_unnamed"] = [
            "byte%d.bit%d" % (o, b) for o in range(len(lef)) for b in range(8)
            if (lef[o] >> b) & 1 and (o, b) not in LE_FEATS]
        st, rp = q(0x201C)
        out["le_states_hex"] = rp[:8].hex() if st == 0 else None
        st, rp = q(0x2002)
        if st == 0 and len(rp) >= 3:
            out["le_buffer_size_v1"] = {"acl_len": struct.unpack("<H", rp[:2])[0], "acl_pkts": rp[2]}
        st, rp = q(0x2007)
        if st == 0 and len(rp) >= 1:
            out["le_adv_channel_tx_power_dbm"] = struct.unpack("<b", rp[:1])[0]
        st, rp = q(0x203A)
        out["le_max_adv_data_len"] = struct.unpack("<H", rp[:2])[0] if st == 0 and len(rp) >= 2 else {"status": st}
        st, rp = q(0x203B)
        out["le_num_adv_sets"] = rp[0] if st == 0 and len(rp) >= 1 else {"status": st}
        st, rp = q(0x204B)
        if st == 0 and len(rp) >= 2:
            mn, mx = struct.unpack("<bb", rp[:2])
            out["le_tx_power_range_dbm"] = {"min": mn, "max": mx}
        else:
            out["le_tx_power_range_dbm"] = {"status": st}
        st, rp = q(0x202F)
        if st == 0 and len(rp) >= 8:
            a, b_, c, d = struct.unpack("<HHHH", rp[:8])
            out["le_max_data_length"] = {"max_tx_octets": a, "max_tx_time_us": b_,
                                         "max_rx_octets": c, "max_rx_time_us": d}
        else:
            out["le_max_data_length"] = {"status": st}
        st, rp = q(0x200F)
        out["le_filter_accept_list_size"] = rp[0] if st == 0 and rp else {"status": st}
        st, rp = q(0x202A)
        out["le_resolving_list_size"] = rp[0] if st == 0 and rp else {"status": st}
        out["raw_responses"] = raw
    finally:
        s.close()
    return out


if __name__ == "__main__":
    outpath = sys.argv[1] if len(sys.argv) > 1 else "bt_probe_raw.json"
    res = {}
    for dev in range(8):
        try:
            res["hci%d" % dev] = probe(dev)
        except Exception as e:
            res["hci%d" % dev] = {"error": repr(e)}
    json.dump(res, open(outpath, "w"), indent=1)
    print("wrote", outpath)
