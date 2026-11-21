# 09 - Installing, packaging and the BlueZ mechanism

Files: `deploy/install.sh`, `deploy/package-offline.sh`, `deploy/ericsson-bluforge.service`,
`deploy/bluforge-bluez.sh`, `desktop/start-and-open.sh`, `desktop/Ericsson-BLE-Demo.desktop`,
`docs/BLUEZ_INSTALL.md`, `docs/lf_kinstall-bluforge-stage.patch`.

## The big picture
```
developer machine                              target unit
  deploy/package-offline.sh  ──► .tar.gz ──►   tar xzf && sudo ./install.sh
   (builds the package)                          ├─ copies src/ + docs/ to /opt/ericsson-bluforge
                                                 ├─ installs /usr/local/sbin/bluforge-bluez
                                                 ├─ writes + starts the systemd service (port 5480)
                                                 └─ waits until the page answers, prints its URL
```

## `deploy/package-offline.sh` - build the installable package
1. Creates a temporary staging folder and copies `src/` into it (the whole folder, so the newer modules are included).
2. Deletes `__pycache__`, `*.pyc` and any stray core-dump files from the copy (a crashed `btmon` once left one in `src/`).
3. Copies `docs/`, `install.sh`, the systemd unit template and `bluforge-bluez.sh`; makes the scripts executable.
4. Writes a `README-INSTALL.txt` into the package (states that BlueZ is **not** included).
5. Packs it all into `/tmp/ericsson-bluforge-offline-<timestamp>.tar.gz` (or the path you give) and prints the size.

## `deploy/install.sh` - run on the target unit (as root, safe to re-run)
Settings: `INSTALL_DIR` (default `/opt/ericsson-bluforge`), `GUI_PORT` (default 5480).
1. **Sanity checks (warnings only, nothing blocks):** root? `src/` present? python3 present and its version; does Python's `socket` module have `AF_BLUETOOTH`; is `bluetoothctl` present (if not: prints that BlueZ is installed by the end-user and how); how many `hciN` radios are visible under `/sys/class/bluetooth`.
2. **Copy the program:** `rsync -a --delete src/` (or `cp -a` if rsync is missing) to `$INSTALL_DIR/src`, plus the docs.
3. **Install the BlueZ helper:** `install -m 0755 bluforge-bluez.sh /usr/local/sbin/bluforge-bluez` (warns if the script is missing from the package).
4. **Install the service:** fills `__INSTALL_DIR__` and `__GUI_PORT__` into the unit template, writes `/etc/systemd/system/ericsson-bluforge.service`, `daemon-reload`, `enable`, `restart`.
5. **Verify:** polls `http://127.0.0.1:<port>/api/status` for up to 15 s using a small Python snippet; prints "GUI is up" or the `journalctl` command to look at.
6. Prints the URL (`http://<ip>:5480`) and the useful `systemctl` / `journalctl` commands.

## `deploy/ericsson-bluforge.service` - the systemd unit
- Starts `python3 .../src/server.py --port <port>` as **root** (raw HCI needs it), working directory = install dir, `PYTHONUNBUFFERED=1`.
- `After=` / `Wants=` network-online and `bluetooth.service`. These are **soft** dependencies on purpose: a missing `bluetooth.service` (no BlueZ yet) does not stop BluForge starting, and a BluForge failure cannot stop Bluetooth or LANforge.
- `Restart=on-failure`, `RestartSec=3`; `WantedBy=multi-user.target` (starts at boot once enabled).

## `desktop/start-and-open.sh` and the `.desktop` file - the desktop icon
The icon runs this script in an xterm. It:
1. checks whether the server already answers on port 5480 (`is_up`, same Python snippet idea); if yes it just opens Firefox in a new window and exits;
2. otherwise starts a background "watcher" that opens the browser as soon as the server answers, then runs `sudo python3 -u server.py --port 5480` **in the foreground of that window**; closing the window stops the service and all running jobs (the server handles the hang-up signal).
**Known problem:** the `.desktop` file's paths point at `/home/lanforge/Desktop/EricssonBluForge/...`, which does not exist (the project lives under `Desktop/BluForge/EricssonBluForge`), so the icon would not work as it stands.

## `deploy/bluforge-bluez.sh` (installed as `bluforge-bluez`) - the BlueZ lifecycle tool
Purpose: Candela may sell BluForge only if **BlueZ is installed by the end-user**. This tool removes it at staging and lets the end-user install it.

