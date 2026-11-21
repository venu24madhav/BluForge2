# 04 - Environment and Dependency Baseline (ct523c-ccb8, 10.28.3.73)

Recon date: 2026-10-05 (unit clock, UTC+05:30). All items below were verified by running commands on the unit as root.
Strictly read-only: nothing installed, no service/radio/rfkill/HCI changes, no API writes.
Side note: `dnf repolist` (requested) may have refreshed dnf's metadata cache under /var/cache/dnf; nothing else was written outside the two permitted directories (scratch test scripts went to the session scratchpad).

Baseline snapshot files (all under `/home/lanforge/Desktop/EricssonBluForge/deps/baseline/`):

| File | Content |
|---|---|
| rpm_baseline.txt | 2547 RPMs, `NAME-VERSION-RELEASE.ARCH`, sorted |
| pip_freeze_venv-3.12.txt / pip_freeze_system_python3.txt | 104 / 110 pip packages (pip itself is excluded by freeze); .err files empty |
| python_import_check.txt | import-by-import result for ~70 modules in both interpreters |
| systemd_enabled_unitfiles.txt, systemd_running_services.txt, etc_systemd_system_listing.txt, systemd_version.txt | unit state (systemd 254.20-1.fc39) |
| ss_lntup.txt, ip_br_a.txt, network_reachability.txt, firewall_selinux.txt | network, firewall, SELinux |
| uname.txt, os_release.txt, fedora_release.txt, tool_versions.txt, disk_mem.txt | OS and tool versions, free disk and RAM |
| dnf_repolist.txt, yum_fedora.repo.txt | repos |
| rfkill_list.txt, lsusb.txt, lspci_nn.txt, lsmod.txt, bluetooth_main.conf.txt | hardware and BT config |
| ls_home_lanforge.txt, ps_snapshot.txt, sha256_key_files.txt | LANforge dir names, process list, hashes of LANforge startup files |

To diff later: `rpm -qa --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' | sort | diff - rpm_baseline.txt` and `pip freeze | diff - pip_freeze_*.txt`.

---

## 1. Baseline facts

| Item | Verified value |
|---|---|
| OS | `Fedora release 39 (Thirty Nine)`, os-release `VERSION="39 (MATE-Compiz)"` |
| Kernel | `Linux ct523c-ccb8 6.18.14+ #84 SMP PREEMPT_DYNAMIC Fri Mar 13 16:41:15 PDT 2026 x86_64` |
| CPU / RAM / swap | 8 cores; `Mem: 15Gi total, 5.1Gi used, 10Gi available`; swap 8.0Gi unused |
| Disk | `/` 49G (28G free), `/home` 164G (127G free), /tmp is tmpfs 7.8G |
| Python | `/usr/bin/python3` = 3.12.7 (`/usr/bin/python3.12`); pip 23.2.1 |
| Node / npm | v20.17.0 / 10.8.2 (RPM); `npm ls -g` is empty, so no global node modules |
| gcc / git / curl | gcc 13.3.1, git 2.47.0, curl 8.2.1 (OpenSSL 3.1.4) |
| Java | OpenJDK 17.0.13 (runs the LANforge GUI) |
| Other tools present | nginx 1.26.2 (RPM, not enabled), sqlite3, openssl, jq, tmux, screen, ncat/nc, virtualenv; absent: uv, socat, websocat |
| Bluetooth userland | BlueZ 5.77 RPM: bluetoothctl, btmgmt, btmon, btattach, busctl present. ABSENT: hcitool, hciconfig, hcidump, gatttool, l2ping, hciattach (bluez-deprecated / hcidump not installed) |
| HackRF userland | `/home/lanforge/local/bin/` has hackrf_info, hackrf_transfer, hackrf_sweep, hackrf_tx_ct, hackrf_debug etc. and `SoapySDRUtil`; `libhackrf.so.0.9.0` is in `/home/lanforge/local/lib` (NOT on the default linker path; `lanforge.profile` adds it). They are not on a plain root PATH (`command -v hackrf_info` is empty). RPMs also present: SoapySDR 0.8.1, gnuradio 3.10.6, gr-osmosdr, rtl-sdr, uhd, hackrf-firmware |
| Kernel BT/Wi-Fi modules | btusb, btintel, btbcm, btmtk, btrtl, bluetooth, rfcomm, bnep; iwlwifi + `iwlmld` (new Intel MLD opmode for BE200) |
| Hardware | 8 x `8087:0036` Intel BT USB devices, 1 x `1d50:6089 HackRF One` (Bus 007 Dev 003), 8 x Intel `8086:272b` Wi-Fi 7, 3 x QCA9984, X550 + I211 NICs |
| rfkill | hci0-hci7 and all phys: `Soft blocked: no / Hard blocked: no` |
| Interfaces | `eth0 10.28.3.73/24` (mgmt, default route), `eth1 10.28.3.132/24`, `eth2 172.16.0.1/16` (LANforge test LAN with nginx:80 + dhcpd), eth3 down, `_vrf3`, `vrf10003` |
| Repos | fedora, fedora-cisco-openh264, rpmfusion-free, rpmfusion-free-updates, updates |
| BlueZ D-Bus tree | `busctl --system tree org.bluez` lists /org/bluez/hci0 .. hci7 |

