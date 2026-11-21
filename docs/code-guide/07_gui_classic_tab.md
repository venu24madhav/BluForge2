# 07 - The "Bluetooth Classic" tab (GUI)

Files covered:

- `src/static/classic.js` (627 lines) - every function, constant and event handler, in file order.
- `src/static/index.html`, lines 261-467 - the Classic tab's HTML (the `c_*` element ids). Nothing else in that file.

Line references look like `classic.js:450` or `index.html:306`. References into other files
(`app.js`, `server.py`, `bluforge_classic.py`) are only there to explain how classic.js is driven; those files
are documented elsewhere. Where I checked a claim against the Python side, I say so. Where I am inferring
from reading code and have not run it, I say that too. Example JSON lines in this guide use the real field
names but invented numbers.

---

## 1. What the tab is, in one page

BluForge tests Bluetooth devices. Most tabs deal with BLE (Bluetooth Low Energy). This tab deals with
**Bluetooth Classic** (also called **BR/EDR**, "Basic Rate / Enhanced Data Rate"): the older Bluetooth used
for headphones, speakers and hands-free car kits. In this tab the computer running BluForge plays the phone,
and the thing you are testing (the **DUT**, "device under test", for example a pair of earbuds) plays the
headset.

The tab has these panels, top to bottom (HTML line numbers in brackets):

| Panel | What it is for | HTML |
|---|---|---|
| Banner | A yellow/red strip with warnings (missing tools) and errors | `index.html:264` |
| Classic radio (BR/EDR) | Pick which Bluetooth radio to use, **reserve** it for Classic, see its state | `266-280` |
| Devices | Scan for devices, list known ones, choose the DUT address | `282-301` |
| Device under test | Pair / Connect / Disconnect / Forget buttons and a live info grid for the DUT | `303-325` |
| A2DP stream test | Stream audio to the DUT and measure it | `327-360` |
| AVRCP | Volume test, remote-control the DUT's media player, "button watch" test | `362-394` |
| HFP hands-free call | Simulate a phone call to the DUT | `396-415` |
| Connection reliability | Connect/disconnect N times, count failures | `417-426` |
| Result box | PASS/FAIL panel for the last finished test (hidden until there is one) | `428` |
| Live metrics | Numbers and two charts that update while a test runs | `430-448` |
| Classic jobs | Table of running tests, with Stop buttons | `450-456` |
| Saved results | Table of past results with download links | `458-464` |
| Console | Scrolling text log | `466` |

### The two kinds of work the tab can ask the server to do

This distinction explains most of the code, so learn it first.

1. **One-shot action** - a short command that finishes and returns an answer: scan, list devices, pair,
   connect, disconnect, forget, read info, AVRCP volume test, AVRCP player button. classic.js sends
   `POST /api/classic/action` with `{dev, action, params}` and **waits** for the HTTP reply. This is the
   function `act()` (`classic.js:135`).
2. **Job** - a long-running test (A2DP stream, AVRCP button watch, HFP call, connect cycling). classic.js
   sends `POST /api/start` with `{tool, dev, params}`; the server starts a helper program and replies
   immediately with a job id. The helper then prints lines of text for as long as it runs, and those lines
   reach the browser through a separate always-open channel (below). This is `startJob()` (`classic.js:265`).

### How live data gets to classic.js

```
 helper program (bluforge_classic.py a2dp ...)     prints one JSON line per second to its stdout
          |
 server.py  _reader (server.py:201)                reads each line, wraps it as {"type":"line","job_id","dev","text"}
          |
 /api/stream  (Server-Sent Events)                 one-way, always-open HTTP stream server -> browser
          |
 app.js  EventSource (app.js:790-821)              finds the job's kind ("c_a2dp", ...), calls the "hooks"
          |
 classic.js  BF.hooks.push(function ...)           classic.js:486  <- classic.js lives here
          |
 onSample / onEvent / showResult                   update numbers, chart arrays, console, result box
```

- **SSE** (Server-Sent Events) is a browser feature: the page opens `/api/stream` once and the server keeps
  pushing messages down it. app.js owns that connection.
- **The `BF` object** (`window.BF`, defined in `app.js:19-23`) is how app.js hands things to classic.js. It has
  three members: `BF.hooks` (an array of functions that app.js calls for events), `BF.state()` (returns the
  latest status snapshot app.js downloaded from `/api/status`), and `BF.refresh()` (asks app.js to download
  that snapshot again). classic.js adds one function to `BF.hooks` at `classic.js:486`. app.js calls every hook inside
  a `try/catch` (`app.js:24-26`), so a bug inside classic.js's hook logs "hook error" to the browser console
  rather than breaking the stream.
- The status snapshot (`BF.state()`) contains the fields classic.js uses: `running` (list of running jobs, each
  with `id`, `kind`, `dev`, `uptime`...), `all_hci` (list of radio numbers), `classic_reserved` (radios currently
  reserved for Classic). It is refreshed every 5 seconds (`app.js:828`) and after every job start/finish event.

### Vocabulary used below

- **hci0, hci1 ...** - Linux's names for Bluetooth radios (controllers). "HCI" = Host Controller Interface.
  In the code a radio is just an integer (`dev`), and the display text is `"hci" + number`.
- **BlueZ** - the Linux Bluetooth software (daemon + libraries). Classic work goes through BlueZ.
- **Reserve** - tell the server "this radio is now for Classic". The BLE tabs use raw HCI commands that would
  fight BlueZ over the same radio, so a radio can be one or the other, never both (`index.html:278`).
- **DUT** - device under test, the Bluetooth address you typed or selected (format `AA:BB:CC:DD:EE:FF`).
- **A2DP** - Advanced Audio Distribution Profile: stereo audio streaming. Host = *source*, DUT = *sink*.
- **Codec** - the audio compression: SBC (mandatory), SBC-XQ, AAC, LDAC, aptX, aptX HD.
- **xrun** - an audio buffer under-run or over-run (a glitch or gap in the stream).
- **ACL** - Asynchronous Connection-Less, the Bluetooth data link A2DP packets travel on. The throughput
  numbers are counted from ACL bytes.
- **AVRCP** - Audio/Video Remote Control Profile: play/pause/next/volume buttons.
- **HFP** - Hands-Free Profile (phone calls). **AG** = Audio Gateway (the phone role, played by this host).
  **SLC** = service-level connection (the control handshake using AT text commands over **RFCOMM**, a
  serial-port-like channel). **SCO** = the voice audio link. **SDP** = Service Discovery Protocol (how a device
  lists what profiles it offers).
- **RSSI offset** - for BR/EDR the Linux "Read RSSI" command does not return dBm; it returns how far the
  signal is from the controller's "golden receive power range". `0` means inside the range, negative means
  too weak. The HTML captions say this (`index.html:445`) and so does the engine's own `rssi_unit` text
  (`bluforge_classic.py:1534`).
- **Link quality** - a number 0-255 the controller reports (`" / 255"` in the UI).
- **DOM** - the browser's in-memory tree of the page. `document.getElementById(...)` fetches one element.
- **IIFE** - "immediately invoked function expression", the `(function () { ... })();` wrapper that keeps
  every variable private to this file.

---

## 2. The HTML section (`index.html:261-467`)

The tab button is `<div class="lf-tab" data-tab="classic">` at `index.html:46`. The section is
`<section class="lf-section" id="tab-classic">` (`index.html:262`). CSS hides every `.lf-section` and shows the one
with class `show` (`style.css:132-133`); app.js moves the `show` class when you click a tab (`app.js:55-62`).
`classic.js` and `app.js` are loaded at the very bottom of the page (`index.html:478-479`), after the HTML exists,
and in that order, which matters (section 4.15).

