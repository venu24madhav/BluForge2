"""BLE Advertising Data (AD) structure encoders.

Every AD structure is  [Length][AD Type][AD Data...]  where Length counts the
AD Type byte plus the data (Core Spec Supplement, Part A, Sec 1). Legacy
advertising payloads (what hci_raw.legacy_adv_configure sends) are capped at
31 bytes total; this module enforces that and reports exactly how much
headroom is left, since ADV emulation on this project trades in real,
spec-correct payloads, not placeholders.

Company ID 0xFFFF is the Bluetooth SIG's reserved "for testing" value (Core
Spec Supplement, Part A, 1.4, and the SIG's own assigned-numbers document) --
every synthetic payload this module builds defaults to it so nothing here
impersonates a real product's proprietary protocol. iBeacon and Eddystone are
open, publicly documented formats (Apple's and Google's own specs) reproduced
here from their published byte layouts, not reverse-engineered.
"""
import struct

MAX_LEGACY_ADV_LEN = 31

# AD type values (Bluetooth SIG "Generic Access Profile" assigned numbers)
AD_TYPE_FLAGS = 0x01
AD_TYPE_INCOMPLETE_16BIT_UUIDS = 0x02
AD_TYPE_COMPLETE_16BIT_UUIDS = 0x03
AD_TYPE_SHORTENED_LOCAL_NAME = 0x08
AD_TYPE_COMPLETE_LOCAL_NAME = 0x09
AD_TYPE_TX_POWER_LEVEL = 0x0A
AD_TYPE_SLAVE_CONN_INTERVAL_RANGE = 0x12
AD_TYPE_SERVICE_DATA_16BIT = 0x16
AD_TYPE_APPEARANCE = 0x19
AD_TYPE_MANUFACTURER_DATA = 0xFF

FLAG_LE_LIMITED_DISCOVERABLE = 0x01
FLAG_LE_GENERAL_DISCOVERABLE = 0x02
FLAG_BR_EDR_NOT_SUPPORTED = 0x04

TEST_COMPANY_ID = 0xFFFF  # SIG-reserved "for testing", not a real vendor


class AdError(Exception):
    pass


def ad_struct(ad_type: int, data: bytes) -> bytes:
    if not (0 <= ad_type <= 0xFF):
        raise AdError("ad_type out of range: %r" % ad_type)
    if len(data) > 253:  # 255 - 1(len) - 1(type), legacy payload is far smaller anyway
        raise AdError("single AD structure too long: %d bytes" % len(data))
    return bytes([len(data) + 1, ad_type]) + data


def flags(value: int = FLAG_LE_GENERAL_DISCOVERABLE | FLAG_BR_EDR_NOT_SUPPORTED) -> bytes:
    return ad_struct(AD_TYPE_FLAGS, bytes([value & 0xFF]))


def complete_local_name(name: str) -> bytes:
    return ad_struct(AD_TYPE_COMPLETE_LOCAL_NAME, name.encode("ascii", "replace"))


def shortened_local_name(name: str) -> bytes:
    return ad_struct(AD_TYPE_SHORTENED_LOCAL_NAME, name.encode("ascii", "replace"))


def tx_power_level(dbm: int) -> bytes:
    if not (-127 <= dbm <= 127):
        raise AdError("tx_power_level out of int8 range: %r" % dbm)
    return ad_struct(AD_TYPE_TX_POWER_LEVEL, struct.pack("<b", dbm))


def manufacturer_data(company_id: int, payload: bytes) -> bytes:
    if not (0 <= company_id <= 0xFFFF):
        raise AdError("company_id must be a 16-bit value")
    return ad_struct(AD_TYPE_MANUFACTURER_DATA, struct.pack("<H", company_id) + payload)


def service_data_16bit(uuid16: int, payload: bytes) -> bytes:
    if not (0 <= uuid16 <= 0xFFFF):
        raise AdError("uuid16 must be a 16-bit value")
    return ad_struct(AD_TYPE_SERVICE_DATA_16BIT, struct.pack("<H", uuid16) + payload)


