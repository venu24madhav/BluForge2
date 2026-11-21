"""BLE advertising-data decoder: turns raw AD structures into what a customer
wants to read -- vendor, advertised services/profiles, beacon formats, flags,
and a plain-language list of what the device says it supports.

Only what a device *advertises* can be decoded here. A device can support more
(its full GATT table) than it puts in an advertisement; that needs a connection.
Name tables hold only entries we are sure of (Bluetooth SIG assigned numbers).
"""
import struct

AD_TYPES = {
    0x01: "Flags", 0x02: "Incomplete UUID16 list", 0x03: "Complete UUID16 list",
    0x04: "Incomplete UUID32 list", 0x05: "Complete UUID32 list",
    0x06: "Incomplete UUID128 list", 0x07: "Complete UUID128 list",
    0x08: "Shortened Name", 0x09: "Complete Name", 0x0A: "Tx Power Level",
    0x0D: "Class of Device", 0x12: "Peripheral Connection Interval",
    0x14: "Solicited UUID16", 0x15: "Solicited UUID128", 0x16: "Service Data (UUID16)",
    0x17: "Public Target Address", 0x18: "Random Target Address", 0x19: "Appearance",
    0x1A: "Advertising Interval", 0x1B: "LE Device Address", 0x1C: "LE Role",
    0x1F: "Solicited UUID32", 0x20: "Service Data (UUID32)", 0x21: "Service Data (UUID128)",
    0x24: "URI", 0x27: "LE Supported Features", 0x30: "Broadcast Name",
    0x3D: "3D Information", 0xFF: "Manufacturer Specific Data",
}

SERVICES = {
    0x1800: "Generic Access", 0x1801: "Generic Attribute", 0x1802: "Immediate Alert",
    0x1803: "Link Loss", 0x1804: "Tx Power", 0x1805: "Current Time", 0x1806: "Reference Time Update",
    0x1807: "Next DST Change", 0x1808: "Glucose", 0x1809: "Health Thermometer",
    0x180A: "Device Information", 0x180D: "Heart Rate", 0x180E: "Phone Alert Status",
    0x180F: "Battery", 0x1810: "Blood Pressure", 0x1811: "Alert Notification",
    0x1812: "HID over GATT", 0x1813: "Scan Parameters", 0x1814: "Running Speed and Cadence",
    0x1815: "Automation IO", 0x1816: "Cycling Speed and Cadence", 0x1818: "Cycling Power",
    0x1819: "Location and Navigation", 0x181A: "Environmental Sensing", 0x181B: "Body Composition",
    0x181C: "User Data", 0x181D: "Weight Scale", 0x181E: "Bond Management",
    0x181F: "Continuous Glucose Monitoring", 0x1820: "Internet Protocol Support",
    0x1821: "Indoor Positioning", 0x1822: "Pulse Oximeter", 0x1823: "HTTP Proxy",
    0x1824: "Transport Discovery", 0x1825: "Object Transfer", 0x1826: "Fitness Machine",
    0x1827: "Mesh Provisioning", 0x1828: "Mesh Proxy", 0x1829: "Reconnection Configuration",
    0x183A: "Insulin Delivery", 0x183B: "Binary Sensor", 0x183C: "Emergency Configuration",
    0x183E: "Physical Activity Monitor", 0x1843: "Audio Input Control", 0x1844: "Volume Control",
    0x1845: "Volume Offset Control", 0x1846: "Coordinated Set Identification",
    0x1847: "Device Time", 0x1848: "Media Control", 0x1849: "Generic Media Control",
    0x184A: "Constant Tone Extension", 0x184B: "Telephone Bearer",
    0x184C: "Generic Telephone Bearer", 0x184D: "Microphone Control",
    0x184E: "Audio Stream Control", 0x184F: "Broadcast Audio Scan",
    0x1850: "Published Audio Capabilities", 0x1851: "Basic Audio Announcement",
    0x1852: "Broadcast Audio Announcement", 0x1853: "Common Audio", 0x1854: "Hearing Access",
    # member-assigned service UUIDs seen commonly in the field
    0xFD5A: "Samsung SmartTag", 0xFD6F: "Exposure Notification", 0xFD84: "Tile",
    0xFE03: "Amazon", 0xFE2C: "Google Fast Pair", 0xFE59: "Nordic Secure DFU",
    0xFE95: "Xiaomi", 0xFE9F: "Google", 0xFEAA: "Eddystone (Google)", 0xFEBE: "Bose",
    0xFEE0: "Anhui Huami", 0xFEE7: "Tencent", 0xFEED: "Tile", 0xFEF3: "Google",
}

