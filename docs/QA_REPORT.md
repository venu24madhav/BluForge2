# BluForge 1.3.0: QA report (2026-11-22)

Scope of this pass: the cluster deployment (manager 1.1 + resource 1.2), the BLE Sniff capture feature, an audit of what the
Classic metrics really measure, and the new A2DP link delay / jitter measurement (`A2DP_DELAY.md`). Everything marked **verified** was run on the real units; nothing here is inferred from reading code
alone unless it says so. Not covered: any test against a real third-party earbud, headset, phone or car kit (none was available).

## 1. Verified on hardware

| # | Check | Result |
|---|---|---|
| 1 | Install on the resource (Fedora 39, 2 x Intel BE200 USB) with the offline package | service up, `/api/status` shows radios 0,1 |
| 2 | Install on the manager (8 x BE200) | service up, listens on 0.0.0.0:5480 |
| 3 | Manager lists the resource's radios | `all_hci` = 0..7 and 100,101 (`1.2.hci0`, `1.2.hci1`), health ok |
| 4 | Start an advertisement on a resource radio from the manager | job `1.2:job1` runs on the resource, appears in the manager's list |
| 5 | Start a BLE Sniff with capture on a resource radio from the manager, download it through the manager | file byte-identical to the file on the resource (md5 equal), 432 packets, decodes in tshark |
| 6 | Restart the manager's service while a job runs on the resource | job keeps running and reappears (jobs belong to the resource) |
| 7 | Stop the resource's service while its job runs | job ends, radio handed back (`UP RUNNING PSCAN`), manager shows a warning and hides the resource, resource returns by itself within one scan interval after restart |
| 8 | `tests/hw_loopback.py` (advertise a known iBeacon on one radio, survey on the other) on the manager (hci2->hci3) and on the resource (hci0->hci1) | 8/8 PASS on both |
| 9 | Classic A2DP test, internal receiver, manager hci6 -> hci7, AAC, 40 s | PASS; 253 kbit/s ACL (matches AAC bitrate), meter integrity ok (8560 frames, 0 unmatched completions, 0 truncated), AFH 50-51 channels, 0 disconnects |
| 10 | Full hardware-free suite | 217 / 217 on 1.2.0; **348 / 348 on 1.3.0**, on the manager and on resource 1.2 |
| 11 | A2DP link delay and jitter, idle radio pair, AAC, 30 s | 1417 packets: delay avg 13.1, p50 13.2, p99 18.2, max 19.2 ms; jitter 2.3 ms; the same run seen from the receiving radio (true one-way delay) 12.7 ms avg, 0 of 1418 packets lost |
| 12 | The sender-side delay against the receiver's own timestamp of the same packet (1280 and 3314 packets) | median difference +0.1 to +0.5 ms, 95 % within 1.5 ms; extremes -3.9 and +9.8 ms |
| 13 | The measurement under impairment: DTM sweep and Wi-Fi-channel interferer; then six radios running full-band DTM at maximum power next to the pair | the worst packet rose from 19 to 21 ms and then 27 ms, jitter rose in the affected intervals; the sender-side and receiver-side figures agreed (27.1 / 27.3 ms); no loss, no disconnect, the hop map shrank from 79 to 20-39 channels |
| 14 | Delay and jitter in the Bluetooth Classic tab of a live GUI, with a real job | live delay (avg / p95 / max), jitter and loopback loss cells, two charts, console lines, limit fields: all populated from a real run (headless Firefox screenshot) |
| 15 | A2DP connect time on a dual-mode device, three runs in a row | 0.7 / 1.4 / 1.7 s (before the fix below: 16 s each time) |
| 16 | The installed manager service, through its API, with limits (`max_delay_ms` 30, `max_jitter_ms` 6) | PASS: delay p50 / p99 / max 13.3 / 18.3 / 22.2 ms, jitter 2.3 ms, 1217 of 1217 packets received, tool version 1.3.0 |
| 17 | Delay measurement on resource 1.2 (PulseAudio, SBC), replayed capture and live | measurement works (734 of 734 packets matched across a stream restart); its Classic stream stops after about 5 s, see F8 |
| 18 | Who suspends the stream on resource 1.2: AVDTP signalling decoded from the monitor, then PulseAudio and BlueZ state sampled every second | the receiving radio sends SUSPEND 5.7 s after START; with the sink resumed on suspend the interference stream passes (90.5 % active, exact), see F8 |
| 19 | Manager (PipeWire) after the change, through the service API | PASS, no remote suspend, suspended 0 %, sink-reported delay 0.0 ms (AVDTP DELAYREPORT) |

