#!/usr/bin/env python3
"""
btinsight.py - BluForge insight engine.

Turns a raw BluForge capture into structured findings rather than a packet
dump. Runs FULLY OFFLINE and deterministically: no network, no API key, no
model - safe for air-gapped customer labs, and reproducible (the same capture
always yields the same findings, which matters when results go in a report).

It emits clean JSON, so an optional natural-language layer (LLM) can be
bolted on later to narrate these findings without re-deriving them.

Three analysis areas:

  1. DEVICE INTELLIGENCE - who is actually in this RF environment: vendor
     (SIG company ID), advertised name, device class inferred from service
     UUIDs / appearance, and beacon-format decode (iBeacon, Eddystone,
     Microsoft Swift Pair).

  2. PRIVACY & SECURITY - the differentiating analysis. Classifies every
     address per Core Spec (public / static random / resolvable private /
     non-resolvable private), flags devices that are passively TRACKABLE
     because they never rotate their address, and correlates rotating
     private addresses that are probably the SAME physical device by
     fingerprinting the advertising payload's structure.

  3. RF & COEXISTENCE HEALTH - RSSI distribution and proximity, advertising
     interval regularity/jitter per device, airtime share of the noisiest
     talkers, PHY/PDU mix, and which Wi-Fi channels the BLE advertising
     channels overlap (ties into BluForge's DTM interference testing).

Usage:
  python3 btinsight.py capture.btsnoop           # human-readable report
  python3 btinsight.py capture.btsnoop --json    # machine/GUI/LLM-readable
"""
import sys, os, json, math, argparse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btlab import (parse_ext_adv_reports, parse_adv_reports, decode_ad,
                   COMPANY_IDS, SERVICE_UUID_NAMES)
from btle_pcap import read_btsnoop_hci_events, BTSNOOP_EPOCH_OFFSET_US

# ---------------------------------------------------------------- reference data
# Service UUIDs that strongly imply a device category.
UUID_DEVICE_HINT = {
    0x1812: "HID input device (keyboard/mouse/remote)",
    0x180D: "Heart-rate monitor / fitness",
    0x180F: "Battery-reporting peripheral",
    0x1808: "Glucose meter (medical)",
    0x1809: "Thermometer (medical)",
    0x110B: "Audio sink (headphones/speaker)",
    0x110E: "A/V remote control",
    0xFEAA: "Eddystone beacon",
    0xFCF1: "Google Fast Pair accessory",
    0xFE9F: "Google service",
    0xFDCD: "Qingping sensor",
    0x180A: "Device-information service",
}

# GAP Appearance (AD type 0x19) - top 10 bits are the category.
APPEARANCE_CATEGORY = {
    0: "Unknown", 1: "Phone", 2: "Computer", 3: "Watch", 4: "Clock",
    5: "Display", 6: "Remote control", 7: "Eye glasses", 8: "Tag",
    9: "Keyring", 10: "Media player", 11: "Barcode scanner", 12: "Thermometer",
    13: "Heart rate sensor", 14: "Blood pressure", 15: "HID",
    16: "Glucose meter", 17: "Running/walking sensor", 18: "Cycling",
    31: "Pulse oximeter", 32: "Weight scale", 33: "Personal mobility",
    34: "Continuous glucose monitor", 35: "Insulin pump", 36: "Medication delivery",
    49: "Outdoor sports", 81: "Earbuds", 82: "Headset",
}

ADDR_PUBLIC          = "public"
ADDR_STATIC_RANDOM   = "static-random"
ADDR_RPA             = "resolvable-private"
ADDR_NRPA            = "non-resolvable-private"

# BLE primary advertising channels -> centre frequency (MHz)
ADV_CHANNEL_FREQ = {37: 2402, 38: 2426, 39: 2480}

# One advertising event hits up to 3 channels within a few ms; reports arriving
# closer together than this belong to the SAME event, not a new interval.
ADV_EVENT_WINDOW_S = 0.010
# Below this many measured gaps an "interval" is statistically meaningless.
MIN_GAPS_FOR_INTERVAL = 3