# profile / capability families a service implies (for the "supports" summary)
LE_AUDIO = {0x184E, 0x184F, 0x1850, 0x1851, 0x1852, 0x1853, 0x1843, 0x1844, 0x1845,
            0x1846, 0x184D, 0x1854}
BASE_UUID_TAIL = "-0000-1000-8000-00805f9b34fb"

COMPANIES = {
    0x0000: "Ericsson Technology Licensing", 0x0001: "Nokia Mobile Phones", 0x0002: "Intel",
    0x0003: "IBM", 0x0004: "Toshiba", 0x0005: "3Com", 0x0006: "Microsoft", 0x0007: "Lucent",
    0x0008: "Motorola", 0x000A: "Qualcomm (CSR)", 0x000D: "Texas Instruments", 0x000F: "Broadcom",
    0x001D: "Qualcomm", 0x0030: "STMicroelectronics", 0x0046: "MediaTek", 0x004C: "Apple",
    0x0059: "Nordic Semiconductor", 0x005D: "Realtek", 0x0075: "Samsung Electronics",
    0x0078: "Nike", 0x0087: "Garmin", 0x009E: "Bose", 0x00C4: "LG Electronics",
    0x00D2: "Dialog Semiconductor", 0x00E0: "Google", 0x012D: "Sony", 0x0131: "Cypress",
    0x0157: "Anhui Huami (Zepp/Amazfit)", 0x0171: "Amazon", 0x02E5: "Espressif",
    0x038F: "Xiaomi", 0x0499: "Ruuvi Innovations",
}

APPEARANCE = {
    0: "Unknown", 1: "Phone", 2: "Computer", 3: "Watch", 4: "Clock", 5: "Display",
    6: "Remote Control", 7: "Eye-glasses", 8: "Tag", 9: "Keyring", 10: "Media Player",
    11: "Barcode Scanner", 12: "Thermometer", 13: "Heart Rate Sensor", 14: "Blood Pressure",
    15: "HID (keyboard/mouse/etc.)", 16: "Glucose Meter", 17: "Running/Walking Sensor",
    18: "Cycling Sensor", 49: "Pulse Oximeter", 50: "Weight Scale", 51: "Personal Mobility",
    81: "Outdoor Sports Activity",
}

APPLE_TYPES = {
    0x02: "iBeacon", 0x05: "AirDrop", 0x07: "Proximity Pairing (AirPods/Beats)",
    0x08: "Hey Siri", 0x09: "AirPlay Target", 0x0A: "AirPlay Source", 0x0B: "Magic Switch",
    0x0C: "Handoff", 0x0D: "Tethering Target", 0x0E: "Tethering Source",
    0x0F: "Nearby Action", 0x10: "Nearby Info", 0x12: "Find My network",
}

PHY_NAMES = {1: "1M", 2: "2M", 3: "Coded"}

_URL_SCHEME = ["http://www.", "https://www.", "http://", "https://"]
_URL_ENC = [".com/", ".org/", ".edu/", ".net/", ".info/", ".biz/", ".gov/",
            ".com", ".org", ".edu", ".net", ".info", ".biz", ".gov"]


def uuid_str(raw):
    """raw = little-endian bytes of a 16/32/128-bit UUID -> (canonical str, short int or None)"""
    b = bytes(reversed(raw))
    if len(raw) == 2:
        v = int.from_bytes(raw, "little")
        return "0x%04X" % v, v
    if len(raw) == 4:
        v = int.from_bytes(raw, "little")
        return "0x%08X" % v, (v if v <= 0xFFFF else None)
    h = b.hex()
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32]), None


def service_entry(raw):
    s, short = uuid_str(raw)
    return {"uuid": s, "name": SERVICES.get(short) if short is not None else None}


