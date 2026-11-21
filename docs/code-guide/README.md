# BluForge code guide - start here

A plain-language, file-by-file walkthrough of the whole code base (about 6,500 lines of guide for about 8,000 lines of code).
Written for someone new to the project. Each guide explains every constant, class and function in file order, with line numbers.

## Read in this order
| # | Guide | Code it explains | Read it to understand |
|---|---|---|---|
| 0 | this page | - | the map |
| 1 | `01_radio_layer.md` | `hci_raw.py`, `ble_channels.py`, `ble_ad.py` | how the program talks to a Bluetooth radio directly (HCI commands/events, taking and returning a radio, advertising data builders) |
| 2 | `02_ble_cli_and_tests.md` | `bluforge_cli.py`, `verify_*.py` | the BLE helper program: advertise, interfere, sniff/survey, capabilities; and the test scripts |
| 3 | `08_decoder_survey_meter.md` | `ble_decode.py`, `ble_survey.py`, `acl_meter.py` | how advertisements become readable device records; the exact throughput meter |
| 4 | `04_classic_engine_part1.md` | `bluforge_classic.py` (first half) | the BlueZ/D-Bus engine: pair, connect, scan, audio plumbing, AVRCP, Interrogate |
| 5 | `05_classic_jobs_and_hfp.md` | `bluforge_classic.py` (second half), `hfp_ag.py` | the long-running tests: A2DP streaming, AVRCP watch, connect cycling, HFP calls |
| 6 | `03_server.md` | `server.py` | the web server that ties everything together: jobs, live stream, API routes, BlueZ gate |
| 7 | `06_gui_main_page.md` | `index.html`, `style.css`, `app.js` | the main web page and the BLE Sniff tab |
| 8 | `07_gui_classic_tab.md` | `classic.js` | the Classic tab, live metrics and charts |
| 9 | `09_install_deploy_bluez.md` | `deploy/*`, `desktop/*` | packaging, installing, the BlueZ install/removal tool |

## The 30-second version
```
Browser page (static/*.html .js .css)
   | HTTP requests + a live event stream
server.py  (root, port 5480)  - starts and watches helper processes, checks every request
   |-- bluforge_cli.py       BLE jobs (advertise / interfere / sniff-survey)  --> hci_raw.py --> radio, exclusive raw access
   '-- bluforge_classic.py   Classic + BlueZ work (pair, A2DP, HFP, AVRCP, interrogate) --> BlueZ over D-Bus --> radio
```
A radio is used in one of two modes at a time: raw (BLE jobs) or under BlueZ (Classic). That is why the Classic tab "reserves" a radio.

## What the guides found (consolidated by importance)
Each guide ends with a "Things I noticed" section. The most important items across all nine, in priority order:

### A. Security - fix before this goes near a customer
1. **No authentication; reachable by anyone on the network or by any web page.** `server.py` runs as root, listens on every address, accepts any content type and does no origin check. A hostile page open in a browser on a machine that can reach it, or any machine on the LAN, can start/stop jobs and call `POST /api/bluez/install` (a root package install). The "accept the licence" flag is only a value the caller sends. (guide 03)

### B. Bugs in the newer survey / throughput / BlueZ code (introduced in the recent work)
2. **Survey warning lines never reach the console correctly.** They are printed with spaces (`{"m": "warn"`), so the server's prefix test (`{"m":"`) misses them and they appear as raw JSON. (02, 03)
3. **CSV export rewrites negative numbers** (`-60` becomes `'-60`): the formula-injection guard should apply only to text fields. (06)
4. **Sort by name puts unnamed devices first.** (06)
5. **"Clear" compares the browser clock with the server clock**, so with different clocks devices can reappear or vanish wrongly. (06)
6. **Graph legend "(avg)" is a 300-sample window average, not a whole-run average.** The exact whole-run figure is in the result summary. (07)
7. **`bluforge_classic.py` imports `acl_meter` at the top of the file**, so a failure in the radio layer breaks even `preflight` with a raw traceback. It should be imported only inside the A2DP job. (04)
8. **"Interrogate never pairs" is unproven.** It reads only characteristics without encryption flags, but BlueZ may raise security and start pairing if a device answers a read with "authentication required". (04)