def classify_address(addr, addr_type):
    """Per Core Spec Vol 6 Part B 1.3: a random address's top two bits of the
    most-significant octet identify its sub-type. This is what determines
    whether a device is passively trackable."""
    if addr_type != "random":
        return ADDR_PUBLIC
    try:
        msb = int(addr.split(":")[0], 16)
    except (ValueError, IndexError):
        return ADDR_PUBLIC
    top = (msb >> 6) & 0x03
    if top == 0b11: return ADDR_STATIC_RANDOM
    if top == 0b01: return ADDR_RPA
    if top == 0b00: return ADDR_NRPA
    return ADDR_STATIC_RANDOM


def decode_beacon(ad):
    """Identify well-known beacon payload formats carried in the AD data."""
    for t, hexval in ad.get("raw", []):
        try: val = bytes.fromhex(hexval)
        except ValueError: continue

        # iBeacon: Apple mfg data, subtype 0x02, length 0x15
        if t == 0xFF and len(val) >= 25 and val[0:2] == b"\x4c\x00" and val[2] == 0x02 and val[3] == 0x15:
            uuid = val[4:20].hex()
            major = int.from_bytes(val[20:22], "big")
            minor = int.from_bytes(val[22:24], "big")
            txp = val[24] - 256 if val[24] > 127 else val[24]
            return {"format": "iBeacon",
                    "uuid": f"{uuid[:8]}-{uuid[8:12]}-{uuid[12:16]}-{uuid[16:20]}-{uuid[20:]}",
                    "major": major, "minor": minor, "tx_power_1m": txp}

        # Eddystone: service data under UUID 0xFEAA, first byte = frame type
        if t == 0x16 and len(val) >= 3 and val[0:2] == b"\xaa\xfe":
            frame = {0x00: "UID", 0x10: "URL", 0x20: "TLM", 0x30: "EID"}.get(val[2])
            if frame:
                return {"format": "Eddystone", "frame_type": frame}

        # Microsoft Swift Pair / CDP beacon
        if t == 0xFF and len(val) >= 3 and val[0:2] == b"\x06\x00":
            return {"format": "Microsoft CDP / Swift Pair",
                    "note": "Windows proximity beacon - device name is intentionally not broadcast"}
    return None


def payload_fingerprint(ad):
    """Structural signature of an advertisement, deliberately EXCLUDING the
    volatile parts (rotating IDs, counters, RSSI). Two different addresses
    emitting the same structure are very likely the same physical device
    rotating its private address - which is how a passive observer defeats
    BLE address privacy, and therefore what we want to surface."""
    parts = []
    for t, hexval in sorted(ad.get("raw", [])):
        try: val = bytes.fromhex(hexval)
        except ValueError: continue
        if t == 0xFF and len(val) >= 2:
            parts.append(f"mfg:{val[0]:02x}{val[1]:02x}:len{len(val)}")
        elif t == 0x16 and len(val) >= 2:
            parts.append(f"svc:{val[0]:02x}{val[1]:02x}:len{len(val)}")
        elif t in (0x02, 0x03, 0x06, 0x07):
            parts.append(f"uuidlist:{t:02x}:{hexval}")
        elif t == 0x01:
            parts.append(f"flags:{hexval}")
        elif t == 0x19:
            parts.append(f"appearance:{hexval}")
        else:
            parts.append(f"t{t:02x}:len{len(val)}")
    return "|".join(parts)


def load_reports(path):
    """Read every LE advertising report out of a raw .btsnoop, with timestamps."""
    reports, reasm = [], {}
    for ts, code, params in read_btsnoop_hci_events(path):
        if code != 0x3E or not params:
            continue
        if params[0] == 0x0D:
            rs = parse_ext_adv_reports(params, reasm)
        elif params[0] == 0x02:
            rs = parse_adv_reports(params)
        else:
            continue
        t_unix = (ts - BTSNOOP_EPOCH_OFFSET_US) / 1_000_000.0
        for r in rs:
            r["_t"] = t_unix
            reports.append(r)
    return reports