def addr_kind(addr, addr_type):
    """public / random-static / random-resolvable (RPA) / random-non-resolvable / random"""
    if addr_type == "public":
        return "public"
    top = int(addr.split(":")[0], 16) >> 6
    return {3: "random-static", 1: "random-resolvable (RPA)", 0: "random-non-resolvable"}.get(top, "random")


def parse_ad(data):
    """-> list of (type, value-bytes); tolerates truncated/garbage tails."""
    out, i = [], 0
    while i < len(data):
        ln = data[i]
        if ln == 0 or i + 1 + ln > len(data):
            break
        out.append((data[i + 1], bytes(data[i + 2:i + 1 + ln])))
        i += 1 + ln
    return out


def decode_flags(v):
    f = v[0] if v else 0
    out = []
    if f & 0x01: out.append("LE Limited Discoverable")
    if f & 0x02: out.append("LE General Discoverable")
    if f & 0x04: out.append("BR/EDR not supported")
    if f & 0x08: out.append("LE+BR/EDR simultaneous (controller)")
    if f & 0x10: out.append("LE+BR/EDR simultaneous (host)")
    return out


def decode_eddystone(v):
    if not v:
        return None
    t = v[0]
    if t == 0x00 and len(v) >= 18:
        return {"type": "Eddystone-UID", "tx_power": struct.unpack("b", v[1:2])[0],
                "namespace": v[2:12].hex(), "instance": v[12:18].hex()}
    if t == 0x10 and len(v) >= 3:
        url = _URL_SCHEME[v[2]] if v[2] < 4 else ""
        for c in v[3:]:
            url += _URL_ENC[c] if c < len(_URL_ENC) else chr(c) if 32 <= c < 127 else ""
        return {"type": "Eddystone-URL", "tx_power": struct.unpack("b", v[1:2])[0], "url": url}
    if t == 0x20 and len(v) >= 14:
        return {"type": "Eddystone-TLM", "battery_mv": int.from_bytes(v[2:4], "big"),
                "temp_c": round(int.from_bytes(v[4:6], "big", signed=True) / 256.0, 2),
                "adv_count": int.from_bytes(v[6:10], "big"),
                "uptime_s": int.from_bytes(v[10:14], "big") / 10.0}
    if t == 0x30:
        return {"type": "Eddystone-EID"}
    return {"type": "Eddystone (0x%02x)" % t}


def decode_mfg(v):
    """Manufacturer Specific Data -> dict with company, raw data, and any known sub-format."""
    if len(v) < 2:
        return None
    cid = int.from_bytes(v[:2], "little")
    d = {"id": cid, "company": COMPANIES.get(cid), "data": v[2:].hex()}
    body = v[2:]
    if cid == 0x004C and body:
        t = body[0]
        d["format"] = APPLE_TYPES.get(t, "Continuity 0x%02x" % t)
        if t == 0x02 and len(body) >= 23 and body[1] == 0x15:
            u = body[2:18].hex()
            d["beacon"] = {"type": "iBeacon",
                           "uuid": "%s-%s-%s-%s-%s" % (u[:8], u[8:12], u[12:16], u[16:20], u[20:]),
                           "major": int.from_bytes(body[18:20], "big"),
                           "minor": int.from_bytes(body[20:22], "big"),
                           "tx_power_1m": struct.unpack("b", body[22:23])[0]}
    elif cid == 0x0006 and body:
        d["format"] = {0x01: "Microsoft CDP", 0x03: "Microsoft Swift Pair"}.get(body[0])
    return d


