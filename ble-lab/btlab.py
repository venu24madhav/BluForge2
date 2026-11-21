#!/usr/bin/env python3
"""
btlab.py - BLE advertisement + 2.4GHz interference engine for the Intel BE200.

Uses an HCI *User Channel* (HCI_CHANNEL_USER) to take exclusive raw control of
the controller. In this mode the in-kernel BlueZ state machine steps aside, so
every raw HCI command is accepted - which is what lets us select the exact
legacy advertising PDU type and drive Direct Test Mode precisely.

Subcommands:
  adv        emit ADV_IND / ADV_DIRECT_IND / ADV_SCAN_IND / ADV_NONCONN_IND
  interfere  generate customizable 2.4GHz interference via LE Transmitter Test (DTM)

Target: Axiomtek CT523c, Intel BE200 = hci0, Fedora 39.  Run as root.
"""
import argparse, ctypes, os, select, socket, struct, subprocess, sys, time, signal

HCI_CHANNEL_USER = 1
HCI_COMMAND_PKT  = 0x01
HCI_EVENT_PKT    = 0x04

# ---- LE opcodes (OGF 0x08) ----
def op(ogf, ocf): return (ogf << 10) | ocf
OP_RESET            = op(0x03, 0x0003)
OP_SET_EVENT_MASK   = op(0x03, 0x0001)
OP_LE_SET_EVENT_MASK= op(0x08, 0x0001)
OP_LE_SET_RAND_ADDR = op(0x08, 0x0005)
OP_LE_SET_ADV_PARAM = op(0x08, 0x0006)
OP_LE_SET_ADV_DATA  = op(0x08, 0x0008)
OP_LE_SET_SCAN_RSP  = op(0x08, 0x0009)
OP_LE_SET_ADV_EN    = op(0x08, 0x000A)
OP_LE_TX_TEST_V1    = op(0x08, 0x001E)
OP_LE_TEST_END      = op(0x08, 0x001F)
OP_LE_TX_TEST_V2    = op(0x08, 0x0034)
OP_LE_TX_TEST_V4    = op(0x08, 0x007B)   # adds Transmit_Power_Level
OP_LE_SET_SCAN_PARAM= op(0x08, 0x000B)
OP_LE_SET_SCAN_EN   = op(0x08, 0x000C)
OP_LE_READ_ADV_TXPWR= op(0x08, 0x0007)
OP_LE_SET_EXT_SCAN_PARAM = op(0x08, 0x0041)   # BT5.0+ - required to see Extended Advertising PDUs
OP_LE_SET_EXT_SCAN_EN    = op(0x08, 0x0042)
OP_LE_READ_LOCAL_SUP_CMDS = op(0x04, 0x0002)

# ---- BR/EDR (Classic) discovery opcodes ----
OP_INQUIRY           = op(0x01, 0x0001)
OP_INQUIRY_CANCEL    = op(0x01, 0x0002)
OP_WRITE_INQUIRY_MODE= op(0x03, 0x0045)
GIAC = bytes([0x33, 0x8B, 0x9E])   # General/Unlimited Inquiry Access Code (LAP), on-wire LE order

C = dict(red="\033[31m", grn="\033[32m", yel="\033[33m", cyn="\033[36m", dim="\033[2m", rst="\033[0m")
def log(m):  print(f"{C['cyn']}[*]{C['rst']} {m}")
def ok(m):   print(f"{C['grn']}[+]{C['rst']} {m}")
def warn(m): print(f"{C['yel']}[!]{C['rst']} {m}", file=sys.stderr)
def die(m):  print(f"{C['red']}[x]{C['rst']} {m}", file=sys.stderr); sys.exit(1)

class HCIError(Exception): pass

class HCIUser:
    """Exclusive raw HCI access via HCI_CHANNEL_USER."""
    def __init__(self, dev_id=0):
        self.dev_id = dev_id
        self.sock = None
        self.bt_was_running = False

    def _svc_active(self):
        out = subprocess.run(["systemctl","is-active","bluetooth"],
                             capture_output=True, text=True).stdout.strip()
        return out in ("active", "activating")   # count mid-restart as running

    def open(self):
        if os.geteuid() != 0:
            die("must run as root (HCI user channel needs CAP_NET_ADMIN)")
        # release the controller from BlueZ so we can grab the user channel
        if self._svc_active():
            self.bt_was_running = True
            subprocess.run(["systemctl","stop","bluetooth"])
        subprocess.run(["rfkill","unblock","bluetooth"], stderr=subprocess.DEVNULL)
        # device MUST be down before binding a user channel. bluetoothd (or a
        # race with a just-released channel) can leave it up/busy, so retry.
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        addr = struct.pack("<HHH", socket.AF_BLUETOOTH, self.dev_id, HCI_CHANNEL_USER)
        last_err = None
        for attempt in range(10):
            subprocess.run(["hciconfig", f"hci{self.dev_id}", "down"], stderr=subprocess.DEVNULL)
            time.sleep(0.2)
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_CLOEXEC,
                              socket.BTPROTO_HCI)
            rc = libc.bind(s.fileno(), addr, len(addr))
            if rc == 0:
                self.sock = s
                break
            last_err = ctypes.get_errno()
            s.close()
            time.sleep(0.3)                      # let a releasing owner/bluetoothd settle
        else:
            raise HCIError(f"bind(user channel) failed after retries: "
                           f"{os.strerror(last_err)} (errno {last_err}); "
                           f"another btlab job may hold hci{self.dev_id}")
        self.cmd(OP_RESET)                 # clean slate
        # After reset the default event masks hide the LE Meta event, so LE
        # Advertising Report events never arrive. Enable everything.
        self.cmd(OP_SET_EVENT_MASK,    b"\xff"*8)
        self.cmd(OP_LE_SET_EVENT_MASK, b"\xff"*8)
        return self

    def close(self):
        try:
            if self.sock: self.sock.close()
        finally:
            self.sock = None
            # closing the user channel leaves the controller DOWN; bring it back
            if self.bt_was_running:
                subprocess.run(["systemctl","start","bluetooth"], stderr=subprocess.DEVNULL)
            dev = f"hci{self.dev_id}"
            for _ in range(15):                      # up to ~3s for a clean UP RUNNING
                subprocess.run(["hciconfig", dev, "up"], stderr=subprocess.DEVNULL)
                st = subprocess.run(["hciconfig", dev], capture_output=True, text=True).stdout
                if "UP RUNNING" in st:
                    break
                time.sleep(0.2)

    def cmd(self, opcode, params=b"", want_status=True, tmo=2.0):
        pkt = struct.pack("<BHB", HCI_COMMAND_PKT, opcode, len(params)) + params
        self.sock.send(pkt)
        deadline = time.time() + tmo
        while time.time() < deadline:
            r,_,_ = select.select([self.sock], [], [], deadline - time.time())
            if not r: break
            data = self.sock.recv(258)
            if len(data) < 3 or data[0] != HCI_EVENT_PKT: continue
            ev, plen = data[1], data[2]
            body = data[3:3+plen]
            if ev == 0x0E and len(body) >= 3:                 # Command Complete
                ncmd, ret_op = body[0], struct.unpack("<H", body[1:3])[0]
                if ret_op == opcode:
                    status = body[3] if len(body) > 3 else 0
                    return status, body[4:]
            elif ev == 0x0F and len(body) >= 4:               # Command Status
                status, ncmd, ret_op = body[0], body[1], struct.unpack("<H", body[2:4])[0]
                if ret_op == opcode:
                    return status, b""
        raise HCIError(f"no Command Complete for opcode 0x{opcode:04x}")

    def read_event(self, tmo=1.0):
        """Return (event_code, params_bytes) for the next HCI event, or None on timeout."""
        r,_,_ = select.select([self.sock], [], [], tmo)
        if not r: return None
        data = self.sock.recv(300)
        if len(data) < 3 or data[0] != HCI_EVENT_PKT: return None
        return data[1], data[3:3+data[2]]

    def cmd_ok(self, opcode, params=b"", label=""):
        st, ret = self.cmd(opcode, params)
        if st == 0:
            ok(f"{label} {C['dim']}(status 00){C['rst']}")
        else:
            raise HCIError(f"{label} failed: status 0x{st:02x}")
        return ret

