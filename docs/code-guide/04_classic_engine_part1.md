# 04 - The Classic engine, part 1 (`src/bluforge_classic.py`, lines 1-1308)

This chapter explains the first two thirds of the file that does all the Bluetooth **Classic** work
in BluForge: everything from the top of the file down to (not including) `def job_a2dp` at line 1309.
The long-running "jobs" (`job_a2dp`, `job_avrcp_watch`, `job_cycle`, `job_hfp`) and the command-line
parser / `main()` are in part 2 and are documented in another chapter. All line numbers below are for
`src/bluforge_classic.py` as it is today, written like `bluforge_classic.py:969`.

Everything here was written by reading the code. Where I am unsure how BlueZ or PipeWire behave (as
opposed to what the Python does), I say so. Nothing was run against a radio.

---

## 1. Overview

### 1.1 What the Classic engine is

BluForge's architecture is:

```
browser GUI  --HTTP-->  src/server.py  --starts a subprocess-->  src/bluforge_classic.py
                                                                       |
                         +---------------------------------------------+---------------------+
                         | system D-Bus (via Gio)                                            | pw-* tools via runuser
                         v                                                                   v
                     bluetoothd (BlueZ)  ---->  Bluetooth radio  hciN                    PipeWire (desktop user's session)
```

`bluforge_classic.py` is a **command-line program that does one thing per run and then exits**.
`server.py` builds an argument list such as

```
python3 -u src/bluforge_classic.py --hci 1 connect 5C:35:89:B4:55:93
```

(see `build_classic_argv`, `server.py:472`), runs it, reads what it printed, and shows it in the GUI.

Why it exists: the other helper, `bluforge_cli.py`, takes a radio away from the operating system
(`HCI_CHANNEL_USER`) so it can send raw Bluetooth Low Energy test commands. Bluetooth **Classic**
(also called BR/EDR, the "old" Bluetooth used for headphones, speakers and car kits) needs the real
Bluetooth stack: pairing, service discovery, audio endpoints, remote-control. So this program leaves the
radio under **BlueZ** and talks to it politely. A radio is therefore either a "BLE-engine radio" or a
"Classic radio", never both (`server.py` enforces that with a "reservation"; see the file docstring at
`bluforge_classic.py:6-11`).

Roles: the lab computer plays the **audio source** (A2DP Source), the **remote-control target**
(AVRCP Target) and the **phone-side of a call** (HFP Audio Gateway). The device under test (**DUT**) is
the headset or speaker (`bluforge_classic.py:13-16`).

### 1.2 The two kinds of command

| Kind | Where defined | Lifetime | Result |
|---|---|---|---|
| **One-shot action** (`adapter`, `scan`, `devices`, `info`, `pair`, `connect`, `disconnect`, `remove`, `avrcp-player`, `avrcp-volume`, `interrogate`) | `act_*` functions, table `ACTIONS` (`:1162`) - **part 1** | seconds | one JSON object |
| **Long-running job** (`a2dp`, `avrcp-watch`, `cycle`, `hfp`) | `job_*`, table `JOBS` - part 2 | up to hours | stream of lines, ends with a summary |
| `preflight` | `act_preflight` (`:788`) - part 1 | instant | one JSON object; special-cased in `main()` (needs no radio) |

### 1.3 The output protocol (what the program prints)

The docstring at `bluforge_classic.py:18-21` states it; the helpers are `say()` (`:74`) and `emit()` (`:78`).

1. **Progress lines** for humans: `[*]` doing something, `[+]` success, `[!]` warning / degraded,
   `[x]` failure. Same convention as `bluforge_cli.py`.
   ```
   [*] connecting 5C:35:89:B4:55:93 via hci1
   [+] connected in 2310 ms
   ```
2. **Machine lines** (jobs only): one JSON object per line with a key `"m"` telling the GUI what it is,
   e.g. `{"m":"sample","t":12.0,"rssi":0,...}`, `{"m":"event","t":3.21,"event":"ready",...}`,
   `{"m":"summary","pass":true,...}`. In part 1 these are produced only by `Recorder.event()`,
   `Recorder.sample()` and `verdict()` (`:1257-1299`).
3. **Final result line** (one-shot actions): the **last** line printed is a JSON object with `"ok": true`
   or `"ok": false`. `main()` prints it; on failure it is `{"ok": false, "error": "..."}`.

The server finds the result by scanning the output **from the end** for the last line that starts with
`{`, and treats every other line as the log (`run_classic_action`, `server.py:567`; the reverse scan is at `:600`). That is why
progress lines never start with `{`.

Exit codes (from `main()` and `verdict()`): `0` = fine / test passed, `1` = the engine could not do what
was asked (`ClassicError`) or crashed, `2` = a *test job* ran but its verdict was FAIL (`:1305`).

Illustrative one-shot output (shape taken from `act_connect`, values made up):

```
[*] connecting 5C:35:89:B4:55:93 via hci1
[+] connected in 2310 ms
{"ok": true, "connect_ms": 2310.4, "transport_ms": 840.2, "device": {"addr": "5C:35:89:B4:55:93", ...}}
```

### 1.4 Glossary (read this once)

| Term | Meaning |
|---|---|
| **BlueZ**, **bluetoothd** | The Bluetooth stack on Linux. `bluetoothd` is the background service (daemon) that owns the radios, remembers paired devices and implements the profiles. We never talk to the radio directly here; we ask `bluetoothd`. |
| **D-Bus** | Linux's message bus between programs. `bluetoothd` publishes **objects** on the **system bus** under the name `org.bluez`. Each object has a **path**, e.g. `/org/bluez/hci1/dev_5C_35_89_B4_55_93`, and one or more **interfaces** with **methods**, **properties** and **signals**. |
| **Interface** | A named group of methods/properties, e.g. `org.bluez.Device1` (a remote device), `org.bluez.Adapter1` (our radio). |
| **Gio / GLib / PyGObject (`gi`)** | GNOME's C library and its Python binding. `Gio` has the D-Bus client; `GLib.Variant` is how values are packed for D-Bus. The only non-standard-library dependency (`:23`). |
| **GVariant type string** | A tiny code for a D-Bus type: `s` string, `b` bool, `o` object path, `u` uint32, `q` uint16, `v` "variant" (any type), `(ss)` a tuple of two strings, `a{sv}` a dictionary string -> variant. Used in `GLib.Variant("(ss)", (...))`. |
| **hciN / controller / adapter / radio** | One Bluetooth chip. `hci1` is the second one; BlueZ exports it as `/org/bluez/hci1`. |
| **BR/EDR vs LE** | The two halves of Bluetooth. BR/EDR = "Classic" (audio, serial); LE = "Low Energy" (sensors, beacons, new audio). |
| **Inquiry** | Classic's word for scanning for nearby devices (LE calls it advertising/scanning). BlueZ method `StartDiscovery`. |
| **Pairing / bonding / trust** | Exchanging keys so two devices can encrypt. **Trusted** = BlueZ may accept that device's connections without asking us. |
| **Agent** | A program that answers BlueZ's questions during pairing ("what is the PIN?", "do these numbers match?"). |
| **SSP "Just Works" / legacy PIN** | Two Classic pairing styles: modern devices pair silently; old ones need a PIN such as `0000`. |
| **Profile** | A defined job a device can do over Bluetooth. |
| **A2DP** | Advanced Audio Distribution Profile: stereo music streaming. *Source* sends audio (us), *Sink* plays it (the DUT). |
| **AVRCP** | Audio/Video Remote Control Profile: play/pause/next buttons and volume. *Controller* sends commands (headset buttons), *Target* receives them (a phone/player). |
| **HFP** | Hands-Free Profile: phone calls. *Audio Gateway* (AG) is the phone side (us), *Hands-Free* the headset. |
| **ACL / SCO / eSCO** | The two link types under a Classic connection: ACL carries data (and A2DP), SCO/eSCO carry call audio. |
| **SDP** | Service Discovery Protocol: ask a Classic device "which profiles do you offer?". Answers are **SDP records**. |
| **GATT** | Generic Attribute Profile: the LE device's database. **Services** contain **characteristics** (values such as "Battery Level"), which can have **descriptors**. |
| **UUID** | A 128-bit identifier. Standard ones are a 16-bit number inside a fixed "Bluetooth base" pattern: `0000110b-0000-1000-8000-00805f9b34fb` is the 16-bit value `0x110B` (A2DP Sink). |
| **PipeWire / WirePlumber** | The desktop's audio system and its "session manager". WirePlumber watches BlueZ and creates the audio objects for a connected headset. |
| **PipeWire card / sink / node** | *Card* = the headset as a device with **profiles** (e.g. `a2dp-sink`, `headset-head-unit`, `off`). *Sink* = the audio output you play into. *Node* = any audio endpoint in the graph. |
| **pw-dump, pw-cli, pw-cat, pw-top** | PipeWire command-line tools: dump the object graph as JSON, send commands, play raw audio from stdin, show per-node load and errors. |
| **xrun** | Buffer under/overrun in the audio path (an audible glitch). `pw-top` counts them in its `ERR` column. |
| **Codec** | How audio is compressed for Bluetooth: **SBC** (mandatory, always available), **AAC**, **aptX / aptX-HD / LDAC** (vendor codecs). |
| **Bitpool** | An SBC quality knob; higher = more bits per frame. |
| **RSSI** | Received signal strength. For a Classic link the number from `hcitool rssi` is **not dBm**: it is an **offset in dB from the "golden receive power range"** (0 = inside the range, negative = too weak). Part 2 labels it so at `:1534`. |
| **Link quality** | A 0-255 number from the controller, meaning is vendor-specific. |
| **evdev / uinput** | Linux's input-device layer. BlueZ turns the headset's AVRCP buttons into a fake keyboard (`uinput`), which the program reads from `/dev/input/eventN`. |
| **runuser** | Run a command as another user. Needed because the radio side runs as root but PipeWire belongs to the desktop user. |

