#!/usr/bin/env python3
"""
bluforge_cli.py - BLE advertisement + 2.4GHz interference engine for the
Intel BE200 (this unit: ct523c-ccb8, 8x BE200 Bluetooth controllers hci0-7).

Each invocation owns exactly ONE controller (--dev N, default 0) for its
whole lifetime via an exclusive HCI_CHANNEL_USER session, and is meant to be
run as its own process (not imported as a library for concurrency) so that a
process manager (server.py) can run several of these at once, one per
controller, and stop any one of them independently with SIGINT/SIGTERM
without risking the others -- this is a deliberate choice after an earlier
in-process-threads design on this project caused 3 controllers to wedge
simultaneously when one hung script was killed with SIGKILL (see
docs/INSTALL_LOG.md 2026-10-05 ~18:52). A single job dying here only ever
touches its own controller.

Subcommands:
  adv        emit ADV_IND / ADV_DIRECT_IND (hi/lo duty) / ADV_SCAN_IND / ADV_NONCONN_IND,
             optionally carrying one of the ble_ad.py beacon profiles (iBeacon/Eddystone/...)
  interfere  generate 2.4GHz interference via LE Transmitter Test (DTM):
             single channel, sweep/hop a list, full-band, or map to a Wi-Fi channel
  sniff      passive/active BLE advertising scan, human table or --json per report
  caps       print capabilities + parameter ranges as JSON (GUI-ready)

Run as root (raw HCI needs CAP_NET_ADMIN).
"""
import argparse
import json
import signal
import struct
import sys
import time

import ble_ad
import ble_channels
from ble_survey import Survey
from pcap_writer import PcapWriter
from hci_raw import (
    HciSocket, HciError, PAYLOAD_TYPES, detect_hci_indices,
    ADV_TYPE_IND, ADV_TYPE_DIRECT_IND_HIGH, ADV_TYPE_SCAN_IND,
    ADV_TYPE_NONCONN_IND, ADV_TYPE_DIRECT_IND_LOW, DIRECTED_ADV_TYPES,
)

C = dict(red="\033[31m", grn="\033[32m", yel="\033[33m", cyn="\033[36m", dim="\033[2m", rst="\033[0m")


def log(m):
    print("%s[*]%s %s" % (C['cyn'], C['rst'], m), flush=True)


def ok(m):
    print("%s[+]%s %s" % (C['grn'], C['rst'], m), flush=True)


def warn(m):
    print("%s[!]%s %s" % (C['yel'], C['rst'], m), file=sys.stderr, flush=True)


def die(m):
    print("%s[x]%s %s" % (C['red'], C['rst'], m), file=sys.stderr, flush=True)
    sys.exit(1)


class RadioGuard:
    """Gives one claimed radio back to BlueZ exactly once, and says honestly whether that worked.

    The stop handlers are installed BEFORE the radio is claimed (see install()): a Stop that arrives
    while HciSocket.open() is still powering the controller off and binding used to hit the default
    handler, kill the process mid-handoff and strand the radio. close() is also safe on a radio that
    was never fully claimed - it just asks BlueZ to power the controller back on."""

    def __init__(self, hci, dev, disable=None):
        self.hci, self.dev, self.disable = hci, dev, disable
        self.closing = False
        self.result = None            # (ok, message) from the restore once it has run

    def close(self):
        if self.closing:
            return self.result
        self.closing = True
        if self.disable:
            try:
                self.disable()
            except Exception:
                pass
        try:
            self.result = self.hci.close()
        except Exception as e:        # never let a failed restore hide the real outcome
            self.result = (False, "restore raised %s: %s" % (type(e).__name__, e))
        return self.result

    def restored(self):
        return bool(self.result and self.result[0])

    def report(self, what):
        """Print the final line: '<what>, hciN restored' only if the restore really succeeded."""
        if self.restored():
            ok("%s, hci%d restored" % (what, self.dev))
        else:
            detail = self.result[1] if self.result else "restore did not run"
            warn("%s, but hci%d was NOT confirmed restored to BlueZ (%s). Recover it with: "
                 "sudo btmgmt --index %d power on  (or restart bluetooth.service)" % (what, self.dev, detail, self.dev))

    def install(self, on_stop):
        """Install SIGINT/SIGTERM handlers. A second signal while the first is still restoring the radio
        is ignored, so a SIGTERM that follows the server's SIGINT cannot abort the restore half way."""
        def handler(*_):
            if self.closing:
                return
            on_stop()
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def claim(self):
        """hci.open(), with a clean error instead of a traceback."""
        try:
            self.hci.open()
        except HciError as e:
            die(str(e))


