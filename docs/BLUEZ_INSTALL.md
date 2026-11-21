# BluForge and BlueZ: staging removal and end-user install

As of 2026-10-26 Candela may sell BluForge as a Bluetooth Test Appliance on ct5xxx units with Intel BE200
radios, **provided BlueZ is installed by the end-user**. This document is the mechanism and the procedure.

## The tool: `bluforge-bluez` (`deploy/bluforge-bluez.sh`, installed to `/usr/local/sbin/bluforge-bluez`)

| Command | Who / when | What it does |
|---|---|---|
| `bluforge-bluez status [--json]` | anyone | Reports whether BlueZ (bluetoothd, bluetoothctl, btmgmt, hcitool, sdptool) is present and the service state. |
| `sudo bluforge-bluez remove --staging [--dry-run] [--yes]` | **Candela, initial staging only** | Previews, then removes the BlueZ daemon and tools. Refuses to run twice and refuses to remove a BlueZ the end-user installed. |
| `sudo bluforge-bluez install [--accept] [--offline-dir DIR] [--dry-run]` | **end-user**, after purchasing BluForge | Shows the third-party/licence notice, installs BlueZ from the OS repositories (or from local packages for air-gapped sites), unmasks and starts `bluetooth.service`. |

State markers in `/var/lib/bluforge/`: `bluez-staged-removed` (staging removal done) and `bluez-user-installed`
(the end-user installed it; after this the staging removal will not touch BlueZ again).
Every install/removal is logged to `/var/log/bluforge-bluez.log`.

### What gets installed / removed
- **Fedora / RHEL (dnf):** install `bluez bluez-deprecated` (`bluez-deprecated` provides `hcitool`, `hciconfig`, `sdptool`).
- **Debian / Ubuntu (apt):** install `bluez libspa-0.2-bluetooth` (the legacy tools ship inside `bluez`).
- **Staging removal removes the BlueZ daemon and tools only**: `bluez`, `bluez-deprecated`, `bluez-tools`, `bluez-obexd`,
  `bluez-mesh`, `bluez-hcidump`, `bluez-hid2hci`, `bluez-cups`, plus Bluetooth-only dependents the package manager
  pulls with them (`blueman`, `NetworkManager-bluetooth`, `pulseaudio-module-bluetooth` on the Fedora 39 reference unit).
- **`bluez-libs` is deliberately kept.** It is the shared `libbluetooth` library; removing it takes ffmpeg, Chromium,
  WirePlumber, the PipeWire utilities and others with it (verified with `dnf remove --assumeno` on the reference unit).
- Removal runs with `clean_requirements_on_remove=False` so the package manager does not strip unrelated packages.

## Staging procedure (Candela, once per unit, at imaging time)
```
sudo bluforge-bluez remove --staging --dry-run    # review the preview
sudo bluforge-bluez remove --staging --yes        # do it
```
It is a no-op if it has already run, so it is safe in an imaging script.

### lf_kinstall.pl hook (proposed - not applied; lf_kinstall.pl is maintained outside this project)
`lf_kinstall.pl` installs `bluez bluez-tools` in its package list (`@yum_pkgs_warn`, the `qw(d-feet bluez bluez-tools libreoffice)`
line) and, in the image-prep path, runs `systemctl disable bluetooth.service`, `disable dbus-org.bluez.service` and `mask dbus-org.bluez.service`.
To make this work with the appliance flow, add:
1. A guard on the package list so later lf_kinstall runs do not put BlueZ back:
   ```perl
   my $bluforge_staged = (-e "/var/lib/bluforge/bluez-staged-removed") ? 1 : 0;
   # ... in the qw() list use: d-feet libreoffice      and append:  bluez bluez-tools  unless $bluforge_staged
   ```
2. A new option, e.g. `--bluforge_stage`, run from the image-prep path:
   `do_cmd("/usr/local/sbin/bluforge-bluez remove --staging --yes");`
The installer already undoes the `mask`/`disable` above (`systemctl unmask ...; systemctl enable --now bluetooth.service`).

## End-user procedure
Either:
- Open the BluForge page. While BlueZ is missing, a banner explains this and offers **Install BlueZ...**
  (shows the notice, then runs the installer and streams its log). All Bluetooth actions are refused until it is installed.
- Or from a shell: `sudo bluforge-bluez install` (add `--offline-dir /path/to/rpms-or-debs` with no internet).

## Open decisions
- **"If they purchased BluForge"**: nothing here checks a purchase. The installer is available to whoever has the unit.
  If a licence key/file is wanted, the check belongs in `bluforge-bluez install` (and optionally the server start-up).
- **Whether `bluez-libs` may stay.** It is kept for the reason above; confirm that satisfies the sales/legal requirement.
- **Air-gapped units** need the BlueZ packages (and their dependencies) supplied as a directory for `--offline-dir`; that path is untested.
- **BLE tabs without BlueZ.** The raw-HCI engine talks to the kernel directly and may run without BlueZ userland; the page
  currently gates *everything* on BlueZ because the requirement says BlueZ must be installed to use BluForge.
- **Access control.** There is no login (changed 2026-11-19, see `ACCESS_CONTROL.md`): anyone who can reach the page can press the BlueZ install button, which still needs the licence notice accepted. Cross-site requests are refused.
