# A2DP link delay and jitter

The A2DP stream test reports how long each audio packet needed to cross the Bluetooth link, and how much that time varied from one
packet to the next. Read this page before quoting a number: it says exactly what is measured, what is not, how it was checked and
how far it can be trusted.

## What is measured

| Figure | Meaning | Where it comes from |
|---|---|---|
| **Link delay** | Time one A2DP media packet (one AVDTP/RTP packet = one L2CAP frame) needed: from the moment its first ACL fragment is handed to the controller until the controller reports its last fragment completed. min / avg / p50 / p95 / p99 / max over the whole run, and avg / p95 / max per sample interval. | The kernel's HCI monitor channel: every ACL packet sent and every "Number Of Completed Packets" event, stamped by the kernel in nanoseconds (`SO_TIMESTAMPNS`). |
| **Link jitter** | `abs(delay[i] - delay[i-1])` for consecutive packets: its mean, 95th percentile and maximum (RFC 3393 IPDV, absolute value), and the RFC 3550 smoothed estimator. | Derived from the delays above. |
| **Loopback one-way delay and loss** (internal receiver only) | The same packet seen leaving the sender's controller and arriving at the receiver's host, matched by RTP (SSRC, sequence number); one kernel clock for both radios. Packets never received are counted lost; duplicates and out-of-order packets are counted. | A second monitor on the receiver radio. |
| **Sender-side check** (internal receiver only) | `completion - arrival` per packet: how far the sender-side delay is from the true delivery time. | Both of the above. |
| **Sink-reported delay** | What the sink itself claims its audio delay is (AVDTP delay report, through BlueZ). **Reported, not measured**; many sinks do not report it. | `org.bluez.MediaTransport1.Delay` |
| **Stream latency setting** | The audio buffer size BluForge asked the audio server for (`--latency`, default 100 ms). **A setting, not a measurement.** | The command line. |

## What it is not
* **It is not the audio latency a listener hears.** That also includes the host's audio buffer and encoder, and the sink's own
  buffer, decoder and DAC. None of those can be seen from the host. Quote "link delay", never "latency".
* **It is not measured at the sink.** For a real sink only the sending side is visible. Whether the sink's buffer ran dry
  (an audible dropout) is not observable; the sender-side xrun counter and the controller's flush events are the only hints.
* **Loss can only be counted with an internal receiver** (both ends in the same host). For a real sink, "lost" is not observable;
  a flush event (the controller dropped audio data) is reported when it happens.
* **A perfect link has jitter, and the baseline depends on the codec.** BR/EDR sends in 625 microsecond slots, and one audio packet
  needs several of them, so the delay of a clean link moves between about 7 and 18 ms. The mean jitter of an idle radio pair was
  2.3 ms with AAC (676-byte packets every 21 ms) and 5.2 ms with SBC (612-byte packets every 14.5 ms), repeatable to about 0.1 ms
  (section "Checked on hardware"). Judge a DUT against a known-good baseline of the same codec, not against zero.
* **Other uses of the radio count.** The Classic link shares its radio with an LE scan, LE advertising, an LE connection attempt and
  a Bluetooth inquiry. An LE scan raised the mean jitter of an idle link from 2.3 to 2.9-3.7 ms for as long as it ran (and BlueZ's
  LE connection scan, left on for a whole stream, to 3.7 ms with worst packets of 40-100 ms instead of 18). The meter therefore
  records what else the radio did during the run, and the result warns when it was busy with it (see "Using it").

## How it is measured
1. The meter reads every monitor frame of the radio, with the kernel's timestamp. Frames without one (older kernels) are stamped when
   read and the result says so (`clock`, and `valid: false` below 99 % kernel-stamped frames).
2. ACL fragments are put back together into L2CAP frames (start fragment carries the frame length; continuation fragments follow).
3. Transmitted fragments wait in a per-connection FIFO; each completion event releases them in order. When the last fragment of a
   frame is released, `delay = completion time - time its first fragment was sent`.