# ---------------------------------------------------------------- adv command
ADV_TYPES = {
    "ind": (ADV_TYPE_IND, "ADV_IND (connectable, scannable, undirected)", True, False),
    "direct": (ADV_TYPE_DIRECT_IND_HIGH, "ADV_DIRECT_IND (connectable, directed, high duty)", False, True),
    "direct-l": (ADV_TYPE_DIRECT_IND_LOW, "ADV_DIRECT_IND (connectable, directed, low duty)", False, True),
    "scan": (ADV_TYPE_SCAN_IND, "ADV_SCAN_IND (scannable, undirected)", True, False),
    "nonconn": (ADV_TYPE_NONCONN_IND, "ADV_NONCONN_IND (non-connectable beacon)", False, False),
}

ADV_FREQ = {37: 2402, 38: 2426, 39: 2480}


def build_advdata(name, use_flags, flags_val, raw_hex, profile, profile_cfg):
    """Resolve the AdvData for a non-directed PDU from exactly one source,
    in order of precedence: --data (raw hex) > --profile (ble_ad beacon) >
    name/flags. Returns (adv_data, scan_rsp_from_profile_or_empty)."""
    if raw_hex:
        return bytes.fromhex(raw_hex.replace(":", "").replace(" ", "")), b""
    if profile:
        built = ble_ad.build_profile(profile, profile_cfg or {})
        return built["adv_data"], built["scan_rsp"]
    out = b""
    if use_flags:
        out += ble_ad.flags(flags_val)
    if name:
        out += ble_ad.complete_local_name(name)
    return out, b""


# What the BE200 controller really radiates, measured over the air with a second radio (RSSI vs
# commanded power): it follows the command ~1 dB per dB up to about +5 dBm and clamps at +8 dBm; asking for
# more (up to the spec's +20) changes nothing. Advertising reports the power the controller selected;
# DTM (LE Transmitter Test) cannot report it, so a request above this ceiling gets a warning.
BE200_MAX_RADIATED_DBM = 8


def parse_power(raw, what="--power", dtm=False):
    """'max'/'min'/dBm string -> dBm int (DTM keeps the spec's 0x7F/0x7E sentinels). None = firmware default."""
    if raw is None or str(raw).strip() == "":
        return None
    pl = str(raw).strip().lower()
    try:
        if dtm:
            power = 0x7F if pl in ("max", "hi", "high") else 0x7E if pl in ("min", "lo", "low") else int(pl)
            if power in (0x7E, 0x7F):
                return power
        else:
            power = 20 if pl in ("max", "hi", "high") else -127 if pl in ("min", "lo", "low") else int(pl)
    except ValueError:
        die("%s must be -127..20 (dBm), or max/min, got %r" % (what, raw))
    if not -127 <= power <= 20:
        die("%s must be -127..20 (dBm), or max/min, got %r" % (what, raw))
    return power


# Per Core Spec, ADV_DIRECT_IND high duty cycle MUST auto-stop (the
# controller disables advertising on its own) after this window if no
# connection is made -- unlike every other legacy PDU type this tool
# supports, which keep advertising until explicitly disabled.
DIRECT_HIGH_DUTY_TIMEOUT_S = 1.28