def analyze(path):
    reports = load_reports(path)
    if not reports:
        # Distinguish "nothing was in the air" from "this is a Classic capture",
        # otherwise a BR/EDR capture looks like a broken BLE one. The insight
        # engine analyses BLE advertising only - Classic discovery data is a
        # different shape (Inquiry Results, no advertising PDUs) and is not
        # covered yet.
        classic = sum(1 for _ts, code, _p in read_btsnoop_hci_events(path)
                      if code in (0x02, 0x22, 0x2F))
        note = ("This is a Classic BR/EDR capture ("
                f"{classic} inquiry result(s)). The insight engine currently "
                "analyses BLE advertising only - open the .pcap in Wireshark "
                "for the Classic decode.") if classic else \
               "No BLE advertising reports in this capture."
        return {"capture": os.path.basename(path), "packets": 0, "devices": [],
                "classic_events": classic,
                "summary": {"note": note}}

    devs = defaultdict(lambda: {"packets": 0, "rssi": [], "times": [], "pdus": set(),
                                "phys": set(), "name": None, "mfg": None, "mfg_name": None,
                                "svc_uuids": set(), "appearance": None, "beacon": None,
                                "fingerprints": set(), "addr_type": None})
    t0 = min(r["_t"] for r in reports)

    for r in reports:
        d = devs[r["addr"]]
        ad = r.get("ad") or {}
        d["packets"] += 1
        d["addr_type"] = r.get("addr_type", d["addr_type"])
        if r.get("rssi") is not None: d["rssi"].append(r["rssi"])
        d["times"].append(r["_t"])
        d["pdus"].add(r.get("pdu", "?"))
        d["phys"].add(r.get("phy", "1M"))
        if ad.get("name"): d["name"] = ad["name"]
        if ad.get("mfg") is not None:
            d["mfg"] = ad["mfg"]; d["mfg_name"] = ad.get("mfg_name")
        if ad.get("svc_uuid") is not None: d["svc_uuids"].add(ad["svc_uuid"])
        for t, hexval in ad.get("raw", []):
            if t == 0x19 and len(hexval) >= 4:
                try: d["appearance"] = int.from_bytes(bytes.fromhex(hexval)[:2], "little")
                except ValueError: pass
        if d["beacon"] is None:
            b = decode_beacon(ad)
            if b: d["beacon"] = b
        fp = payload_fingerprint(ad)
        if fp: d["fingerprints"].add(fp)

    duration = max(r["_t"] for r in reports) - t0

    # ---- per-device records -------------------------------------------------
    out_devices = []
    for addr, d in devs.items():
        cls = classify_address(addr, d["addr_type"])
        rssi = d["rssi"]
        # Advertising interval. IMPORTANT: one advertising EVENT is transmitted
        # on up to three primary channels within a few ms, and because BluForge
        # deliberately disables duplicate-filtering (to catch everything), the
        # controller reports each of those separately. Treating consecutive
        # reports as the interval yields nonsense (e.g. "2 ms", below the spec
        # minimum). So cluster reports into events first, then measure between
        # events - and only report an interval when there are enough events for
        # the number to mean anything.
        ts = sorted(d["times"])
        events = []
        for t in ts:
            if not events or (t - events[-1]) > ADV_EVENT_WINDOW_S:
                events.append(t)
        gaps = [(b - a) * 1000.0 for a, b in zip(events, events[1:]) if 0 < (b - a) < 30]
        if len(gaps) < MIN_GAPS_FOR_INTERVAL:
            mean_gap = jitter = None
        else:
            mean_gap = sum(gaps) / len(gaps)
            jitter = math.sqrt(sum((g - mean_gap) ** 2 for g in gaps) / len(gaps))

        hints = [UUID_DEVICE_HINT[u] for u in d["svc_uuids"] if u in UUID_DEVICE_HINT]
        if d["appearance"] is not None:
            cat = APPEARANCE_CATEGORY.get(d["appearance"] >> 6)
            if cat: hints.append(f"appearance: {cat}")

        avg_rssi = round(sum(rssi) / len(rssi), 1) if rssi else None
        out_devices.append({
            "address": addr,
            "address_class": cls,
            "trackable": cls in (ADDR_PUBLIC, ADDR_STATIC_RANDOM),
            "name": d["name"],
            "vendor": d["mfg_name"],
            "company_id": f"0x{d['mfg']:04x}" if d["mfg"] is not None else None,
            "device_hints": sorted(set(hints)),
            "beacon": d["beacon"],
            "service_uuids": [f"0x{u:04x}" + (f" ({SERVICE_UUID_NAMES[u]})" if u in SERVICE_UUID_NAMES else "")
                              for u in sorted(d["svc_uuids"])],
            "packets": d["packets"],
            "rssi_avg": avg_rssi,
            "rssi_min": min(rssi) if rssi else None,
            "rssi_max": max(rssi) if rssi else None,
            "proximity": (None if avg_rssi is None else
                          "very close (<1m)" if avg_rssi > -50 else
                          "near (1-5m)"      if avg_rssi > -70 else
                          "mid (5-15m)"      if avg_rssi > -85 else "far (>15m)"),
            "adv_interval_ms": round(mean_gap, 1) if mean_gap else None,
            "adv_jitter_ms": round(jitter, 1) if jitter else None,
            "pdu_types": sorted(d["pdus"]),
            "phys": sorted(d["phys"]),
            "_fingerprints": d["fingerprints"],
        })
    out_devices.sort(key=lambda x: -(x["rssi_avg"] if x["rssi_avg"] is not None else -999))

    # ---- privacy correlation: rotating addresses that are one device --------
    by_fp = defaultdict(list)
    for dev in out_devices:
        if dev["address_class"] in (ADDR_RPA, ADDR_NRPA):
            for fp in dev["_fingerprints"]:
                by_fp[fp].append(dev["address"])
    # A device can emit more than one payload shape (e.g. an ADV_IND plus its
    # SCAN_RSP), which yields several overlapping fingerprint sets for the SAME
    # device. Merge any sets that share an address, otherwise one device gets
    # reported twice and the "distinct addresses collapsed" total is inflated.
    merged = []          # list of [set(addresses), set(signatures)]
    for fp, addrs in by_fp.items():
        uniq = set(addrs)
        if len(uniq) < 2:
            continue
        hit = [m for m in merged if m[0] & uniq]
        if not hit:
            merged.append([uniq, {fp}])
            continue
        keep = hit[0]
        keep[0] |= uniq
        keep[1].add(fp)
        for other in hit[1:]:                 # transitive: this set bridged two groups
            keep[0] |= other[0]
            keep[1] |= other[1]
            merged.remove(other)

    rotation_groups = [{
        "addresses": sorted(addrs),
        "count": len(addrs),
        "signature": sorted(sigs)[0][:120],
        "finding": (f"{len(addrs)} rotating private addresses share one advertising "
                    f"structure - almost certainly ONE physical device. Address "
                    f"privacy is defeated by payload fingerprinting."),
    } for addrs, sigs in merged]
    rotation_groups.sort(key=lambda g: -g["count"])

    for dev in out_devices:
        dev.pop("_fingerprints", None)

    trackable = [d for d in out_devices if d["trackable"]]
    named     = [d for d in out_devices if d["name"]]
    beacons   = [d for d in out_devices if d["beacon"]]

    cls_counts = defaultdict(int)
    for d in out_devices: cls_counts[d["address_class"]] += 1

    pdu_counts, phy_counts = defaultdict(int), defaultdict(int)
    for r in reports:
        pdu_counts[r.get("pdu", "?")] += 1
        phy_counts[r.get("phy", "1M")] += 1

    noisiest = sorted(out_devices, key=lambda d: -d["packets"])[:5]

    return {
        "capture": os.path.basename(path),
        "duration_s": round(duration, 1),
        "packets": len(reports),
        "device_count": len(out_devices),
        "packet_rate_pps": round(len(reports) / duration, 1) if duration > 0 else None,

        "device_intelligence": {
            "devices_named": len(named),
            "devices_with_vendor": len([d for d in out_devices if d["vendor"]]),
            "beacons_found": len(beacons),
            "vendor_breakdown": _counts(d["vendor"] for d in out_devices if d["vendor"]),
            "beacons": [{"address": d["address"], **d["beacon"]} for d in beacons],
        },

        "privacy_security": {
            "address_classes": dict(cls_counts),
            "trackable_devices": len(trackable),
            "trackable_pct": round(100.0 * len(trackable) / len(out_devices), 1) if out_devices else 0,
            "trackable_list": [{"address": d["address"], "class": d["address_class"],
                                "name": d["name"], "vendor": d["vendor"]} for d in trackable],
            "rotation_groups": rotation_groups,
            "findings": _privacy_findings(cls_counts, trackable, rotation_groups, out_devices),
        },

        "rf_coexistence": {
            "pdu_mix": dict(pdu_counts),
            "phy_mix": dict(phy_counts),
            "noisiest_devices": [{"address": d["address"], "name": d["name"],
                                  "packets": d["packets"],
                                  "airtime_share_pct": round(100.0 * d["packets"] / len(reports), 1),
                                  "adv_interval_ms": d["adv_interval_ms"]} for d in noisiest],
            "adv_channel_overlap": {
                "note": ("BLE advertises on 3 fixed channels; these are the 2.4 GHz "
                         "Wi-Fi channels they collide with - the same channels BluForge's "
                         "DTM interference mode targets."),
                "ch37_2402MHz": "overlaps Wi-Fi ch 1 (2412) lower skirt",
                "ch38_2426MHz": "sits inside Wi-Fi ch 4-6 (2427-2437)",
                "ch39_2480MHz": "overlaps Wi-Fi ch 13-14 upper skirt",
            },
            "findings": _rf_findings(out_devices, reports, duration),
        },

        "devices": out_devices,
    }


