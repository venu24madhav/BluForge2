# 02 - The BLE command-line helper (`bluforge_cli.py`) and the verify scripts

Files covered (all under `src/`): `bluforge_cli.py` (660 lines), `verify_core.py`,
`verify_adv_profiles.py`, `verify_pdu_types.py`, `verify_hfp_ag.py`.

How to read this guide: line numbers like `bluforge_cli.py:418` point at the file as it stood when this
guide was written. If you edit the file, the numbers drift but the order of things does not.
Everything here was derived by reading the code. The CLI and the verify scripts were **not run** (the task was
read-only and must not touch the radios); the only things executed were a Python syntax check (`py_compile`) and one
tiny stand-alone `json.dumps` test (see section 4, item 7). Where something is my reading rather than a verified
fact, I say so.

---

## 1. Overview

### 1.1 What the program is

`bluforge_cli.py` is a small command-line program. You start it in a terminal like this:

```
sudo python3 bluforge_cli.py --dev 1 adv nonconn --name Hello
        ^^^^^^^^^^^^^^^^^^^^  ^^^^^^^ ^^^ ^^^^^^^ ^^^^^^^^^^^^
        program               radio   |   |       options for that sub-command
                                      |   sub-command's own first argument
                                      sub-command (adv / interfere / sniff / caps)
```

It makes **one Bluetooth radio do one job** and then gives the radio back. It is the only program the BLE
tabs of the GUI use to reach a radio. The web page and `server.py` never touch a radio themselves.

"BLE" means Bluetooth Low Energy, the low-power half of Bluetooth (beacons, sensors, wearables, phones
advertising themselves). Bluetooth Classic (earbuds, A2DP audio, HFP calls) is a different code path in
`bluforge_classic.py` and is not covered here.

### 1.2 Where it sits

```
 browser (GUI)
     |  HTTP + live event stream (SSE)
     v
 server.py  (runs as root, owns no radio)
     |  starts ONE child process per job:   python3 -u bluforge_cli.py --dev N <sub-command> ...
     v
 bluforge_cli.py  (this guide)  -- one process, one radio, one job
     |  uses
     v
 hci_raw.py  HciSocket  -- exclusive raw access to radio hciN
     v
 hciN  (one Intel BE200 Bluetooth radio; this unit has up to 8: hci0..hci7)
```

Other modules the CLI calls (documented elsewhere, only summarised here):
`ble_ad.py` (builds advertising payloads and the 5 named beacon "profiles"), `ble_channels.py` (BLE
channel/frequency maths and Wi-Fi overlap), `ble_survey.py` (`Survey` class that aggregates sniffed
packets per device), `ble_decode.py` (used by `Survey`; decodes advertising bytes).

### 1.3 The four sub-commands

| Sub-command | What it does to the radio | Ends when | Output |
|---|---|---|---|
| `adv` | Transmits BLE **advertising** packets (a "BLE beacon"): chosen packet type, payload, channels, interval | `-t` seconds elapse, or Ctrl-C/SIGTERM. Directed high-duty type stops itself after about 1.28 s | Human text lines |
| `interfere` | Transmits deliberate 2.4 GHz noise using **DTM** (Direct Test Mode) on one channel, a list, a range, the whole band, or the channels that overlap a Wi-Fi channel. Special mode `adv-flood` instead reuses `adv` | `-t` seconds or signal | Human text lines |
| `sniff` | **Listens** for nearby BLE advertisements (passive) or listens and asks for more info (active). Human table, JSON lines, or "survey" mode (per-device summaries once a second) | `-t` seconds or signal | Table, or JSON lines |
| `caps` | Does not use a radio. Prints what the tool can do (types, ranges, defaults) plus which radios exist, as JSON | Immediately | One JSON document |

### 1.4 How the server launches it

* `server.py:378-413` (`build_argv`) turns the GUI form into an argument list:
  `[python, "-u", bluforge_cli.py, "--dev", N, "adv"|"interfere"|"sniff", ...options]`.
  Note `--dev N` goes **before** the sub-command (it is a global option, see 2.12).
* `server.py:175-199` (`Jobs.start`) runs it with `subprocess.Popen(... stdout=PIPE, stderr=STDOUT,
  start_new_session=True)`. Meaning: the child's error text is merged into its normal output, and the
  child gets its own process group so a signal sent to it cannot hit other jobs. `-u` = unbuffered output.
* `server.py:201` (`_reader`) reads the child's output line by line, strips the colour codes, and
  broadcasts each line to the browser. For `sniff` jobs, lines starting with `{"m":"` are parsed as
  survey data (`server.py:206`).
* `server.py:239-276` (`Jobs.stop`) stops a job by sending **SIGINT** (the same signal as Ctrl-C) to the
  process group, waiting 5 s, then **SIGTERM** (polite "please exit"), waiting 3 s more. It never sends
  SIGKILL, because SIGKILL cannot be caught and would skip the radio clean-up described below.
* `server.py:660-668` (`get_caps`) runs `bluforge_cli.py caps --compact` once and caches the result;
  the GUI uses it to fill in its drop-downs.
* One job per radio is enforced by the **server** (`by_dev`), not by this program. If you run the CLI by
  hand on a radio that a GUI job is using, the second `open()` simply fails (the kernel allows one
  exclusive owner).

What the GUI actually sends for sniff is always `--json --survey` (`server.py:414`), plus `--active`,
`--coded`, `--filter`, `-t` from the form. So the plain table output and the non-survey JSON are only
reachable by running the CLI by hand.

### 1.5 What it prints

Every status line starts with a coloured tag (`bluforge_cli.py:42-59`):

| Tag | Function | Goes to | Meaning |
|---|---|---|---|
| `[*]` cyan | `log()` | stdout | information |
| `[+]` green | `ok()` | stdout | success / state change ("ACTIVE", "stopped") |
| `[!]` yellow | `warn()` | stderr | warning, job continues |
| `[x]` red | `die()` | stderr | fatal error, exit code 1 |

Exit codes: 0 after a clean stop (including being stopped by a signal), 1 after `die()` or an uncaught
Python error (traceback on stderr). The server records this as the job's `exit_code`.

### 1.6 The radio lifecycle every command shares

`adv`, `interfere` and `sniff` all follow the same recipe. Understanding it once explains most of the file.

1. **Validate input** using plain Python, before the radio is touched. Errors here are clean `[x]` lines.
2. **Claim the radio**: `hci = HciSocket(dev); hci.open()` (`hci_raw.py:252`). `open()` tells the kernel
   to power the radio off (so BlueZ, the normal Linux Bluetooth service `bluetoothd`, lets go), then
   binds an *exclusive* raw socket ("HCI_CHANNEL_USER", meaning only this process may talk to the radio),
   then sends an HCI Reset and turns on all event types. HCI = Host Controller Interface, the
   command/event language between the computer and the radio chip. While we hold the socket, nothing
   else on the machine can use that radio.
3. **Install signal handlers** for SIGINT and SIGTERM that run a local function called `cleanup`.
4. **Do the work** (advertise / transmit test packets / scan), usually looping until a duration ends or
   a signal arrives.
5. **Clean up**: stop what the radio was doing (advertising disable / LE Test End / scan disable), then
   `hci.close()` (`hci_raw.py:351`): close the socket and ask the kernel to power the radio back on so
   BlueZ gets it again ("restored"). Print the "... restored" line, `sys.exit(0)`.
6. **Safety net**: the whole body from step 4 on is wrapped in `try ... except BaseException:
   safe_close(); raise` so that even an unexpected Python error still releases the radio before the
   process dies.

Two small Python tricks you will see in all three commands:

* `closed = {"v": False}` is a one-item dictionary used as a mutable flag. A nested function cannot
  reassign an outer variable without extra keywords, but it can change the contents of a dict. The
  flag makes `safe_close()` **idempotent** (running it twice does the clean-up only once), which matters
  because both the signal handler and the safety net can call it.
* `sys.exit(0)` inside a signal handler raises `SystemExit` in the main flow, which unwinds everything.
  That is why each command has `except SystemExit: raise` just before the generic handler: let a
  deliberate exit pass through untouched.

Why one process per radio? The module docstring (`bluforge_cli.py:6-15`) explains: an earlier design ran
several radios as threads inside one process; one hung script was killed with SIGKILL and three radios
wedged at once (see `docs/INSTALL_LOG.md`, 2026-10-05 about 18:52). With one process per radio, killing or
crashing one job can only affect its own radio.

### 1.7 Words you will meet

| Term | Plain meaning |
|---|---|
| Radio / controller / `hciN` | One Bluetooth chip. Linux names them `hci0`, `hci1`, ... The CLI's `--dev N` picks `hciN`. |
| BlueZ / `bluetoothd` | The normal Linux Bluetooth service. It owns radios unless we take one exclusively. |
| Advertising | A BLE device repeatedly broadcasting a tiny packet ("I exist, here is a bit of data") so others can find it. |
| Advertising channels 37, 38, 39 | The three radio frequencies (2402, 2426, 2480 MHz) BLE advertising uses. |
| PDU | "Protocol Data Unit": the packet type. Here: which kind of advertising packet. |
| AdvData / AD structure | The payload of an advertisement: a chain of blocks `[length][type][value]`, at most **31 bytes** for classic ("legacy") advertising. |
| Scan response | An optional second 31-byte payload a device returns when a scanner asks (active scanning). |
| Connectable / scannable / directed | Whether the advertiser accepts connections / answers scan requests / addresses one specific peer. |
| Interval | Time between advertisements (or between scan windows). |
| Beacon profile | A well-known advertisement format: iBeacon (Apple), Eddystone-UID/URL (Google), AltBeacon, plus a made-up "generic sensor". |
| DTM (Direct Test Mode) | A radio-lab mode in BLE chips: transmit (or receive) test packets on one chosen channel, no real connection. The CLI's "interference" is built on this (the HCI command "LE Transmitter Test"). |
| RF channel 0..39 | The numbering HCI test commands use: frequency = 2402 + 2 x N MHz. Not the same as the link-layer numbering where advertising channels are called 37/38/39 (see `ble_channels.py`). RF 0 = adv ch 37, RF 12 = adv ch 38, RF 39 = adv ch 39. |
| PHY | The radio modulation: `1M` (1 Mbit/s), `2M` (2 Mbit/s), `Coded` (long range, slow). DTM transmit in this CLI offers 1M and 2M only. |
| Payload pattern (PRBS9 etc.) | The bit pattern inside test packets. PRBS9 is pseudo-random (looks like noise); others are fixed patterns such as all-ones. |
| Dwell | How long the transmitter stays on one channel before moving on. |
| Hop | Moving to the next channel. A "sweep" hops through a range, "hop" through a list. |
| Passive / active scan | Passive: only listen. Active: also transmit a "scan request" to advertisers to get their scan response (so it does radiate). |
| Scan interval / window | A scanner listens for `window` ms out of every `interval` ms. Window = interval means listening all the time. |
| RSSI | Received signal strength in dBm (negative numbers; closer to 0 is stronger). |
| Extended advertising (BT5) | Newer advertising that uses secondary channels and bigger payloads; old-style scanning cannot see it. |
| Coded PHY | BT5 long-range PHY. |
| Dedup | Duplicate filtering: report each device only once. |
| Signals | SIGINT = Ctrl-C; SIGTERM = polite terminate; SIGKILL = cannot be caught, no clean-up possible. |

A short safety note: `interfere` radiates real RF noise on 2.4 GHz, which also carries Wi-Fi. Use it only
in a lab or shielded setup where that is acceptable.

---

## 2. `bluforge_cli.py`, top to bottom

### 2.1 Module docstring and imports (`bluforge_cli.py:1-40`)

* Lines 2-26 are the docstring: purpose, the "one process per radio" rationale, the four sub-commands,
  "run as root" (raw HCI needs the `CAP_NET_ADMIN` privilege).
* 27-31: standard library only: `argparse` (reads the command line), `json`, `signal`, `sys`, `time`.
* 33-35: project modules `ble_ad`, `ble_channels`, and the `Survey` class.
* 36-40: from `hci_raw`: `HciSocket` (the radio session), `HciError` (radio refused a command or timed
  out), `PAYLOAD_TYPES` (name -> DTM payload code), `detect_hci_indices` (list the radios that exist), and
  the five advertising-type constants. `DIRECTED_ADV_TYPES` (line 39) is imported but never used in this
  file.

### 2.2 Console helpers: `C`, `log`, `ok`, `warn`, `die` (`:42-59`)

* `C` (42) is a dictionary of ANSI colour escape codes (`\033[31m` = red, `rst` resets). They are always
  emitted, even when output is a pipe; the server strips them (`server.py` `ANSI` regex).
* `log(m)` (45) cyan `[*]`, stdout. `ok(m)` (49) green `[+]`, stdout. `warn(m)` (53) yellow `[!]`, **stderr**.
  `die(m)` (57) red `[x]`, stderr, then `sys.exit(1)`.
* All four use `flush=True` so each line appears immediately even when piped (otherwise Python would hold
  output in a buffer, and the GUI console would look frozen).
* `die()` raises `SystemExit`, so inside a command's `try` it is passed through by `except SystemExit: raise`.

### 2.3 Advertising tables: `ADV_TYPES`, `ADV_FREQ` (`:63-71`)

`ADV_TYPES` maps the CLI word to a 4-tuple `(HCI Advertising_Type code, description, scannable, directed)`:

| CLI word | Packet type | Code | Scannable | Directed |
|---|---|---|---|---|
| `ind` | ADV_IND: connectable, scannable, undirected | 0x00 | yes | no |
| `direct` | ADV_DIRECT_IND, **high** duty cycle | 0x01 | no | yes |
| `scan` | ADV_SCAN_IND: scannable, undirected | 0x02 | yes | no |
| `nonconn` | ADV_NONCONN_IND: non-connectable beacon | 0x03 | no | no |
| `direct-l` | ADV_DIRECT_IND, **low** duty cycle | 0x04 | no | yes |

