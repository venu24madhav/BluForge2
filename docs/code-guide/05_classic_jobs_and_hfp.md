# 05 - Classic jobs and the HFP Audio Gateway

**What this file covers**

| File | Lines | What |
|---|---|---|
| `src/bluforge_classic.py` | 1308 - 1975 ("PART 2": from `job_a2dp` to the end) | the four long-running **jobs** (`job_a2dp`, `job_avrcp_watch`, `job_cycle`, `job_hfp`), two HFP helpers (`sdp_hfp_channel`, `tone8k`), the `JOBS` table, `build_parser`, `main` |
| `src/hfp_ag.py` | 1 - 189 (whole file) | the HFP **Audio Gateway** protocol engine (the "phone side" of a hands-free call), pure Python, no hardware access |
| `src/acl_meter.py` | 1 - 199 | not part of my assignment, but `job_a2dp` depends on it so its "exact throughput meter" is explained in section 4.3 |

Part 1 of `bluforge_classic.py` (lines 1 - 1306: D-Bus wrapper, pairing agent, PipeWire wrapper, streamers, one-shot actions, KeyWatcher, Recorder) is documented in another guide file. Section 3 below is only a *recap* of the part-1 helpers that the jobs call, so you can follow the jobs without flipping files.

How the notation works: `bluforge_classic.py:1500` means "line 1500 of src/bluforge_classic.py". Statements tagged **[verified locally]** are things I checked by running harmless local code (a fake socket, a pipe; nothing touched Bluetooth). Everything else is read from the code. Where the code is unclear I say so.

---

## 1. Overview: what a "job" is

BluForge is: **web GUI -> `server.py` -> helper programs -> Bluetooth radios**. `bluforge_classic.py` is the helper for **Bluetooth Classic** (also called BR/EDR, the "old" Bluetooth used for headphones, speakers and car kits, as opposed to BLE/Low Energy).

In this tool the roles are fixed (see the file header, `bluforge_classic.py:13-16`):

* **This computer ("host")** pretends to be a *phone*: it is the **A2DP source** (it sends music), the **AVRCP target** (it is the thing being remote-controlled), and the **HFP Audio Gateway** (it is the phone that has the call).
* **The device under test (DUT)** is the headset / earbuds / speaker.

`bluforge_classic.py` offers two kinds of commands:

| Kind | Examples | Behaviour |
|---|---|---|
| **Action** (part 1) | `scan`, `pair`, `connect`, `info` | runs once, prints one JSON result line, exits. Table `ACTIONS` at `bluforge_classic.py:1162`. |
| **Job** (**this guide**) | `a2dp`, `avrcp-watch`, `cycle`, `hfp` | runs for seconds or minutes, prints a stream of progress/metric lines, evaluates **pass/fail thresholds** at the end, saves result files. Table `JOBS` at `bluforge_classic.py:1850`. |

```
 browser GUI
     | HTTP  (/api/start ...)
     v
 server.py  build_classic_argv()  (server.py:472)   spawns one subprocess per job
     |  python3 -u bluforge_classic.py --hci N [--audio-user U] <a2dp|avrcp-watch|cycle|hfp> ADDR ...
     v
 bluforge_classic.py  main() (1946)  ->  JOBS[cmd](a, bz)
     |-- D-Bus ---------------> bluetoothd (BlueZ) ------------------> radio   (connect, device state, volume ...)
     |-- pw-dump/pw-cli/pw-cat/pw-top -> PipeWire (as the desktop user) -> BlueZ A2DP transport -> radio
     |-- HCI *monitor* socket (acl_meter.py) -> kernel   (counts every ACL packet: exact throughput)
     |-- /dev/input/eventN (KeyWatcher) ---> headset buttons (AVRCP key presses)
     |-- RFCOMM + SCO sockets (job_hfp + hfp_ag.py) -> kernel -> radio   (phone-call control + call audio)
     v
 stdout lines --(server.py _reader, :201)--> GUI live log      and      files in logs/classic/
```

### 1.1 Output protocol (what a job prints)