## 2. Defects found and fixed
* **Server ignored two Classic settings.** `build_classic_argv` did not pass `latency` (A2DP, interference stream) or
  `min_active_pct` (interference stream, traffic) to the engine, so a value sent through the API was silently dropped and a bad value
  was not refused. This is what the four long-standing test failures in `test_traffic.Arguments` and `test_classic_load.LoadArguments`
  were reporting. Fixed in `src/server.py` (values are range-checked: latency 10..2000 ms, min active 0..100 %). 217/217 now pass.

* **Four Classic connection defects found and fixed while building the delay measurement.**
  1. *The BR/EDR fallback could not work.* When BlueZ tried the LE bearer for a dual-mode device (the internal receiver, and many
     earbuds), `Device.Connect` timed out, and the existing fallback (`ConnectProfile`) was then refused with "Operation already in
     progress" because the LE attempt was still pending inside bluetoothd. The test failed to connect, repeatably. The fallback now
     cancels the pending attempt (`Disconnect`) first; by hand that sequence connected in 0.65 s where the old one failed.
  2. *Every audio test against such a device started with a 15 s wait.* The connect time reported (`setup_ms`, the cycling test's
     `connect_ms`) then measured BlueZ's choice of bearer, not the device. The A2DP and cycling tests now name the A2DP sink profile
     first when the device offers it, which forces BR/EDR at once (16 s down to 0.7-1.7 s, three runs in a row); anything else, and
     any refusal, takes the generic path as before. Covered by tests (`ConnectFallback`).
  3. *The sound server could be chosen wrongly, failing about every second run.* When the card for the device was not yet in the
     PipeWire graph, `card_for` asked `pactl`; on PipeWire's compatibility server the card appeared between the two queries and the
     run was refused as "handled by PulseAudio". It surfaced once the connect got faster (the card then does not exist yet when
     polling starts). `pactl` is now consulted only when the pulse socket is genuine PulseAudio (`Server Name`), and the choice is
     asked once (`SoundServerChoice` tests).
  4. *Forcing BR/EDR must not be followed by a plain Connect.* The first version of the faster connect did follow it, to bring up
     the remaining profiles. On a dual-mode device that made BlueZ start an LE connection scan (60 ms interval and window) that stayed on for
     the whole stream: mean jitter 2.3 -> 3.7 ms and the worst audio packet 18 -> 40-100 ms on an idle radio pair, in every run.
     Found only because the new measurement showed it; the extra Connect is gone (AVRCP still comes up by itself) and the meter now
     reports and warns about any LE scan, LE connection attempt or inquiry on the radio during a run.

## 3. Findings that need a decision before this is sold (not fixed)