### Top of the file
- `STATE_DIR` (`/var/lib/bluforge`, overridable with `BLUFORGE_STATE_DIR` for tests), `LOG_FILE` (`/var/log/bluforge-bluez.log`).
- Two marker files: `bluez-staged-removed` (staging removal done) and `bluez-user-installed` (the end-user installed it).
- Helpers `log/ok/warn/die` (coloured messages), `PM` = detected package manager (`dnf` or `apt`).
- `install_pkgs`: what to install - dnf: `bluez bluez-deprecated`; apt: `bluez libspa-0.2-bluetooth`. (`bluez-deprecated` holds `hcitool`, `hciconfig`, `sdptool` on Fedora.)
- `find_bluetoothd` (looks in the usual three paths) and `bluez_present` (daemon found **and** `bluetoothctl` on the PATH).

### `status [--json]`
Looks up the daemon path, version, whether `bluetoothctl/btmgmt/hcitool/sdptool` exist, the service's active/enabled state and both markers. Prints text, or one JSON object with `--json`.

### `install [--accept] [--offline-dir DIR] [--dry-run]`
1. Prints the notice (BlueZ is third-party GPL/LGPL software, not supplied by Candela, installed from your OS vendor's repository under its own licence).
2. `--dry-run`: prints the commands it would run and stops.
3. Needs root. Without `--accept` it asks "Install BlueZ now? [y/N]" (and refuses to run non-interactively without `--accept`).
4. Installs: from `--offline-dir` (`dnf install --disablerepo='*' DIR/*.rpm` or `apt-get install DIR/*.deb`) or from the online repositories.
5. Undoes what `lf_kinstall.pl` does to imaged units: `systemctl unmask bluetooth.service dbus-org.bluez.service`, then `enable --now bluetooth.service`.
6. Verifies that the daemon and `bluetoothctl` now exist, writes the `bluez-user-installed` marker, logs the result.

### `remove --staging [--dry-run] [--yes] [--force]`
1. Refuses without `--staging` (it is for initial staging only).
2. Refuses if `bluez-user-installed` exists (never remove a customer's install) and is a no-op if `bluez-staged-removed` exists (`--force` overrides either).
3. Builds the package list: on dnf, every installed package whose name starts with `bluez`, **except `bluez-libs` and `bluez-libs-devel`** - that shared library is needed by ffmpeg, Chromium, WirePlumber and others (removing it would take them out too; verified with a preview).
4. Shows a preview (`dnf remove --assumeno ...`, with `clean_requirements_on_remove=False` so unrelated packages are not stripped).
5. `--dry-run` stops there. Without `--yes` it also stops ("preview only").
6. With `--yes` it removes the packages and writes the `bluez-staged-removed` marker.

## How the GUI and server use it (code in `src/server.py` and `src/static/app.js`)
- `bluez_installed()` checks the daemon and `bluetoothctl` on disk; the status poll (`/api/status`) includes `"bluez": {installed, installing, can_install, notice}`.
- `bluez_gate()` makes `/api/start`, `/api/classic/action`, `/api/classic/reserve` answer **409** with an explanation while BlueZ is missing.
- `POST /api/bluez/install {"accept": true}` starts `bluforge-bluez install --accept` in a thread and streams its lines to the browser as `bluez` events; only one install can run at a time.
- `app.js`: `applyBluez` shows/hides the yellow banner; the button asks for confirmation (showing the notice), posts the install request, and appends streamed lines to the banner's log. When the final event arrives it refreshes status and the banner disappears.

## `docs/lf_kinstall-bluforge-stage.patch` - proposed change to Candela's `lf_kinstall.pl`
Not applied. It adds an option `--bluforge_stage` (used with `--do_image_prep`) that runs the removal command, and stops the script's fixed package list from re-adding `bluez bluez-tools` once the staging marker exists. See `docs/BLUEZ_INSTALL.md`.

## Other folders in the project (not covered by the code guide)
- `ble-lab/` - **an earlier, separate prototype** ("BE200 BLE Lab": `btlab.py`, `btlab_server.py`, `gui.html`, pcap tools, shell wrappers) with its own installer. **Its `install.sh` installs `bluez` automatically**, which conflicts with the new "end-user installs BlueZ" rule. Do not ship it with the appliance without changing that. It also contains old core-dump files (`core.*`, about 600 KB each).
- `recon/` - notes from studying the hardware (controller capabilities etc.).
- `deps/` - a dependency baseline.
- `logs/classic/` - the JSON/CSV results saved by the Classic tests (A2DP, HFP, cycle, AVRCP watch).