# ---------------------------------------------------------------- helpers
def ms_to_units(ms):
    u = int(round(ms * 1000 / 625))
    return max(0x0020, min(0x4000, u))

def addr_bytes_le(a):
    parts = a.split(":")
    if len(parts) != 6: die(f"bad address: {a}")
    return bytes(int(p,16) for p in reversed(parts))

def build_advdata(name, flags, use_flags, raw):
    if raw:
        return bytes.fromhex(raw.replace(":","").replace(" ",""))
    out = b""
    if use_flags:
        out += bytes([0x02, 0x01, int(flags,16)])
    if name:
        nb = name.encode()
        out += bytes([len(nb)+1, 0x09]) + nb
    return out

def pad31(data, what):
    if len(data) > 31:
        die(f"{what} is {len(data)}B, exceeds 31B legacy limit")
    return bytes([len(data)]) + data + b"\x00"*(31-len(data))

# advertising channels use fixed frequencies (not the data-channel formula)
ADV_FREQ = {37: 2402, 38: 2426, 39: 2480}
def dtm_freq(n): return 2402 + 2*n     # DTM/data channel index -> MHz

# ---------------------------------------------------------------- adv command
ADV_TYPES = {
    "ind":      (0x00, "ADV_IND (connectable, scannable, undirected)",   True,  False),
    "direct":   (0x01, "ADV_DIRECT_IND (connectable, directed, high duty)", False, True),
    "direct-l": (0x04, "ADV_DIRECT_IND (connectable, directed, low duty)",  False, True),
    "scan":     (0x02, "ADV_SCAN_IND (scannable, undirected)",           True,  False),
    "nonconn":  (0x03, "ADV_NONCONN_IND (non-connectable beacon)",       False, False),
}

def cmd_adv(a):
    if a.type not in ADV_TYPES: die(f"unknown adv type {a.type}")
    advtype, desc, scannable, directed = ADV_TYPES[a.type]
    if directed and not a.peer: die(f"{a.type} requires --peer <addr>")

    chmap = 0
    for ch in a.channels.split(","):
        chmap |= {"37":1,"38":2,"39":4}.get(ch.strip(), 0)
    if chmap == 0: die("need at least one adv channel of 37,38,39")

    own_t  = 0x01 if a.own == "random" else 0x00
    peer_t = 0x01 if a.peer_type == "random" else 0x00
    peer_b = addr_bytes_le(a.peer) if a.peer else b"\x00"*6

    imin, imax = ms_to_units(a.interval_min or a.interval), ms_to_units(a.interval_max or a.interval)

    hci = HCIUser(a.dev).open()
    def cleanup(*_):
        try: hci.cmd(OP_LE_SET_ADV_EN, b"\x00")
        except Exception: pass
        hci.close(); ok("advertising stopped, controller restored"); sys.exit(0)
    signal.signal(signal.SIGINT, cleanup); signal.signal(signal.SIGTERM, cleanup)

    print()
    log(f"PDU type      : {C['grn']}{desc}{C['rst']}  (Advertising_Type=0x{advtype:02x})")
    log(f"Interval      : min={a.interval_min or a.interval}ms max={a.interval_max or a.interval}ms")
    freqs = " ".join(f"{c}={ADV_FREQ[int(c)]}MHz" for c in a.channels.split(","))
    log(f"Channels      : {a.channels}  (map=0x{chmap:02x})  {freqs}")
    log(f"Own addr type : {a.own}")
    if a.peer: log(f"Peer          : {a.peer} ({a.peer_type})")

    if a.own == "random" and a.random_addr:
        hci.cmd_ok(OP_LE_SET_RAND_ADDR, addr_bytes_le(a.random_addr),
                   f"LE Set Random Address ({a.random_addr})")

    params = struct.pack("<HH", imin, imax) + bytes([advtype, own_t, peer_t]) + \
             peer_b + bytes([chmap, 0x00])
    hci.cmd_ok(OP_LE_SET_ADV_PARAM, params, "LE Set Advertising Parameters")

    if not directed:
        ad = build_advdata(a.name, a.flags, not a.no_flags, a.data)
        if ad:
            log(f"AdvData       : {ad.hex()}  ({len(ad)}B)")
            hci.cmd_ok(OP_LE_SET_ADV_DATA, pad31(ad,"AdvData"), "LE Set Advertising Data")

    if scannable and a.scan_resp:
        sr = bytes.fromhex(a.scan_resp.replace(":","").replace(" ",""))
        log(f"ScanResp      : {sr.hex()}  ({len(sr)}B)")
        hci.cmd_ok(OP_LE_SET_SCAN_RSP, pad31(sr,"ScanResp"), "LE Set Scan Response Data")

    hci.cmd_ok(OP_LE_SET_ADV_EN, b"\x01", "LE Set Advertise Enable")
    print(); ok(f"{C['grn']}Advertising ACTIVE:{C['rst']} {desc}")

    if a.duration > 0:
        log(f"running for {a.duration}s (Ctrl-C to stop early)")
        time.sleep(a.duration); cleanup()
    else:
        log("advertising until Ctrl-C ...")
        while True: time.sleep(3600)