### C. Behaviour that does not match what the page or docs say
9. **Advertising interval is silently forced to at least 100 ms** (`hci_raw.py:532`) while capabilities say 20 ms is allowed and `adv-flood` defaults to 20 ms. (01, 02)
10. **`interfere adv-flood --map` really transmits** instead of doing a dry run. (02)
11. **`hci_raw.py` documentation overstates the code**: no `release()` method, and no extended-advertising or test-v1 methods (only unused constants). (01)
12. **Plan/PDU columns of the Interference and Advertise job tables are always blank** (they read a field the server never sends). (06)
13. **Connect-cycle saved results show "null s" duration; HFP runs show "Active 0.0 %" and an empty throughput chart.** (07)
14. **`saved_as` is set after the summary is written**, so the "saved:" line in the GUI never appears. (05, 07)
15. **`DEPLOYMENT_GUIDE.md` says `verify_core.py` needs three radios; it needs four.** (02)

### D. Reliability
16. **Stop pressed during start-up can strand a radio** (signal handlers are installed after the radio is claimed); "radio restored" prints even if the restore reported failure. (02)
17. **An exception inside an HFP job skips the summary and result files**; `hfp_ag.read_commands` hides real socket errors as "no data". (05)
18. **Memory grows over long runs**: finished-job records and sniff device lists are never pruned. (03)
19. **`stop_all` always replies OK**, even if a job did not stop; a failed capabilities check is cached forever. (03)
20. **BLE job arguments are not validated** the way Classic ones are; `adv -c 37,40` crashes after the radio is claimed; a bad advertising profile can raise an uncaught error. (01, 02, 03)

### E. Housekeeping
21. The desktop launcher's paths point to a folder that does not exist (`Desktop/EricssonBluForge`).
22. The old `ble-lab/` prototype's installer installs BlueZ automatically, which conflicts with the "end-user installs BlueZ" rule; it also contains old core-dump files.
23. `python3 hci_raw.py` with no argument tries `hci7`, which does not exist on a two-radio unit.
24. `verify_pdu_types.py` can pass on any nearby device's packets.

## How these guides were made
Seven read-only helper sessions each read one group of files line by line and wrote their guide; the author wrote 08 and 09. Findings marked in each guide as "unverified" were not run on hardware. A few headline findings (items 2, 3, 6, 9, 14, 7) were re-checked directly against the source.

## Status of the findings (updated 2026-10-27)
**Fixed:** item 1 (security: token login, localhost default, origin/host checks - see `../ACCESS_CONTROL.md`), items 2-5 (survey warning lines,
CSV negative numbers, sort-by-name, Clear clock bug), item 6 (graph legend now shows the whole-run average), item 7 (`acl_meter` is
imported only inside the A2DP job, with a fallback), item 8 (Interrogate now detects if the device made the system pair, reports it
and removes the pairing - tested with a fake BlueZ for five scenarios, not yet on real hardware).
**Fixed (2026-10-27, second batch):** items 9-15 (advertising interval floor, `adv-flood --map`, `hci_raw` docstring, blank job-table columns, cycle/HFP result display, `saved_as`, deployment guide radio count - details below).
**Fixed (2026-10-27, third batch):** items 16-24 - see "Third batch" below.
**Not fixed:** nothing from the numbered list; see "Left as is" at the end.

**Line numbers:** `03_server.md`, `06_gui_main_page.md` and `07_gui_classic_tab.md` cite line numbers from before these fixes. The server gained
about 150 lines (the access-control block and the `do_GET`/`do_POST` guards are new; the original handlers are now `_do_GET`/`_do_POST`),
and `app.js`, `classic.js`, `bluforge_classic.py` shifted by a few lines. Function names are unchanged.

### Second batch - what changed (items 9-15)
- **9** `hci_raw.legacy_adv_configure` no longer raises short intervals to 100 ms. 20 ms is the minimum (Core 5.x); below 20 ms is rejected with a clear message, and a controller that refuses a short interval for non-connectable/scannable types says so. Verified on the BE200: 20 ms goes out as 32 units, 50 ms as 80, and 10 ms is rejected before the radio is touched.
- **10** `interfere adv-flood --map` now prints the plan and transmits nothing (verified: no HCI advertising commands were sent).
- **11** The `hci_raw.py` module docstring now lists what is really implemented (Enhanced TX test or v4 when a power is set, enhanced RX test, test end, legacy advertising, legacy + extended scanning) and what is not (extended advertising, original v1 TX test), and says `close()`/`force_recover()` restore the radio (there is no `release()`).
- **12** `Job.status()` now sends `args` for BLE jobs; the Interference "Plan" and Advertising "PDU" columns show them.
- **13** Connect-cycle summaries now include `duration_s`; the results table treats null as "-"; HFP live samples show SCO voice rates (TX/RX kbit/s) and a relabelled chart, and "Active" shows "n/a (A2DP only)".
- **14** `Recorder.save` sets `saved_as` before the JSON is written and `verdict` saves before it emits the summary, so both the saved file and the live summary carry it (the GUI "saved:" line now appears).
- **15** `DEPLOYMENT_GUIDE.md` now says `verify_core.py` needs four controllers, auto-detected, and lists all four verify scripts.