### 1.5 Map of part 1

| Lines | Section | What it is |
|---|---|---|
| 1-57 | Header, imports, constants | module docstring, optional `gi`, paths, regex |
| 59-94 | Interface names, `STOP`, `say`, `emit`, `ClassicError`, `die`, `norm_addr` | tiny foundations |
| 96-219 | `_bz_error_text`, `class BZ` | the D-Bus wrapper |
| 221-229 | `pump` | let D-Bus callbacks run while waiting |
| 232-285 | `Agent` | pairing agent |
| 288-384 | device helpers, discovery | `human_dev`, `UUID_NAMES`, `classic_devices`, `adapter_info`, `ensure_powered`, `discover`, `ensure_device` |
| 387-432 | link metrics | `_run`, `link_stats`, `acl_links`, `hci_counters` |
| 435-491 | codec decoding | `VENDOR_CODECS`, `codec_name`, `decode_sbc`, `transport_info` |
| 494-597 | PipeWire access | `Audio`, `pick_a2dp_profile` |
| 600-784 | audio sources | `gen_pcm`, `read_wav`, `Streamer`, `probe_media`, `FfmpegStreamer`, `XrunMonitor` |
| 787-1019 | one-shot actions | `act_preflight` ... `act_avrcp_volume` |
| 1022-1159 | interrogate | GATT + SDP helpers, `act_interrogate` |
| 1162-1167 | `ACTIONS` | name -> function table |
| 1170-1242 | AVRCP key reader | `KEYMAP`, `find_avrcp_evdev`, `KeyWatcher` |
| 1245-1305 | run bookkeeping | `Recorder`, `stats`, `verdict` |

### 1.6 What part 1 takes from the other two modules

`bluforge_classic.py:54-55` does `import ble_decode` and `import acl_meter` (plain imports; they work
because Python puts the script's own folder first on the search path).

| Module | Names used in part 1 | Used at |
|---|---|---|
| `ble_decode` | `BASE_UUID_TAIL` | `_short_uuid` (`:1033`) |
| | `SERVICES`, `CHARACTERISTICS`, `DESCRIPTORS` (number -> name tables) | `_gatt_tree` (`:1051,1057,1062`) |
| | `value_text(short, bytes)` -> `(hex, decoded text)` | `_gatt_tree` (`:1070`) |
| | `parse_sdp_records(text)` | `_sdp_records` (`:1086`) |
| | `APPEARANCE`, `CLASSIC_PROFILES` | `act_interrogate` (`:1124,1131,1141`) |
| `acl_meter` | nothing in part 1 | imported here, used only by `job_a2dp` in part 2 (`AclMeter`, `rates_for`) |

Consequence worth knowing: because `import acl_meter` is at module level, and `acl_meter.py` itself does
`from hci_raw import ...`, **even `preflight` fails with a traceback if `hci_raw.py` cannot be imported**,
before `main()`'s friendly error handling exists. See section 18.

---

## 2. File header (`:1-57`)

### Module docstring (`:2-27`)
Documents the role, the output protocol (section 1.3) and the dependencies: standard library +
PyGObject; optional `hcitool`/`hciconfig` (old "bluez-deprecated" tools used for RSSI/TX-power/link-quality
and throughput) and `pactl/pw-cat/pw-top` for audio. "Optional" means the code checks with
`shutil.which()` and degrades to `None` values instead of failing.

### Imports (`:28-44`)
All standard library: `argparse` (part 2), `csv`/`json` (results), `math`/`random`/`struct` (audio
signals, SBC maths, input events), `os`/`pwd` (users, files), `re`, `select` (waiting on an input
device), `shutil` (`which`), `socket` (part 2's RFCOMM/SCO sockets), `signal` (part 2), `subprocess`,
`sys`, `threading`, `time`.

### Optional `gi` import (`:46-51`)
```python
try:
    from gi.repository import Gio, GLib
    HAVE_GI = True
except Exception as _e:          # reported by `preflight`, never a traceback
    HAVE_GI = False
    _GI_ERR = str(_e)
```
If PyGObject is missing the program still starts, so that `preflight` can *report* it. `BZ.__init__`
turns the missing library into a clean error (`:108`). `_GI_ERR` exists only when the import failed.

### Paths and regex (`:53-57`)
- `HERE` (`:53`): folder of this file (`.../src`).
- `LOG_DIR` (`:56`): where results are saved. `$BLUFORGE_LOGS` if set, otherwise `../logs` next to `src`.
  `Recorder.save()` writes under `LOG_DIR/classic`; `server.py:46` reads the same folder.
- `ADDR_RE` (`:57`): matches `AA:BB:CC:DD:EE:FF` (upper or lower case hex, colons).

### D-Bus names (`:59-65`)
String constants so typos are caught in one place:

| Constant | Value | Meaning |
|---|---|---|
| `BLUEZ` | `org.bluez` | the bus name BlueZ registers |
| `IF_ADAPTER` | `org.bluez.Adapter1` | our radio |
| `IF_DEVICE` | `org.bluez.Device1` | a remote device |
| `IF_TRANSPORT` | `org.bluez.MediaTransport1` | an active A2DP audio connection (codec, state, volume) |
| `IF_PLAYER` | `org.bluez.MediaPlayer1` | a remote AVRCP media player (a phone's music app) |
| `IF_PROPS` | `org.freedesktop.DBus.Properties` | standard `Get`/`Set`/`GetAll` for any object |
| `IF_OM` | `org.freedesktop.DBus.ObjectManager` | standard "list every object" (`GetManagedObjects`) |

---

## 3. Foundations (`:67-94`)

- **`STOP`** (`:67`) - a `threading.Event`, the program-wide "please stop" flag. Part 2's `main()` sets it from
  the SIGINT/SIGTERM handler `_on_signal` (`:70`). Every loop in this file that can run long checks
  `STOP.is_set()` (`pump`, `discover`, `wait_for`, `Streamer.run`, ...). One notable exception:
  `BZ.call_loop` does not (section 4).
- **`say(tag, msg)`** (`:74`) - prints `[tag] msg` with `flush=True` so the server sees lines immediately.
- **`emit(**kw)`** (`:78`) - prints one compact JSON line (`separators=(",", ":")`, `default=str` so
  odd objects such as bytes or paths become strings instead of crashing).
- **`ClassicError`** (`:82`) / **`die(msg)`** (`:86`) - the single way expected failures are reported.
  `die()` just raises `ClassicError(msg)`; `main()` (part 2) catches it, prints `[x] msg` and
  `{"ok": false, "error": msg}`, exits 1. The message text is what the user sees in the GUI, so many
  messages are written as advice ("pair it first", "is the radio in range?").
- **`norm_addr(a)`** (`:90`) - validates against `ADDR_RE` and returns upper case. BlueZ object paths use
  upper case (`dev_5C_35_89_B4_55_93`), so every action starts with this. Input `None` or junk -> `die`.

---

## 4. `_bz_error_text` and `class BZ` - the D-Bus wrapper (`:96-219`)

### `_bz_error_text(e)` (`:97`)
A failed D-Bus call raises `GLib.Error` whose text looks like
`GDBus.Error:org.bluez.Error.AlreadyConnected: Already Connected (36)`. The regex pulls out the error name
and message, strips the `org.bluez.Error.` prefix and the trailing `(36)` and returns
`AlreadyConnected: Already Connected`. If the pattern does not match (e.g. the bus itself failed), it
returns `str(e)` unchanged. This is why GUI errors read like "Failed: ..." rather than a long GDBus string.

### `class BZ` (`:104`) - "thin synchronous wrapper over the system bus for one adapter"
"Synchronous" = each call blocks until BlueZ answers. Simple, and right for a program that does one thing
at a time.

**`__init__(hci)`** (`:107`)
1. If `gi` is missing -> `die` with install advice.
2. Remember `self.hci` and `self.adapter = "/org/bluez/hci%d"` (e.g. `/org/bluez/hci1`).
3. `Gio.bus_get_sync(Gio.BusType.SYSTEM, ...)` connects to the system bus -> `self.conn`.
4. `self.exists(self.adapter)`; if BlueZ does not export that object, `die("hciN is not managed by
   bluetoothd (is bluetooth.service running, and is this radio free of a BLE job?)")`. The second half of
   that message is the usual real cause: the BLE engine has taken the radio.

**`dev_path(addr)`** (`:120`) - builds the device object path:
`"5C:35:89:B4:55:93"` on hci1 -> `/org/bluez/hci1/dev_5C_35_89_B4_55_93`. Every device-related call uses it.
It assumes upper case (guaranteed by `norm_addr`).

**`call(path, iface, method, params=None, timeout_ms=30000)`** (`:123`) - the workhorse.
`conn.call_sync(...)` sends the method call and waits. Errors become `ClassicError`
(through `_bz_error_text`). A successful reply is `unpack()`ed into plain Python values. **D-Bus replies are
always tuples**, which is why callers write `[0]` all over the file: `GetAll` returns `({...},)`.
Example (what `BZ.get` does for "is the device connected?"):

```python
self.call("/org/bluez/hci1/dev_5C_35_89_B4_55_93", "org.freedesktop.DBus.Properties",
          "Get", GLib.Variant("(ss)", ("org.bluez.Device1", "Connected")))   # -> (True,)
```

**`call_loop(path, iface, method, params, timeout_ms)`** (`:131`) - the same idea, but **asynchronous
while still looking synchronous to the caller**. Why: during `Pair`, BlueZ calls *back into us* (our
Agent, section 6) on the same D-Bus connection. A blocking `call_sync` would stop this thread from ever
answering, so pairing would deadlock against our own agent (the docstring says exactly this). How it works:
1. Create a `GLib.MainLoop` and a dict `res`.
2. Start `conn.call(..., done)`; `done` stores the result (`res["v"]`) or the error (`res["e"]`) and quits the loop.
3. Add a safety timer `timeout_ms + 3000` that also quits the loop.
4. `loop.run()` - while it runs, GLib dispatches incoming D-Bus traffic, including the Agent's methods.
5. Error -> `ClassicError`; no result at all -> `die("timed out waiting for Pair")`; else unpack.
Used for `Pair` (`:879`), `Connect` (`:901`, `:1114`). Gotcha: it does **not** look at `STOP` (a Stop request
during a connect is only honoured when the call ends), and the 3-second safety timer stays registered
after a normal finish (harmless: it later quits a loop that already ended).

**`exists(path)`** (`:154`) - "is this object currently on the bus?" Reads the `Address` property
(`Adapter1` if `path` is the adapter, otherwise `Device1`) and returns `True`/`False`. Gotcha: it catches
**every** `ClassicError`, so a timeout or permission problem looks the same as "not there". And it only
understands adapters and devices (a transport path would always give `False`).

**`props(path, iface)`** (`:163`) - `Properties.GetAll` -> dict, or `None` on error. Example result for
`org.bluez.Device1` (abridged): `{"Address": "5C:35:89:B4:55:93", "Alias": "WH-1000XM4", "Paired": True,
"Connected": False, "UUIDs": ["0000110b-0000-1000-8000-00805f9b34fb", ...], "RSSI": -57, ...}`.
(Illustrative values.)

**`get(path, iface, name, default=None)`** (`:169`) - one property, `default` on any error.

**`set(path, iface, name, variant)`** (`:175`) - `Properties.Set`. Needs the value as a typed
`GLib.Variant`, e.g. `GLib.Variant("b", True)`. Unlike `get`, errors are **not** swallowed.

**`managed()`** (`:178`) - `ObjectManager.GetManagedObjects` on `/`: the *entire* BlueZ tree as
`{path: {interface: {property: value}}}`. Several helpers below filter this. Cost note: it returns every
adapter, device and child object each time (see section 18).

**`device_objects(addr)`** (`:181`) - from `managed()`, keep the device object and all children
(paths starting with `<device path>/`): e.g. `.../dev_X`, `.../dev_X/sep1/fd0` (transport),
`.../dev_X/player0`, GATT objects `.../dev_X/service000a/char000b`.

**`transport(addr)`** (`:187`) / **`player(addr)`** (`:193`) - find the first child that has
`MediaTransport1` / `MediaPlayer1`; return `(path, properties)` or `(None, None)`.
A transport path looks like `/org/bluez/hci1/dev_5C_35_89_B4_55_93/sep1/fd0` and only exists while an A2DP
stream is set up.

**`subscribe(handler)`** (`:199`) - register for three D-Bus **signals** from `org.bluez`:
`PropertiesChanged` (a property changed), `InterfacesAdded`, `InterfacesRemoved` (an object appeared /
vanished). The three inner callbacks normalise them into one `handler(kind, path, a, b)`:

| kind | `a` | `b` |
|---|---|---|
| `"props"` | interface name | dict of changed properties (e.g. `{"Connected": False}`) |
| `"added"` | list of interface names | their properties |
| `"removed"` | list of interface names | `None` |

Gotchas: it subscribes to **all** adapters and devices (the handler must filter by `path`, as
`act_avrcp_volume` does at `:991`); the subscription ids are discarded, so there is no unsubscribe; and
callbacks only run when someone spins the GLib main loop (`pump`, below).

---

## 5. `pump(seconds)` (`:221`)

"Sleep, but keep D-Bus signal callbacks alive." Loop until the time is up or `STOP` is set: drain pending
GLib events with `ctx.iteration(False)` (non-blocking), then `time.sleep(0.05)`. Without this, the
callbacks registered by `BZ.subscribe` would never fire because nothing runs the main loop. Granularity
is 50 ms. Use `pump` wherever signals matter; use plain `time.sleep` where they do not.

---

## 6. The pairing agent (`:232-285`)

Background: when two Classic devices pair, BlueZ may need an answer from a human (PIN, "do the six digits
match?", "allow this device to use A2DP?"). Whoever answers is the **agent**, registered with BlueZ over
D-Bus. This agent has **no input and no display** (`NoInputNoOutput`) and says yes to everything: a *lab
tool*, as its docstring says (`:249`).

- **`AGENT_PATH`** (`:233`) = `/org/bluforge/agent` - where *our* program exposes the agent object on the bus.
- **`AGENT_XML`** (`:234-243`) - machine-readable description of the `org.bluez.Agent1` interface (the
  methods BlueZ may call): `Release`, `RequestPinCode`, `DisplayPinCode`, `RequestPasskey`,
  `DisplayPasskey`, `RequestConfirmation`, `RequestAuthorization`, `AuthorizeService`, `Cancel`. Gio needs
  this XML to publish the object.

### `class Agent` (`:246`)
Used as a context manager: `with Agent(bz, a.pin): ...` (`act_pair`, `:878`).

- **`__init__(bz, pin="0000")`** (`:251`) stores the connection and PIN.
- **`_method(conn, sender, path, iface, method, params, inv)`** (`:256`) - called by Gio each time BlueZ
  calls one of our methods. Prints `[*] agent: <method> <extra args>`, then answers:

  | BlueZ asks | We answer |
  |---|---|
  | `RequestPinCode` | the PIN string (`"0000"` by default) |
  | `RequestPasskey` | the PIN as a number (`int(pin)` if all digits, else `0`) |
  | anything else (`RequestConfirmation`, `RequestAuthorization`, `AuthorizeService`, `DisplayPinCode`, `DisplayPasskey`, `Cancel`, `Release`) | an empty reply = "yes / OK" |

  (The first argument, the device path, is skipped in the log line, so `RequestPinCode` prints with no
  device.)
- **`__enter__`** (`:266`) 1) parse the XML, 2) `conn.register_object(AGENT_PATH, info, self._method, ...)`,
  3) tell BlueZ `AgentManager1.RegisterAgent(AGENT_PATH, "NoInputNoOutput")` on `/org/bluez`, 4) also
  `RequestDefaultAgent` so BlueZ picks *us* rather than another agent; if that fails it only warns
  (`[!] could not become default agent`) and continues.
