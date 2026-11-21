# Realistic Bluetooth interference (live Classic traffic: A2DP audio, file transfer, video, HID)

Direct Test Mode (the Interference tab's single / sweep / hop / wifi / band modes) is **stress**, not realism: it
saturates BLE channels with back-to-back test packets, which no real Bluetooth device does. BluForge therefore has
two kinds of interference:

| Kind | What it is | Where |
|---|---|---|
| **Realistic, Classic audio** | A live A2DP audio stream from a Classic radio to a real speaker/earbuds: real BR/EDR hopping (1,600 hops/s, up to 79 channels, adaptive hopping), real ACL packets | Interference tab, first panel, traffic type *Audio stream* (job kind `c_load`, CLI `bluforge_classic.py --hci N load ADDR`) |
| **Realistic, Classic data** | Real RFCOMM/L2CAP data links shaped like a file transfer, a video download or keyboard/mouse reports | Interference tab, first panel, other traffic types (job kind `c_traffic`, CLI `bluforge_classic.py --hci N traffic ADDR --kind K`) |
| **Realistic, BLE** | Genuine advertising beacons at a chosen interval and TX power | Interference tab, mode `adv-flood` |
| **Stress** | Direct Test Mode test packets: worst-case, repeatable, not realistic | Interference tab, other modes |

Use them together on different radios: e.g. `adv-flood` on one radio (a crowd of BLE devices) plus a Classic stream on
another (a live audio link), with a DTM sweep on a third when you want the worst case.

## Using the Classic stream
1. Pair the speaker/earbuds once on the **Bluetooth Classic** tab.
2. Interference tab: pick the Classic radio, press **Reserve radio & list devices**, pick the paired device.
3. Choose source (white noise = heaviest for variable-rate codecs; or a music file), codec, duration (0 = until
   stopped), and press **Start Realistic Classic Interference**. The live line shows ACL kbit/s, hop channels in use,
   and the share of time the stream was running; the Classic tab shows the full charts.

CLI: `python3 bluforge_classic.py --hci 1 load AA:BB:CC:DD:EE:FF -t 3600 --source noise --codec sbc_xq`

## Data traffic types (`traffic`)
Audio is only one thing Classic radios do. The `traffic` job runs real data links, shaped like the application they
stand for, so the 2.4 GHz band sees the airtime pattern of that application:

| Type (`--kind`) | Link | Shape | Load on the air |
|---|---|---|---|
| `file` File transfer | RFCOMM, OBEX Object Push | files of `--file-mb` MB (default 2) back to back, 2 s apart; OBEX packets of 16 KB with an acknowledgement each, like a phone sending photos | saturates the link, about 1.3 Mbit/s measured |
| `video` Video download | L2CAP | three segments of buffering up front, then one 4 s segment every 4 s at `--video-kbps` (default 1000); a segment that takes longer than 4 s is counted as late | bursts, about 1 Mbit/s average, 1.4 Mbit/s while a segment is loading |
| `hid` Keyboard / mouse | L2CAP | 8 bytes every 8 ms (125 per second) | a few kbit/s, but constant small packets |

* **Receiver.** The data types need **This unit: hciN (internal receiver)**: the other radio runs the receiving end
  (an OBEX server, or a sink), because no real speaker or phone accepts raw video or HID reports. A real paired
  device can receive **file transfer** only, and only if it offers OBEX Object Push (found over SDP); phones usually ask
  the user to accept each file.
* **Pairing.** Raw data links need the two radios paired (the first start does it, like audio). If the kernel refuses a
  link because the stored pairing is stale (BlueZ can list both radios as paired while the kernel holds no key for one),
  the job re-pairs the two radios once by itself and carries on.
* **No audio stack involved.** No PulseAudio/PipeWire loop to guard against, and it works whichever sound server owns
  Bluetooth audio. When the job ends it drops the link between the two radios.
* **What is reported.** Delivered application kbit/s (counted at the receiving end), exact ACL kbit/s in both directions
  (a download arrives at the sending radio, an upload leaves it), hop channels in use (AFH), the share of time the
  links were up, drops and reconnects, and per stream: files completed with the speed of each, segments and late
  segments. Result files are `logs/classic/*_traffic_*.json/.csv`, test name `traffic`.
* **Never gives up.** Like `load`: a lost link is reconnected every 5 s. The run is flagged if the links were up less
  than the threshold (default 80 %) of the time **since they first came up**; links that never come up score 0 %.
* **Combining.** Use one radio pair per traffic type. Several pairs at once, plus an advertising flood on another radio,
  is the busiest realistic room this unit can produce.
* **Removed types.** Web browsing, serial data (SPP) and a mixed room were built and measured over the air, then taken
  out on 3 Nov 2026 by request, not because of a fault: web and mixed only imitated a traffic pattern (no PAN or HTTP),
  and serial data was not a profile test of its own.

CLI: `python3 bluforge_classic.py --hci 0 traffic 80:84:89:08:E3:EF --kind video -t 600 --video-kbps 1200`

## What "load" mode does differently from the A2DP *test*
* Never gives up: a dropped link or failed reconnect is retried every 5 s until the duration ends or Stop is pressed.
* Does not judge the device (xruns, disconnects and RSSI are reported, never failed).
* Reports how much interference was **delivered**: ACL kbit/s (exact, from the HCI monitor), the number of Classic hop
  channels in use (AFH map, `hcitool afh`), reconnects and failed attempts, and the share of the run the stream was
  active. Outage time counts against that share. The run is **flagged** (result `pass=false`) if the share is below
  the threshold (default 80 %), because the effect on a Wi-Fi measurement would then be understated.
* Result files are saved like other Classic results (`logs/classic/*_load_*.json/.csv`), test name `load`.

## The receiver: a real device, or another radio of this unit
The stream needs something to receive it. In the **Streaming device** list, choose either a paired speaker/earbuds or
**This unit: hciN (internal receiver)**. The internal receiver needs no external device: the sender radio streams A2DP
over the air to another radio of the same unit.

* First use: the two radios pair with each other automatically (about 6 s) and trust each other. After that it just
  connects. `bluforge_classic.py --hci S link-local R` does this step on its own.
* The receiver radio is reserved for Classic (no BLE jobs on it) until you release it on the Bluetooth Classic tab,
  and the sender is too. The job disconnects the pair when it ends.
* The radios sit side by side, so the interference comes from the unit itself. Use a separate speaker when it must
  come from a spot you choose.
* **Feedback loop guard (PulseAudio).** PulseAudio loops every Bluetooth source it sees into the default sink. With an
  internal receiver that sink is the one being streamed into, so audio would circle between the two radios forever
  (seen on the bench: traffic kept flowing after the job stopped until the link was disconnected). The job parks those
  loopbacks on a silent sink (`bluforge_drain`) before streaming and keeps checking every second. If it cannot (needs
  `pactl --format=json`, PulseAudio 15 or newer) it disconnects and refuses to stream. Only loopbacks fed by one of
  this unit's own radios are touched.

## Sound servers
Bluetooth audio can be owned by PipeWire (WirePlumber) or by real PulseAudio. The engine now uses whichever one holds
the device's audio card (PipeWire first, then PulseAudio). On this unit the desktop user's PulseAudio owns it, so the
PipeWire-only engine found "no audio card" for any device. Differences on PulseAudio:
* the realistic stream (`load`) works; **the A2DP test is refused** because audio underruns are counted with `pw-top`
  (PipeWire only) and PulseAudio cannot report them. Results record `sound_server` and `xruns_measured`.
* right after a fresh pairing BlueZ's own profile auto-connect can lose a race ("Device or resource busy") and the
  card never appears; the job now does one clean disconnect/reconnect before giving up.
* **An internal receiver on PulseAudio is suspended by its own receiving radio about every 5 s** (measured on resource 1.2, PulseAudio
  16.1, 22 Nov 2026). PulseAudio does not treat the receiving radio's device as an audio device (its A2DP profiles read "available: no"),
  so nothing takes the incoming stream, and BlueZ on the receiving radio sends AVDTP SUSPEND 5 s after every START. The sender's sink
  is suspended with it. Without a remedy the stream was active 0-25 % of the run, with the original 1.1.0 code and with a plain `pacat`
  alike. The interference stream now resumes the sink within about half a second whenever PulseAudio suspends it
  (`PulseSinkKeeper`), which kept the stream suspended 9.5 % of the time (4.6 s of 48 s, 8 resumes). The result says so:
  `pulse_sink_resumes`, and `stream_control` with each SUSPEND and START and the exact suspended time. PipeWire units (the manager) are
  not affected, and neither is a real speaker as the receiver. If the gaps matter, use PipeWire or a speaker.

## Limits to state honestly
* **Load level.** One A2DP stream is a modest interferer: SBC measured about 337 kbit/s of ACL traffic between the two
  radios, and AAC to HTWS006 about 140 kbit/s (22 Oct 2026), a small share of the link's 1-2 Mbit/s capacity. For more
  airtime use a higher-bitrate codec (SBC-XQ, aptX HD, LDAC) and/or more Classic radios, each streaming to its own
  receiver. Read the delivered kbit/s in the live line instead of assuming a level.
* **Adaptive hopping.** A Classic link that sees busy channels hops around them (AFH), so it interferes less with a
  Wi-Fi channel it can hear than a fixed channel would. On the bench the link used only 20 of 79 channels (the AFH
  minimum). That is realistic, but it is why the hop-channel count is reported. DTM does not adapt.
* **Interference level at your device under test depends on geometry** (radio and receiver positions). It is not
  calibrated: keep the layout fixed between runs and compare runs relative to each other.
* The Classic radios stay under BlueZ while reserved.
* **Verified on hardware (2 Nov 2026, this unit, two BE200 radios, PulseAudio 16.1):** first-use pairing, internal
  receiver stream through the GUI (about 337 kbit/s, SBC, 20 of 79 hop channels read live with `hcitool afh`, 99.7 %
  streaming over 41.6 s, 0 reconnects), loop guard (loopback parked, traffic stopped when the job ended, audio modules
  and the silent sink removed afterwards). 137 hardware-free tests, including a fake-hardware run of the real job code.
* **Not verified:** a real speaker/earbuds as the receiver (both were off), an internal receiver on PipeWire (the
  receiver side is untested there), more than one Classic stream at a time, and runs longer than a few minutes.
* **Data traffic, verified on hardware (3 Nov 2026, same unit, hci0 to hci1, over the air, ACL meter):** each type
  was run for 60 s and compared with what its model predicts:

  | Type | Model predicts | Measured |
  |---|---|---|
  | file | 2 MB files back to back, 2 s apart | 4 files, 1.33 Mbit/s while sending, 1.14 Mbit/s average |
  | video | 1000 kbit/s, 15 segments in 60 s | 15 segments, 989 kbit/s average, none late |
  | hid | 8 B every 8 ms = 8 kbit/s | 8 kbit/s (about 123 reports/s), 12 kbit/s on the air |

  Also verified, with the mixed type that has since been removed (five streams on one link, so a harder case than any
  type left): a 10 minute run (100 % up, 0 drops, 40 AFH reads) and a link dropped on purpose at 30 s (every stream back within 4 s, drops
  counted, run still passed). Start, Stop and Stop All through the web API (link gone within 1 s, result saved, a second
  job on the same radio refused with 409, bad parameters refused with 400), a run started from the web page, and the OBEX
  client against **BlueZ's own `obexd`** (a 200 000 byte file arrived complete under the right name).
  A short video run reads higher than its bitrate (20 s at 500 kbit/s showed 594) because the player-style start-up
  buffer is fetched first; over a minute or more it settles on the bitrate.
* **Data traffic, not verified:** file transfer to a real phone or laptop. `obexd` held the first packet for about a
  minute until its agent (the desktop's Bluetooth applet) answered, as a phone does while it asks the user; the job
  then notes `the receiver took N s to answer ... waiting for someone to accept the file`. Use a receiver set to accept
  automatically if the file transfer is to load the air. Also not verified: several traffic jobs at once on different
  radio pairs (this unit has two radios, so one pair), runs longer than 10 minutes, and `video` as real PAN/BNEP and
  HTTP and `hid` as the real HID profile (both only copy the traffic pattern). The shapes are models of typical
  applications, not recordings of a particular phone or player.