# ---------------------------------------------------------------- interfere command
PAYLOADS = {"prbs9":0x00,"11110000":0x01,"10101010":0x02,"prbs15":0x03,
            "all1":0x04,"all0":0x05,"00001111":0x06,"0x55":0x07,"01010101":0x07}
PHYS = {"1M":0x01,"2M":0x02,"s8":0x03,"s2":0x04}

def wifi_center(ch): return 2484 if ch == 14 else 2407 + 5*ch

def resolve_channels(a):
    m = a.mode
    if m == "single":
        n = int(a.arg); assert 0 <= n <= 39; return [n], f"BLE ch {n} = {dtm_freq(n)}MHz"
    if m == "sweep":
        lo,hi = (int(x) for x in a.arg.split("-")); return list(range(lo,hi+1)), \
            f"BLE ch {lo}..{hi} = {dtm_freq(lo)}..{dtm_freq(hi)}MHz"
    if m == "hop":
        lst = [int(x) for x in a.arg.split(",")]; return lst, f"BLE ch {lst}"
    if m == "band":
        return list(range(0,40)), "FULL 2.4GHz band ch0..39 (2402..2480MHz)"
    if m == "wifi":
        ch = int(a.arg); assert 1 <= ch <= 14
        fc = wifi_center(ch); hb = a.bw//2; flo,fhi = fc-hb, fc+hb
        lst = [n for n in range(40) if flo <= dtm_freq(n) <= fhi]
        return lst, f"Wi-Fi ch {ch} center {fc}MHz, {a.bw}MHz band [{flo}..{fhi}MHz]"
    die(f"unknown interfere mode {m}")

def wifi_dtm_channels(ch, bw):
    fc = wifi_center(ch); hb = bw//2; flo, fhi = fc-hb, fc+hb
    return [n for n in range(40) if flo <= dtm_freq(n) <= fhi], fc, flo, fhi

def scan_busiest(iface):
    """Scan 2.4GHz on `iface`; return (best_channel, {ch: activity_score}).
    Score = sum of linear RSSI power per channel, so strong/nearby APs weigh most."""
    subprocess.run(["ip","link","set",iface,"up"], stderr=subprocess.DEVNULL)
    try:
        out = subprocess.run(["iw","dev",iface,"scan"], capture_output=True,
                             text=True, timeout=25).stdout
    except Exception as e:
        warn(f"scan on {iface} failed: {e}"); return None, {}
    scores, freq = {}, None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("freq:"):
            try: freq = int(float(line.split()[1]))
            except Exception: freq = None
        elif line.startswith("signal:") and freq and freq < 2500:
            try: rssi = float(line.split()[1])
            except Exception: rssi = -95.0
            ch = 14 if freq == 2484 else round((freq-2407)/5)
            if 1 <= ch <= 13:
                scores[ch] = scores.get(ch, 0.0) + 10**(rssi/10)
            freq = None
    if not scores: return None, {}
    return max(scores, key=scores.get), scores

def encode_power(p):
    """'max'->0x7F, 'min'->0x7E, integer dBm -> signed byte, None -> None."""
    if p is None: return None
    if isinstance(p, str):
        pl = p.strip().lower()
        if pl in ("max","hi","high"): return 0x7F
        if pl in ("min","lo","low"):  return 0x7E
        p = int(pl)
    p = int(p)
    return (256 + p) & 0xff if p < 0 else p & 0xff

def dtm_tx(hci, n, length, plc, phyc, pcode):
    """Start an LE Transmitter Test on channel n. Uses v4 (with TX power) when
    pcode is set, else v2 (with PHY), else v1. Ends any prior test first."""
    try: hci.cmd(OP_LE_TEST_END)
    except Exception: pass
    if pcode is not None:
        # v4: ch,len,payload,phy,cte_len,cte_type,switch_len, ant_ids[2], tx_power
        st,_ = hci.cmd(OP_LE_TX_TEST_V4, bytes([n,length,plc,phyc, 0,0,2, 0,0, pcode]))
        if st == 0: return
    st,_ = hci.cmd(OP_LE_TX_TEST_V2, bytes([n,length,plc,phyc]))
    if st == 0: return
    st,_ = hci.cmd(OP_LE_TX_TEST_V1, bytes([n,length,plc]))
    if st != 0: raise HCIError(f"LE Transmitter Test ch{n} status 0x{st:02x}")

def cmd_interfere_auto(a):
    plc = PAYLOADS.get(a.payload); phyc = PHYS.get(a.phy); pcode = encode_power(a.power)
    if plc is None: die(f"unknown payload {a.payload}")
    if phyc is None: die(f"unknown phy {a.phy}")

    print()
    log(f"AUTO interferer: scanning 2.4GHz on {C['grn']}{a.scan_iface}{C['rst']}, "
        f"targeting the busiest channel, re-scanning every {a.retarget}s")
    log(f"Payload={a.payload} len={a.len}B PHY={a.phy} bw={a.bw}MHz dwell={a.dwell}ms "
        f"power={a.power if a.power is not None else 'default'}")

    hci = HCIUser(a.dev).open()
    state = {"hops":0}
    def cleanup(*_):
        try: hci.cmd(OP_LE_TEST_END)
        except Exception: pass
        hci.close()
        ok(f"interference stopped ({state['hops']} channel-dwells), controller restored")
        sys.exit(0)
    signal.signal(signal.SIGINT, cleanup); signal.signal(signal.SIGTERM, cleanup)

    def tx(n): dtm_tx(hci, n, a.len, plc, phyc, pcode)

    ok(f"{C['grn']}AUTO interference ACTIVE{C['rst']} - radiating from the BE200 antenna")
    start = time.time(); dwell_s = a.dwell/1000.0
    while True:
        best, scores = scan_busiest(a.scan_iface)
        if best is None:
            warn("no 2.4GHz APs seen; falling back to full-band jam"); chans = list(range(40))
            log("Target        : full 2.4GHz band (no APs detected)")
        else:
            chans, fc, flo, fhi = wifi_dtm_channels(best, a.bw)
            top = sorted(scores.items(), key=lambda x:-x[1])[:4]
            report = ", ".join(f"ch{c}({'%.0f'%(10*__import__('math').log10(v))}dBm~)" for c,v in top)
            log(f"Target        : {C['yel']}Wi-Fi ch {best}{C['rst']} ({fc}MHz, {flo}-{fhi}MHz)  "
                f"[busiest: {report}]")
        # hit this channel for one retarget window (unless we run out of time first)
        win_start = time.time()
        # DTM (transmitter test) needs its own controller state; hop the covering channels
        try:
            hci.cmd(OP_LE_SET_ADV_EN, b"\x00")
        except Exception: pass
        while time.time() - win_start < a.retarget:
            for n in chans:
                tx(n); state["hops"] += 1; time.sleep(dwell_s)
                if a.duration > 0 and (time.time()-start) >= a.duration: cleanup()
                if time.time() - win_start >= a.retarget: break