- **`__exit__`** (`:278`) unregisters from BlueZ (errors ignored) and removes the local object.

Important: the agent's `_method` only gets to run while `call_loop` is spinning the main loop. That is the
whole reason `act_pair` uses `call_loop` for `Pair`.

---

## 6b. Small device helpers (`:288-345`)

### `human_dev(p)` (`:289`)
Turns the raw property dict of a `Device1` into the compact dict the GUI shows:
`addr, name (Alias, else Name, else ""), class ("0x%06x" Class-of-Device or None), icon (BlueZ's guess, e.g.
"audio-headset"), rssi, paired, trusted, connected (all coerced to bool), uuids (names via uuid_name),
modalias (vendor/product string such as "usb:v054Cp0D9Fd0001")`. `rssi` is typically only present while BlueZ has
recent inquiry data or a connection. Note the precedence in `"0x%06x" % p["Class"] if "Class" in p else None`: it means
`("0x%06x" % p["Class"]) if "Class" in p else None`, which is what was intended.

### `UUID_NAMES` and `uuid_name(u)` (`:301`, `:310`)
A small dictionary keyed by the **first 8 hex digits** of a 128-bit UUID. Examples: `"0000110b"` = "A2DP
Sink", `"0000111e"` = "Handsfree (HFP)", `"0000180f"` = "Battery Service". `uuid_name` returns
`"A2DP Sink (0000110b)"` for known ones and the raw UUID string otherwise. Gotcha: it does not check that
the rest of the UUID is the Bluetooth base pattern, so a vendor UUID that happens to start with a known
prefix would be mislabelled (compare `_short_uuid`, `:1030`, which does check). It also duplicates, with
slightly different wording, `ble_decode.CLASSIC_PROFILES` (section 18).

### `classic_devices(bz, only_bredr=True)` (`:315`)
All devices BlueZ currently has under *this* adapter. Steps: loop over `bz.managed()`; keep objects with a
`Device1` interface whose path starts with `"/org/bluez/hci1/dev_"` (the trailing `/dev_` stops `hci1`
matching `hci10`); if `only_bredr`, skip those whose `AddressType` is `"random"` (BR/EDR addresses are
always public; random addresses mean an LE device); run `human_dev`. Sorted: connected first, then paired,
then by name. Gotcha: an LE-only device with a *public* address still passes the "bredr" filter, so the
filter is approximate.

### `adapter_info(bz)` (`:326`)
`Adapter1` properties as `{hci, addr, name (the Alias), powered, pairable, discoverable, discovering, class, modalias}`.
`discoverable` = other devices can find us; `discovering` = we are currently scanning.

### `ensure_powered(bz)` (`:335`)
If `Powered` is false: print `[*] powering on hciN`, set `Powered` to `True`, then poll up to 20 x 0.25 s = 5 s for it to become true,
otherwise `die("hciN would not power on")`. Python's `for ... else` is used: the `else` branch runs only if
the loop was never `break`-ed. If BlueZ refuses (for example because the radio is blocked by rfkill), the
BlueZ error text is shown. Note `bz.get` returns `None` on error, which counts as "not powered".

---

## 7. Discovery (`:347-384`)