def _counts(it):
    c = defaultdict(int)
    for x in it: c[x] += 1
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))


def _privacy_findings(cls_counts, trackable, groups, devices):
    f = []
    n = len(devices)
    if trackable:
        pct = round(100.0 * len(trackable) / n, 1) if n else 0
        f.append(f"{len(trackable)} of {n} devices ({pct}%) are passively TRACKABLE - they use a "
                 f"public or static-random address that never rotates, so any observer can "
                 f"follow them over time.")
    pub = [d for d in trackable if d["address_class"] == ADDR_PUBLIC]
    if pub:
        named = [d["name"] or d["address"] for d in pub][:5]
        f.append(f"{len(pub)} device(s) advertise a PUBLIC (manufacturer-assigned, permanent) "
                 f"address: {', '.join(named)}. This is the weakest privacy posture.")
    if groups:
        tot = sum(g["count"] for g in groups)
        f.append(f"Address-rotation defeated for {len(groups)} device(s): {tot} distinct private "
                 f"addresses collapse into {len(groups)} real device(s) by payload fingerprinting. "
                 f"Rotating the address alone does not prevent tracking when the payload "
                 f"structure stays constant.")
    if cls_counts.get(ADDR_RPA):
        f.append(f"{cls_counts[ADDR_RPA]} device(s) correctly use Resolvable Private Addresses "
                 f"(good practice - rotates and is resolvable only by bonded peers).")
    if not f:
        f.append("No privacy concerns identified in this capture.")
    return f