def cmd_interfere(a):
    if a.mode == "auto":
        return cmd_interfere_auto(a)
    if a.mode == "adv-flood":
        # delegate to the adv path as a "realistic BLE traffic" interferer
        ns = argparse.Namespace(dev=a.dev, type="nonconn", interval=a.interval,
             interval_min=None, interval_max=None, channels=a.adv_channels,
             own="public", random_addr=None, peer=None, peer_type="public",
             name="INTERFERER", flags="06", no_flags=False, data=None,
             scan_resp=None, duration=a.duration)
        return cmd_adv(ns)

    plc = PAYLOADS.get(a.payload); phyc = PHYS.get(a.phy); pcode = encode_power(a.power)
    if plc is None: die(f"unknown payload {a.payload}")
    if phyc is None: die(f"unknown phy {a.phy}")
    chans, desc = resolve_channels(a)
    if not chans: die("no channels resolved")

    print()
    log(f"Plan          : {C['grn']}{desc}{C['rst']}")
    log(f"DTM channels  : {chans}")
    print(f"{C['dim']}               " +
          " ".join(f"{n}={dtm_freq(n)}MHz" for n in chans) + C['rst'])
    log(f"Payload       : {a.payload} (0x{plc:02x}), len={a.len}B, PHY={a.phy}")
    log(f"TX power      : {a.power if a.power is not None else 'firmware default'}")
    if a.mode != "single": log(f"Dwell         : {a.dwell}ms/channel")
    log(f"Duration      : {'until Ctrl-C' if a.duration==0 else str(a.duration)+'s'}")
    if a.map: ok("map only (no transmission)"); return

    hci = HCIUser(a.dev).open()
    state = {"hops":0}
    def cleanup(*_):
        try: hci.cmd(OP_LE_TEST_END)
        except Exception: pass
        hci.close()
        ok(f"interference stopped ({state['hops']} channel-dwells), controller restored")
        sys.exit(0)
    signal.signal(signal.SIGINT, cleanup); signal.signal(signal.SIGTERM, cleanup)

    def tx(n): dtm_tx(hci, n, a.len, plc, phyc, pcode)

    ok(f"{C['grn']}Interference ACTIVE{C['rst']} - transmitting on the BE200 antenna")
    start = time.time(); dwell_s = a.dwell/1000.0

    if a.mode == "single":
        tx(chans[0]); state["hops"] = 1
        if a.duration > 0: time.sleep(a.duration); cleanup()
        while True: time.sleep(3600)

    while True:
        for n in chans:
            tx(n); state["hops"] += 1; time.sleep(dwell_s)
            if a.duration > 0 and (time.time()-start) >= a.duration: cleanup()

# ---------------------------------------------------------------- sniff command
# LE Advertising Report Event_Type -> PDU name (legacy scan, subevent 0x02)
ADV_REPORT_PDU = {0:"ADV_IND", 1:"ADV_DIRECT_IND", 2:"ADV_SCAN_IND",
                  3:"ADV_NONCONN_IND", 4:"SCAN_RSP"}
AD_TYPE_NAMES = {0x01:"Flags",0x02:"IncompleteUUID16",0x03:"CompleteUUID16",
                 0x06:"IncompleteUUID128",0x07:"CompleteUUID128",0x08:"ShortName",
                 0x09:"CompleteName",0x0A:"TxPower",0x16:"ServiceData",0x19:"Appearance",
                 0xFF:"ManufacturerData"}

# Bluetooth SIG-assigned Company IDs (Manufacturer Specific Data, AD type 0xFF)
# and 16-bit Service UUIDs (Service Data, AD type 0x16). Every entry here was
# verified against Wireshark's own company/UUID database on this machine
# (synthetic frames -> tshark -> confirmed real name, not "Unknown") rather
# than taken from memory. Anything not in these tables just shows as raw hex -
# same fallback behavior as Wireshark itself for an unassigned/unrecognized ID.
COMPANY_IDS = {
    0x0003:"IBM", 0x0006:"Microsoft", 0x000D:"Texas Instruments",
    0x004C:"Apple", 0x0059:"Nordic Semiconductor", 0x0075:"Samsung",
    0x0078:"Nike", 0x0087:"Garmin", 0x008A:"Jawbone", 0x009E:"Bose",
    0x00E0:"Google", 0x012D:"Sony", 0x0157:"Huami (Amazfit)",
    0x0171:"Amazon", 0x01DA:"Logitech", 0x02E5:"Espressif",
    0x038F:"Xiaomi", 0x0499:"Ruuvi Innovations",
}
SERVICE_UUID_NAMES = {
    0x110B:"A2DP Audio Sink", 0x110E:"AVRCP", 0x1800:"Generic Access",
    0x180A:"Device Information", 0x180F:"Battery", 0x1812:"HID",
    0xFCF1:"Google (Fast Pair)", 0xFDCD:"Qingping Technology",
    0xFE9F:"Google", 0xFEAA:"Google (Eddystone)", 0xFEF3:"Google",
}