(The file's own description is at `bluforge_classic.py:18-21`.)

* `[*] [+] [!] [x]` + text: human progress lines, produced by `say()` (`:74`).
* `{"m":"event",...}`, `{"m":"sample",...}`, `{"m":"summary",...}`: machine lines produced by `emit()` (`:78`) via the `Recorder` (see 3.2). The GUI watches for `m === "summary"` to draw the PASS/FAIL box (`static/classic.js:505`). Samples are flagged "nobacklog" by the server (`server.py:226-229`) so one-per-second lines do not flood the replay buffer.
* Jobs do **not** print the final `{"ok": true|false}` line that one-shot actions print. Only when something throws `ClassicError` does `main()` print `{"ok": false, "error": ...}` (`:1963-1966`).

### 1.2 Exit codes

| Code | Meaning | Where |
|---|---|---|
| 0 | job ran and **passed** | `verdict()` returns 0 (`:1305`), `main` returns it (`:1959`) |
| 2 | job ran and **failed** its thresholds | `verdict()` (`:1305`) |
| 1 | the job could not run / crashed (`die()`, any exception) | `main()` (`:1963-1971`) |
| 2 (also) | bad command-line arguments | argparse's own behaviour (so "2" is ambiguous: bad args vs failed test) |

### 1.3 Starting and stopping

The server starts a job as a subprocess (`server.py:175-199`). Stop = send **SIGINT** to its process group, wait 5 s, then SIGTERM (`server.py:239-276`). In `main()` both signals only set a global flag `STOP` (`bluforge_classic.py:67-71`, `1949-1950`). The job loops all poll `STOP`, so a Stop click ends the loop *cleanly* and the job still builds a summary, applies thresholds and saves files. (Consequence: stopping a test early can legitimately produce a FAIL if a threshold, e.g. "expected AVRCP key never seen", was not yet satisfied.)

### 1.4 Where the results go

`Recorder.save()` (`:1269-1284`) writes two files into `<LOG_DIR>/classic/` (`LOG_DIR` = `$BLUFORGE_LOGS` or `<repo>/logs`, set at `:56`):

* `<YYYYmmdd-HHMMSS>_<kind>_<ADDR without colons>.json` : `{"summary": {...}, "events": [...], "samples": [...]}`
* same name `.csv` : one row per sample, columns = the sorted union of all sample keys (only written if there was at least one sample).

`<kind>` is `a2dp`, `avrcp-watch`, `cycle` or `hfp`. Real examples exist in `logs/classic/` (I read a few while writing this guide).

---

## 2. Jargon cheat-sheet (each term is also explained where it first matters)

| Term | Plain meaning |
|---|---|
| **DUT** | Device Under Test: the earbuds/headset being tested. |
| **BlueZ / bluetoothd** | The Linux Bluetooth software. `bluetoothd` is the background service; programs talk to it over **D-Bus** (a local message bus). |
| **HCI** | Host Controller Interface: the wire protocol between the computer and the radio chip. |
| **ACL link** | "Asynchronous Connection-Less": the normal *packet data* link between two Bluetooth devices. Music for A2DP travels over it. |
| **A2DP** | Advanced Audio Distribution Profile: one-way stereo music. Source (us) -> Sink (headset). Uses a codec (SBC, AAC, ...). |
| **AVRCP** | Audio/Video Remote Control Profile: play/pause/next buttons and volume. A headset *sends* these. |
| **HFP** | Hands-Free Profile: phone-call control and call audio. Two roles: **AG** (Audio Gateway = the phone, us) and **HF** (Hands-Free unit = the headset). |
| **SDP** | Service Discovery Protocol: ask a device "which services do you offer, and on which channel?" |
| **RFCOMM** | Radio Frequency COMMunication: a fake "serial cable" over Bluetooth, with numbered channels (1-30). HFP sends its text commands over an RFCOMM channel. |
| **AT commands** | Text commands such as `AT+BRSF=671` (from the old dial-up-modem "ATtention" command set). HFP reuses them. The HF sends commands ending in a carriage return (`\r`); the AG answers with lines wrapped in `\r\n ... \r\n`, finishing with `OK` or `ERROR`. |
| **SLC (service-level connection)** | The "handshake" at the start of HFP: the two sides swap supported features and indicator lists and switch on event reporting. Until it is done, calls and audio are not meant to start. |
| **SCO** | Synchronous Connection-Oriented link: a reserved-bandwidth, fixed-rate link for *voice*. Call audio goes here (8 kHz, 16-bit mono = 16000 bytes per second as seen by the host). |
| **CVSD / mSBC** | The two HFP voice codecs. CVSD = classic narrow-band; mSBC = wide-band. This code only does CVSD. |
| **PipeWire card / profile / sink** | PipeWire is the desktop audio server. A Bluetooth device appears as a **card** (the device), which has **profiles** (modes such as `a2dp-sink` = music, headset = phone calls), and, once a profile is active, a **sink** (a "speaker" node you can play audio into). |
| **pw-cat / pw-top / pw-dump / pw-cli** | PipeWire command-line tools: play audio from stdin / show a live table of audio nodes / dump the whole audio graph as JSON / control objects. |
| **xrun** | An audio glitch: the audio server did not have data ready in time (underrun) or could not keep up (overrun). `pw-top` counts them in its **ERR** column. |
| **Transport (state)** | BlueZ's object for one live audio stream to the DUT (`org.bluez.MediaTransport1`). Its `State` is `idle` (nothing flowing), `pending` (starting) or `active` (audio flowing). The code only ever tests for `"active"`. |
| **HCI monitor channel** | A read-only kernel socket that mirrors every HCI packet of every controller (what `btmon`-style tools read). Used by `acl_meter.py`. |
| **evdev / uinput** | Linux's input-device layer. BlueZ turns the headset's AVRCP button presses into a virtual keyboard-like device `/dev/input/eventN` named "... (AVRCP)". |
| **RSSI "offset"** | For Classic links, `hcitool rssi` does not return dBm. It returns how far the signal is from the controller's "golden receive power range": 0 = inside the range, negative = weaker. The code says this itself (`bluforge_classic.py:1534`). |

---

## 3. Part-1 helpers the jobs depend on (recap only)

### 3.1 Quick table

| Helper | Line | What the jobs use it for |
|---|---|---|
| `die(msg)` / `ClassicError` | 86 / 82 | raise a user-readable error; `main()` turns it into `{"ok": false,...}` and exit 1 |
| `norm_addr` | 90 | validate + upper-case `AA:BB:...` |
| `BZ` (`get`, `set`, `call`, `call_loop`, `subscribe`, `transport`) | 104 | D-Bus wrapper around bluetoothd. `get/props/exists` swallow errors and return a default; `call` and `managed()` **raise** `ClassicError` |
| `pump(seconds)` | 221 | sleep while dispatching D-Bus signal callbacks (the *only* place callbacks run, apart from inside `call_loop`); returns early when `STOP` is set |
| `ensure_powered`, `ensure_device` | 335, 379 | power the adapter; run an inquiry (scan) until BlueZ knows the DUT |
| `do_connect(bz, addr, timeout)` | 895 | BlueZ `Connect` (async, so signals keep being served). Returns elapsed **milliseconds as a float** (`0.0` if already connected). Its docstring says it returns a tuple; it does not. |
| `wait_for(pred, timeout)` | 885 | poll a function every 0.25 s until it returns something truthy; returns `None` on timeout **or when STOP is set** |
| `transport_info(bz, addr)` | 478 | dict about the live A2DP transport (state, codec, volume ...) or `None` |
| `link_stats(hci, addr)` | 396 | runs `hcitool rssi / tpl / lq` (three subprocesses); fields are `None` if `hcitool` is missing |
| `acl_links(hci)` | 412 | parses `hcitool con`; returns a list of **dicts** (`type, addr, handle, role`) although its docstring says tuples |
| `hci_counters(hci)` | 423 | old, inexact adapter byte counters from `hciconfig` (fallback only) |
| `Audio` (`card_for`, `sink_for`, `set_profile`, `run`, `popen`) | 495 | run PipeWire tools *as the desktop user* (`runuser` when we are root); find the BlueZ card and sink of the DUT via `pw-dump` (not `pactl`) |
| `pick_a2dp_profile(card, codec)` | 584 | choose `a2dp-sink` (or the first `a2dp-sink*`; or the one containing the requested codec name) |
| `gen_pcm(kind)` / `read_wav` | 601 / 624 | build a 10-second 48 kHz stereo s16 test signal (`tone` = 440 Hz, `sweep` = 100 Hz -> 8 kHz log sweep, `noise`) / read a WAV |
| `Streamer` (thread) / `FfmpegStreamer` | 632 / 704 | feed audio into `pw-cat -p ... -` (stdin). `Streamer` loops an in-memory PCM buffer in 100 ms chunks; `FfmpegStreamer` pipes `ffmpeg -stream_loop -1` into `pw-cat`. Both have `.start() .alive() .stop() .error .bytes_sent` |
| `probe_media(path)` | 680 | `ffprobe` check of a user audio file |
| `XrunMonitor` (thread) | 747 | repeatedly runs `pw-top -b -n 2` and keeps the ERR column of one node |
| `KeyWatcher` (thread), `find_avrcp_evdev` | 1201, 1178 | find `/dev/input/eventN` named "... (AVRCP)" for this DUT + this adapter and call `on_key("PLAY" ...)` for each key press (`KEYMAP`, `:1171`) |
| `Recorder`, `stats`, `verdict` | 1246, 1287, 1296 | see 3.2 |

### 3.2 `Recorder`, `stats`, `verdict` in more detail (they sit just before part 2 but every job ends with them)

* `Recorder(kind, addr, hci)` (`:1246`): remembers `t0` (start time), a `samples` list, an `events` list and a timestamp used in the file name. `rec.event(name, **kw)` appends `{"t":<seconds since t0>, "event":name, ...}` and also prints it as an `{"m":"event"}` line. `rec.sample(**kw)` does the same for `{"m":"sample"}` lines. **`t` is measured from the Recorder's creation, i.e. it includes setup time (connect, SDP, ...), not from the start of the measurement loop.**
* `stats(vals)` (`:1287`): ignores `None`s; returns `{min, avg, max, p95, n}` or `None` if there is no data. p95 is `v[min(n-1, int(0.95*n))]` on the sorted list (a simple rank, no interpolation).
* `verdict(rec, summary, failures)` (`:1296-1305`): sets `summary["pass"]` and `summary["fail_reasons"]`, **emits** the summary line, prints `RESULT: PASS` or `RESULT: FAIL - ...`, **then** saves files, returns `0` or `2`. Because `save()` adds `summary["saved_as"]` *after* the summary has been emitted and written, `saved_as` is never visible anywhere (see "Things I noticed" #1).

---

## 4. `job_a2dp` - the A2DP streaming test (`bluforge_classic.py:1309-1556`)

**What it does, in one paragraph.** Connects to the DUT (if it is not connected), makes PipeWire switch the DUT's Bluetooth card to the A2DP music profile, plays a test signal (tone/sweep/noise or a user's audio file) to the DUT for a chosen time, and once per interval records: transport state, link quality numbers, exact ACL throughput, and xrun count. It optionally reconnects if the DUT drops. At the end it applies four thresholds and writes a PASS/FAIL result plus JSON/CSV files.

**Inputs** (`a`, parsed in `build_parser`, `:1904-1917`; `bz` is the BZ object for the chosen adapter):

| Arg | Default | Meaning |
|---|---|---|
| `addr` | - | DUT address |
| `-t / --duration` | 60 | seconds of *streaming* (0 = until Stop) |
| `--source` | `sweep` | `tone`, `sweep`, `noise` or `file:/path/to.wav` (any audio file if `ffmpeg` exists). **Not validated by argparse** |
| `--codec` | none | force a codec profile, e.g. `aac` |
| `--latency` | 100 | pw-cat latency, ms |
| `--interval` | 1.0 | seconds between samples |
| `--reconnect` | off | reconnect and resume if the DUT drops |
| `--disconnect-after` | off | disconnect the DUT when finished |
| `--connect-timeout` | 30 | seconds allowed for BlueZ `Connect` |
| `--scan` | 20 | inquiry seconds if BlueZ does not know the DUT yet |
| `--min-active-pct` / `--max-xruns` / `--max-disconnects` / `--min-rssi` | 95.0 / 5 / 0 / none | pass/fail thresholds (4.6) |

**Output:** return code 0 (pass) or 2 (fail), machine lines on stdout, JSON/CSV files.

### 4.1 Timeline: what happens, from start to finish

**Step 1 - Pre-flight checks (`:1310-1321`).**
`norm_addr`; build `Audio(a.audio_user)` and `die` if there is no PipeWire session (`Audio.ok` is false when no `/run/user/<uid>/pipewire-0` socket was found, `:503-522`); check that `pw-cat`, `pw-top`, `pw-dump`, `pw-cli` exist on PATH; `ensure_powered`; `ensure_device` (inquiry scan up to `--scan` s if BlueZ has never seen the DUT); require that the DUT is **Paired** (a2dp never pairs; use the Pair action first).

**Step 2 - Bookkeeping (`:1323-1326`).**
Create `Recorder("a2dp", ...)`; a `state` dict holding what the D-Bus callbacks learn (`connected`, `tstate`, `volume`, counters for disconnects, state changes, volume changes, and a `keys` histogram of AVRCP buttons); and an empty `reconnects` list (milliseconds per reconnect).

**Step 3 - Start the exact ACL meter (`:1327-1342`).**
`acl_meter.AclMeter(bz.hci).open()` then `.start()`. This must happen **before** streaming so "every packet is counted" (comment at `:1327`). If the kernel refuses (`OSError`, e.g. no permission) the job only prints a warning and later uses the old inexact `hciconfig` counters. Inner helper `dut_handles()` (`:1336`) answers "which HCI connection handle(s) belong to the DUT?": first from the meter's own record of Connection-Complete events, else from `hcitool con`; if neither knows, it returns `None` and the rate maths then sums **every** connection on the adapter (see 4.3).

**Step 4 - Subscribe to BlueZ events (`:1344-1363`).**
`on_sig` is registered with `bz.subscribe`. It reacts to:
* `Connected` changing on the device -> sets `state["connected"]`, counts a disconnect, records event `disconnect` / `connected`.
* Anything under `<device path>/fd...` (the transport object, e.g. `.../dev_XX/fd0`): `State` change -> `state["tstate"]`, event `transport_state`; `Volume` change -> `state["volume"]`, event `volume`.
* Transport object removed -> event `transport_removed`.

These callbacks only run while the main thread is inside `pump()` or inside a `call_loop`. So an event's recorded time `t` is when the callback ran, which can lag the real event by the time the loop spent doing blocking work (e.g. up to a few hundred ms in a sample iteration, longer during `bring_up`'s sleeps).

**Step 5 - Start the AVRCP key watcher (`:1365-1370`).**
`KeyWatcher(addr, on_key, alias, local_adapter_address)` runs in its own thread, finds the "(AVRCP)" input device of this DUT, and counts button presses into `state["keys"]` (plus event `avrcp_key`). Lets you press buttons on the earbuds *while* music streams.