4. Delays are kept per connection and L2CAP channel. The **media channel** is the busiest one (at least 20 frames and 5 % of the
   busiest channel's bytes), so signalling and control traffic never count. The same stream on a new connection after a reconnect
   is merged into one measurement.
5. Whole-run figures come from a histogram (0.1 ms bins up to 1 s, 1 ms bins above) with exact count, sum, minimum and maximum, so a
   run of any length uses constant memory. Percentiles are nearest rank; they are accurate to 0.1 ms.
6. **Loopback matching survives stream restarts.** PulseAudio and PipeWire use SSRC 1 and start the sequence number at 0 again when
   a stream restarts, so an identity repeats within a run. Sends and arrivals are kept as ordered queues per identity and paired
   oldest with oldest; a send older than the 5 s timeout when an arrival comes is a lost packet. Matches are applied in arrival order, so
   the result does not depend on how often the run polls (the same capture gives identical figures polled every 200 frames or once
   at the end). A packet is "out of order" when it was sent before one that had already arrived, judged by send time, so a stream
   restart (sequence numbers back to 0, even after a session of a few packets) is not reordering. (Captures of such restarts on
   resource 1.2 are part of the tests.)
7. **What else the radio did.** The meter also reads the host's commands to the controller and their answers: LE scan enable (legacy
   and extended), LE advertising enable, LE connection create and cancel (and its completion), BR/EDR inquiry (and its completion).
   A command the controller refused is not counted. A scan already running when the meter started is counted from the start once its
   disable is seen; the number of LE advertising reports received is reported too, as proof that a scan is running.
8. **Trust flags.** The delay is marked not trustworthy (`valid: false`, with the reason) when the throughput integrity checks fail
   (dropped monitor frames, impossible backlog), when kernel timestamps are missing, or when completions give a negative delay.
   A limit that cannot be checked fails the run; it is never a silent pass.

## Checked on hardware (Intel BE200 radio pair, AAC, 2026-11-22)
| Run | Packets | Link delay avg / p50 / p95 / p99 / max (ms) | Jitter avg (ms) | Lost |
|---|---|---|---|---|
| Idle link, 30 s, sender side | 1417 | 13.13 / 13.15 / 17.75 / 18.15 / 19.19 | 2.34 | n/a |
| Same run, true one-way (loopback) | 1418 | 12.69 / 12.55 / 17.45 / 18.95 / 19.83 | 2.37 | 0 |
| 95 s, DTM sweep and Wi-Fi-channel interferer on a nearby radio | 4495 | 13.11 / 13.05 / 17.65 / 18.15 / **21.46** | 2.33 | 0 |
| 70 s, six radios running full-band DTM interference at maximum power | 3314 | 13.15 / 13.15 / 17.65 / 18.25 / **27.10** | 2.40 | 0 |
| 20 s, BlueZ's LE connection scan (60 ms interval and window) on the same radio, six runs | 940 each | 13.0 / 12.9 / 17.7 / 18.1 / **41-100** | **3.7** | 0 |
| The same six runs without that scan (generic connect), three more with the corrected connect | 945 each | 13.3 / 13.2 / 17.7 / 18.3 / 18-27 | 2.3 | 0 |
| 36 s, an extended LE scan (10 ms interval and window) injected for 12 s of an idle run | 1515 | 13.0 / 13.1 / 17.6 / 18.1 / 23.1 | 2.8 over the run; 2.9-3.7 in the intervals it ran, 2.3 after | 0 |
| SBC from PulseAudio, resource 1.2, 734 packets (replayed capture) | 734 | 12.3 / 11.6 / 16.8 / 17.6 / 33.2 | 5.3 | 0 (true one-way 13.1 avg) |

* **The sender-side figure agrees with the true one-way delay.** Per packet, `completion - arrival` had a median of +0.1 to +0.5 ms,
  95 % within about 1.5 ms, worst cases -3.9 ms (completion reported before the receiver had the packet) and +9.8 ms (one packet in
  3314, an acknowledgement lost and repeated). The relationship shifts by up to about 1 ms with the USB polling phase of the two
  controllers, so quote the delay to the millisecond, not to a tenth. This agreement was established for the BE200 only; re-run the
  loopback check (a normal A2DP test with an internal receiver reports it) before relying on another controller model.
* **The measurement responds to impairment.** Under six full-band interferers the worst packet took 27 ms instead of 19 ms and the
  jitter rose in the affected intervals (2.8 against 2.3 ms), and the loopback saw the same worst case (27.3 ms). The link itself was
  not broken by it: the Classic hop map shrank from 79 to 20-39 channels (AFH), nothing was lost, no disconnects.
* The meter's per-packet bookkeeping is pinned by `tests/test_delay.py` against a real capture (`tests/data/a2dp_loopback.jsonl`).

## Using it
**GUI** (Bluetooth Classic tab, "A2DP stream test"): *max link delay p99* and *max jitter* are optional PASS limits. The live metrics show
the delay (avg / p95 / max) and jitter of the last interval, two charts follow them, and the result box ends with the figures above,
each labelled with what it is. With an internal receiver the packets lost are shown live.