"Duty cycle" here = how densely packets are sent. A *directed* advertisement names one target address and
carries no payload. High duty cycle sends very fast (hci_raw's comment says about 3.75 ms apart) but only
briefly.

`ADV_FREQ` (71) = `{37: 2402, 38: 2426, 39: 2480}` (MHz), used for display (160) and published by `caps`.

The dictionary's keys are also what `argparse` offers as choices (589) and what `caps` lists (554).

### 2.4 `build_advdata(name, use_flags, flags_val, raw_hex, profile, profile_cfg)` (`:74-88`)

Decides what bytes go into the advertisement's AdvData. **Exactly one source wins**, in this order:

1. `raw_hex` (the `--data` option): hex string, `:` and spaces removed, turned into bytes (79). Sent exactly
   as given; the code does **not** check that it is a well-formed chain of AD blocks (only the 31-byte
   limit is checked later, in `hci_raw.py`).
2. `profile` (`--profile`): `ble_ad.build_profile(profile, profile_cfg)` returns a dict with `adv_data` and
   `scan_rsp` (81-82).
3. Otherwise build from pieces: a Flags block (`ble_ad.flags(value)`, default `06` = "LE general
   discoverable, no classic Bluetooth"), unless `--no-flags`, then a Complete Local Name block if
   `--name` was given (83-88).

Returns `(adv_data, scan_rsp_from_profile_or_empty)`. Gotchas:

* The losers are silently ignored: `--data` beats `--profile` beats `--name/--flags`.
* Every profile in `ble_ad.PROFILES` currently returns an empty scan response, so the "profile gives a
  scan response" path (130-131) is never exercised today.
* Names are encoded as ASCII with `replace`, so non-ASCII characters become `?`.

### 2.5 `DIRECT_HIGH_DUTY_TIMEOUT_S = 1.28` (`:91-95`)

The Bluetooth specification says high-duty ADV_DIRECT_IND must stop by itself after 1.28 s if nobody
connects. `cmd_adv` uses this number so the *job* ends when the *radio* does, instead of claiming "ACTIVE"
forever (183-192).

### 2.6 `cmd_adv(a)`: advertise (`:98-210`)

**Input:** `a`, the parsed command line (an `argparse.Namespace`; its fields are listed in 2.12).
**Output:** console lines; runs until its time is up or it is stopped. **Why it exists:** to make a radio
emit a precisely specified BLE advertisement for a test.

Step by step:

1. **Pick the packet type** (99-103). Unpack the tuple from `ADV_TYPES`. If the type is directed and no
   `--peer` was given: `die("... requires --peer <addr>")`.
2. **Channel bit-map** (105-109). The three advertising channels are encoded as bits: 37 -> 1, 38 -> 2,
   39 -> 4, OR-ed together. `37,38,39` gives `0x07`. Unknown entries are silently ignored (`.get(..., 0)`).
   If the result is 0: `die("need at least one adv channel of 37,38,39")`.
   ```
   for ch in a.channels.split(","):
       chmap |= {"37": 1, "38": 2, "39": 4}.get(ch.strip(), 0)
   ```
3. **Parse `--profile-cfg`** (111-116). A string like `major=1,minor=2` becomes `{"major": "1", "minor": "2"}`.
   Pieces without `=` are dropped. Values stay strings; the profile code converts them.
   *Gotchas:* a misspelt key is silently ignored (the profile uses defaults), and a value cannot contain a
   comma (it is the separator), which matters for URLs.
4. **Build the payload before touching the radio** (123-133). Directed packets get an empty `ad`
   (they carry no AdvData). Otherwise `build_advdata(...)`. A scan response is only used for scannable
   types (`ind`, `scan`): `--scan-resp` hex wins, else the profile's (empty today). Any `ValueError` (bad hex,
   bad `--flags`, a profile number that is not an integer) or `ble_ad.AdError` (profile rule broken, payload
   too long) becomes `die("bad --data/--flags/--profile-cfg: ...")`. The long comment at 118-122 explains this
   catch was added because these errors used to leak out as a raw Python traceback.
5. **Claim the radio** (135-137): `HciSocket(a.dev, label="hci%d")`, then `hci.open()`.
   *Note:* `open()` is outside any `try`, so if it fails (for example, the radio cannot be bound within
   about 3 s) the user sees a traceback, not a clean `[x]` message.
6. **Define clean-up** (139-151).
   * `safe_close()` (139): once only (flag), try `hci.legacy_adv_enable(False)` (ignore any error), then
     `hci.close()`.
   * `cleanup(*_)` (148): call `safe_close()`, print `advertising stopped, hciN restored`, `sys.exit(0)`.
     The `*_` swallows the `(signum, frame)` arguments Python passes to signal handlers.
7. **Install handlers** (153-154): `signal.signal(SIGINT, cleanup)` and `SIGTERM`.
8. **Print what is about to happen** (157-168): PDU type with its numeric code, interval, channels with
   frequencies, peer (if any), AdvData in hex with length (and the profile name), scan response.
9. **Configure and enable** (170-178): `hci.legacy_adv_configure(ad, adv_type, channel_map, interval_ms,
   scan_rsp, peer_addr, peer_addr_type)` then `hci.legacy_adv_enable(True)`. On `HciError` or `ValueError`
   (for example payload over 31 bytes, bad peer address, radio refuses): `safe_close()` then
   `die(str(e))`. The radio is released first, then a clean error is shown.
   Own-address type is never set here, so the radio always advertises with its public address.
10. **Announce** (181): `[+] Advertising ACTIVE: <description>`.
11. **Wait** (183-200), three cases:
    * `direct` (high duty): sleep `min(duration, 1.28)` (or 1.28 if no duration), then `cleanup()`. The job ends
      by itself after about 1.28 s with exit code 0.
    * `--duration > 0`: sleep that long, then `cleanup()`.
    * Otherwise: loop `time.sleep(3600)` forever. Only a signal ends it.
    A signal interrupts the sleep because the handler raises `SystemExit`.
12. **Safety net** (201-210): `except SystemExit: raise` (normal exits pass), `except BaseException:
    safe_close(); raise` (anything unexpected: release the radio, then let the error show).

Illustrative output (assembled from the format strings, not captured from a run):

```
[*] PDU type      : ADV_NONCONN_IND (non-connectable beacon)  (Advertising_Type=0x03)
[*] Interval      : 100.0ms
[*] Channels      : 37,38,39  (map=0x07)  37=2402MHz 38=2426MHz 39=2480MHz
[*] AdvData       : 020106...  (17B)
[+] Advertising ACTIVE: ADV_NONCONN_IND (non-connectable beacon)
[*] advertising until Ctrl-C ...
[+] advertising stopped, hci1 restored
```

Gotchas in `cmd_adv` (details in section 4):
* The interval you ask for is **printed as requested, but `hci_raw.py:532` never goes below 100 ms**.
* A mixed channel list such as `-c 37,40` passes step 2 (the `40` is ignored) and then crashes at line 160
  (`ADV_FREQ[40]` -> `KeyError`) after the radio has been claimed; the safety net releases it but you get a
  traceback.
* For directed types `--name`, `--data`, `--profile`, `--scan-resp` are silently ignored. `--scan-resp` is also
  silently ignored for `nonconn`.

### 2.7 Channel helpers for `interfere` (`:214-260`)

**`wifi_dtm_channels(wifi_ch, bw)`** (214): asks `ble_channels.ble_channels_overlapping_wifi` for every
BLE RF channel whose frequency lies within the Wi-Fi channel's centre +/- half its width (inclusive), and
returns `(channel_list, centre_MHz)`. Examples I derived from that rule: Wi-Fi 6 at 20 MHz (centre 2437 MHz) ->
RF 13..22 (2428..2446 MHz, 10 channels); Wi-Fi 1 at 20 MHz -> RF 0..10 (11 channels); Wi-Fi 6 at 40 MHz ->
RF 8..27 (20 channels). The 40 MHz case is centred on the named channel's centre, which is a simplification
(my understanding is that real 40 MHz Wi-Fi channels are centred between two 20 MHz channels).

**`_check_channels(chans, label)`** (220): every channel must be 0..39 and the list must not be empty,
otherwise `die(...)`. It exists because `sweep` and `hop` once let a bad channel through to `hci_raw`, where
it crashed a running sweep (docstring 221-226).

**`resolve_channels(a)`** (235): turns `mode` + `arg` into `(list_of_RF_channels, description)`:

| mode | `arg` means | Result |
|---|---|---|
| `single` | one RF channel, 0..39 | `[n]` |
| `sweep` | `lo-hi`, e.g. `0-39` (lo <= hi) | every channel lo..hi, in order |
| `hop` | comma list, e.g. `0,12,39` | exactly that list, in that order (repeats allowed) |
| `band` | ignored | all 40 channels 0..39 |
| `wifi` | Wi-Fi channel 1..13, with `--bw` 20 or 40 | the overlapping BLE channels |

Anything else ends in `die("unknown interfere mode")` (260), which argparse's `choices` normally prevents.
Gotcha: `arg` is optional on the command line (`nargs="?"`, 607), so `interfere single` with no number reaches
`int(None)` (238) and fails with a Python traceback instead of a usage message. The same applies to `sweep`
(243: `None.split`), `hop`, and `wifi`. Garbage such as `sweep 5` (no dash) also gives a traceback.
These all happen **before** the radio is opened, so no radio is left claimed.

### 2.8 `cmd_interfere(a)`: radio noise (`:263-385`)

**Why it exists:** coexistence testing: put controlled 2.4 GHz energy on chosen frequencies and watch what
happens to Wi-Fi or other BLE links.

How it makes the noise: it uses DTM. The command `hci.tx_test_start(rf_channel, payload_len, payload_type,
phy, power_level)` (`hci_raw.py:416`) first ends any test already running, then issues "LE Enhanced
Transmitter Test" (or, when a power level was requested, the v4 variant with TX power control). The chip then
keeps sending test packets on that channel until "LE Test End".

Step by step:

1. **`adv-flood` shortcut** (264-270). Builds a fake argument set (`argparse.Namespace`) for `nonconn`
   advertising named `BLUFORGE-INTERFERER`, flags `06`, using `--interval` (default 20 ms) and
   `--adv-channels`, and `return cmd_adv(ns)`. So "flood" is just fast ordinary beacons. Everything below
   is skipped, including `--map`, `--dwell`, `--payload`, `--phy`, `--len`, `--power`, and the positional `arg`.
2. **Validate** (272-286), all before the radio is touched:
   * `--payload` must be a key of `PAYLOAD_TYPES` (`prbs9`, `prbs15`, `all0`, `all1`, `10101010`, `01010101`,
     `0x55`, `11110000`, `00001111`).
   * `--phy` must be `1M` or `2M`; `--len` 0..255; `--dwell` > 0.
   * `--power`: `max`/`hi`/`high` -> 0x7F; `min`/`lo`/`low` -> 0x7E (spec sentinels for max/min); otherwise
     an integer dBm, which must be -127..20. Not given -> `None` = firmware default.
     *Gotchas:* the integers 126 and 127 equal the sentinel values 0x7E/0x7F, so they pass and mean min/max;
     a non-number such as `--power abc` gives a traceback (284).
3. **Resolve channels** (288) with `resolve_channels(a)`.
4. **Print the plan** (289-298): plan description, channel list, frequencies, payload code/length/PHY, TX power,
   dwell (only if more than one channel), duration.
5. **`--map`** (299-301): print `map only (no transmission)` and return. No radio access. (But `main()` has
   already demanded root and a valid `--dev`, see 2.12.)
6. **Claim the radio** (303-306): `hci.open()`, `state = {"hops": 0}`, `closed` flag. As in `cmd_adv`,
   `open()` is outside the `try`.
7. **Clean-up and handlers** (308-323). `safe_close()` tries `hci.tx_test_end()` then `hci.close()`.
   `cleanup()` prints `interference stopped (N channel-dwells), hciN restored` and exits 0.
8. **`tx_once(n)`** (325): one `tx_test_start` on channel `n` with the validated settings.
9. **`tx_with_retries(n, retries=3, raise_on_fail=False)`** (328-353): try `tx_once`; on `HciError` or
   `ValueError` print a warning, wait 0.2 s, retry up to 3 times. If all fail: warn "giving up"; then either
   re-raise the last error (`raise_on_fail=True`) or return `False`. The docstring (329-340) states the
   design: on a multi-channel run a bad channel is skipped (noisy warning, keep going); on a single fixed
   channel there is nothing to fall back to, so the job must fail visibly rather than look active while
   radiating nothing.
10. **Announce** (356): `[+] Interference ACTIVE - transmitting on hciN`.
11. **Main loop** (357-374):
    * *One channel* (360-367): `tx_with_retries(..., raise_on_fail=True)`, `hops = 1`; if `--duration > 0` sleep
      then `cleanup()`; otherwise sleep forever.
    * *Several channels* (369-374): forever: for each channel, `tx_with_retries`, `hops += 1`, `sleep(dwell)`,
      and if a duration is set and elapsed, `cleanup()`.
      ```
      while True:
          for n in chans:
              tx_with_retries(n, raise_on_fail=False)
              state["hops"] += 1
              time.sleep(dwell_s)
              if a.duration > 0 and (time.time() - start) >= a.duration:
                  cleanup()
      ```
      The duration is only checked after each dwell, so the job can overrun by up to one dwell. A full pass of
      `band` mode at the default 800 ms dwell is about 40 x 0.8 = 32 s (plus small command overhead).
      Each hop does LE Test End then a new Transmitter Test, so there is a brief silent gap per hop (my reading;
      its length is not measured).
12. **Safety net** (376-385): same shape as `cmd_adv`.

Illustrative output (from the format strings):

```
[*] Plan          : Wi-Fi ch 6 center 2437MHz, 20MHz band
[*] DTM channels  : [13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
               13=2428MHz 14=2430MHz ...
[*] Payload       : prbs9 (0x00), len=37B, PHY=1M
[*] TX power      : firmware default
[*] Dwell         : 800.0ms/channel
[*] Duration      : until Ctrl-C
```

Gotchas: `interfere adv-flood --map` does **not** do a dry run, it transmits (the GUI offers both the "plan only" box
and the adv-flood mode). The `channel-dwells` count in the stop message counts attempts, including channels that
were skipped after failing.

### 2.9 Sniff tables and `decode_ad(data)` (`:389-416`)

* `ADV_REPORT_PDU` (389): legacy advertising-report event type number -> name (0 ADV_IND, 1 ADV_DIRECT_IND,
  2 ADV_SCAN_IND, 3 ADV_NONCONN_IND, 4 SCAN_RSP). Used only by the non-survey path (513). `ble_survey.py` has
  its own copy (`LEGACY_PDU`).
* `AD_TYPE_NAMES` (391): a table of AD type numbers to names. **Never used** anywhere in this file.
* `decode_ad(data)` (396): a deliberately small decoder for the non-survey modes. Walks the chain of
  `[length][type][value]` blocks; stops at a zero length or a block that overruns the data (401). Returns
  `{"raw": [(type, hex), ...]}` plus, when present: `name` (types 0x08/0x09, decoded as UTF-8 with
  replacement), `mfg` (manufacturer data, first two bytes little-endian = the company ID), `svc_uuid`
  (service data, first two bytes). It does not tell short names from complete ones. Survey mode uses the much
  richer `ble_decode.decode_adv` instead (see guide 08).

### 2.10 `cmd_sniff(a)`: listen for advertisers (`:419-546`)

This is the "sniffer", but note what that means here: the radio only receives **advertising** packets on
channels 37/38/39 (and BT5 extended advertising in survey mode). It does not follow connections or capture data
traffic.

Step by step:

1. **Check** `--window <= --interval` (420-421), else `die`. (The scan window cannot be longer than its interval.)
2. **Claim the radio** (422-423), set up `seen = {}` (address -> count, used by the non-survey path),
   `closed` flag, and `survey_ref = [None]`. (A one-item list is the same mutable-cell trick as the dict.)
3. **`safe_close()`** (428-436): tries `ext_scan_enable(False)` and `scan_enable(False)` in turn, ignoring
   errors from each (it does not know which kind of scan was started, so it tries both), then `hci.close()`.
4. **`cleanup()`** (438-449): if in `--survey --json` mode and the survey exists, first prints any not-yet-sent
   device records plus a final stats line (439-445); then `safe_close()`; then, **unless `--json`**, prints
   `sniff stopped, N unique device(s) seen, hciN restored`; `sys.exit(0)`. In JSON mode the stop is silent.
5. **Handlers** (451-452).
6. **Start scanning** (460-473). The newer survey logic:
   ```
   ext_on = False
   if a.survey and a.ext != "off":
       try:
           hci.ext_scan_configure(interval_ms=a.interval, window_ms=a.window, active=a.active, coded=a.coded)
           hci.ext_scan_enable(True, filter_dup=False)
           ext_on = True
       except HciError as e:
           if a.ext == "on": raise
           log("extended scanning unavailable (%s) - falling back to legacy scan" % e)
   ```
   * Extended scanning is tried **only in survey mode** and unless `--ext off`. It receives both Bluetooth 5
     extended advertising and ordinary legacy advertising in one go (`hci_raw.py:586-587`). `--coded` adds the
     long-range Coded PHY as a second scanning PHY.
   * `--ext auto` (default): if the radio refuses (HciError), log and fall back to legacy scanning.
     `--ext on`: re-raise the error (job ends). `--ext off`: skip it.
   * Fallback / non-survey (471-473): `scan_configure(interval, window, active)` then
     `scan_enable(True, filter_dup=...)`. In survey mode the controller's own duplicate filter is forced **off**
     (the survey code wants every report to track RSSI); otherwise `--dedup` decides.
   * `--active` makes the scanner transmit scan requests (it is not purely passive).
   * Note the only error handled is `HciError` for the extended attempt. Anything else (for instance a
     `ValueError` from a silly `--interval`) ends in a traceback after the safety net has released the radio.
7. **Header** (475-481), only when not `--json`: `Sniffing BLE advertising (passive|active), hciN`, the
   filter/dedup/duration line, and a column header `TIME RSSI ADDRESS TYPE PDU NAME/DATA`.
8. **Survey object** (483-488): `start = time.time()`; `survey = Survey(a.filter)` if `--survey`;
   `last_emit` and `warned` are one-item lists (mutable cells).
9. **`emit_survey()`** (490-494): prints one compact JSON line per device that changed since the last call
   (`survey.drain()`), then one stats line (`survey.stats()`), and resets the one-second timer. Compact
   separators (`(",", ":")`) make every line start with exactly `{"m":"dev"` / `{"m":"stats"`, which is what
   the server's prefix test expects.
10. **`on_report(rep)`** (496-527): called by `hci.read_events` for each received advertising report.
    * Survey mode: `survey.add(rep)` inside `try/except Exception`: one malformed report prints a JSON
      `warn` line and never ends the job (498-502). Then return.
    * Otherwise (non-survey): if `--duration` has elapsed, `cleanup()` (503-504); `decode_ad(rep["data"])`;
      apply `--filter` (a case-insensitive substring of "ADDRESS NAME", 507-510); count the address in
      `seen`; look up the PDU name; then print either one JSON object per report (514-518:
      `t`, `addr`, `addr_type`, `rssi`, `pdu`, `ad`, `adv_hex`) or one table row (519-527). The table row shows
      the name, else `mfg=0x....`, else `svc=0x....`.
    Table layout, from the format strings (illustrative):
    ```
    TIME      RSSI ADDRESS           TYPE            PDU             NAME/DATA
       1.23   -61 AA:BB:CC:11:22:33 random          ADV_IND         mfg=0x004c
    ```
11. **Main loop** (529-541), forever:
    * If a duration is set and elapsed: `cleanup()`.
    * `hci.read_events(0.5, on_adv_report=on_report)` reads radio events for 0.5 s, calling `on_report`.
    * If `hci.dead` (the socket broke, e.g. the radio was unplugged or reset): raise `HciError`; the safety net
      releases the radio and the job ends with a traceback.
    * Survey mode only: every second or more, `emit_survey()`. If no report at all arrived within 20 s,
      print one JSON `warn` line ("radio may be unplugged, or no BLE devices are in range"). The 20 s test uses the
      raw report count, so a `--filter` that matches nothing does not trigger it.
12. **Safety net** (542-546).

What the output looks like in the mode the GUI uses (`--json --survey`): a stream of lines such as
`{"m":"dev", ...one device's accumulated record...}` followed once a second by
`{"m":"stats","t":..,"devices":..,"reports":..,"rate":..,"evicted":..,"idle_s":..}`. The fields of the device
record are built by `Survey.record` (`ble_survey.py:115`), described in guide 08. The server turns each batch into
one live event per second (`server.py:201-232`).

Gotchas:
* `--ext`, `--coded` have an effect **only with `--survey`**. In the legacy fallback, `--coded` is dropped without
  any message (so the GUI's "include LE Coded PHY" box does nothing on a radio without extended-scan support).
* `--dedup` is ignored in survey mode.
* `--survey` without `--json`: prints the human header, then JSON lines; the final drain on stop is skipped
  (it requires `--json`, 439) and the stop line reports `0 unique device(s)` because the survey path never fills
  `seen`. Cosmetic, since the GUI always passes `--json`.
* The two JSON `warn` lines (501, 540) are written with `json.dumps(...)` default separators, i.e.
  `{"m": "warn", ...}` with a space, so they do **not** match the server's `{"m":"` test. See section 4.

### 2.11 `cmd_caps(a)`: print capabilities (`:550-578`)

Builds one dictionary and prints it with `json.dumps` (indented unless `--compact`). It needs neither root nor a
working radio (the root/`--dev` checks in `main()` skip `caps`, 641). The `"hci"` field is just `"hci<--dev>"`
even if that radio does not exist.

Content, with the source of each value:

| Key | Content |
|---|---|
| `hci` | `"hci0"` (from `--dev`) |
| `adv.types` | list of `{id, pdu, advertising_type, directed}` from `ADV_TYPES` (554) |
| `adv.profiles` | sorted profile names: `altbeacon, eddystone_uid, eddystone_url, generic_sensor, ibeacon` |
| `adv.channels`, `adv.channel_freqs` | `[37,38,39]`, `{"37":2402,"38":2426,"39":2480}` (JSON turns the numeric keys into strings) |
| `adv.interval_ms` | `{"min":20,"max":10240,"default":100}` (hard-coded in the CLI) |
| `adv.own_addr` | `["public","random"]` (see section 4: not actually selectable) |
| `adv.adv_data_max_bytes` | 31 (`ble_ad.MAX_LEGACY_ADV_LEN`) |
| `interfere.modes` | `wifi, single, sweep, hop, band, adv-flood` |
| `interfere.payloads` | the sorted `PAYLOAD_TYPES` names (includes both `01010101` and its alias `0x55`) |
| `interfere.phys`, `.bw`, `.wifi_channels`, `.dtm_channels` | `["1M","2M"]`, `[20,40]`, `1..13`, `{min 0, max 39, freq_mhz "2402+2*N"}` |
| `interfere.len`, `.dwell_ms`, `.power` | `{0..255, default 37}`, `{default 800}`, `{values: max, min, <dBm int>}` |
| `sniff` | `{"modes":["passive","active"],"dedup_default":false,"outputs":["table","json"]}` |
| `hardware.all_hci` | `detect_hci_indices()`: the radios actually present, read from `/sys/class/bluetooth` |

Notes: `caps` does **not** mention `--survey`, `--ext`, `--coded`, `--filter`, `--interval`, `--window` for sniff.
The GUI fills its drop-downs from `adv.profiles`, `interfere.payloads` and `hardware.all_hci`
(`static/app.js:129-139`); I did not find it reading `adv.interval_ms` or `adv.own_addr`. The server caches the result
(`server.py:660`).

### 2.12 `main()` and every command-line option (`:582-656`)

`argparse` builds a parser with one **global** option and four **sub-commands** (`add_subparsers`, required).
Each sub-parser stores its handler with `set_defaults(func=...)`; at the end `args.func(args)` runs it (656).

**Global**

| Option | Type, default | Meaning |
|---|---|---|
| `--dev N` (585) | int, `0` | Which radio (`hciN`). Help text says "0-7" but the real check is against the radios that exist (see below). **Must come before the sub-command**: `... adv nonconn --dev 1` is rejected by argparse. |

**`adv`** (588-603)

| Option | Type, default | Meaning / notes |
|---|---|---|
| `type` (positional) | one of `ind, direct, direct-l, scan, nonconn` | Packet type (2.3) |
| `-i, --interval` | float, `100` | Advertising interval in ms. Effective minimum is 100 ms (see section 4) |
| `-c, --channels` | `"37,38,39"` | Comma list of 37/38/39 |
| `-p, --peer` | none | Target address `AA:BB:CC:DD:EE:FF`. Required for `direct`/`direct-l` |
| `--peer-type` | `public` or `random`, `public` | Type of the target address |
| `-n, --name` | none | Complete Local Name (used only without `--data`/`--profile`, and not for directed types) |
| `--flags` | `"06"` | Flags byte in hex (parsed with `int(x,16)`) |
| `--no-flags` | switch | Omit the Flags block |
| `-d, --data` | none | Raw AdvData in hex (`:`/spaces allowed). Overrides everything else |
| `--profile` | one of `ble_ad.PROFILES` keys | Beacon profile: `ibeacon`, `eddystone_uid`, `eddystone_url`, `altbeacon`, `generic_sensor` |
| `--profile-cfg` | none | `k=v,k=v` profile parameters, e.g. `major=1,minor=2`. Accepted keys come from `ble_ad.PROFILES` lambdas: ibeacon `uuid,major,minor,power`; eddystone_uid `namespace,instance,power`; eddystone_url `url,power`; altbeacon `beacon_id,mfg_reserved,power`; generic_sensor `name,sensor_id,value` |
| `--scan-resp` | none (dest `scan_resp`) | Scan response data in hex; only for `ind`/`scan` |
| `-t, --duration` | int seconds, `0` | `0` = until stopped |

**`interfere`** (605-618)

| Option | Type, default | Meaning / notes |
|---|---|---|
| `mode` (positional) | `wifi, single, sweep, hop, band, adv-flood` | Kind of interference (2.7/2.8) |
| `arg` (positional, optional) | none | Channel / `lo-hi` / list / Wi-Fi channel. Not needed for `band`, `adv-flood`; **required but not enforced** for the others |
| `-w, --bw` | int, `20` | Wi-Fi width, 20 or 40 (checked only in `wifi` mode) |
| `-l, --len` | int, `37` | DTM payload length in bytes, 0..255 |
| `-P, --payload` | `"prbs9"` | Pattern name from `PAYLOAD_TYPES` |
| `--phy` | `"1M"` | `1M` or `2M` |
| `-D, --dwell` | float ms, `800` | Time per channel when hopping |
| `--power` | none | `max`/`min`/integer dBm. Setting it switches to the v4 transmitter-test command |
| `-t, --duration` | int s, `0` | `0` = until stopped |
| `--map` | switch | Print the plan and exit, no transmission (**not honoured by `adv-flood`**) |
| `--interval` | float ms, `20` | `adv-flood` only: advertising interval |
| `--adv-channels` | `"37,38,39"` | `adv-flood` only |

**`sniff`** (620-634)

| Option | Type, default | Meaning / notes |
|---|---|---|
| `--active` | switch | Active scanning (sends scan requests) |
| `--interval` | float ms, `100` | Scan interval |
| `--window` | float ms, `100` | Scan window; must be <= interval |
| `--dedup` | switch | Controller-side duplicate filter (ignored in survey mode) |
| `--filter` | none | Case-insensitive substring matched against `ADDRESS NAME` |
| `-t, --duration` | int s, `0` | `0` = until stopped |
| `--json` | switch | Machine-readable output |
| `--survey` | switch | Per-device aggregation, one batch per second, scans extended + legacy advertising |
| `--ext` | `auto`, `on`, `off`; `auto` | Survey only: use extended-advertising scanning (auto = if the radio supports it) |
| `--coded` | switch | Survey only: also scan the Coded PHY |

**`caps`** (636-638): `--compact` (single-line JSON).

**After parsing** (640-656):
* If the sub-command is not `caps`: must be root (`os.geteuid() != 0` -> `die`, 643-644). `import os` is done
  here, inside the function (642).
* `present = detect_hci_indices()`; if `--dev` is not in it, `die("--dev N: no such controller on this machine
  (hci... present: ...)")` (651-655). The comment explains why: an earlier fixed `0..7` check let non-existent
  radios through on machines with fewer controllers. (Cosmetic: the message prints `hcis present` when there is
  more than one radio and `hci present` when there is one.)
* `args.func(args)`.
* Because the root and `--dev` checks run for every non-`caps` sub-command, even `interfere ... --map` needs root and a
  real radio number.

Last two lines (659-660): `if __name__ == "__main__": main()`, so importing the file does not run anything.

---

## 3. The verify scripts

All four live in `src/`, import their sibling modules, and can be started from any directory
(`python3 /path/to/src/verify_x.py`, because Python puts the script's own folder on its import path). I did not run
any of them. Three need real radios; the fourth needs none.

**Before running the three hardware scripts:** they power radios off and on and take exclusive control for several
seconds. Run them as root, only against radios that nothing else is using. The GUI server does not know about
them, so stop GUI jobs on those radios (and do not use a radio reserved for Classic).
`docs/DEPLOYMENT_GUIDE.md` lists them as post-install smoke tests.

### 3.1 `verify_core.py`: the two core demo capabilities (needs 4 radios)

**What it proves.**
* Test 1: a radio really puts energy on the air using DTM. Test 2: a custom advertisement really arrives over the air.

**How to run.**
```
sudo /usr/bin/python3 verify_core.py            # uses the first 4 radios found
sudo /usr/bin/python3 verify_core.py 0 1 2 3    # pin the 4 radios (order matters, see below)
```

**Radio choice** (`verify_core.py:17-29`): it needs `_NEEDED = 4` radios. Arguments (first four integers) or the first four
from `detect_hci_indices()`. Fewer than 4 -> message and exit 1. The four are assigned in order to
`INTERFERER_HCI, RX_HCI, ADV_HCI, SCAN_HCI`. This happens at import time, not in a function. The check does not
verify the numbers are distinct.

**Test 1** `test_tx_interference()` (`:32-74`), "interference is real":
1. Channel 19 = 2440 MHz (overlaps Wi-Fi channel 6).
2. The comment (36-42) explains the trick: after a *transmitter* test the radio's "packets" count is always 0 by
   specification, so you cannot ask the transmitter if it worked. Instead a **second** radio listens.
3. Open the RX-witness radio, `rx_test_start(rf_channel=19, phy="1M")` (LE Enhanced Receiver Test).
4. Open the interferer, `tx_test_start(19, payload_len=37, payload_type=0x00 (PRBS9), phy="1M")`.
5. Sleep 2 s. `tx.tx_test_end()`, `tx.close()` (interferer returned to BlueZ).
6. `n = rx.tx_test_end()`: the same "LE Test End" command, which on the receiver returns the number of packets received.
7. PASS if `n` is not None and `n > 0`. Any `HciError` -> FAIL with the message. The `finally` closes both radios.
Historical result recorded in `docs/INSTALL_LOG.md`: 3203 packets received in 2 s.

**Test 2** `test_adv_and_scan()` (`:77-129`), "advertisement is real":
1. Builds AdvData by hand (84-88): Flags block `02 01 06`, then Manufacturer-specific block (type 0xFF) with company ID
   `0xFFFF` (the Bluetooth SIG value reserved for testing) and the marker text `BLUFORGE1`. Total 17 bytes.
2. Advertiser: `legacy_adv_configure(adv_data, adv_type=0x03, interval_ms=100)` (ADV_NONCONN_IND), `legacy_adv_enable(True)`.
3. Scanner: passive scan, interval = window = 100 ms, `read_events(3.0, on_adv_report=...)`: counts reports whose
   data contains the marker and collects RSSI.
4. Disable scan and advertising. PASS if at least one copy was seen (prints the count and average RSSI).
   The `finally` closes both. Historical result: 29 copies, average -37.5 dBm.

**Summary** (`:132-138`): prints `=== SUMMARY: interference=PASS|FAIL, adv+scan=PASS|FAIL ===`. Exit code 0 only if both passed.

Gotchas: it catches only `HciError`; other errors (for instance an `OSError`) abort after the `finally` blocks.
The restore messages print regardless of whether the restore worked.

### 3.2 `verify_adv_profiles.py`: every beacon profile, byte for byte (needs 2 radios)

**Run:** `sudo /usr/bin/python3 verify_adv_profiles.py [ADV_IDX SCAN_IDX]`. Radios: arguments (first two) or the first two
detected (13-20); the first is the advertiser, the second the scanner.

**What it does** (`main`, `:23-51`):
1. Open both radios; scanner is passive, interval = window = 100 ms, enabled once.
2. For **each** name in `ble_ad.PROFILES` (ibeacon, eddystone_uid, eddystone_url, altbeacon, generic_sensor) it builds
   the profile with its default values, advertises it as ADV_NONCONN_IND every 100 ms, and listens for 1.5 s.
3. Counts all reports seen, and the ones whose data **equals the profile's bytes exactly**. PASS for that profile if
   the exact-match count is above 0. Prints a line such as
   `ibeacon  sent=30B  reports_seen=NN  exact_byte_matches=NN  PASS`.
4. `finally`: scanner off, both radios closed. Final line `ALL PROFILES: PASS|FAIL`; exit 0 only if all passed.

Why it is a good test: matching the exact bytes proves the payload built by `ble_ad.py` arrived unchanged; other
advertisers nearby cannot cause a false pass. Historical result: 16/16 exact matches per profile.
Gotcha: radios are opened and the scan is started *before* the `try` (24-31), so a failure there leaves radios unrestored.

### 3.3 `verify_pdu_types.py`: all legacy advertising packet types (needs 2 radios)

**Run:** `sudo /usr/bin/python3 verify_pdu_types.py [ADV_IDX SCAN_IDX]` (same radio selection as above).
`PEER_ADDR = "AA:BB:CC:DD:EE:FF"` (line 21) is an arbitrary made-up target for the directed types.

**What it does** (`main`, `:24-70`):
1. Advertiser opened; scanner opened in **active** mode (so it can also see scan responses), 100 ms / 100 ms.
2. Loops over five configurations: ADV_IND and ADV_SCAN_IND (with Flags and a scan response `05 09 'test'`),
   ADV_NONCONN_IND (Flags only), ADV_DIRECT_IND high duty and low duty (peer address only).
3. For each: clear the counter, configure, enable (reaching the print "enabled OK" means the radio accepted the
   commands, because `_check` would have raised otherwise), listen 1.2 s, disable, and count reports by event type
   (`rep["event_type"]`). Prints the dictionary, for example `{0: 13, 4: 12}` (0 = ADV_IND, 4 = SCAN_RSP).
4. Result per type: PASS if any report was seen; for directed types 0 reports is printed as `INFO` (expected,
   nobody scans for that fake address).
5. At the end it prints a PASS/FAIL for the three non-directed types only and returns that as the exit status.
   Directed types never affect the exit code.

Gotchas: the "reports seen" test counts reports from **any** advertiser the scanner hears (33-35, 54-56) and never
checks the event type or the payload, so in a busy RF environment it could pass even if our own packet was not
sent (compare `verify_adv_profiles.py`). Same "open before `try`" weakness as 3.2 (25-30).

### 3.4 `verify_hfp_ag.py`: self-test of the HFP Audio Gateway (needs no hardware)

**Context.** HFP (Hands-Free Profile) is the Bluetooth Classic profile used for car kits and headsets. In a call
setup the "Audio Gateway" (AG) is the phone side, the "Hands-Free" unit (HF) the headset/car. They talk with text
commands called AT commands (for example `AT+BRSF=...`) over a serial-like link (RFCOMM). `hfp_ag.py` implements the
AG side so this host can act as a phone for a headset under test (`bluforge_classic.py` imports it at line 1707).

**Run:** `python3 verify_hfp_ag.py` (no root needed; no Bluetooth is used). Exit code 0 = pass, 1 = a check failed.

**How it works.**
1. `socket.socketpair()` (`:33`) makes two connected in-memory endpoints (a pretend radio link).
2. A background thread runs `hf_script` (`:7-31`): a scripted Hands-Free unit that sends commands in this order and
   stores each reply: `AT+BRSF=<CLIP|VOL features>`, `AT+CIND=?`, `AT+CIND?`, `AT+CMER=3,0,0,1`, `AT+CHLD=?`,
   `AT+VGS=9`, `AT+VGM=5`, wait for `RING`, `ATA` (answer), `AT+CLCC`, `AT+FOO` (deliberately unknown), `AT+CHUP`
   (hang up).
3. The main thread runs the real `AudioGateway` on the other endpoint: `establish_slc(5)` (the SLC = "service level
   connection": the opening handshake; the script asserts it completed), `pump(0.8)`, `ring("+15551234567")`,
   then waits up to 3 s for `ag.answered` and up to 4 s for `ag.hung_up`.
4. It evaluates 10 checks (`:45-56`) and prints `PASS`/`FAIL` per line, then the AG's event timeline:

| Check | What must be true |
|---|---|
| BRSF reply | AG answers `+BRSF: <its features>` and `OK` |
| CIND test lists callsetup | `"callsetup",(0-3)` appears in the indicator list |
| CIND status | exactly `+CIND: 1,0,0,0,5,0,5` (the default indicator values) |
| CMER accepted | `OK` and `ag.cmer_on` is true |
| volume captured | `(speaker, mic) == (9, 5)` |
| RING + CLIP | `RING`, `+CLIP`, and the number arrived |
| ATA answers + call=1 | AG recorded the answer, emitted `ciev call=1`, replied `OK` |
| CLCC lists active call | `+CLCC: 1,0,0,0,0` |
| unknown AT -> ERROR | `AT+FOO` got `ERROR` |
| CHUP ends call | AG recorded hang-up and `call` indicator is 0 |

Gotcha: if the scripted HF thread gets no `RING` it dies on a socket timeout, the missing replies then cause a
`KeyError` in the checks dictionary (line 46 onward) rather than tidy `FAIL` lines. There are fixed sleeps/timeouts, so
on a very slow machine it could in principle be flaky (my judgement, not observed).

---

## 4. Things I noticed

Ordered roughly by how much they could hurt. None of these were reproduced (I did not run anything); each is
my reading of the code with line numbers so you can check.

1. **[risk] A Stop during start-up can leave a radio stranded.** In all three commands the signal handlers are installed
   *after* `hci.open()`, and `open()` is outside the `try` (`bluforge_cli.py:135-154`, `303-323`, `422-452`). `open()` can take
   a second or more (power-off, up to 3 s of bind retries, HCI reset). If SIGINT/SIGTERM arrives in that window the default
   Python behaviour applies: `KeyboardInterrupt` (or immediate termination for SIGTERM), nothing calls `close()`, and the
   radio is not powered back on by us. `hci_raw.py:726-735` documents that an abrupt exit can leave a radio showing
   `Powered=no` until re-powered. I did not test the exact end state. The server can trigger this by Stop / Stop All pressed
   right after Start.
2. **[inconsistency] "restored" is printed unconditionally.** `safe_close()` throws away the `(ok, msg)` that `hci.close()` returns
   (`bluforge_cli.py:139-146`, `308-315`, `428-436`; `hci_raw.py:351-371` was changed specifically so callers could report real
   failure). So `... hciN restored` appears even if the restore failed.
3. **[inconsistency] The 20 ms advertising interval is not honoured.** `hci_raw.py:532` clamps any interval below 100 ms up
   to 100 ms (`max(..., 160)` in 0.625 ms units). But `caps` publishes `interval_ms.min = 20` (`bluforge_cli.py:559`), the
   `adv-flood` default is 20 ms (`:616`) so "flood" advertises at about 10 per second per channel, not 50, and the `Interval :`
   log line prints the requested value (159), not the effective one. The comment says 100 ms is the spec minimum for
   non-connectable advertising; from my general knowledge that was the Bluetooth 4.x rule and newer versions allow 20 ms, but
   I have not checked what the BE200 firmware accepts.
4. **[bug] `interfere adv-flood --map` transmits.** The `adv-flood` branch returns at `bluforge_cli.py:264-270`, before the
   `--map` check at 299. The GUI shows the "plan only (no transmission)" box together with the adv-flood mode
   (`static/index.html:112`, `app.js:172`).
5. **[bug] Bad channel in a list -> traceback after the radio is claimed.** `adv -c 37,40`: the chmap loop (106-107) ignores `40`,
   the list is non-empty so validation passes, then `ADV_FREQ[int(c)]` at line 160 raises `KeyError`. The safety net releases the
   radio, but the user sees a raw traceback. `-c 37,abc` fails the same way (`ValueError`).
6. **[inconsistency] `own_addr` is advertised but not implemented.** `caps` lists `["public","random"]` (`:560`); `adv` has no option
   for it and never passes `own_addr_type`, so the radio always uses its public address. (`--peer-type` is the *target's* type.)
7. **[bug, cosmetic] Survey `warn` lines never get the intended treatment.** `bluforge_cli.py:501` and `:540` print
   `json.dumps({"m": "warn", ...})` with default separators, producing `{"m": "warn", ...}` (space after the colon). The server
   tests `text.startswith('{"m":"')` (`server.py:206`) with no space, so these lines fall through as raw JSON text in the console;
   the `warn` branch at `server.py:224-225` (which would turn it into a `[!] ...` line) is dead code for these two messages. (`json.dumps` default output vs compact output was checked in isolation.)
8. **[gap] Missing positional argument gives a traceback.** `arg` is `nargs="?"` (`:607`) but `single/sweep/hop/wifi` need it
   (`:238`, `243`, `248`, `253`): `int(None)` / `None.split`. Similar: `--power abc` (`:284`), `sweep 5`.
9. **[gap] Silent no-ops.** `--coded` is dropped silently when extended scanning is unavailable (`:471-473`) and outside `--survey`;
   `--ext` and `--dedup` do nothing in some modes; for `adv`, `--data` beats `--profile` beats `--name/--flags`, directed types
   ignore payload options, and unknown `--profile-cfg` keys are ignored. None of these print a warning.
10. **[risk] A multi-channel interference job never gives up.** In the sweep/hop/band loop (`:369-374`) a channel that keeps failing
    is skipped forever with `raise_on_fail=False`; only a dead socket (`OSError`, which `tx_with_retries` does not catch) ends
    the job. If the radio keeps timing out, the console says `Interference ACTIVE` while nothing may be transmitting. (Single
    channel jobs do fail visibly, `:361`.) Also `state["hops"]` counts skipped channels.
11. **[risk, theoretical] Second signal during a slow restore.** `Jobs.stop` sends SIGINT, then SIGTERM after 5 s
    (`server.py:247-262`). `hci.close()` can legitimately take up to about 5 s when recovery is struggling (`hci_raw.py:370`,
    `726-744`). A second `cleanup()` would find `closed["v"]` already set, skip the work, print "restored" and exit 0 mid-recovery.
12. **[cosmetic] Error-path polish.** `hci.open()` errors surface as tracebacks, and `sniff` only converts `HciError` for the
    extended-scan attempt; other start-up errors are tracebacks (radio still released). The table header `TIME` is 8 wide
    but the time column is 7 wide (`:480` vs `:525-526`). The `--dev` error says `hcis present`. `--power 126/127` are taken as the
    min/max sentinels.
13. **[dead code / duplication]** `AD_TYPE_NAMES` (`:391`) and `DIRECTED_ADV_TYPES` (import, `:39`) are unused; `ADV_REPORT_PDU`
    and `decode_ad` duplicate what `ble_survey.py`/`ble_decode.py` do for survey mode; the profile scan-response path (`:130-131`)
    is dead because no profile returns one; `import os` inside `main()` (`:642`).
14. **[approximation]** `interfere wifi N --bw 40` centres the 40 MHz span on the named channel's centre (`ble_channels.py`
    `ble_channels_overlapping_wifi`), which is not how real 40 MHz Wi-Fi channels are centred.
15. **[test-script weaknesses]** `verify_pdu_types.py` passes on any report from any device and ignores directed types in its exit
    code (`:33-35`, `54-70`). `verify_adv_profiles.py` and `verify_pdu_types.py` open radios before the `try` so a setup failure leaves
    them unrestored (`verify_adv_profiles.py:24-31`, `verify_pdu_types.py:25-30`). `verify_core.py` does not check the four radios
    are distinct. `verify_hfp_ag.py` can end in `KeyError` instead of `FAIL`.
16. **[docs out of date]** `docs/DEPLOYMENT_GUIDE.md` (about line 165-176) says `verify_core.py` uses three radios and tells you to
    edit indices at the top; the script now needs four and takes indices as arguments. It lists three verify scripts; there are four
    (`verify_hfp_ag.py` is not mentioned).

---

## 5. Cheat sheet (examples built from the argparse definitions; not executed)

```
sudo python3 bluforge_cli.py caps --compact                           # no radio used
sudo python3 bluforge_cli.py --dev 1 adv nonconn -n Hello -t 30       # plain beacon for 30 s
sudo python3 bluforge_cli.py --dev 1 adv nonconn --profile ibeacon --profile-cfg major=1,minor=2
sudo python3 bluforge_cli.py --dev 1 adv direct --peer AA:BB:CC:DD:EE:FF   # self-stops after ~1.28 s
sudo python3 bluforge_cli.py --dev 2 interfere wifi 6 --bw 20 --map   # plan only, no transmit
sudo python3 bluforge_cli.py --dev 2 interfere single 19 --power max -t 10
sudo python3 bluforge_cli.py --dev 2 interfere sweep 0-39 -D 200 -t 60
sudo python3 bluforge_cli.py --dev 0 sniff -t 20                      # human table
sudo python3 bluforge_cli.py --dev 0 sniff --json --survey --coded    # what the GUI runs (plus its own flags)
```

Stop any of them with Ctrl-C (or SIGTERM) and let it finish: that is what hands the radio back. Never `kill -9` a running job.