All ids are prefixed `c_`. Naming pattern: `c_a2_*` A2DP inputs, `c_w_*` AVRCP watch, `c_hf_*` HFP, `c_cy_*` cycle,
`c_d_*` DUT info readouts, `c_l_*` live metrics. Everything the JS looks up exists in the HTML (I cross-checked every
`c_*` id both ways). Three HTML ids are never used by the JS (`c_devices_group`, `c_dut`, `c_live`); they are only
structural.

### 2.1 Banner and radio panel (264-280)

- `c_pf_banner` (264): starts with class `hidden`. Text and red/yellow styling are set by `banner()` (`classic.js:71`).
- `c_dev` (270): drop-down of radios. Filled by `populateDev()`.
- `c_radio_state` (272): shows a small coloured "pill" such as "reserved - idle". Filled by `applyEnablement()`.
- `c_reserve` (273): one button whose label flips between "Reserve for Classic" and "Release to BLE engine".
- `c_adapter` (276): text like `00:1A:7D:DA:71:13  powered  scanning`. Filled by `renderAdapter()`.
- A paragraph (278-279) explains why reservation exists.

### 2.2 Devices panel (282-301)

| id | default | purpose |
|---|---|---|
| `c_scan_dur` | `12` | inquiry (scan) length in seconds |
| `c_scan` | | button "Scan" (runs a real inquiry) |
| `c_list` | | button "Known devices" (just lists what BlueZ already knows) |
| `c_pin` | `0000` | legacy PIN, used only when a device asks for one while pairing |
| `c_addr` | empty, placeholder `AA:BB:CC:DD:EE:FF` | the **selected DUT address**. Every action reads this box |
| `c_devTable` | | table with columns Name, Address, Class, RSSI, Paired, Connected, Profiles, Action (297) |

### 2.3 Device under test (303-325)

Buttons `c_pair`, `c_connect`, `c_disconnect`, `c_remove` (labelled "Forget", styled `danger`), `c_info`
("Refresh info"), checkbox `c_autoinfo` (checked by default, "auto-refresh"). Below them a grid `c_dut` (a
CSS grid of label/value boxes) with ten readouts: `c_d_conn` Link, `c_d_state` A2DP transport, `c_d_codec` Codec,
`c_d_rssi` RSSI, `c_d_lq` Link quality, `c_d_txp` TX power, `c_d_vol` AVRCP volume, `c_d_bat` Battery,
`c_d_prof` Audio profile, `c_d_uuids` Profiles offered. All start as `-`.

### 2.4 A2DP test (327-360)

Row 1: `c_a2_dur` (60, "0 = until stopped"), `c_a2_src` (options `sweep`, `tone`, `noise`, `file`), a hidden field
`c_a2_file_f` containing text input `c_a2_file` (a path on the BluForge unit; only shown for "Audio file"),
`c_a2_codec` (empty = auto, or `sbc sbc_xq aac ldac aptx aptx_hd`), `c_a2_lat` latency ms (100), checkbox
`c_a2_reconnect` (unchecked; "reconnect + resume if the DUT drops").
Row 2: PASS thresholds `c_a2_act` active % (95), `c_a2_xr` max xruns (5), `c_a2_dc` max disconnects (0),
`c_a2_rssi` min RSSI offset (blank = off), then button `c_a2_start`.

### 2.5 AVRCP (362-394)

- `c_vol_steps` (default `0,32,64,96,127,64`) + button `c_vol_test`: the absolute-volume test.
- Six buttons with `data-plr="status|play|pause|stop|previous|next"` (371-376). `data-plr` is a custom HTML
  attribute (a "data attribute") that the JS reads as `button.dataset.plr`. These drive the DUT's media player (phones only).
- `c_keys` (381): five checkboxes (`PLAYPAUSE NEXT PREVIOUS VOLUMEUP VOLUMEDOWN`), all checked. Button-watch test:
  you press these buttons on the DUT and the test passes when each has been seen.
- `c_w_dur` timeout (120), button `c_w_start`.

### 2.6 HFP (396-415)

`c_hf_scn` (`call` = incoming call + SCO audio, `sco` = link + SCO audio, `slc` = control connection only),
`c_hf_dur` audio seconds (20), `c_hf_num` caller ID (`+15551234567`), `c_hf_ring` ring timeout s (15), button `c_hf_start`.

### 2.7 Cycle, result, live, tables (417-466)

- `c_cy_n` cycles (10), `c_cy_gap` gap s (3), `c_cy_ok` PASS if success >= % (100), button `c_cy_start`.
- `c_result` (428): `<div class="c-result hidden">`, filled by `showResult()`.
- `c_live` (432) grid with `c_l_job`, `c_l_t` Elapsed, `c_l_state` Transport, `c_l_rssi`, `c_l_lq`, `c_l_kbps`
  ACL TX rate, `c_l_xr` Xruns, `c_l_act` Active, `c_l_dc` Disconnects (starts `0`), `c_l_keys` AVRCP keys (starts `0`).
- Two `<canvas>` elements (445-446): `c_ch_rssi` and `c_ch_kbps`, both `width="520" height="130"` as attributes.
  A canvas is a rectangle the JS paints pixels into. The CSS overrides the size to 100 % width and 110 px height
  (`style.css:228`), and `drawChart` measures the real size at draw time (section 4.13).
- `c_jobsTable` (452): columns Job, Radio, Test, Uptime, Action.
- `c_resTable` (460): columns When, Test, DUT, Radio, Result, Duration, Detail, Download.
- `c_console` (466): `<div class="lf-console">` that receives text lines.

---

## 3. classic.js, in file order

### 3.1 File header, guard, constants, state (`classic.js:1-17`)

- `1-4`: comment. States the design: vanilla JS, no libraries; `/api/classic/*` for one-shot actions,
  reservation and results; `/api/start` for long tests; live data via app.js's SSE stream and `window.BF.hooks`.
- `5-6`: the IIFE wrapper and `"use strict"` (turns some silent mistakes into errors).
- `7-8`: `const BF = window.BF; if (!BF) { console.error(...); return; }`. If app.js did not load first, classic.js
  prints an error and does nothing at all. In `index.html` app.js is loaded first, so `BF` exists.
- `10` **`JOB_LABEL`**: maps a job *kind* to human text: `c_a2dp` "A2DP stream", `c_avrcp_watch` "AVRCP button
  watch", `c_cycle` "Connect cycling", `c_hfp` "HFP call". The kind strings are the server's job names
  (`CLASSIC_JOB_KINDS`, `server.py:48`); anything starting `c_` is a Classic job, and the code uses that
  prefix test (`kind.indexOf("c_") === 0`) in many places to tell Classic jobs from BLE ones.
- `11` **`ADDR_RE`**: regular expression for a Bluetooth address, six pairs of hex digits separated by colons.
  Accepts lower or upper case.
- `12` **`MAX_POINTS = 300`**: the chart keeps at most this many recent points per series (see 3.13).
- `14` **`pending`**: counter of in-flight one-shot actions. While it is above 0 the page locks its buttons.
- `15` **`lastDevices`**: the last device list received (array), kept so the table can be redrawn without asking again.
- `16` **`infoTimer`**: handle of the 5-second auto-refresh timer (3.8).
- `17` **`live`**: one object holding everything the live metrics need:

```js
const live = { rssi: [], kbps: [], done: [], rx: [], active: 0, total: 0, dc: 0, keys: 0, job: null };
```

  `rssi/kbps/done/rx` are arrays of recent chart points (RSSI offset; ACL TX rate; controller-completed rate; RX
  rate). `active`/`total` count samples for the "Active %" readout. `dc` counts disconnect events, `keys` counts
  AVRCP key events. `job` is the label of the current job; **it is written at `classic.js:392` and never read**
  anywhere in the file.

### 3.2 Small utilities (`classic.js:19-53`)

- `$(id)` (20): shorthand for `document.getElementById`.
- `el(tag, cls, text)` (21-26): creates an element, optionally sets its CSS class and its text. It uses
  `textContent`, not `innerHTML`, so text from the device or server can never be interpreted as HTML. That
  is the pattern the whole file follows: `innerHTML = ""` appears only to *clear* a container.