### `discover(bz, seconds, transport="bredr", want=None, quiet=False)` (`:347`)
"Run inquiry for `seconds` (or until `want` shows up). Returns devices."
1. `ensure_powered`.
2. `SetDiscoveryFilter` with `{"Transport": <bredr|le|auto>, "DuplicateData": False}`
   (`a{sv}` dictionary). If BlueZ rejects it, warn and scan unfiltered.
3. `StartDiscovery`. If BlueZ says `InProgress` (scan already running), warn and **join** it
   (`started` stays `False`, so we will not stop someone else's scan). Any other error is re-raised.
4. Wait: every 0.5 s, stop if time is up, `STOP` is set, or `want` is given and
   `bz.exists(bz.dev_path(want))` is true (BlueZ creates the `Device1` object the moment the first
   response arrives, so the wait ends early).
5. `finally:` `StopDiscovery` if *we* started it.
6. Return `classic_devices(...)` (BR/EDR only if `transport == "bredr"`).

Gotchas: the `quiet` parameter is never used (dead). BlueZ forgets devices it has merely *heard* (not
paired) after a while, so a device seen by `scan` may be gone by the time a later command runs; that is
what `ensure_device` is for. The wait does not `pump`, which is fine because nothing is subscribed here.

### `ensure_device(bz, addr, scan_s=20)` (`:379`)
If `Device1` for `addr` does not exist, print `[*] ... running inquiry (up to Ns)`, `discover(..., want=addr)`,
and `die` if it is still absent ("powered on? in range? in pairing mode?"). It uses the default **bredr**
transport, so it cannot find an LE-only device. `act_interrogate` (which supports LE) does its own version.

---

## 8. Link metrics (`:387-432`)

These shell out to the deprecated BlueZ command-line tools; each returns "nothing" if the tool is absent.

### `_run(cmd, timeout=5)` (`:388`)
`subprocess.run` with captured, text output; returns `(returncode, stdout + stderr)`. **Any** exception
(missing program, timeout) becomes `(127, "<exception text>")`. Note stdout and stderr are glued into one
string, so a stray stderr line ends up in the text the callers parse.

### `link_stats(hci, addr)` (`:396`)
Returns `{"rssi": ..., "tx_power": ..., "lq": ...}`, each an int or `None`. Runs, one after another
(3 s timeout each):

| key | command | text matched |
|---|---|---|
| `rssi` | `hcitool -i hci1 rssi 5C:35:89:B4:55:93` | `RSSI return value: -3` |
| `tx_power` | `hcitool -i hci1 tpl <addr>` | `transmit power level: 0` |
| `lq` | `hcitool -i hci1 lq <addr>` | `Link quality: 255` |

Needs a live ACL link to that address; otherwise hcitool prints an error, nothing matches, and the value
stays `None`. Remember: for Classic, `rssi` is an offset from the golden receive power range, **not dBm**
(glossary). Worst case this blocks 9 s.

### `acl_links(hci)` (`:412`)
Parses `hcitool -i hci1 con` lines such as `< ACL 5C:35:89:B4:55:93 handle 12 state 1 lm MASTER` (illustrative) into
`{"type", "addr", "handle", "role"}` dicts. The `handle` (the controller's number for the connection) is what
part 2 uses to attribute exact throughput to the DUT. The `<`/`>` direction is captured by the regex but
dropped. Docstring says tuples; it returns dicts (section 18).

### `hci_counters(hci)` (`:423`)
Reads `hciconfig hci1` and returns `(tx_bytes, rx_bytes, tx_acl_packets, rx_acl_packets)` from the lines
`RX bytes:N acl:N ...` / `TX bytes:N acl:N ...`, or `None`. These are totals since the controller came up;
part 2 subtracts two readings. It is the **fallback** when `acl_meter` (exact counting) is unavailable.

---

## 9. Codec decoding (`:435-491`)

A2DP negotiates a codec when the audio stream is configured. BlueZ shows the result on the transport as
`Codec` (a number) and `Configuration` (bytes).

### `VENDOR_CODECS` (`:436`)
Maps `(vendor id, vendor codec id)` -> name: `(0x004F, 0x0001)` aptX, `(0x00D7, 0x0024)` aptX-HD,
`(0x012D, 0x00AA)` LDAC, `(0x000A, 0x0002)` aptX-LL.

### `codec_name(codec, conf)` (`:440`)
`0` -> "SBC", `1` -> "MPEG-1,2 (MP3)", `2` -> "AAC". `0xFF` means "vendor codec": the configuration bytes
start with the vendor id (little-endian, 4 bytes; the code reads only the low 2: `conf[0] | conf[1] << 8`)
followed by the codec id (2 bytes at offset 4). Example: LDAC is `2d 01 00 00 aa 00 ...` -> vendor
`0x012D` (Sony), codec `0x00AA` -> "LDAC". Unknown vendor -> `"vendor 0x%04x/0x%04x"`; unknown plain
number -> `"codec 0x%02x"`.

### `decode_sbc(conf)` (`:454`)
Decodes the 4-byte SBC configuration into readable fields, plus an upper-bound bitrate.

| byte | bits | meaning (as the code reads them) |
|---|---|---|
| 0 | `0x80/0x40/0x20/0x10` | sample rate 16000 / 32000 / 44100 / 48000 Hz |
| 0 | `0x08/0x04/0x02/0x01` | mono / dual / stereo / joint stereo |
| 1 | `0x80/0x40/0x20/0x10` | block length 16 / 12 / 8 / 4 |
| 1 | `0x08/0x04` | 8 / 4 subbands (the allocation-method bits are ignored) |
| 2, 3 | all | minimum, maximum bitpool |

For a *negotiated* configuration exactly one bit of each group is set, so "first bit that matches" is
enough. Bitrate estimate (`:465-474`): SBC frame length
`4 + 4*subbands*channels/8 + ceil(...)` where the last term depends on the channel mode, using the
**maximum** bitpool; then `bitrate = 8 * frame_len * freq / (subbands * blocks)`. Worked example: bytes
`21 89 02 35` -> 44100 Hz, joint stereo, 16 blocks, 8 subbands, bitpool 2..53 -> frame 119 bytes ->
**328.0 kbit/s** (I checked the arithmetic separately). It is an upper bound because the stream may use a
lower bitpool than the maximum. Missing/short input -> `{}`.

### `transport_info(bz, addr)` (`:478`)
Returns `None` when the device has no `MediaTransport1`; that `None` is what the many `wait_for(lambda:
transport_info(...), N)` polls wait to turn into a dict. Otherwise:

```python
t = {"path": path, "state": p.get("State"), "codec": codec_name(codec, conf),
     "volume": p.get("Volume"), "delay_ms": (p["Delay"] / 10.0) if "Delay" in p else None,
     "uuid": p.get("UUID")}
```
`state` is BlueZ's `idle`/`pending`/`active`; `volume` is the AVRCP absolute volume 0-127 (absent if the DUT
does not support it); `Delay` is reported by BlueZ in tenths of a millisecond, hence `/ 10.0`. For SBC the
decoded fields are added under `"sbc"`; for every other codec the raw configuration is given as
`"config_hex"`. (Gotcha: AAC/LDAC/aptX are *named* but their settings are not decoded.)
Illustrative result: `{"path": ".../dev_5C_35_89_B4_55_93/sep1/fd0", "state": "active", "codec": "SBC",
"volume": 64, "delay_ms": 150.0, "sbc": {"freq": 44100, "mode": "joint", ..., "bitrate_kbps_max": 328.0}}`.

---

## 10. PipeWire access: `class Audio` (`:494-581`)

**The problem it solves.** BlueZ handles the Bluetooth link, but the audio plumbing is PipeWire, which
runs *inside the desktop user's login session*. This program normally runs as **root** (it needs the
radio). Root cannot simply talk to another user's PipeWire. So every `pw-*` command is run **as that user,
with that user's environment variables** (`:496`).