**Command line**
```
bluforge_classic.py --hci 6 a2dp <addr> -t 60 --max-delay-ms 30 --max-jitter-ms 5
```
`--max-delay-ms` is checked against the **99th percentile** of the link delay, `--max-jitter-ms` against the **mean** jitter. Neither is
judged unless given. The interference stream (`load`) and traffic jobs report but never judge.

**Result file** (`logs/classic/*_a2dp_*.json`, field `summary`; schema unchanged, fields added)
* `link_delay`: `available`, `basis` (the definition in words), `clock`, `valid`, `notes`, `delay_ms` {n, min, avg, p50, p95, p99, max},
  `jitter_ms` {avg, p95, max, rfc3550, n}, `late` (packets over the delay limit), `channels`, `bytes`, `flush_events`.
* `loopback` (internal receiver only): `packets_sent`, `matched`, `lost`, `loss_pct`, `in_flight_excluded`, `duplicates`, `reordered`,
  `delay_ms`, `jitter_ms`, `completion_minus_arrival_ms`.
* `link_delay.coexistence`: `window_s`, `le_reports`, and for `le_scan`, `le_adv`, `le_connect`, `inquiry` the `enabled_s`, `enabled_pct`
  and `starts`; `warnings` lists those that disturb a Classic link (an LE scan or LE connection attempt on for 10 % of the run or more,
  an inquiry for 5 % or more). When a delay or jitter limit fails, the warnings are added to the failure reasons as notes: the failure
  may not be the device's.
* `stream_control`: the AVDTP commands that start, stop or reshape the stream, as `counts` per origin (`host_start`, `remote_suspend`,
  `remote_delayreport`, ...), the first 50 `events` with their time, and for a stream that was started: `stream_window_s`, `suspended_s` and
  `suspended_pct`, the exact time the other end held it suspended (from each remote SUSPEND to the host's next START). A receiver
  that suspends the stream is named in the failure when the active share is too low, and `active_pct` is the worse of the sampled
  transport state and `100 - suspended_pct` (`active_pct_sampled` keeps the sampled one). `pulse_sink_resumes`: how often the PulseAudio
  keeper had to resume the sink (PulseAudio interference runs only).
* `sink_reported_delay_ms` (what the sink says its delay is: BlueZ's `Delay`, else the AVDTP DELAYREPORT seen on the air; reported, not
  measured), `stream_latency_setting_ms`, `acl_integrity.timing`, and the two limits under `thresholds`.
* CSV columns per sample interval: `delay_n`, `delay_avg_ms`, `delay_p95_ms`, `delay_max_ms`, `jitter_ms`, and with an internal receiver
  `lb_delay_avg_ms`, `lb_delay_p95_ms`, `lb_lost`.

## What was not tested
* **Host load.** The kernel stamps the frames, so this program's own scheduling does not matter, but a host so busy that the kernel's
  USB and Bluetooth processing is delayed would inflate the figures. Every run above was made on units that were in use (a LANforge
  throughput test on the manager, Android mirroring on the resource, load average about 3 on four cores) and no such effect was
  seen; a heavier load was not tried because the units were not free to be saturated.
* **Controllers other than the Intel BE200**, and **real sinks** (earbuds, speakers): none was available. The sender-side figure is the
  only one a real sink allows; its agreement with the true delay was established on the BE200 pair only.
* **PulseAudio units.** The measurement works there (SBC capture above). The A2DP test itself is refused on PulseAudio (it cannot
  count underruns); the interference stream (`load`) runs. With an internal receiver the receiving radio suspends the stream every ~5 s
  (see `REALISTIC_INTERFERENCE.md`, "Sound servers"); the interference stream resumes the sink each time, the delay figures cover the
  packets that were sent, and the result reports the suspended time (`stream_control`).

## Using it to qualify a device
1. Run the test against a known-good device of the same kind, in the same place, and keep the result file as the baseline.
2. Set the limits a margin above the baseline (for example p99 + 30 %), not at the baseline.
3. Move or disturb the setup as the real use requires (distance, an attenuator, interference from the Interference tab) and compare.
4. A run that shows `valid: false`, or "not measured", is not a pass: fix the cause first.

## Code
`src/delay_stats.py` (histograms, jitter), `src/acl_meter.py` (`AclMeter` timing, `LinkPair`), `job_a2dp` / `link_delay_block` /
`delay_failures` in `src/bluforge_classic.py`, the Classic tab in `src/static/classic.js`. Tests: `tests/test_delay.py`.