def _rf_findings(devices, reports, duration):
    f = []
    if duration > 0:
        f.append(f"{len(reports)} advertising packets from {len(devices)} devices over "
                 f"{duration:.1f}s ({len(reports)/duration:.1f} pkt/s on the advertising channels).")
    fast = [d for d in devices if d["adv_interval_ms"] and d["adv_interval_ms"] < 100]
    if fast:
        top = sorted(fast, key=lambda d: d["adv_interval_ms"])[:3]
        f.append("Aggressive advertisers (<100 ms interval, high airtime cost): " +
                 ", ".join(f"{d['name'] or d['address']} @{d['adv_interval_ms']}ms" for d in top))
    jittery = [d for d in devices if d["adv_jitter_ms"] and d["adv_interval_ms"]
               and d["adv_jitter_ms"] > d["adv_interval_ms"] * 0.5 and d["packets"] > 10]
    if jittery:
        f.append(f"{len(jittery)} device(s) show irregular advertising timing (jitter > 50% of "
                 f"interval) - consistent with RF contention, collisions, or packet loss.")
    close = [d for d in devices if d["rssi_avg"] is not None and d["rssi_avg"] > -50]
    if close:
        f.append(f"{len(close)} device(s) are very close (RSSI > -50 dBm) and will contribute "
                 f"strong in-band energy to any coexistence measurement.")
    coded = [d for d in devices if "Coded" in (d["phys"] or [])]
    if coded:
        f.append(f"{len(coded)} device(s) use the LE Coded PHY (long-range mode) - these occupy "
                 f"the air far longer per packet than 1M/2M.")
    if not f:
        f.append("No RF anomalies identified.")
    return f


