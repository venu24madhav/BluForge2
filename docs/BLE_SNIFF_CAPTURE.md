# BLE Sniff: packet capture for Wireshark

The BLE Sniff tab shows one row per device. It can also **record every advertising report the radio hears to a `.pcap` file**
and offer it for download, so the traffic can be opened in Wireshark.

## Using it
1. BLE Sniff tab, pick a radio, leave **record packet capture (.pcap for Wireshark)** ticked, **Start Survey**.
2. The stats line shows the capture growing (`capture 4757 packets / 294.1 KB`).
3. **Download capture (.pcap)** (next to Export CSV) downloads the newest capture of the selected radio.
   The **Saved packet captures** table lists every capture on the unit, with a Download button each.
   A capture that is still recording can be downloaded; it holds everything up to that moment.
4. Open the file in Wireshark.

Useful Wireshark display filters:

| Filter | Shows |
|---|---|
| `bthci_evt.le_meta_subevent == 0x0d \|\| bthci_evt.le_meta_subevent == 0x02` | only advertising reports (extended and legacy) |
| `bthci_evt.bd_addr == aa:bb:cc:dd:ee:ff` | one advertiser |
| `btcommon.eir_ad.entry.device_name contains "Tag"` | by advertised name |
| `bthci_cmd` | the scan commands the radio was given (active or passive, interval, window) |

## What is in the file, and what is not
* The file is **everything the controller reported to the host (Bluetooth HCI)**: each LE Advertising Report (legacy or extended)
  with address, address type, RSSI and the advertising data, each with its arrival time. The scan commands are recorded too.
* It is **not the raw radio signal.** The controller does not pass the channel number, the CRC or the link-layer framing to the
  host, so Wireshark cannot show them. A true over-the-air capture needs a separate sniffer (for example an nRF52840 dongle with
  Nordic's nRF Sniffer for Bluetooth LE, or an Ubertooth One).
* The capture is **unfiltered**. The "Capture filter" box only narrows the table; the file keeps every packet, because one
  report can carry several advertisers.
* One radio listens on one advertising channel at a time. Every advertising event is sent on all three channels, so each event is
  normally heard once, but an occasional event is missed. Passive scanning (untick "active scan") sends nothing; active scanning
  also records the SCAN_RSP replies.

## Format
Classic pcap, link type 201 (`LINKTYPE_BLUETOOTH_HCI_H4_WITH_PHDR`): a 4-byte direction (0 sent to the controller, 1 received),
then the H4 packet. Microsecond timestamps from the unit's clock. `tshark`, `capinfos`, `editcap`, Scapy and `btmon -r` style tools
read it. Files are created exclusively (never overwritten) and every record is one `write()`, so a file being written is always
readable up to its last whole record.

## Where the files are
`logs/captures/YYYYMMDD-HHMMSS_blesniff_hciN.pcap` under the install folder (or `$BLUFORGE_LOGS/captures`). They are **never deleted
automatically**; delete them from that folder. Recording stops at 512 MB per capture (`--pcap-max-mb`, 0 = no limit); scanning
and the table continue, and the console and stats line say so.

A capture contains the advertisements of **every device in range**, so treat it like any other capture of other people's traffic.

## API and command line
| | |
|---|---|
| `POST /api/start` `{"tool":"sniff","dev":0,"params":{"pcap":true,"pcap_max_mb":512}}` | starts a survey that records (recording is off unless `pcap` is true) |
| `GET /api/sniff/captures` | `{"captures":[{"file","hci","size","mtime","running"}]}`, newest first |
| `GET /api/sniff/capture/<file>.pcap` | the download (`application/vnd.tcpdump.pcap`, `attachment`) |
| `bluforge_cli.py --dev N sniff --survey --json --pcap FILE [--pcap-max-mb N]` | the same from the command line |

Only `*.pcap` files in the capture folder can be fetched; any other name is a 404.
On a clustered manager, resource captures are listed as `<base>__<file>` and fetched through the manager, like Classic result files.

## Code
`src/pcap_writer.py` (the writer), `HciSocket.tap` in `src/hci_raw.py` (every HCI packet sent or received on the session),
`cmd_sniff` in `src/bluforge_cli.py` (`--pcap`), `sniff_captures` / `_send_capture` in `src/server.py`, `merge_captures` /
`fetch_capture` in `src/cluster.py`, and the sniff section of `static/app.js`. Tests: `tests/test_pcap.py`.