- `post(path, body)` (27-33): `fetch` a JSON POST. Steps: send; try to parse the reply as JSON (ignore if it is
  not JSON); **throw an `Error` if HTTP status is not 2xx or the reply contains `"ok": false`**; the error text is
  `j.error`, else `j.msg`, else the HTTP status text. Returns the parsed JSON on success. (The server puts the
  reason in `error` for bad input and one-shot action failures, and in `msg` when `/api/start` or `/api/reserve` refuses a
  request such as "radio busy"; that is why both are tried.)
- `get(path)` (34-38): the GET version; throws on non-2xx, no `ok` check.
- `num(id)` (39): returns the **trimmed text** of an input box. Note it returns a string, never a number. A blank
  box gives `""`, which the server treats as "use the default / leave this option out" (`_num` in `server.py:432`).
  All range checking (for example scan seconds must be 3-120) happens **on the server**, not in classic.js.
- `dev()` (40): the selected radio as an integer (`parseInt` of `c_dev`), or `null` if nothing is selected.
- `addr()` (41): the DUT address from `c_addr`, trimmed and forced to UPPER CASE.
- `fmt(v, unit, digits)` (42-45): formatting helper. `null`, `undefined` or `""` give `"-"`; otherwise the value
  as text plus the unit. The `digits` parameter (decimal places) exists but **no call in the file passes it**.
- `state()` (46): `BF.state()`, the latest status snapshot.
- `isReserved(h)` (47): is radio `h` in `state().classic_reserved`?
- `jobOn(h)` (48-50): the first running job on radio `h` whose kind starts with `c_` (a Classic job), or `undefined`.
- `bleJobOn(h)` (51-53): the first running job on `h` whose kind does **not** start with `c_` (a BLE job).

### 3.3 Console and banner (`classic.js:55-76`)

**`consoleLine(text, dev_, forceCls)`** (56-70) appends one line to `c_console`:

1. Creates a `<div>`. Colour class by the text's first 3 characters: `[*]` -> `s` (blue, progress), `[+]` -> `g`
   (green, success), `[!]` -> `y` (yellow, warning), `[x]` -> `r` (red, error), anything else `dim` (grey).
   If `forceCls` is given it wins.
2. Text is prefixed with `hciN: ` when `dev_` is not null/undefined.
3. Appends it, scrolls the box to the bottom, and deletes oldest lines while there are more than 400.

The engine (Python) uses the same `[*] [+] [!] [x]` tags in its output, so its lines come out coloured too.

**`banner(text, bad)`** (71-76): if `text` is empty/null, hides the banner (adds class `hidden`). Otherwise sets
class `c-banner` (plus `bad` for red) and the text. Setting `className` like this also removes `hidden`.

### 3.4 `applyEnablement()` - which buttons are usable (`classic.js:79-114`)

This function is the traffic light of the tab. It is called after almost every change. It is idempotent: it
recomputes everything from scratch and only touches the DOM where something changed.

Inputs it computes (79-84): `h = dev()`, `reserved` (radio selected and in `classic_reserved`), `job` (a Classic job
on this radio, if any), `ble` (a BLE job on this radio, if any), `locked = pending > 0`.

Rules, in code order:

1. Two lists (86-88). `needReserved` is every button that only makes sense on a reserved radio. `stopsStream` is the
   subset that must **not** be used while a Classic job is running on that radio (scan, pair, connect, disconnect,
   forget, and the four Start buttons). The name is slightly misleading: they are the actions that would
   disturb, or are refused by the server during, a running test (`SAFE_DURING_JOB`, `server.py:563`, is the
   server's matching list).
2. `setDis(node, off)` (89) sets `disabled` only if it would change (avoids flicker).
3. For each id in `needReserved` (90-94): disabled if `!reserved || locked`; also disabled if a Classic job is
   running and the id is in `stopsStream`.
   Consequence: `c_list`, `c_info`, `c_vol_test` stay usable during a test (they are read-only or safe).
4. The six AVRCP player buttons (`[data-plr]`, 95): disabled if `!reserved || locked`.
5. `c_reserve` (96): disabled while `locked`, or when a BLE job is on this radio and it is not reserved (you cannot
   reserve a radio that a BLE job is using). The button label is flipped between "Release to BLE engine" and
   "Reserve for Classic" (97-98).
   It is **not** disabled while a Classic job runs; if you click Release then, the server refuses (409 "stop it first")
   and the error shows in the banner.
6. The "Select" buttons in the device table (`#c_devTable .mini-btn`, 99-101): disabled while `locked`. The
   `needsIdle` condition is also tested, but no button is ever created with `needsIdle` (see 3.7), so only `locked`
   matters today.
7. The state pill (102-113): chooses text and colour with this priority: BLE job running ("BLE job jobN running",
   `busy`); else Classic job ("Classic job jobN (A2DP stream)", `busy`); else reserved ("reserved - idle", `ok`); else
   "not reserved" (`off`). It builds a signature string `pill|text` and stores it in `rs.dataset.sig`; the pill is
   only rebuilt when that string changes.

Stop buttons in the jobs table are not touched here, so Stop always works, even while another action is in flight.
Remember the **server re-checks all of this** (`run_classic_action`, `server.py:567`, and `Jobs.start`,
`server.py:175`); the disabled buttons are a convenience, not the security.

### 3.5 `populateDev()` - the radio drop-down (`classic.js:116-132`)

1. Remember the current choice (`prev`). Read `all_hci` and `classic_reserved` from the status snapshot.
2. Build a signature `"0,1,2|1"` (all radios | reserved radios). If it equals the one stored on the `<select>`
   (`dataset.sig`) return immediately. So the list is only rebuilt when radios appear/disappear or reservation changes.
3. Rebuild the options: text `hciN`, plus two spaces and `(Classic)` if that radio is reserved. Option value is the number.
4. Restore the selection: the previous choice if it still exists; otherwise the first reserved radio; otherwise
   the **last** radio in the list. The comment says why: "default away from hci0 (bluetoothctl's default)", so
   that BluForge does not fight with the system Bluetooth tools that use hci0 by default.

It is called from the render hook (3.14), which runs whenever app.js has a new status snapshot.

### 3.6 `act()` and `needAddr()` - the one-shot action runner (`classic.js:134-156`)

**`act(action, params, quiet)`** (135-151) is the single function through which every one-shot action goes.

1. Get the radio (`dev()`); throw "no radio selected" if none.
2. If **not quiet**: `pending++` and `applyEnablement()` (locks the buttons). The comment at 138-139 explains that
   quiet is for background polls: the 5-second auto-refresh used to grey every button out and flash them back.
3. `POST /api/classic/action` with `{ dev: h, action: action, params: params || {} }`. The server runs the
   helper program and waits for it (timeouts: scan 150 s, pair 120 s, connect 120 s, disconnect 40 s, forget 30 s,
   others 40 s; `ACTION_TIMEOUT`, `server.py:564`). The browser's `fetch` waits that long, and the UI stays
   locked meanwhile (there is no cancel button).
4. On success: if not quiet, clear the banner (`banner(null)`), return the JSON.
5. On failure: if not quiet, print `[x] action: message` to the console and show a red banner `"<action> failed: <message>"`.
   Then **re-throw**, so the caller can stop its own sequence. For quiet calls, nothing is shown at all.
6. `finally`: if not quiet, `pending--` and `applyEnablement()` again (unlocks).

Result shapes the callers rely on (checked in `bluforge_classic.py`): `devices`/`scan` return `{devices, adapter}`;
`info` returns `{device}`; `connect` returns `{connect_ms, transport_ms?, device}`; `avrcp-player` returns `{player}`;
`avrcp-volume` returns `{pass, steps}`.

**Where the engine's own log lines go.** After the helper finishes, the server publishes each of its non-JSON
output lines as an SSE "line" event with kind `c_action` (`server.py:608-609`). app.js treats any kind starting
`c_` as Classic and calls the hook, which prints the line in the console (3.14). Because the server uses
`subprocess.run(... capture_output=True)`, these lines all arrive **at once, after the action ends**, not while it
runs. During a long scan or pair you only see the GUI's own first line ("[*] scanning ..."), then a burst.

**`needAddr()`** (152-156): reads `addr()`. If it does not match `ADDR_RE`, shows a red banner "Enter or select a DUT
address first (AA:BB:CC:DD:EE:FF)." and returns `null`; otherwise returns the upper-case address. Callers do
`const a = needAddr(); if (!a) return;`.

### 3.7 Adapter and device list (`classic.js:158-223`)

**`shortProfiles(uuids)`** (159-166): the engine sends profile names as strings like `"Audio Sink (0000110b)"`
(`uuid_name`, `bluforge_classic.py:310`). The regex `^(.*) \(([0-9a-f]{8})\)$` splits the name from the 8-hex-digit
code; names `PnP`, `GAP` and `GATT` (housekeeping services) are dropped; the rest are joined with `", "`.
Any UUID the engine could not name arrives as a raw long UUID, does not match the regex, and is silently left out.

**`renderAdapter(a)`** (168-171): writes `c_adapter` as `<address>  powered|OFF  [scanning]`, or `-` if no adapter info.

**`renderDevices(list)`** (173-206), step by step:

1. Store the list in `lastDevices` (175).
2. Build a signature: the whole list as JSON plus `|` plus the currently typed address (176). If identical to
   the last drawn signature (`devSig`, 173) return. This stops the table being rebuilt (and your mouse-over
   being lost) every 5 seconds when nothing changed.
3. Clear `#c_devTable tbody`. If the list is empty, show one row spanning 8 columns: "No Classic devices known on
   this radio yet - run Scan with the DUT in pairing/discoverable mode." and stop.
4. For each device make a row. The row gets class `running` (a highlighted background, `style.css:170`) if its
   address equals the typed address **or** it is connected (186). So "selected" and "connected" look the same.
5. Cells: name (or `(unnamed)`), address, icon + class code (for example `audio-headset 0x240404`; the "class of
   device" is a number that says what kind of device it is), RSSI (BlueZ's `RSSI` property, which is dBm and only
   exists while a scan is hearing the device, unlike the "RSSI offset" in the DUT panel), Paired yes/no,
   Connected yes/no, profiles (via `shortProfiles`).
6. The last cell holds a **Select** button, made by the local helper `btn(label, fn, needsIdle)` (195-200). Clicking it
   puts the address in `c_addr`, redraws the table (so the highlight moves) and calls `refreshInfo(true)`. The helper's
   `needsIdle` argument is never passed, so no button carries the `data-needs-idle` marker.
7. Finish with `applyEnablement()` (205) so the fresh buttons get the right enabled/disabled state.

**`loadDevices(scan)`** (208-223), async:

1. If no radio is selected or it is not reserved: empty the table, clear the adapter text, return (210).
2. If `scan` is true: print `[*] scanning (BR/EDR inquiry) on hciN ...` and call `act("scan", { duration: num("c_scan_dur") || 12 })`.
   This is a *non-quiet* action, so the page locks, and the server really runs an inquiry for that many seconds
   (the server clamps/validates 3-120 s; classic.js does not).
3. If `scan` is false: `act("devices", {}, true)`, a quiet call listing what BlueZ already knows. (Note: this
   "devices" listing is not limited to Classic devices; it asks for `only_bredr=False`, `bluforge_classic.py:836`.)
4. `renderAdapter(r.adapter); renderDevices(r.devices);` and for scans print `[+] scan finished: N device(s)`.
5. `catch (e) {}` - errors are swallowed here because scans were already reported by `act()` and the quiet list
   is meant to be silent.

Used by: Scan button, "Known devices" button, radio change, reserve (after reserving), after pair/connect/disconnect/forget,
and when the tab is clicked.

### 3.8 DUT info panel (`classic.js:225-262`)

**`setText(id, v)`** (226): sets `textContent` only if the text differs. Used everywhere numbers refresh often,
so the browser does not redo layout for unchanged values.

**`renderInfo(d)`** (227-242) fills the ten boxes from the `device` object returned by the `info` action:

- Link (`c_d_conn`): "connected (paired)", "connected", "paired, not connected" or "not paired".
- A2DP transport (`c_d_state`): `d.transport.state` (BlueZ's media transport state: idle / pending / active...).
- Codec (`c_d_codec`): codec name, and for SBC when `sbc.bitrate_kbps_max` exists, a detail string:

```js
codec += " (" + t.sbc.freq / 1000 + " kHz " + t.sbc.mode + ", bitpool " + t.sbc.bitpool_min + "-" + t.sbc.bitpool_max
       + ", <= " + t.sbc.bitrate_kbps_max + " kbit/s)";
```

  (the DUT's advertised SBC capabilities; "bitpool" is SBC's quality knob).
- RSSI (`c_d_rssi`): `link.rssi` with the unit text " dB (offset)" - the offset explained above. `link` is `null` when the
  DUT is not connected; `const link = d.link || {}` (229) makes that safe, so all link boxes show `-`.
- Link quality `" / 255"`, TX power `" dBm"`.
- AVRCP volume: `t.volume + " / 127"` (AVRCP absolute volume runs 0-127), else `n/a`.
- Battery: `d.battery + " %"` (from BlueZ's Battery service), else `n/a`.
- Audio profile: `d.audio_card.active_profile` (the PipeWire sound-card profile, for example A2DP vs headset) else `-`.
- Profiles offered: `shortProfiles(d.uuids)`.

**`refreshInfo(force)`** (243-256), async:

1. Needs a radio, the radio reserved, and a syntactically valid address; else return silently.
2. Unless `force`, skip when a locked action is running (`pending > 0`) or the browser tab is hidden (`document.hidden`).
3. `act("info", { addr: a }, true)` (quiet), then `renderInfo(r.device)`.
4. On any error: set all ten boxes to `-`, and set the Link box to "unknown to this radio (scan first)" if the
   error message matches `/not known/` (the engine's message when BlueZ has never seen the address), else `-`.
   **Any** error blanks the panel, including a momentary one.

**`startInfoTimer()`** (257-262): `setInterval` every 5000 ms. Each tick calls `refreshInfo(false)` only if
`#tab-classic` has class `show` (the tab is visible) **and** `c_autoinfo` is ticked. It is started once at load (line 625).
Every tick makes the server start a short helper process (`info`), so auto-refresh costs a process spawn per 5 s.

### 3.9 `startJob()` - starting a long test (`classic.js:264-278`)

```js
async function startJob(tool, params) {
  const h = dev();
  const a = needAddr();
  if (a === null || h === null) return;
  params.addr = a;
  try { const r = await post("/api/start", { tool: tool, dev: h, params: params }); ...
```

1. Needs a radio and a valid DUT address (else banner and stop).
2. Adds the address to `params`.
3. `POST /api/start {tool, dev, params}`. `tool` is one of the four job kinds. The server validates every parameter
   (`build_classic_argv`, `server.py:472`), builds the command line, starts the helper process, returns
   `{ok, msg, job_id, argv}`. If the radio is not reserved, or is busy, the server answers HTTP 409 with `ok:false`
   and `post()` throws the server's `msg`.
4. Success: console `[*] started A2DP stream as job3`, clear banner.
5. Failure: console `[x] could not start ...` and red banner with the reason.

Note what it does **not** do: it does not reset the live display or lock anything. That happens when the server's
"started" event comes back over SSE (3.14).

### 3.10 Result box (`classic.js:280-321`)

**`showResult(sm)`** (281-312) draws the green PASS / red FAIL panel from a **summary** object (the final line a job
prints; see 3.14).

1. Box class becomes `c-result pass` or `c-result fail` (green/red, `style.css:221-223`). Content is cleared.
2. Heading (285-286): `PASS - A2DP stream on AA:BB:... (hci1)`. The label is looked up with
   `JOB_LABEL["c_" + String(sm.test).replace("-", "_")]`, which turns the summary's test name (`a2dp`, `avrcp-watch`,
   `cycle`, `hfp`) into the job kind (`c_a2dp`, `c_avrcp_watch`, ...). If there is no label (the volume test is
   `avrcp-volume`), it falls back to the raw test name.
3. A bullet list. First every entry of `sm.fail_reasons` (empty on a pass); then per test type:
   - **a2dp** (290-297): duration, codec, `active_pct` ("active" = share of time the A2DP transport was in the
     `active` state), xruns, disconnects, reconnect times in ms; RSSI offset min/avg/max with an explanation; ACL TX
     throughput min/avg/max with the measurement source (`throughput_src`: "hci-monitor (exact ACL bytes)" or the less
     exact "hciconfig counters (not exact)"); an "exact totals" line from `acl_total` (bytes sent / completed by the
     controller / received, and the average kbit/s); and AVRCP keys seen as JSON text.
   - **hfp** (298-302): scenario, RFCOMM channel and time, whether the service link came UP or FAILED, SCO setup
     time; unless the scenario is `slc`: bytes of audio sent (and percent of the expected amount), received, and
     whether the DUT answered the ring; RSSI offset.
   - **cycle** (303-306): `ok/cycles cycles OK (pct %)`, and connect-time and A2DP-transport-ready-time stats
     (min/avg/p95/max; p95 = the value 95 % of cycles were faster than).
   - **avrcp-watch** (307-309): the keys seen (JSON), and the number of volume changes.
4. `if (sm.saved_as) li("saved: " ...)` (310). See "Things I noticed" - this line cannot appear for a live result.
5. Append the list.

Local helper `li(t)` (289) appends one `<li>`.

**`showVolumeResult(r)`** (314-321): builds a fake summary `{pass: r.pass, test: "avrcp-volume", addr: addr(), hci: dev(),
fail_reasons: []}`, calls `showResult` to get the heading, then appends one bullet per step:
`set 64 -> read-back 64  sent, accepted by BlueZ (35 ms)` or `... NOT accepted after N ms`, and a final note:
"Command path only: BlueZ does not expose a DUT-side confirmation - verify the level change on the DUT." That is,
the test proves BlueZ accepted the volume command and the transport's `Volume` property read back, not that the DUT
audibly changed.

### 3.11 Saved results table (`classic.js:323-359`)

**`loadResults()`** (324-359), async. Called when the tab is clicked and when a job finishes. It is **not** called at
page load.

1. `GET /api/classic/results`. On failure, returns silently and leaves the old table. The server (`classic_results`,
   `server.py:633`) lists the newest 40 `*.json` files in `logs/classic/` and returns for each: `file`, `csv`
   (name of the matching `.csv` if it exists), `mtime`, and fields lifted from the summary inside the file
   (`test`, `addr`, `hci`, `pass`, `duration_s`, `fail_reasons`, `summary`).
2. Clear the table body. If there are none: one row "No saved Classic results yet." (8 columns).
3. For each result make a row with class `pass` or `fail` (green/red row background, `style.css:229-230`). Cells:
   when (`mtime * 1000` -> `toLocaleString()`; the server gives seconds, JS wants milliseconds), test, DUT address,
   radio (`hciN` or `-`), PASS/FAIL, duration, detail.
4. Detail (340-348): the joined `fail_reasons` if any; otherwise a one-line digest per test: a2dp
   `codec, active N %, xruns N`; cycle `ok/cycles cycles`; hfp `SLC up|failed, SCO N ms`; avrcp-watch `keys A,B`.
5. Download cell (349-355): up to two links, "json" and "csv", `href="/api/classic/result/" + encodeURIComponent(file)`.
   The server only serves names matching `[A-Za-z0-9_.-]+\.(json|csv)` and sends them as attachments
   (`server.py:735-747`). The CSV only exists if the test recorded samples (the AVRCP watch records none).
   The `.json` holds `summary`, `events` and `samples`.

The volume test and the other one-shot actions are **not** saved; only the four jobs write files.

### 3.12 Jobs table (`classic.js:361-388`)

**`renderJobs()`** (363-388): shows only running jobs whose kind starts with `c_`.

1. Signature = the job ids joined by commas (368). The comment at 366-367 explains why ids only: the uptime changes
   at every refresh, and rebuilding the table would destroy the Stop button under your cursor.
2. If the signature is unchanged, just update the Uptime cell (`cells[3]`) of each row in place and return (369-372).
3. Otherwise rebuild: row class `running`; cells job id, `hciN`, test label, uptime (`12.3s`), and a **Stop** button.
4. Stop click (383-385): `POST /api/stop {job_id}`; on failure red banner "Stop failed: ...". Success shows nothing
   directly; the table updates when the server's "finished" event arrives. The server sends SIGINT (Ctrl-C) first
   and waits up to 5 s, then SIGTERM for 3 s (`Jobs.stop`, `server.py:239`), so the request can take several seconds. The helper
   catches both signals and sets an internal stop flag (`bluforge_classic.py:1949-1950`); its test loops check that flag, so by my reading
   it ends the run early but still prints its summary and saves its files (I read the A2DP, cycle, watch and HFP loops; I did not run them).

Uptime in this table is whatever the last status snapshot said, so it visibly moves in steps of roughly 5 seconds.
When there are no jobs the table body is simply empty (no placeholder row).

### 3.13 Live metrics and charts (`classic.js:390-483`)

#### `resetLive(jobLabel)` (391-397)
Called when a Classic job starts. Empties the four chart arrays, zeroes `active`, `total`, `dc`, `keys`, remembers the
label, writes the label into `c_l_job`, sets the readouts `c_l_t c_l_state c_l_rssi c_l_lq c_l_kbps c_l_xr c_l_act`
to `-` and `c_l_dc`, `c_l_keys` to `0`, then `drawAll()` (so both charts show "no data yet").

#### What a job prints (so you know what `onSample` receives)
The helper prints one JSON object per line. Three kinds, told apart by the `m` field (the code is
`emit(m=..., ...)` in `bluforge_classic.py:78` and `Recorder.event/sample`, `:1257-1267` (event at 1257, sample at 1263); summary at `:1299`):

```
{"m":"sample","t":12.01,"state":"active","rssi":0,"tx_power":4,"lq":255,"tx_kbps":231.4,"done_kbps":229.9,"rx_kbps":3.2,"xruns":0,"volume":64}
{"m":"event","t":3.2,"event":"disconnect"}
{"m":"summary","test":"a2dp","addr":"AA:BB:...","hci":1,"pass":false,"fail_reasons":["..."], ...}
```

(field names real, numbers invented). `t` is seconds since the run started. A2DP emits one **sample** per
interval, default 1.0 s (`--interval`, `bluforge_classic.py:1909`, a2dp argparse; the GUI does not set it). Events
are things that happen (disconnect, `avrcp_key`, `volume`, `ready`, `slc`, `sco_up`...). The summary is printed once
at the end.

#### `onSample(s)` (398-424)
There are two shapes of sample, handled by two branches.

**Branch A - a connect-cycle row** (399-405). Detected by `s.connect_ms !== undefined || s.error !== undefined`. (The
cycle job's per-iteration record has `n`, `ok`, `connect_ms` and maybe `error`; `bluforge_classic.py:1632-1665`.)
It sets "Transport" to `cycle N: ok` or `cycle N: FAILED`, Elapsed to `s.t + " s"`, and if the row has an RSSI it pushes it to
`live.rssi` and shows it. Then `drawAll()` and return. The cycle path does not touch the throughput arrays.

**Branch B - a normal sample** (406-423), step by step:

1. Elapsed = `s.t.toFixed(0) + " s"`; Transport = `s.state || "-"`; RSSI = `fmt(s.rssi, " dB")`; Link quality =
   `fmt(s.lq, " / 255")`.
2. ACL TX rate (410-411): `"231.4 kbit/s"`, and if `done_kbps` is known and differs from `tx_kbps` by more than
   0.5, append `" (completed 229.9)"`. Missing `tx_kbps` shows `-`.
3. Xruns = `fmt(s.xruns)`.
4. "Active %" (413-415): `live.total++`; if `s.state === "active"` then `live.active++`; show
   `100 * active / total` with one decimal. This is computed over the **whole run** (counters never trimmed).
5. Chart data (416-421): if `s.rssi` is not null, push to `live.rssi` (0 is a valid value, hence the explicit null
   check). If `s.tx_kbps` is not null, push to all three throughput arrays together: `kbps` = TX, `done` = completed
   (falls back to the TX value when missing), `rx` (falls back to 0 when missing). Pushing together keeps the three
   series the same length.
6. Trim (422): each of the four arrays loses its oldest element when it exceeds `MAX_POINTS` (300), so the chart is a
   sliding window of the latest 300 points (5 minutes at the default 1 s interval).
7. `drawAll()`.

Which fields come from where: when the exact ACL meter is unavailable the engine falls back to the system packet
counters and sends only `tx_kbps` (`bluforge_classic.py:1480-1491`); then `done_kbps` and `rx_kbps` are null and the
chart's "completed" line duplicates TX and "RX" sits at 0 because of the fallbacks in step 5.

#### `onEvent(e)` (425-432)
1. `disconnect` event: `live.dc++`, update `c_l_dc`. `avrcp_key` event: `live.keys++`, update `c_l_keys`.
2. Console line: `[!] +<t>s <event> k=v k=v ...` where the extras are every key except `m`, `event` and `t`, written
   `key=value`. For the `ready` event the extras are skipped (`skip = { ready: 1 }`, 428) because that event carries a
   large info dictionary that would print as clutter.
3. Colour forced to `r` (red) for `disconnect`, `y` (yellow) for everything else. No `hciN:` prefix (the second
   argument is `null`).

#### `drawChart(cv, series, unit, opts)` (434-476) - the chart painter

There is no charting library. This function paints on a `<canvas>` using the 2D drawing API. `series` is
`[{ data: [numbers], color, label }]`; `unit` is text; `opts.zero` and `opts.avg` are switches.

Step by step:

1. **Size and sharpness** (438-443). `dpr = window.devicePixelRatio` (1 on a normal screen, 2 on a high-density
   screen). `w`/`h` are the element's on-screen size in CSS pixels (`clientWidth`/`clientHeight`), falling back to 520x130
   if the canvas is hidden (a hidden element reports size 0). If the canvas's internal pixel grid is not `w*dpr` by
   `h*dpr`, resize it. `setTransform(dpr,0,0,dpr,0,0)` makes later drawing use CSS-pixel coordinates; `clearRect`
   wipes the old drawing; font 10 px.
2. **No data** (445-446): if every series is empty, write grey "no data yet" at the top-left and return.
3. **Vertical range** (447-449):
   - If `opts.zero` (the throughput chart): `lo = 0`, always. `hi = max * 1.1` (10 % headroom), or `1` if the max is 0 or
     negative (avoids dividing by zero). So the **zero baseline is pinned**; the comment at 434-435 says why: otherwise a
     nearly-flat throughput would be auto-zoomed into a line that looks like a big swing.
   - Else (the RSSI chart): `lo = min`, `hi = max` of all data; if they are less than 4 apart, widen to a 4-unit band
     centred on the middle (so a constant RSSI of 0 is drawn as a flat line in a band from -2 to 2, not as a huge zoom).
4. **Layout** (450-453): margins `pad = {l:44, r:8, t:8, b:16}` (left room for numbers, bottom room for the legend). `n` =
   the length of the longest series. `X(i)` spreads indices `0..n-1` evenly across the width (or all at the left edge if
   `n <= 1`); `Y(v)` maps `hi` to the top and `lo` to the bottom. **The horizontal axis is sample number, not time**,
   and there are no labels on it.
5. **Grid and axis labels** (454-458): three light horizontal lines at `lo`, middle, `hi`, each labelled at the left. Labels
   have 1 decimal if the range is under 20, else 0.
6. **Lines** (459-464): for each non-empty series set its colour, line width 1.8 for the first series and 1.2 for the
   others, `moveTo` the first point and `lineTo` the rest, then `stroke`.
7. **Legend** (465-475): along the bottom edge, for each non-empty series: a small colour swatch (8x3), then text
   `label + " " + current + " " + unit` where *current* is the **last** value in the series (1 decimal), and if
   `opts.avg` is on, `" (avg X)"` where X is the mean of the series' data. Each entry is placed to the right of the
   previous using `g.measureText(t).width`. Nothing wraps; a legend wider than the canvas is cut off.

#### `drawAll()` (477-483)
Draws both charts:

| Canvas | Series (colour) | Unit | Options |
|---|---|---|---|
| `c_ch_rssi` | RSSI offset (blue `#1f5fbf`), no label | `dB` | none: auto range, legend shows current only |
| `c_ch_kbps` | `TX` (green `#0a8a3a`), `completed` (orange `#d9822b`), `RX` (purple `#7a4fbf`) | `kbit/s` | `zero: true, avg: true`: baseline pinned at 0, legend shows current and average |

`drawAll` is called from `resetLive`, `onSample`, the tab click handler, a window resize, and once at startup.

### 3.14 The SSE hook - how events reach the tab (`classic.js:485-509`)

`BF.hooks.push(function (evt, kind, dev_) { ... })` registers one function. app.js calls it for:
(a) `{type:"render"}` after every status snapshot (`app.js:312`); (b) every `status` event that is not a full snapshot
(`app.js:796-801`), e.g. a job started or finished; (c) every `line` event whose job kind starts with `c_`
(`app.js:818`). The `kind` argument is not used by classic.js. In order:

1. **Render** (487): `populateDev(); applyEnablement(); renderJobs();` and return. This is why these three are
   re-evaluated every 5 seconds and on every job event.
2. **Job status** (488-497), only for Classic jobs:
   - `started`: hide the result box and call `resetLive("A2DP stream (job3)")`. Because the server broadcasts to
     every open browser, every browser tab resets, not only the one that clicked Start.
   - `finished`: console `[*] A2DP stream job3 finished (exit N)`; set the Job box to `idle (last: job3)`; call
     `loadResults()` (so the new saved file appears) and `refreshInfo(true)`. Exit code 0 = PASS, 2 = FAIL verdict
     (`verdict()` returns those, `bluforge_classic.py:1305`); anything else suggests a crash or kill.
   - Return either way.
   (A "reservation" status event has no `job`, so it matches neither branch and falls through to the next
   `if`, which returns because it is not a line.)
3. **Not a line** (498): ignore everything else.
4. **Line** (499-508): take `evt.text`. If it starts with `{` it is a machine line: `JSON.parse` it (if that fails,
   drop it silently); then by `o.m`: `"sample"` -> `onSample(o)`, `"event"` -> `onEvent(o)`, `"summary"` ->
   `showResult(o)` and un-hide the result box (505). An `m` value other than these three is dropped silently (the
   engine only emits those three).
5. Otherwise it is human text: `consoleLine(text, dev_)`, which prefixes `hciN:` from the event's radio number.
   This covers both a job's `[*] ...` progress lines and the one-shot `c_action` log lines.

Reading the whole thing as a story for an A2DP run: Start click -> `startJob` -> server starts helper -> SSE
`started` -> `resetLive` -> each second a `sample` line -> `onSample` -> charts move -> helper prints `summary` ->
`showResult` -> helper exits -> SSE `finished` -> `loadResults` + `refreshInfo`.

### 3.15 `wire()` - every event handler (`classic.js:511-611`)

`wire()` is called once at load (624) and attaches all the click/change listeners. An *event listener* is a function
the browser runs when something happens to an element. Taken in code order:

**Radio**
- `c_dev` change (513): `applyEnablement(); loadDevices(false); refreshInfo(true)`.
- `c_reserve` click (514-524), the reserve/release flow:
  1. `h = dev()`; stop if none. `on = !isReserved(h)` - the button toggles.
  2. `POST /api/classic/reserve {dev: h, on: on}`. Server side (`Jobs.reserve` / `Jobs.release`, `server.py:148-166`):
     reserve fails if that radio is running a BLE job ("stop it first"); release fails if it is running a Classic job.
     The reservation is a set held in the server's memory; I did not see it saved to disk in these functions.
     The server also broadcasts a `reservation` status event (`server.py:805`) so every browser refreshes.
  3. Print `[+] <server message>`.
  4. `await BF.refresh()` - ask app.js to re-download `/api/status` so `classic_reserved` is current; the render hook
     then updates the drop-down label "(Classic)", the pill and the buttons.
  5. If it was a **reserve**: `await loadPreflight()` (checks the machine for missing tools, 3.16) then
     `loadDevices(false)` (fills the device table). If it was a **release** nothing more happens.
  6. `catch`: console `[x] message` and a red banner with the same message.
- `c_scan` click (525): `loadDevices(true)`. `c_list` click (526): `loadDevices(false)`.

**Address box**
- `c_addr` `input` (527): fires on every keystroke; redraws the device table so the highlighted row follows what you type.
- `c_addr` `change` (528): fires when you finish editing (leave the box or press Enter); calls `refreshInfo(true)`.

**Pair / Connect / Disconnect / Forget / Info** (all begin with `needAddr()`; each calls `act()` which already reports
errors, so their `catch` just returns)
- `c_pair` (530-536): console `[*] pairing <addr> ...`; `act("pair", { addr, pin: num("c_pin") || "0000" })`;
  on success `[+] paired + trusted <addr>` (the engine pairs, then marks the device *trusted* so it may reconnect by
  itself); then `loadDevices(false); refreshInfo(true)`. The server accepts a PIN of 1-16 digits only.
- `c_connect` (537-545): console `[*] connecting ...`; `act("connect", { addr })`; success prints
  `[+] connected in N ms` plus `, A2DP transport ready after M ms` when `transport_ms` is present. The connect
  command waits up to 10 s for the audio transport by default (`--wait-audio`, `bluforge_classic.py:1895`); the GUI
  does not pass that option. Then refresh the list and info.
- `c_disconnect` (546-550): `act("disconnect", { addr })`, then refresh. No GUI message on success (the engine's own log
  line arrives in the console).
- `c_remove` (551-556): asks `confirm("Forget <addr>? This deletes its pairing from hciN.")`; if confirmed,
  `act("remove", { addr })` (the engine removes the device from the adapter), then refresh.
- `c_info` (557): `if (needAddr()) refreshInfo(true)`.

**A2DP** 
- `c_a2_src` change (559-561): shows the file-path field only when the source is `file` (toggles class `hidden`).
- `c_a2_start` click (562-574): reads the source; if it is `file`, requires a non-empty path (else banner
  "Enter the audio file path (on this unit).") and turns it into `"file:" + path`. Then
  `startJob("c_a2dp", {...})` with these parameters (all text from `num()` except the checkbox):

| params key | from | note |
|---|---|---|
| `duration` | `c_a2_dur` | seconds, 0 = until stopped |
| `source` | `c_a2_src` / file | `sweep`, `tone`, `noise`, or `file:<path>` |
| `codec` | `c_a2_codec` | empty = auto |
| `latency` | `c_a2_lat` | ms |
| `min_active_pct` | `c_a2_act` | PASS threshold |
| `max_xruns` | `c_a2_xr` | PASS threshold |
| `max_disconnects` | `c_a2_dc` | PASS threshold |
| `min_rssi` | `c_a2_rssi` | blank = no RSSI rule |
| `reconnect` | `c_a2_reconnect` | `true`/`false` |

  The server checks the file is an existing absolute path with an audio extension (`_source`, `server.py:451`) and
  reports a problem in the banner.

**AVRCP**
- `c_vol_test` click (575-583): `act("avrcp-volume", { addr, steps: num("c_vol_steps") })`, then
  `showVolumeResult(r)` and un-hide the result box. This is a one-shot action, so it is allowed while a job is
  running (it is in `SAFE_DURING_JOB`).
- Six player buttons (584-594): `document.querySelectorAll("[data-plr]")` finds them, each gets a click listener
  calling `act("avrcp-player", { addr, cmd: b.dataset.plr })`. On success, console `[+] DUT player: <name> status=<status>
  [track=<title>]`. A headset has no media player; the engine then errors with an explanation (shown by `act`).
- `c_w_start` click (595-598): collects the ticked boxes inside `#c_keys` (`[...document.querySelectorAll(...)].map(c => c.value)`)
  into a comma string like `PLAYPAUSE,NEXT,...`, then `startJob("c_avrcp_watch", { duration: num("c_w_dur"), expect: keys })`.
  If you untick everything, `expect` is empty and the engine only fails the test if no AVRCP activity at all is seen.

**HFP, Cycle**
- `c_hf_start` (599-601): `startJob("c_hfp", { scenario, duration, number, ring_timeout })` from `c_hf_scn`, `c_hf_dur`,
  `c_hf_num`, `c_hf_ring`.
- `c_cy_start` (602-604): `startJob("c_cycle", { count, gap, min_success })` from `c_cy_n`, `c_cy_gap`, `c_cy_ok`.

**Tab click and resize**
- (606-609) `document.querySelector('.lf-tab[data-tab="classic"]')` click: `loadPreflight(); loadResults(); drawAll();` and, if the
  selected radio is reserved, `loadDevices(false); refreshInfo(true)`. app.js registered its own tab-click handler
  earlier (it loads first), so by the time this runs the Classic section is already visible; that matters because
  `drawChart` reads the canvas's real size and a hidden canvas reports zero.
- (610) `window` `resize` -> `drawAll` (repaint charts for the new width).

### 3.16 `loadPreflight()` and start-up (`classic.js:613-627`)

**`loadPreflight()`** (613-622): `GET /api/classic/preflight` (the server runs `bluforge_classic.py preflight` and
returns a dictionary; `server.py:616`). Steps:

1. Start from `pf.notes` (a list of text warnings like "hcitool missing ...", "PyGObject missing ...",
   "no PipeWire user session: A2DP streaming unavailable").
2. Add `pf.error` if present.
3. If the unit has fewer than 2 Bluetooth controllers, add a note that reserving the only one disables BLE jobs on it (618).
4. `banner(notes.length ? "Check: " + notes.join("  |  ") : null, pf.gi === false)`. The banner is **red** only
   when `pf.gi === false` (the Python GObject library is missing, so the engine cannot run at all), otherwise yellow;
   no notes hides it.
5. If the request itself fails: red banner "Could not run the Classic preflight: ...".

It is a function declaration, so it is usable from `wire()` even though it appears below it (JavaScript "hoisting").
It runs when the tab is clicked and after a successful reserve, **not** at page load.

**Start-up** (624-626): `wire()` attaches the handlers; `startInfoTimer()` starts the 5 s timer; `drawAll()` paints
the two charts as "no data yet". The radio drop-down fills itself later, when app.js delivers the first status snapshot
and fires the render hook.

---

## 4. Three end-to-end walkthroughs

### A. Reserving a radio
Select `hci1` in `c_dev` -> `loadDevices(false)` runs but returns early (not reserved) and clears the table ->
click "Reserve for Classic" (`classic.js:514`) -> `POST /api/classic/reserve` -> server adds 1 to its reserved set and
broadcasts -> `BF.refresh()` -> render hook -> `populateDev` relabels the option `hci1  (Classic)`,
`applyEnablement` turns the pill green "reserved - idle" and enables Scan/List/Pair/... -> `loadPreflight` may show a
warning banner -> `loadDevices(false)` fills the table with known devices.
Releasing: click again ("Release to BLE engine"); the radio goes back to the BLE tabs; the Classic buttons grey out.

### B. Pairing a device (a one-shot action)
Type or Select an address -> click Pair (`classic.js:530`) -> `needAddr()` OK -> console `[*] pairing ...` ->
`act("pair")` increments `pending` (everything greys out) -> `POST /api/classic/action` -> server runs the helper (up to 120 s) ->
reply `{ok:true, device:{...}, log:[...]}` -> `pending--` (unlock) -> console `[+] paired + trusted` -> the helper's own
log lines arrive over SSE as `c_action` lines and appear in the console -> `loadDevices(false)` and `refreshInfo(true)`
update the table and DUT panel.

### C. An A2DP test, click to PASS/FAIL
Click Start (`classic.js:562`) -> `startJob("c_a2dp", ...)` -> `POST /api/start` -> `[*] started A2DP stream as jobN` ->
server starts the helper -> SSE `status started` -> hook: hide result box, `resetLive` -> `render` marks the radio busy and
disables Start/Scan/Pair/... -> each second: JSON `sample` line -> `onSample` -> numbers and charts update; `event`
lines -> `onEvent` -> console + counters -> at the end: `summary` line -> `showResult` shows the green/red box ->
helper writes `logs/classic/<time>_a2dp_<addr>.json/.csv`, exits -> SSE `status finished` -> console line,
`loadResults()` adds the row, `refreshInfo(true)`.
Pressing Stop (`classic.js:383`) sends Ctrl-C to the helper; it ends early but still prints the summary and saves.

---

## 5. Things I noticed

Ordered roughly by how likely a user is to see them. "Verified" means I read both sides of the interface;
"likely" means I reasoned from the code and did not run it.

1. **Cycle results show "null s" in the Duration column (verified).** `classic.js:339` tests
   `x.duration_s !== undefined`. The server always includes the key, set to `null` when the summary lacks it
   (`server.py:633-656`, `"duration_s": sm.get("duration_s")`), and the cycle summary has no `duration_s`
   (`bluforge_classic.py:1668-1673`). In JavaScript `null !== undefined` is true, so the cell becomes `null + " s"` =
   "null s". (`hci` on the line above is handled correctly with a null check; duration is not.) The same fix applies to any other
   summary without a duration.
2. **The "saved:" bullet can never appear (verified).** `showResult` prints `sm.saved_as` (`classic.js:310`). But in
   `verdict()` the summary line is emitted *before* `rec.save()` (`bluforge_classic.py:1299` vs `:1304`), and `save()` only sets
   `summary["saved_as"]` after the JSON file is written (`:1282`). So neither the live summary nor the saved file contains
   it. The user still sees the path in the console via the engine's "[*] results saved: ..." line.
3. **HFP runs feed an A2DP-shaped display (verified).** HFP samples carry `sco_tx_Bps`, `sco_rx_Bps`, `rssi`, `lq`,
   `tx_power` (`bluforge_classic.py:1815`) - no `state`, `tx_kbps` or `xruns`. `onSample` therefore shows Transport `-`,
   ACL TX rate `-`, Xruns `-`, **Active `0.0 %`** (the counter increments `total` but never `active`), and the throughput chart stays
   "no data yet". Only RSSI and link quality are live during an HFP call; the SCO byte rates are never displayed.
4. **"(avg ...)" in the throughput legend is a 300-sample window average, not a run average.** The legend averages
   `s.data` (`classic.js:470`) after the arrays were trimmed to `MAX_POINTS` (422). For runs longer than about 5 minutes at
   1 sample/s the label is misleading. Contrast: the "Active" readout uses whole-run counters.
5. **A one-point chart draws nothing.** With `n <= 1`, `X(0)` is a single point and `moveTo` + `stroke` paints no line
   (`classic.js:452, 459-464`). The legend shows the value but the plot stays empty until the second sample.
6. **Possible double counting after an SSE reconnect (likely, not run).** The server keeps the last 500 non-sample
   `line` events and replays them to any new stream (`server.py:107`, `837-839`). app.js remembers job kinds in `jobMeta`
   for the page's lifetime (`app.js:787-800`), so after a brief disconnect ("reconnecting..." in the header) replayed
   events of Classic jobs, including old ones, would again reach `onEvent` and `showResult`: console lines duplicate, and
   `live.dc` / `live.keys` (global, not per job) increase for events that already happened. The result box would be
   redrawn from the last replayed summary.
7. **Page reload during a running job (likely, not run).** The `started` status event is not part of the replay (only
   `line` events are), so after a reload the "Job" box says `idle` while the radio pill correctly says "Classic job ...".
   Charts begin empty because samples are deliberately not replayed (`server.py:229`).
8. **Releasing a radio leaves stale data on screen.** After "Release", the reserve handler does nothing more
   (`classic.js:522` is inside `if (on)`), so the old device table and DUT info stay visible until something else redraws
   them, even though the radio is no longer Classic's.
9. **Selected and connected rows look identical.** Both get class `running` (`classic.js:186`), so you cannot see which
   row is selected when another device is connected.
10. **Any error blanks the DUT info panel** (`classic.js:251-255`), including a one-off timeout; only the message
    "not known" gets a helpful text.
11. **Responses are not tied to the selection that asked.** `act()` re-reads `dev()` when called, and renders whatever
    returns later. Switching radio or DUT while a request is in flight can show the old answer under the new selection
    (`loadDevices`, `refreshInfo`). Low impact because the quiet refresh repeats every 5 s.
12. **Any successful action clears the preflight warnings.** `act()` calls `banner(null)` on success (`classic.js:143`) and
    `startJob` does the same (273), so the "Check: tool missing..." banner disappears after the first scan or test even if
    the problem remains.
13. **Engine progress is not live for one-shot actions.** Because the server collects output with
    `subprocess.run` and publishes it afterwards (`server.py:608-609`), pair/connect/scan show no engine messages until they finish.
14. **Start buttons are not locked on click.** `startJob` sets no lock; the button only greys out after the server's
    `started` event triggers a status refresh. A quick double click sends two start requests; the second is refused by the server
    ("already running job"), which shows as a red banner.
15. **Possible clipped legend.** The legend is laid out left to right with no wrapping (`classic.js:466-475`). Three
    entries with "(avg ...)" are roughly 400-500 px of text at 10 px font, while the CSS lets a chart column be as narrow as 300 px
    (`style.css:226`). I did not check this in a browser.
16. **Small dead or unused items.** `live.job` is write-only (392); `fmt`'s `digits` argument is never passed (42);
    `btn()`'s `needsIdle` is never true (195-199) so the `needsIdle` test at 100 is inert; the hook's `kind` argument is
    unused (486); HTML ids `c_devices_group`, `c_dut`, `c_live` are not referenced by the JS; the `skip` object holds a single entry.
17. **Unnamed UUIDs are hidden.** `shortProfiles` keeps only strings in the form `Name (8hex)` (`classic.js:162-163`); vendor UUIDs
    the engine could not name never appear in the Profiles columns.
18. **Minor format inconsistency.** Cycle rows show Elapsed as `s.t + " s"` (for example `12.34 s`), normal samples use
    `toFixed(0)` (`12 s`) (`classic.js:401` vs `406`).

Good practices worth keeping: all dynamic text goes through `textContent` (no HTML injection from device names);
signatures (`sig`, `devSig`, `jobsSig`) prevent needless DOM rebuilds so buttons are not replaced while you click them;
the server independently re-validates every parameter and enforces reservation/busy rules, so the JS is not the only gate.