def cmd_adv(a):
    if a.type not in ADV_TYPES:
        die("unknown adv type %r" % a.type)
    adv_type, desc, scannable, directed = ADV_TYPES[a.type]
    if directed and not a.peer:
        die("%s requires --peer <addr>" % a.type)

    chmap = 0
    bad = []
    for ch in a.channels.split(","):
        bit = {"37": 1, "38": 2, "39": 4}.get(ch.strip())
        if bit is None:
            bad.append(ch.strip() or "(empty)")
        else:
            chmap |= bit
    if bad:                         # checked before the radio is touched; used to be ignored, then crashed later
        die("advertising channels must be from 37,38,39 - got: %s" % ", ".join(bad))
    if chmap == 0:
        die("need at least one adv channel of 37,38,39")
    if not 20 <= a.interval <= 10240:
        die("--interval must be 20..10240 ms, got %s" % a.interval)
    if a.duration < 0:
        die("--duration must be >= 0 (0 = until stopped), got %s" % a.duration)
    tx_power = parse_power(getattr(a, "power", None))

    profile_cfg = {}
    if a.profile_cfg:
        for kv in a.profile_cfg.split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                profile_cfg[k.strip()] = v.strip()

    # Runs before hci.open() claims the radio, so a malformed --data/
    # --profile-cfg value here cannot leak a controller -- but it was still
    # reaching the operator as a raw Python traceback instead of this tool's
    # own clean error path (confirmed by code review). ble_ad.AdError is a
    # plain Exception subclass, not ValueError, so it needs its own catch.
    try:
        ad, profile_scan_rsp = (b"", b"") if directed else build_advdata(
            a.name, not a.no_flags, int(a.flags, 16), a.data, a.profile, profile_cfg)
        scan_rsp = b""
        if scannable:
            if a.scan_resp:
                scan_rsp = bytes.fromhex(a.scan_resp.replace(":", "").replace(" ", ""))
            elif profile_scan_rsp:
                scan_rsp = profile_scan_rsp
    except (ValueError, ble_ad.AdError, struct.error, TypeError) as e:
        die("bad --data/--flags/--profile-cfg: %s" % e)

    hci = HciSocket(a.dev, label="hci%d" % a.dev)
    guard = RadioGuard(hci, a.dev, disable=lambda: hci.legacy_adv_enable(False))
    safe_close = guard.close

    def cleanup(*_):
        safe_close()
        guard.report("advertising stopped")
        sys.exit(0 if guard.restored() else 3)

    guard.install(cleanup)          # before the radio is claimed
    guard.claim()

    try:
        print()
        log("PDU type      : %s%s%s  (Advertising_Type=0x%02x)" % (C['grn'], desc, C['rst'], adv_type))
        log("Interval      : %sms" % a.interval)
        freqs = " ".join("%s=%dMHz" % (c, ADV_FREQ[int(c)]) for c in a.channels.split(","))
        log("Channels      : %s  (map=0x%02x)  %s" % (a.channels, chmap, freqs))
        if a.peer:
            log("Peer          : %s (%s)" % (a.peer, a.peer_type))
        if ad:
            log("AdvData       : %s  (%dB)%s" % (ad.hex(), len(ad),
                "  [profile=%s]" % a.profile if a.profile else ""))
        if scan_rsp:
            log("ScanResp      : %s  (%dB)" % (scan_rsp.hex(), len(scan_rsp)))

        try:
            selected = hci.legacy_adv_configure(
                ad, adv_type=adv_type, channel_map=chmap, interval_ms=a.interval,
                scan_rsp=scan_rsp, peer_addr=a.peer,
                peer_addr_type=(0x01 if a.peer_type == "random" else 0x00),
                tx_power_dbm=tx_power)
            hci.legacy_adv_enable(True)
        except (HciError, ValueError) as e:
            safe_close()
            die(str(e))

        if tx_power is None:
            log("TX power      : firmware default (not reported by the controller)")
        elif selected is None or selected == 127:
            log("TX power      : requested %+d dBm, controller did not report the power it applied" % tx_power)
        elif selected != tx_power:
            log("TX power      : requested %+d dBm -> %sapplied %+d dBm%s (controller limit)" % (
                tx_power, C['grn'], selected, C['rst']))
        else:
            log("TX power      : %s%+d dBm applied%s" % (C['grn'], selected, C['rst']))
        print()
        ok("%sAdvertising ACTIVE:%s %s" % (C['grn'], C['rst'], desc))

        if adv_type == ADV_TYPE_DIRECT_IND_HIGH:
            # The radio itself stops transmitting after ~1.28s regardless of
            # what we do -- match the reported job state to reality instead
            # of claiming ACTIVE forever (confirmed by code review: nothing
            # previously reflected this, so the GUI/CLI kept showing a job
            # as running long after the controller silently stopped).
            wait_s = min(a.duration, DIRECT_HIGH_DUTY_TIMEOUT_S) if a.duration > 0 else DIRECT_HIGH_DUTY_TIMEOUT_S
            log("high duty cycle ADV_DIRECT_IND auto-stops after ~%.2fs per spec" % DIRECT_HIGH_DUTY_TIMEOUT_S)
            time.sleep(wait_s)
            cleanup()
        elif a.duration > 0:
            log("running for %ss (Ctrl-C to stop early)" % a.duration)
            time.sleep(a.duration)
            cleanup()
        else:
            log("advertising until Ctrl-C ...")
            while True:
                time.sleep(3600)
    except SystemExit:
        raise
    except BaseException:
        # Any exception not already handled above -- a bug we didn't
        # anticipate, a future HCI quirk, anything -- must still release the
        # controller back to BlueZ before the process dies (confirmed by
        # code review as the root cause behind several other findings: an
        # uncaught exception here previously skipped hci.close() entirely).
        safe_close()
        raise


