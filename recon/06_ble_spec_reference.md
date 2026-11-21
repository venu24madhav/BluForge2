# 06 - BLE Spec Reference and Channel Math (Core 5.4)

Unit `ct523c-ccb8` (10.28.3.73), 8x Intel BE200 (hci0-hci7, BlueZ 5.77). Written for the Ericsson BLE demo
(BLE interference per channel + BLE advertisement emulation). Companion module: `recon/ble_constants.py`
(`python3 ble_constants.py` runs the self-test; the tables below marked *generated* are produced by that module and
the self-test fails if they drift).

**How this was verified (no radio was used, nothing on the system was changed).**
(1) Core 5.4 knowledge of the author. (2) Linux kernel header `/usr/src/kernels/6.11.9-100.fc39.x86_64/include/net/bluetooth/hci.h`
(26 opcodes + AD/event size constants compared by the self-test). (3) BlueZ 5.77 `btmon -r` used as an *offline decoder*:
every command/event built by the module is written to a synthetic btsnoop file under `recon/_xcheck/` and decoded; OGF|OCF and
parameter length must match (37/37 packets, covering all 36 registry opcodes, did) and BlueZ's field text was inspected. (4) 500+ Python assertions and golden byte vectors.
Anything not covered by (2)-(4) is marked with a confidence level in section 7.

Local source facts: `/usr/include/bluetooth/` does **not** exist (no libbluetooth-devel), `hcitool`/`hciconfig` are not installed,
there are no BlueZ `.rst/.txt/.md` docs on disk (only `/usr/share/doc/bluez/{AUTHORS,ChangeLog}` and man pages), `btmon`/`btmgmt`/`bluetoothctl`
5.77 exist. `timeout 10 btmgmt help` printed nothing and hit the timeout (it waits on the mgmt socket); `btmgmt --help` works. The kernel
header has **no** `HCI_OP_LE_*TEST*` opcodes, i.e. LE Direct Test Mode is only reachable through raw HCI.

---

## 0. The ten facts that bite (read first)