def decode_adv(data):
    """Decode one advertising/scan-response payload. Returns a dict of everything found."""
    r = {"name": None, "name_complete": None, "flags": [], "tx_power": None,
         "appearance": None, "services": [], "solicited": [], "service_data": [],
         "mfg": [], "beacon": None, "uri": None, "other": [], "interval_ms": None}
    seen_services = set()

    def add_services(raw, step):
        for k in range(0, len(raw) - step + 1, step):
            e = service_entry(raw[k:k + step])
            if e["uuid"] not in seen_services:
                seen_services.add(e["uuid"])
                r["services"].append(e)

    for t, v in parse_ad(data):
        if t in (0x08, 0x09):
            r["name"] = v.decode("utf-8", "replace")
            r["name_complete"] = (t == 0x09)
        elif t == 0x01:
            r["flags"] = decode_flags(v)
        elif t in (0x02, 0x03):
            add_services(v, 2)
        elif t in (0x04, 0x05):
            add_services(v, 4)
        elif t in (0x06, 0x07):
            add_services(v, 16)
        elif t == 0x0A and v:
            r["tx_power"] = struct.unpack("b", v[:1])[0]
        elif t == 0x19 and len(v) >= 2:
            ap = int.from_bytes(v[:2], "little")
            r["appearance"] = APPEARANCE.get(ap >> 6, "Category %d" % (ap >> 6))
        elif t == 0x1A and len(v) >= 2:
            r["interval_ms"] = round(int.from_bytes(v[:2], "little") * 0.625, 2)
        elif t in (0x14, 0x15, 0x1F):
            step = {0x14: 2, 0x1F: 4, 0x15: 16}[t]
            r["solicited"] += [service_entry(v[k:k + step]) for k in range(0, len(v) - step + 1, step)]
        elif t in (0x16, 0x20, 0x21):
            step = {0x16: 2, 0x20: 4, 0x21: 16}[t]
            if len(v) >= step:
                ent = service_entry(v[:step])
                ent["data"] = v[step:].hex()
                if t == 0x16 and v[:2] == b"\xaa\xfe":
                    ed = decode_eddystone(v[2:])
                    if ed:
                        ent["decoded"] = ed
                        r["beacon"] = ed
                r["service_data"].append(ent)
        elif t == 0xFF:
            m = decode_mfg(v)
            if m:
                r["mfg"].append(m)
                if m.get("beacon"):
                    r["beacon"] = m["beacon"]
        elif t == 0x24:
            r["uri"] = v.decode("utf-8", "replace")
        else:
            r["other"].append({"type": t, "name": AD_TYPES.get(t, "0x%02x" % t), "data": v.hex()})
    return r


def classify(ad):
    """Short human label for what the device is, from the strongest evidence in the advert."""
    if ad.get("beacon"):
        return ad["beacon"]["type"]
    for m in ad.get("mfg", []):
        if m.get("format") and m["format"] not in ("Microsoft CDP",):
            return "%s %s" % (m.get("company") or "0x%04x" % m["id"], m["format"])
    if ad.get("appearance") and ad["appearance"] != "Unknown":
        return ad["appearance"]
    named = [s["name"] for s in ad.get("services", []) if s.get("name")]
    return named[0] if named else ""


def supports(dev):
    """Plain-language 'what this device says it supports', from accumulated advert data.
    dev: the aggregated device record built by the sniffer."""
    out = []
    pdus = dev.get("pdus", {})
    if dev.get("connectable"):
        out.append("Accepts connections (can be interrogated for its GATT services)")
    elif pdus:
        out.append("Broadcast only (not connectable)")
    if dev.get("scannable"):
        out.append("Scannable (provides scan-response data)")
    if dev.get("ext"):
        out.append("Bluetooth 5 extended advertising")
    phys = dev.get("phys") or []
    if "Coded" in phys:
        out.append("LE Coded PHY (long range)")
    if "2M" in phys:
        out.append("LE 2M PHY")
    fl = dev.get("flags") or []
    if "BR/EDR not supported" in fl:
        out.append("LE-only device")
    elif any("simultaneous" in f for f in fl):
        out.append("Dual-mode (LE + BR/EDR Classic)")
    sv = dev.get("services") or []
    shorts = set()
    for s in sv:
        try:
            if s["uuid"].startswith("0x"):
                shorts.add(int(s["uuid"], 16))
        except ValueError:
            pass
    if shorts & LE_AUDIO:
        out.append("LE Audio")
    if shorts & {0x1827, 0x1828}:
        out.append("Bluetooth Mesh")
    if 0x1812 in shorts:
        out.append("HID over GATT (keyboard/mouse/remote)")
    if 0xFD6F in shorts:
        out.append("Exposure Notification")
    if 0xFE2C in shorts:
        out.append("Google Fast Pair")
    for s in sv:
        if s.get("name"):
            out.append("Service: %s (%s)" % (s["name"], s["uuid"]))
        else:
            out.append("Service: %s" % s["uuid"])
    if dev.get("beacon"):
        out.append("Beacon: %s" % dev["beacon"]["type"])
    for m in dev.get("mfg") or []:
        if m.get("format"):
            out.append("%s: %s" % (m.get("company") or "0x%04x" % m["id"], m["format"]))
    if dev.get("appearance"):
        out.append("Appearance: %s" % dev["appearance"])
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