- **`__init__(user=None)`** (`:498`) stores the requested user and calls `_resolve`.
- **`_resolve()`** (`:503`) finds the user id (uid) of the session to use:
  1. `--audio-user NAME` given -> its uid (`pwd.getpwnam`; unknown name -> `die`).
  2. Otherwise: if not root, the current user first; then every numeric folder in `/run/user` with uid >=
     1000 (sorted as text).
  3. The first candidate for which `/run/user/<uid>/pipewire-0` (PipeWire's socket) exists wins:
     `self.uid`, `self.user` (looked up with `pwd.getpwuid`). None found -> `self.uid = None`.
- **`ok`** (`:525`) - property, true when a session was found.
- **`env()`** (`:528`) - the three variables needed: `XDG_RUNTIME_DIR=/run/user/1000`,
  `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`, `PIPEWIRE_REMOTE=pipewire-0`.
- **`wrap(cmd)`** (`:533`) - builds the real command line. Not ok -> `die("no PipeWire session found ...")`.
  Root: `["runuser", "-u", user, "--", "env", "K=V", ..., *cmd]`; non-root: `["env", "K=V", ..., *cmd]`.
- **`run(cmd, timeout=8)`** (`:541`) -> `_run(wrap(cmd), timeout)` (returns `(rc, text)`).
- **`popen(cmd, **kw)`** (`:544`) -> `subprocess.Popen(wrap(cmd), **kw)` for long-running children.
- **`dump()`** (`:547`) - runs `pw-dump` (the whole PipeWire graph as JSON, 10 s timeout) and parses it;
  any failure -> `[]`. Because `_run` merges stderr into the text, one stray warning line makes the JSON
  invalid and the result silently `[]`.
- **`card_for(addr)`** (`:554`) - finds the PipeWire **device** (card) of the headset. It walks the dump for
  objects whose `type` ends in `Interface:Device` and whose `info.props["device.name"]` contains the address
  in lower case with underscores (`5c_35_89_b4_55_93`). Returns
  `{"id", "name", "active_profile", "profiles": {name: {"index", "available"}}}`. `active_profile` is the
  first entry of the object's `Profile` param; `profiles` come from `EnumProfile`, with `available` false only
  when PipeWire says `"no"` (so `"unknown"` counts as available). The docstring explains why it reads
  `pw-dump` instead of `pactl`: a machine can run real PulseAudio on the Pulse socket, which cannot see
  PipeWire's Bluetooth cards.
- **`sink_for(addr)`** (`:570`) - same walk for a **Node** with `media.class == "Audio/Sink"` and the
  address in `node.name` (names like `bluez_output.5C_35_89_B4_55_93.1`). Returns `{"id", "name", "state"}`.
  The `name` is what `pw-cat --target` needs.
- **`set_profile(card, profile)`** (`:579`) - `pw-cli set-param <card id> Profile "{ index: N }"`. Returns
  `(rc, text)`; the caller checks `rc`.

Gotchas: `card_for`/`sink_for` run a full `pw-dump` per call, and callers poll them every 0.25 s for up to
20 s. If the user was found but WirePlumber is not running, `card_for` returns `None` and part 2 reports "no
audio card". Edge case: `_resolve` for a non-root caller may pick another user's socket it cannot use.

### `pick_a2dp_profile(card, codec=None)` (`:584`)
Chooses the PipeWire card profile for streaming. In PipeWire/PulseAudio language the profile **`a2dp-sink`**
means "this card is a speaker that *we* play into", i.e. our A2DP Source role (the naming looks
backwards but is standard).
- With `codec` (e.g. `"aac"`): the first profile that starts with `a2dp-sink` and contains the codec text
  (`a2dp-sink-aac`); none -> `die(...)` listing what the DUT offers.
- Without `codec`: prefer exactly `a2dp-sink` (PipeWire picks the codec), else the first `a2dp-sink*`, else `None`.
Dictionary order follows PipeWire's `EnumProfile`, so `"sbc"` could match `a2dp-sink-sbc` or
`a2dp-sink-sbc_xq`, whichever is listed first.

---

## 11. Audio sources and streamers (`:600-784`)

### `gen_pcm(kind, rate=48000, seconds=10)` (`:601`)
Builds one 10-second loop of a test signal as raw **PCM**: 16-bit signed little-endian stereo
(`struct.pack("<hh", v, v)`, the same sample in both channels), 48,000 samples/s -> 1,920,000 bytes.
Level `0.25 * 32767` = -12 dB below full scale. Three kinds:
- `"tone"` - 440 Hz sine (the note A). 440 Hz x 10 s is a whole number of cycles, so the loop is seamless.
- `"sweep"` - a logarithmic sweep 100 Hz -> 8 kHz across the 10 s (`exp(k*t)` form). Not phase-continuous at
  the loop point, so each repeat can click.
- `"noise"` - white noise from `random.Random(1234)` (fixed seed: same noise every run).
- anything else -> `die("unknown source")`.
It is a pure-Python loop of 480,000 iterations (I timed the tone case at about half a second on this
machine's Python).

### `read_wav(path)` (`:624`)
Reads a WAV with the standard `wave` module; only 16-bit accepted (`die` otherwise); returns
`(raw frames, sample rate, channel count)` held fully in memory. Used by part 2 only when `ffmpeg` is
missing. A non-PCM WAV (e.g. float) raises `wave.Error`, which is not a `ClassicError`, so `main()` reports
it as an "internal error".

### `class Streamer(threading.Thread)` (`:632`)
Plays a PCM buffer in a loop to a PipeWire sink. A **thread** is a second line of execution inside the
program; `daemon=True` means it will not keep the program alive.
- **`__init__(audio, target, pcm, rate, channels, latency_ms, name)`** (`:636`) starts a child process
  (one command line, wrapped here):

  ```
  pw-cat -p --target <sink name> --rate 48000 --channels 2 --format s16 --latency 100ms
         -P "{ node.name=<name> media.name=<name> }" -
  ```

  (`-p` play, `-` read audio from stdin, `-P` set properties so the stream gets a known node name for
  `XrunMonitor`). The child's stdin is a pipe we write to; stdout is discarded; stderr is a pipe.
- **`run()`** (`:647`) - loop forever until `STOP`: write the buffer in chunks of 19,200 bytes (100 ms of 48 kHz
  stereo) to `proc.stdin`, flush, and add to `bytes_sent`. There is **no timing code**: the pipe fills up
  when `pw-cat` is consuming at real-time speed, so `write` blocks and the loop is paced automatically
  ("the pipe blocks when the sink is back-pressured"). Pipe errors set `self.error = "audio writer stopped:
  ..."` and end the thread.
- **`stop()`** (`:661`) - close stdin, `terminate()`, wait 3 s, `kill()` if that fails. As root the process
  being terminated is the `runuser` wrapper; I did not check whether `runuser` forwards the signal to `pw-cat`.
- **`alive()`** (`:672`) - child still running.

### `AUDIO_EXTS` and `probe_media(path)` (`:676`, `:680`)
`AUDIO_EXTS` is the whitelist of file extensions (audio plus video containers, since `ffmpeg` can pull the
audio out). The same list is copied in `server.py:457`. `probe_media` validates a user's file:
1. missing file -> `die`; wrong extension -> `die` listing the allowed ones;
2. if `ffprobe`/`ffmpeg` are not both installed: a `.wav` is accepted blindly (`{"codec": "pcm (native wav
   reader)", "duration_s": None}`), anything else -> `die` telling you to install ffmpeg;
3. otherwise run `ffprobe -v error -select_streams a:0 -show_entries stream=codec_name,sample_rate,channels:format=duration -of json`,
   parse; no audio stream -> `die("no playable audio stream ...")`;
4. return `{"codec", "rate", "channels", "duration_s"}`.
The two return shapes differ (the no-ffmpeg WAV case lacks `rate`/`channels`).

### `class FfmpegStreamer` (`:704`)
"Any audio/video file -> ffmpeg (decode, 48 kHz stereo s16, looped forever) -> pw-cat." Two processes joined
by a pipe, so nothing is held in memory and file length does not matter.
- **`__init__`** (`:709`) starts `ffmpeg -nostdin -loglevel error -stream_loop -1 -i <file> -vn -map 0:a:0
  -f s16le -ar 48000 -ac 2 -` (loop forever, first audio stream, raw PCM to stdout), then `pw-cat` with
  `stdin=self.ff.stdout`. The parent closes its own copy of ffmpeg's stdout (`:718`) so only `pw-cat` holds
  the read end (if `pw-cat` dies, ffmpeg gets a broken pipe instead of hanging).
- **`error`** (property, `:722`) - if ffmpeg has exited with a non-zero code and no stop was requested, reads
  the last line of its stderr and returns `"ffmpeg decoder failed: ..."`; otherwise `None`. Cached once set.
- **`start()`** (`:732`) does nothing (processes already run; it exists so both streamers look alike: part
  2 calls `.start()` on either).
- **`alive()`** (`:735`) - both processes running. **`stop()`** (`:738`) terminate both, wait 3 s each, kill on failure.
- Note: `bytes_sent` stays 0 forever here (no Python writer counts it); and `Streamer.bytes_sent` is not
  read anywhere in the file either.

### `class XrunMonitor(threading.Thread)` (`:747`)
Counts glitches (xruns) of one PipeWire node.
- Why not one long `pw-top -b`? When its output is a pipe it is block-buffered, so lines would arrive
  minutes late (docstring, `:748-750`). Instead it runs a **short** `pw-top -b -n 2` repeatedly (a short run
  flushes on exit).
- **`run()`** (`:777`): until stopped: run `pw-top -b -n 2` (8 s timeout) via the audio user, `parse` it,
  wait 1 s.
- **`parse(text)`** (`:761`): the row format is documented in a comment: `S ID QUANT RATE WAIT BUSY W/Q B/Q
  ERR [FORMAT...] NAME`. Take the last line that has at least 10 columns and ends with the node name;
  `row[8]` (ERR) becomes `self.err`, `self.seen = True`, and `self.last` keeps `quant, rate, wait, busy, w_q, b_q`.
  Because the second sample is the later one, the last matching row is the meaningful one.
- **`stop()`** (`:783`): sets the event; the thread ends at its next wake-up.
Fields read by part 2: `err`, `seen`, `last`.

---

## 12. One-shot actions (`:787-1019`)

All take `(a, bz)`: `a` is the parsed command-line (`a.addr`, `a.timeout`, ...), `bz` is the `BZ` object.
Each returns a dict that `main()` prints as the final JSON line; failures `die(...)`. `act_preflight` takes
only `(a)`.

Reference of the command-line arguments for these actions (defaults from `build_parser`, part 2):

| Action | Arguments |
|---|---|
| `adapter` | `--power on/off`, `--pairable on/off`, `--discoverable on/off` |
| `scan` | `-t/--duration` (10 s), `--transport bredr/auto` |
| `devices` | none |
| `info` | `addr` |
| `pair` | `addr`, `--pin` (0000), `--timeout` (40), `--scan` (20) |
| `connect` | `addr`, `--timeout` (30), `--scan` (20), `--wait-audio` (10; 0 = skip) |
| `disconnect`, `remove` | `addr` |
| `avrcp-player` | `addr`, `pcmd` (`status`, `play`, `pause`, `stop`, `next`, `previous`, `fastforward`, `rewind`) |
| `avrcp-volume` | `addr`, `--steps` (`0,32,64,96,127,64`), `--timeout` (3.0) |
| `interrogate` | `addr`, `--transport auto/le/bredr`, `--scan` (15), `--timeout` (25), `--resolve-timeout` (15), `--read-budget` (25), `--no-read`, `--no-sdp` |

(`server.py` only passes some of these; for example it never passes `--timeout` for `connect`.)

### `act_preflight(a)` (`:788`)
"Is this machine ready?" Needs no radio and no D-Bus. It builds:
- `ok: True`, `gi` (PyGObject present), `root` (`os.geteuid() == 0`);
- `tools`: for each of `bluetoothctl, hcitool, hciconfig, pw-dump, pw-cli, pw-cat, pw-top, runuser, ffmpeg,
  ffprobe` whether `shutil.which` finds it (`bluetoothctl` is listed but nothing else in this file uses it);
- `bluetoothd`: the text of `systemctl is-active bluetooth` (`"active"` when fine);
- `audio`: `session` / `user` from `Audio(a.audio_user)`; if a session exists, also `pipewire_ok`
  (`pw-cli info 0` succeeded and mentions "PipeWire") and `wireplumber` (`systemctl --user is-active
  wireplumber`, run as the desktop user);
- `notes`: plain-English advice for each missing piece (`:800-812`): PyGObject, hcitool, hciconfig,
  ffmpeg/ffprobe, no PipeWire session, bluetooth.service not active.
Gotcha: `pipewire_ok` and `wireplumber` are reported but produce no note. The server passes this through
(`classic_preflight`, `server.py:616`), adding `reserved` and `all_hci`.

### `act_adapter(a, bz)` (`:817`)
Applies any of `--power/--pairable/--discoverable` with `Properties.Set` on `Adapter1`
(`GLib.Variant("b", a.power == "on")`), sleeps 0.5 s after changing power, and returns
`{"ok": True, "adapter": adapter_info(bz)}`. With no flags it is a pure "show adapter" call. Note: `Discoverable`
is on BlueZ's own timer (`DiscoverableTimeout`, default 180 s as I understand it); the code does not touch it.

### `act_scan(a, bz)` (`:828`)
Prints `[*] hci1: BR/EDR inquiry for 10s` (or "dual-mode" for `--transport auto`), calls `discover`,
prints `[+] N device(s) known`, returns `{"ok": True, "devices": [...], "adapter": {...}}`.

### `act_devices(a, bz)` (`:835`)
No scan: returns what BlueZ already knows, **including LE random devices** (`only_bredr=False`), plus the adapter.

### `device_summary(bz, addr)` (`:839`)
Builds the rich device record used by `info`, `pair`, `connect`:
1. `GetAll(Device1)`; `None` -> `die("... is not known to hciN (scan first)")`.
2. `human_dev(p)` as the base.
3. From the device's child objects (`bz.device_objects`): `has_avrcp_control` (any `org.bluez.MediaControl1`
   - an older BlueZ interface that may simply not exist on recent versions, so this can stay `False`),
   `has_player` (a `MediaPlayer1`), `battery` (`Percentage` from `org.bluez.Battery1`, if the device reports it).
4. `services_resolved` and `transport` (`transport_info`).

### `act_info(a, bz)` (`:855`)
`device_summary`, plus, **only if connected**, `link` = `link_stats` (RSSI/TX power/link quality), plus, if
connected and a PipeWire session exists, `audio_card` = `{name, active_profile, profiles: [available names]}`.
Returns `{"ok": True, "device": d}`. This is what the GUI polls every 5 s (see `run_classic_action`), so
it deliberately does not take the per-radio lock. It can take several seconds (up to ~9 s of hcitool plus
a `pw-dump`).

### `act_pair(a, bz)` (`:868`)
1. `norm_addr`, `ensure_powered`, `ensure_device(bz, addr, a.scan)` (inquiry if unknown).
2. If `Paired` is already true: `[+] already paired`. Otherwise:
   `with Agent(bz, a.pin): bz.call_loop(dp, IF_DEVICE, "Pair", None, int(a.timeout * 1000))`.
   (`Device1.Pair` is the BlueZ method; the agent answers BlueZ's questions meanwhile.)
3. Always set `Trusted = True` (even if it was already paired), so BlueZ will accept later connections from
   this device without asking.
4. Return `{"ok": True, "device": device_summary(...)}`.
Gotchas: a failed `Pair` raises (e.g. `AuthenticationFailed`) before `Trusted` is set; the agent is only
present during the `Pair` call.

### `wait_for(pred, timeout, step=0.25)` (`:885`)
Generic poll: call `pred()` every 0.25 s until it returns something truthy (returned as the result) or the
time runs out / `STOP` (returns `None`). It sits mid-file but is used all over, including part 2
(`wait_for(lambda: au.card_for(addr), 20)`).

### `do_connect(bz, addr, timeout)` (`:895`)
`Device1.Connect` (BlueZ connects *all* profiles both sides support) via `call_loop`. Returns the elapsed time
in **milliseconds as a float**, or `0.0` if already connected. The docstring says it returns a tuple
`(connect_ms, services_ms)`; that is stale (section 18).

### `act_connect(a, bz)` (`:905`)
Power on, ensure the device is known, **require it to be paired** (else `die("... pair it first")`), print,
`do_connect`, print `[+] connected in N ms`. Result `{"ok", "connect_ms"}`; if `--wait-audio N` (default 10)
is non-zero: `wait_for(lambda: transport_info(bz, addr), N)` -> `transport_ms` (time until the A2DP
transport object appeared) or `None` with `[!] connected, but no A2DP transport appeared within Ns`. (As I understand it, the
transport is normally created once the audio side - PipeWire/WirePlumber - has configured the endpoint; it
appearing late or never points at the audio stack, not the radio.) Finally adds `device`.

### `act_disconnect(a, bz)` (`:926`)
Unknown device -> `die`; already disconnected -> `{"ok": True, "msg": "already disconnected"}`; otherwise
`Device1.Disconnect` (20 s timeout), `[+] disconnected in N ms`, `{"ok": True, "disconnect_ms": ...}`.

### `act_remove(a, bz)` (`:940`)
If the device object exists: `Adapter1.RemoveDevice(device path)` - which forgets the device **and deletes the
pairing**. Always prints `[+] ... removed ... (pairing deleted)` and returns `{"ok": True}`, even when the
device was not known.

---

## 13. AVRCP actions (`:948-1019`)

### `PLAYER_CMDS` (`:949`)
Maps CLI words to `MediaPlayer1` method names: `play->Play`, `pause->Pause`, `stop->Stop`, `next->Next`,
`previous->Previous`, `fastforward->FastForward`, `rewind->Rewind`.

### `act_avrcp_player(a, bz)` (`:953`)
"Drive the DUT's media player." BlueZ exposes a `MediaPlayer1` object (`.../dev_X/player0`) only when the
remote is an AVRCP *target with a player*, typically a phone. A headset or speaker is controller-only and
has none, so the code stops with a purpose-written message instead of an obscure error.
1. `bz.player(addr)`; none -> `die(...use the AVRCP volume test or the button-watch test instead)`.
2. If `pcmd != "status"`: call the method (`bz.call(pp, IF_PLAYER, "Play", None)`), `[+] AVRCP play sent`,
   sleep 0.5 s to let BlueZ update.
3. Re-read the properties and return `{"ok": True, "player": {"name", "status", "track", "position_ms"}}`
   (`track` is BlueZ's own dictionary: title, artist, album ...).

### `act_avrcp_volume(a, bz)` (`:971`)
Sends a series of **AVRCP absolute volume** values (0-127) to the DUT and checks BlueZ accepts them. The
docstring is candid: BlueZ updates its own `Volume` property when the command is sent, so a "pass" proves
the *command path*, not that the DUT's amplifier moved ("confirm audibly on the DUT").
Step by step:
1. Find the transport; none -> `die("... connect it first (AVRCP volume rides on the A2DP transport)")`.
   No `Volume` property -> `die` ("the DUT ... does not do AVRCP absolute volume").
2. Parse `--steps` into ints; any outside 0-127 -> `die`.
3. `bz.subscribe(on_sig)`; the handler appends `(time, value)` to `events` whenever the transport's
   `Volume` changes (it filters by `path == tp`).
4. Remember `original = p.get("Volume")`.
5. For each step `s`: clear `events`; `bz.set(tp, IF_TRANSPORT, "Volume", GLib.Variant("q", s))` (`q` =
   uint16); `pump(0.05)`; then until `--timeout` (3 s): read back the property; success if it equals `s` or a
   signal reported `s`. Record `{"set", "readback", "ok", "ms"}` and print `[+]/[x] volume  64 -> sent,
   accepted by BlueZ (12 ms)`.
6. Restore the original volume (errors ignored).
7. Return `{"ok": True, "test": "avrcp-volume", "pass": <all steps ok>, "steps": [...]}`. Note the two
   different "ok"s: `ok: True` = the action ran; `pass` = the test verdict.
Gotchas: if BlueZ *rejects* a `Set` (raises `ClassicError`), the whole action aborts - that step is not
recorded as failed; the restore step is the only protected one. If a step equals the current volume the
step passes immediately.

---

## 14. Interrogate: GATT + SDP (`:1022-1159`)

The newest action. Purpose (docstring `:1093-1096`): "connect to a device and list everything it will tell an
unpaired, unencrypted client": the LE **GATT** database with readable values, and the Classic **profiles**
(UUIDs + SDP records). Read-only: nothing is written to the device, and the device is put back as it was.

### Constants (`:1023-1027`)
- `IF_GATT_SVC/CHR/DSC` = `org.bluez.GattService1`, `GattCharacteristic1`, `GattDescriptor1`. Once the
  device's services are resolved, BlueZ exports them as child objects, e.g.
  `/org/bluez/hci1/dev_C0_1C_9A_52_7E_11/service000a` (a service), `.../service000a/char000b` (a
  characteristic), `.../service000a/char000b/desc000d` (a descriptor). (Illustrative address; numbers are
  hex attribute handles.)
- `SECURE_READ_FLAGS` = `encrypt-read`, `encrypt-authenticated-read`, `secure-read` - flags saying a value
  needs an encrypted link to read.
- `NO_READ_SERVICES = {0x1812}` - **HID over GATT** (keyboards/mice). "Reports can carry keystrokes - list it,
  never read it."

### `_short_uuid(u)` (`:1030`)
If the UUID is in the Bluetooth base pattern *and* starts with `0000`, return its 16-bit value:
`"0000180f-0000-1000-8000-00805f9b34fb"` -> `0x180F`. Otherwise `None` (a vendor-specific 128-bit UUID).

### `_gatt_tree(bz, addr, do_read, budget_s)` (`:1038`)
"Enumerate the device's resolved GATT database (read-only)." Returns `(services, stats)`.
1. One `bz.device_objects(addr)` snapshot, split into `svcs`, `chrs`, `dscs` dicts by interface.
2. `deadline = now + budget_s`; `stats = {read_ok, read_failed, skipped_secure, skipped_budget}`.
3. For each service (paths sorted): node `{uuid, name (ble_decode.SERVICES), primary, characteristics: []}`.
4. For each characteristic whose path starts with `<service path>/`: `{uuid, name (ble_decode.CHARACTERISTICS),
   flags, descriptors}`; descriptors (names from `ble_decode.DESCRIPTORS`) found the same way.
5. **Read decision** (`:1063`): read only if `"read"` is in `flags`, reading is enabled (`do_read`; the CLI
   flag `--no-read` turns it off), and the service is not in `NO_READ_SERVICES`.
   - past the deadline or `STOP` -> `skipped_budget += 1`;
   - else `bz.call(cp, IF_GATT_CHR, "ReadValue", GLib.Variant("(a{sv})", ({},)), timeout_ms=6000)`
     (an empty options dictionary), keep at most the first **256 bytes**, convert with
     `ble_decode.value_text(cs, b)` into `value_hex` and `value` (decoded text such as `"87 %"` for 0x2A19
     Battery Level); `read_ok += 1`;
   - on `ClassicError`, store the first 120 characters as `read_error`, `read_failed += 1`.
6. Else, if `"read"` is not in flags but a secure-read flag is: `skipped_secure += 1`.
Gotchas:
- The time budget is checked **before** each read, so the last read can run up to 6 s past it.
- The "never triggers pairing" claim (`:1039-1041`) relies on the `Flags` list. As far as I know, for a
  *remote* device BlueZ derives `Flags` from the characteristic's property bits and does not mark
  encrypt-read permissions, so `skipped_secure` may effectively always be 0 and a read that needs
  encryption would simply fail with an error (or be retried by BlueZ at a higher security level). I did not
  verify this against BlueZ; treat it as an open question (section 18).

### `_sdp_records(hci, addr)` (`:1082`)
SDP = ask a Classic device which services it runs. Uses the old tool `sdptool -i hci1 records <addr>` (40 s
timeout) and `ble_decode.parse_sdp_records` to turn its text into
`[{name, handle, classes, l2cap_psm, rfcomm_channel, profiles}]`. Returns `(records, note)`:
- no `sdptool` -> `(None, "sdptool not installed")`;
- output but nothing parseable -> `([], <last output line or "no SDP records returned">)`;
- otherwise `(records, None)`.

### `act_interrogate(a, bz)` (`:1092`)
1. `norm_addr`, `ensure_powered`; `existed = bz.exists(dp)`.
2. Unknown device: `discover(bz, a.scan, transport=a.transport, want=addr)` (`auto` default, so LE devices
   are found too); still absent -> `die`.
3. Remember `was_connected` and `was_paired`; start `res = {"ok", "addr", "hci", "was_connected"}`.
4. `try:` block:
   - **Connect** if not connected (`call_loop`... `Connect`, `a.timeout`, default 25 s); failure ->
     `die("could not connect to ...")`; records `connect_ms`.
   - **Wait for services**: `wait_for(ServicesResolved, a.resolve_timeout)` (default 15 s) -> `resolved`.
   - **Device facts** (`res["device"]`): name, `address_type` (`public`/`random`), class, icon,
     `appearance` (the 16-bit GAP Appearance value shifted right by 6 and looked up in
     `ble_decode.APPEARANCE`: the top 10 bits are the device *category*), modalias, `paired` (the
     *before* state), `rssi`, `tx_power`.
   - **UUID list** (`res["uuids"]`): each `UUIDs` entry with a name from `CLASSIC_PROFILES` or `SERVICES`.
     (`CLASSIC_PROFILES.get(sh) or SERVICES.get(sh) if sh is not None else None` parses as
     `(A or B) if sh is not None else None`: correct as intended.)
   - **GATT**: if resolved -> `_gatt_tree(bz, addr, a.read, a.read_budget)` (budget default 25 s), print
     `[+] N GATT service(s); M value(s) read`; else `gatt = []`, `gatt_stats = None` and a `[!]` warning.
   - **Classic?** `classic` is true if any advertised UUID is a known profile in the range `0x1100..0x17FF`
     (so GAP/GATT `0x1800/0x1801` do not count). If classic and not `--no-sdp`: `_sdp_records`;
     else `sdp = None` with note `"no BR/EDR profiles reported"` or `"skipped"`.
5. `finally:` **leave it as we found it**: if it was not connected before and is now, `Disconnect`; if it did
   not exist before and was not paired, `RemoveDevice`. Both are in one `try/except ClassicError`, so a
   failed disconnect also skips the removal. Because it is `finally`, cleanup also runs when `die()` fires.
6. Return `res`.
Result shape (keys only): `ok, addr, hci, was_connected, connect_ms, device{...}, uuids[...], gatt[...],
gatt_stats{...}, sdp[...]|None, sdp_note`.
Gotchas: no `Agent` is registered here, so BlueZ has nobody of ours to ask about pairing (a desktop
applet's agent, if present on the bus, could still be asked). If the interrogation itself ends up creating a
bond, `was_paired` is the old value, so the device is removed and the bond deleted (edge case). The warning
text "BR/EDR-only device, or it needs pairing" (`:1140`) is shaky: as I understand BlueZ, `ServicesResolved`
also goes true for Classic devices once SDP finishes, in which case you get `0 GATT service(s)` instead.

---

## 15. `ACTIONS` (`:1162-1167`)

```python
ACTIONS = {"adapter": act_adapter, "scan": act_scan, "devices": act_devices, "info": act_info,
           "pair": act_pair, "connect": act_connect, "disconnect": act_disconnect, "remove": act_remove,
           "avrcp-player": act_avrcp_player, "avrcp-volume": act_avrcp_volume, "interrogate": act_interrogate}
```
`main()` (part 2) does `res = ACTIONS[cmd](a, bz)` then `print(json.dumps(res, default=str))`. `preflight` is
not here (special-cased) and neither are jobs (`JOBS`). The engine's names differ from the server's for
jobs (`server.py` says `c_a2dp`; the engine command is `a2dp`).

---

## 16. AVRCP key watcher (`:1170-1242`)

Purpose: when the user presses a button on the headset (play/pause, next, volume), BlueZ receives an AVRCP
"pass-through" command and re-publishes it as a fake keyboard key. This class reads those key presses so a
test can record what the DUT sent.

### `KEYMAP`, `EV_FMT`, `EV_SIZE` (`:1171-1175`)
`KEYMAP` maps Linux key codes to names: 200/207 PLAY, 201 PAUSE, 164 PLAYPAUSE, 166 STOP, 163 NEXT, 165
PREVIOUS, 208 FASTFORWARD, 168 REWIND, 115 VOLUMEUP, 114 VOLUMEDOWN, 113 MUTE. (I checked these numbers
against `/usr/include/linux/input-event-codes.h`: `KEY_PLAYCD`=200, `KEY_PAUSECD`=201, `KEY_PLAY`=207,
etc.) `EV_FMT = "llHHi"` is the layout of the kernel's `struct input_event`: two longs (timestamp), a 16-bit
`type`, a 16-bit `code`, a 32-bit `value`; `EV_SIZE` = 24 bytes on this 64-bit machine.

### `find_avrcp_evdev(addr, alias=None, local=None)` (`:1178`)
Finds which `/dev/input/eventN` belongs to this headset by reading `/proc/bus/input/devices` (blocks
separated by blank lines). BlueZ names the fake device `"<remote name> (AVRCP)"` (older BlueZ:
`"<BDADDR> (AVRCP)"`) and, per the docstring, sets its `Phys` to the **local** adapter address. For each block:
needs a `Name="..."` containing `(avrcp)` and a `Handlers=... eventN`; if `local` is given and the block's
`Phys` is non-empty and different, skip it (so two radios do not get mixed up); accept if the lower-case
address is in the name, or the name starts with the lower-case alias. Returns `/dev/input/eventN` or `None`.
Gotchas: the alias match is by prefix, so two devices with similar names on one adapter can be confused;
reading the device needs root or the `input` group.

### `class KeyWatcher(threading.Thread)` (`:1201`)
- **`__init__(addr, on_key, alias=None, local=None)`**: `on_key` is a function called with the key name.
- **`run()`** (`:1211`) loops until stopped:
  1. If no open device yet: `find_avrcp_evdev`; none -> wait 1 s and retry (so it picks the device up when
     the headset connects, even if the program started first); then `os.open(path, O_RDONLY|O_NONBLOCK)`
     (stores `self.dev`).
  2. `select.select([fd], [], [], 0.5)` - wait up to half a second for data.
  3. `os.read(fd, EV_SIZE*16)` (up to 16 events); on `OSError` (device vanished when the headset disconnected)
     close the fd and go back to step 1 (it re-finds the device after reconnection).
  4. Walk the buffer 24 bytes at a time; `struct.unpack_from(EV_FMT, ...)`; if `type == 1` (EV_KEY),
     `value == 1` (press, not release/repeat) and the code is in `KEYMAP`, call `on_key(name)`.
  5. On exit, close the fd.
- **`stop()`** (`:1241`) sets the event (the thread exits within ~0.5 s; no join).
Gotchas: `self.dev` is never cleared when the device disappears. A non-blocking read that finds no data
raises `BlockingIOError`, which is an `OSError`, so it is handled like "device gone" (close and re-find;
harmless).

---

## 17. Run bookkeeping: `Recorder`, `stats`, `verdict` (`:1245-1305`)

These give every job the same "record -> summarise -> save -> pass/fail" shape.

### `class Recorder` (`:1246`)
- **`__init__(kind, addr, hci)`** (`:1247`): `kind` is the job name (`"a2dp"`, `"avrcp-watch"`, `"cycle"`, `"hfp"`),
  `t0` = start time, empty `samples` and `events` lists, `stamp` = `YYYYmmdd-HHMMSS`.
- **`rel()`** (`:1254`): seconds since start, 2 decimals (the `"t"` of every record).
- **`event(name, **kw)`** (`:1257`): a one-off happening (`ready`, `stream_stopped`, `avrcp_key`...). Builds
  `{"t": ..., "event": name, **kw}`, stores it, and prints it as `{"m":"event", ...}`.
- **`sample(**kw)`** (`:1263`): a periodic measurement row (`{"t": ..., "rssi": ..., "tx_kbps": ...}`), stored
  and printed as `{"m":"sample", ...}`. The GUI draws graphs from these lines live.
- **`save(summary)`** (`:1269`): creates `LOG_DIR/classic/`, then writes
  `<stamp>_<kind>_<addr without colons>.json` (e.g. `20261027-101500_a2dp_5C3589B45593.json`) containing
  `{"summary", "events", "samples"}`, and a `.csv` of the samples (columns = the sorted union of all sample
  keys; missing cells empty). Prints `[*] results saved: <base>.json / .csv` and sets `summary["saved_as"] =
  base`. An `OSError` (disk full, permissions) only prints `[!] could not save results`. The server's results
  tab lists these files (`classic_results`, `server.py:633`).

### `stats(vals)` (`:1287`)
Ignores `None`s; returns `None` if nothing is left; else
`{"min", "avg" (2 decimals), "max", "p95", "n"}`. `p95` is `sorted[min(n-1, int(0.95*n))]`: with 20 values
it equals the maximum; with 100 values it is the 96th smallest (slightly high; a strict "95th percentile"
would be the 95th).

### `verdict(rec, summary, failures)` (`:1296`)
The common ending of every job:
1. `summary["pass"] = not failures`, `summary["fail_reasons"] = failures` (a list of text reasons).
2. `emit(m="summary", **summary)` - the machine summary line the GUI reads.
3. Print `[x] RESULT: FAIL - reason1; reason2` or `[+] RESULT: PASS`.
4. `rec.save(summary)`.
5. Return `0` for pass, `2` for fail (this becomes the process exit code).

---

## 18. Things I noticed

Ordered roughly by how likely they are to matter. "Verified" = I can see it directly in the code;
"unverified" = depends on BlueZ/hcitool behaviour I could not test here.

**Real inconsistencies / dead code (verified)**
1. **`do_connect` docstring is wrong** (`:896`): says "returns (connect_ms, services_ms)", returns a single
   float (`:898-902`). Callers (`:913`, part 2 `round(do_connect(...), 1)`) treat it as a float, so only the
   comment is stale.
2. **`acl_links` docstring is wrong** (`:413`): promises `[(addr, 'ACL'|'SCO', role)]`, returns a list of
   dicts with `type, addr, handle, role`.
3. **`discover(..., quiet=False)`** (`:347`): the `quiet` parameter is never read.
4. **`summary["saved_as"]` is never seen** (`:1282`): `verdict` already emitted the summary line and `save`
   already wrote the JSON before this key is added, and nothing reads the dictionary afterwards. It is
   effectively dead.
5. **`bytes_sent` counters are unused** (`:645,657,719`): nobody reads them, and the `FfmpegStreamer` one can
   never change from 0.
6. **Duplicate lookup tables**: `UUID_NAMES` (`:301`) vs `ble_decode.CLASSIC_PROFILES` label the same UUIDs
   differently (`0x110D` "A2DP" vs "A2DP (Advanced Audio)", `0x1115` "PAN" vs "PAN User", `0x1116` "NAP" vs "PAN NAP", `0x1200` "PnP" vs "PnP Information"), so the device list and the interrogate result can
   name one profile two ways. `AUDIO_EXTS` (`:676`) is duplicated in `server.py:457` and the two must be kept in
   step by hand.

**Risks (verified in the code, severity usually low)**
7. **`import acl_meter` at module level** (`:55`) pulls in `hci_raw` for every command, including `preflight`.
   If that import ever fails, the user gets a raw traceback before any of the friendly error handling in
   `main()`, even though part 1 does not use `acl_meter` at all.
8. **`exists()` hides all errors** (`:154-161`): a D-Bus timeout reads as "device not present", which can trigger
   a needless up-to-20 s inquiry in `ensure_device` and a misleading "was not found" message.
9. **`call_loop` ignores `STOP`** (`:131-152`): pressing Stop during `Pair`/`Connect` only takes effect when the
   BlueZ call finishes or times out (up to `timeout + 3 s`); its safety timer is also left registered afterwards.
10. **`Agent.RequestPasskey` can overflow** (`:262`): the server accepts PINs up to 16 digits
    (`server.py` regex `[0-9]{1,16}`), and `GLib.Variant("(u)", ...)` only holds 32 bits; a longer number would
    raise inside the callback. (Passkeys are normally 6 digits, and passkey entry is rare for the headsets
    this targets.) Also if `RegisterAgent` fails inside `__enter__` (`:269`), `__exit__` is not called and the
    local object stays registered until the process exits (harmless in a one-shot).
11. **`Audio.dump()` can hide errors** (`:547-552`): `_run` merges stderr into stdout, so any warning text
    from `runuser`/`pw-dump` makes the JSON invalid and the result `[]`, which later shows as "PipeWire created
    no audio card". The same merge could affect every `_run` regex.
12. **Child-process stderr pipes are never drained** (`:641`, `:713`, `:717`): `pw-cat`/`ffmpeg` stderr is a
    pipe nobody reads, so a very chatty child could in theory block on a full pipe. Both are quiet normally
    (`ffmpeg -loglevel error`).
13. **`Streamer.stop()` races with the writer thread** (`:661-670`): closing stdin while `run()` is mid-write
    can set `self.error` ("audio writer stopped") even for a deliberate stop. Part 2 reads `.error` inside its
    loop, so it matters only if a stop is followed by a read.
14. **`FfmpegStreamer.error` after `stop()`** (`:722`): once `stop()` has terminated ffmpeg (exit code -15) and
    no `STOP` is set, `.error` would report "ffmpeg decoder failed: exit -15".
15. **`codec_name(None, ...)`** (`:440-451`) would raise `TypeError` (`"%02x" % None`); a real transport
    always has `Codec`, so only a malformed object could trigger it.
16. **`act_avrcp_volume`** (`:999`): a rejected `Set` aborts the whole action instead of being recorded as a
    failed step; and it only validates the BlueZ side (documented in its docstring).
17. **`act_remove` always says "removed"** (`:944`) even when the device was unknown; `act_pair` sets `Trusted`
    only after a successful pair.
18. **`Recorder.save` uses `json.dump` without `default=str`** (`:1275`) although `emit` has it; a
    non-JSON-serialisable value in a sample would raise `TypeError` (not caught - only `OSError` is) and lose
    the saved files.
19. **`act_interrogate` cleanup** (`:1150-1158`): one `try` covers both `Disconnect` and `RemoveDevice`; a failed
    disconnect skips the removal. It can also delete a bond created *during* the interrogation (it only
    remembers the pre-connect `Paired` value).
20. **Cosmetic**: `Audio._resolve` sorts uids as text (`"10000"` before `"1001"`) and ignores uids below 1000;
    `p95` in `stats` is slightly high for larger samples; `find_avrcp_evdev` leaves the `/proc` file for the
    garbage collector to close.

**Unverified / depends on the outside world**
21. **`acl_links` regex may miss eSCO** (`:418`): it matches `(ACL|SCO|ESCO)` in upper case, but as I recall
    `hcitool con` prints `eSCO` in mixed case. I could not run `hcitool` to confirm. Part 2 only uses `ACL`
    entries, so the effect is limited.
22. **"Never triggers pairing" in `_gatt_tree`** (`:1039`) depends on the characteristic `Flags` list; see the
    gotcha in section 14. If BlueZ does not mark remote characteristics as `encrypt-read`, then
    `skipped_secure` will stay 0 and secure reads depend on BlueZ's own behaviour.
23. **`has_avrcp_control`** (`:846`) looks for `org.bluez.MediaControl1`, a legacy interface that may not exist
    on recent BlueZ; the flag could then always be false.
24. **`Streamer.stop()` as root** terminates the `runuser` wrapper rather than `pw-cat` itself (`:661-670`); whether
    the signal reaches `pw-cat` depends on `runuser`. I did not check it.
25. **Performance**: `transport()`, `player()` and `device_objects()` each call `GetManagedObjects`, which returns
    the whole BlueZ tree; `wait_for` loops poll it every 0.25 s and `act_info` calls it three times. With many
    cached LE devices around this gets heavy, though it is fine on a small bench.