1. **HCI `TX_Channel` / `RX_Channel` = RF channel number k = (F - 2402) / 2, k = 0..39 (0x00..0x27). It is NOT the Link Layer channel index.**
   To sit on advertising channels 37/38/39 (2402/2426/2480 MHz) send **0, 12, 39**. Sending 37/38 would transmit at **2476/2478 MHz**.
   Confidence: very high (spec text; BlueZ's btmon prints `TX frequency: 2440 MHz (0x13)` = 2402 + 2*19, see section 6).
2. The two numberings coincide for nothing: data LL index 0-10 -> RF 1-11, LL 11-36 -> RF 13-38, advertising LL 37/38/39 -> RF 0/12/39.
3. HCI `Advertising_Type` values (0x00 ADV_IND, 0x01 ADV_DIRECT_IND high duty, 0x02 ADV_SCAN_IND, 0x03 ADV_NONCONN_IND, 0x04 ADV_DIRECT_IND low duty)
   are **not** the on-air PDU_Type nibbles (ADV_NONCONN_IND is on-air 0x2 but HCI 0x3; ADV_SCAN_IND is on-air 0x6 but HCI 0x2). In the LE Advertising Report,
   `Event_Type` 0x04 means SCAN_RSP, whereas in the *command* 0x04 means low-duty directed advertising.
4. Legacy advertising/scan-response payload is **31 octets max**. Longer data requires extended advertising (251 octets per HCI command fragment, 1650 per set).
5. All HCI multi-octet fields are little-endian, BD_ADDR is sent LSB first (reverse of the printed `C0:11:22:33:44:55`).
   iBeacon UUID/major/minor and Eddystone TLM fields are *big*-endian inside the AD payload; 128-bit service UUIDs in AD lists are little-endian (reversed).
6. Advertising interval unit is 0.625 ms; legal range 20 ms..10.24 s (0x0020..0x4000) for Core >= 5.0; Core <= 4.2 controllers required >= 100 ms (0x00A0) for ADV_SCAN_IND / ADV_NONCONN_IND.
7. **Set Event Mask default (0x00001FFFFFFFFFFF) does not enable LE Meta events**; bit 61 must be set (BlueZ kernel does `events[7] |= 0x20`) plus `LE Set Event Mask`
   bit 1 (Advertising Report, subevent 0x02) or bit 12 (Extended Advertising Report, subevent 0x0D). Subevent code = LE-mask bit index + 1.
8. LE Transmitter/Receiver Test only runs from the idle state: stop advertising, scanning, initiating and connections first, and always end with `LE Test End` (0x201F).
9. The packet-interval formula in the original task text, `ceil(((L+14+3)*8 + 249)/625)*625`, is **not** the DTM formula. The spec formula is
   `I(L) = ceil((L_us + 249)/625) * 625 us` with `L_us` the on-air packet time (1M: `(Test_Data_Length + 10) * 8` us). It gives the well-known 625 us interval for <= 37 octets
   (376 + 249 = 625); the task's expression would give 1250 us for 37 octets. See section 4.
10. BlueZ 5.77 `btmon` has no decoder for LE Transmitter Test v4 (0x207B) and mislabels v3 (0x2050); this is a btmon limitation, not a protocol difference.

---

## 1. Channel math

### 1.1 Two numberings

| Numbering | Range | Meaning | Where used |
|:--|:--|:--|:--|
| **RF channel k** (physical channel) | 0..39 | centre frequency `F = 2402 + 2k` MHz | HCI `LE Transmitter Test` / `LE Receiver Test` `TX_Channel`/`RX_Channel` (`k = (F-2402)/2`, range 0x00..0x27) |
| **LL channel index** | 0..39 | 0-36 data, 37-39 advertising | Link Layer (CSA#1/#2 hopping, `Advertising_Channel_Map` bits, channel map for connections) |

### 1.2 Mapping (exact)

* Advertising: LL 37 -> RF 0 (2402 MHz); LL 38 -> RF 12 (2426 MHz); LL 39 -> RF 39 (2480 MHz).
* Data: LL 0..10 -> RF 1..11, `F = 2404 + 2*LL`; LL 11..36 -> RF 13..38, `F = 2428 + 2*(LL-11)`.
* RF 12 (2426) and RF 39 (2480) are not data channels (they are advertising channels 38 and 39).

```python
from ble_constants import *
ll_index_to_rf_channel(37)   # 0      freq_mhz(0)  = 2402
ll_index_to_rf_channel(38)   # 12     freq_mhz(12) = 2426
ll_index_to_rf_channel(39)   # 39     freq_mhz(39) = 2480
ll_index_to_rf_channel(10)   # 11     ll_index_to_rf_channel(11) = 13    ll_index_to_rf_channel(36) = 38
rf_channel_to_ll_index(rf), freq_mhz(rf), rf_channel_from_freq_mhz(mhz)
hci_test_channel_from_ll_index(ll)  # value to put in TX_Channel/RX_Channel (== ll_index_to_rf_channel)
```

`Advertising_Channel_Map` (LE Set Advertising Parameters, and Primary channel map of extended advertising): bit0 = LL 37 (RF 0, 2402), bit1 = LL 38 (RF 12, 2426), bit2 = LL 39 (RF 39, 2480); `0x07` default, `0x00` invalid, bits 3-7 reserved.

### 1.3 Which numbering do the HCI test commands use?

The spec text for LE Transmitter Test (all versions) and LE Receiver Test (all versions) is: *"TX_Channel / RX_Channel: N = (F - 2402) / 2, Range: 0x00 to 0x27, Frequency Range: 2402 MHz to 2480 MHz"*.
So the parameter is the **RF channel number k**, never the LL index. Confidence: **very high (>= 97 %)**; independent corroboration on this machine: BlueZ's decoder prints
`TX frequency: 2440 MHz (0x13)` for parameter 0x13 and `RX frequency: 2480 MHz (0x27)` for 0x27 (BlueZ implements the same formula), and `TX channel frequency: 2402 MHz (0x00)` for the v2 command.
Values above 0x27 are invalid (controller returns 0x12 Invalid HCI Command Parameters; the module raises `ValueError`).
Practical consequence: to place a test transmitter on the three advertising channels use `TX_Channel = 0, 12, 39`.
Not verifiable without radio: whether the BE200 firmware honours exactly this mapping (it should; check with a second controller in LE Receiver Test or the HackRF as a spectrum observer in a later task).

### 1.4 All 40 channels (*generated*)

<!-- BEGIN:channels -->
| RF ch k (= HCI TX/RX_Channel) | LL index | MHz | Role | Wi-Fi 20 MHz ch (1-13) whose +/-10 MHz span contains it |
|---:|---:|---:|:--|:--|
| 0 (0x00) | 37 | 2402 | **adv** | 1 |
| 1 (0x01) | 0 | 2404 | data | 1 |
| 2 (0x02) | 1 | 2406 | data | 1 |
| 3 (0x03) | 2 | 2408 | data | 1-2 |
| 4 (0x04) | 3 | 2410 | data | 1-2 |
| 5 (0x05) | 4 | 2412 | data | 1-3 |
| 6 (0x06) | 5 | 2414 | data | 1-3 |
| 7 (0x07) | 6 | 2416 | data | 1-3 |
| 8 (0x08) | 7 | 2418 | data | 1-4 |
| 9 (0x09) | 8 | 2420 | data | 1-4 |
| 10 (0x0A) | 9 | 2422 | data | 1-5 |
| 11 (0x0B) | 10 | 2424 | data | 2-5 |
| 12 (0x0C) | 38 | 2426 | **adv** | 2-5 |
| 13 (0x0D) | 11 | 2428 | data | 3-6 |
| 14 (0x0E) | 12 | 2430 | data | 3-6 |
| 15 (0x0F) | 13 | 2432 | data | 3-7 |
| 16 (0x10) | 14 | 2434 | data | 4-7 |
| 17 (0x11) | 15 | 2436 | data | 4-7 |
| 18 (0x12) | 16 | 2438 | data | 5-8 |
| 19 (0x13) | 17 | 2440 | data | 5-8 |
| 20 (0x14) | 18 | 2442 | data | 5-9 |
| 21 (0x15) | 19 | 2444 | data | 6-9 |
| 22 (0x16) | 20 | 2446 | data | 6-9 |
| 23 (0x17) | 21 | 2448 | data | 7-10 |
| 24 (0x18) | 22 | 2450 | data | 7-10 |
| 25 (0x19) | 23 | 2452 | data | 7-11 |
| 26 (0x1A) | 24 | 2454 | data | 8-11 |
| 27 (0x1B) | 25 | 2456 | data | 8-11 |
| 28 (0x1C) | 26 | 2458 | data | 9-12 |
| 29 (0x1D) | 27 | 2460 | data | 9-12 |
| 30 (0x1E) | 28 | 2462 | data | 9-13 |
| 31 (0x1F) | 29 | 2464 | data | 10-13 |
| 32 (0x20) | 30 | 2466 | data | 10-13 |
| 33 (0x21) | 31 | 2468 | data | 11-13 |
| 34 (0x22) | 32 | 2470 | data | 11-13 |
| 35 (0x23) | 33 | 2472 | data | 11-13 |
| 36 (0x24) | 34 | 2474 | data | 12-13 |
| 37 (0x25) | 35 | 2476 | data | 12-13 |
| 38 (0x26) | 36 | 2478 | data | 13 |
| 39 (0x27) | 39 | 2480 | **adv** | 13 |
<!-- END:channels -->

### 1.5 Overlap with Wi-Fi 2.4 GHz 20 MHz channels (*generated*)

Rule used (as requested): a Wi-Fi 20 MHz channel centred at `fc` overlaps every BLE RF channel whose centre lies within `fc +/- 10 MHz` (inclusive). BLE channels are 2 MHz wide slots
(centre spacing 2 MHz); the LE 1M signal occupies ~1 MHz (20 dB bandwidth) inside its slot, LE 2M fills ~2 MHz. Wi-Fi centres are `2407 + 5*ch` (ch 1-13; ch 14 = 2484).
Odd Wi-Fi channels (1,3,...,11) have an even-MHz centre, so both edges (`fc +/- 10`) land on a BLE centre: 11 BLE channels; even channels: 10 BLE channels; ch 13 is clipped at 2480 MHz.
Example: **Wi-Fi ch 1 (2412 MHz) overlaps BLE RF channels 0-10** (2402-2422 MHz) - which includes advertising channel 37 (RF 0) exactly at the lower edge (2402 = fc - 10).

<!-- BEGIN:wifi -->
| Wi-Fi ch | fc MHz | +/-10 MHz span | BLE RF ch k | #BLE ch | adv channels hit |
|---:|---:|:--|:--|---:|:--|
| 1 | 2412 | 2402-2422 | 0-10 | 11 | 37 |
| 2 | 2417 | 2407-2427 | 3-12 | 10 | 38 |
| 3 | 2422 | 2412-2432 | 5-15 | 11 | 38 |
| 4 | 2427 | 2417-2437 | 8-17 | 10 | 38 |
| 5 | 2432 | 2422-2442 | 10-20 | 11 | 38 |
| 6 | 2437 | 2427-2447 | 13-22 | 10 | none |
| 7 | 2442 | 2432-2452 | 15-25 | 11 | none |
| 8 | 2447 | 2437-2457 | 18-27 | 10 | none |
| 9 | 2452 | 2442-2462 | 20-30 | 11 | none |
| 10 | 2457 | 2447-2467 | 23-32 | 10 | none |
| 11 | 2462 | 2452-2472 | 25-35 | 11 | none |
| 12 | 2467 | 2457-2477 | 28-37 | 10 | none |
| 13 | 2472 | 2462-2482 | 30-39 | 10 | 39 |
<!-- END:wifi -->

Consequences: with Wi-Fi on 1/6/11 the only advertising channel directly inside a 20 MHz span is LL 37 (edge of ch 1); LL 38 (2426) sits 11 MHz from ch 6 and 14 MHz from ch 1, i.e. in the gap;
LL 39 (2480) is 18 MHz from ch 11. Wi-Fi ch 2-5 cover LL 38, Wi-Fi ch 13 covers LL 39. (Wi-Fi ch 14, 2484 MHz, 11b only, would cover RF 36-39.)

---

## 2. HCI reference

### 2.1 Wire format (H4 over a raw HCI socket / what `btmon` shows)

```
Command : 0x01 | opcode (u16 LE) | param_len (u8) | params          opcode = (OGF << 10) | OCF ;  OGF 0x08 = LE Controller
Event   : 0x04 | event_code (u8) | param_len (u8) | params
Command Complete (0x0E): Num_HCI_Command_Packets(1) | Command_Opcode(2, LE) | Return_Parameters...      (Return_Parameters[0] is Status for every command below)
Command Status   (0x0F): Status(1) | Num_HCI_Command_Packets(1) | Command_Opcode(2, LE)
LE Meta Event    (0x3E): Subevent_Code(1) | subevent parameters
```
Over a Linux `AF_BLUETOOTH/BTPROTO_HCI` socket the leading 0x01/0x04 type octet is part of each packet written/read. Python 3.12 here exposes `socket.AF_BLUETOOTH`, `BTPROTO_HCI=1`, `SOL_HCI=0`, `HCI_FILTER=2`
but **not** `HCI_CHANNEL_*`; kernel values are RAW=0, USER=1, MONITOR=2, CONTROL=3, LOGGING=4 (verified in `hci_sock.h`).
Spec status codes used below (btmon-confirmed names): 0x00 Success, 0x01 Unknown HCI Command, 0x07 Memory Capacity Exceeded, 0x0C Command Disallowed, 0x11 Unsupported Feature or Parameter Value,
0x12 Invalid HCI Command Parameters, 0x3A Controller Busy, 0x3C Advertising Timeout, 0x42 Unknown Advertising Identifier, 0x43 Limit Reached, 0x45 Packet Too Long.

### 2.2 Opcode table (*generated; each row asserted `opcode == (ogf<<10)|ocf`, against hand-typed literals, the kernel header where it defines the macro, and btmon*)

<!-- BEGIN:opcodes -->
| Command | Opcode | OGF | OCF | Parameters (octets) | Return parameters |
|:--|:--|:--|:--|:--|:--|
| Set Event Mask | `0x0C01` | `0x03` | `0x0001` | Event_Mask(8) | Status(1) |
| Reset | `0x0C03` | `0x03` | `0x0003` | - | Status(1) |
| Read Local Supported Commands | `0x1002` | `0x04` | `0x0002` | - | Status(1), Supported_Commands(64) |
| LE Set Event Mask | `0x2001` | `0x08` | `0x0001` | LE_Event_Mask(8) | Status(1) |
| LE Read Local Supported Features | `0x2003` | `0x08` | `0x0003` | - | Status(1), LE_Features(8) |
| LE Set Random Address | `0x2005` | `0x08` | `0x0005` | Random_Address(6) | Status(1) |
| LE Set Advertising Parameters | `0x2006` | `0x08` | `0x0006` | Interval_Min(2) Interval_Max(2) Adv_Type(1) Own_Addr_Type(1) Peer_Addr_Type(1) Peer_Addr(6) Chan_Map(1) Filter_Policy(1) = 15 | Status(1) |
| LE Set Advertising Data | `0x2008` | `0x08` | `0x0008` | Data_Length(1) Data(31, zero padded) = 32 | Status(1) |
| LE Set Scan Response Data | `0x2009` | `0x08` | `0x0009` | Data_Length(1) Data(31, zero padded) = 32 | Status(1) |
| LE Set Advertising Enable | `0x200A` | `0x08` | `0x000A` | Advertising_Enable(1) | Status(1) |
| LE Set Scan Parameters | `0x200B` | `0x08` | `0x000B` | Scan_Type(1) Interval(2) Window(2) Own_Addr_Type(1) Filter_Policy(1) = 7 | Status(1) |
| LE Set Scan Enable | `0x200C` | `0x08` | `0x000C` | Scan_Enable(1) Filter_Duplicates(1) = 2 | Status(1) |
| LE Read Supported States | `0x201C` | `0x08` | `0x001C` | - | Status(1), LE_States(8) |
| LE Receiver Test [v1] | `0x201D` | `0x08` | `0x001D` | RX_Channel(1) | Status(1) |
| LE Transmitter Test [v1] | `0x201E` | `0x08` | `0x001E` | TX_Channel(1) Test_Data_Length(1) Packet_Payload(1) = 3 | Status(1) |
| LE Test End | `0x201F` | `0x08` | `0x001F` | - | Status(1), Num_Packets(2) |
| LE Receiver Test [v2] | `0x2033` | `0x08` | `0x0033` | RX_Channel(1) PHY(1) Modulation_Index(1) = 3 | Status(1) |
| LE Transmitter Test [v2] | `0x2034` | `0x08` | `0x0034` | TX_Channel(1) Test_Data_Length(1) Packet_Payload(1) PHY(1) = 4 | Status(1) |
| LE Set Advertising Set Random Address | `0x2035` | `0x08` | `0x0035` | Advertising_Handle(1) Random_Address(6) = 7 | Status(1) |
| LE Set Extended Advertising Parameters [v1] | `0x2036` | `0x08` | `0x0036` | Handle(1) Event_Properties(2) Pri_Int_Min(3) Pri_Int_Max(3) Pri_Chan_Map(1) Own_Addr_Type(1) Peer_Addr_Type(1) Peer_Addr(6) Filter_Policy(1) TX_Power(1,s) Pri_PHY(1) Sec_Max_Skip(1) Sec_PHY(1) SID(1) Scan_Req_Notif(1) = 25 | Status(1), Selected_TX_Power(1,s) |
| LE Set Extended Advertising Data | `0x2037` | `0x08` | `0x0037` | Handle(1) Operation(1) Fragment_Pref(1) Data_Length(1) Data(0..251) | Status(1) |
| LE Set Extended Scan Response Data | `0x2038` | `0x08` | `0x0038` | Handle(1) Operation(1) Fragment_Pref(1) Data_Length(1) Data(0..251) | Status(1) |
| LE Set Extended Advertising Enable | `0x2039` | `0x08` | `0x0039` | Enable(1) Num_Sets(1) { Handle(1) Duration(2) Max_Ext_Adv_Events(1) } x Num_Sets | Status(1) |
| LE Read Maximum Advertising Data Length | `0x203A` | `0x08` | `0x003A` | - | Status(1), Max_Adv_Data_Length(2) |
| LE Read Number of Supported Advertising Sets | `0x203B` | `0x08` | `0x003B` | - | Status(1), Num_Sets(1) |
| LE Remove Advertising Set | `0x203C` | `0x08` | `0x003C` | Advertising_Handle(1) | Status(1) |
| LE Clear Advertising Sets | `0x203D` | `0x08` | `0x003D` | - | Status(1) |
| LE Set Periodic Advertising Parameters [v1] | `0x203E` | `0x08` | `0x003E` | Handle(1) Interval_Min(2) Interval_Max(2) Properties(2) = 7 | Status(1) |
| LE Set Periodic Advertising Data | `0x203F` | `0x08` | `0x003F` | Handle(1) Operation(1) Data_Length(1) Data(0..252) | Status(1) |
| LE Set Periodic Advertising Enable | `0x2040` | `0x08` | `0x0040` | Enable(1) Handle(1) = 2 | Status(1) |
| LE Set Extended Scan Parameters | `0x2041` | `0x08` | `0x0041` | Own_Addr_Type(1) Filter_Policy(1) Scanning_PHYs(1) { Scan_Type(1) Interval(2) Window(2) } x popcount(PHYs) | Status(1) |
| LE Set Extended Scan Enable | `0x2042` | `0x08` | `0x0042` | Enable(1) Filter_Duplicates(1) Duration(2) Period(2) = 6 | Status(1) |
| LE Read Transmit Power | `0x204B` | `0x08` | `0x004B` | - | Status(1), Min_TX_Power(1,s), Max_TX_Power(1,s) |
| LE Receiver Test [v3] | `0x204F` | `0x08` | `0x004F` | RX_Channel(1) PHY(1) Modulation_Index(1) Expected_CTE_Length(1) Expected_CTE_Type(1) Slot_Durations(1) Switching_Pattern_Length(1) Antenna_IDs(n) | Status(1) |
| LE Transmitter Test [v3] | `0x2050` | `0x08` | `0x0050` | TX_Channel(1) Test_Data_Length(1) Packet_Payload(1) PHY(1) CTE_Length(1) CTE_Type(1) Switching_Pattern_Length(1) Antenna_IDs(n) | Status(1) |
| LE Transmitter Test [v4] | `0x207B` | `0x08` | `0x007B` | TX_Channel(1) Test_Data_Length(1) Packet_Payload(1) PHY(1) CTE_Length(1) CTE_Type(1) Switching_Pattern_Length(1) Antenna_IDs(n) Transmit_Power_Level(1,s) | Status(1) |
<!-- END:opcodes -->

### 2.3 Value tables

**Advertising_Type (HCI, LE Set Advertising Parameters)** and what goes on air:

| HCI value | Name | On-air PDU (PDU_Type nibble) | Connectable | Scannable | Directed |
|:--|:--|:--|:--|:--|:--|
| 0x00 | ADV_IND | ADV_IND (0x0) | yes | yes | no |
| 0x01 | ADV_DIRECT_IND, **high duty cycle** | ADV_DIRECT_IND (0x1) | yes | no | yes |
| 0x02 | ADV_SCAN_IND | ADV_SCAN_IND (0x6) | no | yes | no |
| 0x03 | ADV_NONCONN_IND | ADV_NONCONN_IND (0x2) | no | no | no |
| 0x04 | ADV_DIRECT_IND, **low duty cycle** | ADV_DIRECT_IND (0x1) | yes | no | yes |

Other on-air PDU_Type nibbles (advertising channel PDU header bits 0-3): SCAN_REQ 0x3, SCAN_RSP 0x4, CONNECT_IND 0x5, ADV_EXT_IND / AUX_ADV_IND / AUX_SCAN_RSP / AUX_SYNC_IND / AUX_CHAIN_IND 0x7, AUX_CONNECT_RSP 0x8.
Header byte 0: bits 0-3 PDU_Type, bit 4 RFU, bit 5 ChSel, bit 6 TxAdd (advertiser address random), bit 7 RxAdd; byte 1 = payload length (legacy max 37 = 6 AdvA + 31 data).

**LE Advertising Report `Event_Type` (legacy HCI event):** 0x00 ADV_IND, 0x01 ADV_DIRECT_IND, 0x02 ADV_SCAN_IND, 0x03 ADV_NONCONN_IND, 0x04 SCAN_RSP.

**Own_Address_Type** (adv, scan, ext adv, ext scan): 0x00 public; 0x01 random (static/NRPA set with LE Set Random Address / Set Advertising Set Random Address); 0x02 controller-generated RPA from the resolving list, falling back to **public**;
0x03 same but falling back to the **random** address. **Peer_Address_Type:** 0x00 public (or public identity), 0x01 random (or random identity). Random address class = two MSBs of the most-significant octet
(sent last on the wire): `11` static, `01` resolvable private, `00` non-resolvable private (random part must not be all 0 or all 1).

**Advertising_Filter_Policy:** 0x00 scan+connect from anyone; 0x01 scan from Filter Accept List only, connect from anyone; 0x02 scan from anyone, connect from FAL only; 0x03 both FAL.
**Scanning_Filter_Policy:** 0x00 accept all (except directed not for us), 0x01 FAL only, (0x02/0x03 = 0x00/0x01 plus directed adverts using RPAs). **Scan_Type:** 0 passive, 1 active.
**Filter_Duplicates:** 0 off (use this to see every packet), 1 on, 2 on and reset every scan period (Extended Scan Enable only).

**Intervals:** advertising 0.625 ms units, `0x0020..0x4000` = 20 ms..10.24 s, default 0x0800 = 1.28 s; legacy on Core <= 4.2: >= 0x00A0 (100 ms) for ADV_SCAN_IND/ADV_NONCONN_IND, 20 ms for ADV_IND/connectable;
high-duty directed advertising ignores the interval (PDUs <= 3.75 ms apart, stops after 1.28 s). Scan interval/window 0.625 ms units, `0x0004..0x4000` = 2.5 ms..10.24 s, window <= interval (equal = continuous).
Extended primary interval = 3 octets (min 0x000020). Periodic advertising interval unit **1.25 ms**, min 0x0006 (7.5 ms). Extended advertising `Duration` unit **10 ms**, Extended Scan `Duration` 10 ms, `Period` **1.28 s**.
BE200 is a BT 5.4 part, so 20 ms should be accepted for all types; unverified on this firmware.

**Extended advertising `Advertising_Event_Properties` (u16 LE):**

| Bit | Mask | Meaning |
|---:|:--|:--|
| 0 | 0x0001 | Connectable |
| 1 | 0x0002 | Scannable |
| 2 | 0x0004 | Directed |
| 3 | 0x0008 | High-duty-cycle directed connectable advertising (<= 3.75 ms interval, 1.28 s) |
| 4 | 0x0010 | Use legacy advertising PDUs |
| 5 | 0x0020 | Omit advertiser's address (anonymous) |
| 6 | 0x0040 | Include TxPower in the extended header of at least one advertising PDU |
| 7-15 | | reserved |

Legacy-PDU emulations: ADV_IND `0x0013`; ADV_DIRECT_IND low duty `0x0015`; ADV_DIRECT_IND high duty `0x001D`; ADV_SCAN_IND `0x0012`; ADV_NONCONN_IND `0x0010`. With the legacy bit set: Primary PHY must be LE 1M, data <= 31, Operation must be 0x03.
True extended sets: non-connectable non-scannable (`0x0000`), connectable non-scannable (`0x0001`), scannable non-connectable (`0x0002`), plus directed variants; connectable **and** scannable is not allowed for extended PDUs.

**Extended advertising report `Event_Type` (u16):** bit0 connectable, bit1 scannable, bit2 directed, bit3 scan response, bit4 legacy PDU, bits 5-6 data status (00 complete, 01 incomplete/more coming, 10 incomplete/truncated).
Legacy values: ADV_IND 0x0013, ADV_DIRECT_IND 0x0015, ADV_SCAN_IND 0x0012, ADV_NONCONN_IND 0x0010, SCAN_RSP to ADV_IND 0x001B, SCAN_RSP to ADV_SCAN_IND 0x001A.

**PHY values.** Transmitter Test v2+: 0x01 LE 1M, 0x02 LE 2M, 0x03 LE Coded S=8, 0x04 LE Coded S=2. Receiver Test v2+: 0x01 1M, 0x02 2M, 0x03 Coded. Extended advertising: Primary 0x01 1M or 0x03 Coded; Secondary 0x01/0x02/0x03.
Extended Scan Parameters `Scanning_PHYs` bitmask: bit0 = 1M, bit2 = Coded (bit1/2M is not a scanning PHY); one 5-octet block per set bit, 1M first. Reports: Primary_PHY 1 or 3; Secondary_PHY 0 = none, 1, 2, 3.

**Packet_Payload (LE Transmitter Test), patterns written in transmission order (first bit sent = LSB of the octet):**

| Code | Pattern | Octet value (repeating) |
|:--|:--|:--|
| 0x00 | PRBS9, `11111111100000111101...` (x^9+x^5+1, all-ones seed; octets `FF C1 FB E8 4C 90 72 8B ...`, period 511 bits) | - |
| 0x01 | repeated `11110000` | **0x0F** |
| 0x02 | repeated `10101010` | **0x55** |
| 0x03 | PRBS15 | - |
| 0x04 | repeated `11111111` | **0xFF** |
| 0x05 | repeated `00000000` | **0x00** |
| 0x06 | repeated `00001111` | 0xF0 |
| 0x07 | repeated `01010101` | 0xAA |
| 0x08-0xFF | reserved | |

LE event mask (*generated*; bit n enables subevent n+1; default after Reset `0x000000000000001F` = bits 0-4; BlueZ btmon decodes the same bit-to-name mapping):

<!-- BEGIN:eventmask -->
| LE Event Mask bit | Subevent code | Name |
|---:|:--|:--|
| 0 | `0x01` | LE Connection Complete |
| 1 | `0x02` | LE Advertising Report |
| 2 | `0x03` | LE Connection Update Complete |
| 3 | `0x04` | LE Read Remote Features Complete |
| 4 | `0x05` | LE Long Term Key Request |
| 5 | `0x06` | LE Remote Connection Parameter Request |
| 6 | `0x07` | LE Data Length Change |
| 7 | `0x08` | LE Read Local P-256 Public Key Complete |
| 8 | `0x09` | LE Generate DHKey Complete |
| 9 | `0x0A` | LE Enhanced Connection Complete |
| 10 | `0x0B` | LE Directed Advertising Report |
| 11 | `0x0C` | LE PHY Update Complete |
| 12 | `0x0D` | LE Extended Advertising Report |
| 13 | `0x0E` | LE Periodic Advertising Sync Established |
| 14 | `0x0F` | LE Periodic Advertising Report |
| 15 | `0x10` | LE Periodic Advertising Sync Lost |
| 16 | `0x11` | LE Scan Timeout |
| 17 | `0x12` | LE Advertising Set Terminated |
| 18 | `0x13` | LE Scan Request Received |
| 19 | `0x14` | LE Channel Selection Algorithm |
<!-- END:eventmask -->

### 2.4 Commands: exact layouts

Offsets are within the command parameters (after the 4-octet `01 op_lo op_hi plen` header). All commands return `Status(1)` in Command Complete unless stated.

#### Reset — `0x0C03`
No parameters. Aborts everything (advertising, scanning, test mode, all advertising sets); event masks return to defaults (LE Meta disabled again). Under BlueZ the kernel owns this; never send behind `bluetoothd`'s back on a managed controller.

#### Set Event Mask — `0x0C01`
`Event_Mask(8)` u64 LE. Default `0x00001FFFFFFFFFFF`. Needs bit 61 (`0x2000000000000000`, "LE Meta Event") or the host never sees LE subevents. Typical host value `0x20001FFFFFFFFFFF` (module: `EVENT_MASK_HOST_TYPICAL`).

#### LE Set Event Mask — `0x2001`
`LE_Event_Mask(8)` u64 LE, see the table above. To receive reports: bit 1 (`0x2`) legacy Advertising Report, bit 12 (`0x1000`) Extended Advertising Report, bit 17 Advertising Set Terminated, bit 18 Scan Request Received.

#### LE Set Random Address — `0x2005`
`Random_Address(6)` LSB first. Disallowed (0x0C) while legacy advertising, scanning or initiating is enabled - set it first. Extended advertising sets do not use this address; they use `LE Set Advertising Set Random Address`.

#### LE Set Advertising Parameters — `0x2006` (15 octets)
| Off | Field | Size | Encoding |
|--:|:--|--:|:--|
| 0 | Advertising_Interval_Min | 2 | u16 LE, 0.625 ms, 0x0020..0x4000 |
| 2 | Advertising_Interval_Max | 2 | u16 LE, >= Min |
| 4 | Advertising_Type | 1 | 0x00..0x04 (table 2.3) |
| 5 | Own_Address_Type | 1 | 0x00..0x03 |
| 6 | Peer_Address_Type | 1 | 0/1 (directed types only) |
| 7 | Peer_Address | 6 | LSB first (directed types only; otherwise ignored) |
| 13 | Advertising_Channel_Map | 1 | bit0 = 37, bit1 = 38, bit2 = 39 |
| 14 | Advertising_Filter_Policy | 1 | 0..3 |

Disallowed (0x0C) while advertising is enabled. Defaults: 0x0800 / 0x0800, ADV_IND, public, map 0x07, policy 0.

#### LE Set Advertising Data — `0x2008` (32 octets)
`Advertising_Data_Length(1)` 0..31, `Advertising_Data(31)` significant octets first then zero padding. May be updated while advertising (used from the next advertising event). Initial content: empty.

#### LE Set Scan Response Data — `0x2009` (32 octets)
Same layout. Only transmitted in SCAN_RSP, i.e. only for ADV_IND / ADV_SCAN_IND and only to an active scanner that sent SCAN_REQ.

#### LE Set Advertising Enable — `0x200A` (1 octet)
`Advertising_Enable(1)`: 0 disable, 1 enable. Command Complete only; there is no "started" event. High-duty directed advertising ends after 1.28 s with an LE Connection Complete with status 0x3C (Advertising Timeout).

#### LE Set Scan Parameters — `0x200B` (7 octets)
`LE_Scan_Type(1)` | `LE_Scan_Interval(2)` | `LE_Scan_Window(2)` | `Own_Address_Type(1)` | `Scanning_Filter_Policy(1)`. Disallowed (0x0C) while scanning is enabled.

#### LE Set Scan Enable — `0x200C` (2 octets)
`LE_Scan_Enable(1)` | `Filter_Duplicates(1)` (0/1). Every received PDU produces an LE Advertising Report (subevent 0x02) when duplicate filtering is off.

#### LE Transmitter Test [v1] — `0x201E` (3 octets)
`TX_Channel(1)` = **RF channel k 0x00..0x27** | `Test_Data_Length(1)` 0x00..0xFF (controllers <= Core 4.1 only accept 0..0x25 = 37) | `Packet_Payload(1)` 0x00..0x07 (table 2.3). LE 1M PHY.
Return: Status only (Command Complete); the controller then transmits one test packet per packet interval until `LE Test End`. No further events.

#### LE Transmitter Test [v2] — `0x2034` (4 octets)
v1 fields + `PHY(1)`: 0x01 1M, 0x02 2M, 0x03 Coded S=8, 0x04 Coded S=2. (Also called "LE Enhanced Transmitter Test".) **Recommended command for the demo** (PHY selectable, no CTE fields).

#### LE Transmitter Test [v3] — `0x2050` (7 + N octets)
v2 fields + `CTE_Length(1)` (0 = none, else 0x02..0x14 in 8 us units) | `CTE_Type(1)` (0 AoA, 1 AoD 1 us slots, 2 AoD 2 us slots) | `Switching_Pattern_Length(1)` (0x02..0x4B when a CTE is sent) | `Antenna_IDs[N]`(1 each).
With `CTE_Length = 0` send `0,0,0` and no antenna IDs (spec ignores them; confidence medium - BE200 has no antenna array, avoid v3/v4 CTE).

#### LE Transmitter Test [v4] — `0x207B` (8 + N octets)
v3 fields + `Transmit_Power_Level(1)` signed dBm, -127..+20; `0x7E` = minimum, `0x7F` = maximum the controller supports. The only test command that lets the host choose power (use `LE Read Transmit Power` 0x204B for min/max). btmon 5.77 prints it as Unknown.

#### LE Test End — `0x201F`
No parameters. Return: `Status(1)` | `Num_Packets(2)` u16 LE. After a transmitter test Num_Packets = 0; after a receiver test it is the number of packets received with a correct CRC. Returns 0x0C if no test is running.

#### LE Receiver Test [v1] — `0x201D` (1 octet)
`RX_Channel(1)` = RF channel k. Listens for DTM packets (access address 0x71764129) on that channel, counts CRC-good packets, reported by LE Test End.

#### LE Receiver Test [v2] — `0x2033` (3 octets)
`RX_Channel(1)` | `PHY(1)` (1 = 1M, 2 = 2M, 3 = Coded) | `Modulation_Index(1)` (0 standard, 1 stable).

#### LE Receiver Test [v3] — `0x204F` (7 + N octets)
v2 fields + `Expected_CTE_Length(1)` | `Expected_CTE_Type(1)` | `Slot_Durations(1)` (1 = 1 us, 2 = 2 us) | `Switching_Pattern_Length(1)` | `Antenna_IDs[N]`.

#### LE Set Extended Advertising Parameters [v1] — `0x2036` (25 octets)
| Off | Field | Size | Encoding |
|--:|:--|--:|:--|
| 0 | Advertising_Handle | 1 | 0x00..0xEF |
| 1 | Advertising_Event_Properties | 2 | u16 LE (table 2.3) |
| 3 | Primary_Advertising_Interval_Min | 3 | u24 LE, 0.625 ms, >= 0x000020 |
| 6 | Primary_Advertising_Interval_Max | 3 | u24 LE |
| 9 | Primary_Advertising_Channel_Map | 1 | bit0 37, bit1 38, bit2 39 |
| 10 | Own_Address_Type | 1 | 0..3 |
| 11 | Peer_Address_Type | 1 | 0/1 |
| 12 | Peer_Address | 6 | LSB first |
| 18 | Advertising_Filter_Policy | 1 | 0..3 |
| 19 | Advertising_TX_Power | 1 | s8 dBm -127..+20; 0x7F = no preference |
| 20 | Primary_Advertising_PHY | 1 | 0x01 1M or 0x03 Coded |
| 21 | Secondary_Advertising_Max_Skip | 1 | events that may be skipped before the AUX packet |
| 22 | Secondary_Advertising_PHY | 1 | 0x01, 0x02, 0x03 |
| 23 | Advertising_SID | 1 | 0x00..0x0F |
| 24 | Scan_Request_Notification_Enable | 1 | 0/1 |

Return: `Status(1)` | `Selected_TX_Power(1)` s8. Not allowed to change while that set is enabled (0x0C). Secondary/SID fields are unused for legacy-PDU sets.

#### LE Set Extended Advertising Data — `0x2037` (4 + N octets, N <= 251)
`Advertising_Handle(1)` | `Operation(1)`: 0 intermediate fragment, 1 first, 2 last, 3 complete, 4 unchanged data (update DID only) | `Fragment_Preference(1)`: 0 controller may fragment, 1 should not | `Advertising_Data_Length(1)` | `Advertising_Data(N)`.
Total per set up to 1650 octets (further limited by `LE Read Maximum Advertising Data Length` 0x203A). Legacy-PDU sets: <= 31 octets, Operation 3.

#### LE Set Extended Scan Response Data — `0x2038` (4 + N octets, N <= 251)
Same layout; Operation 0..3 (no 4).

#### LE Set Extended Advertising Enable — `0x2039` (2 + 4n octets)
`Enable(1)` | `Number_of_Sets(1)` | per set: `Advertising_Handle(1)` | `Duration(2)` u16 LE in **10 ms** (0 = until disabled) | `Max_Extended_Advertising_Events(1)` (0 = no limit).
`Enable = 0` with `Number_of_Sets = 0` disables every set. Duration/event-limit expiry produces LE Advertising Set Terminated (subevent 0x12; 0x3C = timeout, 0x43 = limit reached).

#### LE Set Advertising Set Random Address — `0x2035` (7 octets)
`Advertising_Handle(1)` | `Random_Address(6)` LSB first. Needed for sets with a random Own_Address_Type before enabling (otherwise the enable is rejected - 0x12 in my recollection, confidence medium).

#### LE Set Extended Scan Parameters — `0x2041` (3 + 5n octets)
`Own_Address_Type(1)` | `Scanning_Filter_Policy(1)` | `Scanning_PHYs(1)` (bit0 1M, bit2 Coded) | per PHY: `Scan_Type(1)` | `Scan_Interval(2)` | `Scan_Window(2)`. Disallowed while extended scanning is enabled.

#### LE Set Extended Scan Enable — `0x2042` (6 octets)
`Enable(1)` | `Filter_Duplicates(1)` (0/1/2) | `Duration(2)` u16 LE in 10 ms | `Period(2)` u16 LE in 1.28 s. `Duration = 0, Period = 0` = scan continuously until disabled. Without extended scanning enabled by the host (and the LE-mask bit 12), extended PDUs are invisible.

#### LE Set Periodic Advertising Parameters [v1] — `0x203E` (7 octets)
`Advertising_Handle(1)` | `Periodic_Advertising_Interval_Min(2)` | `Max(2)` (1.25 ms units, >= 0x0006) | `Periodic_Advertising_Properties(2)` (bit 6 = include TxPower). The handle must belong to a non-connectable, non-scannable, non-legacy extended set. Periodic data is sent as AUX_SYNC_IND on data channels, only to scanners that synchronise.

#### LE Set Periodic Advertising Data — `0x203F` (3 + N octets, N <= 252)
`Advertising_Handle(1)` | `Operation(1)` (0 intermediate, 1 first, 2 last, 3 complete, 4 unchanged) | `Advertising_Data_Length(1)` | `Advertising_Data(N)`.

#### LE Set Periodic Advertising Enable — `0x2040` (2 octets)
`Enable(1)`: bit0 = enable periodic advertising, bit1 = include ADI (Core 5.2+) | `Advertising_Handle(1)`. The extended set must additionally be enabled with `LE Set Extended Advertising Enable` for the periodic train to be announced.

#### Supporting commands (no parameters unless stated)
* Read Local Supported Commands — `0x1002` (OGF 0x04): returns 64-octet bitmap; use it to detect Transmitter Test v2/v3/v4, extended advertising, etc. before relying on them (bit positions deliberately not tabulated here: not verified).
* LE Read Local Supported Features — `0x2003`; LE Read Supported States — `0x201C` (which state combinations such as advertising + scanning are allowed); LE Read Maximum Advertising Data Length — `0x203A` (u16);
  LE Read Number of Supported Advertising Sets — `0x203B` (u8); LE Remove Advertising Set — `0x203C` (handle); LE Clear Advertising Sets — `0x203D`; LE Read Transmit Power — `0x204B` (s8 min, s8 max).

### 2.5 Events

**LE Advertising Report (LE Meta subevent 0x02).** Spec layout is arrays of fields; for `Num_Reports = 1` (what controllers send, and what BlueZ's sequential parser assumes) it is:

| Off | Field | Size |
|--:|:--|--:|
| 0 | Subevent_Code = 0x02 | 1 |
| 1 | Num_Reports (= 1) | 1 |
| 2 | Event_Type (0 ADV_IND, 1 ADV_DIRECT_IND, 2 ADV_SCAN_IND, 3 ADV_NONCONN_IND, 4 SCAN_RSP) | 1 |
| 3 | Address_Type (0 public, 1 random, 2 public identity, 3 random identity) | 1 |
| 4 | Address (LSB first) | 6 |
| 10 | Data_Length (0..31) | 1 |
| 11 | Data | Data_Length |
| 11+L | RSSI (s8 dBm, 127 = not available) | 1 |

Event parameter length = 1 + 1 + 1 + 1 + 6 + 1 + L + 1 = 12 + L.

**LE Extended Advertising Report (LE Meta subevent 0x0D)**, `Num_Reports = 1`; each report is **24 fixed octets + Data** (kernel `struct hci_ev_le_ext_adv_info` agrees):

| Off | Field | Size |
|--:|:--|--:|
| 0 | Subevent_Code = 0x0D | 1 |
| 1 | Num_Reports | 1 |
| 2 | Event_Type (u16 LE, bits in table 2.3) | 2 |
| 4 | Address_Type (0 public, 1 random, 2 public identity, 3 random identity, 0xFF anonymous) | 1 |
| 5 | Address | 6 |
| 11 | Primary_PHY (1 = 1M, 3 = Coded) | 1 |
| 12 | Secondary_PHY (0 none, 1 = 1M, 2 = 2M, 3 = Coded) | 1 |
| 13 | Advertising_SID (0..15; 0xFF = no ADI) | 1 |
| 14 | TX_Power (s8, 127 = n/a) | 1 |
| 15 | RSSI (s8, 127 = n/a) | 1 |
| 16 | Periodic_Advertising_Interval (u16 LE, 1.25 ms; 0 = none) | 2 |
| 18 | Direct_Address_Type | 1 |
| 19 | Direct_Address | 6 |
| 25 | Data_Length (0..229) | 1 |
| 26 | Data | Data_Length |

Other relevant events: LE Advertising Set Terminated (0x12), LE Scan Request Received (0x13, if `Scan_Request_Notification_Enable`), LE Periodic Advertising Sync Established/Report/Lost (0x0E/0x0F/0x10), LE Scan Timeout (0x11).

### 2.6 What happens on the air, and what a scanner sees

**Legacy advertising event (Advertising_Type 0x00/0x02/0x03).** The Link Layer transmits the PDU once on each enabled primary channel, in ascending channel order 37 -> 38 -> 39 (only those enabled in the map),
access address `0x8E89BED6`, CRC init `0x555555`, LE 1M, with at most 10 ms between the *starts* of consecutive PDUs of the same event. Events repeat every `advInterval + advDelay` where advDelay is a pseudo-random 0-10 ms
(none for high-duty directed). A maximum legacy PDU (31 data octets) is `(1+4+2+6+31+3)*8 = 376 us`; one event on three channels is 3 x 376 = 1128 us of airtime.
* **ADV_IND**: after each PDU the advertiser listens (T_IFS = 150 us after the PDU ends) for a SCAN_REQ or CONNECT_IND on that channel. An *active* scanner answers with SCAN_REQ (ScanA + AdvA); 150 us later the advertiser sends SCAN_RSP (AdvA + Scan Response Data, <= 31 octets).
* **ADV_SCAN_IND**: same, but CONNECT_IND is ignored.
* **ADV_NONCONN_IND**: broadcast only - no listening window, SCAN_REQ ignored, scan response data never sent. This is the lowest-airtime type and what beacons normally use.
* **ADV_DIRECT_IND**: AdvA + TargetA, only the addressed initiator reacts; ordinary scanners drop it (a scanner sees Event_Type 0x01 only with the right filter policy). High duty: PDUs <= 3.75 ms apart for <= 1.28 s, interval parameter ignored; low duty: ADV_IND-like intervals.

**What a scanner sees.** A scanner listens on **one** primary channel per scan window and rotates 37 -> 38 -> 39; so it sees, per advertising event, at most the copy sent on the channel it is currently on.
Each PDU that passes AA + CRC produces one LE Advertising Report (Event_Type, address, up to 31 data octets, RSSI); with duplicate filtering on, only the first. A passive scanner never sees scan responses; an active scanner gets a second report with Event_Type 0x04 (SCAN_RSP)
from the same address (BlueZ merges both into one device). A scanner reports only CRC-valid packets, so a packet that collided with interference is simply missing (there is no "corrupt packet" report).
**Extended advertising** puts a small ADV_EXT_IND on 37/38/39 containing an AuxPtr (channel index = a *data* channel 0-36, offset, PHY); the payload (up to 1650 octets over AUX_ADV_IND + AUX_CHAIN_IND) goes out on the data channel on 1M/2M/Coded.
Only scanners using LE Set Extended Scan Parameters/Enable (and event-mask bit 12) get LE Extended Advertising Reports (0x0D), possibly in fragments (data status 01); legacy-only (<= 4.2) scanners never see ADV_EXT_IND. Extended sets that use the legacy bit (props 0x13 etc.) are visible to everyone.
**Periodic advertising** (AUX_SYNC_IND on data channels at a fixed interval) is only receivable after synchronising from the ADV_EXT_IND/AUX_ADV_IND SyncInfo; an unsynchronised scanner just sees a non-zero `Periodic_Advertising_Interval` in the extended report.

### 2.7 State restrictions (what must be idle)

* **LE Transmitter Test / LE Receiver Test (all versions)**: enter the Direct-Test state from Standby. Stop advertising (`0x200A`=0 / extended enable=0), scanning (`0x200C`=0 / `0x2042`=0), initiating and disconnect links first; a controller that is not idle, or already in a test, answers 0x0C Command Disallowed.
  Leave with `LE Test End` (0x201F) or `Reset`. While a test runs, further advertising/scanning/connection commands are refused (0x0C). Confidence high on the principle, medium on exactly which commands a given firmware refuses (BE200 not tested). Only one test per controller: eight BE200 controllers -> up to eight concurrent test transmitters/receivers.
* LE Set Advertising Parameters: not while advertising. LE Set Advertising/Scan Response Data: allowed while advertising. LE Set Scan Parameters: not while scanning. LE Set Random Address: not while legacy advertising/scanning/initiating.
  Extended sets: parameters not while that set is enabled; scan parameters not while extended scan is enabled.
* Under BlueZ: `bluetoothd`/kernel mgmt track adv/scan state and power; commands injected on a raw socket bypass that state. A test run on an `hciN` therefore needs that controller to have no BlueZ scan/advertising instances (verify with `btmon` while running). Not exercised here (read-only recon).

### 2.8 Golden command vectors (*generated; asserted byte-for-byte against hand-typed hex*)

<!-- BEGIN:golden -->
| Packet | H4 bytes |
|:--|:--|
| Reset | `01 03 0c 00` |
| Set Event Mask (default) | `01 01 0c 08 ff ff ff ff ff 1f 00 00` |
| LE Set Event Mask (0x1F) | `01 01 20 08 1f 00 00 00 00 00 00 00` |
| LE Set Random Address C0:11:22:33:44:55 | `01 05 20 06 55 44 33 22 11 c0` |
| LE Set Advertising Parameters (defaults: 0x0800, ADV_IND, ch map 7) | `01 06 20 0f 00 08 00 08 00 00 00 00 00 00 00 00 00 07 00` |
| LE Set Advertising Data 02 01 06 | `01 08 20 20 03 02 01 06 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00` |
| LE Set Advertising Enable = 1 | `01 0a 20 01 01` |
| LE Set Scan Parameters passive 10ms/10ms | `01 0b 20 07 00 10 00 10 00 00 00` |
| LE Set Scan Enable = 1, dup filter off | `01 0c 20 02 01 00` |
| LE Transmitter Test v1 ch=19 (2440) len=37 PRBS9 | `01 1e 20 03 13 25 00` |
| LE Transmitter Test v2 ch=0 len=255 all-ones LE 2M | `01 34 20 04 00 ff 04 02` |
| LE Transmitter Test v4 ch=12 len=37 PRBS9 1M -10 dBm | `01 7b 20 08 0c 25 00 01 00 00 00 f6` |
| LE Receiver Test v1 ch=39 (2480) | `01 1d 20 01 27` |
| LE Receiver Test v2 ch=12 1M std modulation | `01 33 20 03 0c 01 00` |
| LE Test End | `01 1f 20 00` |
| LE Set Ext Adv Params handle0 legacy ADV_IND 30ms | `01 36 20 19 00 13 00 30 00 00 30 00 00 07 00 00 00 00 00 00 00 00 00 7f 01 00 01 00 00` |
| LE Set Ext Adv Data handle0 complete 02 01 06 | `01 37 20 07 00 03 01 03 02 01 06` |
| LE Set Ext Adv Enable handle0 until disabled | `01 39 20 06 01 01 00 00 00 00` |
| LE Set Adv Set Random Address handle0 | `01 35 20 07 00 55 44 33 22 11 c0` |
| LE Set Ext Scan Params 1M passive 10ms/10ms | `01 41 20 08 00 00 01 00 10 00 10 00` |
| LE Set Ext Scan Enable continuous | `01 42 20 06 01 00 00 00 00 00` |
| LE Set Periodic Adv Params handle0 7.5-20ms | `01 3e 20 07 00 06 00 10 00 00 00` |
| LE Set Periodic Adv Data handle0 complete | `01 3f 20 06 00 03 03 02 01 06` |
| LE Set Periodic Adv Enable handle0 | `01 40 20 02 01 00` |
<!-- END:golden -->

---

## 3. AD structures and ready payload builders

### 3.1 AD structure rules

`[Length(1)] [AD_Type(1)] [Data(Length-1)]` - Length counts type + data, so a structure occupies Length+1 octets. A legacy payload is a concatenation of structures, <= **31 octets total**; trailing zero padding is allowed (`Length = 0` terminates).
Every builder in the module validates the 31-octet limit (`check_legacy_len` raises `ValueError`, returns the free octets) and `ad_parse` re-parses and validates.

| AD type | Code | Encoder | Data |
|:--|:--|:--|:--|
| Flags | 0x01 | `ad_flags` | 1 octet: bit0 LE limited disc., bit1 LE general disc., bit2 BR/EDR not supported, bit3 simultaneous LE+BR/EDR (controller), bit4 (host). LE-only general = `02 01 06` |
| Incomplete / Complete 16-bit UUIDs | 0x02 / 0x03 | `ad_uuids16` | u16 LE each |
| Incomplete / Complete 32-bit UUIDs | 0x04 / 0x05 | `ad_uuids32` | u32 LE each |
| Incomplete / Complete 128-bit UUIDs | 0x06 / 0x07 | `ad_uuids128` | 16 octets each, little-endian (reverse of the canonical string) |
| Shortened / Complete Local Name | 0x08 / 0x09 | `ad_name` | UTF-8; use 0x08 for a truncated name |
| Tx Power Level | 0x0A | `ad_tx_power` | s8 dBm |
| Peripheral (slave) Connection Interval Range | 0x12 | `ad_conn_interval_range` | min u16 LE, max u16 LE, 1.25 ms units, 0x0006..0x0C80; 0xFFFF = no specific |
| Service Data - 16 / 32 / 128-bit UUID | 0x16 / 0x20 / 0x21 | `ad_service_data16/32/128` | UUID (LE) + data |
| Appearance | 0x19 | `ad_appearance` | u16 LE (category << 6 \| subcategory; Generic Sensor 0x0540, Generic Tag 0x0200, Thermometer 0x0300) |
| Manufacturer Specific Data | 0xFF | `ad_manufacturer` | Company ID u16 LE + data. **0xFFFF = reserved for internal/interoperability tests** (BlueZ's table names it "internal use"); Apple 0x004C is used only for the open iBeacon format |

### 3.2 Ready payloads (*generated; golden-asserted*)

<!-- BEGIN:payloads -->
| Payload | Octets | Free (of 31) | Hex |
|:--|---:|---:|:--|
| Flags only (LE general discoverable, no BR/EDR) | 3 | 28 | `02 01 06` |
| Flags + Complete Local Name 'BluForge-01' | 16 | 15 | `02 01 06 0c 09 42 6c 75 46 6f 72 67 65 2d 30 31` |
| iBeacon (UUID 12345678-9abc-def0-1234-56789abcdef0, major 1, minor 2, -59 dBm @1 m) | 30 | 1 | `02 01 06 1a ff 4c 00 02 15 12 34 56 78 9a bc de f0 12 34 56 78 9a bc de f0 00 01 00 02 c5` |
| Eddystone-UID (ns edd1ebeac04e5defa017, inst 000000000001, -21 dBm @0 m) | 31 | 0 | `02 01 06 03 03 aa fe 17 16 aa fe 00 eb ed d1 eb ea c0 4e 5d ef a0 17 00 00 00 00 00 01 00 00` |
| Eddystone-URL https://www.example.com/ (-20 dBm @0 m) | 22 | 9 | `02 01 06 03 03 aa fe 0e 16 aa fe 10 ec 01 65 78 61 6d 70 6c 65 00` |
| Eddystone-TLM (3000 mV, 22.5 C, 1000 adv, 3600.0 s) | 25 | 6 | `02 01 06 03 03 aa fe 11 16 aa fe 20 00 0b b8 16 80 00 00 03 e8 00 00 8c a0` |
| AltBeacon (mfg 0xFFFF, id 8492e75f-4fd6-469d-b132-043fe94921d8 / 1 / 2, -59 dBm) | 31 | 0 | `02 01 06 1b ff ff ff be ac 84 92 e7 5f 4f d6 46 9d b1 32 04 3f e9 49 21 d8 00 01 00 02 c5 00` |
| Sensor (company 0xFFFF, id 7, seq 42, -5.25 C, 55.5 %RH, 87 %, name 'S7') | 19 | 12 | `02 01 06 0b ff ff ff 01 07 2a f3 fd ae 15 57 03 09 53 37` |
| Service Data 0x181A + 2 octets, Tx Power +4 dBm, Appearance Generic Sensor | 16 | 15 | `02 01 06 05 16 1a 18 01 02 02 0a 04 03 19 40 05` |
| Peripheral conn-interval range 30-50 ms + 16-bit UUID list 0x180F,0x180A | 12 | 19 | `05 12 18 00 28 00 05 03 0f 18 0a 18` |
<!-- END:payloads -->

Formats:
* **iBeacon** (open format): `02 01 06 | 1A FF 4C 00 02 15 | UUID(16, big-endian as written) | Major(2, BE) | Minor(2, BE) | MeasuredPower(s8, RSSI at 1 m)` = 30 octets (1 free). Typically ADV_IND or ADV_NONCONN_IND.
* **Eddystone** (service UUID 0xFEAA; list `03 03 AA FE` + Service Data `.. 16 AA FE <frame>`): UID frame `00 | txPower@0m(s8) | namespace(10) | instance(6) | 00 00` = 31 octets (0 free);
  URL frame `10 | txPower | scheme(0 http://www., 1 https://www., 2 http://, 3 https://) | encoded URL(<=17; 0x00 .com/ 0x01 .org/ 0x02 .edu/ 0x03 .net/ 0x04 .info/ 0x05 .biz/ 0x06 .gov/ 0x07 .com 0x08 .org 0x09 .edu 0x0A .net 0x0B .info 0x0C .biz 0x0D .gov)`;
  unencrypted TLM `20 | 00 | VBATT(u16 BE mV) | TEMP(s8.8 BE, 0x8000 = n/a) | ADV_CNT(u32 BE) | SEC_CNT(u32 BE, 0.1 s)`.
* **AltBeacon**: `1B FF <Mfg ID u16 LE> BE AC <ID1 16 \| ID2 2 \| ID3 2 (BE)> <RefRSSI s8 @1m> <MfgReserved>` = 31 octets with Flags. The demo uses Mfg ID 0xFFFF.
* **Generic sensor** (synthetic, NOT a registered protocol): Mfg data under 0xFFFF = `version 0x01 | sensor_id | seq | temp s16 LE (0.01 C, 0x8000 n/a) | humidity u16 LE (0.01 %, 0xFFFF n/a) | battery u8 % (0xFF n/a)` (8 octets) optionally followed by a Complete Local Name.

Deliberately not implemented: Apple Continuity / Nearby / Fast Pair / Google Fast Pair / Microsoft Swift Pair style payloads (they trigger pairing or proximity UI on real phones). Use random UUIDs/namespaces for beacons so no real deployment's app reacts.

---

## 4. Interference physics for the demo

### 4.1 What an LE Transmitter Test packet looks like on the air

* Fixed RF channel k (no hopping), GFSK BT = 0.5, modulation index ~0.5 (symbol deviation +/- ~250 kHz for LE 1M, +/- ~500 kHz for LE 2M), CRC init `0x555555`, access address **`0x71764129`** (not the advertising AA `0x8E89BED6`), whitening off (so the patterns below appear verbatim on air; confidence high).
* Packet = preamble (1 octet LE 1M, 2 octets LE 2M) + AA (4) + 2-octet PDU header (length field = `Test_Data_Length`) + payload (`Test_Data_Length` octets of the pattern) + CRC (3). No CTE unless v3/v4 asks for one.
* Payload content changes the *spectrum*, not the duty cycle: PRBS9 (0x00) = random-like data, spectrum of ordinary traffic (~1 MHz wide for LE 1M); `0x0F` (code 0x01) = 4-bit runs, energy concentrated near fc +/- 250 kHz (the delta-f1 test pattern);
  `0x55` (code 0x02) = fastest alternation, energy near fc +/- ~200 kHz (the delta-f2 test pattern; spec floor 185 kHz); `0xFF` / `0x00` (codes 0x04 / 0x05) = constant frequency, i.e. an *unmodulated carrier* at fc +250 kHz / fc -250 kHz (LE 1M). For a "noisy" interferer use PRBS9; for a tone-like interferer use 0xFF/0x00.
* **Packet interval (Core, uncoded PHYs):** `I(L) = ceil((L_us + 249 us) / 625 us) * 625 us`, where `L_us` is the packet air time:
  LE 1M: `L_us = (1 + 4 + 2 + Test_Data_Length + 3) * 8 = (Test_Data_Length + 10) * 8`; LE 2M: `L_us = (2 + 4 + 2 + Test_Data_Length + 3) * 4 = (Test_Data_Length + 11) * 4`.
  Verification: 37 octets, 1M -> 376 us -> I = 625 us (the fixed interval of Core 4.0, which the formula was built to preserve: 376 + 249 = 625). Coded PHYs use a different constant and are not modelled.
  Confidence: 1M formula high; 2M formula medium-high (same constant, only the packet time changes). **Real firmware may differ - measure with a second controller's LE Receiver Test counting packets over a known time, or with the HackRF.**
* **Achievable duty cycle (*generated*):**

<!-- BEGIN:duty -->
| Test_Data_Length | 1M: air time us | 1M: interval us | 1M: duty | 2M: air time us | 2M: interval us | 2M: duty |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 80 | 625 | 12.8 % | 44 | 625 | 7.0 % |
| 10 | 160 | 625 | 25.6 % | 84 | 625 | 13.4 % |
| 37 | 376 | 625 | 60.2 % | 192 | 625 | 30.7 % |
| 100 | 880 | 1250 | 70.4 % | 444 | 1250 | 35.5 % |
| 150 | 1280 | 1875 | 68.3 % | 644 | 1250 | 51.5 % |
| 193 | 1624 | 1875 | 86.6 % | 816 | 1250 | 65.3 % |
| 239 | 1992 | 2500 | 79.7 % | 1000 | 1250 | 80.0 % |
| 255 | 2120 | 2500 | 84.8 % | 1064 | 1875 | 56.7 % |
<!-- END:duty -->

  Payload 255 at LE 1M: 2120 us on air every 2500 us = **84.8 %** (gap 380 us). At LE 2M: 1064 us every 1875 us = **56.7 %** (gap 811 us). The duty-maximising lengths are 193 (1M, 86.6 %, 1624/1875 us) and 239 (2M, 80.0 %, 1000/1250 us) because of the ceil() steps. 100 % is impossible; a per-controller test transmitter tops out ~87 %.
* **Effect on a scanner on the same RF channel.** The test packet cannot be decoded by a scanner (wrong access address) but it occupies the channel; an advertising packet (or SCAN_REQ/SCAN_RSP) that overlaps any part of a test packet is corrupted and lost, unless the wanted signal is strong enough to capture the receiver (BLE needs roughly C/I >= 21 dB co-channel for LE 1M, medium confidence - verify in Core Vol 6 Part A before quoting externally).
  Survival model (*generated*; victim start time uniform, equal-or-stronger interferer, no capture, same PHY timing): `P = max(0, I - L - victim_airtime) / I`:

<!-- BEGIN:survival -->
| Interferer (DTM TX on the SAME RF channel) | air / interval | duty | legacy ADV_IND 31 B (376 us) survives | SCAN_REQ (176 us) | ADV_IND 0 B (128 us) |
|:--|:--|---:|---:|---:|---:|
| LE 1M, Test_Data_Length 37 | 376 / 625 us | 60.2 % | 0.00 % | 11.7 % | 19.4 % |
| LE 1M, Test_Data_Length 100 | 880 / 1250 us | 70.4 % | 0.00 % | 15.5 % | 19.4 % |
| LE 1M, Test_Data_Length 193 | 1624 / 1875 us | 86.6 % | 0.00 % | 4.0 % | 6.6 % |
| LE 1M, Test_Data_Length 255 | 2120 / 2500 us | 84.8 % | 0.16 % | 8.2 % | 10.1 % |
| LE 2M, Test_Data_Length 37 | 192 / 625 us | 30.7 % | 9.12 % | 41.1 % | 48.8 % |
| LE 2M, Test_Data_Length 239 | 1000 / 1250 us | 80.0 % | 0.00 % | 5.9 % | 9.8 % |
| LE 2M, Test_Data_Length 255 | 1064 / 1875 us | 56.7 % | 23.20 % | 33.9 % | 36.4 % |
<!-- END:survival -->

  So a continuous 255-octet LE 1M test transmitter on RF channel 0/12/39 makes a 31-octet ADV_IND on that channel arrive 0.16 % of the time in this model (practically never), while the same advertiser on the other two advertising channels is unaffected; an advertiser rotating through 37/38/39 therefore loses ~1/3 of its copies and a scanner parked on that channel hears nothing.
  The scanner just sees missing reports; with the interferer's power far above the advertiser's it can also desensitise the scanner's AGC for the whole scan window.
* A test transmitter on a *data* channel (RF 1-11, 13-38) cannot disturb *legacy* advertising or scanning (scanners only listen on RF 0/12/39). It does affect established connections that hop onto it (adaptive frequency hopping can blacklist it via the channel map; at least 2 data channels must stay in use), and extended/periodic advertising, whose AUX_* packets are sent on data channels.
* Selectivity against neighbouring channels (BLE channels are 2 MHz apart): the Core receiver requirement is on the order of C/I >= 21 dB co-channel and a tolerated interferer about 17 dB *stronger* than the wanted signal at +/-2 MHz (C/I = -17 dB; the adjacent BLE channel) and about 27 dB stronger at +/-4 MHz, for LE 1M (medium confidence, from memory of Core Vol 6 Part A). A 1M test transmitter on k therefore mostly hurts k only, unless it is >17 dB stronger than the victim signal at the victim receiver.
  In practice, 8 BE200 cards share one chassis: a transmitter inches from a neighbouring card's BT receiver will saturate it on *any* channel (blocking/desense). Expect this and treat per-channel selectivity at the scanner as something to measure, not assume (not verified here).

### 4.2 Wi-Fi 20 MHz versus a 2 MHz BLE channel

* An OFDM 20 MHz Wi-Fi transmission (802.11a/g/n/ac/ax/be 20 MHz) always uses all 52 occupied sub-carriers (312.5 kHz spacing, occupied bandwidth ~16.6 MHz, 802.11 mask 0 dBr to +/-9 MHz, -20 dBr at +/-11 MHz, -28 dBr at +/-20 MHz) simultaneously and with roughly flat power density.
  It cannot confine itself to one 2 MHz BLE channel: every BLE channel inside the span (10 or 11 of them, table 1.5) sees the burst at the same time, each receiving ~9.2 dB less than the total Wi-Fi power (`10*log10(16.56/2)`), flat to within the mask edges.
* The interference is bursty: packets of ~50 us to ~5.5 ms with Wi-Fi's own duty cycle and sync/preamble structure, not a continuous 85 % comb. The BLE side suffers whenever its 376 us advertising packet (or a connection event) overlaps a Wi-Fi burst on a co-spanned channel; with heavy traffic generated by LANforge this approaches the table above only if Wi-Fi airtime is ~85 %.
* So Wi-Fi is the right tool for "wide" interference (e.g. 11 channels at once, plus the bonus that Wi-Fi 1/6/11 leaves the advertising channels mostly clear), and the wrong tool for single-channel interference.
  The one 802.11 exception, a 26-tone OFDMA resource unit (HE/EHT, 26 x 78.125 kHz ~ 2.03 MHz), only exists in the data field of trigger-based/DL-OFDMA frames whose preamble still spans 20 MHz and cannot be commanded from a LANforge station - not a usable per-channel interferer.
* The BLE AFH mechanism only protects *data* channels; the three advertising channels have fixed positions, which is why Wi-Fi ch 1 (LL 37), ch 2-5 (LL 38) and ch 13 (LL 39) directly degrade advertising/discovery.
* For single-BLE-channel interference use (a) a BE200 LE Transmitter Test on RF channel k (section 2, test v2 recommended), or (b) the HackRF (narrowband tone/noise centred on `2402 + 2k` MHz) - never Wi-Fi.

---

## 5. Using the module (API summary)

```python
import sys; sys.path.insert(0, "/home/lanforge/Desktop/EricssonBluForge/recon")
from ble_constants import *

# Channels
freq_mhz(rf), rf_channel_from_freq_mhz(mhz), ll_index_to_rf_channel(ll), rf_channel_to_ll_index(rf)
wifi_overlap_rf_channels(wifi_ch), ble_rf_channel_wifi_overlaps(rf)
# HCI (return full H4 packet bytes; send on an HCI socket yourself)
le_transmitter_test_v2(tx_channel=rf, test_data_length=255, packet_payload=0, phy=PHY_1M); le_test_end()
le_set_adv_params(interval_min=ms_to_adv_units(100), interval_max=ms_to_adv_units(100), adv_type=ADV_NONCONN_IND, channel_map=CHAN_MAP_ALL)
le_set_adv_data(build_ibeacon("12345678-9abc-def0-1234-56789abcdef0", 1, 2, -59)); le_set_adv_enable(True)
le_set_ext_adv_params(...); le_set_ext_adv_data(handle, data, operation=EXT_ADV_OP_COMPLETE); le_set_ext_adv_enable(True, [(0, 0, 0)])
parse_hci_event(pkt)   # Command Complete / Status, LE Advertising Report, LE Extended Advertising Report
# Physics
dtm_packet_airtime_us(n, PHY_1M), dtm_packet_interval_us(n, PHY_1M), dtm_duty_cycle(n, PHY_2M), survival_probability(376, 255, PHY_1M)
```
The module never opens a socket and never executes anything but `btmon -r <file>` in its self-test.

---

## 6. Verification log (*generated by `python3 ble_constants.py --fill-md`; rerun `python3 ble_constants.py` to re-check*)

<!-- BEGIN:selftest -->
```text
[1] channel math: 96 checks ok. 37/38/39 -> RF 0/12/39 -> 2402/2426/2480 MHz; Wi-Fi1->RF0-10, Wi-Fi6->RF13-22, Wi-Fi11->RF25-35, Wi-Fi13->RF30-39
[2a] opcode table: 36 commands, each opcode == (ogf<<10)|ocf and == independently hand-typed literal (212 checks).
[2b] kernel header /usr/src/kernels/6.11.9-100.fc39.x86_64/include/net/bluetooth/hci.h: 26 opcodes + AD/event constants match ours.
[2c] 24 golden H4 command vectors + size/range checks ok.
[2d] event builders/parsers ok (legacy report n=1 and spec array layout n=2; ext report = 24 fixed octets + data).
[3] AD encoders + iBeacon(30B)/Eddystone-UID(31B)/URL/TLM/AltBeacon(31B)/sensor golden vectors + 31-octet limit: 26 checks ok.
[4] DTM/physics: PRBS9 matches spec string; 37 B -> 625 us; 255 B -> 1M 2120/2500 us (84.8%), 2M 1064/1875 us (56.7%); best duty 1M len 193 (86.6%), 2M len 239 (80.0%); 13 checks ok.
btmon -r (BlueZ 5.77) decoded all 37 of our command packets (covering all 36 registry opcodes): OGF|OCF and plen agree, none 'Unknown' except documented v4.
  btmon status names: 11/11 identical to ours
  btmon LE subevent names 0x01..0x14: all 20 codes recognised; 18 identical wording; differing wording: {'0x4': ('LE Read Remote Features Complete', 'LE Read Remote Used Features'), '0xb': ('LE Directed Advertising Report', 'LE Direct Advertising Report')}
  btmon LE Set Event Mask single-bit decode: bit 1 -> LE Advertising Report; bit 12 -> LE Extended Advertising Report; bit 17 -> LE Extended Advertising Set Terminated; bit 18 -> LE Scan Request Received; bit 19 -> LE Channel Selection Algorithm
  btmon quirk (informational): LE Transmitter Test [v4]: btmon 5.77 has no decoder (prints 'Unknown (0x08|0x007b)'); opcode/plen still parsed
  btmon quirk (informational): LE Transmitter Test [v3]: btmon 5.77 decodes it with the RECEIVER-v3 field labels (CTE/slot-duration/antenna count) and says 'too short packet' for 7 octets; spec layout (Core): ..., CTE_Type, Switching_Pattern_Length, Antenna_IDs[i]
  btmon decodes Set Event Mask bit 61 (0x2000000000000000) as 'LE Meta'.
  btmon text ok: 'TX frequency: 2440 MHz (0x13)'
  btmon text ok: 'Test data length: 37 bytes'
  btmon text ok: 'Status: Command Disallowed (0x0c)'
  btmon text ok: 'Number of packets: 1234'
  btmon text ok: 'Event type: Non connectable undirected - ADV_NONCONN_IND (0x03)'
[5] markdown: 8 generated blocks in sync; 28 command headings agree with module opcodes.
SELFTEST PASSED  (538 assertions)
```
<!-- END:selftest -->

BlueZ decode excerpts that corroborate the channel numbering and field layouts (from `recon/_xcheck/ble_constants_crosscheck.btmon.txt`):
`LE Transmitter Test (0x08|0x001e) plen 3: TX frequency: 2440 MHz (0x13), Test data length: 37 bytes` ·
`LE Enhanced Transmit.. (0x08|0x0034) plen 4: TX channel frequency: 2402 MHz (0x00), PHY: LE 2M (0x02)` ·
`LE Receiver Test (0x08|0x001d) plen 1: RX frequency: 2480 MHz (0x27)` ·
`LE Set Advertising P.. (0x08|0x0006) plen 15: Min advertising interval: 1280.000 msec (0x0800), Channel map: 37, 38, 39 (0x07)` ·
`LE Set Extended Adv.. (0x08|0x0036) plen 25: Properties 0x0013: Connectable, Scannable, Use legacy advertising PDUs: ADV_IND` ·
`LE Extended Advertising Report (0x0d): SID: no ADI field (0xff), Secondary PHY: No packets, Legacy PDU Type: ADV_IND (0x0013)`.

## 7. Confidence and open items

| Item | Confidence | How to close |
|:--|:--|:--|
| TX_Channel/RX_Channel = RF channel k (0..0x27) | very high | spec + btmon decode; confirm on air with hci0 TX / hci1 RX `Num_Packets` or HackRF |
| All opcodes (36 commands) | very high (26 against the kernel header, all 36 against btmon's OGF/OCF decode) | `Read Local Supported Commands` bitmap on the BE200 for support |
| Command/event byte layouts | high; btmon decoded every layout except TX v3/v4 (btmon limitation) | - |
| DTM packet interval formula, 1M | high (anchored by 37 octets -> 625 us) | measure |
| DTM packet interval formula, 2M | medium-high | measure |
| Intel BE200 firmware honours: Test_Data_Length up to 255, PHY 2M, v4 power, concurrent tests on 8 controllers | unknown | needs a (non-read-only) test task |
| Which processes (bluetoothd) block raw HCI test commands on `hciN` and whether HCI_CHANNEL_USER (needs controller down) is required | unknown | test task; `btmon` will show every command |
| Co-channel C/I ~21 dB and adjacent-channel numbers, DTM whitening off, header details | medium-high / high | check Core Vol 6 Part A and Part F text before quoting externally |
| Error code returned when random address missing at enable (0x12 vs 0x0C) | medium | try on the controller |
| Legacy minimum interval accepted by the BE200 firmware (20 ms vs 100 ms) | unknown (BT 5.4 part, should be 20 ms) | `LE Set Advertising Parameters` with 0x0020 |
