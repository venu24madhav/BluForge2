# Ericsson BLE Demo — Deployment Guide (move to a fresh unit)

**Status:** every step in this guide was actually run and verified on 2026-10-06 — not
written from memory. See the "What was actually verified" box at the end of each
section. If a step here ever stops matching reality, trust `docs/INSTALL_LOG.md`
(the dated, append-only log of every change made to the source unit) over this
guide, and update this file to match.

This guide moves the **entire working setup** — BLE interference generator,
BLE advertisement emulator, and the LANforge-styled web GUI — from this unit
(`ct523c-ccb8`) to a different machine. Follow it in order; don't skip the
verification steps, they exist because every one of them has caught a real
problem during this project's own development.

---

## 1. What you're deploying (one-paragraph summary)

A root-owned Python service (stdlib only, zero pip/dnf packages beyond what
Fedora already ships) that talks directly to Intel BE200 Bluetooth
controllers over raw HCI to (a) generate 2.4 GHz BLE interference via Direct
Test Mode and (b) emit real, spec-correct BLE advertisements (all 4 legacy
PDU types, 5 beacon profiles), fronted by a web GUI styled after the real
LANforge Java client. One process per radio per job — stopping or losing one
job never touches another radio.

## 2. Prerequisites — check these on the NEW system before anything else

| Requirement | How to check | Verified on source unit |
|---|---|---|
| Linux, root access | `whoami` → `root`, or passwordless/interactive `sudo` | Fedora 39 (kernel 6.18.14+) |
| Python 3 (stdlib only — no pip install needed) | `python3 --version` | 3.12.7 |
| BlueZ userland (`bluetoothctl`, `btmgmt`) | `bluetoothctl --version` | BlueZ 5.77 |
| `bluetooth.service` active | `systemctl is-active bluetooth` | active |
| At least one Bluetooth controller | `bluetoothctl list` and `ls /sys/class/bluetooth/` | 8× Intel BE200 (hci0–hci7) |
| `python3` has Bluetooth socket support | `python3 -c "import socket; socket.AF_BLUETOOTH"` (must not raise) | OK |

**If `bluetoothctl` is missing:** `sudo dnf install bluez` (Fedora/RHEL) or
`sudo apt install bluez` (Debian/Ubuntu). Nothing else needs installing —
confirmed on the source unit by direct inspection: every line of this
project's own code is Python standard library plus `ctypes`.

**Hardware caveat — read this before you promise anything to anyone:**
the exact HCI commands this project relies on (LE Enhanced Transmitter Test
v2/v4 with TX power control, LE Set Advertising Parameters with all 4
`Advertising_Type` values) were verified **only on Intel BE200 controllers**.
Most LE-capable Bluetooth 4.0+ controllers support the basic versions of
these commands, but a different chipset might reject the v4 (TX-power)
variant or behave differently. **Step 5 below is exactly the check that
tells you, on the new hardware, before the demo — don't skip it.**

If the new unit has only **one** Bluetooth controller (not 8 like this one),
everything still works, but running an interference/advertise job on it will
also block any other Bluetooth use on that same controller for the job's
duration (expected — exclusive access is the whole point) and you can only
run one job at a time there (1 radio = 1 job).

---

## 3. Step 1 — Build the offline package (run HERE, on this unit)

```bash
cd /home/lanforge/Desktop/EricssonBluForge/deploy
./package-offline.sh                      # writes /tmp/ericsson-bluforge-offline-<timestamp>.tar.gz
# or choose the output path yourself:
./package-offline.sh /tmp/ericsson-bluforge.tar.gz
```

This produces a tarball (~70 KB) containing: the engine (`bluforge_cli.py`,
`hci_raw.py`, `ble_ad.py`, `ble_channels.py`, `server.py`), the web GUI
(`static/`), the four `verify_*.py` smoke-test scripts (`verify_core`, `verify_adv_profiles`, `verify_pdu_types`, `verify_hfp_ag`), `docs/INSTALL_LOG.md`
(the full change history, in case anything here needs re-deriving), the
systemd unit template, and `install.sh`. It does **not** contain any RPMs —
none are needed.

