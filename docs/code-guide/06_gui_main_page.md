# 06 - The GUI main page: index.html, style.css, app.js

Audience: a beginner who is lost. Every line of the three files below was read to write this guide.

| File | Lines | Job |
|---|---|---|
| `src/static/index.html` | 481 | The skeleton: every tab, form field, table and button, each with an `id` |
| `src/static/style.css` | 238 | How all of it looks (colours, spacing, tables, tabs, console) |
| `src/static/app.js` | 829 | All behaviour for the Radios / Interference / Advertise / BLE Sniff tabs, plus the live connection to the server and the BlueZ banner |

`src/static/classic.js` (the Bluetooth Classic tab) is covered by another guide. This guide only says where `app.js` hands events to it (section 4.14, "The BF hooks").

Line references look like `app.js:420`. They refer to the files as they were when this guide was written.

---------------------------------------------------------------------

## 0. Words you will meet (read this first)

You do not need to memorise these. Come back here when a word is unfamiliar. Each is also explained again where it first matters.

| Word | Plain meaning |
|---|---|
| **Browser / page / GUI** | The program you look at (Chrome, Firefox). The "GUI" (graphical user interface) is the page `index.html` plus its CSS and JavaScript. |
| **HTML** | A text format that describes *what is on the page* (a button, a table, a text box). Written as nested tags like `<button id="x">Go</button>`. |
| **CSS** | A text format that describes *how it looks* (colour, size, position). |
| **JavaScript (JS)** | A programming language the browser runs. It makes the page react to clicks and update itself. "Vanilla" JS means no add-on libraries or frameworks (no React, no jQuery). |
| **DOM** (Document Object Model) | The browser's live, in-memory tree of the page. HTML is read once to build the DOM; JS then reads and changes the DOM, and the screen follows. `document.getElementById("x")` returns one DOM *element*. |
| **id / class** | Two labels you can put on an element. An `id` is unique (`id="sn_start"`), so JS can find that one element. A `class` is shared by many elements (`class="lf-btn"`), so CSS can style them all alike. |
| **Event / event listener** | An event is "something happened" (a click, a key press, a message arrived). A listener is a function you register that the browser calls when that event happens: `button.addEventListener("click", fn)`. |
| **Callback** | A function handed to someone else to be called later (a listener is a callback). |
| **IIFE** | "Immediately invoked function expression": `(function () { ... })();`. It runs once, and everything declared inside stays private to it. `app.js` is wrapped in one (`app.js:4` and `app.js:829`). |
| **`"use strict"`** | A switch (`app.js:5`) that makes JS refuse some sloppy habits, such as using a variable you never declared. |
| **Closure** | A function that remembers the variables around it where it was created. Most listeners in `app.js` are closures. |
| **Map / Set** | Built-in JS containers. A `Map` stores key to value pairs and remembers insertion order; a `Set` stores unique values. |
| **JSON** | A text format for data: `{"dev": 0, "ok": true}`. `JSON.parse` turns text into objects; `JSON.stringify` does the reverse. |
| **API / route** | The server's menu of URLs the page can call, such as `/api/status`. Each URL is a "route". |
| **HTTP GET / POST** | The two request kinds used. GET = "give me data". POST = "do something, here is the data (a JSON body)". The reply carries a *status code*: 200 means OK, 4xx/5xx mean refused or failed (409 = "conflict", for example "that radio is busy"). |
| **fetch** | The browser function that makes an HTTP request from JS. It returns a Promise. |
| **Promise / async / await** | A Promise is "a result that will arrive later". `await somePromise` pauses the current `async function` until it arrives, without freezing the page. `.then(fn)` and `.catch(fn)` are the older way to say the same thing. |
| **try / catch** | Run code; if it throws an error, jump to the `catch` block instead of crashing. |
| **SSE / EventSource** | Server-Sent Events: the server keeps one HTTP connection open and pushes lines of text to the page whenever it has news. `EventSource` is the browser object that holds that connection and automatically reconnects if it drops. This is how the page updates live without you pressing Refresh. |
| **Polling** | Asking the server again and again on a timer ("any news?"). The page does this too, every 5 seconds, as a safety net. |
| **CSS variable** | A named value in CSS written `--lf-text: #333333;` and used as `var(--lf-text)`. Change it in one place and everything using it changes. Also called a "design token". |
| **Flexbox** | A CSS layout mode (`display:flex`) that lines children up in a row or column. |
| **textContent vs innerHTML** | `el.textContent = "x"` puts *plain text* in an element (anything that looks like HTML is shown literally, so it is safe). `el.innerHTML = "x"` parses the string *as HTML* (dangerous with untrusted text). |
| **XSS** (cross-site scripting) | An attack where hostile text, shown as HTML, runs as a script in your page. Relevant here because Bluetooth device names come from the air and are chosen by whoever owns the device. |
| **CSV injection** | A spreadsheet attack: a CSV cell starting with `=`, `+`, `-` or `@` may be run as a formula when opened in Excel. |
| **Blob / object URL** | A Blob is an in-memory file. `URL.createObjectURL(blob)` makes a temporary link to it, which is how the page offers a "download" without the server. |
| **hci / radio** | `hci0`, `hci1`... are the Linux names of the Bluetooth radio chips (Intel BE200 cards) in the machine. The code and the screen call them "radios" or "controllers". |
| **BLE** | Bluetooth Low Energy: the low-power Bluetooth used by beacons, sensors, earbuds' control links. Devices "advertise" (shout their presence) on 3 channels. |
| **Classic / BR/EDR** | Older Bluetooth used for audio (A2DP), hands-free calls (HFP), remote control (AVRCP). Does not advertise like BLE. |
| **RSSI** | Received Signal Strength: how loud a device is heard, in dBm. Always negative; closer to 0 is stronger (-40 is strong, -90 is weak). |
| **BlueZ** | The Linux Bluetooth software stack. BluForge's raw-HCI BLE jobs do not need it; Classic and interrogation do. It is installed by the end user, not shipped. |
| **GATT / SDP** | The "service menus" of a device: GATT for BLE (services, characteristics, values), SDP for Classic (profiles such as headset, A2DP). |
| **Job** | One helper process the server started on one radio (an interference run, an advertising run, a survey...). One job per radio at a time. |

---------------------------------------------------------------------

## 1. How the page is organised

### 1.1 The picture

```
+--------------------------------------------------------------------+
| [Candela logo] BluForge          hostname   (o) connected          |  title band    .lf-titleband
+--------------------------------------------------------------------+
| BlueZ is not installed.  ...text...        [Install BlueZ...]      |  banner        #bluezBanner
|   (notice text)  (install log, appears while installing)           |  (hidden unless BlueZ is missing)
+--------------------------------------------------------------------+
| Control  Reporting  Windows  Info  Tests                           |  menu bar      .lf-menubar   (decoration)
|                                  [Stop All] [Refresh] [HELP]       |  action row    .lf-actions
| [Radios][Interference][Advertise][BLE Sniff][Bluetooth Classic]    |  tab strip     .lf-tabs
| +----------------------------------------------------------------+ |
| |  ONE of five <section>s is visible at a time                   | |  content pane  .lf-pane
| |  (Radios | Interference | Advertise | BLE Sniff | Classic)      | |
| +----------------------------------------------------------------+ |
| Logged in to: <host>   N job(s) running            BluForge v1|  status bar    .lf-status
+--------------------------------------------------------------------+
```