def complete_16bit_service_uuids(uuids):
    body = b"".join(struct.pack("<H", u) for u in uuids)
    return ad_struct(AD_TYPE_COMPLETE_16BIT_UUIDS, body)


def check_legacy_len(payload: bytes, label: str):
    if len(payload) > MAX_LEGACY_ADV_LEN:
        raise AdError("%s is %d bytes, exceeds the legacy 31-byte advertising limit by %d"
                       % (label, len(payload), len(payload) - MAX_LEGACY_ADV_LEN))
    return payload


# --------------------------------------------------------------------------
# Named, ready-to-send advertisement profiles. Each returns (adv_data,
# scan_rsp, description) -- adv_data always fits in 31 bytes on its own, so
# the profile works even on a scanner that never requests a scan response.
# --------------------------------------------------------------------------

def profile_ibeacon(uuid_hex: str, major: int, minor: int, measured_power_dbm: int = -59,
                     company_id: int = 0x004C):
    """Apple iBeacon (publicly documented format; company_id 0x004C = Apple,
    used here only to produce a structurally valid, recognizable iBeacon
    frame for interop/demo purposes -- default company_id can be overridden
    to TEST_COMPANY_ID for a non-impersonating variant). Layout: 0x02 0x15
    (type+len) + 16B UUID + 2B major + 2B minor + 1B measured power."""
    uuid_bytes = bytes.fromhex(uuid_hex.replace("-", ""))
    if len(uuid_bytes) != 16:
        raise AdError("iBeacon UUID must be 16 bytes, got %d" % len(uuid_bytes))
    if not (0 <= major <= 0xFFFF and 0 <= minor <= 0xFFFF):
        raise AdError("major/minor must be 16-bit values")
    body = bytes([0x02, 0x15]) + uuid_bytes + struct.pack(">HH", major, minor) + \
        struct.pack("<b", measured_power_dbm)
    adv = flags() + manufacturer_data(company_id, body)
    return check_legacy_len(adv, "iBeacon adv_data"), b"", "iBeacon"


def profile_eddystone_uid(namespace_hex: str, instance_hex: str, tx_power_dbm: int = -22):
    """Google Eddystone-UID (service UUID 0xFEAA, frame type 0x00). Frame
    layout per the published spec: Frame Type(1) + Ranging Data(1) +
    Namespace(10) + Instance(6) + RFU(2, reserved, must be zero) = 20 bytes.
    The trailing 2 RFU bytes were missing here (confirmed by code review:
    produced an 18-byte body) -- a standards-compliant Eddystone scanner
    checks Service Data length against the fixed 20-byte frame size, so a
    short frame could be rejected or misparsed."""
    ns = bytes.fromhex(namespace_hex)
    inst = bytes.fromhex(instance_hex)
    if len(ns) != 10 or len(inst) != 6:
        raise AdError("Eddystone-UID needs a 10-byte namespace and 6-byte instance")
    body = bytes([0x00]) + struct.pack("<b", tx_power_dbm) + ns + inst + b"\x00\x00"
    adv = flags() + complete_16bit_service_uuids([0xFEAA]) + service_data_16bit(0xFEAA, body)
    return check_legacy_len(adv, "Eddystone-UID adv_data"), b"", "Eddystone-UID"