**Step 6 - `bring_up()` #1: connect -> card -> profile -> sink -> transport (`:1372-1401`).**
Inner function; also reused after a reconnect. In order:
1. If not `Connected`: `do_connect(..., a.connect_timeout)`; record `connect_ms`.
2. `wait_for(au.card_for(addr), 20)`: poll up to 20 s until PipeWire has created a **card** (a Bluetooth device object) for the DUT (`pw-dump`, matching the address inside `device.name`). If not: `die("... PipeWire created no audio card ...")`.
3. `pick_a2dp_profile(card, a.codec)`: with `--codec aac`, the first profile named `a2dp-sink...aac...`, otherwise plain `a2dp-sink`, otherwise the first `a2dp-sink*`. `die` if the DUT offers none.
4. If the card's active profile is not that one: `au.set_profile` -> `pw-cli set-param <card id> Profile "{ index: N }"` (`:579-581`). Non-zero return code -> `die`.
5. `wait_for(au.sink_for(addr), 20)`: poll up to 20 s for a PipeWire **sink** node (`media.class == Audio/Sink`, address in `node.name`). `die` if none.
6. `wait_for(transport_info(bz, addr), 10)`: poll up to 10 s for BlueZ's transport object. **This one is not fatal**: if it never appears, `info["transport"]` is `None` and the codec is later reported as `None`.
7. Returns `{connect_ms?, profile, sink, ready_ms, transport}`. `ready_ms` is measured from the start of `bring_up`, so it **includes** the connect time.

Then (`:1401-1407`): print `A2DP ready: sink=... codec=... N kbit/s max` (for SBC the maximum bitrate is computed from the negotiated config by `decode_sbc`), seed `state["tstate"]`/`state["volume"]`, record event `ready`.

