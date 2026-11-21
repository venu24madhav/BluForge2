# 01 - The Radio Layer: `hci_raw.py`, `ble_channels.py`, `ble_ad.py`

Who this is for: someone who has just opened the BluForge source and is lost. No
Bluetooth knowledge is assumed. Every piece of jargon is explained the first time it
appears (the primer in section 1.4 collects most of it in one place).

## 0. How to read this guide

* Line references such as `hci_raw.py:186` point at the files as I read them
  (`hci_raw.py` = 756 lines, `ble_channels.py` = 165, `ble_ad.py` = 294).
* I read every line of all three files. I did **not** touch any radio. The byte
  examples were produced by running the pure-Python parts of the code against a *fake
  socket* that just records what would have been sent and plays back canned replies,
  so the bytes you see below are what the code really builds. I also ran the built-in
  self-tests of `ble_channels.py` and `ble_ad.py` on copies (both print "OK"), and
  `python3 -m py_compile` on all three (clean).
* Two kinds of statements are mixed in here, and I label them:
  * **(code)** - visible in the repository, or verified by running the code.
  * **(spec)** - what I know about the Bluetooth Core Specification, which the repo
    does not contain. Treat these as "probably right, check the spec before relying
    on it".
  * Comments in the source that say "confirmed live" describe experiments the original
    author ran on real hardware. I could not re-run those, so I quote them as claims.
* "Things I noticed" (section 5) lists real bugs, risks and inconsistencies. Nothing in
  the source tree was modified.

---

## 1. Overview

### 1.1 The big picture

```
 Browser (GUI)
     |  HTTP + SSE
     v
 server.py   (root; holds NO radio sockets; only calls detect_hci_indices())
     |  starts one child process per radio, stops it with SIGINT then SIGTERM
     v
 bluforge_cli.py  adv | interfere | sniff | caps     <-- one process owns exactly one radio
     |            \                \
     |             \ uses            \ uses
     |              ble_ad.py          ble_channels.py
     |              (builds the        (channel numbers, MHz,
     |               advertising        Wi-Fi overlap)
     |               payload bytes)
     v
 hci_raw.py  --- raw HCI over a kernel socket --->  Linux kernel  --->  Bluetooth chip (hci0 / hci1)
     \
      \--- power off / on via the kernel "mgmt" socket (so BlueZ lets go / gets it back)
```

The other way to use a radio, `bluforge_classic.py`, goes through BlueZ's D-Bus API and
does **not** use any of these three files. A given radio is in one mode or the other at
any moment, never both: while `hci_raw.py` holds a radio, BlueZ cannot see it.

### 1.2 The three files in brief

**`hci_raw.py` - the "direct phone line" to the radio chip.** Normally a program asks
BlueZ (the Linux Bluetooth service, `bluetoothd`) to do things for it, like asking a
receptionist to place a call. `hci_raw.py` pushes the receptionist aside for one radio,
takes an exclusive line to the chip and speaks its native command language (HCI)
itself. It can start/stop the chip's test transmitter (the "interference generator"),
make it advertise custom data, and scan for other devices' advertisements. When it is
done it hands the radio back to BlueZ. It is the only one of the three files that talks
to hardware.

**`ble_channels.py` - the "channel phone book".** Bluetooth Low Energy has 40 radio
channels in the 2.4 GHz band and *two different numbering systems* for them. This file
converts between channel number, frequency in MHz, and the other numbering, and works
out which Bluetooth channels sit underneath a given Wi-Fi channel (so the GUI can say
"jam the part of the band that Wi-Fi channel 6 uses"). Pure arithmetic, no I/O. Think
of a street map's index.

**`ble_ad.py` - the "envelope and label maker".** A Bluetooth advertisement is a tiny
(at most 31 bytes) message the radio shouts repeatedly. This file builds those bytes
correctly: generic "flags/name" blocks, plus ready-made formats (iBeacon, Eddystone,
AltBeacon, and a project-private "generic sensor"). Pure byte-building, no I/O.

### 1.3 Who imports what (checked with grep over `src/`)

| Consumer | Uses |
|---|---|
| `bluforge_cli.py:33-39` | `ble_ad`, `ble_channels`, and from `hci_raw`: `HciSocket`, `HciError`, `PAYLOAD_TYPES`, `detect_hci_indices`, the `ADV_TYPE_*` constants, `DIRECTED_ADV_TYPES` |
| `server.py:41` | only `detect_hci_indices` (so the GUI offers radios that really exist) |
| `acl_meter.py:23` | `_hci_bind`, `AF_BLUETOOTH`, `BTPROTO_HCI` (borrows the private bind helper to open a read-only MONITOR socket) |
| `ble_survey.py:10-32` | no import, but consumes the report dicts that `hci_raw.read_events()` produces (keys `ext`, `event_type`, `addr_type`, `primary_phy`, `secondary_phy`, `sid`) |
| `verify_core.py`, `verify_pdu_types.py`, `verify_adv_profiles.py` | `HciSocket`, `HciError`, `detect_hci_indices`, ADV constants, `_parse_legacy_adv_reports`, `ble_ad.PROFILES` / `build_profile` (live hardware check scripts) |

Inside the radio layer the three files do not import each other. The only link is by
convention: `hci_raw.legacy_adv_configure()` wants an `adv_data` byte string (<= 31
bytes) and `ble_ad` is what builds one; and `hci_raw.tx_test_start()` wants an RF channel
number 0..39 in the same numbering that `ble_channels` documents.

### 1.4 Primer: ten words you need

* **Host / controller.** The *host* is the computer and its operating system. The
  *controller* is the Bluetooth chip (here Intel BE200s, named `hci0`, `hci1`).
* **BlueZ / `bluetoothd`.** The standard Linux Bluetooth stack and its background
  service. Normally it owns every controller.
* **HCI (Host Controller Interface).** The standardised message format between host and
  controller. Everything in `hci_raw.py` is HCI. Over a Linux socket every HCI message
  starts with one type byte (this framing is called H4): `0x01` = command from host,
  `0x04` = event from controller (`0x02` ACL data and `0x03` SCO data also exist; not used here).
