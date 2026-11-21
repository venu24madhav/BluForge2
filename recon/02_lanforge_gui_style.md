# 02 - LANforge GUI look-and-feel capture

Unit: ct523c-ccb8 (10.28.3.73), LANforge 5.5.3. Recon only, read-only. Written 2026-10-05.

## 0. Bottom line

* The live LANforge Java GUI window **cannot be screenshotted without un-minimizing it** (details in section 1). I did not un-minimize it (that would change desktop state), so there is **no live capture of the GUI**.
* Instead the style below comes from four sources, in decreasing order of trust:
  1. **Bytecode of the running client** (`lfclient.jar`, via `javap`): exact colour constants, UIDefaults overrides, tab/menu label strings. VERIFIED.
  2. **The JDK that runs the GUI** (OpenJDK 17.0.13, Metal L&F, Ocean theme): resolved UIDefaults dumped by a headless Swing program that applies the same overrides. VERIFIED. A **synthetic reference render** of the Port Mgr screen was produced this way (`screens/synth_lanforge_portmgr_1680x997.png`, real port data from the JSON API). It is a faithful reproduction of the theme, **not** a capture of the real window.
  3. **Documentation screenshots shipped with the install** (`/home/lanforge/LANforgeGUI_5.5.3/html/images/gui_*.png`), from older GUI versions 5.0.2 to 5.3.8 on other machines. Good for layout, wrong for fonts and some colours. Copied to `screens/docs_ref/`.
  4. Fedora MATE theme defaults for the window frame (not directly observed on this box).
* Biggest gotcha for a faithful web clone: on this box the GUI's UI font **resolves to the Java logical font "Dialog" = Noto Sans 12px**, not Lucida (explained in 4.2).

## 1. Screenshot attempt (what worked, what did not)