def decode_ad(data):
    """Parse AD structures -> dict {name, flags, tx_power, mfg, mfg_name,
    svc_uuid, svc_name, raw:[(type,hex)]}. mfg_name/svc_name are resolved via
    COMPANY_IDS/SERVICE_UUID_NAMES when the ID is a known one, else omitted -
    callers fall back to showing the raw hex ID themselves."""
    out = {"raw": []}; i = 0
    while i < len(data):
        ln = data[i]
        if ln == 0 or i+1+ln > len(data): break
        t = data[i+1]; val = data[i+2:i+1+ln]
        out["raw"].append((t, val.hex()))
        if t in (0x08,0x09):
            try: out["name"] = val.decode("utf-8","replace")
            except Exception: pass
        elif t == 0x01 and val: out["flags"] = val[0]
        elif t == 0x0A and val: out["tx_power"] = val[0]-256 if val[0]>127 else val[0]
        elif t == 0xFF and len(val) >= 2:
            cid = int.from_bytes(val[:2],"little")
            out["mfg"] = cid
            if cid in COMPANY_IDS: out["mfg_name"] = COMPANY_IDS[cid]
        elif t == 0x16 and len(val) >= 2:
            uid = int.from_bytes(val[:2],"little")
            out["svc_uuid"] = uid
            if uid in SERVICE_UUID_NAMES: out["svc_name"] = SERVICE_UUID_NAMES[uid]
        i += 1 + ln
    return out

def _ad_significant_bytes(data):
    """Return the prefix of `data` that is well-formed AD structures, stopping
    at the first zero-length byte (the spec-defined start of padding) or the
    first structure that overruns the buffer. Used to find the true end of
    EIR data (fixed-size, zero-padded to 240 bytes): a naive rstrip(b'\\x00')
    is NOT safe here, because real AD structure *values* (e.g. a 128-bit UUID)
    can themselves legitimately end in zero bytes - stripping those corrupts
    the last structure. Walking the same length-prefixed structure format
    decode_ad() uses guarantees eir_hex and the decoded `ad` dict agree on
    exactly the same bytes. (Verified against a real vivo X200T capture where
    a trailing 128-bit UUID list overran the buffer - naive rstrip produced a
    corrupted/malformed frame; this walk correctly stops before it.)"""
    i = 0
    while i < len(data):
        ln = data[i]
        if ln == 0 or i + 1 + ln > len(data):
            break
        i += 1 + ln
    return data[:i]

def parse_adv_reports(params):
    """LE Advertising Report (subevent 0x02, legacy-scan-only). Yields dicts per report."""
    if not params or params[0] != 0x02: return
    num = params[1]; i = 2
    for _ in range(num):
        if i+9 > len(params): break
        etype = params[i]; atype = params[i+1]
        addr = ":".join(f"{b:02x}" for b in params[i+2:i+8][::-1])
        dlen = params[i+8]; i += 9
        adv = params[i:i+dlen]; i += dlen
        rssi = params[i]-256 if i < len(params) and params[i] > 127 else (params[i] if i < len(params) else 0)
        i += 1
        yield {"pdu": ADV_REPORT_PDU.get(etype, f"0x{etype:02x}"),
               "addr": addr, "addr_type": "random" if atype else "public",
               "rssi": rssi, "ad": decode_ad(adv), "adv_hex": adv.hex(), "phy": "1M", "legacy": True}

def _ext_pdu_label(etype):
    legacy    = bool(etype & 0x0010)
    connect   = bool(etype & 0x0001)
    scannable = bool(etype & 0x0002)
    directed  = bool(etype & 0x0004)
    scan_rsp  = bool(etype & 0x0008)
    status    = (etype >> 5) & 0x3      # 0=complete 1=more-data 2=truncated 3=rfu
    if legacy:
        if scan_rsp: label = "SCAN_RSP"
        elif directed: label = "ADV_DIRECT_IND"
        elif connect and scannable: label = "ADV_IND"
        elif scannable: label = "ADV_SCAN_IND"
        else: label = "ADV_NONCONN_IND"
    else:
        parts = [p for p,f in (("CONN",connect),("SCAN",scannable),("DIRECT",directed)) if f]
        label = "EXT_ADV(" + "+".join(parts or ["NONCONN"]) + ")"
    return label, legacy, status

_PHY_NAME = {0x00:"none", 0x01:"1M", 0x02:"2M", 0x03:"Coded"}

def parse_ext_adv_reports(params, reasm):
    """LE Extended Advertising Report (subevent 0x0D, BT5.0+). Reassembles
    AUX_ADV_IND chains (Data_Status=more-data-to-come) keyed by (addr, SID)
    per spec, yielding one dict per COMPLETE (or terminally truncated) report."""
    if not params or params[0] != 0x0D: return
    num = params[1]; i = 2
    for _ in range(num):
        if i+24 > len(params): break
        etype = struct.unpack_from("<H", params, i)[0]; i += 2
        atype = params[i]; i += 1
        addr = ":".join(f"{b:02x}" for b in params[i:i+6][::-1]); i += 6
        pphy = params[i]; i += 1
        sphy = params[i]; i += 1
        sid  = params[i]; i += 1
        txp_b = params[i]; i += 1
        rssi_b = params[i]; i += 1
        i += 2   # Periodic_Advertising_Interval - not surfaced (no periodic-adv test yet)
        i += 1   # Direct_Address_Type
        i += 6   # Direct_Address
        dlen = params[i]; i += 1
        data = bytes(params[i:i+dlen]); i += dlen
        label, legacy, status = _ext_pdu_label(etype)
        txp  = txp_b-256 if txp_b > 127 else txp_b
        rssi = rssi_b-256 if rssi_b > 127 else rssi_b
        key = (addr, sid)
        if status == 1:                              # more fragments coming - buffer and wait
            reasm[key] = reasm.get(key, b"") + data
            continue
        full = reasm.pop(key, b"") + data if key in reasm else data
        yield {"pdu": label, "addr": addr, "addr_type": "random" if atype else "public",
               "rssi": None if rssi_b == 0x7F else rssi,
               "tx_power": None if txp_b == 0x7F else txp,
               "ad": decode_ad(full), "adv_hex": full.hex(),
               "phy": _PHY_NAME.get(pphy, f"0x{pphy:02x}"),
               "sec_phy": _PHY_NAME.get(sphy, f"0x{sphy:02x}") if sphy else None,
               "adv_sid": sid, "legacy": legacy,
               "truncated": status == 2}

# ---------------------------------------------------------------- BR/EDR (Classic) discovery
# Class of Device (24-bit, Bluetooth Assigned Numbers "Baseband"):
#   bits[1:0]=format(2)  bits[7:2]=minor(6)  bits[12:8]=major(5)  bits[23:13]=service class bitmask(11)
_COD_MAJOR = {0x00:"Miscellaneous",0x01:"Computer",0x02:"Phone",0x03:"LAN/AP",
              0x04:"Audio/Video",0x05:"Peripheral",0x06:"Imaging",0x07:"Wearable",
              0x08:"Toy",0x09:"Health",0x1F:"Uncategorized"}