Gotcha: `bluetoothd` has `ProtectHome=true`, `ProtectSystem=strict`, `CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE` (from `systemctl cat bluetooth.service`). It cannot read /home. Do not rely on bluetoothd reading anything from our project dir.

---

## 2. Python environments and what each already has (checked by import)

There are exactly two interpreters, both the same CPython 3.12.7 binary:

1. **System**: `/usr/bin/python3`, packages from RPM (`python3-*`) plus a few pip ones. No third-party web framework.
2. **LANforge venv**: `/home/lanforge/scripts/venv-3.12/bin/python` (`pyvenv.cfg`: `include-system-site-packages = false`, home=/usr/bin). Hosts the Django interop WebUI (port 5454) and celery. DO NOT TOUCH.

Trap: for the `lanforge` user, login shells (`su -l lanforge`) have `/home/lanforge/scripts/venv-3.12/bin` FIRST in PATH, so bare `python3` there is the venv (verified: `which python3` -> `/home/lanforge/scripts/venv-3.12/bin/python3`). In a plain root shell `python3` is `/usr/bin/python3`. Our service must use an absolute interpreter path.

| Module | System `/usr/bin/python3` | LANforge venv-3.12 |
|---|---|---|
| flask | MISSING | MISSING |
| fastapi / uvicorn / starlette | MISSING | MISSING |
| aiohttp | MISSING | OK 3.14.3 |
| websockets | MISSING | MISSING |
| requests | OK 2.28.2 | OK 2.34.2 |
| httpx | MISSING | OK 0.28.1 |
| dbus (dbus-python) | **OK 1.3.2** | MISSING |
| dbus_fast / pydbus | MISSING / MISSING | MISSING / MISSING |
| dasbus (GLib D-Bus lib) | **OK 1.7** | MISSING |
| gi (PyGObject) | **OK 3.46.0** | MISSING |
| bleak / bumble / scapy | MISSING | MISSING |
| numpy | OK 1.24.4 | OK 2.4.6 |
| scipy | OK 1.11.1 | OK 1.17.1 |
| serial (pyserial) | MISSING | OK 3.5 |
| jinja2 | MISSING | MISSING (Django has its own template engine) |
| Django / channels / celery | MISSING | OK 6.0.8 / 4.3.2 / 5.6.3 |
| tornado | MISSING | OK 6.5.10 |
| h11 / wsproto | MISSING | OK 0.16.0 / 1.3.2 |
| websocket-client (client only) | OK 1.3.3 | OK 1.9.0 |
| pyudev | OK 0.24.1 | MISSING |
| psutil | OK 5.9.5 | OK 7.2.2 |
| PyYAML (`yaml`) | OK 6.0.1 | MISSING |
| Mako | OK 1.2.3 | MISSING |
| cffi | OK 1.15.1 | OK 2.0.0 |
| orjson / pandas / matplotlib / plotly | MISSING | OK |
| paramiko / redis / pexpect | MISSING / MISSING / OK | OK / OK / OK |
| stdlib: http.server, socketserver, asyncio, ssl, sqlite3, json, ctypes, socket, select, struct, selectors, wsgiref | all OK | all OK |
| `socket.AF_BLUETOOTH`, `BTPROTO_HCI`, `BTPROTO_L2CAP`, `BTPROTO_RFCOMM` | all True | same stdlib |
| `osmosdr` (gr-osmosdr) | MISSING by default (`libhackrf.so.0: cannot open shared object file`) but **OK when `LD_LIBRARY_PATH=/home/lanforge/local/lib:/home/lanforge/local/lib64`** (import only; no device opened) | n/a |
| `gnuradio` | OK (`/usr/lib64/python3.12/site-packages/gnuradio`) | n/a |
| `SoapySDR` python binding | MISSING | MISSING |
| `bluetooth` (PyBluez) | MISSING | MISSING |