### Third batch - what changed (items 16-24)
- **16** `bluforge_cli.py` has a `RadioGuard` used by `adv`, `interfere` and `sniff`: the stop handlers are installed *before* the radio is claimed, restoring is idempotent, a second signal during the restore is ignored, and the final line says "restored" only if the restore really succeeded (otherwise a warning with the exact recovery command, exit code 3). Tested: SIGINT sent at 0.15 s ... 1.4 s into start-up on the real radio, 15 of 15 runs ended with the radio UP and restored.
- **17** The HFP job turns any unexpected exception into a recorded failure (so the summary and result files are always written); `hfp_ag.read_commands` only treats a timeout as "no data" and raises `HfpError` on a real socket error; malformed AT commands answer ERROR instead of crashing. `verify_hfp_ag.py` still passes 10/10.
- **18** `Jobs` keeps at most 50 finished job records and only the newest sniff job per radio keeps its device table (capped at 6000 devices).
- **19** `stop_all` now replies 500 with the list of jobs that did not stop; a failed capabilities probe is no longer cached forever.
- **20** `server.build_argv` type- and range-checks every BLE parameter (adv, interfere, sniff) and refuses bad ones with a 400; the CLI rejects bad advertising channels, intervals and durations before claiming the radio, and `struct.error`/`TypeError` from bad beacon settings are reported cleanly.
- **21** The desktop launcher's `Exec`/`Icon` paths point at the real project folder; its name is "BluForge".
- **22** The older `ble-lab/` installer and its two packaging scripts no longer install or bundle BlueZ (they warn and say how the end-user installs it).
- **23** `python3 hci_raw.py` with no argument uses the first controller that exists.
- **24** `verify_pdu_types.py` reads the advertiser's own address and counts only reports of the right type from it. Run on the real radios: ADV_IND, ADV_SCAN_IND and ADV_NONCONN_IND each PASS with their own packets (event types 0/2/3, plus scan responses).
- Small items: `acl_links` matches eSCO case-insensitively, stale docstrings fixed, unused `quiet` argument and `Streamer.bytes_sent` removed.

### Left as is
- The eight old `btmon` core dumps in `ble-lab/` (about 600 KB each, from 2026-08-20) were deleted on 2026-10-27 at your request.
- Decisions that are yours, not bugs: licence-key check for the BlueZ install, whether `bluez-libs` may stay, applying `docs/lf_kinstall-bluforge-stage.patch`.
- Not exercised on hardware today: Interrogate against a real device (bench devices were off), a complete A2DP/HFP run, and the browser GUI.

## Fourth batch - data reliability (2026-10-27)
See `../DATA_INTEGRITY.md` for the guarantees, how they are enforced and what is not promised. In short: a 93-test hardware-free suite
(`tests/run_tests.sh`, shipped in the package) plus a two-radio loopback test (`tests/hw_loopback.py`); crash-safe writes for results and the
token (`src/durable.py`); versioned, checksummed, provenance-stamped result files (`src/version.py`); meter loss detection and honest labelling;
parsers that cannot raise on malformed events (the fuzzing found a real `IndexError` in the legacy report parser, now fixed);
`/api/health` plus a page banner (moved install folder, unwritable or full results disk, no radio); self-describing survey export.
Note: the project folder was renamed to `Blucode` on 2026-10-27; line-number references in the guides still describe the same files.

## Version 1.1.0 (2026-11-19) - cluster mode and no login
Two changes since these guides were written; the guides were not rewritten, so read them with these notes:
- **The login is gone.** Item 1 above ("token login") describes the 2026-10-27 fix; on 2026-11-19 the token, cookie, login page and
  rate limit were removed at the owner's request, and `03_server.md` (the access-control block, `Auth`, `LOGIN_PAGE`, `--token-file`) no
  longer matches `server.py`. The Host check and the cross-site / JSON-only checks remain. Current text: `../ACCESS_CONTROL.md`.
- **New file `src/cluster.py`** (not covered by a line-by-line guide): a BluForge on a LANforge manager lists and controls the radios of
  the clustered resources that run BluForge. `server.py` calls it from the status / caps / classic-preflight / results / sniff routes,
  the event stream and the start / stop / action routes, and `static/app.js`, `classic.js`, `index.html` and `style.css` show the units
  and unit-qualified radio names. Overview: `../CLUSTER.md`. Tests: `tests/test_cluster.py` (no hardware), `tests/hw_cluster.py` (loopback + real radios).
- Line numbers cited for `server.py`, `app.js`, `classic.js` and `index.html` have moved again; search by function name.
