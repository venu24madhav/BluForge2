"""Aggregates raw advertising reports into one record per device, so the sniffer
can hand the GUI a small, steady stream (changed devices, once a second)
instead of one event per over-the-air packet."""
import time

import ble_decode

LEGACY_PDU = {0: "ADV_IND", 1: "ADV_DIRECT_IND", 2: "ADV_SCAN_IND", 3: "ADV_NONCONN_IND", 4: "SCAN_RSP"}
# extended-report event_type values with the 'legacy PDU' bit (0x10) set
EXT_LEGACY_PDU = {0x13: "ADV_IND", 0x15: "ADV_DIRECT_IND", 0x12: "ADV_SCAN_IND",
                  0x10: "ADV_NONCONN_IND", 0x1B: "SCAN_RSP", 0x1A: "SCAN_RSP"}
MAX_DEVICES = 5000


def normalise(rep):
    """Legacy and extended reports -> one shape:
    (pdu name, connectable, scannable, directed, is_scan_rsp, addr_type name, phys, sid)."""
    if not rep.get("ext"):
        et = rep["event_type"]
        pdu = LEGACY_PDU.get(et, "0x%02x" % et)
        return (pdu, et in (0, 1), et in (0, 2), et == 1, et == 4,
                "random" if rep["addr_type"] else "public", [], None, False)
    et = rep["event_type"]
    if et & 0x10:
        pdu = EXT_LEGACY_PDU.get(et & 0x1F, "0x%02x" % (et & 0x1F))
    else:
        pdu = "AUX_SCAN_RSP" if et & 0x08 else "ADV_EXT_IND"
    at = rep["addr_type"]
    atype = "public" if at in (0, 2) else "random" if at in (1, 3) else "anonymous"
    phys = [ble_decode.PHY_NAMES.get(p) for p in (rep.get("primary_phy"), rep.get("secondary_phy")) if p in ble_decode.PHY_NAMES]
    return (pdu, bool(et & 0x01), bool(et & 0x02), bool(et & 0x04), bool(et & 0x08),
            atype, phys, rep.get("sid"), not (et & 0x10))


class Survey:
    def __init__(self, name_filter=None):
        self.devs = {}
        self.dirty = set()
        self.reports = 0
        self.evicted = 0
        self.t0 = time.time()
        self.last_report = None
        self.filter = name_filter.lower() if name_filter else None

    def add(self, rep):
        now = time.time()
        self.reports += 1
        self.last_report = now
        pdu, conn, scan, directed, is_rsp, atype, phys, sid, is_ext = normalise(rep)
        key = rep["addr"]
        d = self.devs.get(key)
        if d is None:
            if len(self.devs) >= MAX_DEVICES:
                stalest = min(self.devs, key=lambda k: self.devs[k]["last_seen"])
                del self.devs[stalest]
                self.dirty.discard(stalest)
                self.evicted += 1
            d = self.devs[key] = {
                "addr": key, "addr_type": atype, "addr_kind": ble_decode.addr_kind(key, atype),
                "first_seen": now, "last_seen": now, "count": 0, "rssi_sum": 0, "rssi_n": 0,
                "rssi": None, "rssi_min": None, "rssi_max": None, "pdus": {}, "connectable": False,
                "scannable": False, "directed": False, "ext": False, "phys": [], "sid": None,
                "name": None, "name_complete": None, "flags": [], "tx_power": None, "appearance": None,
                "services": [], "solicited": [], "service_data": [], "mfg": [], "beacon": None,
                "uri": None, "raw": {},
            }
        d["last_seen"] = now
        d["count"] += 1
        d["pdus"][pdu] = d["pdus"].get(pdu, 0) + 1
        r = rep.get("rssi")
        if r is not None:
            d["rssi"] = r
            d["rssi_sum"] += r
            d["rssi_n"] += 1
            d["rssi_min"] = r if d["rssi_min"] is None else min(d["rssi_min"], r)
            d["rssi_max"] = r if d["rssi_max"] is None else max(d["rssi_max"], r)
        d["connectable"] |= conn
        d["scannable"] |= scan
        d["directed"] |= directed
        d["ext"] |= is_ext
        for p in phys:
            if p not in d["phys"]:
                d["phys"].append(p)
        if sid not in (None, 0xFF):
            d["sid"] = sid
        if rep.get("tx_power") is not None:
            d["tx_power"] = rep["tx_power"]
        data = rep["data"]
        if data:
            d["raw"][pdu] = data.hex()
            self._merge(d, ble_decode.decode_adv(data))
        self.dirty.add(key)

    @staticmethod
    def _merge(d, ad):
        if ad["name"] and (not d["name"] or ad["name_complete"] or not d["name_complete"]):
            d["name"], d["name_complete"] = ad["name"], ad["name_complete"]
        for k in ("flags", "tx_power", "appearance", "uri", "beacon"):
            if ad[k]:
                d[k] = ad[k]
        have = {s["uuid"] for s in d["services"]}
        d["services"] += [s for s in ad["services"] if s["uuid"] not in have]
        have = {s["uuid"] for s in d["solicited"]}
        d["solicited"] += [s for s in ad["solicited"] if s["uuid"] not in have]
        for sd in ad["service_data"]:
            d["service_data"] = [x for x in d["service_data"] if x["uuid"] != sd["uuid"]] + [sd]
        for m in ad["mfg"]:
            d["mfg"] = [x for x in d["mfg"] if x["id"] != m["id"] or x.get("format") != m.get("format")] + [m]

    def matches(self, d):
        if not self.filter:
            return True
        return self.filter in (d["addr"] + " " + (d["name"] or "")).lower()

    def record(self, d):
        """JSON-able record for one device, with the derived fields filled in."""
        out = {"m": "dev"}      # first key: the server routes survey lines by this prefix
        out.update({k: v for k, v in d.items() if k not in ("rssi_sum", "rssi_n")})
        out["rssi_avg"] = round(d["rssi_sum"] / d["rssi_n"], 1) if d["rssi_n"] else None
        span = d["last_seen"] - d["first_seen"]
        out["rate_hz"] = round(d["count"] / span, 2) if span > 1 else None
        out["kind"] = ble_decode.classify({"beacon": d["beacon"], "mfg": d["mfg"],
                                           "appearance": d["appearance"], "services": d["services"]})
        out["company"] = next((m["company"] for m in d["mfg"] if m.get("company")), None)
        out["supports"] = ble_decode.supports(d)
        for k in ("first_seen", "last_seen"):
            out[k] = round(d[k], 3)
        return out

    def drain(self):
        """Records for devices changed since the last call (filter applied)."""
        keys, self.dirty = self.dirty, set()
        return [self.record(self.devs[k]) for k in keys if k in self.devs and self.matches(self.devs[k])]

    def stats(self):
        now = time.time()
        up = max(now - self.t0, 0.001)
        return {"m": "stats", "t": round(up, 1), "devices": len(self.devs), "reports": self.reports,
                "rate": round(self.reports / up, 1), "evicted": self.evicted,
                "idle_s": None if self.last_report is None else round(now - self.last_report, 1)}
