# Code guide 03: `src/server.py` (the BluForge server)

Audience: a beginner who is lost. Every line reference like `server.py:162` points at the file as it was
when this guide was written (918 lines). Everything below was read from the code; where something is
unclear or I could not check it, I say so.

---

## 1. What this file is, in one page

BluForge has three layers:

```
 Browser (src/static/index.html, app.js, classic.js, style.css)
     |   ^
     |   |  1) normal requests: "start this", "stop that", "give me status"   (HTTP + JSON)
     v   |  2) one long-lived "live feed" connection (/api/stream)            (SSE)
 +--------------------------------------------------------------------------------+
 |  src/server.py   <-- THIS FILE. Runs as root, listens on port 5480.            |
 |  - serves the web page files                                                   |
 |  - answers JSON requests                                                       |
 |  - starts helper programs, watches their output, relays it to the browser      |
 +--------------------------------------------------------------------------------+
     |  starts as separate programs ("child processes")
     +--> src/bluforge_cli.py      BLE jobs: adv / interfere / sniff  (talks raw HCI to the radio)
     +--> src/bluforge_classic.py  Bluetooth Classic + BlueZ work     (talks to BlueZ over D-Bus)
                       |
                       v
               two Bluetooth radios: hci0, hci1
```

The server is a "control plane": it decides *what* runs and *when*, and tells the browser what is going
on. It does **not** touch a Bluetooth radio itself (docstring, `server.py:15-18`). The only hardware-ish
thing it does is list which radios exist (`all_hci()`, which reads `/sys/class/bluetooth`).

Key design facts (all explained in detail below):

* **Pure Python standard library** (`http.server` + `threading`): nothing to `pip install`
  (`server.py:20-22`).
* **One job per radio.** A "job" is one child process (for example a BLE sniff on `hci1`). The server
  refuses to start a second job on a radio that is busy (`server.py:183-184`).
* **Radios are either "BLE" or "reserved for Classic".** Reserved radios run Bluetooth Classic work only;
  unreserved radios run BLE work only (`server.py:179-182`).
* **Live output** from each child process is read line by line by a "reader thread" and broadcast to every
  open browser tab through Server-Sent Events (SSE).
* **Stopping is polite**: SIGINT first, SIGTERM second, and never SIGKILL from the job-stop path
  (`server.py:239-276`). There is one exception to "never SIGKILL" that I noticed, see section 8.
* **All state lives in memory** (jobs, reservations, backlog, capabilities cache). Restarting the server
  forgets everything except the result files the Classic helper wrote to disk.
* **There is no login of any kind.** See section 8.

Who starts this file: `desktop/start-and-open.sh` (a desktop icon that runs it as root in a terminal
window), `deploy/ericsson-bluforge.service` (a systemd unit, `User=root`, only passes `--port`), or you,
by hand: `sudo python3 src/server.py --port 5480`.

---

## 2. Jargon, explained once

| Word | Plain meaning |
|---|---|
| **Process / subprocess / child process** | A running program. `subprocess.Popen(...)` is Python's way to start another program from inside this one; the new one is the "child". The server starts the two helper CLIs as children. |
| **argv** | The list of words that make up a command line, for example `["python3", "-u", "bluforge_cli.py", "--dev", "1", "sniff"]`. Passing a *list* (not one big string) to `subprocess` means no shell is involved, so a value like `; rm -rf /` is just an odd-looking argument, not a command. This is why "shell injection" is not a risk here. |
| **stdout / stderr / pipe** | The two text channels a program prints to. A *pipe* connects a child's output to the parent so the parent can read it. The server merges both channels into one pipe (`stderr=subprocess.STDOUT`, `server.py:191`). |
| **Thread** | A second line of execution inside the same program, running "at the same time". The server uses one thread per web request, one per running job (the reader thread), and a few more. |
| **Daemon thread** | A thread that does not keep the program alive: when the main program ends, daemon threads are cut off. |
| **Lock (mutex)** | A "one at a time" token. Code that changes shared data first does `with lock:`; any other thread wanting the same lock waits. Prevents two threads corrupting the same list or dict. |
| **Queue** | A thread-safe waiting line. One thread puts items in, another takes them out. Here, each browser tab gets its own queue. |
| **Publish / subscribe (pub/sub)** | A pattern: producers call `publish(event)`; every consumer that previously called `subscribe()` gets a copy. The producer does not know or care who is listening. |
| **SSE (Server-Sent Events)** | A one-way live feed from server to browser. The browser opens a normal HTTP request that the server *never finishes*; whenever something happens the server writes `data: {...}` followed by a blank line. The browser's built-in `EventSource` object turns each into a JavaScript event, and automatically reconnects if the connection drops. |
| **HTTP, route, GET, POST** | The web protocol. A *route* is a URL path such as `/api/status`. GET asks for data; POST sends data (here, a JSON body) to make something happen. |
| **JSON** | Text format for data, such as `{"dev": 1, "tool": "sniff"}`. |
| **Status codes used here** | 200 OK, 400 bad request (your input is wrong), 404 not found, 409 conflict (valid request but the current state forbids it), 422 (understood, but the action itself failed), 500 server problem, 504 timed out. |
| **Signal** | A message the operating system sends to a process. **SIGINT** is what Ctrl-C sends ("please stop"; programs may clean up first). **SIGTERM** is the polite "terminate" request. **SIGHUP** arrives when the terminal window the program runs in is closed. **SIGKILL** cannot be caught or cleaned up after; the process is just removed. |
| **Process group / session** | A bundle of related processes. `start_new_session=True` puts a child in its own bundle, so a signal aimed at the bundle reaches the child and its own children but not the server, and the terminal's Ctrl-C or window-close does not reach it either. |
| **ANSI escape codes** | Invisible colour codes (like `\x1b[32m` for green) that command-line programs print. The server strips them (`ANSI`, `server.py:51`) because the browser would show them as junk. |
| **Regex** | A pattern for checking text. `ADDR_RE` checks that a string looks like `AA:BB:CC:DD:EE:FF`. |
| **HCI / raw HCI** | The low-level command language of a Bluetooth controller (radio chip). `hci0`, `hci1` are the Linux names of the two radios. "Raw HCI" means a program talks to the chip directly, with the operating system's normal Bluetooth service stepping aside. |
| **BlueZ** | The standard Linux Bluetooth software: the background service `bluetoothd` plus tools like `bluetoothctl`. |
| **D-Bus** | Linux's internal "message bus" that programs use to talk to each other. `bluetoothd` publishes its features (pair, connect, scan...) on D-Bus. `bluforge_classic.py` talks D-Bus through GLib/Gio (`bluforge_classic.py:47,64-65`). `server.py` never uses D-Bus itself. |
| **BLE vs Classic** | BLE (Bluetooth Low Energy) is the low-power Bluetooth used by beacons and sensors; the tool jobs `adv`, `interfere`, `sniff` are BLE. Classic is the older Bluetooth used for audio (A2DP = music streaming, AVRCP = play/pause/volume remote control, HFP = hands-free phone calls). |
| **Survey** | The sniff mode where the helper aggregates every advertisement per device and prints one summary line per changed device per second (section 4.5). |

---

## 3. File layout at a glance (in order)

| Lines | What |
|---|---|
| 1-25 | Module docstring |
| 26-41 | Imports |
| 43-51 | Constants |
| 53-59 | `all_hci()` |
| 62-304 | Job manager: `class Job`, `class Jobs` |
| 306 | `JOBS = Jobs()` (the one global instance) |
| 309-362 | BlueZ presence check, gate, install job |
| 364-425 | Argv builders for BLE (`_opt`, `_flag`, `build_argv`) |
| 428-653 | Bluetooth Classic helpers (validators, `build_classic_argv`, `run_classic_action`, preflight, results) |
| 656-669 | Capabilities cache `get_caps` |
| 672-853 | `class Handler` (the HTTP part, including SSE) |
| 856-914 | `main()` and the shutdown logic |
| 917-918 | `if __name__ == "__main__": main()` |

---

## 4. Walkthrough, in file order

### 4.1 Module docstring: `server.py:2-25`

Describes the original intent: a thin control plane over `bluforge_cli.py`, one managed child process per
Bluetooth controller, output streamed over SSE, stop = SIGINT then SIGTERM (never SIGKILL, because an
abrupt kill once made the kernel's "HCI_Reset on close" time out; the docstring points at
`docs/INSTALL_LOG.md`, 2026-10-05).

Out-of-date parts (the code has grown since): it says "hci0-7" and "8 independent BE200 controllers"
(the code now detects radios at run time, see `all_hci`), it only mentions `adv`/`interfere`/`sniff`
(Classic and BlueZ handling were added later), and it says it serves "gui.html from the same directory"
(it actually serves `static/index.html`, see `_send_static`, `server.py:701`).

### 4.2 Imports: `server.py:26-41`

Standard library only, plus one local import: `from hci_raw import detect_hci_indices` (`server.py:41`),
which lists the radios by reading `/sys/class/bluetooth`.
`urlparse` and `parse_qs` (line 39) are imported but never used.

### 4.3 Constants: `server.py:43-51`