# ---------------------------------------------------------------- interfere command
def wifi_dtm_channels(wifi_ch, bw):
    chans = ble_channels.ble_channels_overlapping_wifi(wifi_ch, width_mhz=bw)
    fc = ble_channels.wifi_channel_center_mhz(wifi_ch)
    return chans, fc


def _check_channels(chans, label):
    """'single' mode already validated 0<=n<=39 before this function existed;
    'sweep' and 'hop' did not (confirmed by code review: an out-of-range
    channel reached hci_raw.tx_test_start() mid-sweep, which raises a plain
    ValueError that tx_with_retries() doesn't catch, crashing the job after
    hci.open() already claimed the radio). Validate here, at the CLI
    boundary, before the radio is ever touched."""
    bad = [c for c in chans if not (0 <= c <= 39)]
    if bad:
        die("%s: channel(s) %s out of range 0..39" % (label, bad))
    if not chans:
        die("%s: resolved to zero channels" % label)
    return chans


def resolve_channels(a):
    m = a.mode
    if m == "single":
        n = int(a.arg)
        if not (0 <= n <= 39):
            die("channel must be 0..39")
        return [n], "BLE ch %d = %dMHz" % (n, ble_channels.rf_to_freq_mhz(n))
    if m == "sweep":
        lo, hi = (int(x) for x in a.arg.split("-"))
        if lo > hi:
            die("sweep range must be lo-hi with lo<=hi, got %d-%d" % (lo, hi))
        return _check_channels(list(range(lo, hi + 1)), "sweep"), "BLE ch %d..%d" % (lo, hi)
    if m == "hop":
        lst = [int(x) for x in a.arg.split(",")]
        return _check_channels(lst, "hop"), "BLE ch %s" % lst
    if m == "band":
        return list(range(40)), "FULL 2.4GHz band ch0..39 (2402..2480MHz)"
    if m == "wifi":
        ch = int(a.arg)
        if not (1 <= ch <= 13):
            die("Wi-Fi channel must be 1..13 (2.4GHz)")
        if a.bw not in (20, 40):
            die("--bw must be 20 or 40")
        chans, fc = wifi_dtm_channels(ch, a.bw)
        return _check_channels(chans, "wifi"), "Wi-Fi ch %d center %dMHz, %dMHz band" % (ch, fc, a.bw)
    die("unknown interfere mode %r" % m)