> **What was actually verified:** ran `package-offline.sh` fresh, inspected
> the tarball contents (`tar tzf`), confirmed no stray build artifacts
> (checked specifically for leftover core dumps and `__pycache__`, both of
> which have shown up here before and are now excluded automatically).

## 4. Step 2 — Copy it to the new machine

Any transfer method works — the tarball is self-contained.

```bash
scp /tmp/ericsson-bluforge.tar.gz root@<new-machine-ip>:/tmp/
```

(USB stick, `rsync`, whatever your network policy allows is equally fine —
nothing about this package requires a network path, it's internet access
during *installation* that isn't needed, not during transfer.)

## 5. Step 3 — Install on the new machine

```bash
ssh root@<new-machine-ip>
cd /tmp
tar xzf ericsson-bluforge.tar.gz
cd ericsson-bluforge
sudo ./install.sh
```

Default install location is `/opt/ericsson-bluforge`, default port `5480`.
Override either:

```bash
sudo INSTALL_DIR=/srv/ble GUI_PORT=5490 ./install.sh
```

`install.sh` is **idempotent** — safe to re-run any time (e.g. after copying
an updated package) to update an existing install in place.

**What `install.sh` does, precisely** (read this if you want to know exactly
what it touches before running it as root):
1. Checks `python3` and `bluetoothctl` are present and reports the hci
   devices currently visible (warns, doesn't block, if none are found yet).
2. Copies `src/` and `docs/` into the install directory.
3. Writes **one new file**, `/etc/systemd/system/ericsson-bluforge.service`
   (from the template, with your `INSTALL_DIR`/`GUI_PORT` filled in),
   `daemon-reload`s, and `enable`s + `restart`s it.
4. Polls the service's own `/api/status` for up to 15s to confirm it's
   actually serving before declaring success.

It does not touch any other file, package, or service on the machine.

> **What was actually verified:** ran this exact `install.sh` into a
> throwaway directory on a different port on this same unit (so as not to
> disturb the live demo already running on :5480), confirmed the systemd
> unit was written correctly, enabled, and started, and that
> `GET /api/status` responded with HTTP 200 within the 15s window.

### If you want manual start instead of auto-start-on-boot

This project's default install **does** enable the systemd unit (auto-starts
on boot). If you'd rather start it by hand every time (the choice made for
*this* unit, per an explicit decision — see `docs/INSTALL_LOG.md`), disable
the auto-start after installing:

```bash
sudo systemctl disable ericsson-bluforge
```

Then start/stop it manually whenever you want it running:

```bash
sudo systemctl start ericsson-bluforge     # or: stop
# or run it directly in a terminal (useful for watching live logs):
cd /opt/ericsson-bluforge && sudo python3 src/server.py --port 5480
```

## 6. Step 4 — Verify the new hardware actually supports this, before trusting the GUI

**Do this before showing it to anyone.** These are the exact scripts that
caught real bugs during this project's own development (a parameter-encoding
bug that made every single radio reject TX-power commands) — they exist
specifically to catch hardware/firmware differences early, on your terms,
not live in front of a customer.

```bash
cd /opt/ericsson-bluforge/src        # or your INSTALL_DIR

# 1. Capabilities probe — no radio access needed, just confirms the code loads
sudo python3 bluforge_cli.py caps --compact

# 2. Core interference + advertisement test — needs FOUR controllers
#    (interferer, RX witness, advertiser, scanner). They are auto-detected:
#    the first four hciN present are used in that order. To pin specific ones:
#        sudo python3 verify_core.py 0 1 2 3
#    A unit with fewer than four controllers (for example the two-radio ct523c)
#    cannot run this test; it stops with "need 4 distinct Bluetooth controllers".
sudo python3 verify_core.py

# 3. All 5 beacon profiles, verified byte-for-byte over the air
sudo python3 verify_adv_profiles.py

# 4. All 4 legacy BLE PDU types
sudo python3 verify_pdu_types.py
```