**F1. (Partly resolved in 1.3.0.) The product now measures A2DP link delay and jitter, but not the audio latency a listener hears.**
The A2DP test reports, besides throughput, underruns, disconnects, RSSI and hop channels, the delay of every audio packet across the
link as seen by the host, its jitter, and with an internal receiver the true one-way delay and loss (`A2DP_DELAY.md`; checked
against the receiver's own timestamps and under interference). Still missing: the delay a listener experiences, which also contains the
host's audio buffer and the sink's own buffer and decoder, and any judgement of audio quality. The `--latency` option remains a
*buffer size handed to the audio player*, labelled as a setting in the result. Do not call the new figures "latency": call them link
delay. Measuring the listener's delay needs the sink's output captured at the host (a line-in or microphone) against a known signal;
see section 5.

**F2. Classic RSSI and link quality look constant and implausible.** In the A2DP run RSSI was -84 and link quality 255 in all 8
samples, with the two radios centimetres apart. The field is documented as an offset from the controller's "golden receive range"
(0 = in range), so -84 should mean far out of range, yet the link was perfect. Until it is checked against a known attenuation, treat
RSSI and link quality on Classic links as **unverified**; the pass/fail thresholds `min_rssi` is built on them.

**F3. "Throughput" in the A2DP test is the codec's bitrate, not the link's capacity.** AAC streamed at 253 kbit/s because that is
what the encoder produces; the number is exact (counted) but says nothing about how much more the link could carry. The traffic
tests (file, video) do saturate the link and are the ones to use for capacity.

**F4. HFP is CVSD only (no mSBC).** Current earbuds and phones normally negotiate wideband (mSBC). A call test with such a device
exercises the narrowband fallback, not what users hear. HFP and AVRCP results are protocol checks (connection, SCO link set up,
key events seen); no audio-path or quality metric is produced for HFP.

**F5. No BLE connected-mode traffic.** BLE features are advertising, Direct Test Mode (stress), the sniffer and the Classic tests.
BluForge cannot create a BLE connection and move data over it (GATT notifications, L2CAP credit-based channels, connection-event
timing). So "real BLE traffic" today means advertising only.

**F6. Network exposure.** No login, plain HTTP, now listening on all interfaces of the manager (192.168.206.99) and the resource
(192.168.204.24). Anyone who can reach port 5480 can drive the radios and download captures, which contain nearby people's
advertisements. Acceptable on an isolated lab network; for a customer deliverable decide on a login, a firewall rule or TLS.

**F7. Unbounded growth.** Result files and captures are never deleted. A capture stops at 512 MB, but many captures fill the disk.
The resource has 2.5 GB free (95 % used). The health banner warns on low space; nothing cleans up.

**F8. (Understood and mitigated in 1.3.0.) Resource 1.2 (PulseAudio): the Classic stream stopped after about 5 s.** Cause, found by
decoding the AVDTP signalling of the radio pair: the *receiving* radio (hci1) sends AVDTP SUSPEND about 5.7 s after every START. PulseAudio
does not treat that radio's device as an audio device (its A2DP profiles read "available: no"), so nothing takes the incoming stream and
BlueZ on the receiving side suspends it; the sender's sink is suspended with it. It happened with the original 1.1.0 code and with a
plain `pacat`, so it is the combination "internal receiver on PulseAudio", not a regression. It does not affect PipeWire (the manager) or
a real speaker. Mitigation: the interference stream resumes the sink within about half a second whenever PulseAudio suspends it
(`PulseSinkKeeper`); on the resource that took the active share from 0-25 % (run flagged) to 90.5 % (suspended 4.6 s of 48 s, 8 resumes,
2341 of 2342 packets accounted for, run passes), and the result reports every suspend (`stream_control`, `pulse_sink_resumes`). The
A2DP test on PulseAudio stays refused by design (no underrun counting). Also found on that unit: the stored pairing of its two radios was
stale (bluetoothd "avdtp_connect_cb: Permission denied") and `link-local` said "already paired"; it had to be removed and redone.
Remaining decision: the documented prerequisite is PipeWire; run Classic audio tests on PipeWire units where the gaps matter.

**F9. The delay figures are sensitive to what else the radio does.** An LE scan raised the idle-link jitter by 25-60 % while it ran.
This is real for the Classic link, but a customer comparing two devices must not let one run include a scan and the other not; the result
now says so (`link_delay.coexistence`).

## 4. About realism, as measured here
* Radios inside one unit are centimetres apart. On the resource, hci0 advertising and hci1 listening gave RSSI of -13 to -17 dBm (about as
  strong as Bluetooth ever gets); the same room's other devices were -42 to -84 dBm. So same-unit traffic is real radio traffic, but there is
  no distance, wall or fading between its two ends. It is a faithful *interference source* and a fair *protocol* test; it is not a model
  of a link at range or in motion.
* Across units there is real distance: with the resource advertising at the default power, the manager's radios did not hear it at all
  in a 15 s survey while hearing neighbours at -73 to -93 dBm. A cluster therefore gives genuine geometry, and the unit placement is a
  test variable you control.
* Realistic outcomes need a real device (earbud, phone, car kit) as one end, placed where the customer's product would be, and, for
  repeatable range, an attenuator or a shielded box between it and the unit's radios.

## 5. Recommended work, in order
1. Audio latency a listener hears (the link delay is done in 1.3.0): play a known marker signal, capture the sink's output at the
   host (line-in or microphone), correlate, and report the delay and its variation with the capture device's own delay stated.
   Needs a capture input and a real sink to validate; the link-delay meter above does not.
2. Validate or remove Classic RSSI and link quality (sweep an attenuator, plot the reading).
3. BLE connected-mode traffic between two radios (and to a real device): connection interval, throughput, latency, retransmissions.
4. HFP wideband (mSBC) and an audio-path measurement for calls.
5. Optional login or TLS, and a retention setting for results and captures.
6. A DUT qualification run per profile on the customer's real devices, with the result files archived as evidence.