# ============================================================ GATT / SDP interrogation helpers
CHARACTERISTICS = {
    0x2A00: "Device Name", 0x2A01: "Appearance", 0x2A02: "Peripheral Privacy Flag",
    0x2A03: "Reconnection Address", 0x2A04: "Peripheral Preferred Connection Parameters",
    0x2A05: "Service Changed", 0x2A06: "Alert Level", 0x2A07: "Tx Power Level",
    0x2A19: "Battery Level", 0x2A1C: "Temperature Measurement", 0x2A23: "System ID",
    0x2A24: "Model Number", 0x2A25: "Serial Number", 0x2A26: "Firmware Revision",
    0x2A27: "Hardware Revision", 0x2A28: "Software Revision", 0x2A29: "Manufacturer Name",
    0x2A2A: "IEEE 11073-20601 Regulatory Certification", 0x2A2B: "Current Time",
    0x2A31: "Scan Refresh", 0x2A35: "Blood Pressure Measurement", 0x2A37: "Heart Rate Measurement",
    0x2A38: "Body Sensor Location", 0x2A39: "Heart Rate Control Point", 0x2A4A: "HID Information",
    0x2A4B: "Report Map", 0x2A4C: "HID Control Point", 0x2A4D: "Report", 0x2A4E: "Protocol Mode",
    0x2A4F: "Scan Interval Window", 0x2A50: "PnP ID", 0x2A5B: "CSC Measurement",
    0x2A5C: "CSC Feature", 0x2A63: "Cycling Power Measurement", 0x2A6E: "Temperature",
    0x2A6F: "Humidity", 0x2A9D: "Weight Measurement", 0x2AA6: "Central Address Resolution",
    0x2AC9: "Resolvable Private Address Only", 0x2B29: "Client Supported Features",
    0x2B2A: "Database Hash", 0x2B3A: "Server Supported Features",
}
DESCRIPTORS = {
    0x2900: "Characteristic Extended Properties", 0x2901: "Characteristic User Description",
    0x2902: "Client Characteristic Configuration", 0x2903: "Server Characteristic Configuration",
    0x2904: "Characteristic Presentation Format", 0x2906: "Valid Range",
}
_STRING_CHARS = {0x2A00, 0x2A24, 0x2A25, 0x2A26, 0x2A27, 0x2A28, 0x2A29}

CLASSIC_PROFILES = {
    0x1101: "Serial Port (SPP)", 0x1105: "Object Push (OPP)", 0x1106: "File Transfer (FTP)",
    0x1108: "Headset (HSP)", 0x110A: "A2DP Source", 0x110B: "A2DP Sink", 0x110C: "AVRCP Target",
    0x110D: "A2DP (Advanced Audio)", 0x110E: "AVRCP Controller", 0x110F: "AVRCP",
    0x1112: "Headset AG (HSP)", 0x1115: "PAN User", 0x1116: "PAN NAP", 0x1117: "PAN GN",
    0x111E: "Handsfree (HFP)", 0x111F: "Handsfree AG (HFP)", 0x112D: "SIM Access (SAP)",
    0x112F: "Phonebook Access Server (PBAP)", 0x1130: "Phonebook Access Client (PBAP)",
    0x1132: "Message Access Server (MAP)", 0x1133: "Message Notification Server (MAP)",
    0x1134: "Message Access Profile (MAP)", 0x1124: "HID", 0x1200: "PnP Information",
    0x1203: "Generic Audio", 0x1800: "Generic Access", 0x1801: "Generic Attribute",
}


def _printable(b):
    return bool(b) and all(32 <= c < 127 for c in b)