Raw HCI socket check (socket created and closed, never bound or sent): `socket.socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI)` succeeded as root AND as user `lanforge` in `/usr/bin/python3`. Creation succeeding does not prove bind/commands work for non-root; binding to a controller needs CAP_NET_RAW/CAP_NET_ADMIN and was not tested (it would touch the controller).

### Recommendation: runtime for the new web service (nothing to install)

**Use `/usr/bin/python3` (3.12.7) with the standard library, plus libraries already in that interpreter.**

- Web server: `http.server.ThreadingHTTPServer` (or `asyncio` streams) serving static HTML/CSS/JS and a small JSON API.
- Live updates to the browser: **Server-Sent Events** over the same HTTP server (browser `EventSource`, zero deps). If real two-way WebSocket is wanted, hand-write the RFC 6455 handshake with `hashlib`/`base64`/`struct` (about 80 lines); no `websockets` package exists on the system python.
- LANforge JSON API at `http://localhost:8080/`: `urllib.request` (stdlib) or `requests` 2.28.2 (already installed).
- BlueZ control/scan/advertising: `dbus-python` 1.3.2 + `gi.repository.GLib` (PyGObject 3.46.0), or `dasbus` 1.7. These exist ONLY in the system python, which is the main reason not to pick the venv.
- Raw HCI (custom HCI commands, LE test modes): stdlib `socket.AF_BLUETOOTH`/`BTPROTO_HCI`, needs root.
- Signal/spectrum math: `numpy` 1.24.4, `scipy` 1.11.1. Hotplug: `pyudev` 0.24.1. Host stats: `psutil` 5.9.5. Config: `PyYAML` 6.0.1.
- HackRF: via the LANforge-managed `hackrf_*` CLIs in `/home/lanforge/local/bin` (with `LD_LIBRARY_PATH=/home/lanforge/local/lib`) or via LANforge's own RF-generator API; `ctypes` on `/home/lanforge/local/lib/libhackrf.so.0` is also possible. No python hackrf binding is installed.
- Front end: hand-written static files, no build step. Node 20/npm exist but there are no global modules and no registry access if the demo is offline.

Why not the venv: it belongs to LANforge (Django/celery), `lf_kinstall.pl` may rebuild it, and it has no dbus/gi. Its `aiohttp` 3.14.3 (HTTP + WebSocket server) and `tornado` are a viable fallback ONLY if we decide to talk to BlueZ via subprocess (`busctl`/`bluetoothctl`) or raw sockets, and even then we would depend on LANforge's package set. Not recommended.