_COD_MINOR_AV = {0x01:"Headset",0x02:"Handsfree",0x04:"Microphone",0x05:"Loudspeaker",
                 0x06:"Headphones",0x07:"Portable Audio",0x08:"Car Audio",0x0A:"VCR",
                 0x0B:"Video Camera",0x12:"Video Display+Loudspeaker"}
_COD_MINOR_PHONE = {0x01:"Cellular",0x02:"Cordless",0x03:"Smartphone",0x04:"Modem/Gateway"}

def decode_class_of_device(cod24):
    major = (cod24 >> 8) & 0x1F
    minor = (cod24 >> 2) & 0x3F
    major_name = _COD_MAJOR.get(major, f"0x{major:02x}")
    minor_name = ""
    if major == 0x04: minor_name = _COD_MINOR_AV.get(minor, "")
    elif major == 0x02: minor_name = _COD_MINOR_PHONE.get(minor, "")
    label = f"{major_name}/{minor_name}" if minor_name else major_name
    return {"major": major_name, "minor": minor_name, "label": label, "raw": cod24}

def parse_inquiry_result(params):
    """Inquiry Result (0x02, no RSSI). Yields dicts per response."""
    if not params: return
    num = params[0]; i = 1
    for _ in range(num):
        if i + 14 > len(params): break
        addr = ":".join(f"{b:02x}" for b in params[i:i+6][::-1]); i += 6
        i += 1   # Page_Scan_Repetition_Mode
        i += 2   # Reserved
        cod = params[i] | (params[i+1] << 8) | (params[i+2] << 16); i += 3
        i += 2   # Clock_Offset
        yield {"kind": "classic", "addr": addr, "rssi": None,
               "cod": decode_class_of_device(cod), "eir": {}, "eir_hex": ""}

def parse_inquiry_result_rssi(params):
    """Inquiry Result with RSSI (0x22). Yields dicts per response."""
    if not params: return
    num = params[0]; i = 1
    for _ in range(num):
        if i + 14 > len(params): break
        addr = ":".join(f"{b:02x}" for b in params[i:i+6][::-1]); i += 6
        i += 1   # Page_Scan_Repetition_Mode
        i += 1   # Reserved
        cod = params[i] | (params[i+1] << 8) | (params[i+2] << 16); i += 3
        i += 2   # Clock_Offset
        rssi_b = params[i]; i += 1
        rssi = rssi_b - 256 if rssi_b > 127 else rssi_b
        yield {"kind": "classic", "addr": addr, "rssi": rssi,
               "cod": decode_class_of_device(cod), "eir": {}, "eir_hex": ""}

def parse_extended_inquiry_result(params):
    """Extended Inquiry Result (0x2F) - always exactly 1 response, carries EIR
    (same length-type-value AD structure format as LE advertising data)."""
    if not params or len(params) < 1 + 6+1+1+3+2+1+240: return
    i = 1   # Num_Responses, always 0x01 for this event
    addr = ":".join(f"{b:02x}" for b in params[i:i+6][::-1]); i += 6
    i += 1   # Page_Scan_Repetition_Mode
    i += 1   # Reserved
    cod = params[i] | (params[i+1] << 8) | (params[i+2] << 16); i += 3
    i += 2   # Clock_Offset
    rssi_b = params[i]; i += 1
    rssi = rssi_b - 256 if rssi_b > 127 else rssi_b
    eir = _ad_significant_bytes(bytes(params[i:i+240]))
    yield {"kind": "classic", "addr": addr, "rssi": rssi,
           "cod": decode_class_of_device(cod), "eir": decode_ad(eir), "eir_hex": eir.hex()}

CLASSIC_EVENT_NAME = {0x02: "INQUIRY_RESULT", 0x22: "INQUIRY_RESULT_RSSI", 0x2F: "EXT_INQUIRY_RESULT"}

def cmd_sniff(a):
    """Dispatcher: --mode le (default) or --mode classic. The two engines are
    kept fully separate (not interleaved in one loop) because the production
    GUI now runs them as two independent, mutually-exclusive jobs (separate
    tabs, one radio job at a time) rather than one merged session - see
    cmd_sniff_ble / cmd_sniff_classic below."""
    if a.mode == "classic":
        cmd_sniff_classic(a)
    else:
        cmd_sniff_ble(a)