def cmd_interfere(a):
    if a.mode == "adv-flood":
        if a.map:
            # --map means "show the plan, transmit nothing" in every mode
            print()
            log("Plan          : %sadv-flood: ADV_NONCONN_IND every %sms on channel(s) %s%s" % (
                C['grn'], a.interval, a.adv_channels, C['rst']))
            log("Duration      : %s" % ("until Ctrl-C" if a.duration == 0 else "%ss" % a.duration))
            ok("map only (no transmission)")
            return
        ns = argparse.Namespace(
            dev=a.dev, type="nonconn", interval=a.interval, channels=a.adv_channels,
            peer=None, peer_type="public", name="BLUFORGE-INTERFERER", flags="06",
            no_flags=False, data=None, scan_resp=None, profile=None, profile_cfg=None,
            duration=a.duration, power=a.power)
        return cmd_adv(ns)

    plc = PAYLOAD_TYPES.get(a.payload)
    if plc is None:
        die("unknown payload %r, have: %s" % (a.payload, sorted(PAYLOAD_TYPES)))
    if a.phy not in ("1M", "2M"):
        die("phy must be 1M or 2M for DTM on this controller")
    if not (0 <= a.len <= 255):
        die("--len must be 0..255, got %r" % a.len)
    if a.dwell <= 0:
        die("--dwell must be > 0ms, got %r" % a.dwell)
    power = parse_power(a.power, dtm=True)
    if power is not None and power not in (0x7E, 0x7F) and power > BE200_MAX_RADIATED_DBM:
        warn("requested %+d dBm: the BE200 radiates at most about +%d dBm, so this is the same as max" % (
            power, BE200_MAX_RADIATED_DBM))

    chans, desc = resolve_channels(a)
    print()
    log("Plan          : %s%s%s" % (C['grn'], desc, C['rst']))
    log("DTM channels  : %s" % chans)
    print("%s               %s%s" % (C['dim'],
          " ".join("%d=%dMHz" % (n, ble_channels.rf_to_freq_mhz(n)) for n in chans), C['rst']))
    log("Payload       : %s (0x%02x), len=%dB, PHY=%s" % (a.payload, plc, a.len, a.phy))
    log("TX power      : %s" % (
        "requested %s (DTM cannot report the power the controller applied)" % a.power
        if a.power is not None else "firmware default"))
    if len(chans) > 1:
        log("Dwell         : %sms/channel" % a.dwell)
    log("Duration      : %s" % ("until Ctrl-C" if a.duration == 0 else "%ss" % a.duration))
    if a.map:
        ok("map only (no transmission)")
        return

    hci = HciSocket(a.dev, label="hci%d" % a.dev)
    state = {"hops": 0}
    guard = RadioGuard(hci, a.dev, disable=lambda: hci.tx_test_end())
    safe_close = guard.close

    def cleanup(*_):
        safe_close()
        guard.report("interference stopped (%d channel-dwells)" % state["hops"])
        sys.exit(0 if guard.restored() else 3)

    guard.install(cleanup)          # before the radio is claimed
    guard.claim()

    def tx_once(n):
        hci.tx_test_start(rf_channel=n, payload_len=a.len, payload_type=plc, phy=a.phy, power_level=power)

    def tx_with_retries(n, retries=3, raise_on_fail=False):
        """A single bad channel/transient firmware hiccup must not take down
        an entire multi-channel sweep -- but silently 'succeeding' without
        ever actually transmitting would be worse (a job that looks ACTIVE
        in the GUI while radiating nothing). So: retry briefly, and on a
        multi-channel sweep just skip the channel for this pass (loud
        warning, keep going); on a single fixed channel there is nothing
        else to fall back to, so raise after retries exhaust -- the job
        should visibly stop rather than claim to be jamming a channel it
        isn't. Catches ValueError too (e.g. a bad parameter that somehow
        slipped past upfront validation), not just HciError -- confirmed by
        code review as a real gap: a ValueError previously bypassed this
        retry/skip logic entirely and crashed the job immediately."""
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                tx_once(n)
                return True
            except (HciError, ValueError) as e:
                last_err = e
                warn("TX on channel %d failed (attempt %d/%d): %s" % (n, attempt, retries, e))
                time.sleep(0.2)
        warn("channel %d: giving up after %d attempts" % (n, retries))
        if raise_on_fail:
            raise last_err
        return False

    try:
        ok("%sInterference ACTIVE%s - transmitting on hci%d" % (C['grn'], C['rst'], a.dev))
        start = time.time()
        dwell_s = a.dwell / 1000.0

        if len(chans) == 1:
            tx_with_retries(chans[0], raise_on_fail=True)
            state["hops"] = 1
            if a.duration > 0:
                time.sleep(a.duration)
                cleanup()
            while True:
                time.sleep(3600)

        while True:
            for n in chans:
                tx_with_retries(n, raise_on_fail=False)  # one bad channel skips, sweep continues
                state["hops"] += 1
                time.sleep(dwell_s)
                if a.duration > 0 and (time.time() - start) >= a.duration:
                    cleanup()
    except SystemExit:
        raise
    except BaseException:
        # Any exception not already handled above must still release the
        # controller back to BlueZ before the process dies (confirmed by
        # code review: this was the root cause making the TX-power bug, an
        # out-of-range channel, or any future firmware quirk leave the radio
        # stuck instead of just failing cleanly).
        safe_close()
        raise


# ---------------------------------------------------------------- sniff command
ADV_REPORT_PDU = {0: "ADV_IND", 1: "ADV_DIRECT_IND", 2: "ADV_SCAN_IND",
                   3: "ADV_NONCONN_IND", 4: "SCAN_RSP"}
AD_TYPE_NAMES = {0x01: "Flags", 0x02: "IncompleteUUID16", 0x03: "CompleteUUID16",
                 0x08: "ShortName", 0x09: "CompleteName", 0x0A: "TxPower",
                 0x16: "ServiceData", 0x19: "Appearance", 0xFF: "ManufacturerData"}


def decode_ad(data):
    out = {"raw": []}
    i = 0
    while i < len(data):
        ln = data[i]
        if ln == 0 or i + 1 + ln > len(data):
            break
        t = data[i + 1]
        val = data[i + 2:i + 1 + ln]
        out["raw"].append((t, val.hex()))
        if t in (0x08, 0x09):
            try:
                out["name"] = val.decode("utf-8", "replace")
            except Exception:
                pass
        elif t == 0xFF and len(val) >= 2:
            out["mfg"] = int.from_bytes(val[:2], "little")
        elif t == 0x16 and len(val) >= 2:
            out["svc_uuid"] = int.from_bytes(val[:2], "little")
        i += 1 + ln
    return out


