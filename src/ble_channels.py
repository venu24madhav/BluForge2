"""BLE 2.4 GHz channel math (Bluetooth Core Spec v5.4, Vol 6 Part B, Sec 1.4.1).

Two numbering schemes exist and this module is careful never to confuse them:

  RF channel "f"  0..39 (0x00..0x27) -> frequency = 2402 + 2*f MHz.
      This is the numbering the HCI commands actually use: Core Spec v5.4,
      Vol 4 Part E, Sec 7.8.28/7.8.29 define the LE Receiver/Transmitter Test
      "RX_Channel"/"TX_Channel" parameter directly as N = (F - 2402) / 2.
      All raw-HCI code in this project (hci_raw.py) works in THIS numbering.

  LL channel index "k"  0..39 -> the link-layer channel index from Vol 6
      Part B, Sec 1.4.1, used for data/advertising channel-map bitfields
      (channel selection algorithms #1/#2). It is NOT what the HCI test
      commands take, and this project's control code never needs it -- it
      is provided only for display/education, so a demo screen can show
      "RF channel N = LL index k" next to each other correctly.
      Per the spec table: LL 0..10 = 2404..2424 MHz, LL 11..36 = 2428..2478
      MHz (both data), and the three advertising channels get the LL
      numbers 37 (2402 MHz), 38 (2426 MHz), 39 (2480 MHz) -- deliberately
      out of frequency order. In RF-channel numbering those same three
      advertising frequencies are RF 0, RF 12 and RF 39 respectively.

This module builds the LL mapping programmatically from those fixed facts
(instead of a hand-typed table) and self-checks it: 37 data + 3 advertising
= 40 channels, and every RF->LL->RF round-trip must be the identity.
"""

ADVERTISING_RF_CHANNELS = (0, 12, 39)   # 2402, 2426, 2480 MHz -- fixed by spec
ADV_CHANNEL_MAP_BIT = {37: 0b001, 38: 0b010, 39: 0b100}  # LL idx -> CHM bit


def rf_to_freq_mhz(rf_channel: int) -> int:
    if not (0 <= rf_channel <= 39):
        raise ValueError("RF channel must be 0..39, got %r" % (rf_channel,))
    return 2402 + 2 * rf_channel


def freq_mhz_to_rf(freq_mhz: int) -> int:
    if (freq_mhz - 2402) % 2 or not (2402 <= freq_mhz <= 2480):
        raise ValueError("not a valid BLE channel centre frequency: %r" % (freq_mhz,))
    return (freq_mhz - 2402) // 2


def is_advertising_rf(rf_channel: int) -> bool:
    return rf_channel in ADVERTISING_RF_CHANNELS


def rf_to_ll_index(rf_channel: int) -> int:
    """Map an RF channel index (0..39, what HCI test commands use) to the
    link-layer channel index (37/38/39 = advertising, 0..36 = data)."""
    if rf_channel == 0:
        return 37
    if rf_channel == 12:
        return 38
    if rf_channel == 39:
        return 39
    # data channels: LL index increases by 1 per RF step, skipping the three
    # RF slots consumed by advertising (0, 12, 39) that sit below this one.
    skipped = sum(1 for a in ADVERTISING_RF_CHANNELS if a < rf_channel)
    return rf_channel - skipped


def ll_index_to_rf(ll_index: int) -> int:
    if ll_index in (37, 38, 39):
        return {37: 0, 38: 12, 39: 39}[ll_index]
    for rf in range(40):
        if rf not in ADVERTISING_RF_CHANNELS and rf_to_ll_index(rf) == ll_index:
            return rf
    raise ValueError("no RF channel maps to LL index %r" % (ll_index,))


def all_channels():
    """List of dicts describing all 40 BLE RF channels, sorted by RF index."""
    out = []
    for rf in range(40):
        ll = rf_to_ll_index(rf)
        out.append({
            "rf_channel": rf,
            "freq_mhz": rf_to_freq_mhz(rf),
            "ll_index": ll,
            "role": "advertising" if ll in (37, 38, 39) else "data",
        })
    return out


# ---------------------------------------------------------------- Wi-Fi overlap
def wifi_channel_center_mhz(wifi_ch: int) -> int:
    if wifi_ch == 14:
        return 2484
    if 1 <= wifi_ch <= 13:
        return 2412 + 5 * (wifi_ch - 1)
    raise ValueError("2.4 GHz Wi-Fi channel must be 1..14")