| Name | Line | Value / meaning |
|---|---|---|
| `HERE` | 43 | Folder that contains `server.py` (the `src` folder), as an absolute path. Everything else is located relative to it. |
| `CLI` | 44 | Path to `bluforge_cli.py` (BLE helper). |
| `CLASSIC` | 45 | Path to `bluforge_classic.py` (Classic/BlueZ helper). |
| `RESULTS_DIR` | 46 | Where Classic test results are saved: `$BLUFORGE_LOGS/classic`, or, if that environment variable is unset, `<project>/logs/classic`. The Classic helper computes its own log folder the same way (`bluforge_classic.py:56`), which is why the two agree. The server only *reads* this folder. |
| `BLE_KINDS` | 47 | `("adv", "interfere", "sniff")`: the three BLE job types. |
| `CLASSIC_JOB_KINDS` | 48 | `("c_a2dp", "c_avrcp_watch", "c_cycle", "c_hfp")`: the four long-running Classic job types. The `c_` prefix is the server's own naming; the helper's sub-commands do not have it (`a2dp`, `avrcp-watch`, `hfp`, `cycle`). |
| `ADDR_RE` | 49 | Regex for a Bluetooth address `AA:BB:CC:DD:EE:FF` (six pairs of hex digits, colon separated). |
| `STATIC_DIR` | 50 | `<src>/static`, where the web page files live. |
| `ANSI` | 51 | Regex that matches colour escape sequences (`ESC [ digits/semicolons m`). Only colour codes are matched, not other terminal escapes. |

### 4.4 `all_hci()`: `server.py:53-59`

* **What:** returns the list of radios present on this machine, such as `[0, 1]`.
* **How:** calls `detect_hci_indices()` from `hci_raw.py`, which lists `/sys/class/bluetooth`, keeps names
  that look like `hciN`, and returns the sorted integers (`hci_raw.py:72-91`). Returns `[]` if the folder
  does not exist.
* **Why:** an earlier version hard-coded "8 radios" and offered radios that did not exist on a 4-radio
  unit (docstring, `server.py:54-58`). It is called *fresh every time* (cheap), so hot-plugged radios show up.
* **Used by:** `Jobs.available_hci`, `reserve`, `start`, `snapshot`, `run_classic_action`,
  `classic_preflight`.

### 4.5 The job manager: `class Job` and `class Jobs`

Terminology used throughout: a **job** is one child process started by the server on one radio. Its `kind`
is one of the `BLE_KINDS` or `CLASSIC_JOB_KINDS`; its `dev` is the radio's number (1 means `hci1`).

#### `class Job`: `server.py:63-84`

A small record about one job. It does no work itself.

**`Job.__init__(job_id, kind, dev, argv)`, `server.py:64-74`** stores:

| Field | Meaning |
|---|---|
| `id` | `"job1"`, `"job2"`, ... (the counter never repeats while the server runs). |
| `kind`, `dev`, `argv` | What it is, which radio, and the exact command line used. |
| `proc` | The `subprocess.Popen` object (filled in by `Jobs.start`). |
| `started` | Unix time when created. |
| `stopped`, `exit_code` | Filled in by the reader thread when the process ends; `None` until then. |
| `devices` | Only for sniff jobs: a dict `address -> latest record of that device`. |
| `stats` | Only for sniff jobs: the latest `{"m":"stats",...}` object. |

**`Job.status()`, `server.py:76-84`** returns a JSON-friendly summary: `id, kind, dev, hci` (like
`"hci1"`), `running`, `started`, `stopped`, `uptime`, `exit_code`.

* `running` is computed live as "process object exists and `proc.poll()` is `None`" (`poll()` asks the OS
  "has it exited yet?"). It is *not* taken from the `stopped` field.
* `uptime` is `None` once `stopped` is set.
* Gotcha: between the moment the process dies and the moment the reader thread records `stopped`
  (a few milliseconds), `running` is already `False` while `stopped` is still `None`. Harmless.

#### `class Jobs`: `server.py:87-303`

The one object (`JOBS`, line 306) that owns all jobs, all reservations and the live-event broadcasting.

**`Jobs.__init__`, `server.py:91-103`** creates:

| Attribute | Purpose |
|---|---|
| `lock` | A lock that guards `jobs`, `by_dev`, `reserved`, `dev_locks`, `next_id`. |
| `jobs` | `job_id -> Job`, every job since the server started (finished ones too; nothing ever removes them, see section 8). |
| `by_dev` | `dev -> job_id`, only the job that is *currently running* on each radio. This dict is what enforces "one job per radio". |
| `next_id` | Counter for naming jobs. |
| `subs`, `subs_lock` | The list of subscriber queues (one per open browser live-feed) and its lock. |
| `backlog` | The last up-to-500 console lines, replayed to new subscribers. |
| `reserved` | Set of radio numbers currently handed to BlueZ for Classic work. |
| `dev_locks` | `dev -> Lock`, one lock per radio, used to run only one Classic one-shot action at a time per radio. |

The code never takes `lock` and `subs_lock` at the same time (it releases one before calling into the
other), so there is no lock-ordering deadlock to worry about. `Jobs.lock` is a plain `Lock`, not
re-entrant, which is why `snapshot()` calls `available_hci()` only *after* leaving its own `with` block
(`server.py:296-303`).

##### Publish / subscribe and the SSE stream

This is the heart of the "live console". Three methods plus `Handler._stream` (section 4.13).

**`Jobs.subscribe()`, `server.py:131-136`**
1. Creates a new `queue.Queue(maxsize=2000)`, a waiting line that holds at most 2000 events.
2. Under `subs_lock`, adds it to `self.subs` and takes a copy of the current `backlog`.
3. Returns `(queue, backlog_copy)`.

Because the registration and the backlog copy happen under the same lock that `publish` uses, a new
listener neither misses nor double-receives an event around that moment.

**`Jobs.unsubscribe(q)`, `server.py:138-141`** removes the queue if still present (safe to call twice).

**`Jobs.publish(evt)`, `server.py:105-129`**: broadcasts one event (a dict) to everybody.
1. Takes `subs_lock`.
2. **Backlog rule** (lines 107-110): only events with `type == "line"` (console text) are remembered, and
   only if they do not carry `nobacklog`. The backlog is trimmed to the last 500. Status events, sniff
   summaries and BlueZ-install events are *never* backlogged.
3. For each subscriber queue, `put_nowait(evt)` (add without waiting).
4. **Overflow handling** (lines 114-127): if a queue is full (a slow or stuck browser, more than 2000
   unread events), the code does not silently drop events. It empties that queue, puts one
   `{"type": "overflow"}` marker in, and removes the queue from the subscriber list:

```python
                    try:
                        while True:
                            q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait({"type": "overflow"})
                    except queue.Full:
                        pass
                    self.subs.remove(q)
```

   The stream thread of that client sees the marker and ends the HTTP response (`server.py:847-848`);
   the browser's `EventSource` reconnects by itself and receives a fresh `hello` + backlog, so it
   "resyncs". Comment at lines 115-117 gives the reason: a silently dropped client would look connected
   but stop updating.
5. Any other exception on a queue also removes it (line 128-129; defensive, `put_nowait` normally only
   raises `Full`).

* **Who calls `publish`:** the reader thread (every console line, sniff summary, "finished" status),
  `Jobs.start` ("started"), the `/api/classic/reserve` route ("reservation"), `run_classic_action`
  (log lines of one-shot actions), `bluez_install_job` (install progress).
* **Event shapes** (what the browser receives, one JSON object per SSE message):

| `type` | Extra fields | Sent when | In backlog? |
|---|---|---|---|
| `status` | `event: "started"`/`"finished"`, `job`, `ts` | a job starts or ends (`server.py:197, 237`) | no |
| `status` | `event: "reservation"`, `ts` | after any reserve/release request (`server.py:805`) | no |
| `status` | `event: "hello"`, `status: <snapshot>` | first message to a newly connected client only; written directly, not via `publish` (`server.py:837`) | n/a |
| `line` | `job_id, dev, text, nobacklog` | one console line of a job (`server.py:228`) | yes, except lines starting `{"m":"sample"` |
| `line` | `job_id:"action", kind:"c_action", dev, text` | progress lines of a one-shot Classic action (`server.py:609`) | yes |
| `sniff` | `job_id, dev, devices[], stats, now` | once per second per sniff job (`server.py:219`) | no |
| `bluez` | `text` or `done, ok` | BlueZ install progress (`server.py:355,359,362`) | no |
| `overflow` | none | internal only; makes `_stream` close; never reaches the browser | no |

* **Gotchas:** the backlog is one shared list for *all* jobs, so a chatty job can push everyone else's
  history out of the 500. It is trimmed by re-slicing (`self.backlog[-500:]`), which copies the list each
  time it passes 500: wasteful but harmless at these volumes. There is no SSE `id:` field, so a
  reconnecting browser cannot say "I already have up to X"; the server just replays the whole backlog.
  I did not find anything in `app.js`'s `connectStream` that clears the consoles or removes duplicates on
  reconnect, so repeated lines after a reconnect are possible (not tested).

##### Radio reservation (Classic vs BLE)