# ---------------------------------------------------------------- text report
def render(a):
    L = []
    P = L.append
    P("=" * 74)
    P(f" BluForge Insight Report - {a['capture']}")
    P("=" * 74)
    if not a.get("packets"):
        P(" No advertising reports in this capture.")
        return "\n".join(L)
    P(f" {a['packets']} packets | {a['device_count']} devices | {a['duration_s']}s "
      f"| {a['packet_rate_pps']} pkt/s")
    P("")

    di = a["device_intelligence"]
    P("-- DEVICE INTELLIGENCE " + "-" * 51)
    P(f"   named: {di['devices_named']}   vendor-identified: {di['devices_with_vendor']}"
      f"   beacons: {di['beacons_found']}")
    if di["vendor_breakdown"]:
        P("   vendors: " + ", ".join(f"{k} x{v}" for k, v in di["vendor_breakdown"].items()))
    for b in di["beacons"][:5]:
        P(f"   beacon {b['address']}: {b['format']}" +
          (f" uuid={b.get('uuid','')} {b.get('major','')}/{b.get('minor','')}" if b.get("uuid") else ""))
    P("")

    pv = a["privacy_security"]
    P("-- PRIVACY & SECURITY " + "-" * 53)
    P("   address classes: " + ", ".join(f"{k}={v}" for k, v in pv["address_classes"].items()))
    for line in pv["findings"]:
        P("   * " + _wrap(line, 68, "     "))
    for g in pv["rotation_groups"][:5]:
        P(f"   ! {g['count']} addresses = 1 device: {', '.join(g['addresses'][:4])}"
          + (" ..." if g["count"] > 4 else ""))
    P("")

    rf = a["rf_coexistence"]
    P("-- RF & COEXISTENCE HEALTH " + "-" * 48)
    P("   PDU mix: " + ", ".join(f"{k}={v}" for k, v in rf["pdu_mix"].items()))
    P("   PHY mix: " + ", ".join(f"{k}={v}" for k, v in rf["phy_mix"].items()))
    for line in rf["findings"]:
        P("   * " + _wrap(line, 68, "     "))
    P("   top talkers:")
    for d in rf["noisiest_devices"]:
        P(f"     {d['address']}  {d['packets']:5d} pkts  {d['airtime_share_pct']:5.1f}%  "
          f"{(d['name'] or '')[:24]}")
    P("")

    P("-- DEVICES " + "-" * 64)
    P(f"   {'RSSI':>5} {'ADDRESS':17} {'CLASS':20} {'VENDOR':12} NAME / HINT")
    for d in a["devices"][:30]:
        hint = d["name"] or (d["device_hints"][0] if d["device_hints"] else
                             (d["beacon"]["format"] if d["beacon"] else ""))
        r = f"{d['rssi_avg']:.0f}" if d["rssi_avg"] is not None else "--"
        P(f"   {r:>5} {d['address']:17} {d['address_class']:20} "
          f"{(d['vendor'] or '-'):12} {hint[:30]}")
    if len(a["devices"]) > 30:
        P(f"   ... and {len(a['devices']) - 30} more")
    return "\n".join(L)


def _wrap(s, width, indent):
    words, lines, cur = s.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur: lines.append(cur)
    return ("\n" + indent).join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="BluForge offline insight engine")
    ap.add_argument("capture", help="raw .btsnoop capture from BluForge")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a text report")
    args = ap.parse_args()
    if not os.path.exists(args.capture):
        print(f"no such capture: {args.capture}", file=sys.stderr); sys.exit(1)
    result = analyze(args.capture)
    print(json.dumps(result, indent=2) if args.json else render(result))