def ble_channels_overlapping_wifi(wifi_ch: int, width_mhz: int = 20):
    """BLE RF channels whose 2 MHz span falls within a Wi-Fi channel's
    occupied bandwidth (center +/- width/2), i.e. channels a Wi-Fi TX on
    `wifi_ch` would plausibly desensitize/collide with."""
    center = wifi_channel_center_mhz(wifi_ch)
    lo, hi = center - width_mhz / 2, center + width_mhz / 2
    return [c["rf_channel"] for c in all_channels() if lo <= c["freq_mhz"] <= hi]


# --------------------------------------------------------------------- timing
def tx_test_packet_duration_us(payload_len: int, phy: str = "1M") -> float:
    """Approx on-air duration of one LE Transmitter Test packet (Core spec
    Vol 6 Part F 4.1 formula, uncoded PHYs): whole packet = preamble + access
    address + PDU header(2) + payload + CRC(3), at 1 Mbps or 2 Mbps symbol
    rate. Coded PHY (S=8) is intentionally not modeled here (not used by this
    project's TX-test paths)."""
    bits = (1 + 4 + 2 + payload_len + 3) * 8  # preamble+AA+header+payload+CRC, bytes->bits
    if phy == "1M":
        return bits / 1.0
    if phy == "2M":
        return bits / 2.0
    raise ValueError("phy must be '1M' or '2M' for this helper")


if __name__ == "__main__":
    # ---- self test (run: python3 ble_channels.py) ----
    chans = all_channels()
    assert len(chans) == 40
    data = [c for c in chans if c["role"] == "data"]
    adv = [c for c in chans if c["role"] == "advertising"]
    assert len(data) == 37, len(data)
    assert len(adv) == 3, len(adv)
    assert sorted(c["rf_channel"] for c in adv) == [0, 12, 39]
    assert sorted(c["freq_mhz"] for c in adv) == [2402, 2426, 2480]
    assert rf_to_freq_mhz(0) == 2402 and rf_to_freq_mhz(39) == 2480
    assert freq_mhz_to_rf(2436) == (2436 - 2402) // 2  # 2436 MHz is a valid BLE centre (RF 17)
    try:
        freq_mhz_to_rf(2437)  # Wi-Fi ch6 centre, NOT a BLE channel centre (odd MHz) -> must reject
        raise AssertionError("freq_mhz_to_rf accepted a non-BLE frequency")
    except ValueError:
        pass
    for rf in range(40):
        ll = rf_to_ll_index(rf)
        assert ll_index_to_rf(ll) == rf, (rf, ll)
    # cross-check against bit flags
    for ll, bit in ADV_CHANNEL_MAP_BIT.items():
        assert bit in (0b001, 0b010, 0b100)
    # Wi-Fi overlap sanity: Wi-Fi ch6 (2437 MHz centre) spans 2427-2447 MHz:
    # BLE RF17 (2436 MHz, nearest BLE channel to the Wi-Fi centre) must fall
    # inside that span; BLE RF0 (2402 MHz) clearly must not.
    assert rf_to_freq_mhz(17) == 2436
    ov6 = ble_channels_overlapping_wifi(6)
    assert 17 in ov6 and 0 not in ov6
    ov1 = ble_channels_overlapping_wifi(1)
    assert freq_mhz_to_rf(2412) in ov1
    # duration formula sanity: longer payload -> longer airtime; 2M faster than 1M
    d255_1m = tx_test_packet_duration_us(255, "1M")
    d255_2m = tx_test_packet_duration_us(255, "2M")
    d0_1m = tx_test_packet_duration_us(0, "1M")
    assert d255_1m > d0_1m
    assert d255_2m < d255_1m
    print("ble_channels self-test OK")
    print()
    print("%-4s %-9s %-4s %-6s %s" % ("RF", "Freq MHz", "LL#", "Role", ""))
    for c in chans:
        print("%-4d %-9d %-4d %-6s" % (c["rf_channel"], c["freq_mhz"], c["ll_index"], c["role"]))
    print()
    for wc in (1, 6, 11):
        ov = ble_channels_overlapping_wifi(wc)
        print("Wi-Fi ch%-2d (%d MHz, 20MHz wide) overlaps BLE RF channels: %s"
              % (wc, wifi_channel_center_mhz(wc), ov))