Why this exists: BLE raw-HCI jobs borrow a radio away from BlueZ (the unit file comments say the project
"uses HCI_CHANNEL_USER to borrow one controller at a time, returning it to bluetoothd automatically").
Classic work needs BlueZ to own the radio. So a radio is in one of two modes, and the server enforces the
split. **Important:** "reserve" in the server is *only a flag in a Python set*; it does not itself
power anything on or off. The actual radio handling is done by the helper CLIs.

**`Jobs.available_hci()`, `server.py:143-146`**: radios that are present, have no running job, and are not
reserved: the ones a BLE job could start on. Sent to the browser in every status snapshot.

**`Jobs.reserve(dev)`, `server.py:148-156`**: returns `(ok, message)`.
1. Radio must exist (`all_hci()`), else `False, "hciN is not a controller present on this unit"`.
2. Under the lock: if a *BLE* job is running there, refuse (`"hciN is running BLE job ... stop it first"`).
3. Otherwise add to `reserved`.

**`Jobs.release(dev)`, `server.py:158-164`**: if a *Classic* job is running there, refuse; otherwise remove
from `reserved`. Does not check that the radio exists (harmless).

**`Jobs.dev_lock(dev)`, `server.py:166-168`**: returns the per-radio `Lock`, creating it on first use
(`setdefault`). Used by `run_classic_action` so that two pair/connect/scan operations on the same radio
cannot run at once.

**`Jobs.classic_job_on(dev)`, `server.py:170-173`**: the id of the running Classic job on that radio, or
`None`.

Reservations are in memory only; a server restart forgets them. (The GUI re-sends a reserve before its
Classic scan/interrogate calls: `app.js:613, 712`.)

##### `Jobs.start(kind, dev, argv)`: `server.py:175-199`

Starts a job. Returns `(ok, message, job_id)`.

1. `dev` must be a present radio, else `(False, "hciN is not a controller present on this unit", None)`.
2. Under `self.lock`, three rules in order:
   * a BLE `kind` on a *reserved* radio: refused ("release it on the Classic tab first");
   * a Classic `kind` on a radio that is *not* reserved: refused ("reserve it on the Classic tab first");
   * any job already on that radio (`by_dev`): refused ("already running job jobN").
3. Allocates `job_id = "job%d" % next_id` and creates the `Job`.
4. Copies the environment and adds `PYTHONUNBUFFERED=1` (so the child's `print` output arrives line by line
   instead of in big chunks when piped).
5. Starts the child (`server.py:190-192`):

```python
job.proc = subprocess.Popen(
    argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, bufsize=1, start_new_session=True, env=env)
```

   `text=True` decodes bytes into text; `bufsize=1` means line-buffered; `start_new_session=True` gives the
   child its own session and process group (explained in section 2). If starting fails, returns
   `(False, "failed to start: ...", None)` (the job id counter has already moved on; harmless).
6. Registers the job in `jobs` and `by_dev` (still under the lock).
7. After releasing the lock: publishes the `started` status event and starts a **daemon reader thread**
   running `self._reader(job)` (`server.py:197-198`).
8. Returns `(True, "started", job_id)`.

* **Gotcha:** the child is a different session, so pressing Ctrl-C in the server's terminal does not reach
  it; the server must stop jobs itself (see `main()`). If the server is killed with SIGKILL, children keep
  running as orphans with no way to stop them from the GUI.
* **Gotcha:** the lock is held while `Popen` runs (process creation takes a few milliseconds); other callers
  wait that long.

##### `Jobs._reader(job)`: `server.py:201-237` (the reader thread)

One of these threads runs per job. Its life:

1. `for line in job.proc.stdout:` blocks until the child prints a line, then gives it to the loop. The loop
   ends when the child closes its output (normally when it exits).
2. For each line: remove the newline and the colour codes: `text = ANSI.sub("", line.rstrip("\n"))` (205).
3. **Survey aggregation (sniff jobs only)**, lines 206-225. A sniff job started with `--survey` prints
   two kinds of JSON lines instead of one line per radio packet (the format is produced by
   `ble_survey.py:117,138`):
   * `{"m":"dev", ...}`: the current full record of one device that changed during the last second.
   * `{"m":"stats", ...}`: counters (uptime `t`, number of `devices`, `reports`, `rate`, `evicted`,
     `idle_s`), printed *after* that second's device lines.

   The server's logic (a line is only treated this way if the job kind is `sniff` and it begins with
   exactly `{"m":"`, line 206):
   * parse it with `json.loads`; if the text is not valid JSON, treat as not-an-object (`obj = None`);
   * `m == "dev"`: add the object to a local list `pending` and also store it in `job.devices[addr]`
     (so `job.devices` always holds the latest record per device); then `continue` (no console line is
     produced, lines 213-216);
   * `m == "stats"`: save it as `job.stats`, then publish **one** SSE event
     `{"type":"sniff","job_id","dev","devices": pending,"stats": obj,"now": time.time()}`, reset
     `pending`, `continue` (lines 217-223);
   * `m == "warn"`: rewrite the text as `"[!] <msg>"` and fall through to be published as a normal console
     line (224-225);
   * anything else (including invalid JSON that happens to start with `{"m":"`) falls through and is
     published as a plain console line.

   Result: instead of thousands of per-packet events there is about **one SSE event per second per sniff
   job**, containing only the devices that changed. The browser upserts them into a table.
   `"now"` is the server's clock, so the browser can compute "last seen N s ago" without trusting its own
   clock (`app.js: snIngest`).
4. **Everything else** is published as a console line (lines 226-229). Classic jobs print a 1 Hz metrics
   line starting `{"m":"sample"`; those get `nobacklog: True` so they do not flush real history out of
   the 500-line backlog within minutes (the results are saved to disk anyway, comment at 226-227).
5. `except Exception: pass` (230-231) swallows *any* error in the loop above, which ends the loop. See the
   gotcha below.
6. After the loop: `job.exit_code = job.proc.wait()` (waits for the child to really exit and reaps it),
   `job.stopped = time.time()`, then under the lock removes `by_dev[dev]` if it still points at this job
   (234-236), then publishes the `finished` status event (237).

* **Gotchas:**
  * *Unread pipe risk.* If something inside the loop raises (for example a `dev` JSON object without an
    `addr` key would raise `KeyError` at line 215; undecodable bytes could raise `UnicodeDecodeError` while
    iterating), the error is swallowed, reading stops, and the code goes to `proc.wait()`. A child that
    keeps printing could then block forever on a full pipe, and the job would look hung. I have not seen
    this happen; it is a latent risk of the broad `except`.
  * *Tiny race after stop.* `Jobs.stop` reports "stopped" as soon as the process exits, but `by_dev` is
    cleared by this thread a moment later. A new job started on the same radio in that gap would be
    refused with "already running job". The GUI refreshes, so it is rarely noticed.

##### `Jobs.stop(job_id, timeout=5.0)`: `server.py:239-276`

Returns `(ok, message)`. Step by step:

1. Look up the job (under the lock). Unknown id: `(False, "no such job 'x'")`.
2. If the process already exited (`proc.poll()` is not `None`): `(True, "already stopped")`.
3. **SIGINT** to the job's whole process group (lines 246-252):

```python
        try:
            os.killpg(os.getpgid(job.proc.pid), signal.SIGINT)
        except Exception:
            try:
                job.proc.send_signal(signal.SIGINT)
```

   (`killpg` = signal a whole group; falls back to signalling just the process.) The helper CLIs install
   their own SIGINT/SIGTERM handler that flushes, restores the radio and exits
   (`signal.signal(signal.SIGINT, cleanup)` at `bluforge_cli.py:153, 322, 451`, one per tool;
   `bluforge_classic.py:1949-1950` for Classic), which is why SIGINT is the polite first choice.
4. Poll every 0.1 s for up to `timeout` (5 s) for the process to exit.
5. Still alive: **SIGTERM** to the group (fallback `proc.terminate()`), then poll up to 3 more seconds
   (lines 258-273). If it exits: `(True, "stopped")`.
6. Still alive after both: `(False, "hciN job did not stop after SIGINT+SIGTERM -- it is likely wedged. Manual
   recovery may be needed...")` (274-275). **No SIGKILL.** The comment (263-268) explains this replaced an
   older version that claimed success right after sending SIGTERM without checking.

* **Gotchas:** it blocks the calling thread for up to 8 s (the HTTP request thread waits). It does not
  update `by_dev` itself (the reader thread does).

##### `Jobs.stop_all()`: `server.py:278-282`

Takes the ids of *every* job ever created (finished ones just answer "already stopped") and calls
`stop(jid)` for each, **one after another**. The return values are thrown away, so the caller cannot tell
whether any job failed to stop. Used by `POST /api/stop_all` and by the shutdown handlers.

##### `Jobs.sniff_snapshot()`: `server.py:284-294`

For each radio, picks the **most recent sniff job** (running or finished) and returns a list of
`{job_id, dev, running, stats, devices: [all records]}`. Purpose (docstring): a page load or reconnect can
rebuild its device table without replaying packets. This is what `GET /api/sniff/devices` returns.

