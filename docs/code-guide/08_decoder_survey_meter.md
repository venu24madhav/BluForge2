# 08 - The decoder, the survey aggregator and the throughput meter

Three small modules written for the survey / interrogate / exact-throughput work.
Files: `src/ble_decode.py`, `src/ble_survey.py`, `src/acl_meter.py`.

## How they fit together

```
radio --(HCI events)--> hci_raw.py ---------------> bluforge_cli.py sniff --survey
                         parses reports                |   for every advertisement:
                                                       |     ble_survey.Survey.add(report)
                                                       |         -> ble_decode.decode_adv(bytes)
                                                       v
                                     once a second: Survey.drain() -> JSON lines -> server -> browser

bluforge_classic.py interrogate  -> ble_decode.py   (names for GATT services/characteristics, SDP parsing)
bluforge_classic.py job_a2dp     -> acl_meter.py    (exact ACL byte counts from the HCI monitor channel)
```

---

## `ble_decode.py` - turning raw advertising bytes into meaning

**Idea.** A BLE device announces itself with a packet of up to 31 bytes (longer for Bluetooth 5).
The data is a chain of blocks, each `length, type, value`. Example from a real device on this bench:

```
02 01 06                         length 2: type 0x01 (Flags)             value 0x06
03 03 F0 FF                      length 3: type 0x03 (16-bit services)   value 0xFFF0 (little-endian bytes F0 FF)
10 09 4C32363031323846423130...  length 16: type 0x09 (Complete name)    value "L260128FB100159"
```

The module has two halves: lookup tables, and functions that use them.

### Tables (all are plain Python dictionaries)
| Name | What it maps |
|---|---|
| `AD_TYPES` | the type byte of a block -> its name ("Flags", "Complete Name", "Manufacturer Specific Data"...) |
| `SERVICES` | 16-bit service numbers -> names (0x180F "Battery", 0x180D "Heart Rate", 0x1812 "HID over GATT"...), plus a few vendor-assigned ones seen in the field (0xFEAA Eddystone, 0xFD6F Exposure Notification, 0xFE2C Google Fast Pair...) |
| `LE_AUDIO` | the set of service numbers that mean "LE Audio" |
| `BASE_UUID_TAIL` | the fixed tail `-0000-1000-8000-00805f9b34fb` that every 16-bit Bluetooth UUID shares; used to turn a 128-bit UUID back into its short form |
| `COMPANIES` | 16-bit company IDs -> company ("0x004C" Apple, "0x0006" Microsoft, "0x0075" Samsung...). Only entries that were certain are included, so an unknown company is shown as a number, not guessed. |
| `APPEARANCE` | the "appearance" category (Phone, Watch, Keyboard...) |
| `APPLE_TYPES` | sub-formats inside Apple's manufacturer data (iBeacon, AirDrop, Find My...) |
| `PHY_NAMES` | PHY number -> "1M", "2M", "Coded" (the three BLE radio speeds/ranges) |
| `CHARACTERISTICS`, `DESCRIPTORS` | names for GATT characteristics (0x2A19 "Battery Level"...) and descriptors, used by Interrogate |
| `CLASSIC_PROFILES` | Classic profile numbers -> names (0x111E "Handsfree", 0x110B "A2DP Sink", 0x1101 "Serial Port") |

### Functions
- **`uuid_str(raw)`** - takes the little-endian bytes of a UUID (2, 4 or 16 bytes) and returns `("0x180F", 0x180F)` for short ones, or the standard 8-4-4-4-12 text for 128-bit ones.
- **`service_entry(raw)`** - wraps `uuid_str` and adds the human name from `SERVICES`. Returns `{"uuid": "0x180F", "name": "Battery"}`.
- **`addr_kind(addr, addr_type)`** - classifies an address. A *public* address is the real, registered one. A *random* address is classified by the top two bits of its first byte: `11` static random, `01` resolvable private (RPA - changes every few minutes for privacy), `00` non-resolvable private.
- **`parse_ad(data)`** - walks the length-type-value chain and returns a list of `(type, value_bytes)`. It stops cleanly on a zero length or a truncated block, so a damaged packet cannot crash the sniffer.
- **`decode_flags(v)`** - turns the flags byte into words ("LE General Discoverable", "BR/EDR not supported" which means the device is LE-only, etc.).
- **`decode_eddystone(v)`** - decodes Google's Eddystone beacon frames: UID (namespace + instance), URL (compressed URL scheme/suffix tables), TLM (battery mV, temperature, uptime), EID.
- **`decode_mfg(v)`** - manufacturer data: the first two bytes are the company ID, the rest is vendor-specific. For Apple it checks the type byte and decodes an iBeacon fully (UUID, major, minor, calibrated power); for Microsoft it names CDP / Swift Pair.
- **`decode_adv(data)`** - the main function. Calls `parse_ad`, then for each block type fills one dictionary: name (and whether it is complete or shortened), flags, Tx power, appearance, list of services (16/32/128-bit, de-duplicated), solicited services, service data (with Eddystone decoded), manufacturer data, URI, and any unknown blocks under `other`.
- **`classify(ad)`** - picks one short label for what the device is, using the strongest evidence in order: beacon type, an Apple/vendor format, appearance, first named service.
- **`supports(dev)`** - builds the plain-language "what it advertises it supports" list from an aggregated device record: connectable or broadcast-only, extended advertising (Bluetooth 5), Coded/2M PHY, LE-only vs dual-mode, LE Audio, Mesh, HID, Exposure Notification, Fast Pair, each named service, beacon, vendor formats, appearance. Duplicates are removed, order kept.