def cmd_sniff(a):
    if a.window > a.interval:
        die("--window must be <= --interval")
    pcap = None
    if a.pcap:
        if a.pcap_max_mb < 0:
            die("--pcap-max-mb must be 0 (no limit) or more")
        try:
            pcap = PcapWriter(a.pcap, max_bytes=a.pcap_max_mb * 1024 * 1024)
        except OSError as e:
            die("cannot create the capture file %s: %s" % (a.pcap, e))
    hci = HciSocket(a.dev, label="hci%d" % a.dev)
    hci.tap = pcap.tap if pcap is not None else None       # every HCI packet of this session, for Wireshark
    seen = {}
    survey_ref = [None]

    def stats_obj():
        st = survey_ref[0].stats()
        if pcap is not None:
            st.update(pcap_packets=pcap.packets, pcap_bytes=pcap.bytes, pcap_full=pcap.full)
        return st

    def _scans_off():
        for off in ((lambda: hci.ext_scan_enable(False)), (lambda: hci.scan_enable(False))):
            try:
                off()
            except Exception:
                pass

    guard = RadioGuard(hci, a.dev, disable=_scans_off)
    safe_close = guard.close

    def cleanup(*_):
        if a.survey and a.json and survey_ref[0] is not None and not guard.closing:
            try:
                for rec in survey_ref[0].drain():
                    print(json.dumps(rec, separators=(",", ":")), flush=True)
                print(json.dumps(stats_obj(), separators=(",", ":")), flush=True)
            except Exception:
                pass
        safe_close()
        if pcap is not None:                    # after safe_close(): the scan-off commands are part of the capture too
            pcap.close()
            if not a.json:
                log("capture saved: %s (%d packets, %d bytes)" % (a.pcap, pcap.packets, pcap.bytes))
        if not a.json:
            guard.report("sniff stopped, %d unique device(s) seen" % len(seen))
        elif not guard.restored():
            warn("hci%d was NOT confirmed restored to BlueZ (%s)" % (a.dev, (guard.result or (0, "?"))[1]))
        sys.exit(0 if guard.restored() else 3)

    guard.install(cleanup)          # before the radio is claimed
    guard.claim()

    try:
        # Confirmed by code review: unlike cmd_adv/cmd_interfere, these two
        # calls had NO exception handling at all -- any HciError/ValueError
        # (e.g. the window>interval case now rejected above, or any future
        # firmware rejection) previously crashed the job immediately after
        # hci.open() claimed the radio, with no cleanup.
        ext_on = False
        if a.survey and a.ext != "off":
            try:
                hci.ext_scan_configure(interval_ms=a.interval, window_ms=a.window,
                                       active=a.active, coded=a.coded)
                hci.ext_scan_enable(True, filter_dup=False)
                ext_on = True
            except HciError as e:
                if a.ext == "on":
                    raise
                log("extended scanning unavailable (%s) - falling back to legacy scan" % e)
        if not ext_on:
            hci.scan_configure(interval_ms=a.interval, window_ms=a.window, active=a.active)
            hci.scan_enable(True, filter_dup=False if a.survey else a.dedup)

        if pcap is not None:
            log("Recording every advertising report to %s%s" % (
                a.pcap, "" if a.pcap_max_mb == 0 else " (stops at %d MB)" % a.pcap_max_mb))
        if not a.json:
            print()
            log("Sniffing BLE advertising (%s), hci%d" % ("active" if a.active else "passive", a.dev))
            log("Filter: %s   Dedup: %s   Duration: %s" % (
                a.filter or "none", a.dedup, "until Ctrl-C" if a.duration == 0 else "%ss" % a.duration))
            print("%s%-8s %5s %-17s %-15s %-15s NAME/DATA%s" % (
                C['dim'], "TIME", "RSSI", "ADDRESS", "TYPE", "PDU", C['rst']))

        start = time.time()

        survey = Survey(a.filter) if a.survey else None
        survey_ref[0] = survey
        last_emit = [time.time()]
        warned = [False]

        def emit_survey():
            for rec in survey.drain():
                print(json.dumps(rec, separators=(",", ":")), flush=True)
            print(json.dumps(stats_obj(), separators=(",", ":")), flush=True)
            last_emit[0] = time.time()

        pcap_full_warned = [False]

        def on_report(rep):
            if survey is not None:
                try:
                    survey.add(rep)
                except Exception as e:      # one malformed report must never end the job
                    print(json.dumps({"m": "warn", "msg": "bad report from %s: %s" % (rep.get("addr"), e)}, separators=(",", ":")), flush=True)
                return
            if a.duration > 0 and time.time() - start >= a.duration:
                cleanup()
            ad = decode_ad(rep["data"])
            name = ad.get("name", "")
            if a.filter:
                hay = (rep["addr"] + " " + name).lower()
                if a.filter.lower() not in hay:
                    return
            seen.setdefault(rep["addr"], 0)
            seen[rep["addr"]] += 1
            pdu = ADV_REPORT_PDU.get(rep["event_type"], "0x%02x" % rep["event_type"])
            if a.json:
                print(json.dumps({"t": round(time.time() - start, 3), "addr": rep["addr"],
                                   "addr_type": "random" if rep["addr_type"] else "public",
                                   "rssi": rep["rssi"], "pdu": pdu, "ad": ad,
                                   "adv_hex": rep["data"].hex()}), flush=True)
            else:
                extra = name
                if not extra and ad.get("mfg") is not None:
                    extra = "mfg=0x%04x" % ad["mfg"]
                elif not extra and ad.get("svc_uuid") is not None:
                    extra = "svc=0x%04x" % ad["svc_uuid"]
                ts = "%7.2f" % (time.time() - start)
                print("%s %5d %-17s %-15s %-15s %s" % (
                    ts, rep["rssi"], rep["addr"], "random" if rep["addr_type"] else "public", pdu, extra))

        while True:
            if a.duration > 0 and time.time() - start >= a.duration:
                cleanup()
            hci.read_events(0.5, on_adv_report=on_report)
            if hci.dead:
                raise HciError("hci%d socket closed unexpectedly (controller removed or reset?)" % a.dev)
            if pcap is not None and pcap.full and not pcap_full_warned[0]:
                pcap_full_warned[0] = True
                msg = "capture file reached %d MB - recording stopped, scanning continues" % a.pcap_max_mb
                if a.json:
                    print(json.dumps({"m": "warn", "msg": msg}, separators=(",", ":")), flush=True)
                else:
                    warn(msg)
            if survey is not None:
                if time.time() - last_emit[0] >= 1.0:
                    emit_survey()
                if not warned[0] and survey.reports == 0 and time.time() - start > 20:
                    warned[0] = True
                    print(json.dumps({"m": "warn", "msg": "no advertising reports in 20 s - "
                                      "radio may be unplugged, or no BLE devices are in range"}, separators=(",", ":")), flush=True)
    except SystemExit:
        raise
    except BaseException:
        safe_close()
        if pcap is not None:
            pcap.close()
        raise