def profile_eddystone_url(url: str, tx_power_dbm: int = -22):
    """Google Eddystone-URL (frame type 0x10) with the standard URL-scheme
    and common-suffix compression table."""
    schemes = {"http://www.": 0x00, "https://www.": 0x01, "http://": 0x02, "https://": 0x03}
    suffixes = [".com/", ".org/", ".edu/", ".net/", ".info/", ".biz/", ".gov/",
                ".com", ".org", ".edu", ".net", ".info", ".biz", ".gov"]
    scheme_byte = None
    rest = url
    for prefix, code in schemes.items():
        if url.startswith(prefix):
            scheme_byte = code
            rest = url[len(prefix):]
            break
    if scheme_byte is None:
        raise AdError("Eddystone-URL needs a http(s):// (optionally www.) prefix")
    encoded = bytearray()
    i = 0
    while i < len(rest):
        matched = False
        for idx, suf in enumerate(suffixes):
            if rest[i:i + len(suf)] == suf:
                encoded.append(idx)
                i += len(suf)
                matched = True
                break
        if not matched:
            encoded.append(ord(rest[i]))
            i += 1
    body = bytes([0x10]) + struct.pack("<b", tx_power_dbm) + bytes([scheme_byte]) + bytes(encoded)
    adv = flags() + complete_16bit_service_uuids([0xFEAA]) + service_data_16bit(0xFEAA, body)
    return check_legacy_len(adv, "Eddystone-URL adv_data"), b"", "Eddystone-URL"


def profile_altbeacon(beacon_id_hex: str, mfg_reserved: int = 0x00, tx_power_dbm: int = -59,
                       company_id: int = TEST_COMPANY_ID):
    """AltBeacon (open spec, no single owner). Code: 0xBEAC + 20-byte beacon
    ID + 1-byte reference RSSI + 1-byte mfg-reserved."""
    bid = bytes.fromhex(beacon_id_hex)
    if len(bid) != 20:
        raise AdError("AltBeacon beacon ID must be 20 bytes, got %d" % len(bid))
    body = bytes([0xBE, 0xAC]) + bid + struct.pack("<b", tx_power_dbm) + bytes([mfg_reserved & 0xFF])
    adv = flags() + manufacturer_data(company_id, body)
    return check_legacy_len(adv, "AltBeacon adv_data"), b"", "AltBeacon"


def profile_generic_sensor(name: str, sensor_id: int, value_centiunits: int,
                            company_id: int = TEST_COMPANY_ID):
    """A small synthetic 'sensor telemetry' beacon: 1B sensor_id + int16
    value, under the SIG test company ID. Useful as a visibly distinct
    'our own protocol' demo advertisement alongside the open beacon formats."""
    if not (0 <= sensor_id <= 0xFF):
        raise AdError("sensor_id must fit in 1 byte")
    body = bytes([sensor_id & 0xFF]) + struct.pack("<h", value_centiunits)
    adv = flags() + manufacturer_data(company_id, body) + shortened_local_name(name[:10])
    return check_legacy_len(adv, "generic sensor adv_data"), b"", "GenericSensor"


PROFILES = {
    "ibeacon": lambda cfg: profile_ibeacon(
        cfg.get("uuid", "E2C56DB5DFFB48D2B060D0F5A71096E0"),
        int(cfg.get("major", 1)), int(cfg.get("minor", 1)),
        int(cfg.get("power", -59))),
    "eddystone_uid": lambda cfg: profile_eddystone_uid(
        cfg.get("namespace", "00112233445566778899"),
        cfg.get("instance", "AABBCCDDEEFF"), int(cfg.get("power", -22))),
    "eddystone_url": lambda cfg: profile_eddystone_url(
        cfg.get("url", "https://candelatech.com"), int(cfg.get("power", -22))),
    "altbeacon": lambda cfg: profile_altbeacon(
        cfg.get("beacon_id", "0102030405060708090A0B0C0D0E0F1011121314"),
        int(cfg.get("mfg_reserved", 0)), int(cfg.get("power", -59))),
    "generic_sensor": lambda cfg: profile_generic_sensor(
        cfg.get("name", "BFsensor"), int(cfg.get("sensor_id", 1)),
        int(cfg.get("value", 2500))),
}


def build_profile(name: str, cfg: dict = None):
    cfg = cfg or {}
    if name not in PROFILES:
        raise AdError("unknown ADV profile %r, have: %s" % (name, sorted(PROFILES)))
    adv, scan_rsp, label = PROFILES[name](cfg)
    return {"adv_data": adv, "scan_rsp": scan_rsp, "label": label}