def decode_char_value(short, b):
    """Human reading of a characteristic value we understand; None if we don't."""
    try:
        if short in _STRING_CHARS:
            return b.decode("utf-8", "replace").rstrip("\x00")
        if short == 0x2A19 and len(b) >= 1:
            return "%d %%" % b[0]
        if short == 0x2A01 and len(b) >= 2:
            ap = int.from_bytes(b[:2], "little")
            return APPEARANCE.get(ap >> 6, "category %d" % (ap >> 6)) + " (0x%04x)" % ap
        if short == 0x2A07 and len(b) >= 1:
            return "%d dBm" % struct.unpack("b", b[:1])[0]
        if short == 0x2A04 and len(b) >= 8:
            mn, mx, lat, to = struct.unpack("<HHHH", b[:8])
            return "conn interval %.2f-%.2f ms, latency %d, supervision timeout %d ms" % (mn * 1.25, mx * 1.25, lat, to * 10)
        if short == 0x2A50 and len(b) >= 7:
            src, vid, pid, ver = struct.unpack("<BHHH", b[:7])
            who = COMPANIES.get(vid) if src == 1 else None
            return "vendor id source %s, vendor 0x%04x%s, product 0x%04x, version %d.%d.%d" % (
                {1: "Bluetooth SIG", 2: "USB-IF"}.get(src, src), vid, " (%s)" % who if who else "", pid,
                ver >> 8, (ver >> 4) & 0xF, ver & 0xF)
        if short == 0x2A23 and len(b) == 8:
            return b.hex()
        if short == 0x2A4A and len(b) >= 4:
            return "HID %x.%02x, country %d, flags 0x%02x" % (b[1], b[0], b[2], b[3])
        if short == 0x2A02 and len(b) >= 1:
            return "privacy enabled" if b[0] else "privacy disabled"
    except Exception:
        return None
    return None


def value_text(short, b):
    """-> (hex, decoded-or-ascii-or-None)"""
    d = decode_char_value(short, b) if short is not None else None
    if d is None and _printable(b):
        d = b.decode("ascii")
    return b.hex(), d


def parse_sdp_records(text):
    """Parse `sdptool records` output -> [{name, handle, classes[], l2cap_psm, rfcomm_channel, profiles[]}]."""
    import re
    recs = []
    for block in re.split(r"\n\s*\n", text):
        if "Service RecHandle" not in block and "Service Name" not in block:
            continue
        r = {"name": None, "handle": None, "classes": [], "l2cap_psm": None, "rfcomm_channel": None, "profiles": []}
        section, last_proto, last_prof = None, None, None
        for ln in block.splitlines():
            if not ln.strip():
                continue
            if not ln.startswith((" ", "\t")):
                m = re.match(r"Service Name:\s*(.*)", ln)
                if m:
                    r["name"] = m.group(1).strip()
                m = re.match(r"Service RecHandle:\s*(\S+)", ln)
                if m:
                    r["handle"] = m.group(1)
                section = ln.rstrip(":").strip() if ln.endswith(":") else None
                continue
            s = ln.strip()
            m = re.match(r'"?([^"(]*?)"?\s*\((0x[0-9a-fA-F]+)\)', s)
            if section == "Service Class ID List" and m:
                r["classes"].append("%s (%s)" % (m.group(1).strip() or "?", m.group(2)))
            elif section == "Protocol Descriptor List" and m:
                last_proto = m.group(1).strip()
            elif section == "Profile Descriptor List" and m:
                last_prof = {"profile": "%s (%s)" % (m.group(1).strip() or "?", m.group(2)), "version": None}
                r["profiles"].append(last_prof)
            else:
                m2 = re.match(r"Channel:\s*(\d+)", s)
                if m2 and last_proto == "RFCOMM":
                    r["rfcomm_channel"] = int(m2.group(1))
                m2 = re.match(r"PSM:\s*(0x[0-9a-fA-F]+|\d+)", s)
                if m2 and last_proto == "L2CAP":
                    r["l2cap_psm"] = m2.group(1)
                m2 = re.match(r"Version:\s*(0x[0-9a-fA-F]+)", s)
                if m2 and last_prof:
                    last_prof["version"] = m2.group(1)
        recs.append(r)
    return recs