The look imitates the Java client of LANforge (Candela's product): grey "Metal/Ocean" look, small 12 px text, square gradient buttons.

### 1.2 The five tabs and what each is for

| Tab (label) | Section id | Purpose |
|---|---|---|
| Radios | `tab-radios` | Read-only overview: one row per radio (idle / running / reserved for Classic), plus recent job history. |
| Interference | `tab-interfere` | Start 2.4 GHz BLE interference (Direct Test Mode) on one or more radios; list and stop those jobs; see their console output. |
| Advertise | `tab-advertise` | Start a BLE advertisement (iBeacon, Eddystone...) on one radio; list/stop; console. |
| BLE Sniff | `tab-sniff` | Listen for BLE advertisers over the air and show a live, sortable, searchable table ("survey"); inspect one device; "interrogate" it; run a Classic inquiry into the same table; export JSON/CSV. |
| Bluetooth Classic | `tab-classic` | A2DP/AVRCP/HFP tests. Its HTML is in `index.html`, its behaviour is in `classic.js` (not this guide). |

### 1.3 How the pieces talk

```
   Browser (index.html + style.css + app.js + classic.js)
        |  fetch  GET /api/status, /api/caps, /api/sniff/devices
        |  fetch  POST /api/start, /api/stop, /api/stop_all, /api/classic/*, /api/bluez/install
        |  EventSource  GET /api/stream   <--- server PUSHES events: status, line, sniff, bluez
        v
   src/server.py  (Python, port 5480)  -- starts/stops helper processes, one per radio
        |
        v
   helper programs (bluforge_cli.py, bluforge_classic.py) talking to the radios
```

Two ways the page learns what is going on, working together:

1. **Push**: the SSE stream (`/api/stream`) delivers news the moment it happens (a job started, a console line, a batch of sniffed devices).
2. **Pull**: `refreshStatus()` asks `/api/status` for a full snapshot every 5 seconds and after most pushed events. If a push is ever missed, the next pull repairs the screen.

### 1.4 Load order (important)

The browser reads `index.html` top to bottom. The two `<script>` tags are the *last* things in `<body>` (`index.html:478-479`):

```html
<script src="/app.js"></script>
<script src="/classic.js"></script>
```

Because they come after all the HTML, every element already exists in the DOM when the scripts run, so `document.getElementById(...)` always finds its target. `app.js` runs first and creates `window.BF`; `classic.js` then requires `window.BF` (it prints an error and quits if it is missing).

The server serves these files itself (`server.py` `do_GET`: `/`, `/style.css`, `/app.js`, `/classic.js`, `/assets/...`) with `Cache-Control: no-store`, so a reload always gets the newest copy.

### 1.5 State lives in three places

| Where | What | Survives page reload? |
|---|---|---|
| **The server** | Which jobs exist/run, which radios are reserved for Classic, each sniff job's device list | Yes (until the server restarts) |
| **JS variables in `app.js`** | A copy of the server's status (`STATE`), the survey table (`snDevs`), the selected device, interrogation results | No. Reloading rebuilds `STATE` and the survey table from the server; interrogation results and the selected device are lost |
| **The DOM itself** | What you typed into form fields | No. There is no `localStorage`; reloading resets every field and returns to the Radios tab |

---------------------------------------------------------------------

## 2. index.html, section by section

Conventions in this file: class names starting `lf-` are the "LANforge look" building blocks; class names starting `c-` belong to the Classic tab. A form field is a `div.lf-field` holding a `<label>` and a control; fields are grouped in `div.lf-row` (a wrapping row) inside a `fieldset.lf-group` (a bordered box with a `<legend>` title).

### 2.1 `<head>` (`index.html:3-9`)

UTF-8 text, a "viewport" meta tag (makes the page scale on phones), title "BluForge", favicon `/assets/favicon.ico`, stylesheet `/style.css`.

### 2.2 Title band (`index.html:12-18`)

| id | What it is |
|---|---|
| (img.logo) | Candela logo, `/assets/CandelaLogo2-90dpi-200x90-trans.png` |
| `hostInfo` | The machine name. The text `ct523c-ccb8` is only a placeholder; `applyState()` overwrites it with the server's real hostname (`app.js:359`) |
| `connDot` | A 9 px round dot. Red by default; gets class `up` (green) when the SSE stream is connected (`app.js:791`) |
| `connText` | Text next to the dot: "connecting...", "connected", "reconnecting..." |

### 2.3 BlueZ banner (`index.html:20-29`)

A yellow strip, **hidden by default** with an inline `style="display:none"`. Shown only when the server reports BlueZ is not installed.

| id | What it is |
|---|---|
| `bluezBanner` | The whole strip |
| `bluezInstallBtn` | "Install BlueZ..." button |
| `bluezNotice` | A licence/explanation paragraph. Its text comes from the server (`BLUEZ_NOTICE` in `server.py`), not from the HTML |
| `bluezLog` | A dark console, hidden until an install starts, that shows installer output (max height 120 px) |

### 2.4 Menu bar (`index.html:31-33`)

Five words: Control, Reporting, Windows, Info, Tests. They are plain `<span>`s with **no id and no click handler anywhere**. They are decoration copied from the Java client; they do nothing (CSS gives them a hover highlight, which makes them look clickable).

### 2.5 Action row (`index.html:35-39`)

| id | Button | Handler |
|---|---|---|
| `stopAllBtn` | **Stop All** (red-bordered) | Tells the server to stop every job (`app.js:216`) |
| `refreshBtn` | Refresh | Re-fetches status and caps (`app.js:222`) |
| `helpBtn` | HELP | Shows a pop-up text (`app.js:223`) |

### 2.6 Tab strip (`index.html:41-47`)

`<div id="tabs">` holds five `div.lf-tab`. The one with class `sel` is the selected look. Each has a `data-tab="..."` attribute (`radios`, `interfere`, `advertise`, `sniff`, `classic`). JS builds the section id from it: `"tab-" + data-tab`. They are `div`s, not buttons (see "Things I noticed", accessibility).

### 2.7 Content pane and the five sections (`index.html:49-469`)

All five `<section class="lf-section">` live in `div.lf-pane`. Only the one with the extra class `show` is displayed (CSS: `.lf-section {display:none}` / `.lf-section.show {display:block}`). Radios starts with `show`.

#### 2.7.1 Radios section, `id="tab-radios"` (`index.html:52-71`)

| id | What |
|---|---|
| `radiosTable` | Columns: Radio, State, Job, Kind, Uptime, Action. One row per radio; `<tbody>` is empty in the HTML and filled by `renderRadios()` |
| (note paragraph) | Explains one job per radio at a time and that jobs are independent processes |
| `historyTable` | Columns: Job, Radio, Kind, Started, Exit. Last finished jobs, newest first |

The caption text says "BE200 ... (hci0-hci7)" as fixed words even though the code detects the real radios at runtime.

#### 2.7.2 Interference section, `id="tab-interfere"` (`index.html:74-128`)

Group "2.4 GHz BLE Interference Generator (Direct Test Mode)":

| id | Control | Meaning |
|---|---|---|
| `if_dev_checks` | box of checkboxes (built by JS) | Which radios to run on. One independent job per ticked radio |
| `if_mode` | select | `single`, `sweep`, `hop`, `wifi`, `band`, `adv-flood` (what channel pattern to blast) |
| `if_arg_field` / `if_arg_label` / `if_arg` | text field, default `19` | The mode's argument. Its label and visibility change with the mode (`app.js:147`) |
| `if_payload` | select (empty until caps load) | Payload pattern |
| `if_phy` | select `1M`/`2M` | Radio PHY (the modulation speed) |
| `if_len` | text, `37` | Payload length 0-255 |
| `if_dwell` | text, `800` | Milliseconds spent on each channel |
| `if_power` | text, placeholder "default / max / min / dBm" | TX power |
| `if_bw` | select `20`/`40` | Wi-Fi bandwidth (used by `wifi` mode only) |
| `if_duration` | text, `0` | Seconds; 0 = until stopped |
| `if_map` | checkbox | "plan only (no transmission)" |
| `if_start` | primary button | Start on the selected radios |

Then a note, `ifJobsTable` (Job, Radio, Plan, Uptime, Action) and a black console `if_console`.

#### 2.7.3 Advertise section, `id="tab-advertise"` (`index.html:131-179`)

| id | Control | Meaning |
|---|---|---|
| `adv_dev` | select | Which radio (one only) |
| `adv_type` | select | PDU (packet) type: `ind`, `scan`, `nonconn` (default), `direct`, `direct-l` |
| `adv_interval` | text, `100` | Interval in ms |
| `adv_peer_field` / `adv_peer` | text | Peer address, only for the directed types |
| `adv_payload_row` | a whole row `div` | Wraps the four fields below so JS can grey them out together |
| `adv_profile` | select | Beacon profile; first option is `""` "(none - use name/flags below)"; the rest are added by `loadCaps()` |
| `adv_profile_cfg` | text | `k=v,k=v` profile parameters, e.g. `major=1,minor=1` |
| `adv_name` | text, `BluForge` | Local name when no profile is chosen |
| `adv_duration` | text, `0` | Seconds; 0 = until stopped |
| `adv_start` | primary button | Start Advertising |

Then a note, `advJobsTable` (Job, Radio, PDU, Uptime, Action) and console `adv_console`.

#### 2.7.4 BLE Sniff section, `id="tab-sniff"` (`index.html:182-258`)

Top group "Over-the-air BLE Advertising Survey":

| id | Control | Meaning |
|---|---|---|
| `sn_dev` | select | Radio to survey with |
| `sn_active` | checkbox, ticked | Active scan (also ask devices for names/scan responses) |
| `sn_coded` | checkbox | Include the LE Coded PHY (long range) |
| `sn_filter` | text | Capture filter: address or name contains this text (applied by the sniffer process, i.e. before the browser sees anything) |
| `sn_duration` | text, `0` | Seconds; 0 = until stopped |
| `sn_start` | primary button | Start Survey |

Second row of the same group, the **Classic inquiry**:

| id | Control | Meaning |
|---|---|---|
| `sn_cl_dev` | select | Radio to use for the BR/EDR inquiry (built by JS) |
| `sn_cl_secs` | text, `12`, 60 px wide | Seconds (JS clamps to 3-60) |
| `sn_cl_btn` | button | "Classic inquiry" |
| `sn_cl_status` | span | Progress/result message |

A note explains why: Classic devices do not advertise over BLE, only discoverable ones answer, and a device already connected to another host will not answer.

Filter row (above the table):

| id | Control | Meaning |
|---|---|---|
| `sn_search` | text | Free-text search over address, name, vendor, services |
| `sn_sort` | select | `rssi` (Strongest signal), `first`, `last`, `count`, `name` |
| `sn_hidestale` | checkbox | Hide devices not seen for 30 s |
| `sn_stats` | span | One-line statistics ("12 shown / 40 devices, 85 pkt/s") |
| `sn_clear` / `sn_exp_json` / `sn_exp_csv` | buttons | Clear the table / download JSON / download CSV |

Survey table `sniffTable`: nine columns: Address, Addr type, Name, Vendor / kind, RSSI, Pkts, Conn, Advertised services / data, Seen. Click a row to select a device.

Detail panel `sn_detail` (hidden until a device is selected; shown by an inline style toggled from JS):

| id | What |
|---|---|
| `sn_detail_title` | Heading "Device detail - ADDR (name)" |
| `sn_detail_body` | The decoded fields, rebuilt by JS |
| `sn_int_dev` | Radio for interrogating |
| `sn_int_read` | "read values" checkbox, ticked (read characteristic values, never write) |
| `sn_int_btn` | "Interrogate selected device" |
| `sn_int_status` | Progress message |
| `sn_int_out` | Where the interrogation result is drawn |

Below: a note, console `sn_console` (110 px high) and `snJobsTable` (Job, Radio, Uptime, Action).

#### 2.7.5 Bluetooth Classic section, `id="tab-classic"` (`index.html:262-467`)

All ids start with `c_`. `app.js` never touches them; `classic.js` does. Layout, for orientation only:

| Block (fieldset legend) | Contents |
|---|---|
| (banner) `c_pf_banner` | Preflight warning strip |
| Classic radio (BR/EDR) | `c_dev` radio select, `c_radio_state`, `c_reserve` button, `c_adapter` |
| Devices | `c_scan_dur`, `c_scan`, `c_list`, `c_pin`, `c_addr` (selected DUT), table `c_devTable` |
| Device under test | `c_pair`, `c_connect`, `c_disconnect`, `c_remove`, `c_info`, `c_autoinfo`; a readout grid `c_d_*` |
| A2DP stream test | `c_a2_*` fields, `c_a2_start` |
| AVRCP | `c_vol_steps`, `c_vol_test`, six `data-plr` buttons, `c_keys` checkboxes, `c_w_dur`, `c_w_start` |
| HFP hands-free call | `c_hf_*` fields, `c_hf_start` |
| Connection reliability | `c_cy_*` fields, `c_cy_start` |
| (result box) `c_result` | PASS/FAIL summary |
| Live metrics | `c_l_*` readouts, two `<canvas>` charts `c_ch_rssi`, `c_ch_kbps` |
| tables | `c_jobsTable` (Classic jobs), `c_resTable` (Saved results), console `c_console` |

### 2.8 Status bar (`index.html:471-476`)

`statusHost` (hostname, default "this unit"), `statusJobs` ("0 jobs running" until the first render), and the fixed text "BE200 BLE Lab v1".

### 2.9 Things to know about the HTML

- **Empty tables on purpose.** Every `<tbody>` is empty. JS fills them from the server data.
- **Header counts must match JS.** For instance the survey table has nine `<th>` and the JS builds nine `<td>` per row (`app.js:483`). Change one without the other and columns shift.
- **Inline styles.** Several elements use `style="..."` (the two `display:none` toggles and some margins). JS flips those inline styles directly for the banner and the detail panel, instead of toggling a class.
- **Field labels are not linked to their inputs** (`<label>` without `for`), except for the checkbox labels that wrap their input. See accessibility notes at the end.
- **Hard-coded words**: "hci0-hci7", "BE200", "8", the hostname placeholder. They contradict the detect-at-runtime design described in the comment at `app.js:7-13`.

---------------------------------------------------------------------

## 3. style.css

The header comment (`style.css:1-6`) says the tokens and recipes were copied from a file `recon/02_lanforge_gui_style.md`, derived from the real LANforge Java client. That file is not part of what I read, so I cannot verify that claim.

### 3.1 Design tokens: the CSS variables (`style.css:8-56`)

A CSS variable is declared inside `:root { ... }` (the page's top element), so it is available everywhere, and used as `var(--name)`.

| Group | Variables | Used for |
|---|---|---|
| Base greys and borders | `--lf-control` (#eee page background), `--lf-text` (#333), `--lf-text-disabled`, `--lf-border` (#7a8a99), `--lf-border-soft` (#b8cfe5) | Page background, text, control and box borders |
| "Ocean" blues | `--lf-primary1` (#6382bf), `--lf-primary2` (#a3b8cc), `--lf-primary3` (#b8cfe5), `--lf-tab-selected` (#c8ddf2), `--lf-tab-area` (#dadada) | Selected tab, pane border, active button, focus ring |
| Menu bar | `--lf-menubar-top/-bottom/-rule` | Gradient and underline of the menu bar |
| Button gradient | `--lf-btn-top/-mid/-bottom` | The three stops of the button gradient |
| Table | `--lf-table-bg`, `--lf-table-grid`, `--lf-table-header-bg`, `--lf-select-bg`, `--lf-select-fg` | Cell background, gridlines, header |
| Row state colours | `--lf-row-mgt-port` (light cyan: a running row), `--lf-row-pass` (green), `--lf-row-inuse`, `--lf-row-info`, `--lf-row-warn` (yellow), `--lf-row-error` (red) | Status dot, pills, running/error rows |
| Others | `--lf-modified-field`, `--lf-tab-alert`, `--candela-green`, `--candela-gold` | Orange "changed" field, yellow alert tab, brand colours |
| Fonts | `--lf-font` (Noto Sans, DejaVu Sans...), `--lf-font-brand`, `--lf-mono` (Liberation Mono...) | Text, brand, console |
| Sizes | `--lf-fs: 12px`, `--lf-row-h: 19px`, `--lf-btn-h: 27px`, `--lf-menubar-h: 22px`, `--lf-tab-h: 20px`, `--lf-status-h: 24px` | Base font size and bar heights |

**Declared but never used** (I searched the CSS, `index.html`, `app.js`, `classic.js`): `--candela-gold`, `--candela-green`, `--lf-font-brand`, `--lf-row-info`, `--lf-row-inuse`, `--lf-select-fg`, `--lf-text-on-select`, `--lf-white`. `--lf-select-bg` is not used in CSS but is used by `app.js:498` as an inline style to highlight the selected survey row.

Many colours are *not* tokens and are typed directly into rules (`#0082fc`, `#f4f8fb`, `#456`, `#0b0e13`...). The tokens cover the base chrome only.

### 3.2 Rule groups, in file order

**Reset and page frame (`:58-64`)**
- `* { box-sizing: border-box }`: width/height include padding and border, which makes sizing predictable.
- `html, body`: no margin, 100% height, page background, and the shorthand `font: 12px/1.3 <font stack>` (size / line-height / family).
- `body { display:flex; flex-direction:column }`: the page is a vertical stack (title band, banner, menu, actions, tabs, pane, status). The pane has `flex:1`, so it takes all leftover height and scrolls inside itself (`overflow:auto`) while the title band and status bar stay put.

**Title band (`:66-84`)**: a flex row, 34 px high. `@font-face` loads the bundled font "Exo 2" bold from `/assets/exo2-700.woff2` (`font-display: swap` = show fallback text first, swap when loaded). `.title` is 20 px blue. `.spacer { flex:1 }` is a generic "push everything after me to the right" element used all over. `.lf-dot` is a 9 px circle; it is red (`--lf-row-error`) and `.lf-dot.up` is green.

**Menu bar (`:86-94`)**: a 22 px gradient strip; `span` children with padding and a hover highlight.

**Action row and buttons (`:96-108`)**: `.lf-actions` right-aligns buttons. `.lf-btn` is the standard button: 27 px high, min 63 px wide, square corners, 3-stop gradient. States: `:hover` (slightly dimmer), `:active` (flat blue), `:disabled` (grey, `not-allowed` cursor). Variants: `.danger` (reddish border), `.primary` (blue border, bold).

**Tabs (`:110-123`)**: `.lf-tab` is a bordered label with no bottom border; `margin-right:-1px` makes neighbouring borders overlap into one line. `.lf-tab.sel` gets a thicker blue top border, a lighter background, bold text, and is 1 px shorter with `padding-top:0` so it looks "raised". `.lf-tab.alert` (yellow) is defined, but nothing in `index.html`, `app.js` or `classic.js` sets it.

**Content pane (`:125-133`)**: bordered box with inset shadows to imitate the Metal look; `.lf-section {display:none}` and `.lf-section.show {display:block}` are the whole tab-switching mechanism on the CSS side.

**Forms (`:135-153`)**: `.lf-group` is the bordered fieldset box; its `legend` is small bold blue-grey. `.lf-row` is a flex row that wraps (`flex-wrap`) with `align-items:flex-end` so inputs line up at the bottom even when labels differ. `.lf-field` is a label stacked over a control. `.lf-input` (and `select.lf-input`) is 22 px high, white. `.lf-input:focus` shows a 1 px dotted pale outline. `.lf-input.changed` (orange) exists but nothing sets it. `.lf-check` aligns a checkbox with its text. `.lf-radio-checks` is the white bordered box holding the radio checkboxes; `label.busy` and `input[disabled] + span` grey out busy radios (this relies on the markup `label > input + span` that `populateInterfereChecks()` builds).

**Tables (`:155-180`)**:
- `.lf-tablewrap` (white bordered box, `overflow:auto` so a wide table scrolls inside it) with a centred `.cap` caption bar.
- `table.lf-table`: collapsed borders, full width, 12 px. `th` has the light header look (white top/left borders, grey bottom/right, to imitate a raised cell). `td` is `nowrap` (never wraps), so long text widens the table and scrolls.
- `td.num` right-aligns and uses tabular figures so digits line up.
- Row states set on the `<tr>`: `.idle` grey text, `.running` light-cyan cells, `.err` red cells with white text. (`.pass` and `.fail` are further down, in the Classic block.)
- `.mini-btn`: the small "Stop" button inside a table cell. (Note: it is a different class from `.lf-btn.mini`, and the sheet styles them separately.)
- `.pill`: a small rounded label. `.ok` green, `.busy` yellow, `.off` grey. Used by the Radios table state column.

**Console (`:181-190`)**: `.lf-console` is a 220 px high, dark box with monospace 11 px text, `white-space: pre-wrap` (keep line breaks, wrap long lines), scrollable. Child classes are colour codes JS puts on each line: `.s` blue (info `[*]`), `.g` green (`[+]`), `.y` yellow (`[!]`), `.r` red (`[x]`), `.dim` grey (everything else).

**Status bar (`:192-197`)**: 24 px flex row at the bottom.

**Misc (`:199-202`)**: `.note` small grey help text; `a.lf-link` (used once, in `classic.js`); `.hidden { display:none !important }`. The `!important` is deliberate: it must beat rules like `display:flex` on the same element.

**Small-screen rule (`:204-208`)**: below 760 px width, form rows become columns, fields lose their minimum width, and table wrappers scroll sideways. It is the only responsive rule.

**Bluetooth Classic block (`:210-233`)**: `.c-banner` (yellow warning strip, `.bad` red), `.c-readout` (white read-only box), `.c-grid` (CSS Grid: `repeat(auto-fill, minmax(150px, 1fr))` = "as many 150 px-or-wider columns as fit"), `.c-btns`, `.lf-btn.mini` (smaller button), `.c-result` (PASS = green `.pass`, FAIL = red `.fail`), `.c-charts` (two-column grid of canvases), table row colours `.pass`/`.fail`, `td.wrap` (allow wrapping, max 420 px). Notes: `.c-readout` is declared twice (`:214` and `:233`; the second adds `display:flex`); `.lf-btn.mini[disabled], .lf-btn[disabled]` (`:232`) dims to 60% in addition to `.lf-btn:disabled`; the charts' CSS height is 110 px (`:228`) while the `<canvas>` elements are `width=520 height=130` in the HTML, so the drawing is scaled to fit (I did not check how `classic.js` draws).

**BlueZ banner (`:235-238`)**: `.lf-banner` yellow with an amber bottom border, `.lf-banner-row` flex row, `.lf-banner-note` smaller text up to 900 px wide. It uses `style.display` from JS, not `.hidden`.

---------------------------------------------------------------------

## 4. app.js, in file order

### 4.0 Shape of the file

Everything is inside one IIFE, `(function () { "use strict"; ... })();` (`app.js:4` to `app.js:829`). Nothing leaks into the page's global scope except `window.BF` (`app.js:19`). The whole file is plain callbacks and `async/await`; there is no framework, so "rendering" means: read data, then create or edit DOM elements by hand.

The file is organised as: (1) shared state, (2) small helpers, (3) tabs, (4) filling forms from the server's capabilities, (5) form handlers and the Start/Stop buttons, (6) rendering of the status tables, (7) BlueZ banner, (8) status polling, (9) consoles, (10) the sniff survey, (11) the live SSE connection, (12) start-up.

### 4.1 Shared state (`app.js:14-26`)

| Name | Line | Type | Meaning |
|---|---|---|---|
| `ALL_HCI` | 14 | array of numbers | The radio indices present on this machine, e.g. `[0,1,2,3]`. Starts empty. Set from `/api/caps` (`:132`) and again from every status snapshot (`:360`) |
| `CAPS` | 15 | object or null | "Capabilities" from `/api/caps`: payload pattern names, beacon profile names, hardware list |
| `STATE` | 16 | object | The most recent status snapshot from the server |

The comment at `app.js:7-13` explains a past bug: radios were once hard-coded as 0-7, which showed non-existent radios on a 4-radio unit.

**Shape of `STATE`** (what `/api/status` returns; see `Jobs.snapshot()` in `server.py`):

```
{ running:  [ job, ... ],        // jobs still alive
  finished: [ job, ... ],        // the last 20 finished
  available_hci: [0,2,...],      // radios free for a BLE job (not busy, not reserved)
  all_hci:       [0,1,2,3],
  classic_reserved: [1],         // radios handed to BlueZ
  hostname: "ct523c-ccb8",
  bluez: { installed, installing, can_install, notice } }
job = { id:"job3", kind:"interfere"|"adv"|"sniff"|"c_a2dp"|..., dev:0, hci:"hci0",
        running, started (epoch s), stopped, uptime (s or null), exit_code }
```

Note there is **no `argv` field** in a job. This matters for finding 1 in the last section.

### 4.2 The BF object and `fireHooks` (`app.js:17-26`)

`BF` ("BluForge") is the *only* door between `app.js` and `classic.js`. It is stored on `window` so the other script can find it:

```js
const BF = window.BF = {
  hooks: [],
  state: function () { return STATE; },
  refresh: function () { return refreshStatus(); },
};
```

- `BF.hooks`: a list of functions that `classic.js` pushes itself onto (`classic.js:486`).
- `BF.state()`: returns the *current* `STATE`. Because `STATE` is replaced wholesale on each refresh (`:357`), a caller must call `BF.state()` each time rather than keep the result.
- `BF.refresh()`: lets `classic.js` trigger an immediate status refresh.

`fireHooks(evt, kind, dev)` (`:24-26`) calls every hook with the event plus the job's `kind` and `dev` already worked out. Each call is wrapped in `try/catch` so a bug in `classic.js` only produces a console warning and cannot break `app.js`. Where it is called is covered in 4.14.

### 4.3 Utility helpers (`app.js:28-52`)

- **`$(id)`** (`:29`): short for `document.getElementById(id)`. Used about everywhere.
- **`el(tag, cls, text)`** (`:30-35`): creates an element, optionally sets its CSS class and its text. It uses `textContent`, so the text can never be interpreted as HTML. This helper is why `app.js` is safe against XSS (see last section).
- **`fmtUptime(job)`** (`:36-40`): if `job.uptime` is a number, returns it with one decimal and `s` (`"12.3s"`); otherwise `"stopped"` if `job.stopped` is set; else `"-"`.
- **`api(path, opts)`** (`:41-49`), the single place that talks HTTP with `fetch`:
  1. `await fetch(path, opts)`.
  2. If the status code is not 2xx (`!r.ok`): try to read the JSON body and take `j.error` or `j.msg` as the message (falling back to the HTTP status text), then `throw new Error(msg)`.
  3. Otherwise return `r.json()` (the parsed reply).
  So callers get *either* data *or* an exception whose message is the server's explanation. Consequence: a refused `/api/start` (HTTP 409) arrives as an exception, not as `{ok:false}`.
- **`post(path, body)`** (`:50-52`): `api()` with `method:"POST"`, JSON header and `JSON.stringify(body)`.

### 4.4 Tabs (`app.js:54-62`)

```js
document.querySelectorAll(".lf-tab").forEach(function (tab) {
  tab.addEventListener("click", function () {
    ...remove "sel" from every tab, remove "show" from every section...
    tab.classList.add("sel");
    $("tab-" + tab.dataset.tab).classList.add("show");
  });
});
```

Step by step: find all five tab elements; give each a click listener; on click clear the highlight from all tabs and hide all sections, then highlight the clicked tab and reveal the section whose id is `tab-` plus the tab's `data-tab` value (`dataset.tab` reads the HTML attribute `data-tab`). The choice is not stored anywhere; a reload returns to Radios. The tabs do not trigger any data load; everything is already kept fresh in the background. `classic.js:260` relies on the `show` class to know whether the Classic tab is visible.

### 4.5 Filling the forms from the server (`app.js:64-140`)

**`fillSelect(sel, items, labelFn)`** (`:65-73`): empties a `<select>` and adds one `<option>` per item. An item may be a string or an object with `.value`. `labelFn` (optional) makes the visible text. Only one caller (`:133`, for payload patterns), and no caller passes `labelFn`.

**`populateDevSelects()`** (`:75-94`) fills the two radio drop-downs (`adv_dev`, `sn_dev`). For each id:
1. Remember the current value (`prev`).
2. Empty the select; for each radio in `ALL_HCI` add an option labelled `hciN  (idle)`, `hciN  (busy)` or `hciN  (Classic)` (when it is in `STATE.classic_reserved`). If the radio is not in `STATE.available_hci`, the option is `disabled` (greyed, cannot be picked by the user).
3. Put the previous choice back if that option still exists; otherwise pick the first non-disabled option.
Finally (`:93`) it calls `populateInterfereChecks()`.
Observation: step 3 restores `prev` even if it just became busy/disabled, so after you start a job on the radio you had selected, the drop-down keeps showing that now-busy radio.

**`populateInterfereChecks()`** (`:96-116`) rebuilds the radio checkboxes of the Interference tab (`if_dev_checks`):
1. Collect the values of the boxes the user already ticked into a `Set` (so a refresh does not untick them).
2. Empty the container; for each radio create `<label class="busy?"><input type=checkbox value=N disabled?> <span>hciN (busy)</span></label>`.
3. Re-tick the remembered ones. A busy radio is `disabled` but can stay ticked.

**`selectedInterfereDevs()`** (`:118-127`) returns the ticked radios as numbers, using the CSS selector `input[type=checkbox]:checked:not(:disabled)`. The `:not(:disabled)` is deliberate (the comment at `:119-124` explains a bug where already-running radios were re-submitted and returned a false 409 alarm).

**`loadCaps()`** (`:129-140`), an `async` function:
1. `CAPS = await api("/api/caps")`. (The server runs `bluforge_cli.py caps --compact` once and caches the result.)
2. If the reply has an `error` field, log a warning and stop.
3. If `CAPS.hardware.all_hci` is an array, store it in `ALL_HCI`.
4. Fill `if_payload` with `CAPS.interfere.payloads`.
5. For each name in `CAPS.adv.profiles`, **append** an `<option>` to `adv_profile`.
Step 5 only appends. It never clears the list first, so calling `loadCaps()` twice duplicates every profile (see finding 5).
There is no `try/catch`: if `/api/caps` fails, `loadCaps()` rejects, and nothing at its two call sites (`:222`, `:826`) catches that.

### 4.6 Form behaviour: Interference mode and Advertise type (`app.js:142-161`)

**`IF_ARG_LABELS`** (`:143-146`) is a lookup of label text per interference mode: `single` "Channel (0-39)", `sweep` "Range a-b (e.g. 0-39)", `hop` "Channel list (e.g. 0,12,24,39)", `wifi` "Wi-Fi channel (1-13)", `band` and `adv-flood` empty.

**`if_mode` change listener** (`:147-152`): a *change* event fires when the user picks a different option. If the mode is `band` or `adv-flood` (no argument needed) it hides the argument field by adding the `hidden` class; otherwise it shows it and rewrites `if_arg_label` from `IF_ARG_LABELS`. It does not change the field's *value*: after switching from `single` to `sweep` the text box still says `19` until you edit it. The listener is not fired at load because the default (`single`) already matches the HTML.

**`adv_type` change listener** (`:155-160`): `directed` is true when the value starts with `direct`. If directed: show the Peer address field (it toggles `visibility`, which keeps its space in the layout), dim `adv_payload_row` to 40% opacity, and set `disabled` on every `input`/`select` in that row. If not directed: the reverse. Line `:161` fires a synthetic `change` event once at load so the initial look is right (default `nonconn`, so the peer field starts hidden). Note `adv_duration` lives *inside* `adv_payload_row`, so choosing a directed type also disables the Duration box, yet `adv_start` still sends its value (see finding 10).

### 4.7 Start and stop actions (`app.js:163-234`)

**Start Interference `if_start` click** (`:164-190`), an `async` listener:
1. `devs = selectedInterfereDevs()`. If none, `alert(...)` and stop. (`alert` is the blocking browser pop-up.)
2. Build a `params` object from the form: `mode`, `arg`, `bw`, `len`, `payload`, `phy`, `dwell`, `power` (empty becomes `null`), `duration`, `map` (boolean). All values go as *strings*; the server validates them.
3. For every selected radio, `POST /api/start` with `{tool:"interfere", dev, params}`. All requests are launched together with `Promise.all(...)`. Each one has its own `.catch` that turns an exception into `{ok:false, msg}`, so one radio failing cannot cancel the others.
4. Collect failures; if any, one `alert` listing `hciN: message` lines.
5. `refreshStatus()` (not awaited).

**Start Advertising `adv_start` click** (`:192-203`): builds `params` (`type`, `interval`, `peer`, `name`, `profile`, `profile_cfg`, `duration`; empty peer/profile become `null`), then `await post("/api/start", {tool:"adv", dev: $("adv_dev").value, params})`. `if (!r.ok) throw ...` is effectively redundant: `api()` has already thrown for any non-2xx reply, and the server only answers 200 when `ok` is true. On error `alert("Could not start: ...")`. It does not call `refreshStatus()`; the SSE "started" event does that.

**Start Survey `sn_start` click** (`:205-214`): same pattern with `tool:"sniff"` and params `active`, `coded`, `filter` (empty becomes `null`), `duration`.

**Stop All `stopAllBtn` click** (`:216-221`): `post("/api/stop_all", {})`; a failure is shown with `alert`. The comment explains it used to fail silently on this one panic button.

**Refresh `refreshBtn` click** (`:222`): `refreshStatus(); loadCaps();`. It calls `/api/caps` without `?refresh=1`, so the server returns its cached copy and does not rescan.

**HELP `helpBtn` click** (`:223-228`): an `alert` with a paragraph of help text (it says "all 8 BE200"; hard-coded).

**`stopJob(jobId)`** (`:230-234`): `POST /api/stop {job_id}`. This route replies HTTP 200 even when it failed, with `{ok:false,msg}`, so unlike the start buttons it checks `r.ok` and shows `alert("Stop failed for ...")`. A network exception is also alerted.

### 4.8 Rendering the status tables (`app.js:236-314`)

"Rendering" here = throw away the old table rows and build new ones from `STATE`. (The survey table is the exception, 4.11.)

**`renderRadios()`** (`:237-275`):
1. *Radios table*: clear `<tbody>`; build `byDev`, a lookup from radio number to its running job. For each radio in `ALL_HCI`, make a row with class `running` or `idle`: name `hciN`; a state **pill** (`RUNNING` yellow, `CLASSIC (reserved)` grey, or `idle` green); job id; kind; uptime; and a **Stop** button (only if running) whose click calls `stopJob(j.id)`.
2. *History table*: clear; for `STATE.finished` **copied and reversed** (`.slice().reverse()`, newest first, without altering `STATE`), make rows: job id, `hciN`, kind, start time as local time (`toLocaleTimeString`, since `started` is in seconds, it multiplies by 1000), exit code. A row is class `idle` if `exit_code === 0`, else `err` (red).

**`renderKindTable(tableId, kind, cols)`** (`:277-290`): a generic builder for the three job tables. It keeps only `STATE.running` jobs of that `kind`, and per job builds a row of class `running` with one cell per entry in `cols` (each entry is `{fn, num?}` where `fn(job)` gives the text and `num` right-aligns) plus a final Stop-button cell.

**`render()`** (`:292-314`), the one function that repaints everything from `STATE`:
1. `renderRadios()`.
2. Three `renderKindTable` calls: `ifJobsTable` (columns: id, `hciN`, "plan", uptime), `advJobsTable` (same), `snJobsTable` (id, `hciN`, uptime).
3. `populateDevSelects()` (which also rebuilds the checkboxes).
4. `BF.hooks.length && fireHooks({type:"render"}, null, null)`: if any hook is registered, tell `classic.js` to repaint too. (`a && b()` is a shorthand for `if (a) b()`.)
5. `statusJobs` text = `N job(s) running`.

The "plan" column uses `(j.argv || []).slice(4).join(" ")` (`:297`, `:303`). As noted, jobs carry no `argv`, so this is always empty (finding 1).

### 4.9 Status refresh, the BlueZ banner and `applyState` (`app.js:316-370`)

**`_statusReqSeq`** (`:323`): a counter used to ignore stale replies. The comment (`:316-322`) explains: starting 3 radios fires 3 near-simultaneous "started" events, each calling `refreshStatus()`. Replies could arrive out of order, so an old snapshot could overwrite a newer one and make a running job flicker to "idle".

**`refreshStatus()`** (`:362-370`), `async`:
1. `const myReq = ++_statusReqSeq` (take a ticket number).
2. `const s = await api("/api/status")`.
3. `if (myReq !== _statusReqSeq) return;`: if a newer request was issued meanwhile, drop this reply.
4. `applyState(s); render();`.
5. Any error is swallowed silently (`catch (e) { }`): "server not reachable yet". The page does not show that status calls are failing.

**`applyState(s)`** (`:356-361`): `STATE = s` (replace the whole snapshot); `applyBluez(s.bluez)`; if `s.hostname` is present, write it to `hostInfo` and `statusHost`; if `s.all_hci` is an array, update `ALL_HCI`.

**Polling schedule**: `refreshStatus` runs on a 5-second timer (`:828`), after every SSE `status` event (`:801`), after a BlueZ install finishes (`:807`), after the Classic-reserve steps (`:614`, `:713`), from the Refresh button, and via `BF.refresh()`.

**BlueZ banner**

BlueZ (the Linux Bluetooth software) is installed by the end user. If it is missing, the server refuses every Bluetooth action (HTTP 409 with `bluez_missing:true`) and the page shows a banner with an installer button.

- **`bluezBusy`** (`:326`): a boolean, true between clicking Install and the installer finishing; keeps the button disabled.
- **`applyBluez(b)`** (`:327-336`), called by every `applyState`:
  1. If `b` is missing, do nothing.
  2. `show = !b.installed`; set `banner.style.display` to `""` (visible) or `"none"`.
  3. If it should be hidden: `bluezBusy = false` and return.
  4. Otherwise write `b.notice` into `bluezNotice` and set the button `disabled` if `bluezBusy || b.installing || !b.can_install`. If the installer script is missing (`can_install` false) the button's tooltip says so.
- **`bluezInstallBtn` click** (`:337-347`), `async`:
  1. Read the notice text and ask `window.confirm(notice + "\n\nInstall BlueZ now?")` (OK/Cancel). Cancel stops everything.
  2. Show the log box (`bluezLog`), clear it, set `bluezBusy = true`, disable the button.
  3. `await post("/api/bluez/install", {accept:true})`. The server starts the installer in the background and returns immediately.
  4. If that request throws, reset `bluezBusy`, re-enable the button and print `[x] message` in the log.
  The installer's output then arrives later as SSE `bluez` events (4.12).
- **`appendBluezLine(t)`** (`:348-354`): shows the log box, adds one `<div>` with a colour class chosen by prefix (`[x]` red `r`, `[+]` green `g`, `[!]` yellow `y`, otherwise grey `dim`), and scrolls to the bottom.

When the installer finishes, the SSE handler (`:803-808`) clears `bluezBusy`, prints a final line (`[+] BlueZ is ready - the page is unlocked.` or `[x] BlueZ installation did not complete - see the log above.`), and calls `refreshStatus()`. That fetches a new `bluez.installed` value, and `applyBluez` hides the banner.

### 4.10 Consoles (`app.js:372-396`)

**`consoleFor(kind)`** (`:373-375`): `interfere` gives `if_console`, `adv` gives `adv_console`, `sniff` gives `sn_console`, anything else `null` (Classic kinds are not handled here).

**`appendConsoleLine(kind, dev, text)`** (`:382-396`):
1. Find the console; return if none.
2. Create a `<div>`; its CSS class comes from the text prefix: `[*]` `s` blue, `[+]` `g` green, `[!]` `y` yellow, `[x]` `r` red, else `dim`.
3. Text = `"hciN: " + text` when `dev` is known (so lines from several radios can be told apart, see comment `:376-381`).
4. Append, scroll to the bottom (always, even if you had scrolled up to read), and while there are more than 300 children delete the oldest (`:395`).

### 4.11 The BLE sniff survey (`app.js:398-777`)

#### 4.11.1 The idea

The sniffer process on the server sees thousands of advertising packets per second. It does not send them one by one. It keeps a record per device, and **once per second** it emits only the devices that changed, plus a statistics line. The server forwards that as one SSE event `sniff` per second per sniff job. So the browser:

1. **Ingests** the records into a `Map` keyed by address (`snIngest`).
2. **Repaints** the table on a 1-second timer (`renderSniffTable`), with no per-packet DOM work.

The device ages shown in the "Seen" column are computed by the browser from its own stopwatch (`performance.now()`), so they keep ticking between updates.

#### 4.11.2 Sniff state (`app.js:402-404`)

| Name | Type | Meaning |
|---|---|---|
| `snDevs` | `Map` addr to record | Every device known. Each record is the server's object plus a browser-added `_rx` field: the `performance.now()` time of its last sighting (in milliseconds on the browser's stopwatch) |
| `snRows` | `Map` addr to `{tr, cells}` | The `<tr>` element and its nine `<td>`s that show a device; reused between repaints |
| `snDirty` | boolean | "something changed". Set in many places, **never read** (finding 2) |
| `snSelected` | address or null | The device whose detail panel is open |
| `snClearedAt` | number (epoch s) | Time of the last Clear, in the *browser's* clock |
| `snStats` | object | Latest statistics line from the sniffer (`rate`, `idle_s`, `evicted`...). A single global: with two radios surveying, whichever reported last wins |

#### 4.11.3 `snIngest(devs, stats, serverNow)` (`:406-417`)

For each incoming device record `d`:
1. Skip it if `d.last_seen < snClearedAt` (it was last heard before you pressed Clear).
2. Skip it if the stored copy is newer (`prev.last_seen > d.last_seen`), which protects against out-of-order arrivals.
3. Work out `_rx`:
   ```js
   d._rx = serverNow ? nowMs - Math.max(serverNow - d.last_seen, 0) * 1000 : nowMs;
   ```
   Meaning: "the server said this device was last heard `serverNow - last_seen` seconds ago, so on my stopwatch that was that many seconds before now". Because both numbers come from the server's clock, the result does not depend on whether the browser's clock agrees with the server's.
4. `snDevs.set(d.addr, d)`: insert or replace. Replacing the value of an existing key keeps the key's original position in the `Map`, which matters for stable sorting (below).
Then store `stats` if given and set `snDirty = true`.

Callers: the SSE `sniff` handler (`:810`), `snResync` (`:423`), and the Classic inquiry (`:642`, with `stats = null`).

#### 4.11.4 `snResync()` (`:419-425`)

`fetch("/api/sniff/devices")` (raw `fetch`, not `api()`), then for each radio's sniff snapshot (`j.sniffs`) calls `snIngest(sn.devices, sn.stats, j.now)`. It is called on every SSE (re)connect (`:791`), so a freshly opened page, or one that was disconnected for a while, rebuilds the whole table in one step instead of waiting for devices to talk again. Errors are ignored because the stream will bring the next update.

#### 4.11.5 Small formatting helpers (`:427-463`)

- **`snAgo(d)`** (`:427`): seconds since `d._rx` (never negative).
- **`snAgoText(d)`** (`:428-431`): `"now"` under 2 s, `"N s ago"` under 90 s, else `"N m ago"`.
- **`snSummary(d)`** (`:432-442`): builds the "Advertised services / data" cell text: up to 4 service names (or UUIDs if no name) plus `"+N more"`; any beacon type not already in the manufacturer formats; each manufacturer-data format; "data NAME" for service data that was not decoded; the appearance text. Joined with `", "`.
- **`snKindText(d)`** (`:443-446`): the "Vendor / kind" cell: the company name (or `mfg 0xNNNN` from the first manufacturer id, hex padded to 4 digits); if there is also a `kind` that does not already contain the company text, `"company - kind"`.
- **`snRssiText(d)`** (`:447-449`): `"-60"` or `"-60 (avg -62)"`; empty if no RSSI.
- **`snVisible(d)`** (`:450-456`): the filter. If "hide not seen for 30 s" is ticked and the device's age exceeds 30 s, hide it. Otherwise, with an empty search box, show it; with text, lower-case it and look for it inside a haystack of address, name, company, kind, summary text and "supports" list.
- **`SN_SORTS`** (`:457-463`): five comparison functions keyed by the select's values:

  | key | order |
  |---|---|
  | `rssi` | strongest first (missing RSSI counts as -999) |
  | `first` | oldest `first_seen` first |
  | `last` | most recently heard first (`_rx` descending) |
  | `count` | most packets first |
  | `name` | alphabetical on name; unnamed devices use `"~"` as a stand-in, then address as tie-breaker. See finding 4: `"~"` actually sorts *first* |

- **`snSetCell(row, i, text)`** (`:465-468`): writes `textContent` of cell `i` only if it differs. Skipping no-op writes avoids needless browser work and keeps any text you are selecting.

#### 4.11.6 `renderSniffTable()` (`:470-514`), the heart of the tab

Step by step:
1. **Pick and order**: `Array.from(snDevs.values()).filter(snVisible).sort(SN_SORTS[select value] || SN_SORTS.rssi)`.
2. **Stable sorting**: modern JS guarantees `sort` is *stable* (items that compare equal keep their previous relative order). The array starts in `Map` insertion order (= order of first sighting), so devices with equal RSSI or equal name never swap places randomly from one second to the next.
3. **Row reuse**: for each device, `snRows.get(addr)`. If there is none, create the `<tr>` once: pointer cursor, a click listener (`snSelected = d.addr; snDirty = true`), and nine empty `<td>`s (columns 4 and 5, RSSI and Pkts, get class `num`). It is stored in `snRows`. Rows are never rebuilt; they are only edited.
4. **Fill cells** with `snSetCell` (`:487-497`):

   | col | content |
   |---|---|
   | 0 Address | `addr` |
   | 1 Addr type | `"classic"`, else `addr_kind` (or `addr_type`) with `"random-"` removed and `" (RPA)"` shortened to `" RPA"` |
   | 2 Name | `name` or empty |
   | 3 Vendor / kind | `snKindText` |
   | 4 RSSI | `snRssiText` |
   | 5 Pkts | `"-"` for Classic, else the packet count |
   | 6 Conn | Classic: `connected`/`classic`; BLE: `directed` / `yes` (connectable) / `no` |
   | 7 Advertised services / data | `snSummary` |
   | 8 Seen | `snAgoText` |

5. **Selection highlight**: background `var(--lf-select-bg)` if it is the selected device, else cleared (`:498`).
6. **Order with the fewest DOM moves** (`:499-502`):
   ```js
   const want = prevNode ? prevNode.nextSibling : tbody.firstChild;
   if (want !== row.tr) tbody.insertBefore(row.tr, want);
   prevNode = row.tr;
   ```
   Walk the sorted list; the row that belongs at this position is the node right after the previous placed row (or the first child). If that is already our row, do nothing; otherwise move our row there. A table whose order did not change costs zero DOM operations.
7. **Remove rows** for devices not in the visible list (`:504-506`).
8. **`snFillRadios()`** (`:507`) so the radio pickers are filled even before any device is selected.
9. **Stats line** (`:508-512`): `"{shown} shown / {total} devices"`, plus `", N pkt/s"` if the stats have a `rate`, plus `", no packets for Ns"` if the sniffer has been silent for more than 5 s, plus `", N evicted"` (the sniffer discards the stalest devices past its limit; the browser itself never evicts).
10. **`renderSniffDetail()`**.

The repaint is driven by `setInterval(function () { renderSniffTable(); snDirty = false; }, 1000)` (`:777`). It runs every second **whether or not anything changed** and even when the tab is hidden, because `snDirty` is never tested.

#### 4.11.7 The detail panel: `renderSniffDetail()` (`:516-563`)

1. Look up `snDevs.get(snSelected)`. If none, hide `sn_detail` and return.
2. Show it, fill the radio pickers (`snFillRadios()`), draw the interrogation output (`snRenderInterro()`), set the Interrogate button's `disabled` from `snIntBusy`, and set the title to `Device detail - ADDR  (name)`.
3. Empty `sn_detail_body` and rebuild it. A local helper `sect(title, lines)` adds a bold blue heading and then indented text lines, and skips empty sections. Sections, in order: Identity (address with kind, name and "(shortened)", vendor/kind, appearance); "What it advertises it supports"; Classic device **or** Radio (RSSI last/avg/min/max, packets and rate, PDU types with counts, PHY and extended-advertising SID, advertised Tx power); Flags; Services (advertised); Service data (with any decoded JSON); Manufacturer data; Beacon; URI; Raw advertising data.

All the text is set through `el()`/`textContent`. The panel is rebuilt from scratch on **every** tick (finding 8).

#### 4.11.8 Radio pickers: `snFillRadios()` and `snFillRadioSelect(sel)` (`:569-589`)

Used for the two selects `sn_int_dev` (interrogate) and `sn_cl_dev` (Classic inquiry).
1. Build the set of radios that have a running job (of *any* kind, despite the variable name `busyBle`).
2. `opts` = the radios in `ALL_HCI` that are not busy.
3. Make a `key` string from the option list plus the reserved list. If it equals `sel.dataset.key` (a custom data attribute used as a cache), return: nothing changed, so the drop-down is not rebuilt and an open list does not collapse.
4. Otherwise rebuild: option text is `hciN (ready)` if that radio is already reserved for Classic, else `hciN (will reserve)`. Restore the previous selection.

So both buttons reserve the radio for BlueZ automatically if needed.

#### 4.11.9 Classic (BR/EDR) inquiry into the same table (`:591-650`)

**`COD_MAJOR`** (`:592`) and **`COD_AV`** (`:594`): lookup tables for the Bluetooth "Class of Device" number: major class (Computer, Phone, Audio/Video...) and, for Audio/Video, the minor class (Headphones, Loudspeaker...).

**`snCodText(cls)`** (`:598-604`): parse the class (a hex string) with `parseInt(cls, 16)`; the major class is bits 8-12 (`(v >> 8) & 0x1f`), the minor class bits 2-7 (`(v >> 2) & 0x3f`); return the Audio/Video minor name if major is 4 and known, else the major name, else `"Class 0x..."`. (A *bit shift* `>>` moves bits right; `& 0x1f` keeps the lowest 5.)

**`snClassicScan()`** (`:605-649`), `async`, run by the `sn_cl_btn` click (`:650`):
1. `hci = parseInt($("sn_cl_dev").value, 10)`. If it is not a number (no radio free) show "no radio available (stop the survey on one radio, or release one)" and stop.
2. Disable the button.
3. If the radio is not in `STATE.classic_reserved`: show "reserving hciN for BlueZ..." and `POST /api/classic/reserve {dev, on:true}`, then `await refreshStatus()`.
4. `secs` = the seconds field, clamped to 3-60, default 12.
5. Show "inquiry running (N s)..." and `POST /api/classic/action {dev, action:"scan", params:{duration:secs, transport:"auto"}}`. The server runs this *synchronously*, so this request does not return until the scan ends (up to 150 s allowed by the server).
6. Convert the reply: keep only devices that have a Class of Device (`d.class`) and that are not already in the table as a BLE device (`!(prev && prev.addr_type !== "classic")`). Each becomes a fake "survey record" of the same shape the sniffer makes: `addr_type:"classic"`, `addr_kind` "BR/EDR (heard)" / "(connected)" / "(known, not heard)", `first_seen = last_seen = now (browser clock)`, `count: 0`, services from the device's UUIDs, `kind` from `snCodText` or the icon, `supports` lines (paired, connected, class, modalias), and a `classic` sub-object. The name is blank if BlueZ's fallback name equals the address with dashes.
7. `snIngest(devs, null, nowS)`. Status line: `N device(s): M heard in this inquiry`.
8. On error: `st.textContent = "failed: " + message`. A `finally` block always re-enables the button.

Side effect to know: the radio stays **reserved for Classic** afterwards. It disappears from BLE radio lists until released on the Classic tab.

#### 4.11.10 Interrogate (`:565-567`, `:652-729`)

State: `snInterro` (`Map` addr to the last result), `snIntBusy` (a request is running), `snIntShown` (a signature of what is drawn), `snIntDirty` (force a redraw).

**`sn_int_btn` click** (`:703-729`), `async`:
1. `addr = snSelected`, `hci` from `sn_int_dev`. Return if either is missing or `snIntBusy`.
2. Set `snIntBusy`, disable the button.
3. Reserve the radio if needed (same two calls as the Classic inquiry).
4. Status "interrogating ADDR (up to ~2 min)...".
5. `POST /api/classic/action {dev:hci, action:"interrogate", params:{addr, read: <checkbox>, transport}}`. `transport` is `"le"` for random addresses, `"bredr"` for Classic records, else `"auto"`.
6. Save `res` into `snInterro` with `res.when = Date.now()` and `res.hci`; set `snIntDirty`; status "done".
7. `catch` shows `failed: ...`; `finally` clears `snIntBusy` and re-enables the button.
The server connects read-only (nothing written, no pairing), lists GATT services/characteristics (optionally reading values) and Classic SDP records.

**`snRenderInterro()`** (`:652-701`) draws the result into `sn_int_out`. It builds a **signature** `snSelected + ":" + r.when` and returns immediately if it equals the last drawn signature and nothing is dirty (`:655-656`), so, unlike the detail panel, it is not rebuilt every second. If it must draw, it uses a local `line(text, indent, bold)` helper and prints: heading with time and radio; device identity; GATT summary line (services count, values read, read failed, "need encryption"); every service, its characteristics with flags, hex/decoded values or read errors, and descriptors; "No GATT services resolved..." if there are none; Profile UUIDs known to BlueZ; SDP records (name, RFCOMM channel, L2CAP PSM, profile and version, classes) or an `sdp_note`.

#### 4.11.11 Export: JSON and CSV (`:731-769`)

- **`snDownload(name, mime, text)`** (`:731-738`): makes a Blob, creates a temporary `<a href=blobURL download=name>`, adds it to the page, clicks it by code, then removes it and frees the URL after 1 s. The browser saves a file, with no server round trip.
- **`snCsvCell(v)`** (`:739-743`): turns one value into one safe CSV field:
  ```js
  let t = v == null ? "" : String(v);
  if (/^[=+\-@\t\r]/.test(t)) t = "'" + t;     // names come from the air: no formula injection
  return /[",\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
  ```
  1. null/undefined becomes empty.
  2. **CSV-injection guard**: if the text starts with `=`, `+`, `-`, `@`, tab or carriage return, prefix a single quote so a spreadsheet treats it as text, not a formula. This is what protects you from a device that advertises a name like `=HYPERLINK(...)`.
  3. If the text contains a quote, comma or newline, wrap it in double quotes and double any inner quotes (standard CSV escaping).
  Caveat: the guard is applied to *every* value, so negative numbers get a quote too (finding 3).
- **`snExportRecords()`** (`:744-751`): copy every record (`Object.assign({}, d)`), delete the browser-only `_rx`, and attach the interrogation result if one exists under key `interrogation`.
- **`SN_STAMP`** (`:752`): a function returning a file-name time stamp such as `2026-10-27T12-34-56` (ISO time, `:` and `.` replaced by `-`, cut to 19 chars). It uses UTC, not local time.
- **`sn_exp_json` click** (`:753-755`): `ble-survey-<stamp>.json`, `JSON.stringify(records, null, 1)` (indent 1 space). It contains everything, including interrogations.
- **`sn_exp_csv` click** (`:756-769`): fixed columns `addr, addr_kind, name, company, kind, rssi, rssi_avg, rssi_min, rssi_max, count, connectable, scannable, ext, phys, services, supports, first_seen, last_seen`. Per record, `phys` is joined with spaces, `services` and `supports` with `"; "`. Header line, then one line per device, joined with `\n`, final newline, MIME `text/csv`. `first_seen`/`last_seen` are raw epoch seconds, not dates. No byte-order mark is written.

The export covers **all** devices in `snDevs`, not just the ones passing the current search/hide filter.

#### 4.11.12 Clear and filter wiring (`:770-777`)

- **`sn_clear` click** (`:770-773`): `snClearedAt = Date.now() / 1000`; `snDevs.clear()`; `snSelected = null`; `snDirty = true`. The server keeps its own copy, so devices re-appear on their next packet (their `last_seen` is later than `snClearedAt`). The rows disappear on the next 1 s repaint, as they are no longer in `keep` (`:504`). `snInterro` is kept.
- **Filter listeners** (`:774-776`): for `sn_search`, `sn_sort`, `sn_hidestale`, an `input` event sets `snDirty = true`. Since the flag is not read, the visible effect arrives with the next 1-second tick (up to one second of lag).
- **The timer** (`:777`) described above.

### 4.12 The SSE connection (`app.js:779-823`)

**What it is.** `new EventSource("/api/stream")` opens a long-lived HTTP GET. The server answers with `Content-Type: text/event-stream` and then keeps writing messages of the form `data: {json}` followed by a blank line. Every 15 s of silence the server sends a comment line `: ping`, which the browser ignores (it just keeps the connection alive). If the connection drops, the browser reconnects by itself after a short wait (about 3 s; the server does not send a different `retry:`).

**`jobMeta`** (`:787`): a `Map` from job id to `{kind, dev}`. It is filled synchronously from each `status` event's embedded job (`:799`). The comment (`:779-786`) explains a race it fixes: a console `line` for a just-started job can arrive *before* the `/api/status` fetch finishes, so looking only in `STATE.running` would silently drop that line. It is never pruned.

**`connectStream()`** (`:789-823`), called once at start-up:

- `es.onopen` (`:791`): turn the dot green (`up` class), text "connected", then `snResync()` (rebuild the survey table from the server). `onopen` fires on the first connect *and* after every automatic reconnect.
- `es.onerror` (`:792`): dot red, text "reconnecting...". It does not close the stream: the browser keeps retrying.
- `es.onmessage` (`:793-822`): the message handler.
  1. `JSON.parse(ev.data)`; ignore malformed messages.
  2. Branch on `evt.type`:

| `evt.type` | Who sends it (`server.py`) | Payload | What the page does |
|---|---|---|---|
| `status` with a `status` field (the "hello") | First message after every connect: `_stream()` writes `{type:"status", event:"hello", status:<snapshot>}` | whole snapshot | `applyState(evt.status); render();` (`:797`). This is the resync of `STATE` after a reconnect |
| `status` without `status` (events `started`, `finished`, `reservation`) | `Jobs.start`, `Jobs._reader` on exit, `/api/classic/reserve` | `{event, job?, ts}` | If it carries a job, `jobMeta.set(id, {kind, dev})`; `fireHooks(evt, kind, dev)` (for `classic.js`); then `refreshStatus()` (`:799-801`) |
| `bluez` | `bluez_install_job()` | `{text}` for a log line, or `{done:true, ok}` at the end | `done`: clear `bluezBusy`, print the final `[+]`/`[x]` line, `refreshStatus()`. Otherwise `appendBluezLine(evt.text)` (`:803-808`) |
| `sniff` | `_reader()`, one per second per sniff job | `{job_id, dev, devices[], stats, now}` | `snIngest(evt.devices, evt.stats, evt.now)` and flag dirty (`:809-811`) |
| `line` | `_reader()` per output line of a job; `run_classic_action()` for Classic action logs | `{job_id, dev, text, kind?}` | see below |
| `overflow` | `Jobs.publish()` when a client's queue (2000 events) is full | `{type:"overflow"}` | **Never reaches the page.** `_stream()` consumes it and ends the connection. The page just sees the connection drop: `onerror` ("reconnecting..."), then automatic reconnect, then `onopen` (`snResync`) plus the "hello" snapshot. Net effect: the page resynchronises |

  **`line` handling** (`:812-821`):
  1. `meta = jobMeta.get(evt.job_id) || STATE.running.find(j => j.id === evt.job_id)`.
  2. `kind = meta ? meta.kind : null`; `dev = meta ? meta.dev : evt.dev`.
  3. If the event carries its own `kind` (one-shot Classic actions send `kind:"c_action"`), it wins (`:817`).
  4. If `kind` starts with `c_` (Classic), hand it to the hooks: `fireHooks(evt, kind, dev)`.
  5. Otherwise `appendConsoleLine(kind, dev, evt.text)`. The code has two branches (`kind === "sniff"` and a plain `else`) which do exactly the same thing (`:819-820`). If the kind is unknown, `consoleFor` returns `null` and the line is dropped.

On connect the server also **replays up to 500 buffered `line` events** (`_stream`: `for evt in backlog: write(evt)`) so a freshly opened page shows recent console history. Backlog lines of still-running jobs find their kind through `STATE.running`, which the preceding "hello" message has already filled.

### 4.13 Start-up (`app.js:825-828`)

```js
loadCaps().then(refreshStatus);
connectStream();
setInterval(refreshStatus, 5000);
```

1. Load capabilities (fill payload and profile lists, learn `ALL_HCI`), and *after that* fetch the first status snapshot.
2. Open the SSE stream (its "hello" message also delivers a snapshot).
3. Start the 5-second safety-net poll.
The 1-second survey repaint timer was already started at `:777`, and the two "change" listeners and every click handler were registered while the file was being read from top to bottom.

### 4.14 The BF hooks: where `app.js` hands events to `classic.js`

`classic.js` registers one function with `BF.hooks.push(...)` (`classic.js:486`). `app.js` calls every registered hook (through `fireHooks`) in exactly three places:

| Where | Event passed | Kind / dev arguments |
|---|---|---|
| `render()` (`app.js:312`) | `{type:"render"}` | `null, null` |
| SSE `status` message without a snapshot (`app.js:800`) | the raw event: `started`, `finished`, `reservation` | the job's `kind` and `dev`, or `null` |
| SSE `line` message whose resolved `kind` starts with `c_` (`app.js:818`) | the raw event (has `text`) | resolved `kind` (`c_a2dp`, `c_avrcp_watch`, `c_cycle`, `c_hfp`, `c_action`) and `dev` |

The hooks do **not** see `sniff` or `bluez` events, nor the "hello" status. A Classic line never reaches an `app.js` console; the Classic tab owns it. The two functions on `BF` run the other direction: `classic.js` calls `BF.state()` (`classic.js:46`) to read radio/reservation state and `BF.refresh()` (`classic.js:521`) after reserving a radio.

---------------------------------------------------------------------

## 5. Four walkthroughs

**A. Opening the page.** Browser GETs `/`, then `/style.css`, `/app.js`, `/classic.js`. `app.js` executes top to bottom: sets up state, `window.BF`, listeners. Start-up (4.13) fetches `/api/caps`, then `/api/status`; the SSE stream connects, `onopen` turns the dot green and `snResync()` fetches `/api/sniff/devices`; the "hello" snapshot and up to 500 backlog console lines arrive. Tables fill, radios appear, the BlueZ banner shows only if `bluez.installed` is false.

**B. Starting a survey.** Pick radio, click Start Survey, `sn_start` handler (`:205`) POSTs `/api/start`. Server starts a helper process, publishes `status/started`. The page gets it, stores the job in `jobMeta`, fires hooks, calls `refreshStatus()`; `render()` shows the radio as RUNNING and a row in "Sniff jobs". Each second a `sniff` event arrives, `snIngest` adds device records, the 1-second timer runs `renderSniffTable`, rows appear and reorder in place.

**C. Clicking Stop on a radio.** `stopJob` (`:230`) POSTs `/api/stop`. Server sends SIGINT, waits up to 5 s (then SIGTERM, 3 s more), and the process exit triggers `status/finished`; the page refreshes and the radio shows idle, and the job appears in history.

**D. Network blip.** The SSE connection drops: dot red "reconnecting...". The browser retries; on reconnect `onopen` turns the dot green, `snResync()` rebuilds the survey table, and "hello" replaces `STATE`. Meanwhile the 5 s poll continues. Console lines in the backlog are replayed again (finding 6).

---------------------------------------------------------------------

## 6. Reference tables

### 6.1 API routes the page calls

| Route | Method | Called from | Body | Notes |
|---|---|---|---|---|
| `/api/caps` | GET | `loadCaps` `:130` | - | Cached on the server; `?refresh=1` exists but the page never sends it |
| `/api/status` | GET | `refreshStatus` `:365` | - | Snapshot (`STATE`) |
| `/api/stream` | GET (SSE) | `connectStream` `:790` | - | Long-lived |
| `/api/sniff/devices` | GET | `snResync` `:421` | - | `{sniffs:[{job_id, dev, running, stats, devices}], now}` |
| `/api/start` | POST | `:179`, `:200`, `:211` | `{tool, dev, params}` | 409 if radio busy/reserved or BlueZ missing |
| `/api/stop` | POST | `stopJob` `:231` | `{job_id}` | 200 with `ok:false` on failure |
| `/api/stop_all` | POST | `:220` | `{}` | |
| `/api/bluez/install` | POST | `:345` | `{accept:true}` | Server refuses without `accept:true` |
| `/api/classic/reserve` | POST | `:613`, `:712` | `{dev, on:true}` | |
| `/api/classic/action` | POST | `:618`, `:717` | `{dev, action, params}` | Synchronous; `scan` and `interrogate` can take up to 150 s |

Routes on the server that `app.js` does not call: `/api/bluez/status` (no caller in `app.js`, `classic.js` or `index.html`), `/api/classic/preflight` (called by `classic.js:615`), `/api/classic/results` and `/api/classic/result/<file>` (presumably also `classic.js`; I did not check).

### 6.2 Module-level names in `app.js`

| Name | Line | Kind |
|---|---|---|
| `ALL_HCI`, `CAPS`, `STATE` | 14-16 | state |
| `BF`, `fireHooks` | 19, 24 | hook interface |
| `$`, `el`, `fmtUptime`, `api`, `post` | 29-52 | helpers |
| `fillSelect`, `populateDevSelects`, `populateInterfereChecks`, `selectedInterfereDevs`, `loadCaps` | 65-129 | forms |
| `IF_ARG_LABELS` | 143 | constant |
| `stopJob`, `renderRadios`, `renderKindTable`, `render` | 230-292 | actions / rendering |
| `_statusReqSeq`, `bluezBusy`, `applyBluez`, `appendBluezLine`, `applyState`, `refreshStatus` | 323-362 | status and banner |
| `consoleFor`, `appendConsoleLine` | 373, 382 | consoles |
| `snDevs`, `snRows`, `snDirty`, `snSelected`, `snClearedAt`, `snStats` | 402-404 | survey state |
| `snIngest`, `snResync`, `snAgo`, `snAgoText`, `snSummary`, `snKindText`, `snRssiText`, `snVisible`, `SN_SORTS`, `snSetCell`, `renderSniffTable`, `renderSniffDetail` | 406-516 | survey |
| `snInterro`, `snIntBusy`, `snIntShown`, `snIntDirty`, `snFillRadios`, `snFillRadioSelect`, `COD_MAJOR`, `COD_AV`, `snCodText`, `snClassicScan`, `snRenderInterro` | 566-652 | Classic inquiry / interrogate |
| `snDownload`, `snCsvCell`, `snExportRecords`, `SN_STAMP` | 731-752 | export |
| `jobMeta`, `connectStream` | 787, 789 | SSE |

### 6.3 Event listeners registered by `app.js`

| Element | Event | Handler (line) |
|---|---|---|
| every `.lf-tab` | click | tab switch (55) |
| `if_mode` | change | show/hide argument (147) |
| `adv_type` | change | show peer / dim payload row (155), fired once at load (161) |
| `if_start`, `adv_start`, `sn_start` | click | start jobs (164, 192, 205) |
| `stopAllBtn`, `refreshBtn`, `helpBtn` | click | 216, 222, 223 |
| `bluezInstallBtn` | click | install flow (337) |
| `sn_cl_btn` | click | `snClassicScan` (650) |
| `sn_int_btn` | click | interrogate (703) |
| `sn_exp_json`, `sn_exp_csv`, `sn_clear` | click | 753, 756, 770 |
| `sn_search`, `sn_sort`, `sn_hidestale` | input | set `snDirty` (774) |
| each survey `<tr>` | click | select device (481) |
| each Stop mini-button | click | `stopJob` (257, 285) |
| `EventSource` | open / error / message | 791 / 792 / 793 |
| timers | `setInterval` | survey repaint 1 s (777), status poll 5 s (828) |

---------------------------------------------------------------------

## 7. Things I noticed

Ordered roughly by how likely they are to bite. "Verified" means I reproduced the behaviour with a small pure-JavaScript check (no browser, no network). "Likely" means it follows from the code, but I did not run it in a browser.

**Bugs**

1. **The "Plan" and "PDU" columns of the Interference and Advertise job tables are always empty.** `app.js:297` and `:303` print `(j.argv || []).slice(4)`, but the server's job status (`Job.status()` in `server.py`) never includes `argv`. Even if it did, the argument list starts `[python, "-u", cli, "--dev", N, tool, ...]`, so `slice(4)` would begin at the radio number, not the plan (it was probably written before `-u` was added). Verified by reading both sides.
2. **`snDirty` is a write-only flag.** It is set to true in five places (`:416`, `:481`, `:772`, `:775`, `:811`) and cleared once (`:777`) but never tested, so the table repaints every second no matter what, even while another tab is shown. More noticeable: typing in the search box, changing the sort, or clicking a row only sets the flag, so the screen reacts up to one second later. Calling `renderSniffTable()` directly from those handlers would remove the lag.
3. **The CSV export corrupts negative numbers (verified).** The CSV-injection guard `snCsvCell` (`:741`) prefixes `'` to anything beginning with `-`, and RSSI values are negative: `-60` is exported as `'-60`, and the same for `rssi_avg`, `rssi_min`, `rssi_max`. Spreadsheets will show the apostrophe and treat the number as text. Fix idea: apply the guard only to string fields (name, company, kind, services, supports), not numbers. Also a `\r` in the middle of a value is not quoted (the quote test at `:742` checks `"`, `,`, `\n` only).
4. **"Sort by Name" puts unnamed devices first, not last (verified).** The code uses `"~"` as a stand-in for "no name" (`:462`), assuming `~` sorts after letters. `localeCompare` (locale-aware sorting) ranks punctuation before letters: `"~".localeCompare("Apple")` is -1 in Node, giving `["~", "alpha", "Beta", "Zed"]`.
5. **The Refresh button duplicates the beacon profile list and resets the payload select.** `loadCaps()` appends options to `adv_profile` without clearing (`:134-139`), so each click on Refresh adds another full copy of every profile. `fillSelect` does clear `if_payload`, so your payload choice resets to the first entry. Also Refresh never forces the server to rescan (no `?refresh=1`).
6. **Console history is duplicated after a reconnect.** The server replays its backlog (up to 500 lines) to every new stream connection, and `appendConsoleLine` has no de-duplication. On a first page load this is exactly what you want; on an automatic reconnect of the same page you see the lines again (the 300-line trim hides only the oldest). Related: on a fresh page load, backlog lines of jobs that have already finished are dropped, because their kind cannot be resolved (`jobMeta` is empty and the job is not in `STATE.running`).
7. **Clear compares two different clocks.** `snClearedAt` uses the browser's clock (`Date.now()`, `:771`) but is compared with `d.last_seen` stamped by the server machine (`ble_survey.py` uses `time.time()`). If the browser's computer is ahead of the server, devices vanish after Clear until the server clock catches up (a skew of minutes means minutes of nothing); if it is behind, old devices re-appear immediately. They only agree when both run on the same machine or have synchronised clocks.
8. **The device detail panel is rebuilt every second.** `renderSniffDetail` empties and refills `sn_detail_body` on every tick (`:526`), so you cannot select/copy text from it, and a long panel may flicker. The interrogation output avoids this with its signature check (`:655`); the detail panel does not.
9. **Likely: open drop-downs and keyboard focus are disturbed every 5 seconds.** `populateDevSelects()` and `populateInterfereChecks()` throw away and recreate the radio `<select>`s and checkboxes at each `render()` (every 5 s plus every SSE status event). In most browsers rebuilding a `<select>` while its list is open closes the list, and rebuilding a checkbox you have tabbed onto loses keyboard focus. `snFillRadioSelect` shows the cure (a cache key; `:577-579`) but is not used here. Not tested in a browser.
10. **Directed advertising disables the Duration box.** `adv_duration` is inside `adv_payload_row`, which `adv_type` change greys out and disables for `direct`/`direct-l` (`:155-160`). `adv_start` still sends whatever the Duration box held. So the user cannot change duration for directed PDUs, yet a value is submitted.
11. **A busy radio can stay selected.** After starting a job on the radio chosen in `adv_dev`/`sn_dev`, the drop-down keeps showing it as "(busy)" because the old value is restored (`:87`). Clicking Start again produces a 409 pop-up. Switching radio is manual.
12. **BlueZ install progress is lost on reload.** The log lines are published with `nobacklog:true` (not replayed) and the page never calls `GET /api/bluez/status` (which does keep the last 50 lines). Reloading mid-install shows the banner (button disabled because `installing`) but an empty log, and you never see the final "ready"/"failed" line unless the tab stayed open.
13. **Errors from the status poll are invisible.** `refreshStatus` swallows all errors (`:369`), and the green/red dot reflects only the SSE stream. If `/api/status` fails while the stream stays up, the screen quietly goes stale. Likewise `loadCaps()` has no `.catch` (`:222`, `:826`), producing "Uncaught (in promise)" console errors and, at start-up, skipping the first `refreshStatus` (the 5-second poll recovers).
14. **Survey statistics are global and devices are not tagged by radio.** With two radios surveying at once, `snStats` holds only whichever stats line came last, and a device heard by both is a single row with no radio shown. The interrogation busy flag and its status text (`snIntBusy`, `sn_int_status`) are also global rather than per device.

**Consistency, dead code and leftovers**

15. Hard-coded hardware words contradict the dynamic detection: "hci0-hci7" (`index.html:54`), "all 8 BE200" (`app.js:224`), "BE200 BLE Lab v1", and the placeholder host `ct523c-ccb8`.
16. The menu bar (Control, Reporting, Windows, Info, Tests) is inert but styled like a menu.
17. Duplicate branches: both arms at `app.js:819-820` do the same thing; `fillSelect`'s `labelFn` is never passed; `#tabs` has no use in JS; `snFillRadios()` is called from both `renderSniffTable` and `renderSniffDetail`; `appendBluezLine` and `appendConsoleLine` repeat the same prefix-to-colour logic.
18. Unused CSS: `.lf-tab.alert`, `.lf-input.changed`, and eight unused variables (listed in 3.1). `.c-readout` is declared twice. `assets/lanforge.png` is not referenced by the three files I read.
19. Unbounded browser memory: `snDevs`, `snInterro` and `jobMeta` only grow (the sniffer itself evicts old devices, the page does not). Console boxes are the exception (trimmed to 300 lines).
20. The console auto-scrolls to the bottom on every line (`:394`), even if you scrolled up to read earlier output.

**Safety**

21. **XSS: handled well.** Every piece of dynamic text, including over-the-air device names, goes through `el()`/`textContent`; the only `innerHTML` uses in `app.js` assign `""` to empty a container (`:66`, `:79`, `:102`, `:239`, `:265`, `:279`). The two `style.cssText` strings are constants. JSON from the server is rendered as text. (`classic.js` also only uses `innerHTML = ""`, by grep.)
22. **CSV injection: guarded** (see finding 3 for the side effect). The JSON export needs no guard.
23. **No authentication or CSRF defence (server-side, noted for completeness).** The server listens on `0.0.0.0`, runs as root, and `do_POST` parses the body as JSON whatever the `Content-Type` is. A hostile web page opened in a browser on the same network could try to POST to `/api/start` or `/api/stop_all`. Acceptable on an isolated lab network; worth knowing before wider deployment.
24. **`alert()` / `confirm()` everywhere.** Blocking pop-ups interrupt the live update and are easy to miss when a test run is unattended. Several failures only surface this way.

**Accessibility**

25. Tabs are `<div>`s: not keyboard-focusable, no `role="tab"`, no `aria-selected`; screen readers do not know which is current.
26. `.lf-field` labels are not tied to their inputs (no `for`/`id` pairing, no wrapping), so clicking a label does not focus its field and assistive technology cannot name the control.
27. Survey rows are clickable `<tr>`s with no `tabindex` or key handler: the detail view and Interrogate are mouse-only. The detail panel and consoles are not `aria-live`, so updates are not announced.
28. Small type (10-12 px), a very pale focus outline (`1px dotted --lf-primary2`) and several colour-only distinctions (row colours, the dot) reduce readability. The dot has adjacent text, which helps.
29. The only responsive rule is at 760 px; the 9-column survey table relies on sideways scrolling inside `.lf-tablewrap`.

**Unclear to me (not verified)**

- What exact shapes `/api/caps` returns (I only know the three fields app.js reads: `hardware.all_hci`, `interfere.payloads`, `adv.profiles`); `fillSelect` accepts strings or `{value}` objects, so either works.
- How `classic.js` draws the charts when the canvas attribute (130 px) and CSS (110 px) heights differ.
- Whether the browsers you use re-open a closed `<select>` list on rebuild (finding 9).