* **Gotcha:** `devices` here are all records the server has *ever* stored for that job, not only the ones the
  helper currently holds (the helper evicts the stalest device beyond 5000: `ble_survey.py:12,53`; the server
  never evicts). `j.devices.values()` is read while the reader thread may be adding to it, without a lock;
  that works in CPython (the standard Python) because of its global interpreter lock, but it is not a
  documented guarantee.

##### `Jobs.snapshot()`: `server.py:296-303`

The "state of the world" dictionary behind `GET /api/status` and the SSE `hello` message:

| Key | Meaning |
|---|---|
| `running` | `status()` of every job whose process is alive |
| `finished` | `status()` of finished jobs, **only the last 20** (`finished[-20:]`) |
| `available_hci` | radios free for BLE |
| `all_hci` | all radios present |
| `classic_reserved` | sorted list of reserved radios |
| `hostname` | `socket.gethostname()` |
| `bluez` | `bluez_info()` (section 4.7) |

### 4.6 `JOBS = Jobs()`: `server.py:306`

The single global instance every route handler uses (created once when the module loads). All the state in
section 7 hangs off it.

### 4.7 BlueZ presence check, gate and install job: `server.py:309-362`

**Background.** BluForge units are sold *without* BlueZ; the end user installs it themselves
(`server.py:310`, `docs/BLUEZ_INSTALL.md`). Until BlueZ is installed the server refuses Bluetooth actions
and the page shows a banner with an "Install BlueZ" button. BlueZ and `bluetoothctl` are explained in
section 2.

| Item | Line | What |
|---|---|---|
| `BLUEZ_NOTICE` | 311-314 | The licence/third-party notice text shown to the user before installing. It says BlueZ is GPL/LGPL, not supplied by Candela Technologies, and is downloaded from the OS vendor's repository. |
| `BLUEZ_JOB` | 315 | A global dict `{"running": False, "log": [], "rc": None}`: state of the (single) install job: is it running, last up to 200 output lines, exit code. |
| `BLUEZ_LOCK` | 316 | A lock that makes "check `running`, then set it" one atomic step in the install route. |

**`bluez_installed()`, `server.py:319-322`.** True only if *both*: (a) a `bluetoothd` executable exists in
one of `/usr/libexec/bluetooth/bluetoothd`, `/usr/lib/bluetooth/bluetoothd`, `/usr/sbin/bluetoothd`
(`os.access(p, os.X_OK)`); and (b) `bluetoothctl` is found on the PATH (`shutil.which`). It checks files,
not whether the service is running. It does real file-system lookups each time it is called, which is
cheap. Same logic as `find_bluetoothd`/`bluez_present` in `deploy/bluforge-bluez.sh`.

**`bluez_script()`, `server.py:325-332`.** Finds the installer script, trying in order
`/usr/local/sbin/bluforge-bluez`, `<src>/../deploy/bluforge-bluez.sh`, `<src>/bluforge-bluez.sh`; returns
the absolute path of the first one that is a file with an execute bit, else `None`.

**`bluez_info()`, `server.py:335-337`.** `{"installed": ..., "installing": BLUEZ_JOB["running"],
"can_install": script found, "notice": BLUEZ_NOTICE}`. It is embedded in every `Jobs.snapshot()`, so the page
learns about BlueZ from the normal 5-second status refresh.

**`bluez_gate()`, `server.py:340-344`.** Returns `None` if BlueZ is installed, otherwise the error text
"BlueZ is not installed. Install it from the banner at the top of this page (or run: sudo bluforge-bluez
install)."

*Where the gate is applied:* only in `do_POST`, for exactly three routes: `/api/start`,
`/api/classic/action`, `/api/classic/reserve` (`server.py:765-768`). A blocked request gets HTTP 409 with
`{"ok": False, "error": <text>, "bluez_missing": True}`. Note `/api/start` is gated for **BLE jobs too**:
the code treats "BlueZ present" as a precondition for the whole product. I did not check whether BLE raw-HCI
jobs truly need BlueZ. Not gated: stopping jobs, `/api/caps`, `/api/classic/preflight`, all other GETs.

**`bluez_install_job()`, `server.py:347-362`.** Runs on its own daemon thread (started by
`POST /api/bluez/install`). Step by step:

1. `script = bluez_script()`.
2. Starts `[script, "install", "--accept"]` with output merged into one pipe and `TERM=dumb`
   (`server.py:350-351`). The script itself refuses to run without a terminal unless given `--accept`
   (`deploy/bluforge-bluez.sh:129-130`), and installs BlueZ with `dnf` or `apt` from the OS repositories.
3. For each output line: strip ANSI colours, append to `BLUEZ_JOB["log"]` (keeping the last 200, by
   rebuilding the list each time), and publish `{"type":"bluez","text": line,...}` to all browsers.
4. `rc = p.wait()` gets the exit code. Any exception (for example the script vanished, so `script` is `None`
   and `Popen` raises) becomes `rc = 1` plus a published "install failed to start: ..." line.
5. Writes `BLUEZ_JOB["rc"]`, sets `running = False`, and publishes
   `{"type":"bluez","done":True,"ok": rc == 0 and bluez_installed()}`: success needs both a clean exit
   *and* the files actually being there.

* **Gotchas:** no timeout; a hung package manager leaves `running = True` forever and later install requests
  get 409 "an install is already running" until the server restarts. The install child is not tracked by the
  shutdown code. The `nobacklog: True` flag on these events does nothing (only `line` events are backlogged,
  `server.py:107`). Because they are not backlogged, a browser that reconnects mid-install misses earlier lines
  (but `GET /api/bluez/status` returns the last 50, see the route table).

### 4.8 BLE argv builders: `server.py:364-425`

"argv builder" = turns the browser's JSON options into the exact command line for the BLE helper.

**`_opt(argv, flag, v)`, `server.py:365-370`.** Appends `flag, str(v)` to the list, *unless* `v` is `None` or a
blank string. Note `0` and `False` are **not** skipped (they become `"0"`/`"False"`).

**`_flag(argv, flag, v)`, `server.py:373-375`.** Appends just the flag if `v` is truthy (for switches such as
`--no-flags`).

**`build_argv(tool, dev, p)`, `server.py:378-425`.** Inputs: tool name (`adv`/`interfere`/`sniff`), radio
number, and the params dict `p` from the browser. Output: a list starting
`[python, "-u", bluforge_cli.py, "--dev", "<dev>", ...]` (`-u` = unbuffered output).

| Tool | Command built | Options copied (if present) |
|---|---|---|
| `adv` (381-395) | `adv <type>` (default type `nonconn`) | `-i interval`, `-c channels`, `-p peer`, `--peer-type`, `-n name`, `-d data`, `--profile`, `--profile-cfg`, `--scan-resp`, `--flags`, `--no-flags` (switch), `-t duration` |
| `interfere` (396-412) | `interfere <mode>` (default `single`), then the positional `arg` only for modes `wifi/single/sweep/hop` and only if non-empty | `-w bw`, `-l len`, `-P payload`, `--phy`, `-D dwell`, `--power`, `--interval`, `--adv-channels`, `-t duration`, `--map` (switch) |
| `sniff` (413-424) | always `sniff --json --survey` | `--active`, `--coded` (switches), `--ext` only if one of `auto/on/off`, `--interval`, `--window`, `--dedup` (switch), `--filter`, `-t duration` |
| anything else | raises `ValueError("unknown tool ...")` (425) | |

* **Important:** this builder does **not** validate values. It relies on the CLI's own `argparse` (which has
  `choices=` and numeric types) to reject bad input. A bad value therefore shows up as a job that starts and
  immediately exits with a usage error in the console, not as an HTTP 400. This is the opposite of the Classic
  builder, whose docstring promises that "every value is validated/typed here" (`server.py:473-475`).
  Because argv is a list, there is no shell injection. A value starting with `-` could be parsed by the CLI as
  an option; I judged the impact low.
* **Gotcha:** the server always passes `--survey` for sniff, and in survey mode the CLI forces duplicate
  filtering off (`bluforge_cli.py:465, 473`), so the `--dedup` option sent at line 421 has no effect.

### 4.9 Bluetooth Classic: constants, validators, argv builder

#### `AUDIO_USER`: `server.py:429`