# ---------------------------------------------------------------- caps (GUI-ready JSON)
def cmd_caps(a):
    caps = {
        "hci": "hci%d" % a.dev,
        "adv": {
            "types": [{"id": k, "pdu": v[1], "advertising_type": v[0], "directed": v[3]}
                      for k, v in ADV_TYPES.items()],
            "profiles": sorted(ble_ad.PROFILES.keys()),
            "channels": [37, 38, 39],
            "channel_freqs": ADV_FREQ,
            "interval_ms": {"min": 20, "max": 10240, "default": 100},
            "own_addr": ["public", "random"],
            "adv_data_max_bytes": ble_ad.MAX_LEGACY_ADV_LEN,
        },
        "interfere": {
            "modes": ["wifi", "single", "sweep", "hop", "band", "adv-flood"],
            "payloads": sorted(PAYLOAD_TYPES.keys()),
            "phys": ["1M", "2M"],
            "dtm_channels": {"min": 0, "max": 39, "freq_mhz": "2402+2*N"},
            "wifi_channels": list(range(1, 14)),
            "bw": [20, 40],
            "len": {"min": 0, "max": 255, "default": 37},
            "dwell_ms": {"default": 800},
            "power": {"values": ["max", "min", "<dBm int>"]},
        },
        "sniff": {"modes": ["passive", "active"], "dedup_default": False, "outputs": ["table", "json"]},
        "hardware": {"all_hci": detect_hci_indices(),
                     "note": "Bluetooth controllers actually detected on this machine right now"},
    }
    print(json.dumps(caps, indent=2 if not a.compact else None))