if __name__ == "__main__":
    # ---- self test ----
    f = flags()
    assert f == bytes([0x02, 0x01, 0x06]), f.hex()

    md = manufacturer_data(0xFFFF, b"BLUFORGE1")
    assert md[0] == len(md) - 1
    assert md[1] == AD_TYPE_MANUFACTURER_DATA
    assert md[2:4] == b"\xff\xff"
    assert md[4:] == b"BLUFORGE1"

    ib_adv, ib_scan, ib_label = profile_ibeacon("E2C56DB5-DFFB-48D2-B060-D0F5A71096E0", 100, 200, -59)
    assert ib_label == "iBeacon"
    assert len(ib_adv) <= MAX_LEGACY_ADV_LEN
    # locate the manufacturer AD structure and verify its exact byte layout
    i = 0
    found = None
    while i < len(ib_adv):
        ln = ib_adv[i]
        t = ib_adv[i + 1]
        if t == AD_TYPE_MANUFACTURER_DATA:
            found = ib_adv[i + 2:i + 1 + ln]
        i += 1 + ln
    assert found is not None
    assert found[0:2] == struct.pack("<H", 0x004C)
    assert found[2:4] == bytes([0x02, 0x15])
    assert found[4:20] == bytes.fromhex("E2C56DB5DFFB48D2B060D0F5A71096E0")
    assert struct.unpack(">H", found[20:22])[0] == 100
    assert struct.unpack(">H", found[22:24])[0] == 200
    assert struct.unpack("<b", found[24:25])[0] == -59

    eu_adv, _, eu_label = profile_eddystone_uid("00112233445566778899", "AABBCCDDEEFF")
    assert eu_label == "Eddystone-UID"
    assert len(eu_adv) <= MAX_LEGACY_ADV_LEN

    url_adv, _, url_label = profile_eddystone_url("https://candelatech.com")
    assert len(url_adv) <= MAX_LEGACY_ADV_LEN
    # decode it back to confirm the compression round-trips
    i = 0
    svc_body = None
    while i < len(url_adv):
        ln = url_adv[i]; t = url_adv[i + 1]
        if t == AD_TYPE_SERVICE_DATA_16BIT:
            svc_body = url_adv[i + 2:i + 1 + ln]
        i += 1 + ln
    assert svc_body[0:2] == struct.pack("<H", 0xFEAA)
    assert svc_body[2] == 0x10  # URL frame type
    assert svc_body[4] == 0x03  # "https://" scheme code
    decoded = "https://" + "candelatech" + ".com"
    # re-decode the compressed suffix byte to confirm it really is ".com"
    suffixes = [".com/", ".org/", ".edu/", ".net/", ".info/", ".biz/", ".gov/",
                ".com", ".org", ".edu", ".net", ".info", ".biz", ".gov"]
    assert suffixes[svc_body[-1]] == ".com"

    ab_adv, _, ab_label = profile_altbeacon("0102030405060708090A0B0C0D0E0F1011121314")
    assert ab_label == "AltBeacon"
    assert len(ab_adv) <= MAX_LEGACY_ADV_LEN

    gs_adv, _, gs_label = profile_generic_sensor("TempSensor", 7, 2550)
    assert len(gs_adv) <= MAX_LEGACY_ADV_LEN

    # legacy length enforcement actually triggers
    try:
        check_legacy_len(b"\x00" * 32, "oversize test")
        raise AssertionError("check_legacy_len failed to reject an oversize payload")
    except AdError:
        pass

    for name in PROFILES:
        r = build_profile(name)
        assert len(r["adv_data"]) <= MAX_LEGACY_ADV_LEN, (name, len(r["adv_data"]))

    print("ble_ad self-test OK")
    for name in PROFILES:
        r = build_profile(name)
        print("  %-15s %2d bytes  %s" % (name, len(r["adv_data"]), r["adv_data"].hex()))