`{"name": None}`. A dict used as a "settable global": `main()` stores the `--audio-user` command-line value in
it (`server.py:863`) and the Classic functions read it. The name is the desktop user whose PipeWire audio
session carries the Classic audio (so the root-run helper can reach the user's sound system). `None` means the
helper auto-detects.

#### `_num(v, name, lo, hi, cast=int, default=None)`: `server.py:432-441`

Number validator.
1. `None` or blank string: return `default`.
2. Convert with `cast` (`int` or `float`); `TypeError`/`ValueError` becomes `ValueError("<name> must be a
   number")`.
3. Check `lo <= x <= hi` (inclusive); otherwise `ValueError("<name> must be between lo and hi")`.
4. Return the number.

* **Gotchas:** `int("3.5")` fails ("must be a number") but `int(3.5)` (a JSON number) silently becomes 3.
  JSON allows the non-standard value `Infinity` in Python's parser; `int(float("inf"))` raises
  `OverflowError`, which `_num` does not catch (I checked this with a one-line test). On the
  `/api/classic/action` route that exception is not caught either (section 8). `NaN` is rejected properly.

#### `_addr(p)`: `server.py:444-448`

Reads `p["addr"]`, strips spaces, requires it to match `ADDR_RE`, returns it **upper-cased**. Otherwise
`ValueError("a valid Bluetooth address (AA:BB:CC:DD:EE:FF) is required")`. (If `addr` is not a string, such as
a number, `.strip()` raises `AttributeError`, not `ValueError`.)

#### `_source(v)`: `server.py:451-462`

Validates the audio source for an A2DP stream test. Default `"sweep"`. Accepts `tone`, `sweep`, `noise`, or
`file:/absolute/path` where the path must be absolute, end in one of 23 audio/video extensions (`.wav`,
`.mp3`, `.flac`, `.mp4`, `.mkv`...), and be an existing file (`os.path.isfile`). Else `ValueError`.

* **Gotcha:** this lets any browser pick any audio-extension file readable by root on the server's disk to be
  played into the device under test. It cannot read the file back, but the error message tells whether a
  file exists. Low risk, but worth knowing.

#### `_classic_base(dev)`: `server.py:465-469`

Starts every Classic command line: `[python, "-u", bluforge_classic.py, "--hci", "<dev>"]` and, if set,
`"--audio-user", <name>`. Both of these are *global* options of the Classic CLI, so they come before the
sub-command (`bluforge_classic.py:1856-1857`).

#### `build_classic_argv(name, dev, p)`: `server.py:472-559`

Builds the command line for a Classic **one-shot action** or **long-running job**. Docstring: every value is
validated here "so nothing from the browser reaches a shell or the CLI unchecked". It raises `ValueError`
with a human-readable message on bad input; callers turn that into HTTP 400.

| `name` | Command built (after the base) | Validation |
|---|---|---|
| `adapter` (477-483) | `adapter [--power on/off] [--pairable on/off] [--discoverable on/off]` | each key only added if value is exactly `"on"` or `"off"` (others silently ignored); with none it just shows state |
| `scan` (484-488) | `scan -t N [--transport auto]` | duration 3-120, default 10; `transport` only if exactly `"auto"` |
| `devices` (489-490) | `devices` | none |
| `info`, `disconnect`, `remove` (491-492) | `<name> <addr>` | valid address |
| `pair` (493-497) | `pair <addr> --pin PIN` | PIN must match `[0-9]{1,16}`, default `"0000"` |
| `connect` (498-499) | `connect <addr>` | valid address |
| `interrogate` (500-507) | `interrogate <addr> [--transport auto/le/bredr] --scan N [--no-read]` | scan 3-60, default 15; `--no-read` if `p["read"] is False` |
| `avrcp-player` (508-512) | `avrcp-player <addr> <cmd>` | `cmd` one of `status play pause stop next previous fastforward rewind` (default `status`) |
| `avrcp-volume` (513-517) | `avrcp-volume <addr> --steps S` | `S` matches `\d{1,3}(,\d{1,3}){0,19}` (1-20 numbers). The error text says 0-127 but the pattern allows up to 999; the helper itself re-checks 0-127 (`bluforge_classic.py:984-987`) |
| `c_a2dp` (518-534) | `a2dp <addr> -t N --source S [--codec C] [--latency] [--interval] [--min-active-pct] [--max-xruns] [--max-disconnects] [--min-rssi] [--reconnect] [--disconnect-after]` | duration 0-86400 (0 = until stopped), default 60; source via `_source`; codec `[a-z0-9_]{2,16}`; latency 20-2000; interval 1-60 (float); min-active-pct 0-100; max-xruns 0-100000; max-disconnects 0-1000; min-rssi -127..20 |
| `c_avrcp_watch` (535-541) | `avrcp-watch <addr> -t N [--expect LIST]` | duration 5-3600, default 120; `expect` must match `[A-Za-z,]{1,120}` |
| `c_hfp` (542-553) | `hfp <addr> --scenario S -t N [--number X] [--ring-timeout R]` | scenario `slc`/`sco`/`call` (default `call`); duration 5-3600, default 20; number `\+?[0-9]{3,20}`; ring-timeout 3-120 |
| `c_cycle` (554-558) | `cycle <addr> -n N [--gap G] [--min-success P]` | count 1-1000, default 10; gap 1-60 (float); min-success 0-100 (float) |
| other | `ValueError("unknown Classic action ...")` (559) | |

(xruns = audio buffer under/over-runs, a glitch counter in the audio test; RSSI = received signal strength.)

* **Naming gotcha:** the server's job kinds are `c_a2dp`, `c_avrcp_watch`, `c_hfp`, `c_cycle`, but the
  command words are `a2dp`, `avrcp-watch` (hyphen), `hfp`, `cycle`.
* **Gotcha:** optional numbers use `_opt(a, flag, _num(...))`; because `_num` returns `None` when the field is
  blank, the flag is omitted and the CLI's own default applies (so the *server's* defaults only apply where a
  `default=` is given, for example duration).

### 4.10 One-shot actions: `server.py:562-613`

A **one-shot action** is a quick Classic operation (scan, pair, connect, info...) that the server runs to
completion *inside the web request*, then answers the request with the result. This is different from a **job**
(`Jobs.start`): jobs are long, stream their output, and are tracked in `by_dev`. One-shot actions are **not**
registered as jobs.

**`SAFE_DURING_JOB`, `server.py:563`.** `("info", "devices", "adapter", "avrcp-player", "avrcp-volume")`:
actions that cannot disturb a running stream/cycle/watch job on the same radio. Everything else is refused
while a Classic job runs on that radio.

**`ACTION_TIMEOUT`, `server.py:564`.** Per-action seconds allowed: `interrogate 150`, `scan 150`, `pair 120`,
`connect 120`, `disconnect 40`, `remove 30`. Any other action uses 40 (`ACTION_TIMEOUT.get(action, 40)`,
line 589).

**`run_classic_action(dev, action, params)`, `server.py:567-613`.** Returns `(http_status_code, dict)`.

1. Radio not present: `400`. (570-571)
2. Radio not reserved: `409` "reserve it first". (572-573)
3. A Classic job is running there and the action is not in `SAFE_DURING_JOB`: `409` "jobN is running on hciN
   - stop it before 'x'". (574-576)
4. `build_classic_argv` (validation); `ValueError` gives `400`. (577-580)
5. **Locking** (581-586). Fetch the radio's lock. `info` and `devices` are *read-only*: they neither take nor
   wait for the lock, because the GUI polls them every 5 s and must not block behind (or block) a pair that is
   in progress. Every other action must get the lock *immediately*; if it cannot, `409` "another Classic action
   is still running on hciN" (it does **not** queue):

```python
    readonly = action in ("info", "devices")
    if not readonly and not lock.acquire(blocking=False):
        return 409, {"ok": False, "error": "another Classic action is still running on hci%d" % dev}
```

6. Run the helper synchronously with `subprocess.run(argv, capture_output=True, text=True, timeout=...)`, with
   `PYTHONUNBUFFERED=1`. Output = stdout + stderr. (587-591)
7. **Timeout** (`subprocess.TimeoutExpired`): returns `504` with the last 20 output lines (the code handles
   the Python quirk that the captured output on timeout is bytes). The `finally` block always releases the lock
   (595-597). `subprocess.run` kills a timed-out child with SIGKILL (see section 8).