def cmd_sniff_ble(a):
    import json as _json
    hci = HCIUser(a.dev).open()
    seen = {}
    reasm = {}          # AUX chain reassembly buffer, keyed (addr, adv_sid)
    use_ext = {"v": False}

    def cleanup(*_):
        try:
            if use_ext["v"]: hci.cmd(OP_LE_SET_EXT_SCAN_EN, b"\x00\x00\x00\x00\x00\x00")
            else: hci.cmd(OP_LE_SET_SCAN_EN, b"\x00\x00")
        except Exception: pass
        hci.close()
        if not a.json:
            ok(f"sniff stopped, {len(seen)} unique device(s) seen, controller restored")
        sys.exit(0)
    signal.signal(signal.SIGINT, cleanup); signal.signal(signal.SIGTERM, cleanup)

    stype = 0x01 if a.active else 0x00
    interval = max(0x0004, min(0x4000, int(a.interval*1000/625)))
    window   = max(0x0004, min(0x4000, int(a.window*1000/625)))
    # Filter_Duplicates OFF by default: a "sniff everything in the air" tool must not
    # let the controller silently drop repeat advertisements. --dedup opts back in.
    dedup = 0x01 if a.dedup else 0x00

    # Prefer BT5.0+ Extended scanning: it reports BOTH legacy AND Extended
    # Advertising PDUs via one unified event (0x0D). Legacy-only scanning
    # (0x0B/0x0C) is blind to devices that advertise exclusively via Extended
    # PDUs. Scan on 1M *and* Coded PHY so long-range/Coded-only beacons are
    # seen too. Falls back to legacy scan commands if unsupported.
    ext_params = bytes([0x00, 0x00, 0x05]) + \
                 bytes([stype]) + struct.pack("<HH", interval, window) + \
                 bytes([stype]) + struct.pack("<HH", interval, window)
    st, _ = hci.cmd(OP_LE_SET_EXT_SCAN_PARAM, ext_params)
    if st == 0:
        st2, _ = hci.cmd(OP_LE_SET_EXT_SCAN_EN, bytes([0x01, dedup]) + struct.pack("<HH", 0, 0))
        use_ext["v"] = (st2 == 0)
    if not use_ext["v"]:
        hci.cmd(OP_LE_SET_SCAN_PARAM, bytes([stype]) + struct.pack("<HH", interval, window) + bytes([0x00, 0x00]))
        hci.cmd(OP_LE_SET_SCAN_EN, bytes([0x01, dedup]))

    if not a.json:
        print()
        mode = "Extended (legacy + BT5.0 Extended Advertising, 1M+Coded PHY)" if use_ext["v"] else "Legacy only (1M PHY)"
        log(f"Sniffing BLE advertising ({'active' if a.active else 'passive'}) - {mode}")
        log(f"Filter: {a.filter or 'none'}   Dedup: {a.dedup}   "
            f"Duration: {'until Ctrl-C' if a.duration==0 else str(a.duration)+'s'}")
        if not use_ext["v"]:
            warn("controller declined extended scan - Extended-Advertising-only devices won't be seen")
        print(f"{C['dim']}{'TIME':8} {'RSSI':>5} {'ADDRESS':17} {'TYPE':15} {'PDU':15} {'PHY':6} NAME/DATA{C['rst']}")

    start = time.time()
    while True:
        if a.duration > 0 and time.time()-start >= a.duration: cleanup()
        ev = hci.read_event(0.5)
        if not ev: continue
        code, params = ev
        if code != 0x3E: continue                      # LE Meta event
        reports = parse_ext_adv_reports(params, reasm) if use_ext["v"] else parse_adv_reports(params)
        for r in reports:
            name = r["ad"].get("name","")
            if a.filter:
                hay = (r["addr"]+" "+name).lower()
                if a.filter.lower() not in hay: continue
            key = r["addr"]
            seen.setdefault(key, 0); seen[key] += 1
            if a.json:
                r["t"] = round(time.time()-start,3); print(_json.dumps(r), flush=True)
            else:
                extra = name
                if not extra and r["ad"].get("mfg") is not None:
                    mfg_name = r["ad"].get("mfg_name")
                    extra = f"{mfg_name} (0x{r['ad']['mfg']:04x})" if mfg_name else f"mfg=0x{r['ad']['mfg']:04x}"
                elif not extra and r["ad"].get("svc_uuid") is not None:
                    svc_name = r["ad"].get("svc_name")
                    extra = f"{svc_name} (0x{r['ad']['svc_uuid']:04x})" if svc_name else f"svc=0x{r['ad']['svc_uuid']:04x}"
                if r.get("truncated"): extra += " [TRUNCATED]"
                rssi = r["rssi"] if r["rssi"] is not None else -127
                ts = f"{time.time()-start:7.2f}"
                col = C['grn'] if rssi > -60 else (C['yel'] if rssi > -80 else C['dim'])
                rssi_s = f"{rssi:5d}" if r["rssi"] is not None else "   --"
                print(f"{ts} {col}{rssi_s}{C['rst']} {r['addr']:17} "
                      f"{r['addr_type']:15} {r['pdu']:15} {r.get('phy','1M'):6} {extra}")


CLASSIC_EVENT_PARSER = {0x02: parse_inquiry_result, 0x22: parse_inquiry_result_rssi,
                        0x2F: parse_extended_inquiry_result}

def cmd_sniff_classic(a):
    """Classic BR/EDR discovery via Inquiry - a dedicated, LE-free engine (no
    LE scan is enabled in this mode; this is the exclusive job the 'Classic
    Sniff' tab runs). RSSI+EIR inquiry mode (0x02) gives device name/data via
    Extended Inquiry Result, decoded with the same AD-structure format LE
    advertising data uses."""
    import json as _json
    hci = HCIUser(a.dev).open()
    seen = {}
    active = {"v": False}   # whether Classic Inquiry is currently accepted+running

    def cleanup(*_):
        if active["v"]:
            try: hci.cmd(OP_INQUIRY_CANCEL)
            except Exception: pass
        hci.close()
        if not a.json:
            ok(f"classic sniff stopped, {len(seen)} unique device(s) seen, controller restored")
        sys.exit(0)
    signal.signal(signal.SIGINT, cleanup); signal.signal(signal.SIGTERM, cleanup)

    def start_inquiry():
        hci.cmd(OP_WRITE_INQUIRY_MODE, bytes([0x02]))
        stI, _ = hci.cmd(OP_INQUIRY, GIAC + bytes([0x08, 0x00]))   # 10.24s window, unlimited responses
        active["v"] = (stI == 0)
        if not active["v"] and not a.json:
            warn("Classic Inquiry declined by controller")
    start_inquiry()
    if not active["v"]:
        hci.close()
        die("controller declined Classic Inquiry (Inquiry not supported/available)")

    if not a.json:
        print()
        log("Sniffing Classic BR/EDR devices (Inquiry, RSSI+EIR)")
        log(f"Filter: {a.filter or 'none'}   Duration: {'until Ctrl-C' if a.duration==0 else str(a.duration)+'s'}")
        print(f"{C['dim']}{'TIME':8} {'RSSI':>5} {'ADDRESS':17} {'CLASS':22} {'EVENT':18} NAME{C['rst']}")

    start = time.time()
    while True:
        if a.duration > 0 and time.time()-start >= a.duration: cleanup()
        ev = hci.read_event(0.5)
        if not ev: continue
        code, params = ev

        if code in CLASSIC_EVENT_PARSER:
            for r in CLASSIC_EVENT_PARSER[code](params):
                r["pdu"] = CLASSIC_EVENT_NAME[code]
                r["addr_type"] = "classic"
                r["phy"] = "BR/EDR"
                r["legacy"] = True
                r["ad"] = r.pop("eir", {})
                r["adv_hex"] = r.pop("eir_hex", "")
                r["cod_label"] = r["cod"]["label"]
                name = r["ad"].get("name", "")
                if a.filter:
                    hay = (r["addr"] + " " + name + " " + r["cod_label"]).lower()
                    if a.filter.lower() not in hay: continue
                seen.setdefault(r["addr"], 0); seen[r["addr"]] += 1
                if a.json:
                    r["t"] = round(time.time()-start, 3); print(_json.dumps(r), flush=True)
                else:
                    rssi = r["rssi"] if r["rssi"] is not None else -127
                    ts = f"{time.time()-start:7.2f}"
                    col = C['grn'] if rssi > -60 else (C['yel'] if rssi > -80 else C['dim'])
                    rssi_s = f"{rssi:5d}" if r["rssi"] is not None else "   --"
                    print(f"{ts} {col}{rssi_s}{C['rst']} {r['addr']:17} "
                          f"{r['cod_label']:22} {r['pdu']:18} {name}")

        elif code == 0x01 and active["v"]:                 # Inquiry Complete - re-arm for continuous scan
            start_inquiry()