Optional later: if a server framework is really wanted, `dnf install python3-flask`/`python3-aiohttp` is possible only while online (see section 3), and would then have to be replicated on every target unit. Avoid unless needed.

---

## 3. Network reachability (timeouts 8-10s)

| Test | Result |
|---|---|
| `curl -sI https://pypi.org` | `HTTP/2 200` |
| `curl -sI https://github.com` | `HTTP/2 200` |
| `curl -sI https://download.fedoraproject.org` | `HTTP/2 302` (redirect to mirror, reachable) |
| `curl -sI https://cdnjs.cloudflare.com` | `HTTP/2 200` |
| `https://pypi.org/simple/bleak/` | `HTTP/2 200` |
| `https://mirrors.fedoraproject.org/metalink?repo=fedora-39&arch=x86_64` | `HTTP/2 200`, returns metalink (repomd timestamp 1698797559 = 2023-11-01 for the release repo) |
| `https://dl.fedoraproject.org/pub/archive/fedora/linux/updates/39/Everything/x86_64/repodata/repomd.xml` | `HTTP/1.1 200 OK` |
| DNS | works: `getent hosts` resolved pypi.org, github.com, download.fedoraproject.org, cdnjs.cloudflare.com (IPv6 answers shown for most). `/etc/resolv.conf`: `nameserver 8.8.8.8`, `options timeout:1` |
| ICMP | 8.8.8.8: 1/1 received, 7.8 ms; gateway 10.28.3.1: 1/1, 0.26 ms |
| Proxy env vars | none set; `pip config list` is empty |

**Verdict: the unit currently HAS working internet** (DNS, HTTPS to PyPI, GitHub, Fedora mirrors, cdnjs). So `pip install` and `dnf install` would technically work today (Fedora 39 is past its end-of-life date per general knowledge, not checked on-box; the mirrors still answered). Actual installs were NOT attempted (rule: no installs).

Do not count on this at the demo. The customer lab may be air-gapped. Recommendation: **no external CDN, no web fonts from Google, no `pip` at start-up.** Ship every JS/CSS/font/icon file inside our own static directory. Installed fonts on the unit (fc-list): DejaVu Sans, DejaVu Sans Mono, Liberation Sans/Serif/Mono, Cantarell, Droid Sans, Carlito, Caladea, and more. These are on the unit, NOT necessarily on the viewing laptop, so use a font stack like `system-ui, "Segoe UI", Arial, "Liberation Sans", "DejaVu Sans", sans-serif`, or bundle a self-hosted .woff2.

---

## 4. Ports, firewall, SELinux

### Listening TCP ports in use (`ss -lntp`, verified)