8. **Parse the output** (598-607): drop blank lines, strip ANSI colours; scan from the *last* line backwards for
   the first line that starts with `{` and parses as JSON, and call that the `result`. All lines not starting
   with `{` are the progress `log`. (So the contract with the helper is: "your final answer is a one-line JSON
   object; everything else is human-readable log".)
9. Publish each log line as an SSE `line` event with `job_id: "action"`, `kind: "c_action"` so the Classic
   console shows it (608-609).
10. No JSON found: `result = {"ok": False, "error": "no result from the Classic engine (exit N)"}`.
11. Attach the last 30 log lines as `result["log"]` and return `200` if `result["ok"]` is truthy, else `422`.

* **Gotchas:** the web request thread is blocked for the whole action (up to 150 s). Pretty-printed
  (multi-line) JSON would not be found, only single-line. The function accepts *any* name that
  `build_classic_argv` knows, including the long-running `c_a2dp`/`c_hfp`/`c_cycle`/`c_avrcp_watch`; sent
  here they run synchronously and are killed at 40 s. The GUI does not do this (see section 8).

### 4.11 More Classic helpers: preflight and results

**`classic_preflight()`, `server.py:616-627`.** Runs `python bluforge_classic.py [--audio-user X] preflight`
(15 s timeout), takes the **last stdout line** as JSON. On any failure returns
`{"ok": False, "error": "preflight failed: ...", "notes": []}`. Always adds `reserved` (sorted reserved radios)
and `all_hci`. It is the dependency self-check shown on the Classic tab (`GET /api/classic/preflight`). It
starts a Python process on every call.

**`SAFE_FILE`, `server.py:630`.** Regex `^[A-Za-z0-9_.-]+\.(json|csv)$`: the only filenames the result
downloader will serve (letters, digits, `_`, `.`, `-`, ending in `.json` or `.csv`).

**`classic_results(limit=40)`, `server.py:633-653`.** Lists saved Classic test results.
1. List `RESULTS_DIR` for `*.json` files, newest first by modification time, keep the first 40. If the folder
   does not exist, return `[]`.
2. For each: skip files over 8 MiB; load the JSON; take its `"summary"` object (invalid/unreadable files are
   skipped).
3. Produce `{file, csv (the matching .csv name if it exists, else None), mtime, test, addr, hci, pass,
   duration_s, fail_reasons, summary}`.

* **Gotcha:** only `OSError` and `ValueError` are caught; a result file whose top-level JSON is not an object
  would raise `AttributeError` and break the whole list request. Result files are written by the Classic
  helper, so this is unlikely.

### 4.12 Capabilities cache: `server.py:656-669`

**`_CAPS`, line 657.** `{"data": None}`: a cache in a dict (so it can be changed from inside a function without
`global`).

**`get_caps(refresh=False)`, lines 660-669.** If cached and not `refresh`, return the cache. Otherwise run
`python bluforge_cli.py caps --compact` (10 s timeout, stdout only), parse the JSON, cache it. On *any*
exception, cache `{"error": "<message>"}`. The `caps` sub-command only prints a description of what the BLE
engine supports (advertising types, interference modes, channel lists, plus the list of radios); the CLI does
not require root for `caps` and does not touch a radio for it (`bluforge_cli.py:641` skips the root check for
`caps`; `cmd_caps` is `bluforge_cli.py:550-575`).

* **Gotcha:** the failure result is cached too, so one transient failure (say a slow cold start exceeding 10 s,
  or the warm-up thread at `main()` line 909 running first) leaves the GUI with `{"error": ...}` until the
  server restarts or someone calls `/api/caps?refresh=1`. Nothing in this repository's GUI calls it with
  `refresh=1`, and when `app.js` sees `CAPS.error` it just logs a warning and stops populating the BLE controls
  (`app.js:131`).

### 4.13 `class Handler`: the HTTP side: `server.py:672-853`

`Handler` subclasses `BaseHTTPRequestHandler` from the standard library. The library creates one `Handler`
per incoming connection (on its own thread, because `main()` uses `ThreadingHTTPServer`) and calls
`do_GET` or `do_POST` depending on the request method. Any other method (HEAD, PUT, DELETE...) gets the
library's default 501 reply.

**`protocol_version = "HTTP/1.0"`, `server.py:681`.** Deliberate (comment 674-680): the SSE stream writes
forever with no `Content-Length`, which is invalid under HTTP/1.1 keep-alive unless chunked encoding is used.
In HTTP/1.0 "the response ends when the connection closes", which is exactly what a never-ending stream needs.
The authors report that HTTP/1.1 produced zero bytes on the stream. A side effect: every normal request uses a
fresh TCP connection (closed after the reply).

**`log_message(self, *a)`, `server.py:683-684`.** Overrides the library's per-request log line with `pass`:
**the server prints no access log at all**. Uncaught handler exceptions still go to stderr via the library.

**`_send(code, body, ctype="application/json")`, `server.py:686-699`.** The common reply helper.
1. `dict`/`list` body is turned into JSON bytes; `str` into UTF-8 bytes; `bytes` pass through.
2. Sends status, `Content-Type`, `Content-Length`, and `Cache-Control: no-store, no-cache, must-revalidate,
   max-age=0` (browsers must never reuse an old answer).
3. Writes the body; a write error (the browser went away) is ignored.

**`_send_static(rel_path, ctype)`, `server.py:701-707`.** Serves a file from `STATIC_DIR`: 404 JSON error if it
is not a regular file, else reads the whole file and sends it. No caching, no range requests.

**`do_GET`, `server.py:709-755`.** Drops the `?query` part of the URL (`self.path.split("?")[0]`), then picks a
branch by path (see the route table, section 5):
* Static files: `/` and `/index.html`, `/style.css`, `/app.js`, `/classic.js`, `/assets/<name>`. For assets
  only `os.path.basename(path)` is used, so `..` tricks cannot escape the folder. Content type by extension:
  `.svg`, `.png`, `.ico`, `.woff2`, else `application/octet-stream`.
* API reads: `/api/caps`, `/api/status`, `/api/classic/preflight`, `/api/classic/results`,
  `/api/classic/result/<file>`, `/api/bluez/status`, `/api/sniff/devices`, `/api/stream`.
* `/api/classic/result/<file>` (735-747): takes `os.path.basename` of the path, requires `SAFE_FILE` to match
  *and* the file to exist in `RESULTS_DIR`, then sends it as a download (`Content-Disposition: attachment`),
  `text/csv` or `application/json`. This is the safe-file check that keeps `../../etc/passwd` out.
* Anything else: 404 `{"error": "not found"}`.

**`do_POST`, `server.py:757-822`.**
1. Read the body: `length = int(Content-Length header or 0)`, then read exactly that many bytes (no maximum
   size, no timeout), or use `b"{}"` if none (758-759).
2. Parse as JSON. **If parsing fails, the body silently becomes `{}`** (760-763), so a malformed body to
   `/api/stop_all` still works and a malformed body to `/api/start` yields "bad tool". The `Content-Type`
   header is ignored.
3. **BlueZ gate** for the three routes (765-768, section 4.7).
4. Route (details in the table):
   * `/api/bluez/install` (769-781): needs `accept: true` (else 400); if BlueZ is already installed returns
     `{"ok": true, "msg": "BlueZ is already installed"}`; if the script is not found, 500; under
     `BLUEZ_LOCK`, if an install is running, 409, else mark `running`, clear log, `rc = None`; then start
     `bluez_install_job` on a daemon thread and reply "install started".
   * `/api/start` (782-798): `tool` must be in `BLE_KINDS + CLASSIC_JOB_KINDS` else 400 "bad tool"; `dev` is
     converted with `int()` else 400; builds argv with `build_classic_argv` (Classic kinds) or `build_argv`
     (BLE kinds), any exception gives 400 with the message; calls `JOBS.start`; replies with
     `{"ok", "msg", "job_id", "argv"}`, status 200 on success and **409 on any failure** (including "radio
     does not exist" and "failed to start"). The reply includes the full `argv` list.
   * `/api/classic/reserve` (799-806): `dev` int (else 400); `on` (default true) chooses reserve or release;
     then **always** publishes an SSE `reservation` status event (even on failure) so other tabs refresh;
     replies `{"ok","msg","reserved":[...]}`, 200 or 409.
   * `/api/classic/action` (807-813): `dev` int (else 400); calls `run_classic_action(dev, str(body.get("action")),
     body.get("params") or {})` and replies with its `(code, dict)`.
   * `/api/stop` (814-817): `JOBS.stop(body.get("job_id"))`; replies **200** `{"ok", "msg"}` even when `ok` is
     false (a wedged job).
   * `/api/stop_all` (818-820): `JOBS.stop_all()`; replies `{"ok": true}` unconditionally.
   * anything else: 404.

**`_stream()`, `server.py:824-853`: the SSE endpoint.** Step by step:
1. Sends `200`, `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `Connection: keep-alive`, and
   ends the headers. It never sends `Content-Length`.
2. `q, backlog = JOBS.subscribe()` (section 4.5).
3. Defines `write(evt)`: writes `data: <json>` plus a blank line and flushes (832-834):

```python
        def write(evt):
            self.wfile.write(("data: %s\n\n" % json.dumps(evt)).encode())
            self.wfile.flush()
```

4. Inside a `try`: sends the **hello** event (`{"type":"status","event":"hello","status": JOBS.snapshot()}`) so a
   new page has the whole state immediately; then replays every backlog event (up to 500 console lines).
5. Loop forever: `evt = q.get(timeout=15)`. If nothing arrived for 15 s, writes `: ping` (an SSE *comment*,
   ignored by the browser) to keep the connection alive and to notice a dead client (the write fails). If the
   event is the internal `overflow` marker, `return` (closing the response; the browser reconnects). Otherwise
   write it.
6. Any exception (client closed the tab, broken pipe) ends the loop quietly; `finally` calls
   `JOBS.unsubscribe(q)` so the queue stops receiving events.

* **Gotchas:** every open tab keeps one server thread blocked here. Detecting a vanished client can take up to
  15 s (the next ping). There is no socket timeout; a client that stops reading entirely could leave its
  thread stuck inside `write` (not tested).

### 4.14 `main()` and shutdown: `server.py:856-914`

1. **Arguments** (857-862): `--port` (default **5480**), `--host` (default **`0.0.0.0`**, meaning "listen on
   every network interface"), `--audio-user` (stored in `AUDIO_USER`, line 863).
2. **Root check** (864-865): if `os.geteuid() != 0`, prints a warning to stderr ("jobs will fail to grab raw
   HCI. Start with sudo.") but **keeps running**.
3. **Signal handlers.** `handle_sigterm` (867-880) is registered for **SIGTERM** (881) and **SIGHUP** (889).
   When either arrives it:
   * calls `JOBS.stop_all()` (each child gets SIGINT then SIGTERM, one after another);
   * *then* starts a **2-second timer** that calls `os._exit(0)` (an immediate, no-cleanup exit);
   * then `sys.exit(0)`, which raises `SystemExit` in the main thread, leaving `serve_forever()`.

```python
        t = threading.Timer(2.0, os._exit, args=(0,))
        t.daemon = True
        t.start()
        sys.exit(0)
```

   The long comment (869-876) explains: with a real browser tab connected, `sys.exit()` once did *not* end the
   process, so the timer guarantees the process disappears. SIGHUP matters because the desktop launcher runs
   the server in the foreground of a terminal window; closing the window sends SIGHUP, and without this handler
   running jobs would be orphaned (comment 882-888).
4. **Server object** (891): `ThreadingHTTPServer((host, port), Handler)`: "one new thread per connection".
5. `srv.daemon_threads = True` (903): connection threads will not stop the process from exiting. The long
   comment (892-902) says this setting defaults to `False`; that is true for the base `ThreadingMixIn`, but
   the standard `ThreadingHTTPServer` already sets it to `True` (I checked on Python 3.12.7), so this line is
   redundant, though harmless.
6. **Banner** (904-908): tries `socket.gethostbyname(socket.gethostname())` for a friendly IP (falls back to
   `--host`) and prints `[+] Ericsson BLE demo GUI on http://<host>:<port>  (try http://<ip>:<port>)`. The IP
   found this way can be a loopback-ish address like 127.0.1.1 on some systems.
7. **Warm-up** (909): a daemon thread runs `get_caps()` once so the first page load is fast.
8. **Serve** (910-914): `srv.serve_forever()` runs until interrupted. **SIGINT / Ctrl-C** raises
   `KeyboardInterrupt` inside it; the handler there calls `JOBS.stop_all()` and prints `[+] bye`.
   (`srv.server_close()` is never called; the process just exits.)

**Shutdown summary.**

| How the server is told to stop | What happens to child jobs | Notes |
|---|---|---|
| Ctrl-C (SIGINT) | `stop_all()` (SIGINT then SIGTERM per job) then exit | no 2 s timer on this path |
| SIGTERM (for example `systemctl stop`, `kill`) | `stop_all()`, arm 2 s `os._exit(0)`, `sys.exit(0)` | the timer is only armed *after* `stop_all()` returns, so a wedged job (up to 8 s each) delays it |
| SIGHUP (terminal closed) | same as SIGTERM | the reason the desktop launcher works as "close window = stop" |
| SIGKILL / power loss | nothing runs | children (their own sessions) keep running, orphaned |

Not stopped by any shutdown path: one-shot Classic action subprocesses and the BlueZ install child (neither
runs in its own session, and nothing tracks them).

---

## 5. Every HTTP route

Callers are from a search of `src/static/*.js`, `index.html`, `style.css` and `desktop/start-and-open.sh`.

### GET routes (`Handler.do_GET`, `server.py:709`)

| Route | Line | What it does | Called by |
|---|---|---|---|
| `/` and `/index.html` | 711 | Sends `static/index.html` | the browser address bar; `desktop/start-and-open.sh` opens `http://localhost:5480/` |
| `/style.css` | 713 | Sends `static/style.css` | `index.html:8` |
| `/app.js` | 715 | Sends `static/app.js` | `index.html:478` |
| `/classic.js` | 717 | Sends `static/classic.js` | `index.html:479` |
| `/assets/<name>` | 719 | Sends a file from `static/assets/` (basename only) | `index.html:7,13` (favicon, logo), `style.css:75` (font) |
| `/api/caps` (`?refresh=1` re-runs it) | 726 | BLE capabilities JSON (cached) | `app.js:130` on page load. Nothing uses `refresh=1`. |
| `/api/status` | 729 | `Jobs.snapshot()`: running/finished jobs, radios, reservations, hostname, BlueZ info | `app.js:365` (page load, every 5 s, after each status event); `desktop/start-and-open.sh` uses it to test whether the server is up |
| `/api/classic/preflight` | 731 | Runs the Classic dependency check | `classic.js:615` |
| `/api/classic/results` | 733 | List of the 40 newest saved Classic results | `classic.js:326` |
| `/api/classic/result/<file>` | 735 | Downloads one result `.json`/`.csv` (name must match `SAFE_FILE`) | `classic.js:353` (download links) |
| `/api/bluez/status` | 748 | `bluez_info()` plus the last 50 install log lines and exit code | I found **no caller** in the GUI code (BlueZ info already arrives inside `/api/status`); probably for debugging/scripts |
| `/api/sniff/devices` | 750 | `{"sniffs": Jobs.sniff_snapshot(), "now": <server time>}` | `app.js:421` (`snResync`, run every time the live feed connects, to rebuild the device table) |
| `/api/stream` | 752 | The SSE live feed (`_stream`) | `app.js:790` (`new EventSource("/api/stream")`) |
| anything else | 754 | 404 JSON | |

### POST routes (`Handler.do_POST`, `server.py:757`)

| Route | Line | What it does | Called by |
|---|---|---|---|
| `/api/bluez/install` | 769 | Body `{"accept": true}`. Starts the BlueZ installer in a background thread. Replies 400 / 200 (already installed) / 500 (no script) / 409 (already running) / 200 (started) | `app.js:345` (banner button, after a confirm dialog) |
| `/api/start` | 782 | Body `{"tool", "dev", "params"}`. Builds argv, starts a job. Gated by BlueZ check. | `app.js:179` (interfere), `200` (adv), `211` (sniff); `classic.js:271` (Classic jobs) |
| `/api/classic/reserve` | 799 | Body `{"dev", "on"}`. Reserve (`on` true, default) or release a radio for Classic. Gated by BlueZ check. | `app.js:613, 712`; `classic.js:519` |
| `/api/classic/action` | 807 | Body `{"dev", "action", "params"}`. Runs one one-shot Classic action and returns its JSON result. Gated by BlueZ check. | `classic.js:142`; `app.js:618` (scan), `717` (interrogate) |
| `/api/stop` | 814 | Body `{"job_id"}`. Stops one job (SIGINT, then SIGTERM). Can take up to 8 s. | `app.js:231`; `classic.js:384` |
| `/api/stop_all` | 818 | Stops every job. Always replies `{"ok": true}`. | `app.js:220` ("Stop All" button) |
| anything else | 821 | 404 JSON | |

Routes that never exist: there is no login, logout, or file-upload route, and no route that reads arbitrary
files (only the `static/` folder and `RESULTS_DIR` are served, both by basename).

---

## 6. Three end-to-end walk-throughs

**A. Starting a BLE sniff on hci1.**
1. The Sniff tab sends `POST /api/start {"tool":"sniff","dev":"1","params":{...}}` (`app.js:211`).
2. `do_POST` runs the BlueZ gate (BlueZ must be present), checks `tool`, converts `dev` to `1`, calls
   `build_argv("sniff", 1, params)` and gets `python -u bluforge_cli.py --dev 1 sniff --json --survey ...`.
3. `Jobs.start` verifies radio 1 exists, is not reserved and is not busy, spawns the child, registers `job1`,
   publishes `status/started`, and starts the reader thread. The HTTP reply goes back (200, `job_id: "job1"`).
4. The child prints a `{"m":"dev"...}` line per changed device and a `{"m":"stats"...}` line each second. The
   reader thread folds them into one `sniff` event per second and every open browser tab receives it over its
   SSE connection and updates its table.
5. Pressing Stop sends `POST /api/stop {"job_id":"job1"}`; `Jobs.stop` sends SIGINT (the helper prints its final
   summary and restores the radio), waits up to 5 s, then the reader thread sees EOF, records the exit code,
   frees radio 1 and publishes `status/finished`.

**B. A Classic scan on hci0.**
1. Classic tab: `POST /api/classic/reserve {"dev":0,"on":true}` puts radio 0 in the reserved set.
2. `POST /api/classic/action {"dev":0,"action":"scan","params":{"duration":10,"transport":"auto"}}`.
3. `run_classic_action` checks the radio and reservation, builds `bluforge_classic.py --hci 0 scan -t 10
   --transport auto`, takes radio 0's lock, runs it for at most 150 s, finds the last JSON line, publishes the
   progress lines to the console, and returns the JSON with `log` attached.

**C. A browser reconnecting.** The page's `EventSource` connects to `/api/stream`. `_stream` subscribes, sends
`hello` (full state), then the last 500 console lines, then live events; `app.js` also fetches
`/api/sniff/devices` on open to repaint the survey table. If the browser tab freezes long enough to fill its
2000-event queue, the server closes the stream and the browser reconnects, getting the same resync.

---

## 7. Where each shared piece of state lives (cheat sheet)

| State | Where | Protected by | Lost on restart? |
|---|---|---|---|
| All jobs, current job per radio | `Jobs.jobs`, `Jobs.by_dev` | `Jobs.lock` | yes |
| Reserved radios | `Jobs.reserved` | `Jobs.lock` | yes |
| Per-radio Classic-action locks | `Jobs.dev_locks` | `Jobs.lock` (to create); each is itself a lock | yes |
| Live-feed subscribers and 500-line backlog | `Jobs.subs`, `Jobs.backlog` | `Jobs.subs_lock` | yes |
| Survey device table per job | `Job.devices`, `Job.stats` | none (single writer thread) | yes |
| BlueZ install status and log | `BLUEZ_JOB` | `BLUEZ_LOCK` only for the start check | yes |
| Capabilities | `_CAPS` | none | yes |
| `--audio-user` | `AUDIO_USER` | none (written once at startup) | n/a |
| Classic result files | files in `RESULTS_DIR` | written by the Classic helper, only read here | no |

---

## 8. Things I noticed

Ordered by how much they matter. "Confirmed" = visible directly in the code; "likely"/"not tested" = reasoning
or something I could not run, because I was not allowed to start the server or touch radios.

### Security

1. **No authentication or authorisation anywhere** (`server.py:709-822`). Any client that can reach port 5480
   can start RF-interference and advertising jobs, stop jobs, pair/connect/remove devices, download test
   results, and trigger a BlueZ package install. Confirmed.
2. **Listens on all network interfaces by default** (`--host` default `0.0.0.0`, line 859). Both the systemd
   unit (`ExecStart ... --port`) and the desktop launcher (`desktop/start-and-open.sh:73`, `--port "$PORT"`)
   pass only `--port`, so both leave the server exposed to the whole network, even though the launcher itself
   only opens `http://localhost`. There is a `--host` option, so `--host 127.0.0.1` is possible. Confirmed.
3. **Runs as root**, and so do all children (service `User=root`; launcher uses root). Combined with 1 and 2,
   any network client effectively gets root-run Bluetooth control, plus `POST /api/bluez/install`, which runs
   `bluforge-bluez install --accept` (a package-manager install as root) on request. The "you must accept the
   licence" check (`body.get("accept") is not True`, line 770) is enforced only by whatever the caller sends,
   not by a person. Confirmed.
4. **Cross-site request risk (likely).** The POST body is parsed as JSON whatever `Content-Type` says (line
   761), and there is no `Origin`/`Host` check and no token. A web page open in any browser that can reach the
   box could send a "simple" cross-origin POST (for example `Content-Type: text/plain`) and have it executed;
   the page could not read the reply, but the action would happen. DNS-rebinding is similar. I did not test
   this.
5. **No limit on request size or time** (lines 758-759): `rfile.read(Content-Length)` reads whatever length the
   client claims into memory and waits for it; one thread per connection. A client can exhaust memory or hold
   threads. Confirmed from the code.
6. **No access log** (`log_message` is empty, lines 683-684): there is no record of who started or stopped
   what.
7. Minor: `/api/start` echoes the full command line back (line 798); `_source` lets a caller probe for the
   existence of audio-extension files anywhere on disk and make the box play them (451-462).
   `GET /api/classic/result/<file>` and `/assets/<name>` are properly restricted to a basename plus (for
   results) a filename whitelist, so no path traversal there.

### Bugs and robustness

8. **BLE argv is not validated** (`build_argv`, 378-425), even though the Classic builder's docstring (473-475)
   promises "nothing reaches the CLI unchecked". Bad values only fail later as a job that exits straight away
   (no shell injection, since argv is a list). Confirmed.
9. **Malformed or odd requests can kill a handler without any HTTP reply.** `int(Content-Length)` (758) raises on
   a non-numeric header; a JSON body that is a list/number/`null` becomes `body` (761-763) and then
   `body.get(...)` raises `AttributeError` (for example in `/api/stop`, line 815); `params` that is not an
   object breaks `p.get`; `Infinity` in a numeric Classic field raises an `OverflowError` that
   `run_classic_action` does not catch (578-580 catches only `ValueError`). The library prints a traceback and
   closes the connection. (`/api/start` is protected by `except Exception`, line 795.) Confirmed by reading;
   the `Infinity` case also checked with a one-line Python test.
10. **`stop_all` always says `{"ok": true}`** (278-282, 818-820) even if a job failed to stop (wedged
    controller). `POST /api/stop` returns HTTP 200 with `ok: false` in that case (814-817). Inconsistent.
11. **Memory growth.** `Jobs.jobs` is never pruned (nothing deletes from it), and each sniff job keeps a record
    for every address it ever saw (`job.devices`, line 215), while the helper itself caps at 5000 and evicts the
    stalest (`ble_survey.py:12,53`). With rotating random BLE addresses a long-running server grows, and
    `/api/sniff/devices` can return a very large list including devices the helper already forgot. The GUI only
    shows the last 20 finished jobs (line 300), which hides the leak. Confirmed.
12. **Capabilities failure is cached forever** (660-669): one transient failure leaves `{"error": ...}` until
    restart; nothing calls `refresh=1`. Likely to be rare but would leave the BLE tabs empty. Confirmed.
13. **Swallowed errors in the reader thread** (230-231): any exception ends output reading while the child may
    still be running and writing, which can stall the child on a full pipe (see 4.5). Not seen in practice.
14. **Shutdown timing.** The "gone within 2 s" guarantee (comment 874-876) only starts counting *after*
    `stop_all()` finishes (the timer is armed at 877, `stop_all()` is called at 868), and `stop_all` is
    sequential with up to 8 s per stuck job. Under systemd, children are in the same control group as the server (`start_new_session` does not change that), and
    the unit file sets no `KillMode`, so by systemd's default behaviour a `systemctl stop` would also signal the
    children directly with SIGTERM at the same time, bypassing the SIGINT-first design. This last point is from
    general systemd knowledge, **not tested here**.
15. **One place that does use SIGKILL.** `subprocess.run(..., timeout=...)` in `run_classic_action` (589) kills a
    timed-out child with SIGKILL (standard Python behaviour), contradicting the "never SIGKILL" rule in the
    docstring (13-15). The rule was written about raw-HCI BLE jobs; Classic goes through BlueZ, so the same
    hazard may not apply, but I cannot tell from this file. Also that child shares the server's process group,
    so any grandchildren it started are not killed with it.
16. **`/api/classic/action` accepts long-running names** (`c_a2dp`, `c_hfp`, `c_cycle`, `c_avrcp_watch`) because
    nothing restricts `action` to one-shot names; they would run synchronously, unregistered, and be killed at
    the 40 s default timeout (`ACTION_TIMEOUT.get(action, 40)`). The GUI does not do this. Confirmed by reading.
17. **Race right after a stop** (see 4.5): `by_dev` is cleared by the reader thread slightly after `stop()`
    reports success; an immediate restart on the same radio can get a spurious 409.
18. **`/api/classic/reserve` treats any truthy `on` as "reserve"** (line 804): the string `"false"` would reserve.
    The GUI sends a real boolean (`app.js:613`, `classic.js:519`).
19. **`--dedup` is dead for sniff** (421): survey mode ignores it (`bluforge_cli.py:465,473`).
20. **Inconsistent HTTP status codes:** every `JOBS.start` failure is 409 (even "no such radio" or "failed to
    start"); stop failures are 200; failed Classic tests are 422; BlueZ-missing is 409 with `bluez_missing`.
21. **Possible duplicate console lines after reconnect.** Every reconnect replays up to 500 backlog lines and I
    did not find de-duplication in `app.js`'s stream code. Not tested.
22. **`classic_results`** can fail the whole list if a result file's JSON is not an object (645-646, 647).
23. **`avrcp-volume` steps check** (515-516) allows 3-digit values while its error message says 0-127; the
    helper catches it later (`bluforge_classic.py:984-987`). Harmless inconsistency.

### Dead or outdated code and comments

24. Unused imports `urlparse`, `parse_qs` (line 39). Confirmed with a syntax-tree check.
25. `nobacklog: True` on `bluez` events (355, 359, 362) has no effect (only `line` events are backlogged, line
    107).
26. `srv.daemon_threads = True` (903) is redundant on current Python; its comment describes the base class
    default, not `ThreadingHTTPServer`'s.
27. The module docstring is stale: mentions "hci0-7", "8 BE200 controllers", only three BLE tools, and
    `gui.html` (lines 5-10, 22). The start-up banner says "Ericsson BLE demo GUI" (908).
28. `BLUEZ_JOB` is changed by two threads (the request thread and the install thread) with `BLUEZ_LOCK` used
    only for the start check. In practice the install thread only writes after `running` was set, so it works,
    but the lock does not cover everything.

### Unclear or not verified

* I did **not** run the server, any helper, or any Bluetooth command, as instructed. Behaviour that depends on
  the helpers (what exactly is printed, how they react to SIGINT) comes from skimming them, not from running
  them.
* Whether BLE (non-Classic) jobs truly need BlueZ installed, even though `/api/start` is gated for them
  (765-768). The code treats BlueZ as required for everything.
* Who, if anyone, calls `GET /api/bluez/status` and `GET /api/caps?refresh=1`: no caller exists in this
  repository's GUI.
* The SSE comment at 674-680 says HTTP/1.1 produced zero bytes on the stream; I did not reproduce it.
* Reservations are only a flag: this file does not itself hand radios between "raw HCI" and BlueZ. Where and
  how that happens lives in the helper CLIs, which I did not document here.