| Step | Result |
|---|---|
| X server | `Xvnc :1 -geometry 1680x1050 -auth /home/lanforge/.Xauthority -rfbport 5901`, socket `/tmp/.X11-unix/X1` |
| Tools present | `import` (ImageMagick), `xprop`, `xrandr`, `ffmpeg`, python3 + PIL + python-xlib. Absent: `xwd`, `scrot`, `gnome-screenshot`, `xdotool`, `xwininfo`, `vncsnapshot` |
| Access | `DISPLAY=:1 XAUTHORITY=/home/lanforge/.Xauthority import -window root root_full.png` worked as root. |
| Root screenshot | `screens/root_full.png` (1680x1050). It shows **only Firefox** (maximized, on `localhost:8080/`, the LANforge JSON API index in Firefox's JSON viewer, with the URL dropdown open). No LANforge GUI is visible. |
| GUI window | `xprop -root _NET_CLIENT_LIST` window `0x1c00036`, name `"LANforge Manager    Version(5.5.3)  "`, class `candela-lanforge-lfclient`, **`_NET_WM_STATE_HIDDEN`, `WM_STATE: Iconic`**. Xlib `get_attributes().map_state == 0` (Unmapped). `import -window 0x1c00036` fails (`error/import.c/ImportImageCommand/1289`) because an unmapped window has no pixels in Xvnc. |
| Why I stopped | The only way to get pixels is to de-iconify the window (wmctrl/xdotool/XMapWindow) which changes shared desktop state; forbidden by the read-only rules. If you are fine with that, a single un-minimize followed by `import -window 0x1c00036 lfgui.png` would capture it (X is reachable; window geometry is 1678x965 at 0,53 with frame extents 1,1,31,1). |
| Other windows | MATE panel (top, `#d4d4d4`), MATE Terminals (hidden), Caja "EricssonBluForge" (maximized), Firefox (focused). |
| Remembered GUI size | `lfcnf.txt`: `WINDOW_SIZE_OPTION remember`, `WINDOW_SIZE_PREV_WIDTH 1680`, `WINDOW_SIZE_PREV_HEIGHT 997`; X geometry 1678x965 + frame (1,1,31,1). |

Files:
* `/home/lanforge/Desktop/EricssonBluForge/recon/screens/root_full.png` (live, Firefox only)
* `/home/lanforge/Desktop/EricssonBluForge/recon/screens/synth_lanforge_portmgr_1680x997.png` (synthetic Metal/Ocean render, see above)
* `/home/lanforge/Desktop/EricssonBluForge/recon/screens/synth_zoom_topleft.png` (crop of the synthetic render)
* `/home/lanforge/Desktop/EricssonBluForge/recon/screens/docs_ref/` : `gui_ports.png` (5.3.8), `gui_generic_tab_clive.png` (5.2.10), `gui_wl.png` (5.2.4), `gui_status.png` (5.2.8), `gui_cards.png`, `gui_cx.png`, `gui_events.png`, `gui_port_crmod-wiphy.png`, `gui_splash.png` (connection dialog, 5.3.8), `ports-tab-hi.png` (5.0.2, 3121 px wide, shows the dense Port Mgr table)
* Re-render source: `/home/lanforge/Desktop/EricssonBluForge/recon/ref_render/RefRender.java` (`env -u DISPLAY java -Djava.awt.headless=true RefRender.java <dir>`), full resolved defaults in `ref_render/ui_defaults_dump.txt` (375 lines).

## 2. Visual description (synthetic render + docs shots + bytecode)

**Look**: classic Java Swing "Metal / Ocean". `lfcnf.txt` has `SYSTEM_LOOK_FEEL false`; `lfclient.class` therefore logs "Loading Metal theme" and does not call the system L&F; `swing.boldMetal=false` so nothing is bold by default. Everything is flat light grey `#eeeeee` with blue-grey 1px borders and pale-blue gradients on buttons. No dark mode, no rounded corners, no shadows, no icons in tabs.

* **Window chrome**: drawn by the window manager, not Swing. Marco theme for MATE on Fedora is `BlueMenta` (from `/usr/share/glib-2.0/schemas/10_mate-fedora.gschema.override`; the lanforge user's dconf has no override). Title `LANforge Manager    Version(5.5.3)  `, frame 1px left/right/bottom, 31px titlebar, grey-blue GTK bg `#d4d4d4` and selected `#6699CC`. In the docs screenshots (older Marco/Menta themes) the title is centered and the buttons are round at the right. Java coffee-cup icon at top-left. NOT observed on this box (window hidden), treat as approximate.
* **Menu bar** (22px high): vertical white to `#dadada` gradient (`MenuBar.gradient 1.0 0.0 #ffffff #dadada #dadada`), 1px `#cccccc` bottom rule. Menus in 5.5.3 resource bundle: **Control, Reporting, Windows (tear-off), Info, Tests (plugins)**. The docs screenshots of older versions show "Control, Reporting, Tear-Off, Info, Plugins". Menu hover `#a3b8cc`, menu-item hover `#b4dcff` with text `#000500`.
* **Top action row** (right aligned, 27px high buttons, 12px gaps): `Stop All`, `Restart Manager`, `Refresh`, `HELP`. Older versions also had `Flush SS`, `Tooltips` and `Poll Mgr` checkboxes.
* **Tab bar**: `JTabbedPane`, TOP placement, WRAP layout (multi-row; Metal rotates the row of the selected tab next to the content area). Rectangular tabs, 1px `#7a8a99` border, unselected fill `#eeeeee`, selected fill `#c8ddf2` with a 1px `#6382bf` top edge and 1px white highlight under it. Tab height about 20px, padding 9px left/right. A `[+]` tab/button (blue line border) shows hidden tabs. A tab can be flagged `ORANGE` (`LFTabbedPane.setBackgroundAt(i, Color.ORANGE)`) for activity/alerts.
* **Content pane**: `#eeeeee`, framed by `#6382bf` (1px) + white + `#c8ddf2` (2px) on top/left.
* **Port Mgr control strip** (from docs 5.3.8): `Disp:` text field + `Sniff Packets`; `Down` checkbox, `Clear Counters`, `Reset Port`, `Delete`; second row `Rpt Timer:` combo (`medium (8 s)`), `Apply`, `VRF` checkbox, `Display`, `Create`, `Modify`, `Batch Modify`. Buttons 27px tall, Ocean gradient, 1px `#7a8a99` border.
* **Table**: titled border (`#b8cfe5`, centered title, e.g. "All Ethernet Interfaces (Ports) for all Resources."). White body (`#ffffff`), **no row striping**, 1px grid `#bebec8` both directions, row pitch 19px, header cells `#eeeeee` with bevel and centered text, numeric columns right aligned with thousands separators (`SHOW_COMMAS true`). Selection `#b4dcff` with near-black text `#000500`. Horizontal and vertical scrollbars always present, 17px, thumb `#a3b8cc`, track `#eeeeee`.
* **Status bar**: bottom, `Logged in to: localhost:4002   as: Admin`, etched border, ~23px.
* **Dialogs** (Create/Modify Port, wiphy "Configure Settings", etc.): same Metal look, nested titled-border groups, bottom button row `Print Display Logs | Probe Sync | Apply OK Cancel`. **Modified/changed fields are filled peach `#ffe0c0`** (docs, `gui_port_crmod-wiphy.png`). Dialog bg `#eeeeee`.
* **Connection dialog** ("LANforge Connection Management"): Candela green/gold logo at the left, `Server Name/IP` combo, buttons `Clear Connect Disconnect Discover Cancel`, "Discover Resources" checkbox, list of discovered managers.
* **Branding**: Candela Technologies logo (green `#007000` wordmark with gold swoosh `#f8c048`), a brass anvil icon (`canvil.ico`, `anvil-right.svg`), a `LANforge` wordmark (`lanforge.png`: green 2x4 LED dots over mono text on `#c8f0f8`).
* **Status tab colours** (docs): resource boxes with link LEDs (red `#ff0000`, green `#00ff00`, orange `#ffa26b`, yellow) and a port-type tile (blue `#4265ef`, green `#73d38c`, yellow `#efefef`/`#ffff66`).

## 3. Tabs

Strings from `candela/lanforge/MessageBundle_en_US.properties` in `lfclient.jar` where a key is shown; labels without a key (Layer-4, Event Log, Alerts, Test Group, PPP-Links) were read off the docs screenshots, not the bundle. Visibility from `/home/lanforge/LANforgeGUI_5.5.3/lfcnf.txt` `NO_*` flags (0 = shown, 1 = hidden) as last saved by the running GUI (file mtime Oct 5 17:58). The live window was not observed, and licence can additionally hide tabs (`NO_TABS_FROM_LICENSE false` here), so treat the visible list as probable.

| Tab label | Key | lfcnf flag | Likely visible |
|---|---|---|---|
| Status | STATUS | NO_STATUS 0 | yes |
| Layer-3 | L3CX_TO | NO_L3 0 | yes |
| L3 Endps | ENDPOINTS_TO | NO_L3 0 | yes |
| VoIP/RTP, VoIP/RTP Endps | RTPCX_TO, RTP_ENDPOINTS_TO | NO_VOIP 1 | **hidden** |
| Armageddon | ARM_TO | NO_ARM 0 | yes |
| WanLinks | WL_TO | NO_ICE 0 | yes |
| Attenuators | ATTEN_TO | NO_ATTENS 0 | yes |
| RF-Generator | RFGEN_TO | NO_RFGEN 0 | yes (this is where the HackRF is controlled) |
| File-IO | FE_TO | NO_FE 0 | yes |
| Layer-4 | (Layer-4) | NO_L4 0 | yes |
| **Generic** | GENERIC / FE_GENERIC | NO_GEN 1 | **hidden** (can be re-enabled in Preferences, then it appears) |
| Test Mgr | TM_TO | NO_TM 1 | hidden |
| Test Group / Connection Group | CXG_TO | NO_GROUPS 1 | hidden |
| Resource Mgr | CARD_MGR_TO | NO_CARDS 0 | yes |
| Event Log | (Event Log) | NO_EVENTS 1 | hidden |
| Alerts | (Alerts) | NO_ALERTS 0 | yes |
| **Port Mgr** | PORT_MGR | NO_PORTS 0 | yes. Contains the wiphyN radios (Port Type `WIFI-Radio`) |
| vAP Stations | STATION_MGR | NO_STATION 0 | yes |
| Messages / Warnings / Wifi-Messages | CLI_TO / CLI_WRN_TO / CLI_WIFI_TO | NO_CLI 0, NO_WARNINGS 0, NO_WIFI_EVENTS 0 | yes |
| DUT / Profiles / Traffic-Profiles | DUT_MGR_TO / PROFILE_MGR_TO / TPROF_MGR_TO | NO_DUT 0, NO_PROFILE 0, NO_TPROFILE 0 | yes |
| Interop, Cell Emulator | ADB_TO, CELL_EMULATOR_TO | (no flag; `no_cell_emulator` default 0) | probably |
| Collision-Domains, Serial Spans, PPP-Links | CD_TO, SERIAL_SPAN_TO, (PPP) | NO_CD 1, NO_SPANS 1, NO_PPP 1 | hidden |

Notes:
* There is **no tab called "Wifi radios"**. Radios are `wiphyN` rows in **Port Mgr** (Port Type `WIFI-Radio`, Mode e.g. `802.11abgn-BE` for the BE200s). Selecting a row then **Modify** opens a "wiphyN (...) Configure Settings" dialog with its own inner tabs `Standard Configuration | RF Patterns | Firmware`.
* Real data seen via GET on this unit: ports eth0..eth3 and wiphy0,1,2,4..11 (no wiphy3), see `ref_render/ports.tsv`.
* The Bluetooth controllers (hci0..hci7) are **not** LANforge ports/tabs in this GUI; the demo UI will have to add its own BLE pages in this style.

## 4. Style guide

### 4.1 Provenance legend
`[bc]` bytecode constant in `lfclient.jar`; `[jdk]` resolved from OpenJDK 17.0.13 Metal/Ocean with the lfclient overrides (see `ref_render/ui_defaults_dump.txt`); `[doc]` sampled from an install-shipped documentation screenshot; `[web]` from the webGUI/docs CSS.

### 4.2 Fonts
* `[bc]` `lfclient.class` loads `LucidaBright*`, `LucidaSans*`, `LucidaTypewriter*` and `century-gothic-sans*` TTFs from the jar and sets UIManager `defaultFont` = LucidaBrightRegular 12. **But** its next step replaces every FontUIResource in `UIManager` with `new FontUIResource("LucidaBrightRegular", -2, 12)`, and the string `"LucidaBrightRegular"` matches no installed family (registered family is "Lucida Bright", font name "Lucida Bright Regular"). `[jdk]` verified in the headless run: `new Font("LucidaBrightRegular",PLAIN,12)` returns family `Dialog`. So on this machine all Swing widgets render in **logical font `Dialog` 12 px plain = Noto Sans Regular** (`fc-match Dialog` gives `NotoSans-Regular.ttf`). Docs screenshots from older/other machines show DejaVu Sans or Lucida Sans, which is why they look slightly different.
* `[bc]` Table cells and column headers: `new Font("Dialog", 0, 12)` (`tbl_cll_font`, `tbl_column_font`). `TextArea.font` Arial 9, `TextPane/EditorPane.font` Courier 11. Text antialiasing: LCD (`-Dawt.useSystemAAFontSettings=lcd -Dswing.aatext=true`).
* `[web]` Docs/website text: `"Century Gothic", Arial, Helvetica, sans-serif`; code `"Courier 12", courier, monospace`.
* No bold anywhere in chrome (boldMetal=false), except dialog/group titles in the docs shots.

### 4.3 Paste-ready CSS variables

```css
:root {
  /* ---- Metal/Ocean base [jdk] ---- */
  --lf-control:            #eeeeee; /* panels, buttons, menus, unselected tabs, table headers, dialogs */
  --lf-white:              #ffffff; /* table body, text fields, lists, highlights */
  --lf-text:               #333333; /* every foreground */
  --lf-text-disabled:      #999999;
  --lf-text-on-select:     #000500; /* [bc] selection foreground */
  --lf-border:             #7a8a99; /* controlDkShadow: button/tab/field 1px border */
  --lf-border-soft:        #b8cfe5; /* controlShadow / titled-border / scrollbar edges / tooltip bg */
  --lf-primary1:           #6382bf; /* tab-content outer border, focus */
  --lf-primary2:           #a3b8cc; /* focus ring, scrollbar thumb, menu hover */
  --lf-primary3:           #b8cfe5;
  --lf-tab-selected:       #c8ddf2; /* selected tab + content-area rim */
  --lf-tab-area:           #dadada;
  --lf-menubar-top:        #ffffff;
  --lf-menubar-bottom:     #dadada;
  --lf-menubar-rule:       #cccccc;
  --lf-btn-top:            #dde8f3; /* Button.gradient: 0-30% top->white, then white->bottom */
  --lf-btn-mid:            #ffffff;
  --lf-btn-bottom:         #b8cfe5;

  /* ---- Table [bc]/[jdk] ---- */
  --lf-table-bg:           #ffffff;
  --lf-table-grid:         #bebec8; /* [bc] Table.gridColor (190,190,200) */
  --lf-table-header-bg:    #eeeeee;
  --lf-select-bg:          #b4dcff; /* [bc] (180,220,255) used for table/list/menu/combo/text selection */
  --lf-select-fg:          #000500; /* [bc] (0,5,0) */
  --lf-drop-cell:          #d2e9ff;

  /* ---- Row / status semantics [bc] LANforgeMgr ---- */
  --lf-row-mgt-port:       #d1f4fc; /* management port row (blue) */
  --lf-row-data-a:         #d2ebd2; /* endpoint A tint */
  --lf-row-data-b:         #dcdcf5; /* endpoint B tint */
  --lf-row-pass:           #00e68a;
  --lf-row-inuse:          #00ff99;
  --lf-row-info:           #99ccff;
  --lf-row-warn:           #ffff66; /* phantom / warning (yellow) */
  --lf-row-error:          #ff3333; /* ignored / error (red) */
  --lf-warn-border:        #800000;
  --lf-cx-paused:          #daf8ff;
  --lf-cx-connected:       #d8fbd8;
  --lf-cx-error:           #f3b4b4;
  --lf-peer:               #f4f2f2;
  --lf-modified-field:     #ffe0c0; /* [doc] peach fill for edited dialog fields */
  --lf-tab-alert:          #ffc800; /* java.awt.Color.ORANGE */

  /* ---- Colour ramps [bc] (graphs / badges) ---- */
  --lf-blue-1:#00178b; --lf-blue-2:#001fbe; --lf-blue-3:#506dff; --lf-blue-4:#8b9eff;
  --lf-green-1:#006200; --lf-green-2:#026c45; --lf-green-3:#00b100; --lf-green-4:#b1ffb1; --lf-green-pale:#d9fdc9;
  --lf-burgundy-1:#590d02; --lf-burgundy-2:#7f1302; --lf-burgundy-3:#b91b03; --lf-burgundy-4:#e02104;
  --lf-yellow-1:#584900; --lf-yellow-2:#8b7400; --lf-yellow-3:#be9f00; --lf-yellow-4:#dbb400;
  --lf-ul:#c242f4; --lf-ul-dark:#a20ddd; --lf-ul-light:#d98af8;   /* upload series */
  --lf-dl:#875d32; --lf-dl-dark:#624324; --lf-dl-light:#ac7740;   /* download series */
  --lf-endp-a-dark:#5cd65c; --lf-endp-b-dark:#cc66ff;
  --lf-graph-bg:#ffffff; --lf-plot-bg:#c0c0c0; --lf-plot-grid:#ffffff;  /* Color.white / lightGray */

  /* ---- Port link-light icons [bc tooltip text] ---- */
  --lf-link-10g: purple; --lf-link-1g: orange; --lf-link-100m: green; --lf-link-10m: yellow; --lf-link-none: red;

  /* ---- Candela brand ---- */
  --candela-green:         #007000; /* logo wordmark */
  --candela-gold:          #f8c048; /* logo swoosh / favicon */
  --candela-site-green:    #2a5b29; /* docs site */
  --candela-site-gold:     #aa7700;

  /* ---- Type & metrics ---- */
  --lf-font: "Noto Sans", "DejaVu Sans", "Lucida Sans", Verdana, sans-serif; /* = Java Dialog on this box */
  --lf-font-brand: "Century Gothic", Arial, Helvetica, sans-serif;
  --lf-mono: "Courier 12", "Liberation Mono", Courier, monospace;
  --lf-fs: 12px;
  --lf-row-h: 19px;       /* 18 + 1px grid, measured in render */
  --lf-btn-h: 27px;
  --lf-menubar-h: 22px;
  --lf-tab-h: 20px;
  --lf-status-h: 24px;
  --lf-scrollbar-w: 17px;
}
```

### 4.4 Component recipes (measured from the synthetic render)

```css
html,body{margin:0;background:var(--lf-control);color:var(--lf-text);font:var(--lf-fs)/1.2 var(--lf-font)}
.lf-menubar{height:22px;background:linear-gradient(var(--lf-menubar-top),var(--lf-menubar-bottom));border-bottom:1px solid var(--lf-menubar-rule);display:flex;align-items:center;padding:0 2px}
.lf-menubar span{padding:0 6px}                       /* hover: background var(--lf-primary2) */
.lf-actions{display:flex;justify-content:flex-end;gap:12px;padding:4px 12px}
.lf-btn{height:27px;min-width:63px;padding:0 14px;color:var(--lf-text);font:inherit;
  border:1px solid var(--lf-border);border-radius:0;
  background:linear-gradient(to bottom,var(--lf-btn-top) 0%,var(--lf-btn-mid) 30%,var(--lf-btn-bottom) 100%)}
.lf-btn:active{background:var(--lf-primary3)}
.lf-btn:disabled{color:var(--lf-text-disabled);background:var(--lf-control);border-color:var(--lf-primary3)}
.lf-btn:focus-visible{outline:1px dotted var(--lf-primary2);outline-offset:-4px}
.lf-tabs{display:flex;flex-wrap:wrap;padding:2px 2px 0 2px}   /* tabs wrap onto 2+ rows like Swing */
.lf-tab{height:20px;padding:0 9px 1px;border:1px solid var(--lf-border);border-bottom:0;background:var(--lf-control);margin-right:-1px}
.lf-tab.sel{background:var(--lf-tab-selected);border-top-color:var(--lf-primary1);box-shadow:inset 0 1px 0 #fff}
.lf-tab.alert{background:var(--lf-tab-alert)}
.lf-pane{background:var(--lf-control);border:1px solid var(--lf-primary1);box-shadow:inset 1px 1px 0 #fff,inset 3px 3px 0 var(--lf-tab-selected);padding:6px}
.lf-group{border:1px solid var(--lf-border-soft);margin:6px 0;padding:6px}   /* titled border, legend centered */
.lf-group>legend{padding:0 4px;margin:0 auto}
.lf-input{height:20px;border:1px solid var(--lf-border);background:#fff;padding:0 3px}
.lf-table{border-collapse:collapse;background:var(--lf-table-bg);font-size:12px}
.lf-table th{background:var(--lf-table-header-bg);font-weight:400;padding:2px 8px;height:20px;text-align:center;
  border:1px solid var(--lf-table-grid);border-top-color:#fff;border-left-color:#fff;box-shadow:inset -1px -1px 0 var(--lf-border-soft)}
.lf-table td{height:18px;padding:0 3px;border:1px solid var(--lf-table-grid);white-space:nowrap}
.lf-table td.num{text-align:right}
.lf-table tr.sel td{background:var(--lf-select-bg);color:var(--lf-select-fg)}
.lf-table tr.mgt td{background:var(--lf-row-mgt-port)}
.lf-table tr.warn td{background:var(--lf-row-warn)}
.lf-table tr.err td{background:var(--lf-row-error)}
.lf-status{height:24px;border-top:1px solid var(--lf-border-soft);box-shadow:inset 0 1px 0 #fff;padding:3px 6px}
```

Measured layout of the 1680x997 client area (synthetic render; pixel scans): menu bar y=0-22; button row y=27-53 (Stop All x=1288 w=76, Restart Manager w=128, Refresh w=77, HELP w=63, 12px gaps, 12px right margin); tab strip y=60-80 (single row at this width; with this tab set it would wrap to two rows at narrower windows, as in the docs shots); content border y=80; control rows at y~100 and y~135 (35px pitch); titled table border at y=160; header y=171-191 (21px); first data row starts y=192 (19px pitch); horizontal scrollbar y=962-973; right scrollbar ~17px wide at x~1655-1672; status bar etched top edge y=972-973 and bottom edge y=994-995 (about 24px).

Main-window layout (top to bottom): WM titlebar, menu bar, right-aligned action buttons, wrapped tab strip, tab content (control strip -> titled-border table that fills remaining space with always-visible scrollbars), status bar. Content is dense (12px type, 19px rows), table-first, no whitespace-heavy cards.

## 5. The other web UIs on this box

### 5.1 `/home/lanforge/local/webGUI` (LANforge "WebGUI", build 5.5.3, git `2ffe441f LMWUI-339`) - **not running**
* Tech: **React 19 + Vite 7 SPA**, MUI 7 (`@mui/material`), Emotion, Tailwind 4 (`@tailwindcss/vite`), zustand, react-router 7, axios, echarts/recharts/plotly/jointjs/reactflow. Fonts: `@fontsource-variable/inter`. Backend: Django + DRF + Channels (ASGI), Celery, PostgreSQL, ClickHouse, Redis; nginx fronts it. Only `frontend/` source is present: **no `node_modules/`, no `dist/`** (so a build needs `npm install` with internet; Node v20.17.0, npm and `/usr/sbin/nginx` are installed).
* Launch: `deployment/scripts/run_with_nginx.sh --install | --run-project | --run-services` (README), or `run_project.py` (orchestrates `run_backend.py` + `run_frontend.py`). Config via `LMWEBUI.env` (example: nginx HTTP 8442 / HTTPS 8443, backend 127.0.0.1:8686; Vite dev proxy defaults to backend 8000). Needs root or passwordless sudo.
* Evidence it is not up: no listener on 8442/8443/8686 (`ss -ltnp`), `curl localhost:8442` refused. Redis (6379) and an Erlang `beam.smp` (5672, likely RabbitMQ) are running, but they are not shown to belong to it.
* Theme (`frontend/src/theme/theme.js`, `src/index.css`): light MUI theme, **not LANforge-GUI-like** (rounded 8px, gradient contained buttons, Inter 13/14px). Tokens: primary `#1565C0` (light `#42A5F5`, dark `#0D47A1`), secondary `#1976D2`, bg `#FAFAFA`, paper `#FFFFFF`, text secondary `#666666`, success `#2E7D32`, error `#D32F2F`, warning `#F57C00`, **navbar/accent `#303C54`** (178 uses), sidebar white 240px / 60px collapsed, scrollbar `#888` on `#f0f0f0`. Tab indicator 3px `#1565C0`. Page title "Lanforge GUI", favicon `/Manager/c_logo-min.jpg`.
* Assets in `frontend/public/Manager/` (not copied, large): `c_logo-min.jpg` 1.1MB, `candelabg.png`, `candelabg_golden.png` 1.6MB, `login_bg.jpeg`, `tests_logo.png` 18KB, `candela_swirl_small-72h.png` 13KB (same as GUI), `wifi_{4,5,6,7}_radio.png`, `ct523c-0bdd.png` (this unit's photo), many OS/AP PNGs.

### 5.2 `/home/lanforge/local/interop-webGUI` - **running** (the web UI actually up)
* `python3 manage.py runserver 10.28.3.73:5454` (Django dev server, `Server: WSGIServer/0.2 CPython/3.12.7`) plus a Celery worker/beat (process list); HTTP 200 on `/`, page title "Login - Candela Technologies India PVT.LTD". Bootstrap-based admin template (assets `color_skins.css`, jvectormap, morris), Lato font, accents `#00cfd1` (teal) and `#0062cc` (counted from the login page HTML). Also listening: Apache `httpd` on 10.28.3.73:80/443 (presumably serving `/var/www/html`, not checked) and nginx on 172.16.0.1:80.
* Neither web UI resembles the Java GUI; the Java Metal look above is the one to copy.

### 5.3 `/var/www/html` and the docs tree
* Candela logo SVG/PNG candidates: `/var/www/html/LANforgeDocs-5.5.2/images/CandelaLogo_top_corner.svg` (28KB), `candela_swirl.png`, `candela_swirl_micro.png`, `CandelaLogo2-90dpi-200x90-trans.png`. Docs CSS (`default.css`, `screen.css`): `"Century Gothic", Arial, Helvetica, sans-serif`; greens `#2a5b29`, golds `#aa7700 #eecc22 #ccaa22`, light greens `#eeffcc #ddffcc #aaffaa`.
* Note the install-shipped GUI docs page `lfgui_ug.html` documents every tab with screenshots (`/home/lanforge/LANforgeGUI_5.5.3/html/`).

## 6. Assets copied (`/home/lanforge/Desktop/EricssonBluForge/recon/assets/`, all Candela's own, each under 10KB)

| File | Source | What it is |
|---|---|---|
| `CandelaLogo2-90dpi-200x90-trans.png` | `LANforgeGUI_5.5.3/images/` (also inside `lfclient.jar` `candela/images/`) | 200x90 RGBA Candela Technologies logo (used in the connection dialog and report header) |
| `anvil-right.svg` | same | LANforge anvil vector, 8.5KB |
| `favicon.ico` | same | 32x32 favicon (gold + green) |
| `lanforge.png` | same | 200x70 "LANforge" wordmark with LED dots |
| `control_play_blue.png` | same | small play control icon (pair `control_pause_blue.png` also exists in the jar) |

The jar's other UI icons (`port_ok.gif`, `port_mgt.gif`, `port_phantom.gif`, `port_na.png`, `port_admin_down.gif`, `card*.gif`, `larrow/rarrow.gif`, `zoom-*.gif`) were listed (`unzip -l`) but not copied. The 3,732-entry jar contains 49 images under `candela/images/`, the 14 TTFs under `fonts/`, message bundles and no CSS/theme files, so there is no separate theme resource to extract.

## 7. API notes found along the way (useful for the web backend)
* `http://localhost:8080/` (served by the GUI's embedded NanoHTTPD, `ENABLE_HTTPD true`, `HTTPD_PORT 8080`) returns the JSON endpoint index; `curl` GET works. Also listening: 127.0.0.1:8081 (same java pid), 4001/4002 (LANforge server / btserver), 3999.
* **Gotcha**: `GET /port/1/1/<alias>` returns **HTTP 500 unless the request has an `Accept` header** (Python `urllib` and `http.client` send none by default; with `Accept: */*` or `application/json` they get 200, as does curl). Tested only with GET.
* `GET /ports/1/1` lists ports with `_links`; `GET /port/1/1/eth0` returns the full field set (rx/tx bytes/pkts, pps/bps, mac, mode, channel, ip, down, phantom, parent dev, etc.).

## 8. Caveats / not verified
* No live pixels of the real GUI exist in this report. Window chrome, exact row heights, column widths, the actual tab row wrapping at 1678px and any custom per-tab colours (e.g. Status tab tiles) are inferred.
* Visible tab list is derived from saved preferences, not from the window.
* Colours marked `[doc]` come from older-version screenshots; the `[bc]` and `[jdk]` ones are authoritative for 5.5.3 on this JDK.
* Nothing was changed on the system: only X read requests (root capture, `xprop`, Xlib attribute queries), GET requests to :8080 and :5454, JDK/javap reads, and files written under `recon/`.