# ---------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser(prog="bluforge_cli.py",
                                 description="BE200 BLE adv + interference engine (multi-radio)")
    p.add_argument("--dev", type=int, default=0, help="hci device index 0-7 (default 0)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("adv", help="generate a BLE advertising PDU")
    pa.add_argument("type", choices=list(ADV_TYPES.keys()))
    pa.add_argument("-i", "--interval", type=float, default=100)
    pa.add_argument("-c", "--channels", default="37,38,39")
    pa.add_argument("-p", "--peer")
    pa.add_argument("--peer-type", choices=["public", "random"], default="public")
    pa.add_argument("-n", "--name")
    pa.add_argument("--flags", default="06")
    pa.add_argument("--no-flags", action="store_true")
    pa.add_argument("-d", "--data", help="raw AdvData as hex")
    pa.add_argument("--profile", choices=sorted(ble_ad.PROFILES.keys()),
                     help="named beacon profile (ibeacon/eddystone_uid/eddystone_url/altbeacon/generic_sensor)")
    pa.add_argument("--profile-cfg", help="profile params as k=v,k=v (e.g. major=1,minor=2)")
    pa.add_argument("--scan-resp", dest="scan_resp")
    pa.add_argument("--power", default=None, help="TX power dBm (-127..20, max, min); default = controller's own")
    pa.add_argument("-t", "--duration", type=int, default=0)
    pa.set_defaults(func=cmd_adv)

    pi = sub.add_parser("interfere", help="generate 2.4GHz interference (DTM)")
    pi.add_argument("mode", choices=["wifi", "single", "sweep", "hop", "band", "adv-flood"])
    pi.add_argument("arg", nargs="?", default=None)
    pi.add_argument("-w", "--bw", type=int, default=20)
    pi.add_argument("-l", "--len", type=int, default=37)
    pi.add_argument("-P", "--payload", default="prbs9")
    pi.add_argument("--phy", default="1M")
    pi.add_argument("-D", "--dwell", type=float, default=800, help="ms per channel")
    pi.add_argument("--power", default=None)
    pi.add_argument("-t", "--duration", type=int, default=0)
    pi.add_argument("--map", action="store_true")
    pi.add_argument("--interval", type=float, default=20, help="adv-flood interval ms")
    pi.add_argument("--adv-channels", default="37,38,39")
    pi.set_defaults(func=cmd_interfere)

    ps = sub.add_parser("sniff", help="sniff BLE advertising over the air")
    ps.add_argument("--active", action="store_true")
    ps.add_argument("--interval", type=float, default=100)
    ps.add_argument("--window", type=float, default=100)
    ps.add_argument("--dedup", action="store_true")
    ps.add_argument("--filter", default=None)
    ps.add_argument("-t", "--duration", type=int, default=0)
    ps.add_argument("--json", action="store_true")
    ps.add_argument("--survey", action="store_true",
                    help="aggregate per device and emit changed devices once a second (GUI mode); "
                         "scans extended (BT5) + legacy advertising")
    ps.add_argument("--ext", choices=["auto", "on", "off"], default="auto",
                    help="survey: extended-advertising scan (auto = use it if the controller supports it)")
    ps.add_argument("--coded", action="store_true", help="survey: also scan the LE Coded PHY (long range)")
    ps.add_argument("--pcap", metavar="FILE", default=None,
                    help="also record every HCI packet of the session (all advertising reports, unfiltered) to this pcap "
                         "file for Wireshark. The file must not exist yet.")
    ps.add_argument("--pcap-max-mb", type=int, default=512,
                    help="stop recording at this size (0 = no limit; default %(default)s). Scanning continues.")
    ps.set_defaults(func=cmd_sniff)

    pc = sub.add_parser("caps", help="print capabilities as JSON (GUI-ready)")
    pc.add_argument("--compact", action="store_true")
    pc.set_defaults(func=cmd_caps)

    args = p.parse_args()
    if args.cmd != "caps":
        import os
        if os.geteuid() != 0:
            die("must run as root (raw HCI needs CAP_NET_ADMIN)")
        # Checks against the controllers actually present on THIS machine,
        # not a hardcoded range -- confirmed live as a real portability gap:
        # a fixed 0..7 check (this unit's own controller count) would have
        # silently accepted --dev values for radios that don't exist at all
        # on a machine with fewer controllers, only failing later and more
        # confusingly inside hci.open().
        present = detect_hci_indices()
        if args.dev not in present:
            die("--dev %d: no such controller on this machine (hci%s present: %s)" % (
                args.dev, "" if len(present) == 1 else "s",
                ", ".join("hci%d" % h for h in present) or "none detected"))
    args.func(args)


if __name__ == "__main__":
    main()