* **Command, event, opcode (OGF/OCF).** The host sends *commands*; the controller answers
  with *events*. Each command has a 16-bit **opcode** made of a 6-bit **OGF** (Opcode
  Group Field: the category, e.g. `0x08` = "LE commands", `0x03` = "controller and
  baseband") and a 10-bit **OCF** (Opcode Command Field: the command inside the group):
  `opcode = (OGF << 10) | OCF`. Example: OGF `0x08`, OCF `0x0034` gives opcode `0x2034`.
* **Command Complete / Command Status events.** The controller's two ways to answer a
  command: *Command Complete* (event code `0x0E`) = "done, here is the status and any
  return data"; *Command Status* (`0x0F`) = "accepted (or rejected), the real result will
  come later". Status `0x00` means success; anything else is an error code.
* **Event mask.** A bit-field telling the controller which kinds of events to bother
  sending. Command Complete/Status are always sent; many others (including the "LE Meta"
  wrapper that carries advertising reports) are off until you enable them.
* **LE / BLE.** Bluetooth Low Energy: the low-power half of Bluetooth that does
  advertising, beacons and sensors. Classic Bluetooth (A2DP, HFP...) is a different half.
* **Advertising, PDU, AD structure.** A BLE device announces itself by repeatedly sending
  *advertising packets* on three dedicated channels. A **PDU** (Protocol Data Unit) is one
  packet; its *type* says whether the sender is connectable, scannable, directed at one
  peer, or a pure broadcast. The payload (<= 31 bytes for the "legacy" kind) is a list of
  **AD structures**, each `[length][type][data...]`. **Scanning** is listening for those;
  *passive* scanning only listens, *active* scanning also asks advertisers for an extra
  "scan response" packet.
* **PHY.** The physical-layer modulation. BLE has LE 1M (1 megasymbol/s, the default), LE 2M
  (twice as fast) and LE Coded (slower, longer range).
* **DTM (Direct Test Mode).** A factory-test mode defined by the spec: the chip transmits
  (or receives) test packets continuously on one chosen channel, with no connection. This
  project uses DTM transmit as a noise/interference generator.
* **RSSI.** Received Signal Strength Indicator: how strong a received packet was, in dBm.
  Negative numbers; closer to 0 is stronger (-40 strong, -90 weak).
* **Little-endian and `struct`.** HCI sends multi-byte numbers least-significant byte first
  (`0x2034` goes on the wire as `34 20`). Python's `struct.pack` builds such bytes. In its
  format strings `<` = little-endian with no padding, `B` = unsigned 1 byte, `H` = unsigned
  2 bytes, `b` = signed 1 byte, `6s` = 6 raw bytes, `x` = one zero pad byte.
* **Socket "channels".** A Bluetooth HCI socket in Linux can be bound in several modes
  (called channels): `RAW` (0, shares the device with BlueZ, filtered), `USER` (1, the
  program becomes the exclusive owner, sees everything), `MONITOR` (2, read-only observer
  of all traffic), `CONTROL` (3, the kernel's "mgmt" management protocol used by
  `bluetoothd`/`btmgmt`, e.g. to power a controller on or off).

---

## 2. `hci_raw.py` - raw HCI control of one radio

### 2.0 Module docstring (`hci_raw.py:1-47`)

**What it says.** The module drives a BE200 through `AF_BLUETOOTH`/`BTPROTO_HCI`
sockets, bypassing `bluetoothd`; that taking a USER-channel socket locks `bluetoothd`
out of *that one* controller only; that the stdlib `socket.bind()` cannot request the
USER channel on this Python build, so the module packs the `sockaddr_hci` struct itself
and calls libc `bind()` through `ctypes` (lines 27-35); that the `hci_filter` struct is
the 16-byte userspace shape (36-40); and that power off/on is done over the kernel mgmt
protocol instead of shelling out to `btmgmt`, because the CLI hung in this environment
(41-46).

**Gotchas (docstring is partly stale):**
* Line 7-9 promises "LE Transmitter Test (v1/v2/v4)" and "LE Set Extended Advertising
  Parameters/Data/Enable". Only v2 and v4 test commands and **legacy** advertising have
  methods. v1 (`LE_TRANSMITTER_TEST`) and the extended-advertising opcodes exist only as
  entries in the `OCF` table (`hci_raw.py:160-170`); nothing sends them.
* Line 16 says "`release()` always restores the controller". There is no `release()`; the
  method is `close()` (`hci_raw.py:351`).
* Line 3 and 72-82 talk about "BE200" and an 8-controller unit; the machine this guide
  was written on has two (`hci0`, `hci1`).

### 2.1 Imports (`hci_raw.py:48-54`)

`ctypes`/`ctypes.util` (to call libc `bind`), `os`, `socket`, `struct`, `threading`
(the lock), `time`. All standard library; no third-party dependency (the docstring's
"stdlib only").

### 2.2 Protocol constants (`hci_raw.py:56-69`)

| Name | Value | Meaning / why it is here |
|---|---|---|
| `AF_BLUETOOTH` (56) | 31 | Linux address-family number for Bluetooth sockets. (Python's `socket` module has this too; it is repeated so `_hci_bind` can put it into the raw struct.) |
| `BTPROTO_HCI` (57) | 1 | The HCI protocol within the Bluetooth family. |
| `SOL_HCI` (58), `HCI_FILTER` (59) | 0, 2 | Option level / option name for `setsockopt` of a packet filter. Only used by the unused `_set_default_filter()` (2.13.4). |
| `HCI_CHANNEL_RAW/USER/MONITOR/CONTROL` (61-64) | 0/1/2/3 | The socket "channels" from the primer. This module uses USER (to own a radio) and CONTROL (to power it). RAW and MONITOR are defined but not used in this file (`acl_meter.py:25` re-defines MONITOR itself instead of importing it). |
| `HCI_DEV_NONE` (65) | 0xFFFF | "Not bound to a specific controller"; required when binding CONTROL (or MONITOR) because those channels address controllers by index inside the messages. |
| `OGF_LE`, `OGF_HOST_CTL`, `OGF_INFO_PARAM` (67-69) | 0x08, 0x03, 0x04 | Opcode groups: LE commands, "Controller & Baseband" (Reset, Set Event Mask), and "Informational parameters". `OGF_INFO_PARAM` is never used anywhere in the project. |

### 2.3 `detect_hci_indices()` (`hci_raw.py:72-91`)

* **What:** returns the controller numbers present right now, e.g. `[0, 1]`.
* **How:** lists `/sys/class/bluetooth` (line 84); returns `[]` if the directory cannot be
  read (85-86); keeps names that start with `hci` and whose remainder is all digits (89),
  converts to int, returns sorted (91).
* **Why it exists:** the original unit had 8 controllers and `server.py`/`app.js` hard-coded
  `range(8)`; on a 4-controller unit the GUI offered radios that did not exist (the docstring
  at 73-82 tells this story). This function is the "single source of truth".
* **Gotchas:** the `isdigit()` test matters because (spec/Linux knowledge, not seen in this
  repo) `/sys/class/bluetooth` can also contain per-connection entries named like
  `hci0:256`, which must not be mistaken for controllers. It lists *every* controller,
  including non-BE200 USB dongles and radios that are currently powered off or claimed by
  another process. It is placed oddly, between the constant blocks. On this machine it returns `[0, 1]`
  (I ran it; it only reads a directory).

### 2.4 Packet/event constants (`hci_raw.py:93-97`)

`HCI_EVENT_PKT = 0x04` (the H4 type byte for events), `EVT_CMD_COMPLETE = 0x0E`,
`EVT_CMD_STATUS = 0x0F`, `EVT_LE_META = 0x3E` ("LE Meta Event": one wrapper event whose
first byte is a *sub-event code*; all LE news - advertising reports etc. - arrives
inside it).
**Gotcha:** the real code paths hard-code `0x04` (lines 394, 621) and `0x01` (384) instead
of using `HCI_EVENT_PKT`; the constant is used only by the dead `_set_default_filter()`.

### 2.5 `_libc` and `_hci_bind()` (`hci_raw.py:99-113`)

**`_libc` (99-101).** Loads the C library (`ctypes.CDLL(find_library("c"), use_errno=True)`)
and declares `bind(int fd, void *addr, int len) -> int`. The explicit `argtypes` is not
decoration: the docstring (33-35) says calling without them "silently corrupts the fd
argument" and produced a misleading `EBADFD`. `use_errno=True` is what lets
`ctypes.get_errno()` work afterwards. This runs at import time, so importing `hci_raw`
(even just for `detect_hci_indices`, as `server.py` does) loads libc; harmless and needs no root.

**`_hci_bind(sock, dev_id, channel)` (104-113).**
* **What:** binds a Bluetooth HCI socket to a controller and channel mode.
* **How:** (1) packs `sockaddr_hci` as three little-endian u16s: family, device number,
  channel (108) - 6 bytes. For `hci0` + USER this is `1f 00 | 00 00 | 01 00`; for "no device" +
  CONTROL it is `1f 00 | ff ff | 03 00` (verified). (2) copies that into a ctypes buffer
  (109). (3) calls libc `bind(fd, buf, 6)` (110). (4) on a non-zero return, reads `errno` and
  raises `OSError(errno, strerror)` (111-113).
* **Inputs/outputs:** socket, device index, channel; returns nothing, raises `OSError`
  (e.g. `EBUSY` if the controller is still powered on, `EPERM` if not root, `ENODEV` if the
  index does not exist).
* **Why:** Python's `socket.bind()` for this protocol only accepts `(dev_id,)` and always
  uses the RAW channel (docstring 27-32), so there is no stdlib way to ask for USER or CONTROL.
* **Gotchas:** the `<` format forces little-endian, which is also what the kernel wants on
  x86/ARM; it would be wrong on a big-endian host (irrelevant here). The leading-underscore name
  is "private", yet `acl_meter.py:23` imports it.

### 2.6 Report sub-event codes (`hci_raw.py:116-118`)

`LE_ADVERTISING_REPORT = 0x02` (the original "legacy" advertising report sub-event) and
`LE_EXT_ADVERTISING_REPORT = 0x0D` (Bluetooth 5 "extended" report). Line 118 defines
`LE_EXTENDED_ADVERTISING_REPORT` as a second name for the same value; nothing uses that alias.

### 2.7 `PAYLOAD_TYPES` (`hci_raw.py:120-126`)

* **What:** name -> code for the test pattern the DTM transmitter fills its packets with
  (spec: Vol 4 Part E, Table 7.8.29 per the comment). `prbs9`=0x00 (pseudo-random, 511-bit
  sequence), `11110000`=0x01, `10101010`=0x02, `prbs15`=0x03, `all1`=0x04, `all0`=0x05,
  `00001111`=0x06, `01010101`=0x07, plus an alias `0x55`=0x07.
* **Why:** so the CLI/GUI can offer names (`bluforge_cli.py:272` looks the name up) rather than magic numbers.
* **Gotcha:** `tx_test_start()` does not itself check that `payload_type` is 0..7 (2.13.9).

### 2.8 Advertising PDU types (`hci_raw.py:128-140`)

| Constant | Value | Plain meaning |
|---|---|---|
| `ADV_TYPE_IND` | 0x00 | `ADV_IND`: "I'm here; you may connect to me or ask me for more (scan response)". |
| `ADV_TYPE_DIRECT_IND_HIGH` | 0x01 | `ADV_DIRECT_IND`, high duty cycle: addressed to *one* named peer, sent very fast (about every 3.75 ms), and the controller stops by itself after ~1.28 s (that auto-stop is handled in `bluforge_cli.py:183-191`, not here). |
| `ADV_TYPE_SCAN_IND` | 0x02 | `ADV_SCAN_IND`: scannable (scan response allowed) but not connectable. |
| `ADV_TYPE_NONCONN_IND` | 0x03 | `ADV_NONCONN_IND`: pure broadcast beacon; default for `legacy_adv_configure`. |
| `ADV_TYPE_DIRECT_IND_LOW` | 0x04 | `ADV_DIRECT_IND`, low duty cycle: addressed to one peer at the user's interval. |

`DIRECTED_ADV_TYPES = (0x01, 0x04)` (140). Directed PDUs carry the target address and **no**
advertising-data field on the air, which is why `legacy_adv_configure()` refuses data for them.

### 2.9 `opcode(ogf, ocf)` (`hci_raw.py:143-144`)

Returns `(ogf << 10) | ocf`. Worked example: `opcode(0x08, 0x0034)` = `0x2000 | 0x0034` =
`0x2034`. `opcode(0x03, 0x0001)` = `0x0C01` (Set Event Mask). `opcode(0x08, 0x007B)` = `0x207B`
(verified). On the wire the two bytes are sent little-endian (`34 20`).

### 2.10 `OCF` table (`hci_raw.py:147-174`)

A dictionary name -> 10-bit OCF. **It stores only the OCF, not the OGF**, so the same number
can mean different commands in different groups: `SET_EVENT_MASK` and `LE_SET_EVENT_MASK`
are both `0x0001` and are told apart only by the `ogf` argument at the call site (`OGF_HOST_CTL`
vs `OGF_LE`) - a classic trap. Entries actually sent by some method: `RESET` (0x0003),
`SET_EVENT_MASK`, `LE_SET_EVENT_MASK`, `LE_SET_ADV_PARAMETERS` (0x0006), `LE_SET_ADV_DATA` (0x0008),
`LE_SET_SCAN_RESPONSE_DATA` (0x0009), `LE_SET_ADV_ENABLE` (0x000A), `LE_SET_SCAN_PARAMETERS`
(0x000B), `LE_SET_SCAN_ENABLE` (0x000C), `LE_TEST_END` (0x001F), `LE_ENHANCED_RECEIVER_TEST`
(0x0033), `LE_ENHANCED_TRANSMITTER_TEST` (0x0034), `LE_SET_EXT_SCAN_PARAMETERS` (0x0041),
`LE_SET_EXT_SCAN_ENABLE` (0x0042), `LE_TRANSMITTER_TEST_V4` (0x007B). **Never sent:**
`LE_SET_RANDOM_ADDRESS`, `LE_RECEIVER_TEST`, `LE_TRANSMITTER_TEST` (v1), and all the extended
advertising entries (`LE_SET_ADV_SET_RANDOM_ADDRESS`, `LE_SET_EXT_ADV_PARAMETERS/DATA/ENABLE`,
`LE_SET_EXT_SCAN_RESPONSE_DATA`, `LE_REMOVE_ADV_SET`, `LE_CLEAR_ADV_SETS`). The comment at 147
mentions a "full table in ble_spec docs"; I did not find such a file under `docs/`.

### 2.11 `HciError` (`hci_raw.py:177-178`)

An empty `Exception` subclass. Raised for "the controller said no" (non-zero status), "no
response in time", "socket not open", and the bind-failure case in `open()`. Callers in
`bluforge_cli.py` catch it (e.g. lines 176, 346, 467). Note that some failures are *not*
`HciError`: bad arguments raise `ValueError`/`KeyError`, and OS-level socket errors raise `OSError` (section 5).

### 2.12 mgmt power control (`hci_raw.py:181-229`)

**Constants (181-183).** `MGMT_OP_SET_POWERED = 0x0005` (the mgmt command "set powered"),
`MGMT_EV_CMD_COMPLETE = 0x0001`, `MGMT_EV_CMD_STATUS = 0x0002` (mgmt's own replies; mgmt is a
separate protocol from HCI with its own, unrelated numbering).

**`btmgmt_power(index, on, timeout=5.0)` (186-229).**
* **What:** switches controller `hci<index>` on or off *through the kernel's management
  interface*, the same thing `btmgmt`/`bluetoothd` do. "Off" is what lets us bind USER; "on"
  hands the radio back to BlueZ.
* **How, step by step:**
  1. Open a raw HCI socket and bind it to (`HCI_DEV_NONE`, `HCI_CHANNEL_CONTROL`) (199-201).
  2. Set a socket timeout (202).
  3. Build the mgmt command: a 6-byte header `<HHH` = opcode, controller index, parameter length
     (=1), then one parameter byte 0/1 (203-204). For "power off hci1": `05 00 | 01 00 | 01 00 | 00`
     (verified).
  4. Send, then loop until the deadline (205-206) reading replies (208). On a timeout leave the loop (209-210).
  5. Parse each reply's 6-byte header `<HHH` = event code, controller index, length (213),
     ignore packets for other controllers (215-216) and anything shorter than 6 bytes (211-212).
  6. If it is Command Complete (0x0001) or Command Status (0x0002) whose first two body bytes
     (the echoed opcode) equal `SET_POWERED`, the third body byte is the mgmt status;
     return `(status == 0, "mgmt status 0x%02x")` (217-226).
  7. If nothing matched in time return `(False, "timeout waiting for mgmt reply (index N)")` (227).
  8. Always close the socket (228-229).
* **Inputs / outputs:** `index` int, `on` bool, `timeout` seconds. Returns a tuple `(ok: bool, message: str)`.
  It never raises for a mgmt-level failure, but **can raise `OSError`** (not root, socket/bind
  failure, send failure).
* **Why:** the `btmgmt` command-line tool was found to hang "indefinitely in this environment" (docstring 189-192).
* **Gotchas:** the code only reads `body[0:3]` (opcode + status). In the real mgmt protocol (spec/mgmt
  documentation, not visible in this repo) the Command Complete for Set Powered also carries the controller's
  4-byte "current settings" after the status; this function ignores them, so it cannot tell you the
  *resulting* state, only that the kernel accepted the request. A non-zero status on "power off" is
  tolerated by `open()` (comment 257-260 claims "already powered off" can report non-zero on some firmware; I
  could not confirm that).

### 2.13 `class HciSocket` (`hci_raw.py:232-634`)

One object = one exclusive session on one radio. Typical life cycle (this is what every
`bluforge_cli.py` sub-command does):

```
h = HciSocket(0)
h.open()      # mgmt power-off -> bind USER (retry <=3 s) -> HCI Reset -> Set Event Mask -> LE Set Event Mask
...           # h.send_cmd()/_check()/tx_test_*/legacy_adv_*/scan_*/read_events()
h.close()     # close socket -> 0.1 s pause -> force_recover(): mgmt power-on, retried up to 5 s
```

#### 2.13.1 `__init__(hci_index, label="")` (235-249)
Stores `index`, a printable `label` (default `"hciN"`), `sock = None`, a lock, a flag
`_opened_user_channel`, an `_ExtReassembler` (section 2.16, defined later in the file, which is
fine because it is only looked up when an object is created), and `dead = False`.
* The lock is an **`RLock`** (re-entrant) on purpose: `bluforge_cli.py` installs a signal handler
  (`cleanup`) that calls back into `send_cmd()`; if the signal arrives while the main thread is
  already inside `send_cmd()` holding the lock, an ordinary `Lock` would deadlock (comment 239-245).
* `dead` is set to `True` by `read_events()` when the socket breaks; the CLI polls it
  (`bluforge_cli.py:533`).
* **Gotcha:** `_opened_user_channel` is written (300, 367) but never read anywhere: dead state.

#### 2.13.2 `open()` (252-302)
* **What:** takes exclusive ownership of the radio and puts it in a known state. Returns `self`.
* **How:**
  1. `btmgmt_power(index, False)` (256). The result's `ok` is ignored (257-260), only `msg` is kept
     for a possible error message.
  2. Try to bind a fresh socket to (`index`, USER), **up to 30 times, 0.1 s apart (about 3 s)** (269-279).
     Reason (comment 261-266): mgmt's "power off done" reply arrives *before* the controller is really
     bindable, so a fixed sleep would be a guess; a bounded retry is not. Each failed attempt closes its socket.
  3. If all attempts failed: call `force_recover(index, 5.0)` to give the radio back to BlueZ
     (287), then raise `HciError` explaining "another process may be holding this controller"
     (288-292).
  4. On success set a 5 s timeout, keep the socket in `self.sock`, mark `_opened_user_channel`
     (293-300), and run `_reset_and_enable_event_masks()` (301).
* **Inputs / outputs:** none / `self`.
* **Why:** the kernel refuses USER binds on a powered controller (`EBUSY`, per the docstring at 41-43),
  and a USER socket gets *no* BlueZ initialisation, so we must initialise the chip ourselves.
* **Gotchas:** (a) A USER channel socket cannot take `HCI_FILTER` (note 295-299): it already receives every
  event unfiltered, which is the point. (b) Only the bind step is protected by `force_recover`; if
  something *after* a successful bind raises (a raw `OSError` from `send`), nothing here releases the radio
  (section 5). (c) Worst-case failure takes about 3 s + up to 5 s (+ one 5 s mgmt timeout). (d) Calling `open()`
  twice on one object leaks the first socket (no guard).

#### 2.13.3 `_reset_and_enable_event_masks()` (304-332)
* **What:** the mini version of the start-up sequence `bluetoothd` normally runs.
* **How:**
  1. **HCI Reset** (OGF 0x03, OCF 0x0003, opcode `0x0C03`), wire bytes `01 03 0c 00`, for a clean slate
     (322-325). Errors are swallowed.
  2. **Set Event Mask** (opcode `0x0C01`) with 8 bytes of `0xFF`, then **LE Set Event Mask** (opcode `0x2001`)
     with 8 bytes of `0xFF` (326-332) - "enable every event", as BlueZ does. Wire bytes:
     `01 01 0c 08 ff ff ff ff ff ff ff ff` and `01 01 20 08 ff ff ff ff ff ff ff ff` (verified).
     Both calls are in the *same* `try`, so if the first fails the second is skipped.
* **Why:** the comment (312-321) says that without the event mask the controller never emitted LE Meta Events,
  so a successful scan produced *zero* advertising reports over several seconds. (spec) The extended report
  sub-event additionally needs its bit in the LE event mask.
* **Gotchas:** every `HciError` here is swallowed, **including a timeout**, so `open()` reports success even if
  the controller is not answering. The symptom would be a scan that silently sees nothing.

#### 2.13.4 `_set_default_filter()` (334-349)
* **What:** builds a `hci_filter` struct and `setsockopt(SOL_HCI, HCI_FILTER, ...)` so a **RAW**-channel socket
  would pass Command Complete, Command Status and LE Meta events.
* **How:** `type_mask = 1 << 4` (events only), `event_mask0` sets bits 14 and 15 (events 0x0E, 0x0F),
  `event_mask1` sets bit 30 of the second word (event 0x3E = 62 -> 62 - 32 = 30), opcode 0 = "any".
  Packed as `<III H 2x` = 4+4+4+2+2 = 16 bytes (336-348).
* **Why / status:** it is explicitly "only valid on a HCI_CHANNEL_RAW socket" and `open()` uses USER, so
  **nothing calls it**. It is dead code kept as documentation of the filter layout.

#### 2.13.5 `close(restore_to_bluez=True)` (351-371)
* **What:** release the session and give the radio back to BlueZ. Returns `(ok, msg)`.
* **How:** under the lock close the socket (swallowing `OSError`), set `sock = None`, clear the flag (360-367).
  If `restore_to_bluez`: sleep 0.1 s and return `force_recover(index, 5.0)` (368-370); otherwise
  `(True, "not requested")` (371).
* **Why the return value matters:** an earlier version ignored the result and every caller told the operator the
  radio was "restored" even when it was not (docstring 356-359).
* **Gotchas:** By default `close()` re-powers the radio, as a precaution: `force_recover`'s docstring (727-736) describes the abrupt-kill case, where the kernel's
  close-time reset timed out and the radio stayed "Powered: no" until explicitly re-powered.
  `__exit__` (below) throws the returned tuple away. `bluforge_cli.py:safe_close()` does the same, so a failed
  restore is not surfaced there.

#### 2.13.6 `__enter__` / `__exit__` (373-377)
Allows `with HciSocket(0) as h:`. `__enter__` returns `self.open()` (which returns `self`); `__exit__` calls
`close()` and returns `None`, so exceptions propagate. If `open()` itself raises, `__exit__` is not called (but
`open()` cleans up its own bind failure). The CLI does not use `with`; it calls `open()`/`close()` explicitly.

#### 2.13.7 `send_cmd(ogf, ocf, params=b"", expect_complete=True, timeout=5.0)` (380-407)
* **What:** sends one HCI command and waits for *its* reply. Returns a dict
  `{"event": "complete"|"status", "status": int|None, "ret": bytes}`.
* **How, step by step:**
  1. Refuse if the socket is not open (381-382).
  2. `op = opcode(ogf, ocf)`; build the packet `struct.pack("<BHB", 0x01, op, len(params)) + params`:
     type byte `01`, opcode (2 bytes LE), parameter length (1 byte), parameters (383-384).
  3. Under the lock: set the socket timeout, send, compute a deadline (385-388).
  4. Loop `recv(1024)`; stop on socket timeout (389-393). Skip empty reads and anything not an event packet (394).
  5. Event layout: `data[1]` = event code, `data[2]` = parameter length, then the body (396-397).
  6. **Command Complete** (0x0E): body = `[num_cmds(1)] [opcode(2)] [status(1)] [return params...]`.
     If the echoed opcode (`body[1:3]`) matches, return `status = body[3]` and `ret = body[3:]` (398-402).
  7. **Command Status** (0x0F): body = `[status(1)] [num_cmds(1)] [opcode(2)]`; on a matching opcode return
     `status = body[0]`, `ret = b""` (403-406).
  8. Events for other opcodes (or other event types, e.g. advertising reports) are silently dropped and the loop
     continues; if the deadline passes, raise `HciError("timeout waiting for response to opcode 0x%04x ...")` (407).
* **Worked example (verified with the fake socket).** LE Enhanced Transmitter Test on RF channel 19, 37-byte
  payload, PRBS9 pattern, 1M PHY.
  Bytes the code sends, then the reply it waits for (hex, in wire order):

  ```
  sent:     01 34 20 04 13 25 00 01
  reply:    04 0e 04 01 34 20 00
  returns:  {'event': 'complete', 'status': 0, 'ret': b'\x00'}
  ```

  | Sent byte(s) | Meaning |
  |---|---|
  | `01` | H4 packet type: HCI command |
  | `34 20` | opcode `0x2034` sent little-endian: OGF `0x08` (LE), OCF `0x0034` (Enhanced Transmitter Test) |
  | `04` | parameter length = 4 bytes follow |
  | `13` | TX channel `0x13` = 19, i.e. 2402 + 2x19 = 2440 MHz |
  | `25` | Test_Data_Length `0x25` = 37 bytes per packet |
  | `00` | Packet_Payload `0x00` = PRBS9 pattern |
  | `01` | PHY 1 = LE 1M |

  | Reply byte(s) | Meaning |
  |---|---|
  | `04` | H4 packet type: HCI event |
  | `0e` | event `0x0E` = Command Complete |
  | `04` | parameter length = 4 |
  | `01` | "the controller can accept 1 more command" (ignored by this code) |
  | `34 20` | echoed opcode `0x2034`; `send_cmd` compares this to `op` (`body[1:3]`) to be sure the reply is *ours* |
  | `00` | status 0 = success (`body[3]`) |
* **Gotchas:** the `expect_complete` parameter is accepted but never used. The per-`recv` timeout is the *full*
  `timeout`, not the time remaining. Dropping non-matching events means that any advertising report arriving
  during a `send_cmd` is lost. It can raise a raw `OSError` from `send`/`recv` if the controller vanishes
  (not wrapped in `HciError`). Deadlines use `time.time()`, which can jump if the clock is changed.

#### 2.13.8 `_check(name, ogf, ocf, params=b"", timeout=5.0)` (409-413)
Calls `send_cmd` and raises `HciError("<name> on <label> failed, HCI status 0x..")` when the status is not
`0` and not `None`; otherwise returns the reply dict. (A Command Complete that carries no status byte gives
`None`, treated as success.) Every method below goes through this, so "failure" always surfaces as `HciError`.

#### 2.13.9 `tx_test_start(rf_channel, payload_len=37, payload_type=0x00, phy="1M", power_level=None)` (416-472)
* **What:** starts the DTM transmitter: the chip emits test packets back-to-back on one channel until stopped.
  This is the project's "interference generator".
* **How:**
  1. Validate `rf_channel` in 0..39 and `payload_len` in 0..255, else `ValueError` (432-435).
  2. Call `tx_test_end()` first and ignore `HciError` (436-439): a clean "stop, then start" on every hop
     (the comment says a prior implementation did the same; re-issuing a start while running is not guaranteed
     by the spec to behave identically).
  3. Map `phy` through `{"1M": 1, "2M": 2}` (440).
  4. If `power_level` is None use **LE Enhanced Transmitter Test [v2]** (opcode `0x2034`):
     `struct.pack("<BBBB", channel, len, payload, phy)` (471-472).
  5. If `power_level` is given use **LE Transmitter Test [v4]** (opcode `0x207B`), which adds TX power:
     validate it is -127..+20 dBm or the sentinels `0x7E` (minimum) / `0x7F` (maximum) (451-454), then pack ten bytes
     `<BBBBBBBBBb` = channel, len, payload, phy, CTE_Length 0, CTE_Type 0, Switching_Pattern_Length **2**,
     antenna IDs `0, 0`, power (signed) (467-470).
* **Output:** the reply dict from `_check`.
* **Worked example, v4 (verified):** `tx_test_start(19, 37, 0, "2M", power_level=-10)` sends
  `01 7b 20 0a 13 25 00 02 00 00 02 00 00 f6`: opcode `7b 20`, length `0a` (10), channel 19, len 37, PRBS9,
  PHY 2, CTE 0/0, pattern length 2, antennas `00 00`, power `f6` (= -10 as a signed byte).
* **Why the odd "pattern length 2":** the comment (457-466) records a real bug: sending 0 made every radio
  reject the command with HCI status `0x12` ("Invalid HCI Command Parameters"), because the spec's valid range for
  Switching_Pattern_Length is 0x02-0x4B regardless of whether CTE is used. Supplying a dummy two-entry pattern fixes it.
* **Gotchas:**
  * Bad `phy` raises **`KeyError`**, not `ValueError`, and it happens *after* `tx_test_end()` has already stopped any
    running test (I verified `01 1f 20 00` was sent before the `KeyError`). The same ordering applies to a bad
    `power_level` (`ValueError` after the stop).
  * `payload_type` is not validated (an out-of-range value reaches the firmware).
  * `0x7E` (126) and `0x7F` (127) are sentinels, so "126 dBm" cannot be requested; `power_level=21` or `-128` are rejected (verified).
  * Only 1M and 2M are supported for TX (no Coded).
  * When no test is running, the `tx_test_end()` inside is expected to fail (spec: "Command Disallowed", status `0x0C`; in my fake-socket run I fed it that status to mimic this), which is the "expected first time" error swallowed at 438-439. A *timeout* is swallowed the same way, adding up to 5 s.

#### 2.13.10 `rx_test_start(rf_channel, phy="1M", modulation_index="standard")` (474-486)
* **What:** starts the DTM *receiver* (LE Enhanced Receiver Test [v2], opcode `0x2033`) so a second radio can
  prove that a first radio's transmitter really puts energy on a channel.
* **How:** validate channel; map `phy` via `{"1M":1,"2M":2,"Coded":3}`; map `modulation_index` via
  `{"standard":0,"stable":1}`; pack `<BBB` (481-486). Example: `rx_test_start(19, "1M")` sends `01 33 20 03 13 01 00` (verified).
* **Why:** per the docstring, LE Test End's "number of packets" is meaningful only for the *receiver* side.
* **Gotchas:** bad `phy`/`modulation_index` raises `KeyError`. Used only by `verify_core.py:48`, not by the CLI.

#### 2.13.11 `tx_test_end()` (488-497)
Sends **LE Test End** (opcode `0x201F`, no parameters: `01 1f 20 00`). On success the return parameters are
`status(1) + Number_Of_Packets(2, LE)`; it returns that count as an int via `r["ret"][1:3]` (496), or `None` if the
reply had no data. Example: a reply carrying `23 01` gives `0x0123` = 291 (verified). Non-zero status raises
`HciError`. Gotcha: for a *transmit* test the count is always 0 by spec (comment 489-492), so it is not a success
measure. If no test is active the controller (spec) answers "Command Disallowed" (`0x0C`), which becomes `HciError`.

#### 2.13.12 `legacy_adv_configure(...)` (500-557)
Signature: `adv_data=b"", own_addr_type=0x00, adv_type=ADV_TYPE_NONCONN_IND, channel_map=0x07, interval_ms=100,
scan_rsp=b"", peer_addr=None, peer_addr_type=0x00`.
* **What:** programs the controller with *what* to advertise (up to three commands). It does not start sending; call
  `legacy_adv_enable(True)` for that.
* **How, step by step:**
  1. Directed types need `peer_addr` and must not have `adv_data` (`ValueError`, 513-518).
  2. `adv_data` must be <= 31 bytes (519-520); `interval_ms` must be <= 10240 (530-531).
  3. Convert the interval to 0.625 ms units: `iv = max(round(interval_ms / 0.625), 160)` (532). 100 ms -> 160 = `0x00A0`.
  4. Peer address: split `"AA:BB:CC:DD:EE:FF"`, **reverse** it (HCI stores addresses little-endian), convert to 6 bytes (533-536);
     all zeros if no peer (538).
  5. Send **LE Set Advertising Parameters** (opcode `0x2006`, 15 bytes) (542-544).
  6. For non-directed types send **LE Set Advertising Data** (opcode `0x2008`): `<B31s` = real length + data zero-padded to 31 (545-548).
  7. If `scan_rsp` is given, it must be for a scannable type (ADV_IND or ADV_SCAN_IND) and <= 31 bytes; send **LE Set Scan Response
     Data** (opcode `0x2009`) (549-557).
* **Worked example (verified):** a non-connectable beacon with just the Flags AD structure `02 01 06`, 100 ms, all three channels.
  Wire bytes (verified with the fake socket):

  ```
  Set Advertising Parameters:  01 06 20 0f a0 00 a0 00 03 00 00 00 00 00 00 00 00 07 00
  Set Advertising Data:        01 08 20 20 03 02 01 06 00 00 00 ... (padded with zeros to 31 data bytes; 36 bytes in total)
  ```

  | Bytes | Meaning |
  |---|---|
  | `01` | H4 command packet |
  | `06 20` | opcode `0x2006` (LE Set Advertising Parameters), little-endian |
  | `0f` | 15 parameter bytes follow |
  | `a0 00` | Advertising_Interval_Min = 160 x 0.625 ms = 100 ms |
  | `a0 00` | Advertising_Interval_Max = the same (the code always sets min = max, `hci_raw.py:542`) |
  | `03` | Advertising_Type `0x03` = ADV_NONCONN_IND |
  | `00` | Own_Address_Type 0 = public address |
  | `00` | Peer_Address_Type 0 (unused for this type) |
  | `00 00 00 00 00 00` | Peer address (six zero bytes, unused) |
  | `07` | Channel map `0b111` = advertising channels 37, 38 and 39 |
  | `00` | Filter policy 0 = no filter |

  Second command: opcode `08 20` (LE Set Advertising Data), length `20` (= 32), then the real data length `03`, the AD bytes `02 01 06`, then 28 zero bytes of padding.
* **Why:** to emulate real advertisers (beacons, directed, scannable...) for interoperability tests.
* **Gotchas:**
  * **The scan-response checks come *after* the first two commands were already sent** (verified: a bad `scan_rsp` raises `ValueError` after 2 commands
    went out), so the controller is left half-configured (not enabled).
  * **The 100 ms floor applies to every type**, silently: `interval_ms=20` or `0` both become 160 (verified). The comment at 521-532 says the floor is the
    spec minimum "for non-connectable/scannable advertising specifically", but the code applies it to connectable and directed types too.
    (spec, from memory: Core 5.0+ allows 20 ms for these; so the code is stricter than necessary - check before relying on this.)
  * Own address type is configurable, but no method here sets a random address (the `LE_SET_RANDOM_ADDRESS` opcode is unused), so
    `own_addr_type=0x01` would depend on state this module never sets. The CLI never passes it.
  * `channel_map` is not range-checked; the CLI checks it (`bluforge_cli.py:105-109`).
  * (spec) Advertising parameters cannot be changed while advertising is enabled; this method does not disable first. The CLI is fine because
    it always starts right after `open()`'s Reset.
  * The docstring's phrase "see PAYLOAD_TYPES / DIRECTED_ADV_TYPES docs above" (511) wrongly mentions PAYLOAD_TYPES (unrelated).

#### 2.13.13 `legacy_adv_enable(on)` (559-561)
Sends **LE Set Advertising Enable** (opcode `0x200A`) with one byte: `01 0a 20 01 01` to start, `... 00` to stop (verified). Raises
`HciError` on failure. Gotcha: for high-duty directed advertising the controller turns itself off after ~1.28 s; the CLI accounts for this.

#### 2.13.14 `scan_configure(interval_ms=100, window_ms=100, active=True, own_addr_type=0x00)` (564-578)
* **What:** sets the legacy scan parameters (**LE Set Scan Parameters**, opcode `0x200B`). *Interval* = how often a scan cycle starts,
  *window* = how long within each cycle the radio actually listens; window == interval means "listen all the time".
* **How:** reject either value > 10240 ms and `window > interval` with `ValueError` (571-574); convert to 0.625 ms units with a floor of 4
  (= 2.5 ms) (575-576); pack `<BHHBB` = scan type (1 active / 0 passive), interval, window, own address type, filter policy 0 (577).
  Example `scan_configure(100, 100, True)` sends `01 0b 20 07 01 a0 00 a0 00 00 00` (verified).
* **Why the checks:** an earlier version let invalid values reach the firmware or overflow `struct.pack` (comment 565-570).

#### 2.13.15 `scan_enable(on, filter_dup=False)` (580-582)
**LE Set Scan Enable** (opcode `0x200C`), `<BB` = enable, filter-duplicates: `01 0c 20 02 01 00`. `filter_dup=True` makes the controller report each
advertiser once instead of every time it is heard.

#### 2.13.16 `ext_scan_configure(interval_ms=100, window_ms=100, active=False, coded=False, own_addr_type=0x00)` (584-598)
* **What:** **LE Set Extended Scan Parameters** (opcode `0x2041`). Needed to see Bluetooth 5 *extended* advertising, which legacy scanning cannot
  receive; it also still reports legacy advertisers, so one path covers both (docstring 586-589).
* **How:** reject `window > interval` (590-591); convert units, floor 4 (592-593); `phys` bitmask = `0x01` (1M) plus `0x04` (Coded) if `coded` (594);
  pack `<BBB` = own address type, filter policy 0, phys (595); then append one `<BHH` (scan type, interval, window) block **per selected PHY**
  (596-597). With `coded=True` the same interval/window/type is used for both PHYs. Example (verified, coded): `01 41 20 0d 00 00 05 00 a0 00 a0 00 00 a0 00 a0 00`.
* **Gotchas:** unlike `scan_configure`, there is **no upper-bound check**: `ext_scan_configure(50000, 100)` raises a raw `struct.error` (verified), not
  `ValueError`. Default `active=False` here vs `True` in `scan_configure` (the CLI always passes it explicitly).

#### 2.13.17 `ext_scan_enable(on, filter_dup=False)` (600-603)
**LE Set Extended Scan Enable** (opcode `0x2042`), `<BBHH` = enable, filter-dup, duration 0, period 0 ("scan continuously until disabled").
Bytes: `01 42 20 06 01 00 00 00 00 00` (verified).

#### 2.13.18 `read_events(duration_s, on_adv_report=None)` (605-634)
* **What:** the receive loop. Blocks for `duration_s` seconds, calling `on_adv_report(dict)` for each advertising report. With no callback it is
  just a drain. Returns how many reports were delivered.
* **How:**
  1. Set the socket timeout to 0.5 s so the loop wakes up regularly (611).
  2. `recv(2048)`; on timeout just loop (614-618); on any other `OSError` set `self.dead = True` and stop (619-620).
  3. Ignore non-event packets (621-622); take `evt`, `plen`, `body` as in `send_cmd` (623-624).
  4. For LE Meta events (`0x3E`) with a callback: `body[0]` is the sub-event. `0x02` -> `_parse_legacy_adv_reports(body[1:])`, call the callback
     per report. `0x0D` -> `_parse_ext_adv_reports(body[1:])`, pass them through the reassembler first, then the callback (625-633).
  5. Every other event is ignored.
* **Worked example (verified through the fake socket):** the controller sends (hex, wire order)

  ```
  04 3e 0f 02 01 00 00 66 55 44 33 22 11 03 02 01 06 c4
  ```

  | Bytes | Meaning |
  |---|---|
  | `04` | H4 packet type: event |
  | `3e` | `0x3E` = LE Meta Event (a wrapper; the next byte says which LE event) |
  | `0f` | parameter length = 15 bytes |
  | `02` | sub-event `0x02` = LE Advertising Report (legacy) |
  | `01` | Num_Reports = 1 |
  | `00` | Event_Type 0 = ADV_IND |
  | `00` | Address_Type 0 = public |
  | `66 55 44 33 22 11` | address, little-endian, so printed reversed: `11:22:33:44:55:66` |
  | `03` | data length = 3 |
  | `02 01 06` | the advertised data: one Flags AD structure |
  | `c4` | RSSI `0xc4` as a signed byte = -60 dBm |

  `read_events` slices `evt = data[1] = 0x3e`, `plen = data[2] = 15`, `body = data[3:18]`; `body[0] = 0x02` selects the legacy parser,
  which receives `body[1:]`. The callback gets
  `{'event_type': 0, 'addr_type': 0, 'addr': '11:22:33:44:55:66', 'data': b'\x02\x01\x06', 'rssi': -60}`.
* **Gotchas:** not protected by the lock (only `send_cmd` and `close` take it), so it is meant for one thread. It dereferences `self.sock` without a
  None check (calling it before `open()` gives `AttributeError`). A malformed report raises out of this function (2.14 explains), and the CLI treats any
  uncaught exception as fatal for the job. An empty `recv` result is skipped with `continue`, so a socket that returns empty reads forever would spin (I
  have no evidence this happens; broken sockets normally raise `OSError`, which sets `dead`).

### 2.14 `_parse_legacy_adv_reports(payload)` (`hci_raw.py:637-668`)

* **What:** turns the body of a legacy LE Advertising Report (everything after the sub-event byte) into a list of dicts
  `{event_type, addr_type, addr, data, rssi}`.
* **How:** the layout is "array of fields", not "array of reports". After `Num_Reports` (1 byte) come, in order: all `Event_Type`s (1 byte each), all
  `Address_Type`s, all `Address`es (6 bytes each), all `Data_Length`s, then all `Data` blobs back to back, then all RSSIs (signed byte). The code walks the
  buffer with an offset `off` once per array (646-659), then zips the arrays into dicts (660-667). The address is reversed and formatted as upper-case
  `AA:BB:..` (664). In practice `Num_Reports` is usually 1 (my understanding, not checked here), but the code handles more.
* **Event types** (legacy): 0 ADV_IND, 1 ADV_DIRECT_IND, 2 ADV_SCAN_IND, 3 ADV_NONCONN_IND, 4 SCAN_RSP (`bluforge_cli.py:389` maps them to names).
* **Gotchas:** (a) **no bounds checking**: a truncated report raises `IndexError` (verified) or `struct.error` and aborts `read_events`. (b) The RSSI value 127
  (spec: "not available") is passed through as `127` (verified), whereas the extended parser converts it to `None`. (c) `event_type` has a *different meaning* for legacy and extended
  reports; consumers must look at the `ext` key (`ble_survey.py:18` does).

### 2.15 Extended report constants and `_parse_ext_adv_reports(payload)` (`hci_raw.py:671-699`)

**Constants (672).** Bits of the extended report's 16-bit `event_type`: `EXT_CONNECTABLE=0x01`, `EXT_SCANNABLE=0x02`, `EXT_DIRECTED=0x04`, `EXT_SCAN_RSP=0x08`,
`EXT_LEGACY=0x10` (the report was produced from a legacy PDU). Bits 5-6 hold the *data status* (0 complete, 1 "more data coming", 2 "truncated"), read at line 697. Only
`EXT_SCAN_RSP` is used in this file (713); the others are unused anywhere (`ble_survey.py` uses its own literal `0x10`, `0x08`...).

**`_parse_ext_adv_reports`.**
* **What:** like the legacy parser but for the Bluetooth 5 report; fields are interleaved per report instead of grouped.
* **How:** read `Num_Reports`; for each report require at least 24 header bytes remaining (684-685), unpack them in one go with
  `struct.unpack_from("<HB6sBBBbbHB6sB", payload, off)` (686-688), advance 24, then slice `dlen` data bytes (689-690).
* **The 24 header bytes:** event_type(2), address_type(1), address(6), primary_phy(1), secondary_phy(1), SID(1, "advertising set ID"), tx_power(1, signed), rssi(1, signed),
  periodic_adv_interval(2), direct_address_type(1), direct_address(6), data_length(1). The direct address fields are parsed and thrown away.
* **Output:** dicts with `ext: True`, `event_type`, `addr_type`, `addr`, `primary_phy`, `secondary_phy`, `sid`, `tx_power` and `rssi` (127 -> `None`), `periodic_interval`,
  `data` (bytes) and `status` = `(event_type >> 5) & 3` (691-698). A verified example: a fragment with `event_type 0x21` gives `status 1`.
* **Gotchas:** if the data length claims more than is present, the slice is silently short (no exception). The `sid` of 0xFF ("no SID") is not translated. Phy codes are
  raw (primary: 1=1M, 3=Coded; secondary: 0=none, 1=1M, 2=2M, 3=Coded - spec).

### 2.16 `class _ExtReassembler` (`hci_raw.py:702-723`)

* **What:** extended advertising data can be longer than one report can carry, so the controller delivers it in *fragments* (status 1 = "more to come"). This class glues
  the fragments together and releases only finished (status 0) or truncated (status 2) reports.
* **How:** `MAX_PENDING = 64` (705); `pending` is a dict keyed by `(address, sid, is_scan_response)` (713). For each incoming report: if a fragment with the same key is pending,
  pop it and prepend its data (714-716); if this report's status is 1, store it as the new pending (evicting the oldest entry if 64 are already waiting, 718-720); otherwise append
  it to the finished list (721-722). Returns the finished list.
* **Worked example (verified):** fragment 1 (`status 1`, data `AAAA`) -> `[]`; fragment 2 (`status 0`, data `BBB`, same address and SID) -> one report with data `AAAABBB`.
  The merged report keeps the metadata (RSSI etc.) of the *last* fragment.
* **Gotchas:** there is **no timeout or staleness check**. If the last fragment is lost, the orphan stays pending and the *next* unrelated report with the same key gets its data glued
  onto the old data. The 64-entry cap bounds memory but silently drops the oldest. Not thread-safe (fine: one reader).

### 2.17 `force_recover(index, timeout=10.0)` (`hci_raw.py:726-744`)

* **What:** "make sure this radio is powered on and back with BlueZ". Returns the last `(ok, msg)` from `btmgmt_power`.
* **How:** until the deadline: call `btmgmt_power(index, True)`; if it succeeds return; else sleep 0.5 s and retry (737-744).
* **Why:** the docstring (727-736) describes a real incident: a process holding a USER socket was killed abruptly (SIGKILL), the kernel's close-time HCI Reset timed out
  (`dmesg: "HCI reset during shutdown failed"`, "Opcode 0x0c03 failed: -110"), and three controllers stayed "Powered: no" until explicitly re-powered; one Set Powered call fixed all three.
  It is why `server.py` only ever sends SIGINT/SIGTERM.
* **Used by:** `open()` (bind failure, 287) and `close()` (370). Safe to call on a healthy controller.
* **Gotchas:** each `btmgmt_power` call may itself wait up to 5 s, so the overall time can overshoot `timeout`. If `btmgmt_power` raises `OSError` (e.g. not root), it propagates, and in
  `open()`'s failure path that would mask the intended `HciError`.

### 2.18 `if __name__ == "__main__"` smoke test (`hci_raw.py:747-756`)

Takes an optional hci index (default **7**), prints, does `HciSocket(idx).open()`, then `close()`, then prints "smoke test OK". **I did not run it** (it touches a radio).
Notes from reading: (a) the default 7 does not exist on a two-radio machine; from the code I would expect `open()` to fail after about 8 s (3 s of bind retries on a non-existent
index, then `force_recover` retrying for 5 s) and raise `HciError`; pass `0` or `1` explicitly. (b) The comment calls it "read-only", and it transmits nothing, but it *does* take the radio away from
BlueZ, reset it, and hand it back: do not run it while something else is using that radio.

---

## 3. `ble_channels.py` - channel arithmetic

### 3.0 Module docstring (`ble_channels.py:1-26`)
Explains the central trap: **two numbering systems for the same 40 channels**.

* **RF channel `f`, 0..39**: frequency = 2402 + 2*f MHz (2402...2480). *This is what the HCI test commands take* (`tx_test_start`'s `rf_channel`). All raw-HCI code uses this.
* **LL (link-layer) channel index `k`, 0..39**: the numbering used in channel maps and channel-selection algorithms. Data channels get LL 0..36 in frequency order, but the three *advertising*
  channels are numbered **37, 38, 39** even though they sit at 2402, 2426 and 2480 MHz, i.e. RF 0, 12 and 39. The project's control code never needs LL numbers; they are "for display/education".
* The module builds the mapping by rule and self-checks it (round-trips, 37 + 3 = 40).

### 3.1 Constants (`ble_channels.py:28-29`)
* `ADVERTISING_RF_CHANNELS = (0, 12, 39)`: RF numbers of the advertising channels (2402, 2426, 2480 MHz).
* `ADV_CHANNEL_MAP_BIT = {37: 0b001, 38: 0b010, 39: 0b100}`: which bit of the advertising channel-map byte enables which advertising channel (the `channel_map=0x07` seen in 2.13.12 is all three).
  **Never used outside this file's self-test**: the CLI has its own copy (`bluforge_cli.py:107` `{"37": 1, "38": 2, "39": 4}`) and its own frequency table (`bluforge_cli.py:71` `ADV_FREQ`).

### 3.2 `rf_to_freq_mhz(rf_channel)` (`:32-35`)
Checks 0..39 (`ValueError` otherwise) and returns `2402 + 2*rf`. `rf_to_freq_mhz(19)` = 2440. Used by the CLI to print channel -> MHz (`bluforge_cli.py:241, 293`).

### 3.3 `freq_mhz_to_rf(freq_mhz)` (`:38-41`)
Inverse. Rejects odd offsets or anything outside 2402..2480 with `ValueError`: `freq_mhz_to_rf(2437)` (Wi-Fi channel 6's centre) fails because BLE channel centres are even MHz offsets from 2402
(verified: 2403 rejected, 2480 -> 39). Only used by the self-test.

### 3.4 `is_advertising_rf(rf_channel)` (`:44-45`)
`rf_channel in (0, 12, 39)`. Unused outside the file (the dict in `all_channels` derives the role differently, from the LL index).

### 3.5 `rf_to_ll_index(rf_channel)` (`:48-60`)
* **What:** RF number -> LL index.
* **How:** the three advertising channels are special-cased (RF 0 -> 37, 12 -> 38, 39 -> 39) (51-56). A data channel's LL index is its RF number minus the count of advertising channels that sit
  below it (59-60). Verified: RF 1 -> LL 0, RF 11 -> 10, RF 13 -> 11, RF 38 -> 36. So RF 19 (2440 MHz) is LL 17.
* **Gotcha:** unlike its siblings it does not validate its input (RF 40+ yields nonsense instead of an error). Only `all_channels()` and the self-test call it.

### 3.6 `ll_index_to_rf(ll_index)` (`:63-69`)
Inverse by brute force: 37/38/39 from a small dict, otherwise search RF 0..39 (skipping advertising ones) for the one whose LL index matches; `ValueError` if none (e.g. LL 40). Self-test only.

### 3.7 `all_channels()` (`:72-83`)
Returns a list of 40 dicts `{rf_channel, freq_mhz, ll_index, role}` where role is `"advertising"` if the LL index is 37/38/39 else `"data"`. Verified advertising rows: `(0, 2402, 37)`, `(12, 2426, 38)`,
`(39, 2480, 39)`. Used by `ble_channels_overlapping_wifi` and the self-test.

### 3.8 `wifi_channel_center_mhz(wifi_ch)` (`:87-92`)
Wi-Fi 2.4 GHz channel -> centre frequency: channel 14 -> 2484 (the odd one out, Japan only), channels 1-13 -> `2412 + 5*(ch-1)` (ch 1 = 2412, ch 6 = 2437, ch 11 = 2462). Otherwise `ValueError`.
Used by the CLI (`bluforge_cli.py:216`).

### 3.9 `ble_channels_overlapping_wifi(wifi_ch, width_mhz=20)` (`:95-101`)
* **What:** which BLE RF channels lie underneath a Wi-Fi channel. This powers "interfere with Wi-Fi channel N" (`bluforge_cli.py:215`).
* **How:** `center` from the previous function; `lo = center - width/2`, `hi = center + width/2`; return every BLE RF channel whose centre frequency is in `[lo, hi]` **inclusive** (99-101).
* **Verified results (width 20):** Wi-Fi 1 -> RF 0..10; Wi-Fi 6 (2427-2447 MHz) -> RF 13..22; Wi-Fi 11 -> RF 25..35; Wi-Fi 14 -> RF 36..39.
* **Gotchas:** this is a first-order approximation ("plausibly desensitize/collide"): real Wi-Fi spectrum spills outside +/- 10 MHz, and a Wi-Fi channel 1 overlap includes RF 0 only because the edge is inclusive. For `width_mhz=40`
  (which the CLI allows, `bluforge_cli.py:256`) the function centres the span on the *channel number's* own centre; a real 40 MHz Wi-Fi channel is centred 10 MHz above or below the primary channel, so the 40 MHz result
  for channel 6 (RF 8..27, verified) is probably off-centre. `width_mhz` is not validated.

### 3.10 `tx_test_packet_duration_us(payload_len, phy="1M")` (`:105-116`)
* **What:** estimated on-air time of one DTM packet in microseconds.
* **How:** `bits = (1 + 4 + 2 + payload_len + 3) * 8` = preamble 1 byte + access address 4 + header 2 + payload + CRC 3, times 8; divide by 1.0 (1M) or 2.0 (2M); other PHYs raise `ValueError`.
  Verified: 37-byte payload on 1M = 376 us, on 2M = 188 us; 255 bytes on 1M = 2120 us.
* **Gotchas:** nothing in the project calls it except the self-test. (spec, from memory) the LE 2M PHY uses a 2-byte preamble, which this formula does not account for, so 2M numbers are about 4 us short. The docstring cites
  "Vol 6 Part F 4.1"; I cannot check that here. Coded PHY is deliberately not modelled.

### 3.11 Self-test (`:119-165`)
Runs when the file is executed directly: asserts 40 channels, 37 data + 3 advertising, frequencies, the 2437 rejection, round-trips for all 40 RF channels, Wi-Fi overlap sanity (RF 17 in Wi-Fi 6's span, RF 0 not), duration sanity;
then prints a table and the overlap lists for Wi-Fi 1, 6, 11. It is pure and safe to run (I ran it on a copy: "ble_channels self-test OK"). Note the `ADV_CHANNEL_MAP_BIT` "cross-check" (140-141) only asserts that each bit is one of three allowed
values, which cannot fail.

---

## 4. `ble_ad.py` - advertising-data builders

### 4.0 Module docstring (`ble_ad.py:1-16`)
Describes the AD structure `[Length][AD Type][AD Data...]` (Length counts the type byte plus the data), the 31-byte legacy limit, and the reservation of company ID `0xFFFF` as the SIG's "for testing" value so that synthetic payloads do
not impersonate real products. iBeacon and Eddystone are open, public formats. **Gotcha:** the claim "every synthetic payload this module builds defaults to [0xFFFF]" is not true of iBeacon, which defaults to Apple's `0x004C`
(`ble_ad.py:100-101`, admitted in its own docstring at 102-106).

### 4.1 Constants (`ble_ad.py:19-37`)
* `MAX_LEGACY_ADV_LEN = 31` (19): the legacy advertising payload cap (also exposed in the CLI's `caps` output, `bluforge_cli.py:561`).
* `AD_TYPE_*` (22-31): the one-byte AD type codes (Bluetooth SIG assigned numbers): Flags `0x01`, Incomplete/Complete list of 16-bit UUIDs `0x02`/`0x03`, Shortened/Complete local name `0x08`/`0x09`, TX Power Level `0x0A`,
  Peripheral connection interval range `0x12` (named "SLAVE" here), 16-bit Service Data `0x16`, Appearance `0x19`, Manufacturer Specific Data `0xFF`. `INCOMPLETE_16BIT_UUIDS`, `SLAVE_CONN_INTERVAL_RANGE` and `APPEARANCE` are not used by anything.
* `FLAG_*` (33-35): bits for the Flags byte: LE Limited Discoverable `0x01`, LE General Discoverable `0x02`, "BR/EDR not supported" `0x04` (i.e. "this is an LE-only device"). `FLAG_LE_LIMITED_DISCOVERABLE` is unused.
* `TEST_COMPANY_ID = 0xFFFF` (37).

### 4.2 `AdError` (`ble_ad.py:40-41`)
Empty `Exception` subclass; **not** a `ValueError`. The CLI therefore catches both (`bluforge_cli.py:132`), and a comment there explains why.

### 4.3 `ad_struct(ad_type, data)` (`:44-49`)
* **What:** the basic building block: returns `bytes([len(data) + 1, ad_type]) + data`.
* **How:** check `ad_type` is 0..255 and `len(data) <= 253` (`AdError`), then prepend length and type. Example: Flags `ad_struct(0x01, b"\x06")` = `02 01 06`.
* **Gotchas:** the 253 limit is the single-structure limit (length byte max 255), *not* the 31-byte legacy limit; that limit is enforced separately by `check_legacy_len`, or by `legacy_adv_configure`.

### 4.4 Small encoders (`:52-84`)
| Function | Builds | Notes |
|---|---|---|
| `flags(value=0x06)` (52-53) | Flags: `02 01 06` by default (General Discoverable + BR/EDR Not Supported) | `value & 0xFF` so it never overflows |
| `complete_local_name(name)` (56-57) | type `0x09` + ASCII name | non-ASCII characters become `?` (`encode("ascii","replace")`; `"Hié"` -> `48 69 3f`, verified): silent downgrade; real BLE names are UTF-8 |
| `shortened_local_name(name)` (60-61) | type `0x08` | same ASCII caveat |
| `tx_power_level(dbm)` (64-67) | type `0x0A`, signed byte | accepts -127..127; -128 (valid for a signed byte) is rejected; unused by anything |
| `manufacturer_data(company_id, payload)` (70-73) | type `0xFF`: 2-byte company ID little-endian + payload | `AdError` if company ID > 16 bits |
| `service_data_16bit(uuid16, payload)` (76-79) | type `0x16`: 2-byte UUID LE + payload | |
| `complete_16bit_service_uuids(uuids)` (82-84) | type `0x03`: each UUID as 2 bytes LE | `[0x180F, 0xFEAA]` -> `05 03 0f 18 aa fe` (verified) |

### 4.5 `check_legacy_len(payload, label)` (`:87-91`)
Raises `AdError("<label> is N bytes, exceeds the legacy 31-byte advertising limit by M")` if `len(payload) > 31`, else returns the payload unchanged. Every profile ends with this call.

### 4.6 The profiles (`ble_ad.py:94-189`)
Each returns a 3-tuple `(adv_data, scan_rsp, description)`. All are built so `adv_data` fits in 31 bytes *by itself*, and `scan_rsp` is always empty `b""`.

**`profile_ibeacon(uuid_hex, major, minor, measured_power_dbm=-59, company_id=0x004C)` (100-115).** Apple's iBeacon: Flags + a Manufacturer Specific structure whose payload is `02 15` + 16-byte UUID + major (2 bytes, **big-endian**) +
minor (2, big-endian) + measured power (signed byte, the signal strength at 1 m, used by phones to estimate distance). Steps: strip dashes and `bytes.fromhex` the UUID, require 16 bytes (107-109); range-check major/minor (110-111); assemble (112-114);
length check; return. **Worked example (verified, 30 bytes):**

```
02 01 06                       Flags structure
1a ff                          length 0x1a = 26, type 0xFF (manufacturer data)
4c 00                          company ID 0x004C (Apple), little-endian
02 15                          iBeacon type 0x02, remaining length 0x15 = 21
e2 c5 6d b5 df fb 48 d2 b0 60 d0 f5 a7 10 96 e0     the 16-byte UUID
00 01                          major = 1   (big-endian)
00 01                          minor = 1
c5                             measured power 0xc5 = -59 dBm
```
Gotchas: the default company ID is Apple's; `PROFILES["ibeacon"]` (193-196) never passes a company ID, so the "override to `TEST_COMPANY_ID`" mentioned in the docstring is only reachable by calling the function directly.
A non-hex UUID raises `ValueError` (not `AdError`), and an out-of-range power raises `struct.error` (neither is caught by the CLI's `except (ValueError, AdError)`, verified at module level).

**`profile_eddystone_uid(namespace_hex, instance_hex, tx_power_dbm=-22)` (118-132).** Google Eddystone-UID: Flags + Complete list of 16-bit UUIDs `[0xFEAA]` + Service Data for UUID `0xFEAA` whose body is frame type `0x00`, TX power (signed byte, "calibrated at 0 m"),
10-byte namespace, 6-byte instance and 2 reserved zero bytes = 20 bytes (130). The docstring records that the two trailing RFU bytes were once missing (an 18-byte body that a strict scanner could reject). Default total: **31 bytes**, exactly the limit:
`02 01 06 03 03 aa fe 17 16 aa fe 00 ea 00112233445566778899 aabbccddeeff 00 00` (verified). `ValueError` for non-hex input; `AdError` for wrong namespace/instance length.

**`profile_eddystone_url(url, tx_power_dbm=-22)` (135-165).** Google Eddystone-URL (frame type `0x10`) with the spec's URL compression: a one-byte scheme code (`http://www.`=0, `https://www.`=1, `http://`=2, `https://`=3) and one-byte expansions for common suffixes
(`.com/`=0 ... `.gov`=13; the list at 139-140 is ordered so the longer `.com/` is tried before `.com`). Steps: find the matching scheme prefix or raise `AdError` (143-149); walk the remainder, at each position trying the suffix table, otherwise emitting the ASCII byte (150-162);
frame = `10`, power, scheme code, encoded bytes. **Worked example (verified):** `https://candelatech.com` -> scheme `03`, the 11 ASCII bytes of `candelatech`, then `07` (".com") = 26 bytes total:
`02 01 06 03 03 aa fe 12 16 aa fe 10 ea 03 63 61 6e 64 65 6c 61 74 65 63 68 07`. Capacity: 17 URL bytes after compression; `https://candelatech.com/products/ble` fails with `AdError` ("38 bytes, exceeds ... by 7", verified).
Gotchas: a non-ASCII character makes `bytes()` raise `ValueError`; control characters below 0x0E would be ambiguous with expansion codes (not checked).

**`profile_altbeacon(beacon_id_hex, mfg_reserved=0, tx_power_dbm=-59, company_id=TEST_COMPANY_ID)` (168-177).** AltBeacon (open spec): Flags + Manufacturer Specific data whose payload is `be ac` + 20-byte beacon ID + reference RSSI + 1 reserved byte. Default total **31 bytes** (exactly the
limit; verified). The company ID is the test ID `0xFFFF`.

**`profile_generic_sensor(name, sensor_id, value_centiunits, company_id=TEST_COMPANY_ID)` (180-189).** A project-private demo: Flags + manufacturer data (`sensor_id` 1 byte + `int16` little-endian value) + a Shortened Local Name cut to 10 characters. For `("TempSensor", 7, 2550)`: `02 01 06 06 ff ff ff 07 f6 09 0b 08 54 65 6d 70 53 65 6e 73 6f 72` (verified: 22 bytes, `2550 = 0x09f6`).
Gotchas: `value_centiunits` outside -32768..32767 raises `struct.error` (verified), not `AdError`.

### 4.7 `PROFILES` and `build_profile(name, cfg=None)` (`ble_ad.py:192-216`)
* **What:** the lookup table the CLI/GUI use. `PROFILES` maps each profile name (`ibeacon`, `eddystone_uid`, `eddystone_url`, `altbeacon`, `generic_sensor`) to a `lambda cfg:` that reads settings from a dict with defaults
  (e.g. iBeacon UUID `E2C56DB5DFFB48D2B060D0F5A71096E0`, major/minor 1, power -59) and calls the matching `profile_*` function. `build_profile` looks the name up (`AdError` if unknown), calls it and returns
  `{"adv_data": bytes, "scan_rsp": bytes, "label": str}`.
* **Where it is used:** `bluforge_cli.py:81` (`build_advdata`) and 556, 598 (the `caps` listing and the `--profile` choices); `verify_adv_profiles.py` cycles every profile through a real radio.
* **Gotchas:** every value is converted with `int(...)`, because `--profile-cfg k=v,k=v` supplies strings (`bluforge_cli.py:111-116`). Config keys not listed in a lambda are silently ignored (e.g. there is no `company_id` key). The default profile sizes are
  ibeacon 30, eddystone_uid 31, eddystone_url 26, altbeacon 31, generic_sensor 20 bytes (verified).

### 4.8 Self-test (`ble_ad.py:219-294`)
Asserts exact byte layouts (Flags `02 01 06`, the manufacturer structure, a full iBeacon decode, Eddystone-URL suffix round trip) and the 31-byte limit, then prints each default profile in hex. Safe to run (I ran it on a copy: "ble_ad self-test OK").

---

## 5. Things I noticed

Grouped by how much they could hurt. Everything here was either verified by running the pure-Python code against a fake socket (marked *verified*) or read directly from the lines cited.

### 5.1 Real risks and bugs

1. **Validation after side effects (verified).** `legacy_adv_configure` checks `scan_rsp` (type and length) only *after* it has already sent LE Set Advertising Parameters and LE Set Advertising Data (`hci_raw.py:544-557`), so a `ValueError` leaves the
   controller half-configured. `tx_test_start` calls `tx_test_end()` *before* validating `phy` and `power_level` (`:436-454`), so a bad value stops a currently running test and then raises.
2. **Wrong exception types.** Bad `phy` / `modulation_index` raise `KeyError` (`hci_raw.py:440, 483-484`); `ext_scan_configure(interval_ms>~40959)` raises `struct.error` (verified); `ble_ad` profile builders raise `struct.error` for out-of-range
   power or sensor value (`ble_ad.py:113, 130, 163, 175, 187`; verified for `power=200`, `value=40000`), and `ValueError` for non-hex strings. The CLI's `except (ValueError, ble_ad.AdError)` (`bluforge_cli.py:132`) does not catch `struct.error`
   or `KeyError`, so a bad `--profile-cfg power=200` ends in a traceback instead of a clean message. (It happens before `open()`, so no radio is leaked.)
3. **`open()` can report success on a dead controller.** `_reset_and_enable_event_masks` swallows *every* `HciError`, including timeouts (`hci_raw.py:322-332`), and the two mask commands share one `try`, so a failure of Set Event Mask skips LE Set Event Mask.
   Per the source's own comments, a missing event mask makes scanning silently deliver zero reports.
4. **Radio not handed back on some failures after bind.** `force_recover` is only called when the *bind* fails (`:280-292`). If `_reset_and_enable_event_masks` raises something other than `HciError` (e.g. `OSError` from `send` if the controller disappears), `open()` exits with
   the socket still bound and without restoring the radio. Likewise `force_recover`/`btmgmt_power` can raise `OSError` and mask the intended `HciError`.
5. **Fragment reassembly has no staleness handling** (`:702-723`): a lost final fragment is glued to the next report with the same `(address, SID, scan-rsp)` key.
6. **Legacy parser has no bounds checks** (`:637-668`): a truncated or corrupt report raises `IndexError`/`struct.error` (verified), which propagates out of `read_events()` and (via the CLI's catch-all) ends the sniff job. The extended parser checks the 24-byte header but not the data length.
7. **`close()` result is discarded by `__exit__` and by the CLI's `safe_close`**, so a failed restore is still reported to the operator as "restored" in those paths (the very problem `close()`'s docstring says it fixed).

### 5.2 Inconsistencies and misleading text

8. **Advertising interval floor** (`hci_raw.py:521-532`): the comment says 100 ms is the minimum "for non-connectable/scannable advertising specifically", but the code applies it to *all* types and clamps silently (20 ms and 0 both become 100 ms, verified).
   So connectable/directed advertising cannot be tested below 100 ms. (spec, from memory: newer spec versions allow 20 ms.)
9. **Stale module docstring** (`hci_raw.py:1-18`): mentions `release()` (does not exist), "LE Transmitter Test v1" and "Extended Advertising Parameters/Data/Enable" (no methods), and "BE200 / 8 controllers".
10. **iBeacon impersonation vs the docstring** (`ble_ad.py:10-12` vs `100-106`): the module claims nothing defaults to a real vendor's ID, but iBeacon defaults to Apple's `0x004C`, and `PROFILES` offers no way to change any profile's company ID.
11. **Inconsistent defaults and checks**: `scan_configure` has `active=True` and an upper-bound check, `ext_scan_configure` has `active=False` and no upper-bound check (`:564-598`). `send_cmd`'s `expect_complete` parameter is never used (`:380`).
    Legacy reports pass RSSI 127 through as a number; extended ones turn it into `None`. `event_type` has two different meanings depending on the `ext` flag.
12. **The smoke test defaults to `hci7`** (`:751`) and is described as "read-only" though it resets and detaches the radio (`:747-756`).
13. **`tx_test_packet_duration_us`** appears to ignore the second preamble byte of LE 2M (spec, from memory) and is unused by the project (`ble_channels.py:105-116`). The Wi-Fi overlap for `width_mhz=40` assumes the Wi-Fi channel number is the centre of the 40 MHz block (`:95-101`), which is not how 40 MHz channels work.
14. **Duplicated constants across files**: advertising frequencies and channel-map bits live in `ble_channels.py:28-29` *and* `bluforge_cli.py:71, 107`; `HCI_CHANNEL_MONITOR`/`HCI_DEV_NONE` live in `hci_raw.py:63,65` *and* `acl_meter.py:25-26`; `acl_meter.py` imports the private `_hci_bind`.
15. **Non-ASCII names are silently turned into `?`** (`ble_ad.py:56-61`), although BLE local names are UTF-8.

### 5.3 Dead or unused code (not harmful, but misleading to a newcomer)

16. `HciSocket._set_default_filter` (`hci_raw.py:334-349`), `_opened_user_channel` (written, never read), `OGF_INFO_PARAM`, `HCI_CHANNEL_RAW/MONITOR`, `SOL_HCI`/`HCI_FILTER` (only the dead method), `HCI_EVENT_PKT` (only the dead method), `LE_EXTENDED_ADVERTISING_REPORT` alias, the `EXT_CONNECTABLE/SCANNABLE/DIRECTED/LEGACY`
    constants, and most of the `OCF` table (random address, v1 TX/RX test, all extended-advertising opcodes).
17. In `ble_channels.py`: `ADV_CHANNEL_MAP_BIT`, `is_advertising_rf`, `freq_mhz_to_rf`, `rf_to_ll_index`, `ll_index_to_rf`, `all_channels` (other than via the overlap helper), and `tx_test_packet_duration_us` are only used by their own self-test. Only `rf_to_freq_mhz`,
    `wifi_channel_center_mhz` and `ble_channels_overlapping_wifi` are used by the CLI.
18. In `ble_ad.py`: `tx_power_level`, `AD_TYPE_INCOMPLETE_16BIT_UUIDS`, `AD_TYPE_SLAVE_CONN_INTERVAL_RANGE`, `AD_TYPE_APPEARANCE`, `FLAG_LE_LIMITED_DISCOVERABLE` are unused.

### 5.4 Operational notes that bite newcomers

* **A radio is exclusive.** While a `bluforge_cli.py` job holds `hci0`, BlueZ (and therefore the Classic tab and `bluetoothctl`) cannot see `hci0`. Do not start a second job on the same radio; and never `kill -9` a job: use SIGINT/SIGTERM so `close()` can run (the whole of `force_recover` exists because of a SIGKILL incident).
* **Timing budgets to expect:** `open()` up to ~3 s of bind retries; `close()` ~0.1 s plus up to 5 s of power-on retries; every `send_cmd` can block up to 5 s on a silent controller. A long pause is usually one of these, not a hang.
* **Opcode table is OGF-less.** If you add a command, pass the right `OGF_*` at the call site; `SET_EVENT_MASK` and `LE_SET_EVENT_MASK` share the number `0x0001`.
