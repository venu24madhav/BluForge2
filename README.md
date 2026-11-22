# BluForge

A web-based Bluetooth test tool for LANforge units with Intel BE200 radios. One page drives every Bluetooth radio of a unit, and, on a
LANforge manager, the radios of the clustered resources too. Version 1.3.0. Python 3 standard library only (no pip packages).

## What it does
| Area | What you can do |
|---|---|
| **Interference** | Generate 2.4 GHz interference for Wi-Fi tests: BLE Direct Test Mode (single channel, sweep, hop, Wi-Fi-channel shaped, full band), advertising floods, and realistic Bluetooth Classic traffic (A2DP audio, file transfer, video, keyboard/mouse). |
| **Advertise** | Emit spec-correct BLE advertisements: the four legacy PDU types and five beacon profiles (iBeacon, Eddystone UID, Eddystone URL, AltBeacon, a generic sensor). |
| **BLE Sniff** | Survey every device advertising nearby, and record everything the radio hears to a `.pcap` that opens in Wireshark. |
| **Bluetooth Classic** | Test a device under test (earbuds, speaker, car kit): pair and connect, A2DP stream test, AVRCP, HFP call, connect/disconnect cycling. The A2DP test reports exact throughput, audio underruns, and **link delay and jitter** measured from kernel timestamps. |
| **Cluster** | On a LANforge manager, list and control the radios of every resource that runs BluForge (`1.2.hci0`, ...). |

It talks to the controllers directly over raw HCI (one process per radio per job) and gives every radio back to BlueZ when a job ends.

## Repository layout
| Path | Contents |
|---|---|
| `src/` | The service: `server.py` (web server and job control), `bluforge_cli.py` (BLE jobs), `bluforge_classic.py` (Classic tests), `hci_raw.py` (radio access), `acl_meter.py` and `delay_stats.py` (throughput, delay, jitter), `cluster.py`, `pcap_writer.py`, `static/` (the web page) |
| `tests/` | 352 automated tests that need no hardware (`./run_tests.sh`), plus hardware checks (`hw_*.py`) and two real capture fixtures in `tests/data/` |
| `docs/` | Guides and reports (see below) |
| `deploy/` | Installer, systemd unit and the offline packager |
| `desktop/` | Desktop launcher |
| `ble-lab/` | The earlier command-line lab scripts this grew out of |
| `recon/` | Research notes and reference material gathered before the build |

## Run it
```bash
# development: from this folder, as root (raw HCI needs it); the page is on http://localhost:5480/
sudo python3 src/server.py

# the hardware-free test suite (about 90 s)
cd tests && ./run_tests.sh

# install as a service on a unit: build the offline package, copy it over, install
cd deploy && ./package-offline.sh /tmp/bluforge.tar.gz
# on the target: tar xzf bluforge.tar.gz && cd ericsson-bluforge && sudo ./install.sh
```

## Where to start reading
1. [`docs/QA_REPORT.md`](docs/QA_REPORT.md): what was verified on hardware, the defects found and fixed, and the open findings. Start here.
2. [`docs/A2DP_DELAY.md`](docs/A2DP_DELAY.md): how link delay and jitter are measured, and what they are not.
3. [`docs/DEPLOYMENT_GUIDE.md`](docs/DEPLOYMENT_GUIDE.md) and [`docs/CLUSTER.md`](docs/CLUSTER.md): installing, and running a manager with resources.
4. [`docs/DATA_INTEGRITY.md`](docs/DATA_INTEGRITY.md) and [`docs/ACCESS_CONTROL.md`](docs/ACCESS_CONTROL.md): what the data guarantees, and the access model.
5. [`docs/REALISTIC_INTERFERENCE.md`](docs/REALISTIC_INTERFERENCE.md), [`docs/BLE_SNIFF_CAPTURE.md`](docs/BLE_SNIFF_CAPTURE.md), [`docs/BLUEZ_INSTALL.md`](docs/BLUEZ_INSTALL.md).
6. [`docs/INSTALL_LOG.md`](docs/INSTALL_LOG.md): the dated log of every change, and [`docs/code-guide/`](docs/code-guide/): a file-by-file walkthrough of the code.

## Status and limits
* Verified on a LANforge manager with eight BE200 radios and one resource with two. No real earbud, phone or other third-party device has been available yet, so the profile tests (A2DP, AVRCP, HFP) are not yet validated against real devices; `docs/QA_REPORT.md` lists this and the other open items.
* **There is no login and the page is plain HTTP**: use it on an isolated lab network (`docs/ACCESS_CONTROL.md`).
* Classic audio tests need PipeWire; a unit with real PulseAudio runs the interference stream only (details in `docs/QA_REPORT.md`).
* Link delay is the time an audio packet needs to cross the link as the host sees it. It is not the audio latency a listener hears.
