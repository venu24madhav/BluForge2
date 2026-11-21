# Data reliability and consistency in BluForge

What the product guarantees about its data, how that is enforced, how to check it, and what it cannot promise.

## Guarantees and how they are enforced

| Guarantee | How |
|---|---|
| **A saved result file is never half-written.** | Results and CSVs are written to a temporary file, flushed to disk (`fsync`), then renamed over the target in one atomic step (`src/durable.py`). A crash, kill or full disk leaves either the complete old file or the complete new one; leftover temp files are swept and never listed as results. |
| **A result file says what it is.** | Every `logs/classic/*.json` carries `schema` (layout version), `tool.version`, `started_at`/`finished_at` (UTC), host and kernel. |
| **A CSV can be checked against its summary.** | The JSON records the CSV's `csv_sha256` and `csv_rows`; the JSON is written last, so its existence means the CSV is complete. Verify: `sha256sum x.csv` equals `summary.csv_sha256`. |
| **Throughput is counted, not estimated.** | The A2DP meter counts every ACL packet on the kernel's HCI monitor channel and reconciles sends with the controller's completions (`src/acl_meter.py`). Cross-checked against the kernel's own packet counters (exact match). |
| **Link delay and jitter are measured, with their own trust flag.** | Each audio packet is timed from the kernel's nanosecond timestamps of the same HCI monitor frames (`src/acl_meter.py`, `src/delay_stats.py`); the result says which clock was used and sets `valid: false` with the reason when kernel timestamps are missing, monitor frames were dropped or a completion gives an impossible delay. A delay limit that cannot be checked fails the run instead of passing. With an internal receiver the figure is checked against the receiving host's own timestamp of the same packet (`loopback.completion_minus_arrival_ms`). What it is and is not: `A2DP_DELAY.md`. |
| **Loss in the meter is detected and labelled.** | The kernel does not report dropped monitor frames, so the meter checks invariants (completions that match no send, an impossible send backlog, truncated frames) and uses a 16 MB receive buffer. A run that fails them says `INCOMPLETE - see acl_integrity` in `throughput_src` instead of presenting the number as exact. |
| **Survey records are consistent.** | One record per device; counters reconcile (sum of per-device counts = total reports); the browser rebuilds its table from the server on every reconnect, so nothing is lost or double counted after a dropped connection. |
| **A malformed radio event cannot end a job.** | Parsers return nothing for a short or corrupt event instead of raising; skipped events are counted. Fuzzed with 100,000+ random and hostile inputs in the test suite. |
| **A radio is always handed back, and the tool says honestly whether it was.** | `RadioGuard`: stop handlers are installed before the radio is claimed, restoring happens once, and "restored" is printed only when the restore succeeded (otherwise a warning with the recovery command and exit code 3). |
| **Bad input is refused before it can do harm.** | Every parameter is type- and range-checked by the server and again by the command line, before any radio is touched. |
| **Silent environmental failures are surfaced.** | `/api/health` and a red banner on the page report: installation folder moved/removed, results folder not writable (really tested by writing a file), low disk space, no radio visible, BlueZ missing. |
| **Exports describe themselves.** | The survey JSON export carries schema, tool version, time, unit and units of each field. |
| **Cross-site requests are refused.** | Origin / Fetch-Metadata / JSON-only checks and a Host check; there is no login (see `ACCESS_CONTROL.md`). |

## How to check it yourself
- **Automatic suite (no hardware, about 10 s):** `cd tests && ./run_tests.sh` - 93 tests: decoder vectors and fuzzing, survey aggregation, radio-event parsers, the meter, HFP protocol, server access control and validation, crash-safe writes, result integrity, health.
- **Hardware loopback (two radios):** `sudo python3 tests/hw_loopback.py` - one radio advertises a known iBeacon, the other surveys it; the decoded UUID/major/minor must match, and both radios must come back up.
- **Result file check:** `sha256sum logs/classic/<name>.csv` against `summary.csv_sha256` in the matching `.json`.

## What it cannot promise
- **Throughput is ACL-level.** Over-the-air retransmissions and baseband overhead are invisible to the host; the figure is what the host handed to and received from the controller.
- **Link delay is not audio latency.** It is the time one audio packet needs to cross the link as the host sees it; the host's audio buffer and the sink's own buffer and decoder are not in it, and a real sink's dropouts are not observable.
- **RSSI on Classic links** is the controller's offset from its "golden receive range", not dBm.
- **A survey lists what devices advertise**, and a device may support more than it advertises; random (privacy) addresses change, so one physical device can appear as several rows over time.
- **No automatic deletion.** Result files accumulate in `logs/classic/`; the health banner warns when disk space is low, but nothing is deleted for you.
- **Plain HTTP, no login.** Data is not encrypted in transit and anyone who can reach the port can use the page; use a trusted network, a firewall rule, an SSH tunnel or a TLS front end.
- **Not run on hardware in every combination.** The loopback test covers the BLE advertise/survey path; Classic audio, HFP and Interrogate against real third-party devices are exercised by hand (see `code-guide/README.md`).