| Port | Bind | Process |
|---|---|---|
| 22 | 0.0.0.0, [::] | sshd |
| 80 | 10.28.3.73 | httpd (Apache, LANforge pages); also `172.16.0.1%eth2:80` nginx (LANforge's own nginx) |
| 111 | 0.0.0.0, [::] | rpcbind |
| 443 | 10.28.3.73 | httpd |
| 631 | 127.0.0.1, ::1 | cupsd |
| 2049 | 0.0.0.0, [::] | nfs |
| 3389 | * | xrdp |
| 3999 | 127.0.0.1 | gnuserver (LANforge) |
| 4001, 4002 | 0.0.0.0 | btserver manager pid 3867 (run_mgr_0 passes `--cli_port 4001 --gui_port 4002`); 4002 also UDP |
| 4003, 4004 | 0.0.0.0 | btserver resource pid 4245; 4004 also UDP |
| 4369 | * | epmd; 5672 AMQP and 25672 | rabbitmq (beam.smp) |
| 5037 | 127.0.0.1 | adb |
| 5454 | 10.28.3.73 | **LANforge interop WebUI** (python3 manage.py runserver, venv) |
| 5901, 6001 | 0.0.0.0, [::] | Xvnc |
| 6379 | 127.0.0.1, ::1 | redis |
| **8080** | **0.0.0.0** | **java pid 72529 (LANforge GUI) = JSON API** |
| 8081 | 127.0.0.1 | same java pid 72529 |
| 20048 | 0.0.0.0, [::] | rpc.mountd |
| 54069 (rpc.statd), 45595 and [::]:35261 (no owner shown), [::]:45979 (rpc.statd) | | NFS/rpc dynamic |
| UDP 1812/1813/18120 | | radiusd; UDP 67 dhcpd on eth2 |

Important: the JSON API on :8080 is served by the **LANforge Java GUI process** (`ss -lntp` shows pid 72529 = `/usr/bin/java ... lfgui` owning both 8080 and 8081). `GET http://localhost:8080/` returned `HTTP 200, 14025 bytes`. If someone closes the GUI the API disappears, so our service must handle "API down" gracefully and show it in the UI.

Other known Candela web stack (installed under /home/lanforge/local/webGUI, NOT running now) defaults to 8686, 5858, 8442, 8443, 8123 (ClickHouse), 5432 (Postgres). Avoid those too.

### Proposed port

- **Primary: TCP 5480** (next to LANforge's WebUI on 5454, in the requested 5470-5499 range).
- **Fallback: TCP 8850** (inside 8800-8899).
- Verified during this recon that no listener or socket in any state exists on 5470-5499 or 8800-8899 (`ss -ant` scan produced nothing), and neither port appears in any LANforge config grep. Both are below the kernel ephemeral range (`ip_local_port_range = 10000 61001`), so outbound connections cannot steal them.
- Bind to `0.0.0.0:5480` (IPv4). Reasons: LANforge's own WebUI hard-codes `10.28.3.73` in `runwebgui.bash`, which breaks if the DHCP address changes. Browse to `http://10.28.3.73:5480/`.
- Caution on addresses: `eth1` has `10.28.3.132/24` in the same subnet as eth0, but the main routing table has only `10.28.3.0/24 dev eth0` (+ default via eth0). Use **10.28.3.73 (eth0)** as the canonical URL; .132 may reply asymmetrically. `eth2` (172.16.0.1/16) is the LANforge test LAN; binding 0.0.0.0 also exposes the UI there, restrict with an explicit bind if that matters.

### Firewall and SELinux (verified)

| Check | Result |
|---|---|
| `getenforce` | `Disabled` (`SELINUX=disabled` in /etc/selinux/config) |
| firewalld | `inactive`, unit is **masked** (`/etc/systemd/system/firewalld.service -> /dev/null`); `firewall-cmd --state` -> `not running` |
| iptables/ip6tables/nftables units | all inactive / not found |
| `nft list ruleset` | empty |
| `iptables -S`, `-t nat -S`, `ip6tables -S` | all chains `-P ... ACCEPT`, no rules. Warning printed: `iptables-legacy tables present`; `/proc/net/ip_tables_names` lists `nat` and `raw` (legacy). The `iptables-legacy` binary is not installed, so the contents of those two legacy tables could not be inspected. No legacy `filter` table exists. |
| Inbound proof | an established SSH session from `10.253.8.141` (another subnet) to 10.28.3.73:22 exists; LANforge GUI/API on 8080 and WebUI on 5454 are served on LAN addresses |

Conclusion: a new listening port should be reachable from 10.28.3.0/24 with no firewall change (no firewalld, empty nft ruleset, ACCEPT policies, SELinux disabled). Residual uncertainty: the legacy `raw` table could in theory drop packets and could not be read; confirm with one test from another host (`curl -m5 http://10.28.3.73:5480/` or `nc -zv 10.28.3.73 5480`) once the service exists. Not verified from a second host in this recon (none available).

---

## 5. How LANforge services and autostart are organised

**Mechanism: SysV script wrapped by systemd, plus per-process supervisor loops. Not native systemd units.**

1. `/etc/rc.d/init.d/lanforge` (38821 bytes, header `# chkconfig: 235 99 1`). `/home/lanforge/serverctl.bash` is a byte-identical copy (`cmp` says identical). systemd-sysv-generator turns it into `lanforge.service`, generated at `/run/systemd/generator.late/lanforge.service`: `Type=forking`, `RemainAfterExit=yes`, `After=network-online.target`, `Before=multi-user.target graphical.target`, `ExecStart=/etc/rc.d/init.d/lanforge start`. There is no file for it in /etc/systemd/system. Status: `lanforge.service loaded active running LSB: LANforge server processes.`
2. `start()` in that script runs (among many tunings: `rfkill unblock all`, ip_forward, sysctls, `lf_ifrename.pl`, CPU governor, optional `iw reg set` from /etc/default/lfregdom) then `/home/lanforge/lf_start`. Optional hooks if present: `/home/lanforge/lfcustom_ctrl.bash pre|post` (does NOT exist now), `/home/lanforge/nextboot/*`, `/home/lanforge/do_interactive_start` flag (not present).
3. `lf_start` (and `lf_stop`) are Perl, autogenerated by `lfconfig` ("You should NOT edit it by hand"). `lf_start` writes and forks `do_run_client_1`, `do_run_mgr_0`, `do_run_pesq_0..5`, each = `run_X | logchopper`. `run_mgr_0` and `run_client_1` are `while true` supervisors around `btserver` (manager: `--cli_port 4001 --gui_port 4002`; resource on 4003/4004). The btserver manager also spawns `runwebgui.bash` (`su -l lanforge -c "... python3 manage.py runserver 10.28.3.73:5454"`) and `runcelery.bash` (celery worker+beat) - verified by `ps` parentage (pid 4124 `./runwebgui.bash` child of btserver 4122; 4126 `./runcelery.bash` child of btserver 4125). `strings /home/lanforge/btserver` contains WEBGUI license-check messages ("The WEBGUI license key is in-correct"), so the WebUI launch appears to be license-gated.
4. The LANforge **Java GUI is not autostarted by systemd or rc.local**. It runs as user `lanforge` inside the VNC/MATE desktop (`lfgui.all` -> `lfclient.bash` -> java; parent is `caja`, i.e. launched from the desktop shortcut `LANforge-ALL.desktop`). `~lanforge/.config/autostart` has only `mate-screensaver.desktop` and `terminal.desktop`. The VNC session itself is `vncserver@:1.service`.
5. Other custom units in `/etc/systemd/system/`: `loadmon.service` (runs `/home/lanforge/scripts/loadmon.pl --interval 300`), `heatmon.service` + `heatmon.timer` (every 2 min), `create_https_cert.service`; drop-ins for httpd, nginx, php-fpm, redis, vncserver@:1. `/etc/rc.local` (hash in baseline) is LANforge-generated by `lf_kinstall.pl` and only fixes /etc/resolv.conf. Cron: no root or lanforge crontab; only `/etc/cron.d/0hourly`.
6. Enabled non-LANforge services that matter to us: `bluetooth.service` (enabled, running, `bluetoothd` as root), `dbus-broker`, `httpd`, `php-fpm`, `redis`, `rabbitmq-server`, `radiusd`, `sshd`, `xrdp`, `ModemManager`.

### Collision hazards (what LANforge restarts kill or reset)

`lf_stop`, the init `stop()` and the `run_client_1` loop use broad `pkill -f` patterns. **Never put these strings in our process command line, script names, or unit ExecStart path**: `btserver`, `btstack`, `run_pesq_`, `run_client_`, `run_mgr_`, `pesqserver`, `polqaserver`, `vr_conf/mgt_pipe_`, `local/bin/wpa_supplicant`, `local/bin/hostapd`, `vr_conf/dhclient_`, `xorp`, `XORPsh`, `gen_cfg.pl`, `virtual_router_setup`, `parse-iw-event.pl`, `parse-wpacli-event.pl`, `local/sbin/iw`, `celery.*beat`; also `pkill adb`, `pkill -9 gnuserver`, `killall runserver` (matches process NAME `runserver`; do not name a process `runserver`). Our project name `bluforge` / `ericsson-bluforge` does not match any of them.

Side effects of a LANforge (re)start that our service must tolerate: `rfkill unblock all`, WiFi driver and network device renames/reloads (`lf_ifrename.pl`, `pre_lanforge.pl`), hci devices may re-enumerate. Our service should re-discover `/org/bluez/hciN` periodically instead of caching at start.

LANforge upgrades (`lf_kinstall.pl`, sources in `/home/lanforge/LANforgeServer-*`) rewrite files in `/home/lanforge` (run_*, lf_start, lanforge.profile, serverctl.bash, rc.local). So we should NOT edit any of them.

### Proposed auto-start (design only; nothing was installed)

A separate, self-contained systemd unit that only ADDS one file, `/etc/systemd/system/ericsson-bluforge.service`, then `systemctl enable` it. Draft:

```ini
[Unit]
Description=Ericsson BluForge BLE demo web service
After=network-online.target bluetooth.service lanforge.service
Wants=network-online.target bluetooth.service
# Deliberately NO Requires=/PartOf=lanforge.service so LANforge restarts do not stop us
# and our failures never affect LANforge.

[Service]
Type=simple
User=root
WorkingDirectory=/home/lanforge/Desktop/EricssonBluForge
Environment=PYTHONUNBUFFERED=1
Environment=LD_LIBRARY_PATH=/home/lanforge/local/lib:/home/lanforge/local/lib64
ExecStart=/usr/bin/python3 /home/lanforge/Desktop/EricssonBluForge/src/server.py --port 5480
Restart=on-failure
RestartSec=3
# Log to journal, same idea as loadmon.service: journalctl -u ericsson-bluforge

[Install]
WantedBy=multi-user.target
```

Notes:
- Do not use `/etc/rc.local` (generated, may be overwritten) and do not hook `lfcustom_ctrl.bash`/`lf_start` (LANforge-owned, restart-coupled).
- Use the absolute `/usr/bin/python3`, never bare `python3` (venv PATH trap above).
- Do NOT use `ProtectHome=` / `ProtectSystem=strict` on our unit if the code lives under /home.
- The unit needs no network ordering on LANforge because the JSON API (8080) can come and go; the app should retry.
- For replication on another unit: copy the project dir + the unit file, `systemctl daemon-reload && systemctl enable --now ericsson-bluforge`. Target needs the same system RPMs: python3, python3-dbus, python3-gobject (and optionally python3-dasbus, python3-numpy, python3-scipy, python3-pyudev), bluez. All are already in LANforge's Fedora image.

### Which user: **root**

- LANforge's core (`btserver`, `wpa_supplicant`, `bluetoothd`) already run as root; only the Django WebUI/celery run as `lanforge`.
- Raw HCI (`AF_BLUETOOTH`/`BTPROTO_HCI` bind, HCI user channel, vendor/test-mode commands) needs `CAP_NET_RAW`/`CAP_NET_ADMIN`; exclusive control via `HCI_CHANNEL_USER` also needs the controller down and root. User `lanforge` is in group `root` (gid 0) and `wheel` among its groups (`uid=1000 ... groups=...,0(root),10(wheel),...`), but a supplementary group does not grant capabilities.
- BlueZ D-Bus policy (`/usr/share/dbus-1/system.d/bluetooth.conf`): `<policy user="root">` may own/send, and `<policy context="default"><allow send_destination="org.bluez"/>` lets any user call org.bluez methods, so a non-root user could use the D-Bus API (some methods may still be refused; not tested). Running as root removes that question.
- Hardening idea if desired: `User=root` with `CapabilityBoundingSet=CAP_NET_RAW CAP_NET_ADMIN CAP_NET_BIND_SERVICE` and `NoNewPrivileges=yes`, plus `ProtectKernelModules=yes`.
- `/dev/rfkill` is `crw-rw-r-- root:root` (world-readable, root-writable); `/dev/vhci` is `crw------- root`.

---

## 6. What we can use WITHOUT installing anything (summary)

| Need | Use | Verified present |
|---|---|---|
| Interpreter | `/usr/bin/python3` 3.12.7 (absolute path) | yes |
| HTTP server + static UI | stdlib `http.server.ThreadingHTTPServer` / `asyncio` | yes |
| Live push to browser | Server-Sent Events (stdlib) or hand-rolled WebSocket (stdlib) | yes (no `websockets` pkg) |
| LANforge API client | `urllib.request` or `requests` 2.28.2 on `http://localhost:8080` | yes, HTTP 200 now |
| BlueZ (scan, adv, power, props) | `dbus-python` 1.3.2 + `gi`/GLib 3.46.0, or `dasbus` 1.7; CLI `bluetoothctl`, `btmgmt`, `btmon`, `busctl` | yes |
| Raw HCI / LE test mode | stdlib `socket.AF_BLUETOOTH` (root) | yes, socket create OK |
| Math / spectrum | `numpy` 1.24.4, `scipy` 1.11.1 | yes |
| USB hotplug / host stats / config | `pyudev`, `psutil`, `PyYAML` | yes |
| HackRF | `/home/lanforge/local/bin/hackrf_*` (+ `LD_LIBRARY_PATH=/home/lanforge/local/lib`), LANforge RF-generator API, `gnuradio`+`osmosdr` python (needs that LD_LIBRARY_PATH) | yes (not exercised) |
| Front end | hand-written static HTML/CSS/JS, self-hosted fonts or system stack, no CDN, no build | n/a |
| Process manager | new systemd unit `ericsson-bluforge.service` | systemd 254 |
| Port | TCP 5480 (fallback 8850), bind 0.0.0.0, canonical URL http://10.28.3.73:5480/ | free, no firewall |

NOT available (would need install): flask, fastapi, uvicorn, aiohttp (system python), websockets, httpx, jinja2, bleak, bumble, scapy, dbus_fast, pydbus, pyserial (system python), hcitool/hciconfig/hcidump/gatttool, python SoapySDR binding. Each has a stdlib or installed-library workaround above; BLE advertising/scan in particular can be done through BlueZ D-Bus (`org.bluez.LEAdvertisingManager1`, `org.bluez.Adapter1.StartDiscovery`) or raw HCI without bleak.

## 7. Risks

1. API on :8080 exists only while the LANforge Java GUI runs; add a clear "LANforge API offline" state.
2. `lanforge.service` restart / `serverctl.bash restart` runs `rfkill unblock all` and may reset Wi-Fi/BT-related state; hci devices could re-enumerate. Do not run it during the demo, and keep our service resilient.
3. Process-name collisions with LANforge `pkill -f` patterns (list in section 5).
4. `python3` ambiguity: venv first in PATH for user `lanforge`; always `/usr/bin/python3`.
5. Both eth0 and eth1 are on 10.28.3.0/24; only eth0 has the subnet route. Use 10.28.3.73.
6. Legacy iptables `nat`/`raw` tables exist but are unreadable (no `iptables-legacy` binary); confirm reachability from a second host.
7. Internet is currently up but must not be relied on; Fedora 39 is past end-of-life (general knowledge; mirrors still answer today), so any later `dnf install` is a one-off and must be recorded against `rpm_baseline.txt`.
8. `bluetoothd` is sandboxed (`ProtectHome=true`); do not make it read files from /home.