# ---------------------------------------------------------------- caps (GUI-ready JSON)
def cmd_caps(a):
    import json as _json
    caps = {
        "hci": f"hci{a.dev}",
        "adv": {
            "types": [{"id":k,"pdu":v[1],"advertising_type":v[0]} for k,v in ADV_TYPES.items()],
            "channels": [37,38,39],
            "channel_freqs": ADV_FREQ,
            "interval_ms": {"min":20,"max":10240,"default":100},
            "own_addr": ["public","random"],
            "adv_data_max_bytes": 31,
            "note": "legacy PDUs; TX power fixed by firmware for legacy advertising",
        },
        "interfere": {
            "modes": ["auto","wifi","single","sweep","hop","band","adv-flood"],
            "payloads": list(PAYLOADS.keys()),
            "phys": list(PHYS.keys()),
            "dtm_channels": {"min":0,"max":39,"freq_mhz":"2402+2*N"},
            "wifi_channels": list(range(1,15)),
            "bw": [20,40],
            "len": {"min":0,"max":255,"default":255},
            "dwell_ms": {"default":20},
            "power": {"supported": None, "values": ["max","min","<dBm int>"]},
        },
        "sniff": {"supported": True, "modes":["passive","active"],
                  "scope":"legacy + BT5.0 Extended Advertising (1M+Coded PHY), AUX-chain reassembled",
                  "dedup_default": False,
                  "outputs":["table","json"]},
        "env": {"wifi_ifaces": [], "hci_devices": []},
        "limits": ["single BT radio: sniff and interfere/advertise are mutually exclusive in time"],
    }
    # live probes
    try:
        h = HCIUser(a.dev).open()
        st,_ = h.cmd(OP_LE_TX_TEST_V4, bytes([0,0x25,0,1,0,0,2,0,0,0x7F]))
        h.cmd(OP_LE_TEST_END)
        caps["interfere"]["power"]["supported"] = (st == 0)
        h.close()
    except Exception as e:
        caps["interfere"]["power"]["supported"] = False
        caps["interfere"]["power"]["probe_error"] = str(e)
    # environment
    import glob
    caps["env"]["wifi_ifaces"] = [os.path.basename(p) for p in glob.glob("/sys/class/net/wlan*")]
    caps["env"]["hci_devices"] = [os.path.basename(p) for p in glob.glob("/sys/class/bluetooth/hci*")]
    print(_json.dumps(caps, indent=2 if not a.compact else None))

# ---------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser(prog="btlab.py", description="BE200 BLE adv + interference engine")
    p.add_argument("--dev", type=int, default=0, help="hci device index (default 0 = hci0)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("adv", help="generate a BLE advertising PDU")
    pa.add_argument("type", choices=list(ADV_TYPES.keys()))
    pa.add_argument("-i","--interval", type=float, default=100)
    pa.add_argument("--interval-min", type=float, default=None, dest="interval_min")
    pa.add_argument("--interval-max", type=float, default=None, dest="interval_max")
    pa.add_argument("-c","--channels", default="37,38,39")
    pa.add_argument("--own", choices=["public","random"], default="public")
    pa.add_argument("--random-addr", dest="random_addr")
    pa.add_argument("-p","--peer")
    pa.add_argument("--peer-type", choices=["public","random"], default="public")
    pa.add_argument("-n","--name")
    pa.add_argument("--flags", default="06")
    pa.add_argument("--no-flags", action="store_true")
    pa.add_argument("-d","--data")
    pa.add_argument("--scan-resp", dest="scan_resp")
    pa.add_argument("-t","--duration", type=int, default=0)
    pa.set_defaults(func=cmd_adv)

    pi = sub.add_parser("interfere", help="generate 2.4GHz interference (DTM)")
    pi.add_argument("mode", choices=["auto","wifi","single","sweep","hop","band","adv-flood"])
    pi.add_argument("arg", nargs="?", default=None, help="channel/range/list for the mode")
    pi.add_argument("--scan-iface", default="wlan0", help="Wi-Fi iface used to scan the air (auto mode)")
    pi.add_argument("--retarget", type=float, default=10, help="seconds between re-scans (auto mode)")
    pi.add_argument("-w","--bw", type=int, default=20, help="Wi-Fi bandwidth for 'wifi' mode")
    pi.add_argument("-l","--len", type=int, default=255, help="DTM payload length 0-255")
    pi.add_argument("-P","--payload", default="prbs9")
    pi.add_argument("--phy", default="1M")
    pi.add_argument("-D","--dwell", type=float, default=20, help="ms per channel")
    pi.add_argument("--power", default=None, help="TX power: max | min | <dBm int> (DTM v4)")
    pi.add_argument("-t","--duration", type=int, default=0)
    pi.add_argument("--map", action="store_true")
    pi.add_argument("-i","--interval", type=float, default=20, help="adv-flood interval ms")
    pi.add_argument("--adv-channels", default="37,38,39")
    pi.set_defaults(func=cmd_interfere)

    ps = sub.add_parser("sniff", help="sniff BLE advertising or Classic BR/EDR devices over the air")
    ps.add_argument("--mode", choices=["le","classic"], default="le", help="le = BLE advertising scan (default), classic = BR/EDR Inquiry")
    ps.add_argument("--active", action="store_true", help="[le] active scan (send SCAN_REQ, get names/SCAN_RSP)")
    ps.add_argument("--interval", type=float, default=100, help="[le] scan interval ms")
    ps.add_argument("--window", type=float, default=100, help="[le] scan window ms")
    ps.add_argument("--dedup", action="store_true", help="[le] let the controller filter repeat advertisements (default: OFF, report every packet - nothing dropped)")
    ps.add_argument("--filter", default=None, help="only show reports whose address/name/class contains this")
    ps.add_argument("-t","--duration", type=int, default=0)
    ps.add_argument("--json", action="store_true", help="emit one JSON object per report (GUI-friendly)")
    ps.set_defaults(func=cmd_sniff)

    pc = sub.add_parser("caps", help="print capabilities + parameter ranges as JSON (GUI-ready)")
    pc.add_argument("--compact", action="store_true", help="single-line JSON")
    pc.set_defaults(func=cmd_caps)

    a = p.parse_args()
    a.func(a)

if __name__ == "__main__":
    main()