### Interrogation helpers (added later in the same file)
- **`_printable(b)`** - true if every byte is a printable ASCII character.
- **`decode_char_value(short, b)`** - understands a few characteristics: strings (name, model, serial, firmware, hardware, software, manufacturer), battery %, appearance, Tx power, preferred connection parameters, PnP ID (vendor id/product/version), HID info, privacy flag. Returns `None` if it does not know the type.
- **`value_text(short, b)`** - returns `(hex_string, decoded_text_or_None)`; falls back to ASCII when the bytes are printable.
- **`parse_sdp_records(text)`** - parses the text output of `sdptool records ADDR` into records with name, handle, service classes, RFCOMM channel, L2CAP PSM and profile versions. It splits on blank lines, then reads indented lines under each heading ("Service Class ID List:", "Protocol Descriptor List:", "Profile Descriptor List:").

**Honest limit:** only what a device *advertises* is decoded here. A device can support more services than it puts in an advertisement.

---

## `ble_survey.py` - one record per device

**Idea.** An advertiser can be heard 10+ times a second. Sending each packet to the browser would flood it, so this module keeps one dictionary per device and the helper reports only the changed ones, once a second.

- **`LEGACY_PDU`, `EXT_LEGACY_PDU`** - names for the packet types: ADV_IND (connectable), ADV_SCAN_IND, ADV_NONCONN_IND (beacon-style), SCAN_RSP (the reply to an active scan)...
- **`MAX_DEVICES = 5000`** - memory cap; beyond it the device not heard for longest is dropped (counted in `evicted`).
- **`normalise(rep)`** - takes a report from either the legacy or the extended scan path and returns one common tuple: PDU name, connectable?, scannable?, directed?, is-scan-response?, address type, PHYs, advertising-set id, extended?. The extended report event-type has bit flags (connectable 0x01, scannable 0x02, directed 0x04, scan-response 0x08, legacy 0x10).
- **`Survey` class**
  - `__init__(name_filter)` - empty table, counters, optional filter text.
  - `add(rep)` - the core. Creates the device record on first sight (with all fields initialised), then updates: count, last-seen, RSSI last/min/max and a running sum (for the average), PDU counts, connectable/scannable/directed (sticky - once seen true stays true), PHYs, Tx power, the latest raw bytes per PDU type, and merges the decoded data via `_merge`.
  - `_merge(d, ad)` - combines a new decode into the record: prefers a complete name over a shortened one, keeps flags/Tx power/appearance/URI/beacon, unions services, replaces service data per UUID, replaces manufacturer data per company+format. This is how the scan response (a second packet carrying extra data) joins the advertisement.
  - `matches(d)` - the capture filter (address or name contains text).
  - `record(d)` - the JSON-able copy sent out: adds `"m":"dev"` **as the first key** (the server recognises survey lines by that prefix), average RSSI, observed rate, `kind`, `company`, the `supports` list, rounded timestamps.
  - `drain()` - records for devices changed since the last call, then clears the changed-set.
  - `stats()` - the once-a-second `{"m":"stats"...}` line: elapsed time, device count, reports, reports/second, evictions, seconds since the last packet (`idle_s`).

---

## `acl_meter.py` - exact throughput

**Idea.** The kernel can mirror every packet to and from every Bluetooth controller on a special socket, the **HCI monitor channel** (`HCI_CHANNEL_MONITOR = 2`). Counting those packets gives byte-exact rates; no polling, no sampling error.

A monitor frame is: 6-byte header (`opcode u16, controller index u16, length u16`) + the packet. Opcodes used: 3 event, 4 ACL transmit, 5 ACL receive, 6/7 SCO (voice) transmit/receive.

- **`AclMeter(hci)`** - a background thread.
  - `open()` - creates the socket and binds it to the monitor channel (using `hci_raw._hci_bind`, because Python's `bind()` cannot select the channel).
  - `feed(opcode, payload)` - accounts one frame (kept separate from the socket reading so it can be unit-tested with fake bytes):
    - ACL TX: handle (low 12 bits of the first 16-bit field) and data length; adds to `tx_b`/`tx_p`; appends the length to a per-handle FIFO queue.
    - ACL RX: adds to `rx_b`/`rx_p`.
    - Event **Number Of Completed Packets (0x13)**: lists handles then counts; for each count it pops that many lengths from the handle's FIFO and adds them to `done_b`. If the FIFO is empty (packets sent before the meter started) it counts `done_unmatched` instead.
    - Event Disconnection Complete (0x05): clears that handle's queue/address.
    - Event Connection Complete (0x03) and LE Connection Complete (0x3E sub-events 0x01/0x0A/0x29): remember handle -> device address.
  - `run()` - the thread loop: receive a frame, ignore other controllers' index, call `feed`.
  - `handles_for(addr)` - connection handles currently mapped to a device address.
  - `snapshot()` / `snapshot_all()` - copy the cumulative counters (all handles, or per handle) plus a monotonic timestamp, under a lock.
  - `close()` - stop and close.
- **`totals(snap, handles)`** - sum counters over the chosen handles.
- **`rates_for(prev, cur, handles)`** - the exact kbit/s between two snapshots: `(bytes delta * 8) / elapsed seconds / 1000`, for TX (sent to controller), "done" (controller-completed) and RX. Counters are cumulative per handle, so a reconnect (new handle) cannot produce a negative rate.
- **`rates(prev, cur)`** - the same for the all-handles snapshot form.

**What it measures:** ACL-level throughput (L2CAP header + payload as the host sees it). It cannot see over-the-air retransmissions or baseband overhead.

**Verified:** on a live LE connection the meter counted 14 TX and 14 RX packets, matching the kernel's own counters exactly; a 30 s A2DP run gave 523,066 bytes = 137.1 kbit/s.