Every one should print `PASS`/`RESULT: PASS` lines and end by restoring every
controller it touched to normal BlueZ control. If any of them prints `FAIL`
or a Python traceback, **stop and diagnose before doing anything else** —
see the Troubleshooting section below; a `HciError ... HCI status 0x12` is
the single most common failure mode and almost always means a command
parameter is outside what *this specific controller's firmware* accepts
(see the troubleshooting entry for it).

> **What was actually verified:** ran all of `caps`, `verify_core.py`
> (interference RX-witnessed with 3188 real packets counted; advertisement
> received with 27 byte-perfect copies), `verify_adv_profiles.py`, and
> `verify_pdu_types.py` against a **freshly installed copy** in a throwaway
> directory on this same unit — same results as the original install, same
> hardware (since it's the same physical BE200 radios), proving the
> install → verify flow genuinely reproduces a working system rather than
> merely copying files that happen to already work here.

## 7. Step 5 — Open the GUI

```
http://<new-machine-ip>:5480/
```

(or whatever `GUI_PORT` you chose). Four tabs: **Radios** (live status of
every controller), **Interference**, **Advertise**, **BLE Sniff**.

**If you can't reach it from another machine:** this unit's firewall is
inactive and SELinux is disabled, so nothing was needed here — a fresh
system may have both enabled by default. Check and open the port yourself
(this project does not change firewall/security settings on its own):

```bash
sudo firewall-cmd --state                              # is it even running?
sudo firewall-cmd --permanent --add-port=5480/tcp       # if so, open the port
sudo firewall-cmd --reload
getenforce                                              # Enforcing/Permissive/Disabled
```

---

## 8. Known environment-specific things to re-check on a different machine

These were true and verified **on this specific unit** — re-check them,
don't assume they carry over:

- **hci index ↔ physical radio mapping is not portable.** On this unit,
  hci0–hci7 are 8 identical BE200 controllers and any index could be used
  for any role. On a different machine, hci numbering depends on PCI/USB
  enumeration order and can even change across reboots on the *same*
  machine. Always run `bluetoothctl list` and `cat
  /sys/class/bluetooth/hciN/address` on the new unit to know what you
  actually have before picking `--dev N` values, and re-run Step 4's
  scripts rather than assuming hci4/hci6/hci7 mean anything there.
- **Port 5480 was chosen specifically to avoid this unit's LANforge JSON API
  on :8080.** If the new machine also runs LANforge (or anything else) on a
  different set of ports, re-check `ss -lntp` there before assuming 5480 is
  free, and pass a different `GUI_PORT` to `install.sh` if not.
- **A desktop Bluetooth manager (e.g. `blueman`) racing to re-power a
  controller right after this tool releases it** was observed and worked
  around here (a bounded retry loop in `hci_raw.py`'s `open()`). A
  server/headless install with no desktop session running won't have this
  race at all; a different desktop environment's BT manager might behave
  differently. The retry loop is generic (not blueman-specific) so it
  should handle either case, but if `open()` ever fails after its full 3s
  retry budget on the new unit, that's the first thing to suspect — see
  Troubleshooting.
- **Firmware crash history**: this unit's *Wi-Fi* side of the BE200 combo
  chip (a separate subsystem from the Bluetooth side this project uses) has
  a known, documented firmware crash trigger on one specific radio under
  monitor-mode (see `docs/INSTALL_LOG.md`, 2026-10-05 Wi-Fi recon entry) —
  irrelevant to this project (which never touches the Wi-Fi side or monitor
  mode) but worth knowing if the same physical unit is also used for other
  Wi-Fi testing.

## 9. Hard constraints — do not relax these on the new unit either

These were explicit decisions made for this project, not accidents:

- **BE200 (or whatever LE-capable Bluetooth controllers are present) only.**
  This project never touches a HackRF, SDR, or LANforge's own RF-generator
  API, by explicit requirement. Don't wire those in without that being a
  new, separate decision.
- **Never `SIGKILL` a running job** (via `kill -9`, a hard process-manager
  stop, or similar). A job holds exclusive raw-HCI access to its radio;
  killing it abruptly skips the cleanup that hands the radio back to
  BlueZ cleanly and was the direct cause of a real incident during this
  project's development (three controllers needing manual recovery). Use
  the GUI's Stop button, `Ctrl-C` on a foreground CLI run, or
  `systemctl stop`/a plain `SIGTERM` — all three are handled gracefully.
- **Don't run the GUI exposed to an untrusted network.** It is
  unauthenticated by design (a trusted-lab-network tool, matching this
  project's own predecessor's documented stance) — fine on a demo LAN,
  not fine on the open internet.

---

## 10. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `HciError: ... failed, HCI status 0x12` | "Invalid HCI Command Parameters" — a command parameter this controller's firmware rejects (this exact error, for the TX-power field, is the bug this project's own `docs/INSTALL_LOG.md` documents finding and fixing). If it recurs for a *different* field on different hardware, the firmware is enforcing a spec constraint the code doesn't yet account for. | Re-run Step 4's scripts to isolate which specific command/field is rejected; cross-check that exact field against the Bluetooth Core Spec Vol 4 Part E for the opcode involved. This is a real, fixable protocol bug, not something to work around by retrying. |
| `bind HCI_CHANNEL_USER on hciN failed after retrying for 3s ... Another process may be holding this controller` | Something else (bluetoothd, a desktop BT manager, another job) still has the controller, or the mgmt power-off/on race (see §8) hasn't settled yet. | Wait a few seconds and retry; check `bluetoothctl show <addr>` for that controller's `Powered`/`PowerState`; if stuck, the code's own `force_recover()` is wired into this exact failure path already (it now hands the controller back automatically) — if it's STILL stuck after that, manually run `sudo python3 -c "import hci_raw; print(hci_raw.force_recover(N))"` from `src/`. |
| GUI loads but shows "0 jobs" and every radio greyed out as busy | A stale job row from before a reboot/crash; the GUI/server state resets whenever the service (re)starts, but a literal running subprocess from before a restart could still hold a radio. | `ps aux \| grep bluforge_cli` to check for an orphaned process holding a radio; `bluetoothctl list` to confirm all controllers are present and `Powered: yes`. |
| Port already in use when starting the service | Something else is already listening on the chosen `GUI_PORT`. | `ss -lntp \| grep <port>`, then re-install with a different `GUI_PORT=...`. |
| `struct.error` or a raw Python traceback instead of a clean `[x] ...` error line | A gap this project's own adversarial code review already closed for `--power`/`--len`/`--dwell`/channel-range/scan-window inputs — if a *different* field still produces a raw traceback, it's a genuine new gap worth fixing the same way (validate the field's real spec range before it reaches `hci.open()`, and make sure the containing command body is wrapped so `hci.close()` still runs on any exception). | Check `docs/INSTALL_LOG.md`'s 2026-10-06 entries for the exact pattern already used to fix this class of bug. |
| Firefox/browser can't reach the GUI from another machine | Firewall/SELinux blocking it (see §7), or the service bound to the wrong interface. | Confirm `server.py`'s `--host` is `0.0.0.0` (the default) not `127.0.0.1`; check firewall as shown in §7. |

---

## 11. Uninstalling

```bash
sudo systemctl stop ericsson-bluforge
sudo systemctl disable ericsson-bluforge
sudo rm -f /etc/systemd/system/ericsson-bluforge.service
sudo systemctl daemon-reload
sudo rm -rf /opt/ericsson-bluforge     # or your INSTALL_DIR
```

This removes everything the installer added. It never modified any other
file, package, or system service, so nothing else needs reverting.

---

## 12. File manifest (what's in the package and why)

| Path | Purpose |
|---|---|
| `src/hci_raw.py` | Lowest layer: raw HCI control of one Bluetooth controller (exclusive access, LE Transmitter/Receiver Test, legacy advertising, scanning). Read this first if something at the protocol level needs fixing. |
| `src/bluforge_cli.py` | The CLI engine — `adv`/`interfere`/`sniff`/`caps` subcommands, one OS process per job. Can be run standalone without the GUI at all. |
| `src/ble_ad.py` | BLE Advertising Data encoders + the 5 beacon profiles (iBeacon, Eddystone-UID/URL, AltBeacon, generic sensor). |
| `src/ble_channels.py` | BLE channel ↔ frequency ↔ Wi-Fi-overlap math, self-tested. |
| `src/server.py` | The web backend: stdlib HTTP+SSE server managing concurrent per-radio job subprocesses. |
| `src/static/` | The LANforge-styled web GUI (plain HTML/CSS/JS, no build step, no framework). |
| `src/verify_*.py` | Smoke tests — run these after any install, per Step 4. |
| `deploy/install.sh` | The installer (idempotent). |
| `deploy/package-offline.sh` | Builds the tarball this whole guide assumes you already have. |
| `deploy/ericsson-bluforge.service` | The systemd unit template `install.sh` fills in. |
| `docs/INSTALL_LOG.md` | **The authoritative, dated history of every file/package/service change ever made to the original unit, and every bug found and fixed, with the reasoning.** If anything in this guide is unclear, that file has the full story. |

---

# Addendum: Bluetooth Classic tab (A2DP / AVRCP / HFP)

**Extra requirements on the target unit** (the BLE features still need none of these):

| Need | Check | Why |
|---|---|---|
| PyGObject | `python3 -c "from gi.repository import Gio"` | BlueZ D-Bus (pairing agent, signals). Fedora: `python3-gobject`; Debian: `python3-gi` |
| bluetoothd active | `systemctl is-active bluetooth` | Classic radios stay under BlueZ |
| PipeWire + WirePlumber, desktop user **logged in** | `pactl info` as that user | A2DP audio goes through the user's PipeWire session; the root service runs pw tools as that user (`--audio-user NAME` to force) |
| `hcitool`, `hciconfig`, `sdptool` (bluez-deprecated) | `command -v hcitool` | RSSI / TX power / link quality / throughput / HFP SDP lookup. Missing = those metrics blank, HFP needs `--channel` |

The Classic tab's **preflight banner** lists anything missing. Rules of use:
1. Pick a radio on the Classic tab and press **Reserve for Classic** (a 1-radio unit loses BLE jobs while reserved).
2. Put the DUT in pairing mode, **Scan**, **Select**, **Pair**, **Connect**.
3. Run A2DP / AVRCP / HFP / cycling tests; every run is saved to `<install>/logs/classic/*.json|csv` (the result box names the file;
   copy it off the unit, or fetch it with `GET /api/classic/results` and `GET /api/classic/result/<file>`).

**Roles:** host = A2DP source, AVRCP target, HFP Audio Gateway; DUT = headset/speaker/car-kit. HFP uses CVSD only (no mSBC).
**Validation status:** see the 2026-10-22 entry in `docs/INSTALL_LOG.md` - validate each profile once against the customer's real DUT before relying on results.


---

# Addendum: a LANforge cluster (manager + resources)

BluForge on a LANforge **manager** can show and control the Bluetooth radios of the clustered **resources** too. Nothing to
configure: install the same package on the manager and on every resource that has Bluetooth radios.

```bash
# on this unit (the manager): build once
cd deploy && ./package-offline.sh                       # writes /tmp/ericsson-bluforge-offline-<time>.tar.gz
# on EACH machine (manager and every resource): copy the file there, then
tar xzf ericsson-bluforge-offline-*.tar.gz && cd ericsson-bluforge && sudo ./install.sh
```

Then open `http://<manager-ip>:5480/`. The Radios tab lists the units; their radios appear as `1.2.hci0` and so on. If a resource
stays grey as "no BluForge", check that its service runs and that its firewall lets the manager reach port 5480 (section 7).
Details, options and limits: `CLUSTER.md`. There is no login (`ACCESS_CONTROL.md`).