**Step 7 - Prepare the audio source (`:1409-1423`).**
`node_name = "bluforge-a2dp-<pid>"` is the name given to *our* PipeWire stream, so that `pw-top` can later be asked about exactly that node.
* `--source file:...` -> `probe_media` (extension allow-list + `ffprobe`), print codec/duration; if `ffmpeg` exists, decoding will be done by `FfmpegStreamer` (any format, loops forever, nothing held in RAM); otherwise `read_wav` loads the whole 16-bit WAV into memory.
* otherwise `gen_pcm(a.source)`: builds 10 s of 48 kHz stereo s16 at -12 dBFS (`amp = 0.25 * 32767`) in a **pure-Python loop** (480 000 samples, so expect it to take noticeable CPU time) and `die`s on an unknown name. This happens **after** the connect and profile switch (see "Things I noticed" #11).

**Step 8 - Start streaming (`:1425-1433`).**
`make_streamer(sink)` builds the right streamer; `streamer.start()`; `XrunMonitor(...).start()`. The streamer launches roughly:

```
pw-cat -p --target <sink name> --rate 48000 --channels 2 --format s16 --latency 100ms \
       -P "{ node.name=bluforge-a2dp-<pid> media.name=bluforge-a2dp-<pid> }" -
```

(`bluforge_classic.py:638-640`), i.e. "play whatever arrives on stdin into that sink". There is no timing code: the writer thread pushes 19200-byte (100 ms) chunks and the pipe **blocks** whenever pw-cat is not ready for more, so audio runs at real-time speed (docstring `:633-634`). Run as root, pw-cat is started via `runuser -u <desktop user> -- env XDG_RUNTIME_DIR=... pw-cat ...` (`Audio.wrap`, `:533-539`).

**Step 9 - Take baselines (`:1435-1442`).**
`prev_c = hci_counters(...)` (fallback counters); `prev_s = first_s = meter.snapshot_all()` (see 4.3); `t_run0` = now (the clock for `--duration`); `active_s`, `total_s` (seconds the transport was `active` / seconds measured), `stream_restarts`, `failures = []`.

**Step 10 - The sampling loop (`:1443-1505`).** One iteration per interval:

1. **Stop conditions** (`:1444-1446`): `STOP` set, or `a.duration` elapsed since `t_run0`. (Checked *before* the sleep, so the last interval can overshoot.)
2. `pump(a.interval)` (`:1448`): sleeps `a.interval` seconds in 50 ms steps while processing D-Bus signals. Then `dt` = real seconds since the last iteration.
3. **Dead-stream check** (`:1451-1454`): if `pw-cat` has exited (or the writer thread recorded an error) **and** BlueZ still says connected -> record event `stream_stopped`, add the failure `audio stream stopped unexpectedly`, leave the loop. (If the DUT is *not* connected, the next branch handles it instead.)
4. **Link lost** (`:1455-1479`): see 4.4.
5. **Throughput** (`:1480-1491`): with the meter, `rates_for(prev_s, cur_s, dut_handles())` gives `tx_kbps`, `done_kbps`, `rx_kbps` over the exact elapsed time. Without the meter, only `tx_kbps` from `hciconfig` byte counters (whole adapter, inexact). See 4.3.
6. **Transport state** (`:1492-1494`): `transport_info` -> `tstate` (`"none"` if no transport object).
7. **Link stats** (`:1495`): `link_stats` runs three `hcitool` calls (RSSI offset, TX power, link quality).
8. **Accounting** (`:1496-1498`): `total_s += dt`; `active_s += dt` if `tstate == "active"`.
9. **Record** (`:1500-1501`): `rec.sample(state, rssi, tx_power, lq, tx_kbps, done_kbps, rx_kbps, xruns, volume)`. `xruns` is `xr.err`, the latest ERR value `XrunMonitor` read from `pw-top`. `volume` is the last value seen by the D-Bus callback.
10. **Progress line** (`:1502-1505`): printed when `int(rec.rel()) % 10 == 0`. That is a test on the rounded-down elapsed seconds, not a timer, so lines appear irregularly (see "Things I noticed" #16).

**Step 11 - Cleanup, always (`finally`, `:1506-1518`).**
Take `end_s = meter.snapshot_all()` and `hs_end = dut_handles()` *first*, then close the meter, stop the KeyWatcher, the streamer and the XrunMonitor; if `--disconnect-after`, call BlueZ `Disconnect` (15 s timeout, errors ignored).

**Step 12 - Build the summary (`:1520-1545`).** See 4.5 for the fields. `acl_total` is the exact whole-run total, computed from `first_s` (start of streaming) to `end_s` over the DUT's handle(s).

**Step 13 - Apply thresholds (`:1546-1555`).** See 4.6.

**Step 14 - `verdict(...)` (`:1556`)**: emit the summary line, print `RESULT: PASS/FAIL`, save the JSON and CSV, return 0/2.

### 4.2 Why PipeWire "card -> profile -> sink" is done by hand

BlueZ only creates the Bluetooth link. The *audio* side is handled by WirePlumber (PipeWire's session manager), which owns the A2DP endpoints. For audio to flow, the card must be in an A2DP profile and the matching sink must exist. The job therefore (a) waits for the card, (b) sets the profile with `pw-cli` if needed, (c) waits for the sink, and only then starts `pw-cat`. The code uses native `pw-dump`/`pw-cli` rather than `pactl` on purpose: on at least one test machine the "pulse" socket is real PulseAudio and cannot see PipeWire's Bluetooth cards (comment at `:556-558`).

### 4.3 The exact ACL throughput meter (`acl_meter.py`)

**Why it exists.** The older way (`hciconfig` byte counters) is deprecated and inexact. The meter instead *listens to every ACL packet* the host sends or receives, so the numbers are byte-exact at the "what the host handed to / got from the controller" level (docstring `acl_meter.py:1-16`).

**What is an ACL packet here?** Music for A2DP travels over an ACL link (AVDTP inside L2CAP inside ACL packets). Counting ACL bytes = counting the audio bitrate on the wire as the host sees it, including protocol overhead. It is **not** the raw radio bit rate: "over-the-air retransmissions and baseband overhead are not visible to the host" (`acl_meter.py:14-16`).

**How it gets the data.**
1. `AclMeter(hci).open()` (`acl_meter.py:45-54`): creates a raw Bluetooth HCI socket and binds it to the **monitor channel** for *all* controllers (`HCI_DEV_NONE = 0xFFFF`, `HCI_CHANNEL_MONITOR = 2`; the special bind call lives in `hci_raw._hci_bind`, because Python's own `bind()` cannot select a channel). It sets a 0.5 s read timeout.
2. `run()` (`acl_meter.py:120-134`), in a daemon thread: `recv(65536)`; each frame has a 6-byte header `<opcode, controller index, length>`; frames from other controllers are skipped (`index != self.hci`); the payload goes to `feed()`.
3. `feed()` (`acl_meter.py:72-118`) keeps counters **per HCI connection handle** (the small number the controller uses to label each link, masked with `0x0FFF`):

| Monitor opcode | Meaning | Counter updated |
|---|---|---|
| 4 `MON_ACL_TX` | host -> controller ACL packet | `tx_b += data length`, `tx_p += 1`, and the length is pushed on a per-handle FIFO |
| 5 `MON_ACL_RX` | controller -> host ACL packet | `rx_b`, `rx_p` |
| 6 / 7 `MON_SCO_TX/RX` | voice packets | `sco_tx_b` / `sco_rx_b` (not used by the a2dp job) |
| 3 `MON_EVENT_PKT`, event `0x13` Number Of Completed Packets | the controller says "I finished N packets on handle H" | for each completed packet, pop one length off that handle's FIFO and add it to `done_b`; if the FIFO is empty, `done_unmatched += 1` (packet was sent before the meter started) |
| event `0x03` Connection Complete / LE Meta `0x01, 0x0A, 0x29` | a link came up | remember `handle -> BD_ADDR` in `addr_of` |
| event `0x05` Disconnection Complete | a link went away | forget the FIFO and the address mapping (the cumulative counters are kept) |

So there are three numbers: **tx** (what the host pushed), **done** (what the controller reports as finished = sent and acknowledged, or flushed), and **rx**. A persistent gap between tx and done means the controller is queueing data (a sign of trouble on the air).

4. **Reading rates.** `snapshot_all()` (`acl_meter.py:153-156`) returns a copy of all per-handle counters plus a **monotonic** timestamp. `rates_for(prev, cur, handles)` (`acl_meter.py:172-188`) takes the difference of two snapshots (summed over `handles`, or over every handle if `None`) and divides by the *measured* time between the two snapshots:

```python
dt = cur["t"] - prev["t"]
...
"tx_kbps": round(d["tx_b"] * 8.0 / dt / 1000.0, 2),
```

Because the counters are cumulative, a reconnect that brings a new handle number is harmless (acl_meter.py:173-174 says so too). There is no polling error: the rate over 1 s is exactly "bytes counted in that second".

**Which handle belongs to the DUT?** `handles_for(addr)` (`acl_meter.py:137`) only knows handles whose Connection-Complete event was seen **after** the meter was opened. If the DUT was already connected when the job started, the map is empty, and `dut_handles()` (`bluforge_classic.py:1336-1342`) falls back to `hcitool con`. If `hcitool` is also missing, it returns `None` and the throughput includes every link on the adapter. The summary field `throughput_src` (`:1533`) tells you whether the exact meter or the fallback produced the numbers.

**Not-exact fallback.** If the meter could not open, `tx_kbps` is computed from `hci_counters()` (adapter-wide TX bytes from `hciconfig`), `done_kbps` and `rx_kbps` stay `None` (`:1487-1491`), and `throughput_src` says "hciconfig counters (not exact)".

**Nobody checks the meter's health.** If the meter thread dies (it sets `self.error`, `acl_meter.py:126-128`), the counters just stop growing and the rates read 0; nothing in `job_a2dp` looks at `meter.error`.

### 4.3a Delay and jitter on top of the meter (added in 1.3.0; line numbers above predate it)
The same monitor frames carry a kernel timestamp (`SO_TIMESTAMPNS`). `AclMeter` now also puts ACL fragments back into L2CAP frames,
times each transmitted frame from its first fragment to the completion event of its last fragment, and keeps the result per
connection handle and L2CAP channel in a bounded histogram (`delay_stats.py`). `job_a2dp` reads the busiest channel (the media channel)
once per sample (`interval_report`) and once at the end (`delay_report`), writes `link_delay` into the summary and `delay_*`/`jitter_ms`
columns into the CSV, and checks the optional `--max-delay-ms` (p99) and `--max-jitter-ms` (mean). With an internal receiver a second
meter watches the receiver radio and `LinkPair` matches each RTP packet at both ends for true one-way delay and loss. What the figures
mean and how they were validated: `docs/A2DP_DELAY.md`. `do_connect(..., prefer_bredr=True)` (A2DP and cycling tests) names the A2DP sink
profile first so a dual-mode device is not tried over the LE bearer.

### 4.4 Reconnect handling (`:1455-1479`)

Condition: BlueZ reported `Connected = False` (so `state["connected"]` is false after the `pump`).

* Without `--reconnect`: add failure `link lost (DUT disconnected) and reconnect is off` and leave the loop.
* With `--reconnect`:
  1. `streamer.stop()` and `xr.stop()`.
  2. `bring_up()` again (the same 6 sub-steps as Step 6: reconnect, wait for card, profile, sink, transport). A `ClassicError` there -> failure `reconnect failed: ...`, leave the loop.
  3. Record the elapsed time in `reconnects` and event `reconnected`.
  4. Create **new** `Streamer` and **new** `XrunMonitor` (same node name), `stream_restarts += 1`.
  5. Reset `prev_t`, `prev_c`, `prev_s`, then `continue` (this iteration produces no sample).

Consequences (all from reading the code):
* The reconnect time is **not** added to `total_s`, so `active_pct` does not count the outage. The outage is penalised by the *disconnects* threshold instead (default 0, so with default thresholds any drop fails the run even when `--reconnect` recovers it).
* The new `XrunMonitor` starts at 0, so xruns counted before the drop are **lost** from the final `summary["xruns"]` (`:1473`, `:1529`).
* `acl_total` only counts the final connection handle(s) (`hs_end`), so traffic on a previous handle is excluded (`:1540-1541`).
* If the DUT drops and comes back by itself *within one interval*, the loop never sees `connected == False`, so no reconnect branch runs; the disconnect is only counted. Whether `pw-cat` survives that is not something the code checks (it only checks "process alive").
* If the user presses Stop during a reconnect, `wait_for` returns `None` and `bring_up` `die`s, so the stop is reported as `reconnect failed`.

### 4.5 Summary fields (`:1523-1545`) and sample columns

Summary: `test, addr, hci, duration_s` (wall time from Recorder creation, includes setup), `source, media` (ffprobe info or `None`), `codec, codec_detail` (SBC details or `config_hex`; **taken from the first bring-up**, not refreshed after a reconnect), `profile, setup_ms, active_pct, transport_state_changes, disconnects, reconnect_ms[], stream_restarts, xruns, xrun_seen` (did `pw-top` ever show our node? `False` means "0 xruns" is meaningless), `rssi, link_quality, tx_power, tx_kbps, done_kbps, rx_kbps` (each `{min,avg,max,p95,n}` or `None`), `throughput_src, rssi_unit, avrcp_keys, avrcp_volume_changes, avrcp_key_device, thresholds{...}`, optional `acl_total{seconds, tx_bytes, done_bytes, rx_bytes, avg_tx_kbps, avg_done_kbps, avg_rx_kbps}`, then `pass`, `fail_reasons`.

Sample columns (CSV header is alphabetical): `done_kbps, lq, rssi, rx_kbps, state, t, tx_kbps, tx_power, volume, xruns`.

A real saved run (`logs/classic/20261024-200015_a2dp_5C3589B45593.json`, an MP3 streamed to test earbuds): codec AAC, 180 s, active 100 %, 3 xruns (limit 5), ~137 kbit/s average TX = controller-completed, `throughput_src: hci-monitor (exact ACL bytes)`, PASS.

### 4.6 Pass/fail rules (`:1546-1555`, plus failures added inside the loop)

| Check | Default limit | Fails when | Notes |
|---|---|---|---|
| Active time | `--min-active-pct 95.0` | `active_pct < limit` (only evaluated if `total_s > 0`) | share of measured time with transport `State == "active"` |
| Xruns | `--max-xruns 5` | `xr.err > limit` | final `XrunMonitor` value |
| Disconnects | `--max-disconnects 0` | `state["disconnects"] > limit` | counted by the D-Bus callback |
| RSSI | `--min-rssi` (off) | `summary["rssi"]["min"] < limit` | **silently skipped** if no RSSI data (e.g. no `hcitool`) |
| Samples | - | `ss` empty -> `no samples collected` | |
| Inside loop | - | stream process died while connected; link lost with no `--reconnect`; reconnect failed | each breaks the loop |

### 4.7 Files written

`logs/classic/<stamp>_a2dp_<ADDR>.json` and `.csv` (see 1.4). Events present in `events[]` for this job: `ready, connected, disconnect, transport_state, volume, transport_removed, avrcp_key, stream_stopped, reconnected`.

### 4.8 Gotchas specific to `job_a2dp`

* The progress line labels the RSSI as `rssi_offset` because it is a Classic RSSI offset, not dBm.
* `a.duration` counts only the streaming loop; `duration_s` and every `t` include setup time, so a "60 s" run shows ~61-70 s.
* The pw-cat **stderr is never read** (`Streamer` / `FfmpegStreamer` open it as a pipe, `:641`, `:717`), so when pw-cat dies you only get "pw-cat exited", not its reason.
* On the final `acl_total`: `hs_end` is computed once, after the loop; if the DUT had already dropped, `dut_handles()` may return `None`, in which case the total covers every link on the adapter.
* Everything about `Streamer.stop()` blocking is in "Things I noticed" #6.

---

## 5. `job_avrcp_watch` - observe the DUT's buttons (`bluforge_classic.py:1560-1624`)

**What it does.** Waits (default 120 s) while *you* press buttons on the earbuds, and records which AVRCP commands arrive: **passthrough keys** (play/pause/next/previous...) and **absolute volume** changes. Optionally you list the buttons you expect and it ends early as soon as all were seen. It transmits nothing to the DUT and does not stream audio.

**Inputs:** `addr`; `-t/--duration` (default 120; 0 = until Stop); `--expect` e.g. `PLAYPAUSE,NEXT,PREVIOUS,VOLUMEUP` (comma list, default empty). The GUI passes `-t 5..3600` and only letters/commas (`server.py:535-541`).

**Output:** pass/fail result, files `<stamp>_avrcp-watch_<ADDR>.json/.csv`. (Real examples: `logs/classic/*avrcp-watch*`; the one I read had `keys: {}` and FAILED with `never saw: PLAYPAUSE, NEXT, PREVIOUS`, `key_device: /dev/input/event11`.)

**Why two mechanisms?** An AVRCP controller (headset) can tell the source "volume is now X" (absolute volume, which BlueZ shows as the `Volume` property of the transport) *or* send a key press (passthrough, which BlueZ turns into a Linux input event). Different earbuds do different things, so both are watched.

### 5.1 Step by step

1. **Connected check** (`:1561-1564`): `die` unless the DUT exists and is `Connected`. (No scanning or connecting is done here.)
2. `expect = [...]` upper-cased list (`:1565`); `Recorder("avrcp-watch")`; `keys = {}` (histogram); `vols = []` (every raw volume value seen); `last_vol` seeded from the current transport volume if a transport exists (`:1568-1569`; kept in a one-element list so the nested function can modify it).
3. `on_key(k)` (`:1574`): called by the KeyWatcher thread; counts the key, event `avrcp_key`, prints `DUT sent AVRCP key: K`.
4. `on_sig(...)` (`:1579-1594`) via `bz.subscribe`:
   * **Volume property changed** on any path under the device: append to `vols`; infer a direction by comparing with the previous level (**absolute volume carries a level, not "up/down"**), and count that as a synthetic `VOLUMEUP`/`VOLUMEDOWN` key (`:1584`):

   ```python
   d = None if last_vol[0] is None else ("VOLUMEUP" if v > last_vol[0] else "VOLUMEDOWN" if v < last_vol[0] else None)
   ```
   * A property change on a path ending in `player0` -> event `player` (all changed properties, as text). Only appears if the DUT exposes a media player.
   * `Connected` becoming `False` -> event `disconnect` and a `[x] DUT disconnected` line. (It does **not** stop the loop; see "Things I noticed" #15.)
5. Start `KeyWatcher` (`:1596-1597`), same arguments as in `job_a2dp`.
6. `seen(k)` (`:1599-1603`): special rule: expecting `PLAYPAUSE` is satisfied by `PLAYPAUSE`, `PLAY` **or** `PAUSE` (many earbuds send separate PLAY and PAUSE instead of one toggle). The reverse is not true: expecting `PLAY` needs an actual `PLAY`.
7. **Loop** (`:1606-1614`): until `STOP`, or `a.duration` elapsed, or (if `expect` is non-empty) every expected key was seen. Each pass is `pump(0.5)`. If after about 5 s no "(AVRCP)" input device was found (`kw.dev is None`), print a warning (the loop can print it twice because the test is `int(rec.rel()) == 5` on 0.5 s steps).
8. **Cleanup** (`:1615-1616`): stop the KeyWatcher.
9. **Summary** (`:1617-1621`): `missing = [k for k in expect if not seen(k)]`; summary fields `test, addr, hci, duration_s, keys, volume_changes (raw list), expected, missing, key_device`.
10. **Pass/fail**: FAIL `never saw: ...` if anything expected is missing; FAIL `no AVRCP activity observed` if nothing was expected **and** no key and no volume change were seen. Otherwise PASS. `verdict()` writes files.

### 5.2 Gotchas

* Valid names are exactly the values of `KEYMAP` (`:1171`): `PLAY, PAUSE, PLAYPAUSE, STOP, NEXT, PREVIOUS, FASTFORWARD, REWIND, VOLUMEUP, VOLUMEDOWN, MUTE`. A typo in `--expect` can never be seen, so the run waits the whole duration and fails.
* A volume change can be counted twice if a DUT sends both an absolute-volume update and a `VOLUMEUP` key (not verified on hardware; the Classic memory note says the button watch was not hardware-tested at the time it was written).
* A Stop click with expected keys still missing yields FAIL.

---

## 6. `job_cycle` - connect/disconnect reliability loop (`bluforge_classic.py:1628-1678`)

**What it does.** Repeats "disconnect -> wait -> connect -> measure how long until the A2DP transport (and audio sink) appear" N times, to measure how reliably and how fast the DUT reconnects. No audio is streamed.

**Inputs:** `-n/--count` (10), `--gap` seconds (3.0), `--connect-timeout` (30), `--profile-timeout` (15), `--min-success` percent (100.0), `--scan` (20).

### 6.1 Step by step

1. Setup (`:1629-1636`): `ensure_powered`, `ensure_device`, require Paired, `Audio(...)` (note: **no `die` if PipeWire is missing**, it just skips sink timing), `Recorder("cycle")`, `ok_n = 0`.
2. For `i` in `1..count` (`:1639`), unless `STOP`:
   1. If currently connected: BlueZ `Disconnect` (20 s timeout), `row["disconnect_ms"]`, then `pump(a.gap)` so the DUT settles.
   2. `t0 = now`; `do_connect(...)`; `row["connect_ms"]`.
   3. `tp = wait_for(transport_info, a.profile_timeout)`; `row["transport_ms"]` = time **since `t0`** (so it *includes* connect time).
   4. If PipeWire is available and the transport appeared: `sk = wait_for(au.sink_for, a.profile_timeout)`; `row["sink_ms"]` (again since `t0`), or `None` on timeout.
   5. `row["rssi"] = link_stats(...)["rssi"]`.
   6. `row["ok"] = (tp is not None)`: **success means "the A2DP transport appeared"**. A missing sink does not make the row fail. If no transport: `row["error"] = "connected but no A2DP transport within Ns"`.
   7. A `ClassicError` anywhere above -> `ok=False`, `error=str(e)` (the row may then lack some timings).
   8. Count, `rec.sample(**row)` (the row becomes a sample line), print `cycle i/N: {...}` (`[+]` ok, `[x]` failed), then `pump(a.gap)` again.
3. Summary (`:1668-1674`): `cycles, ok, success_pct`, `connect_ms` stats (successful rows only), `transport_ms` stats, `sink_ms` stats, first 10 error strings.
4. **Pass/fail**: FAIL if `success_pct < --min-success` (default 100, so one bad cycle fails the run). `verdict()` writes files.

Note `summary["ok"]` here is an *integer count* of successful cycles; in other places "ok" is a boolean. The GUI relies on it (`static/classic.js:304`).

### 6.2 Gotchas

* The DUT is left **connected** at the end (the final `pump(gap)` follows a successful connect).
* Many headsets reconnect on their own during the gap. Then `do_connect` sees `Connected` already true and returns `0.0` instantly, so `connect_ms` can be ~0 for reasons that are not about speed.
* Pressing Stop during a wait makes `wait_for` return `None`, so the interrupted cycle is recorded as a failed cycle ("no A2DP transport"), which also lowers `success_pct`.
* Real saved result I read: 5/5 cycles, connect 701-2903 ms, transport and sink a few ms later (`logs/classic/20261022-112827_cycle_...json`).

---

## 7. `sdp_hfp_channel` - find the HFP RFCOMM channel (`bluforge_classic.py:1682-1695`)

**What it does.** Asks the DUT over **SDP** "what is the RFCOMM channel number of your *Hands-Free* service?" so `job_hfp` knows where to connect. (RFCOMM channels are not fixed; each device chooses its own and advertises them in SDP.)

**Steps.**
1. If `sdptool` (a deprecated BlueZ tool) is missing: `die` and tell the user to pass `--channel N`.
2. Run `sdptool -i hciN records <addr>` (40 s timeout). It uses `records`, not `browse`, because many earbuds publish no browse group and `browse` then prints nothing (comment `:1686-1687`). To make the SDP query the adapter must be able to page the DUT, so the DUT must be in range and not busy.
3. Split the text into blocks at each `Service Name:` / `Service RecHandle:` line (`:1689`).
4. In each block, look for the Hands-Free class id `(0x111e)` (case-insensitive). Note this is the **HF** UUID; the AG UUID `0x111f` is a different string and does not match.
5. In that block, regex `RFCOMM[^\n]*\n\s*Channel:\s*(\d+)` extracts the channel number; return it as `int`.
6. If nothing matched: `die` with "advertises no Hands-Free (HFP HF) service over SDP ...", quoting the last line of sdptool's output.

**Gotchas.** A device that offers only the older *Headset* profile (HSP, `0x1108`) is rejected as "no HFP". Output parsing depends on `sdptool`'s exact text format (not verified against other BlueZ versions). The real logs show channel 4 on the test earbuds.

---

## 8. `tone8k` - the test tone for call audio (`bluforge_classic.py:1698-1700`)

Returns `seconds` (default 2.0) of a 440 Hz sine wave at **8000 samples per second**, 16-bit, mono (little-endian, amplitude 0.3 of full scale), as `bytes` (`16000 samples = 32000 bytes`). Built in a pure-Python comprehension. 8 kHz/16-bit mono is exactly what an SCO voice link carries, so each byte of it can go straight to the SCO socket.

Gotcha: `job_hfp` loops it with `pos = (pos + mtu) % (len(pcm) - mtu)` (`:1786`). Because the modulus is not a multiple of the frame size, the loop wraps a few samples off a clean boundary, so there may be a faint click each 2 seconds. Harmless for a link test; mentioned only because it is not a seamless loop.

---

## 9. `job_hfp` - the HFP Audio Gateway test (`bluforge_classic.py:1703-1847`)

### 9.1 The concepts you need first

* **Roles.** In HFP there is a **Hands-Free unit (HF)** (headset, car kit) and an **Audio Gateway (AG)** (the phone). Here the DUT is the HF and the host is the AG (docstring `:1704-1706`; `hfp_ag.py:3-5`). BluForge therefore has to *behave like a phone*: answer the headset's setup commands, announce an incoming call, accept the answer, and carry call audio.
* **Two separate links are used.**
  1. **RFCOMM** (control): a serial-like text channel. All AT commands and replies go over it. The AG opens it to the HF's RFCOMM channel (found via SDP, section 7).
  2. **SCO** (audio): a fixed-rate voice link. Opened separately, after the call is active.
* **AT commands** are simple text lines. Example exchange (generated by my local fake-socket run of `hfp_ag.py` **[verified locally]**, `\r\n` shown literally):

```
HF -> AG   AT+BRSF=671\r
AG -> HF   \r\n+BRSF: 288\r\n\r\nOK\r\n
```

* **SLC (service-level connection).** The HF sends `AT+BRSF` (my features), the AG answers with its features; `AT+CIND=?` (what status indicators do you have?), `AT+CIND?` (their current values), `AT+CMER=3,0,0,1` (please push indicator changes to me). After that the two sides can run calls.
* **Indicators** are small status numbers: `service, call, callsetup, callheld, signal, roam, battchg`, announced to the HF with lines like `+CIEV: 3,1` ("indicator #3, callsetup, is now 1 = incoming call").
* **Simulated call.** There is no real phone network. The AG just announces `callsetup=1` and sends `RING` + `+CLIP: "+1555..."` (caller id), then waits for the HF to answer with `ATA`.
* **CVSD only.** No wide-band (mSBC) negotiation is offered; `hf_features` in real logs shows the earbuds support codec negotiation, but the AG does not advertise it (`hfp_ag.py:38`, default features 288), so call audio is narrow-band.

### 9.2 Inputs

`addr`; `-t/--duration` (20: seconds of SCO audio; 0 = until Stop/hang-up); `--scenario slc|sco|call` (default `call`: `slc` = handshake only, `sco` = handshake + audio link, `call` = handshake + simulated incoming call + audio); `--channel` (0 = look up via SDP); `--number` (caller id, default `+15551234567`); `--ring-timeout` (15; the GUI sends its own value); `--slc-timeout` (10); `--connect-timeout` (20; used for **both** RFCOMM and SCO connects); `--interval` (1.0: sampling period); `--scan` (20); `--verbose` (log every AT line as `AG> ...` / `HF> ...`). (`build_parser`, `:1923-1934`.)

### 9.3 Timeline

1. **Setup** (`:1707-1717`): lazy `import hfp_ag` (so a missing module never breaks the other jobs); `ensure_powered`, `ensure_device`, require Paired; `local = adapter address` (needed to bind the sockets to *this* radio); `Recorder("hfp")`; `ch = a.channel or sdp_hfp_channel(...)` (SDP lookup can take many seconds: in one real log `rfcomm_connected` happened at t = 17.6 s).
2. **RFCOMM connect** (`:1718-1729`): kernel RFCOMM socket `AF_BLUETOOTH / SOCK_STREAM / BTPROTO_RFCOMM`, `bind((local, 0))` (channel 0 = any), `settimeout`, `connect((addr, ch))`. Failure -> `die`. Record `rfcomm_ms` and event `rfcomm_connected`. Note the job talks to the kernel's RFCOMM directly; it does not go through BlueZ's profile API or PipeWire.
3. **Create the AG engine** (`:1730`): `AudioGateway(rf, log=...)`.
4. **SLC** (`:1736-1741`): `ag.establish_slc(a.slc_timeout)` pumps the engine until `ag.slc` is true or time runs out. Event `slc` with the HF's feature bits and codec list. If it never completes: failure `service-level connection did not complete within Ns`, and the job skips everything else.
5. **Scenario `slc`**: done after step 4.
6. **Simulated call, scenario `call`** (`:1743-1756`): `ag.ring(number)`; wait up to `--ring-timeout` for `ag.answered` (DUT sent `ATA`) or `ag.hung_up` (DUT sent `AT+CHUP`, i.e. rejected) or STOP, pumping every 0.2 s. If the DUT answered: event `answered`. **Otherwise** print "no answer from the DUT - activating the call from the AG side" and call `ag.activate_call()` (just sets `call=1` and `callsetup=0`). `call_active = True` in both cases. Not answering is *not* a failure: the saved real logs for the test earbuds show `dut_answered: false` and PASS.
7. **SCO connect** (`:1757-1773`): socket `SOCK_SEQPACKET / BTPROTO_SCO`, `bind(local.encode())`, `connect(addr.encode())` (SCO addresses must be `bytes`). On success: read the SCO MTU (maximum packet size) with `getsockopt(17, 1, 2)` (level `SOL_SCO` = 17, option `SCO_OPTIONS` = 1, a 2-byte `mtu` field), default 48 if unreadable; record `sco_ms` and event `sco_up`. Real logs show MTU 96. On failure: failure `SCO audio link failed: ...`.
8. **SCO audio loop** (`:1774-1818`), non-blocking sockets, runs until duration, STOP, hang-up or error. Each pass (about every 1.5 ms):
   * **Send** one MTU-sized frame of the tone when `now >= nxt`; `nxt += period` where `period = mtu / 16000.0` s (16000 bytes/s is 8 kHz x 2 bytes). The schedule is absolute, so it does not drift. `BlockingIOError` (socket buffer full) silently **drops** that frame but time still advances.
   * **Receive** whatever the DUT's microphone sent (`recv(512)`), counting bytes into `rx_bytes`.
   * `ag.pump(0.001)` so the AT channel is still serviced (volume commands `AT+VGS/VGM`, `AT+CHUP`, ...). `HfpError` -> failure `RFCOMM control link lost` and event `disconnect`.
   * `ag.hung_up` -> event `dut_hangup`, leave the loop.
   * Every `--interval`: `rec.sample(sco_tx_Bps, sco_rx_Bps, rssi, lq, tx_power)` (bytes per second since the last sample; RSSI/LQ via `link_stats`).
   * `sleep(0.0005)`.
9. **End the call** (`:1819-1820`): `ag.end_call()` -> `+CIEV: 2,0` and `+CIEV: 3,0`.
10. **Close** SCO and RFCOMM sockets (`:1823-1829`).
11. **Summary and thresholds** (`:1830-1847`).

### 9.4 Summary fields and pass/fail

Summary: `test, addr, hci, duration_s, scenario, rfcomm_channel, rfcomm_ms, slc_ok, hf_features` (the HF's `AT+BRSF` bitmask; e.g. 671 = EC/NR, three-way calling, caller-id, voice recognition, remote volume, codec negotiation, eSCO S4), `hf_codecs` (from `AT+BAC`; default `[1]` = CVSD only), `codec` (**the literal string `"CVSD"`**, not read back from the link), `dut_answered, sco_ms, sco_mtu, sco_tx_bytes, sco_rx_bytes, speaker_gain` (last `AT+VGS`), `mic_gain` (last `AT+VGM`), `protocol_timeline` (last 40 `(seconds, what)` pairs from `ag.events`), `rssi, link_quality` stats, and for non-`slc` scenarios `sco_tx_pct_of_expected`.

Failures:
* SLC not completed (step 4) / SCO connect failed / SCO read or write error / RFCOMM link lost.
* For scenario `sco`/`call`, when SLC is up and SCO connected (`:1839-1846`): `expect = 16000 * (duration or measured_run_s)`; `sco_tx_pct_of_expected = 100 * tx_bytes / expect`; if a duration was set and the percentage is `< 90` -> FAIL `SCO sent only N% of the expected audio`; if `rx_bytes == 0` -> FAIL `no microphone audio received from the DUT over SCO`.
* If the DUT hangs up early the *real* reason ("dut_hangup" event) only shows in the JSON; the printed failure is the "SCO sent only ..." one.

Files: `<stamp>_hfp_<ADDR>.json/.csv`. Events: `rfcomm_connected, slc, answered, sco_up, disconnect, dut_hangup`.

Real example (`logs/classic/20261022-112802_hfp_5C3589B45593.json`): RFCOMM channel 4 in 50 ms, SLC OK, `hf_features` 671, SCO up in 33 ms with MTU 96, 240000 bytes sent (100 % of expected), 239472 bytes received, DUT did not answer (AG activated the call at t = 6.2 s), PASS. The timeline in that file also shows `unsupported:AT+CGMI?` (the DUT asked for the manufacturer name; the AG replied `ERROR`).

### 9.5 Gotchas

* The metrics call `link_stats` (three subprocesses) runs in **the same thread that feeds the audio** (`:1814`). Each sample interval therefore stalls the audio feed briefly; the catch-up frames can hit `BlockingIOError` and be dropped. The real logs show 100 % delivered at a 1 s interval, so in practice it was fine on that setup, but it is a design weakness if `hcitool` is slow.
* `sco_mtu` in the summary is `48` when SCO never came up (it is the initial value, `:1734`).
* `--duration 0` means "until Stop or hang-up", and then the `< 90 %` check is skipped (it needs a duration).
* The tone is only ever *sent*; received microphone audio is only counted, never analysed.

---

## 10. `hfp_ag.py` - the Audio Gateway protocol engine (whole file)

**Purpose (docstring `hfp_ag.py:1-16`).** "Hands-Free Profile (HFP 1.7) *Audio Gateway* side, protocol only." It is a small state machine that reads AT lines from any object with `send(bytes)`, `recv(n)` and `settimeout()`, and answers like a phone. Because it is "pure protocol", it can be tested with a `socketpair` and no hardware (that is what `src/verify_hfp_ag.py` does; I did not run it, but ran an equivalent local fake-socket check).

**What it implements:** SLC (`AT+BRSF`, `AT+BAC`, `AT+CIND=?`, `AT+CIND?`, `AT+CMER`, `AT+CHLD=?`), volume sync (`AT+VGS`, `AT+VGM`), codec connection (`AT+BCS`), caller id (`+CLIP`), call control (`ATA`, `AT+CHUP`, `ATD`, `AT+CLCC`), HSP button (`AT+CKPD`), and AG-initiated incoming-call / call-active / call-end indicators. **Not implemented** (answered with `ERROR`): three-way calling actions (`AT+CHLD=n`), voice recognition, enhanced call control.

### 10.1 Constants (`hfp_ag.py:21-30`)

* `AG_*` and `HF_*`: feature **bit masks** exchanged in `AT+BRSF`. AG bits (HFP 1.7 section 4.34.2): `AG_THREEWAY=1, AG_ECNR=2, AG_VOICE_RECOG=4, AG_INBAND_RING=8, AG_VOICE_TAG=16, AG_REJECT_CALL=32, AG_ENH_CALL_STATUS=64, AG_ENH_CALL_CTRL=128, AG_EXT_ERR=256, AG_CODEC_NEG=512`. HF bits: `HF_ECNR=1, HF_THREEWAY=2, HF_CLIP=4, HF_VOICE_RECOG=8, HF_VOL=16, HF_ENH_STATUS=32, HF_ENH_CTRL=64, HF_CODEC_NEG=128`. They are generated with `[1 << i for i in range(n)]` (`:22-26`).
* `INDICATORS` (`:28-29`): the 7 indicators in spec order, with ranges: `service 0-1, call 0-1, callsetup 0-3, callheld 0-2, signal 0-5, roam 0-1, battchg 0-5`. `IDX` maps name to its **1-based position** (used in `+CIEV: <position>,<value>`).

### 10.2 `class HfpError` (`:33-34`)

An empty exception: "the HFP link is unusable". Raised only in one place: `read_commands` when the peer closes the connection (`:79`).

### 10.3 `class AudioGateway` (`:37-189`)

**`__init__(link, features=AG_REJECT_CALL | AG_EXT_ERR, log=None)`** (`:38-55`). State:

| Field | Meaning |
|---|---|
| `link` | the socket-like object |
| `features` | what we advertise (default 32 + 256 = **288**: "can reject a call" and "extended error codes"; **no** codec negotiation, so the HF stays on CVSD) |
| `log` | callback for AT-line logging (default: do nothing) |
| `buf` | bytes received but not yet terminated by `\r` |
| `hf_features` | the HF's bitmask from `AT+BRSF` (initially 0) |
| `hf_codecs` | from `AT+BAC` (initially `[1]`) |
| `cmer_on` | has the HF switched event reporting on? |
| `slc` | is the service-level connection complete? |
| `cind` | current value of each indicator (start: service 1, call 0, callsetup 0, callheld 0, signal 5, roam 0, battchg 5) |
| `number` | caller id used for the call (initially `+10000000000`) |
| `speaker`, `mic` | last `AT+VGS` / `AT+VGM` values (volume gains 0-15 in HFP) |
| `codec` | currently selected codec number (1 CVSD, 2 mSBC) |
| `events` | the protocol timeline: list of `(seconds since start, what)` |
| `answered`, `hung_up` | the DUT sent `ATA` / `AT+CHUP`. **Sticky**: never reset to False |
| `t_start` | for the timeline |

**`_mark(what)`** (`:58-59`): appends `(elapsed seconds, what)` to `events`.

**`send(line)`** (`:61-63`): logs `AG> line` and sends `"\r\n" + line + "\r\n"` (the format HFP requires for AG responses). It calls `link.send` **once** and ignores the return value (on a stream socket, `send` may in theory write fewer bytes than requested; `sendall` would be safer). **`ok()`/`err()`** (`:65-69`) send `OK`/`ERROR`.

**`read_commands(timeout)`** (`:71-87`): sets the socket timeout, does one `recv(512)`, appends to `buf`, and splits out complete commands at each `\r` (stripping `\n` and spaces). Returns a list of command strings.
* `recv` raises anything (including the normal timeout) -> returns `[]`. **[verified locally]** This also swallows real socket errors (e.g. connection reset), which are therefore treated like "nothing arrived".
* `recv` returns `b""` (orderly close by the peer) -> `HfpError("RFCOMM link closed by the DUT")`. **[verified locally]**

**`handle(cmd)`** (`:90-146`). Upper-cases the whole command, then picks a branch by prefix. This is the heart of the file:

| HF sends | AG does | Line |
|---|---|---|
| `AT+BRSF=<n>` | stores `hf_features = n`; replies `+BRSF: 288` then `OK` | 93 |
| `AT+BAC=1,2` | stores the HF's codec list; `OK` | 97 |
| `AT+CIND=?` | replies the indicator list `+CIND: ("service",(0,1)),("call",(0,1)),("callsetup",(0-3)),...`; `OK` | 100 |
| `AT+CIND?` | replies current values `+CIND: 1,0,0,0,5,0,5`; `OK` | 104 |
| `AT+CMER=3,0,0,1` | sets `cmer_on` **only if** the first four params are exactly 3,0,0,1; always `OK`; calls the empty hook `_after_cmer()` | 107 |
| `AT+CHLD=?` | `+CHLD: (0,1)`; `OK` (the HF learns which call-hold actions exist) | 111 |
| `AT+CHLD=<n>` | `ERROR` (three-way calling not implemented) | 114 |
| `AT+VGS=<n>` / `AT+VGM=<n>` | store speaker / microphone gain; `OK`; timeline `vgs=n` / `vgm=n` | 116, 118 |
| `AT+CLIP=`, `AT+CCWA=`, `AT+NREC=`, `AT+BIA=`, `AT+CMEE=`, `AT+XAPL=`, `AT+IPHONEACCEV=` | `OK` and **ignored** (the last two are Apple extensions) | 120 |
| `AT+BCS=<n>` | stores the chosen codec; `OK` | 123 |
| `ATA` (answer) | `OK`; `answered = True`; `+CIEV call=1` then `+CIEV callsetup=0` | 125 |
| `AT+CHUP` (hang up / reject) | `OK`; `hung_up = True`; call=0, callsetup=0 | 128 |
| `ATD...` (dial) | `OK`; fakes an outgoing call: callsetup 2, 3, then call 1, callsetup 0 | 131 |
| `AT+CLCC` (list calls) | if a call is active, one line `+CLCC: 1,0,0,0,0,"<number>",145`; then `OK` | 135 |
| `AT+CKPD=<n>` (headset button, HSP) | `OK`, timeline `hsp_button`; does **not** change call state | 139 |
| anything else | `ERROR`; timeline `unsupported:<cmd>` | 141 |

After every command, the tail of `handle` (`:144-146`) checks "is the SLC complete yet?" and, the first time it is, sets `slc = True` and marks `slc_up`.

**`_slc_complete()`** (`:148-151`): returns `self.cmer_on`. "CMER enable is the final mandatory step" says the comment. In other words the engine declares the SLC complete the moment event reporting is switched on, without checking that `AT+BRSF` / `AT+CIND` were seen first. **[verified locally: a lone `AT+CMER=3,0,0,1` sets `slc` true]**. The extra condition `self.hf_features is not None` in `:144` is always true (it starts at 0, never `None`).

**`_after_cmer()`** (`:153-154`): an empty hook (`pass`).

**`pump(timeout=0.2)`** (`:156-158`): `read_commands(timeout)` then `handle()` on each. **There are no threads in this class**: it only does something when someone calls `pump()`. `job_hfp` therefore keeps calling `ag.pump()` all through the call, otherwise the DUT's `AT+VGS` / `AT+CHUP` would go unanswered.

**`set_ind(name, value)`** (`:161-165`): update `cind[name]`; if event reporting is on, send `+CIEV: <position>,<value>`; add `ciev name=value` to the timeline. If `cmer_on` is false the update is stored silently.

**`establish_slc(timeout=10.0)`** (`:167-172`): pump (0.3 s slices) until `slc` or the deadline; returns `self.slc`.

**`ring(number=None)`** (`:174-181`): optionally set the caller id; `set_ind("callsetup", 1)` (-> `+CIEV: 3,1`, "incoming call"); send `RING`; if the HF advertised `HF_CLIP`, also send `+CLIP: "<number>",145` (145 = international-number format); timeline `ring`. **RING is sent once**, not repeated.

**`activate_call()`** (`:183-185`): `call=1`, `callsetup=0`: used when the DUT does not answer.

**`end_call()`** (`:187-189`): `call=0`, `callsetup=0`.

### 10.4 A complete exchange (generated by my local run **[verified locally]**; not captured from a real DUT)

```
HF AT+BRSF=671        -> AG +BRSF: 288 / OK
HF AT+CIND=?          -> AG +CIND: ("service",(0,1)),("call",(0,1)),("callsetup",(0-3)),("callheld",(0-2)),("signal",(0-5)),("roam",(0,1)),("battchg",(0-5)) / OK
HF AT+CIND?           -> AG +CIND: 1,0,0,0,5,0,5 / OK
HF AT+CMER=3,0,0,1    -> AG OK                 (SLC complete)
-- job calls ag.ring("+15551234567") --
AG +CIEV: 3,1  RING  +CLIP: "+15551234567",145
HF ATA                -> AG OK, +CIEV: 2,1, +CIEV: 3,0   (call active; job then opens SCO)
HF AT+CLCC            -> AG +CLCC: 1,0,0,0,0,"+15551234567",145 / OK
HF AT+CHUP            -> AG OK, +CIEV: 2,0, +CIEV: 3,0
```

### 10.5 Gotchas inside `hfp_ag.py`

* **Malformed numbers crash `handle()`.** `AT+VGS=`, `AT+BRSF=abc` and `AT+BCS=x` raise `ValueError` out of `handle()` **[verified locally]**. `job_hfp` only catches `HfpError`, so such a command ends the job as an "internal error" (see "Things I noticed" #3).
* `AT+CLIP=1` is acknowledged but ignored; `+CLIP` is sent whenever the HF *advertised* caller-id capability, not when it switched it on.
* `+CLCC` always reports direction `0` (see "Things I noticed" #9).

---

## 11. The `JOBS` table (`bluforge_classic.py:1850`)

```python
JOBS = {"a2dp": job_a2dp, "avrcp-watch": job_avrcp_watch, "cycle": job_cycle, "hfp": job_hfp}
```

A plain dictionary from sub-command name to function. `main()` uses it (`:1958-1959`): if the sub-command is in `JOBS`, call it and return its exit code; otherwise look it up in `ACTIONS` (`:1162`, part 1) and print its JSON result. The server's internal names (`c_a2dp`, `c_avrcp_watch`, `c_cycle`, `c_hfp`, `server.py:48`) are translated to these CLI names in `build_classic_argv` (`server.py:518-558`).

---

## 12. `build_parser` - the command line (`bluforge_classic.py:1854-1943`)

Builds an `argparse` parser:

* **Global options:** `--hci N` (which controller; required for everything except `preflight`), `--audio-user USER` (whose PipeWire session to use; default auto-detect).
* **Sub-commands** (`dest="cmd"`, required): `preflight`, `adapter`, `scan`, `devices`, `info`, `interrogate`, `pair`, `connect`, `disconnect`, `remove`, `avrcp-player`, `avrcp-volume` (all part 1, one-shot) and the four jobs in the table below.
* `addr_parser(name, help_, **extra)` (`:1873-1876`) is a local helper that creates a sub-parser with a positional `addr`. `**extra` is accepted but unused.

Job arguments (all defaults copied from the code):

| Sub-command | Arguments (default) |
|---|---|
| `a2dp` | `-t/--duration` 60, `--source` sweep, `--codec`, `--latency` 100, `--interval` 1.0, `--reconnect`, `--disconnect-after`, `--connect-timeout` 30, `--scan` 20, `--min-active-pct` 95.0, `--max-xruns` 5, `--max-disconnects` 0, `--min-rssi` None |
| `avrcp-watch` | `-t/--duration` 120, `--expect` "" |
| `hfp` | `-t/--duration` 20, `--scenario` call, `--channel` 0, `--number` +15551234567, `--ring-timeout` 15, `--slc-timeout` 10, `--connect-timeout` 20, `--interval` 1.0, `--scan` 20, `--verbose` |
| `cycle` | `-n/--count` 10, `--gap` 3.0, `--connect-timeout` 30, `--profile-timeout` 15, `--min-success` 100.0, `--scan` 20 |

Note the server's GUI path clamps or validates most of these (`server.py:518-558`, e.g. duration 5-3600 for hfp, 0-86400 for a2dp), but the command line does not (e.g. `--source` is free text).

---

## 13. `main` - program entry (`bluforge_classic.py:1946-1971`)

1. `a = build_parser().parse_args()`; `cmd = a.cmd`. (argparse exits on bad arguments by itself, status 2.)
2. Install `SIGINT` and `SIGTERM` handlers that only set `STOP` (`:1949-1950`). Installed *after* parsing.
3. Inside a big `try`:
   * `preflight` -> print `act_preflight(a)` JSON, return 0. This is the only command that works without `--hci` and without BlueZ.
   * Every other command needs `--hci N`: else `die("--hci N is required")`.
   * `bz = BZ(a.hci)` (`:1957`): connects to the system D-Bus and checks that `bluetoothd` manages `hciN`.
   * If `cmd in JOBS`: `return JOBS[cmd](a, bz)` (0 pass / 2 fail).
   * Else run the action, print its dict as the final JSON line, return 0.
4. `except ClassicError` -> `[x] message` + `{"ok": false, "error": ...}`, return 1.
5. `except Exception` (anything else, e.g. `OSError`, `ValueError`) -> full traceback on stderr, `{"ok": false, "error": "internal error: Type: msg"}`, return 1. Comment: "never a bare traceback to the GUI".

Last lines: `if __name__ == "__main__": sys.exit(main())` (`:1974-1975`).

Important consequence (see "Things I noticed" #2): an exception that escapes a job produces *no* summary and *no* result files, even though `rec` may already hold minutes of samples; they only exist as the already-printed `{"m":"sample"}` lines.

---

## 14. Things I noticed

Ordered roughly by how much they could matter. "Verified" = reproduced with harmless local code; "reading" = deduced from code only.

1. **`saved_as` is never shown (dead GUI code).** `Recorder.save` sets `summary["saved_as"]` (`bluforge_classic.py:1282`) *after* `verdict()` already emitted the summary line (`:1299`) and after the JSON was written (`:1275`). The GUI reads `sm.saved_as` (`static/classic.js:310`) from that line, so the "saved: ..." line never appears. Verified: none of the existing `logs/classic/*.json` summaries contain `saved_as`. Cosmetic.
2. **An exception escaping a job loses everything.** Only `ClassicError` and `HfpError` are anticipated. In `job_hfp`, `ag.end_call()` (`:1820`), `ag.ring(...)` and `activate_call()` call `link.send` directly (`hfp_ag.py:63`). If the RFCOMM socket is dead, that can raise `OSError`, which is not an `HfpError`, so it goes up to `main()` (`:1967`), prints "internal error", and **no summary, no pass/fail and no files** are produced. (Reading: I did not provoke a real broken RFCOMM link.) The same happens for any D-Bus error in the middle of a job: `transport_info` -> `BZ.managed()` raises `ClassicError` (`:178-179`) and `job_a2dp`/`job_cycle` abort without saving.
3. **Bad AT parameters crash the AG engine.** `int()` on a non-numeric parameter in `AT+BRSF`, `AT+VGS`, `AT+VGM`, `AT+BCS` raises `ValueError` (`hfp_ag.py:94, 117, 119, 124`); `job_hfp` does not catch it, so it falls into #2. Verified locally. A DUT that sends an empty or vendor-specific value would abort the test.
4. **A2DP reconnect forgets earlier xruns.** A new `XrunMonitor` starts at 0 after each reconnect (`:1473`) and only the last one is reported (`:1529`).
5. **`acl_total` after a reconnect.** It is computed over the final handle(s) only (`:1540-1541`, `rates_for` with `hs_end`), so bytes sent on the pre-drop handle are left out while the time span covers the whole run.
6. **`Streamer.stop()` can block forever (verified analog).** It closes `pw-cat`'s stdin *first* (`:663`), but the writer thread may be blocked in `stdin.write` while pw-cat is not draining (for example a vanished sink). In a local test with a child that never reads, `close()` from another thread did not return within 3 s. If that happens in the reconnect path (`:1459`) or the cleanup (`:1512`), the job hangs and the server eventually reports it as "wedged" (`server.py:274`). In normal operation pw-cat drains the pipe, so `close()` returns after at most one chunk. Not reproduced with the real pw-cat.
7. **HFP metrics share the audio-feed thread (`:1814`).** `link_stats` runs three subprocesses inside the loop that paces SCO frames; whether this drops audio depends on the machine. Real logs at 1 s interval show 100 %, so it is a risk, not a demonstrated bug.
8. **`sco_mtu` reports 48 when no SCO link exists (`:1734`, `:1835`)** and `codec` is the literal `"CVSD"` (`:1834`), not a measured value.
9. **HFP protocol simplifications.** `+CLCC` always uses direction `0` (`hfp_ag.py:137`), which as I understand the 3GPP/HFP definition means "mobile originated", although the simulated call is *incoming* (direction `1` would be expected; I did not check the spec text again, so treat this as "probably"). `RING` is sent once (`hfp_ag.py:178`) whereas, as I understand the HFP spec, the AG repeats it periodically while a call is pending. `AT+CLIP=1` is ignored. How individual earbuds react to these was not tested here; the real test DUT worked.
10. **SLC "complete" is declared on `AT+CMER` alone** (`hfp_ag.py:148-151`) even if `AT+BRSF`/`AT+CIND` never arrived; and the `hf_features is not None` test (`:144`) can never be false (dead check).
11. **Late validation in `job_a2dp`.** A bad `--source` name (`gen_pcm` dies, `:1423`) or a non-WAV file without `ffmpeg` (`probe_media` dies, `:1413`) is only detected *after* the DUT was connected and its PipeWire profile switched (`:1401`). The GUI validates the source in `server.py:_source`, so only command-line users are affected.
12. **`--min-rssi` is silently ignored when `hcitool` is absent** (`:1552`): there is no RSSI data, so the `if ... summary["rssi"]` test is skipped without a note.
13. **D-Bus callbacks run late.** Event timestamps are the time the callback ran inside `pump()` or `call_loop`, not when BlueZ emitted the signal (`pump` `:221-229`).
14. **Stop is counted as failure in two places.** `wait_for` returns `None` on STOP, so a stop during `bring_up` is reported `reconnect failed` (`:1465`) and a stop during a cycle wait is recorded as a failed cycle (`:1652`).
15. **`job_avrcp_watch` quirks.** A DUT disconnect is logged but neither ends the loop nor fails the run (`:1592-1594`); misspelt `--expect` names can never be satisfied; the "no AVRCP device" warning can print twice (`:1613`); `PLAY` is not satisfied by `PLAYPAUSE` while the reverse is allowed (`:1599-1603`).
16. **Progress-line timing.** `int(rec.rel()) % 10 == 0` (`:1502`) depends on loop drift and interval: with a 1 s interval and ~1.2 s real spacing some multiples of 10 are skipped; with intervals under 1 s a line can print repeatedly; with a 7 s interval it may print only by chance.
17. **pw-cat stderr is never read** (`:641`, `:717`), so a dying pw-cat is reported only as "pw-cat exited".
18. **Docstring drift in part 1 that affects the jobs:** `do_connect` says it returns a tuple but returns a float (`:896`); `acl_links` says tuples but returns dicts (`:413`). Code handles the real return values correctly.
19. **Exit code 2 is ambiguous**: both "test failed its thresholds" (`:1305`) and "argparse rejected the arguments".
20. **Open question, not a verified bug:** `job_hfp` opens its own kernel RFCOMM socket to the DUT. The code does not check or coordinate with BlueZ/WirePlumber's own HFP handling (PipeWire can run an HFP AG role itself). On the single test DUT it worked; what happens when another HFP stack is already connected to the DUT is not handled or documented in the code.
21. **`summary["ok"]` in `job_cycle` is an integer** (`:1670`) while one-shot results use boolean `ok`. Works today because the GUI keys on `m === "summary"`.

---

## 15. What I did to check these notes

* Read all of part 2, all of `hfp_ag.py`, `acl_meter.py`, the relevant parts of `bluforge_classic.py` part 1, `server.py` (argument building, job start/stop, result listing), and `static/classic.js` (result box).
* Read several saved result files in `logs/classic/` (read-only) to compare code with reality (HFP, A2DP, cycle, AVRCP).
* Ran two local-only experiments in the scratchpad (no Bluetooth, no PipeWire, no network): (a) `hfp_ag.AudioGateway` against a fake in-memory link, to capture the exact byte strings in 10.4 and to confirm the `ValueError`, `[]`-on-error and `HfpError`-on-EOF behaviours; (b) a pipe-to-a-non-reading-child test for the `stdin.close()` blocking in #6. I did **not** run any `verify_*.py` script and did not touch any radio, service or `pw-*` tool.
