"""Link delay and jitter of an A2DP stream: the statistics, the per-packet timing taken from the kernel's HCI monitor frames,
the loopback matcher, the result fields and limits. No radio is needed: monitor frames are built by hand with explicit
timestamps, and one real capture (tests/data/a2dp_loopback.jsonl: AAC over a real radio pair, payloads cut to their headers)
pins the algorithm to what real controllers produce."""
import json
import os
import random
import struct
import types
import unittest
import _path  # noqa: F401
import acl_meter as m
import delay_stats as ds
import bluforge_classic as bc
import server
import test_classic_load as tcl

HERE = os.path.dirname(os.path.abspath(__file__))
MS = 1000000                       # nanoseconds per millisecond
T0 = 10 ** 15                      # an arbitrary kernel time


# ------------------------------------------------------------------ helpers that build monitor frames
def l2cap(cid, body):
    return struct.pack("<HH", len(body), cid) + body


def rtp(seq, ssrc=1, ts=0, pt=0xE0, payload=b"\0" * 8):
    return struct.pack(">BBHII", 0x80, pt, seq, ts, ssrc) + payload


def acl(handle, pb, data, dlen=None):
    return struct.pack("<HH", handle | (pb << 12), len(data) if dlen is None else dlen) + data


def completed(handle, count):
    return bytes([0x13, 5, 1]) + struct.pack("<HH", handle, count)


def media(x, seq, t_tx, t_done, handle=1, cid=0x41, size=600, ssrc=1):
    """One A2DP media frame sent at t_tx and reported completed at t_done (ns)."""
    body = rtp(seq, ssrc, payload=b"\0" * (size - 12))
    x.feed(m.MON_ACL_TX, acl(handle, m.PB_START_FLUSH, l2cap(cid, body)), t_tx)
    x.feed(m.MON_EVENT_PKT, completed(handle, 1), t_done)


# ------------------------------------------------------------------ statistics
class Statistics(unittest.TestCase):
    def test_percentiles_are_within_one_bin_of_the_exact_nearest_rank(self):
        rng = random.Random(3)
        vals = [rng.lognormvariate(2.5, 0.5) for _ in range(5000)]            # about 12 ms, long tail
        h = ds.Hist()
        for v in vals:
            h.add(v)
        s = sorted(vals)
        for p in (50, 90, 95, 99, 99.9):
            self.assertLessEqual(abs(h.percentile(p) - ds.percentile_exact(s, p)), 0.1, "p%s" % p)

    def test_count_sum_min_max_are_exact(self):
        h = ds.Hist()
        for v in (3.04, 5.0, 7.77, 5.5):
            h.add(v)
        self.assertEqual((h.n, h.lo, h.hi), (4, 3.04, 7.77))
        self.assertAlmostEqual(h.total, 21.31)

    def test_values_above_a_second_use_millisecond_bins(self):
        h = ds.Hist()
        h.add(1500.4)
        self.assertLessEqual(abs(h.percentile(50) - 1500.4), 1.0)
        h.add(10 ** 9)                                                         # absurd: clamped into the last bin, max stays exact
        self.assertEqual(h.hi, 10 ** 9)

    def test_negative_and_nan_delays_are_refused_not_stored(self):
        h = ds.Hist()
        for bad in (-0.001, float("nan")):
            with self.assertRaises(ValueError):
                h.add(bad)
        self.assertEqual(h.n, 0)

    def test_empty_histogram_has_no_statistics(self):
        self.assertIsNone(ds.Hist().percentile(50))
        self.assertIsNone(ds.DelayAcc().summary())
        self.assertIsNone(ds.DelayAcc().take_interval())
        self.assertIsNone(ds.percentile_exact([], 50))

    def test_nearest_rank_definition(self):
        v = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual([ds.percentile_exact(v, p) for p in (10, 50, 90, 95, 100)], [1, 5, 9, 10, 10])

    def test_signed_histogram_orders_negatives_first(self):
        h = ds.SignedHist()
        for v in (-3.0, -1.0, 0.5, 2.0):
            h.add(v)
        s = h.summary()
        self.assertEqual((s["n"], s["min"], s["max"]), (4, -3.0, 2.0))
        self.assertLessEqual(abs(h.percentile(50) - (-1.0)), 0.1)              # rank 2 of 4
        self.assertLessEqual(abs(h.percentile(100) - 2.0), 0.1)
        self.assertIsNone(ds.SignedHist().summary())


class Jitter(unittest.TestCase):
    def test_jitter_is_the_mean_absolute_difference_between_consecutive_packets(self):
        a = ds.DelayAcc()
        for v in (10.0, 12.0, 9.0, 9.0):
            a.add(v)
        j = a.summary()["jitter_ms"]
        self.assertAlmostEqual(j["avg"], (2 + 3 + 0) / 3.0, places=3)
        self.assertEqual((j["n"], j["max"]), (3, 3.0))

    def test_rfc3550_estimator(self):
        a = ds.DelayAcc()
        for v in (10.0, 12.0, 9.0, 9.0):
            a.add(v)
        j = 0.0
        for d in (2.0, 3.0, 0.0):
            j += (d - j) / 16.0
        self.assertAlmostEqual(a.summary()["jitter_ms"]["rfc3550"], round(j, 3), places=3)

    def test_a_constant_delay_has_no_jitter_and_one_packet_has_no_jitter_figure(self):
        a = ds.DelayAcc()
        for _ in range(10):
            a.add(7.0)
        self.assertEqual(a.summary()["jitter_ms"]["avg"], 0.0)
        b = ds.DelayAcc()
        b.add(7.0)
        self.assertIsNone(b.summary()["jitter_ms"])

    def test_late_packets_are_counted_exactly_against_the_limit(self):
        a = ds.DelayAcc(late_limit_ms=20.0)
        for v in (5.0, 20.0, 20.001, 35.0):
            a.add(v)
        self.assertEqual(a.summary()["late"], {"limit_ms": 20.0, "n": 2})       # strictly greater than the limit
        self.assertIsNone(ds.DelayAcc().add(1.0) or ds.DelayAcc().summary())

    def test_interval_statistics_use_the_exact_values_and_start_over(self):
        a = ds.DelayAcc()
        for v in (1.0, 2.0, 3.0, 4.0, 100.0):
            a.add(v)
        i = a.take_interval()
        self.assertEqual((i["n"], i["p50"], i["p95"], i["max"]), (5, 3.0, 100.0, 100.0))
        self.assertAlmostEqual(i["avg"], 22.0)
        self.assertIsNone(a.take_interval())                                    # nothing new since
        a.add(6.0)
        self.assertEqual(a.take_interval()["n"], 1)
        self.assertEqual(a.summary()["delay_ms"]["n"], 6)                       # the whole-run figures kept everything

    def test_interval_buffers_are_not_kept_unless_asked_for(self):
        a = ds.DelayAcc(intervals=False)
        for i in range(10000):
            a.add(1.0 + (i % 7))
        self.assertEqual((len(a.ival), len(a.idiff)), (0, 0))                   # constant memory on a long run
        self.assertIsNone(a.take_interval())
        self.assertEqual(a.summary()["delay_ms"]["n"], 10000)


# ------------------------------------------------------------------ per-packet timing in the meter
class MeterTiming(unittest.TestCase):
    def test_one_packet_delay_is_completion_minus_send(self):
        x = m.AclMeter(0)
        media(x, 1, T0, T0 + 5 * MS)
        a = x.accs[(1, 0x41)]
        self.assertEqual(a.n, 1)
        self.assertAlmostEqual(a.delay.lo, 5.0)

    def test_a_fragmented_frame_is_timed_from_its_first_fragment_to_its_last_completion(self):
        x = m.AclMeter(0)
        frame = l2cap(0x41, rtp(1, payload=b"x" * 6))                          # 4 + 12 + 6 = 22 bytes
        x.feed(m.MON_ACL_TX, acl(1, m.PB_START_FLUSH, frame[:14]), T0)
        x.feed(m.MON_ACL_TX, acl(1, m.PB_CONT, frame[14:]), T0 + 1 * MS)
        x.feed(m.MON_EVENT_PKT, completed(1, 1), T0 + 3 * MS)                  # the first fragment is done: frame not yet
        self.assertNotIn((1, 0x41), x.accs)
        x.feed(m.MON_EVENT_PKT, completed(1, 1), T0 + 6 * MS)
        self.assertAlmostEqual(x.accs[(1, 0x41)].delay.lo, 6.0)
        self.assertEqual(x.snapshot([1])["tx_p"], 2)                            # byte and packet counting is unchanged
        self.assertEqual(x.snapshot([1])["tx_b"], 22)

    def test_one_completion_event_for_two_frames_gives_each_its_own_delay(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_TX, acl(1, m.PB_START_FLUSH, l2cap(0x41, rtp(1))), T0)
        x.feed(m.MON_ACL_TX, acl(1, m.PB_START_FLUSH, l2cap(0x41, rtp(2))), T0 + 2 * MS)
        x.feed(m.MON_EVENT_PKT, completed(1, 2), T0 + 10 * MS)
        a = x.accs[(1, 0x41)]
        self.assertEqual((a.delay.n, round(a.delay.lo, 3), round(a.delay.hi, 3)), (2, 8.0, 10.0))

    def test_completions_of_different_handles_are_matched_per_handle(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_TX, acl(1, m.PB_START_FLUSH, l2cap(0x41, rtp(1))), T0)
        x.feed(m.MON_ACL_TX, acl(2, m.PB_START_FLUSH, l2cap(0x42, rtp(1))), T0 + MS)
        x.feed(m.MON_EVENT_PKT, bytes([0x13, 9, 2]) + struct.pack("<HHHH", 2, 1, 1, 1), T0 + 4 * MS)
        self.assertAlmostEqual(x.accs[(2, 0x42)].delay.lo, 3.0)
        self.assertAlmostEqual(x.accs[(1, 0x41)].delay.lo, 4.0)

    def test_a_completion_before_its_send_is_counted_as_impossible_not_recorded(self):
        x = m.AclMeter(0)
        media(x, 1, T0 + 5 * MS, T0)
        self.assertEqual((x.bad_delays, len(x.accs)), (1, 0))
        t = x.integrity()["timing"]
        self.assertFalse(t["ok"])
        self.assertIn("impossible delay", " ".join(t["notes"]))

    def test_a_continuation_with_no_start_is_counted_and_ignored(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_TX, acl(1, m.PB_CONT, b"x" * 10), T0)
        self.assertEqual(x.orphans, 1)
        self.assertEqual(x.snapshot([1])["tx_b"], 10)

    def test_a_cut_short_capture_still_counts_bytes_from_the_header_length(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_RX, acl(1, m.PB_START_FLUSH, l2cap(0x41, b"\0" * 20)[:6], dlen=24), T0)
        self.assertEqual(x.snapshot([1])["rx_b"], 24)

    def test_flush_events_are_counted_per_handle_and_reported(self):
        x = m.AclMeter(0)
        for i in range(25):
            media(x, i, T0 + i * 20 * MS, T0 + i * 20 * MS + 5 * MS)
        x.feed(m.MON_EVENT_PKT, bytes([0x11, 2]) + struct.pack("<H", 1), T0 + SEC)
        x.feed(m.MON_EVENT_PKT, bytes([0x11, 2]) + struct.pack("<H", 1), T0 + SEC)
        x.feed(m.MON_EVENT_PKT, bytes([0x11, 2]) + struct.pack("<H", 9), T0 + SEC)
        self.assertEqual(x.delay_report()["flush_events"], 2)

    def test_signalling_and_short_lived_channels_are_not_reported_as_the_media_channel(self):
        x = m.AclMeter(0)
        for i in range(40):
            media(x, i, T0 + i * 20 * MS, T0 + i * 20 * MS + 6 * MS, cid=0x41, size=600)
            media(x, i, T0 + i * 20 * MS + MS, T0 + i * 20 * MS + 2 * MS, cid=0x40, size=16)      # signalling: tiny, but frequent
        for i in range(5):
            media(x, i, T0 + i * 20 * MS, T0 + i * 20 * MS + 3 * MS, cid=0x42, size=600)           # busy for a moment only
        rep = x.delay_report()
        self.assertEqual([c["cid"] for c in rep["channels"]], [0x41])
        self.assertEqual(rep["delay_ms"]["n"], 40)

    def test_the_same_stream_on_a_new_handle_after_a_reconnect_is_one_measurement(self):
        x = m.AclMeter(0)
        for i in range(30):
            media(x, i, T0 + i * 20 * MS, T0 + i * 20 * MS + 5 * MS, handle=1)
            media(x, i, T0 + SEC + i * 20 * MS, T0 + SEC + i * 20 * MS + 9 * MS, handle=2)
        both = x.delay_report([1, 2])
        self.assertEqual(both["delay_ms"]["n"], 60)
        self.assertAlmostEqual(both["delay_ms"]["avg"], 7.0, places=1)
        self.assertEqual(x.delay_report([2])["delay_ms"]["n"], 30)
        self.assertIsNone(x.delay_report([7]))

    def test_nothing_is_reported_before_enough_media_packets(self):
        x = m.AclMeter(0)
        for i in range(m.MIN_MEDIA_FRAMES - 1):
            media(x, i, T0 + i * 20 * MS, T0 + i * 20 * MS + 5 * MS)
        self.assertIsNone(x.delay_report())

    def test_interval_report_covers_only_what_is_new_and_only_the_media_channel(self):
        x = m.AclMeter(0, intervals=True)
        for i in range(30):
            media(x, i, T0 + i * 20 * MS, T0 + i * 20 * MS + 5 * MS)
            media(x, i, T0 + i * 20 * MS, T0 + i * 20 * MS + MS, cid=0x40, size=16)
        r = x.interval_report()
        self.assertEqual(r["n"], 30)
        self.assertAlmostEqual(r["avg"], 5.0, places=2)
        self.assertIsNone(x.interval_report())
        media(x, 99, T0 + SEC, T0 + SEC + 7 * MS)
        self.assertEqual(x.interval_report()["n"], 1)

    def test_every_channel_starts_a_new_interval_so_unreported_ones_do_not_grow(self):
        x = m.AclMeter(0, intervals=True)
        for i in range(30):
            media(x, i, T0 + i * 20 * MS, T0 + i * 20 * MS + 5 * MS, handle=1)
            media(x, i, T0 + i * 20 * MS, T0 + i * 20 * MS + 5 * MS, handle=2, cid=0x50)
        x.interval_report([1])
        self.assertEqual(len(x.accs[(2, 0x50)].ival), 0)

    def test_a_meter_that_is_never_asked_for_intervals_keeps_constant_memory(self):
        x = m.AclMeter(0)
        for i in range(2000):
            media(x, i, T0 + i * 20 * MS, T0 + i * 20 * MS + 5 * MS)
        self.assertEqual(len(x.accs[(1, 0x41)].ival), 0)
        self.assertEqual(x.accs[(1, 0x41)].n, 2000)

    def test_timing_trust_depends_on_kernel_timestamps(self):
        x = m.AclMeter(0)
        self.assertEqual(x.integrity()["timing"]["clock"], "not recorded")
        x.ts_kernel, x.ts_user = 1000, 0
        t = x.integrity()["timing"]
        self.assertEqual((t["clock"], t["ok"]), ("kernel", True))
        x.ts_kernel, x.ts_user = 100, 900
        t = x.integrity()["timing"]
        self.assertEqual((t["clock"], t["ok"]), ("arrival-in-process", False))
        self.assertIn("kernel timestamp", " ".join(t["notes"]))
        self.assertTrue(x.integrity()["ok"])                                    # throughput is unaffected by the clock

    def test_a_throughput_integrity_problem_also_makes_the_delay_untrustworthy(self):
        x = m.AclMeter(0)
        x.feed(m.MON_EVENT_PKT, completed(1, m.STARTUP_GRACE_PKTS + 1), T0)
        self.assertFalse(x.integrity()["timing"]["ok"])

    def test_disconnect_forgets_half_received_frames(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_TX, acl(1, m.PB_START_FLUSH, l2cap(0x41, rtp(1))[:10], dlen=10), T0)
        x.feed(m.MON_ACL_TX, acl(1, m.PB_START_FLUSH, l2cap(0x41, b"\0" * 100)[:20]), T0)
        self.assertTrue(x.cur)
        x.feed(m.MON_EVENT_PKT, bytes([0x05, 4, 0]) + struct.pack("<H", 1) + bytes([0x13]), T0 + MS)
        self.assertFalse(x.cur)

    def test_random_input_never_raises_and_never_stores_a_negative_delay(self):
        rng = random.Random(21)
        x = m.AclMeter(0, track_rtp=True, intervals=True)
        for _ in range(30000):
            op = rng.choice([2, 3, 4, 5, 6, 7, 8, 99])
            n = rng.randint(0, 60)
            body = bytes(rng.getrandbits(8) for _ in range(n))
            if op in (4, 5) and n >= 4 and rng.random() < 0.9:                  # mostly plausible ACL headers
                body = struct.pack("<HH", rng.choice([1, 2, 0x1001, 0x2001, 0x2002]) | (rng.choice([0, 1, 2, 3]) << 12), n - 4) + body[4:]
            if op == 3 and rng.random() < 0.5:
                body = bytes([rng.choice([0x13, 0x11, 0x05]), 5, 1]) + struct.pack("<HH", rng.choice([1, 2]), rng.randint(0, 3))
            x.feed(op, body, T0 + rng.randint(-5, 10 ** 8))
        for acc in x.accs.values():
            self.assertGreaterEqual(acc.delay.lo or 0, 0)
        x.delay_report()
        x.interval_report()
        x.integrity()
        x.drain_rtp()


SEC = 1000 * MS


class RtpIdentity(unittest.TestCase):
    def test_a2dp_media_header_is_recognised(self):
        head = rtp(0x1234, ssrc=0xDEADBEEF)[:12]
        self.assertEqual(m.rtp_identity(0x41, head), (0xDEADBEEF, 0x1234))
        self.assertEqual(m.rtp_identity(0x41, rtp(5, pt=0x60)[:12]), (1, 5))     # marker bit clear

    def test_signalling_control_and_fixed_channels_are_not_media(self):
        self.assertIsNone(m.rtp_identity(0x40 - 1, rtp(1)[:12]))                 # fixed channel
        self.assertIsNone(m.rtp_identity(0x41, bytes([0x80, 0x01]) + b"\0" * 10))  # AVDTP signal id 1, not a payload type >= 96
        self.assertIsNone(m.rtp_identity(0x41, bytes([0x10, 0x11, 0x0E, 0x01]) + b"\0" * 8))   # AVCTP
        self.assertIsNone(m.rtp_identity(0x41, rtp(1)[:11]))                     # too short
        self.assertIsNone(m.rtp_identity(0x41, None))
        bad_version = bytes([0x40]) + rtp(1)[1:12]
        self.assertIsNone(m.rtp_identity(0x41, bad_version))


# ------------------------------------------------------------------ the loopback matcher
def rx_frame(x, seq, t_first, t_last, handle=1, cid=0x41, size=600, ssrc=1):
    """A media frame arriving at the receiver in two fragments, the last one at t_last."""
    f = l2cap(cid, rtp(seq, ssrc, payload=b"\0" * (size - 12)))
    x.feed(m.MON_ACL_RX, acl(handle, m.PB_START_FLUSH, f[:300], dlen=300), t_first)
    x.feed(m.MON_ACL_RX, acl(handle, m.PB_CONT, f[300:], dlen=len(f) - 300), t_last)


def make_pair(n=100, period_ms=20, link_ms=6, done_ms=5, skip=(), dup=(), late=None):
    tx, rx = m.AclMeter(6, track_rtp=True), m.AclMeter(7, track_rtp=True)
    pair = m.LinkPair(tx, rx)
    arrive = {i: T0 + i * period_ms * MS + link_ms * MS for i in range(n)}
    for i, extra_ms in (late or {}).items():           # a packet that arrives later than its successors: they overtake it
        arrive[i] += extra_ms * MS
    for i in range(n):
        t = T0 + i * period_ms * MS
        media(tx, i, t, t + done_ms * MS)
    for i in sorted((i for i in range(n) if i not in skip), key=lambda i: arrive[i]):     # the monitor delivers frames in time order
        t = arrive[i]
        rx_frame(rx, i, t - MS, t)
        if i in dup:
            rx_frame(rx, i, t - MS, t + MS)
    return tx, rx, pair


class LoopbackMatcher(unittest.TestCase):
    def test_every_packet_is_matched_and_the_one_way_delay_is_exact(self):
        _tx, _rx, pair = make_pair()
        pair.finish()
        r = pair.report()
        self.assertEqual((r["packets_sent"], r["matched"], r["lost"], r["loss_pct"]), (100, 100, 0, 0.0))
        self.assertAlmostEqual(r["delay_ms"]["avg"], 6.0, places=3)
        self.assertEqual(r["jitter_ms"]["avg"], 0.0)
        c = r["completion_minus_arrival_ms"]                                  # completion at 5 ms, arrival at 6 ms
        self.assertEqual((c["n"], c["min"], c["max"]), (100, -1.0, -1.0))

    def test_packets_that_never_arrive_are_counted_lost(self):
        _tx, _rx, pair = make_pair(n=150, skip=(10, 11, 40))
        pair.finish()
        r = pair.report()
        self.assertEqual((r["lost"], r["matched"]), (3, 147))
        self.assertAlmostEqual(r["loss_pct"], 100.0 * 3 / 150, places=2)

    def test_packets_still_in_flight_at_the_end_are_not_called_lost(self):
        _tx, _rx, pair = make_pair(n=150, skip=(148, 149))                    # the last 40 ms: not yet arrived when the run stopped
        pair.finish()
        r = pair.report()
        self.assertEqual((r["lost"], r["in_flight_excluded"]), (0, 2))
        self.assertIsNone(m.LinkPair(m.AclMeter(1), m.AclMeter(2)).report()["loss_pct"])      # nothing sent: no percentage

    def test_a_packet_received_twice_is_reported_and_counted_once(self):
        _tx, _rx, pair = make_pair(dup=(5,))
        pair.finish()
        r = pair.report()
        self.assertEqual((r["matched"], r["duplicates"]), (100, 1))
        self.assertAlmostEqual(r["delay_ms"]["max"], 6.0, places=1)             # the first arrival stood, not the later copy

    def test_a_copy_that_arrives_after_the_match_is_still_a_duplicate(self):
        tx, rx = m.AclMeter(6, track_rtp=True), m.AclMeter(7, track_rtp=True)
        pair = m.LinkPair(tx, rx)
        media(tx, 1, T0, T0 + 5 * MS)
        rx_frame(rx, 1, T0 + 5 * MS, T0 + 6 * MS)
        pair.poll()
        rx_frame(rx, 1, T0 + 8 * MS, T0 + 9 * MS)
        pair.poll()
        self.assertEqual((pair.matched, pair.dup_rx), (1, 1))

    def test_a_restarted_stream_reuses_ssrc_and_sequence_numbers_without_confusing_the_match(self):
        # PulseAudio and PipeWire use SSRC 1 and start the sequence at 0 again whenever the stream restarts (seen on the resource).
        tx, rx = m.AclMeter(6, track_rtp=True), m.AclMeter(7, track_rtp=True)
        pair = m.LinkPair(tx, rx)
        for session, t_base in ((0, 0), (1, 3 * SEC)):
            for i in range(50):
                t = T0 + t_base + i * 20 * MS
                media(tx, i, t, t + 5 * MS)
                rx_frame(rx, i, t + 5 * MS, t + 6 * MS)
        pair.poll()
        pair.finish()
        r = pair.report()
        self.assertEqual((r["packets_sent"], r["matched"], r["lost"], r["duplicates"], r["unmatched_arrivals"]), (100, 100, 0, 0, 0))
        self.assertEqual(r["reordered"], 0)                                     # sequence numbers going back to 0 is a restart
        self.assertAlmostEqual(r["delay_ms"]["avg"], 6.0, places=3)

    def test_a_restart_after_a_very_short_session_is_not_reordering(self):
        # the receiver suspended the stream right after START: a session of a few packets, then the sequence starts at 0 again
        tx, rx = m.AclMeter(6, track_rtp=True), m.AclMeter(7, track_rtp=True)
        pair = m.LinkPair(tx, rx)
        t = T0
        for session_len in (200, 12, 9, 200):
            for i in range(session_len):
                media(tx, i, t, t + 5 * MS)
                rx_frame(rx, i, t + 5 * MS, t + 6 * MS)
                t += 20 * MS
            t += 300 * MS
        pair.poll()
        pair.finish()
        r = pair.report()
        self.assertEqual((r["matched"], r["lost"], r["reordered"], r["duplicates"]), (421, 0, 0, 0))

    def test_a_restart_straight_after_a_lost_packet_still_pairs_the_rest(self):
        tx, rx = m.AclMeter(6, track_rtp=True), m.AclMeter(7, track_rtp=True)
        pair = m.LinkPair(tx, rx)
        for session, t_base in ((0, 0), (1, 8 * SEC)):                          # the second session starts after the 5 s timeout
            for i in range(50):
                t = T0 + t_base + i * 20 * MS
                media(tx, i, t, t + 5 * MS)
                if not (session == 0 and i == 7):
                    rx_frame(rx, i, t + 5 * MS, t + 6 * MS)
        pair.poll()
        pair.finish()
        r = pair.report()
        self.assertEqual((r["matched"], r["lost"]), (99, 1))
        self.assertAlmostEqual(r["delay_ms"]["max"], 6.0, places=1)             # the lost packet was not paired with the restart's

    def test_an_arrival_that_precedes_every_send_we_hold_is_not_paired(self):
        tx, rx = m.AclMeter(6, track_rtp=True), m.AclMeter(7, track_rtp=True)
        pair = m.LinkPair(tx, rx)
        rx_frame(rx, 3, T0, T0 + MS)                                            # its send happened before the meter started
        media(tx, 3, T0 + SEC, T0 + SEC + 5 * MS)
        rx_frame(rx, 3, T0 + SEC + 5 * MS, T0 + SEC + 6 * MS)
        pair.poll()
        pair.finish()
        r = pair.report()
        self.assertEqual((r["matched"], r["unmatched_arrivals"]), (1, 1))
        self.assertAlmostEqual(r["delay_ms"]["avg"], 6.0, places=3)

    def test_out_of_order_arrival_is_reported(self):
        _tx, _rx, pair = make_pair(late={20: 30})            # packet 20 arrives 30 ms late, after packet 21 (20 ms behind it)
        pair.finish()
        r = pair.report()
        self.assertEqual((r["reordered"], r["matched"], r["lost"]), (1, 100, 0))

    def test_a_packet_older_than_the_timeout_is_lost_during_the_run_too(self):
        tx, rx, pair = make_pair(n=400, skip=(1,))                            # 8 s of traffic
        pair.poll()
        self.assertEqual(pair.lost, 1)                                         # decided by the 5 s horizon, before finish()

    def test_interval_figures_and_losses_since_the_last_call(self):
        tx, rx, pair = make_pair(n=300, skip=(3,))
        r, lost = pair.interval()
        self.assertEqual(r["n"], 299)
        self.assertEqual(lost, 1)
        self.assertEqual(pair.interval(), (None, 0))

    def test_signalling_frames_are_never_matched_as_media(self):
        tx, rx = m.AclMeter(6, track_rtp=True), m.AclMeter(7, track_rtp=True)
        for i in range(10):
            tx.feed(m.MON_ACL_TX, acl(1, 0, l2cap(1, bytes([0x0A, 0x01, i]) + b"\0" * 6)), T0 + i * MS)
            rx.feed(m.MON_ACL_RX, acl(1, 2, l2cap(1, bytes([0x0A, 0x01, i]) + b"\0" * 6)), T0 + i * MS)
        pair = m.LinkPair(tx, rx)
        pair.finish()
        self.assertEqual(pair.report()["packets_sent"], 0)

    def test_the_meters_keep_nothing_unless_asked_to_track_rtp(self):
        x = m.AclMeter(6)
        media(x, 1, T0, T0 + 5 * MS)
        self.assertEqual(x.drain_rtp(), ([], []))

    def test_drained_identities_are_not_returned_twice(self):
        x = m.AclMeter(6, track_rtp=True)
        media(x, 1, T0, T0 + 5 * MS)
        self.assertEqual(len(x.drain_rtp()[0]), 1)
        self.assertEqual(x.drain_rtp(), ([], []))


# ------------------------------------------------------------------ a real capture
def replay_fixture():
    with open(os.path.join(HERE, "data", "a2dp_loopback.jsonl")) as f:
        rows = [json.loads(line) for line in f]
    tx, rx = m.AclMeter(6, track_rtp=True, intervals=True), m.AclMeter(7, track_rtp=True)
    for ts, index, op, hx in rows:
        (tx if index == 6 else rx).feed(op, bytes.fromhex(hx), ts)
    pair = m.LinkPair(tx, rx)
    pair.finish()
    return tx, rx, pair


class RealCapture(unittest.TestCase):
    """AAC over a real radio pair (Intel BE200 x2, 103 media packets, 2.2 s). The figures were produced by this code from the
    capture; a change in them means the algorithm changed, which must be deliberate."""

    @classmethod
    def setUpClass(cls):
        cls.tx, cls.rx, cls.pair = replay_fixture()

    def test_the_sender_side_delay(self):
        rep = self.tx.delay_report()
        d = rep["delay_ms"]
        self.assertEqual((d["n"], rep["channels"]), (103, [{"handle": 256, "cid": 67, "frames": 103}]))
        for key, want in (("min", 8.673), ("avg", 13.398), ("p50", 13.45), ("p95", 17.95), ("p99", 18.25), ("max", 18.251)):
            self.assertAlmostEqual(d[key], want, delta=0.002, msg=key)
        self.assertAlmostEqual(rep["jitter_ms"]["avg"], 2.267, delta=0.002)
        self.assertEqual(rep["jitter_ms"]["n"], 102)
        self.assertEqual(rep["bytes"], 103 * 676)

    def test_every_packet_is_seen_at_both_ends_and_none_is_lost(self):
        r = self.pair.report()
        self.assertEqual((r["packets_sent"], r["matched"], r["lost"], r["duplicates"], r["reordered"]), (103, 103, 0, 0, 0))
        self.assertEqual(self.tx.integrity()["unmatched_completions"], 0)

    def test_the_true_one_way_delay_is_close_to_the_sender_side_figure(self):
        lb = self.pair.report()["delay_ms"]
        sender = self.tx.delay_report()["delay_ms"]
        self.assertAlmostEqual(lb["avg"], 14.251, delta=0.002)
        self.assertLess(abs(lb["avg"] - sender["avg"]), 1.5)                   # the sender-side figure is a good proxy
        c = self.pair.report()["completion_minus_arrival_ms"]
        self.assertEqual(c["n"], 103)
        self.assertLess(abs(c["p50"]), 1.5)
        self.assertGreater(c["min"], -4.0)

    def test_the_receiver_reassembles_two_fragments_per_packet(self):
        # 103 matched packets of two fragments each, plus one more packet that arrived just after the window began
        self.assertEqual(self.rx.snapshot()["rx_p"], 208)
        self.assertEqual(self.rx.orphans, 0)


class RealRestart(unittest.TestCase):
    """SBC from PulseAudio on resource 1.2 (tests/data/a2dp_restart_pulse.jsonl, 86 packets): SSRC 1 as always, and the
    sequence number goes back to 0 when the stream restarts in the middle of the capture. Before the matcher kept an ordered queue
    per identity it paired packets of the two sessions with each other (negative delays, hundreds of false duplicates)."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "data", "a2dp_restart_pulse.jsonl")) as f:
            rows = [json.loads(line) for line in f]
        cls.tx, cls.rx = m.AclMeter(0, track_rtp=True, intervals=True), m.AclMeter(1, track_rtp=True)
        for ts, index, op, hx in rows:
            (cls.tx if index == 0 else cls.rx).feed(op, bytes.fromhex(hx), ts)
        cls.pair = m.LinkPair(cls.tx, cls.rx)
        cls.pair.finish()

    def test_every_packet_across_the_restart_is_paired_with_its_own_arrival(self):
        r = self.pair.report()
        self.assertEqual((r["packets_sent"], r["matched"], r["lost"], r["in_flight_excluded"]), (86, 85, 0, 1))
        self.assertEqual((r["duplicates"], r["reordered"], r["unmatched_arrivals"]), (0, 0, 0))
        self.assertGreater(r["delay_ms"]["min"], 0)                              # no packet "arrived before it was sent"
        self.assertAlmostEqual(r["delay_ms"]["avg"], 12.778, delta=0.002)

    def test_the_sender_side_figures_of_the_sbc_stream(self):
        rep = self.tx.delay_report()
        self.assertEqual((rep["delay_ms"]["n"], rep["channels"][0]["cid"], rep["bytes"]), (85, 65, 85 * 612))
        self.assertAlmostEqual(rep["delay_ms"]["avg"], 11.928, delta=0.002)
        self.assertAlmostEqual(rep["jitter_ms"]["avg"], 5.151, delta=0.002)       # SBC's cadence gives a different baseline than AAC's 2.3 ms
        self.assertAlmostEqual(self.pair.report()["completion_minus_arrival_ms"]["p50"], -1.15, delta=0.1)


# ------------------------------------------------------------------ result fields and limits
GOOD = {"available": True, "valid": True, "notes": [], "delay_ms": {"p50": 13.0, "p95": 17.0, "p99": 18.0, "max": 25.0},
        "jitter_ms": {"avg": 2.4}}


class Limits(unittest.TestCase):
    def test_nothing_is_judged_without_a_limit(self):
        self.assertEqual(bc.delay_failures(GOOD, None, None), [])
        self.assertEqual(bc.delay_failures(None, None, None), [])

    def test_p99_delay_over_the_limit_fails_and_names_the_figures(self):
        out = bc.delay_failures(GOOD, 15.0, None)
        self.assertEqual(len(out), 1)
        self.assertIn("p99 18.0 ms exceeds the limit of 15.0 ms", out[0])
        self.assertEqual(bc.delay_failures(GOOD, 18.0, None), [])               # equal to the limit passes

    def test_jitter_over_the_limit_fails(self):
        self.assertIn("jitter 2.40 ms exceeds the limit of 2.00 ms", bc.delay_failures(GOOD, None, 2.0)[0])
        self.assertEqual(bc.delay_failures(GOOD, None, 2.4), [])

    def test_a_limit_that_cannot_be_checked_fails_instead_of_passing(self):
        self.assertIn("could be measured", bc.delay_failures(None, 20, None)[0])
        self.assertIn("could be measured", bc.delay_failures({"available": False, "reason": "no packets"}, 20, None)[0])
        bad = dict(GOOD, valid=False, notes=["only 3% of monitor frames carried a kernel timestamp"])
        self.assertIn("not trustworthy", bc.delay_failures(bad, 20, None)[0])
        no_jitter = dict(GOOD, jitter_ms=None)
        self.assertIn("too few packets", bc.delay_failures(no_jitter, None, 5)[0])

    def test_the_result_block_says_what_was_measured_and_whether_to_trust_it(self):
        rep = {"delay_ms": {"n": 5}, "jitter_ms": None, "late": None, "channels": []}
        integ = {"timing": {"clock": "kernel", "ok": True, "notes": []}}
        b = bc.link_delay_block(rep, integ)
        self.assertTrue(b["available"] and b["valid"])
        self.assertEqual(b["clock"], "kernel")
        self.assertIn("not the audio latency", b["basis"].lower())
        none = bc.link_delay_block(None, integ)
        self.assertFalse(none["available"])
        self.assertIn("no A2DP media packet", none["reason"])
        self.assertIn("unavailable", bc.link_delay_block(None, None)["reason"])
        flagged = bc.link_delay_block(rep, {"timing": {"clock": "arrival-in-process", "ok": False, "notes": ["x"]}})
        self.assertFalse(flagged["valid"])


class Arguments(unittest.TestCase):
    ADDR = "AA:BB:CC:DD:EE:FF"

    def test_limits_are_passed_to_the_a2dp_test(self):
        a = server.build_classic_argv("c_a2dp", 1, {"addr": self.ADDR, "max_delay_ms": "25", "max_jitter_ms": "4.5"})
        self.assertEqual(a[a.index("--max-delay-ms") + 1], "25.0")
        self.assertEqual(a[a.index("--max-jitter-ms") + 1], "4.5")

    def test_blank_limits_are_left_to_the_engine(self):
        a = server.build_classic_argv("c_a2dp", 1, {"addr": self.ADDR, "max_delay_ms": "", "max_jitter_ms": " "})
        self.assertNotIn("--max-delay-ms", a)
        self.assertNotIn("--max-jitter-ms", a)

    def test_bad_limits_are_refused(self):
        for bad in ({"max_delay_ms": "0"}, {"max_delay_ms": "x"}, {"max_delay_ms": "nan"}, {"max_delay_ms": "100000"},
                    {"max_jitter_ms": "0"}, {"max_jitter_ms": "-1"}, {"max_jitter_ms": "inf"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                server.build_classic_argv("c_a2dp", 1, dict(bad, addr=self.ADDR))

    def test_the_command_line_has_the_limits_and_they_default_to_not_judged(self):
        a = bc.build_parser().parse_args(["--hci", "1", "a2dp", self.ADDR])
        self.assertEqual((a.max_delay_ms, a.max_jitter_ms), (None, None))
        a = bc.build_parser().parse_args(["--hci", "1", "a2dp", self.ADDR, "--max-delay-ms", "30", "--max-jitter-ms", "5"])
        self.assertEqual((a.max_delay_ms, a.max_jitter_ms), (30.0, 5.0))
        load = bc.build_parser().parse_args(["--hci", "1", "load", self.ADDR])
        self.assertEqual((load.max_delay_ms, load.max_jitter_ms), (None, None))


# ------------------------------------------------------------------ through the real job code with a fake meter
class FakeMeter:
    """Stands in for acl_meter.AclMeter: no socket, canned delay data."""
    instances = []
    report = None                      # what delay_report() returns
    interval = None
    truncated = 0
    error = None
    integ = {"ok": True, "frames": 10, "truncated": 0, "unmatched_completions": 0, "max_backlog_pkts": 0, "notes": [],
             "timing": {"clock": "kernel", "ok": True, "notes": [], "kernel_stamped": 10, "process_stamped": 0,
                        "orphan_fragments": 0, "impossible_delays": 0}}

    def __init__(self, hci, **kw):
        self.hci, self.kw = hci, kw
        self.track_rtp = kw.get("track_rtp", False)
        FakeMeter.instances.append(self)

    def open(self):
        return self

    def start(self):
        pass

    def close(self):
        self.closed = True

    def snapshot_all(self):
        import time
        return {"t": time.monotonic(), "per": {}}

    def handles_for(self, addr):
        return [7]

    def integrity(self):
        return self.integ

    def delay_report(self, handles=None):
        FakeMeter.asked_handles = handles
        return self.report

    def interval_report(self, handles=None):
        return self.interval

    activity = None
    avdtp = None

    def activity_report(self):
        return self.activity

    def avdtp_report(self, handles=None):
        return self.avdtp


class FakeLinkPair:
    instances = []

    def __init__(self, tx, rx, **kw):
        self.tx, self.rx, self.kw, self.finished = tx, rx, kw, False
        FakeLinkPair.instances.append(self)

    def interval(self):
        return {"n": 40, "avg": 12.5, "p50": 12.4, "p95": 16.0, "max": 17.0, "jitter_avg": 2.0, "jitter_p95": 7.0}, 2

    def finish(self):
        self.finished = True

    def report(self):
        return {"packets_sent": 400, "matched": 398, "lost": 2, "loss_pct": 0.5, "delay_ms": {"avg": 12.5}}


def fake_module(report=None, interval=None, integ=None, meter_open_fails=(), activity=None, avdtp=None):
    class M(FakeMeter):
        def open(self):
            if self.hci in meter_open_fails:
                raise OSError("no monitor")
            return self
    M.report, M.interval, M.activity, M.avdtp = report, interval, activity, avdtp
    if integ is not None:
        M.integ = integ
    return types.SimpleNamespace(AclMeter=M, rates_for=m.rates_for, LinkPair=FakeLinkPair)


REPORT = {"delay_ms": {"n": 400, "min": 8.0, "avg": 13.0, "p50": 13.0, "p95": 17.0, "p99": 18.0, "max": 19.0},
          "jitter_ms": {"avg": 2.3, "p95": 8.6, "max": 9.0, "rfc3550": 2.2, "n": 399}, "late": None,
          "channels": [{"handle": 7, "cid": 65, "frames": 400}], "bytes": 100000, "flush_events": 0}
INTERVAL = {"n": 50, "avg": 13.1, "p50": 13.0, "p95": 17.1, "max": 18.0, "jitter_avg": 2.3, "jitter_p95": 8.0}


class JobIntegration(unittest.TestCase):
    TAIL = ["a2dp", tcl.ADDR, "-t", "1", "--interval", "0.05"]

    def run_it(self, extra=(), **kw):
        mod = fake_module(**kw)
        rc, sm = tcl.run_job(self.TAIL + list(extra), [None], drop_at=10 ** 6, acl_module=mod)
        return rc, sm

    def test_the_delay_block_and_per_interval_columns_are_in_the_result(self):
        rc, sm = self.run_it(report=REPORT, interval=INTERVAL)
        self.assertEqual(rc, 0, sm.get("fail_reasons"))
        ld = sm["link_delay"]
        self.assertTrue(ld["available"] and ld["valid"])
        self.assertEqual((ld["delay_ms"]["p99"], ld["jitter_ms"]["avg"], ld["clock"]), (18.0, 2.3, "kernel"))
        self.assertEqual(sm["stream_latency_setting_ms"], 100)                  # the buffer that was asked for, labelled as such
        self.assertIsNone(sm["loopback"])                                       # not an internal receiver
        self.assertEqual(sm["thresholds"]["max_delay_ms"], None)

    def test_the_handles_of_the_dut_are_what_is_measured(self):
        self.run_it(report=REPORT, interval=INTERVAL)
        self.assertEqual(FakeMeter.asked_handles, [7])

    def test_a_delay_limit_that_is_exceeded_fails_the_run(self):
        rc, sm = self.run_it(["--max-delay-ms", "15"], report=REPORT, interval=INTERVAL)
        self.assertEqual(rc, 2)
        self.assertIn("p99 18.0 ms exceeds", " ".join(sm["fail_reasons"]))
        self.assertEqual(sm["thresholds"]["max_delay_ms"], 15.0)

    def test_a_jitter_limit_that_is_exceeded_fails_the_run(self):
        rc, sm = self.run_it(["--max-jitter-ms", "1.5"], report=REPORT, interval=INTERVAL)
        self.assertEqual(rc, 2)
        self.assertIn("jitter 2.30 ms exceeds", " ".join(sm["fail_reasons"]))

    def test_limits_that_are_met_pass(self):
        rc, sm = self.run_it(["--max-delay-ms", "20", "--max-jitter-ms", "3"], report=REPORT, interval=INTERVAL)
        self.assertEqual(rc, 0, sm.get("fail_reasons"))

    def test_a_limit_with_no_measurement_fails_rather_than_passes(self):
        rc, sm = self.run_it(["--max-delay-ms", "50"], report=None, interval=None)
        self.assertEqual(rc, 2)
        self.assertFalse(sm["link_delay"]["available"])
        self.assertIn("could be measured", " ".join(sm["fail_reasons"]))

    def test_a_limit_with_an_untrustworthy_clock_fails(self):
        integ = dict(FakeMeter.integ, timing={"clock": "arrival-in-process", "ok": False, "notes": ["only 1% kernel timestamps"]})
        rc, sm = self.run_it(["--max-delay-ms", "50"], report=REPORT, interval=INTERVAL, integ=integ)
        self.assertEqual(rc, 2)
        self.assertIn("not trustworthy", " ".join(sm["fail_reasons"]))
        self.assertFalse(sm["link_delay"]["valid"])

    def test_without_a_limit_a_missing_measurement_does_not_fail_the_run(self):
        rc, sm = self.run_it(report=None, interval=None)
        self.assertEqual(rc, 0, sm.get("fail_reasons"))
        self.assertFalse(sm["link_delay"]["available"])

    def test_the_meter_is_asked_to_keep_intervals_and_to_count_late_packets_against_the_limit(self):
        FakeMeter.instances = []
        self.run_it(["--max-delay-ms", "20"], report=REPORT, interval=INTERVAL)
        kw = FakeMeter.instances[0].kw
        self.assertEqual((kw.get("late_limit_ms"), kw.get("intervals")), (20.0, True))

    def test_load_mode_reports_the_delay_but_never_judges_it(self):
        mod = fake_module(report=REPORT, interval=INTERVAL)
        rc, sm = tcl.run_job(["load", tcl.ADDR, "-t", "1", "--interval", "0.05"], [None], drop_at=10 ** 6, acl_module=mod)
        self.assertEqual(rc, 0, sm.get("fail_reasons"))
        self.assertTrue(sm["link_delay"]["available"])

    def run_internal(self, **kw):
        from unittest import mock
        FakeMeter.instances, FakeLinkPair.instances = [], []
        tag = tcl.ADDR.replace(":", "_").lower()
        mod = fake_module(report=REPORT, interval=INTERVAL, **kw)
        with mock.patch.object(bc, "hci_of_addr", lambda b, a: 2):
            return tcl.run_job_ctx(self.TAIL, [None], drop_at=10 ** 6, acl_module=mod, local={tag})

    def test_an_internal_receiver_gets_a_second_monitor_and_a_loopback_measurement(self):
        rc, sm, ctx = self.run_internal()
        self.assertEqual(rc, 0, sm.get("fail_reasons"))
        tx, rx = FakeMeter.instances[0], FakeMeter.instances[1]
        self.assertEqual((tx.hci, rx.hci), (1, 2))
        self.assertTrue(tx.track_rtp and rx.track_rtp)                          # both ends keep the RTP identities
        self.assertTrue(getattr(rx, "closed", False))
        pair = FakeLinkPair.instances[0]
        self.assertTrue(pair.finished)
        self.assertEqual(sm["loopback"]["lost"], 2)
        self.assertTrue(sm["internal_receiver"])
        samples = [json.loads(l) for l in ctx["out"].splitlines() if l.startswith('{"m":"sample"')]
        self.assertEqual((samples[-1]["lb_delay_avg_ms"], samples[-1]["lb_delay_p95_ms"], samples[-1]["lb_lost"]), (12.5, 16.0, 2))

    def test_a_receiver_monitor_that_cannot_open_only_loses_the_loopback_figures(self):
        rc, sm, ctx = self.run_internal(meter_open_fails=(2,))
        self.assertEqual(rc, 0, sm.get("fail_reasons"))
        self.assertIsNone(sm["loopback"])
        self.assertTrue(sm["link_delay"]["available"])                           # the sender-side measurement is unaffected
        self.assertIn("receiver-side monitor unavailable", ctx["out"])

    def test_an_external_receiver_has_no_loopback(self):
        FakeMeter.instances, FakeLinkPair.instances = [], []
        rc, sm = self.run_it(report=REPORT, interval=INTERVAL)
        self.assertEqual((len(FakeMeter.instances), len(FakeLinkPair.instances)), (1, 0))

    def test_samples_carry_the_interval_delay_columns(self):
        mod = fake_module(report=REPORT, interval=INTERVAL)
        rc, sm, ctx = tcl.run_job_ctx(self.TAIL, [None], drop_at=10 ** 6, acl_module=mod)
        samples = [json.loads(l) for l in ctx["out"].splitlines() if l.startswith('{"m":"sample"')]
        self.assertTrue(samples)
        s = samples[-1]
        self.assertEqual((s["delay_avg_ms"], s["delay_p95_ms"], s["delay_max_ms"], s["jitter_ms"], s["delay_n"]),
                         (13.1, 17.1, 18.0, 2.3, 50))
        self.assertNotIn("lb_lost", s)                                          # only with an internal receiver


# ------------------------------------------------------------------ what else the radio did (LE scan, advertising, inquiry)
def hci_cmd(opcode, params=b""):
    return struct.pack("<HB", opcode, len(params)) + params


def cmd_complete(opcode, status=0):
    return bytes([0x0E, 4, 1]) + struct.pack("<H", opcode) + bytes([status])


def cmd_status(opcode, status=0):
    return bytes([0x0F, 4, status, 1]) + struct.pack("<H", opcode)


def issue(x, opcode, params, t, status_event="complete", status=0):
    """The host sends a command at t and the controller answers 2 ms later."""
    x.feed(m.MON_COMMAND_PKT, hci_cmd(opcode, params), t)
    ev = cmd_complete(opcode, status) if status_event == "complete" else cmd_status(opcode, status)
    x.feed(m.MON_EVENT_PKT, ev, t + 2 * MS)


class RadioActivity(unittest.TestCase):
    def test_an_le_scan_is_timed_from_its_enable_to_its_disable(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(1))), T0)                  # the window starts here
        issue(x, 0x200C, b"\x01\x00", T0 + 2 * SEC)
        issue(x, 0x200C, b"\x00\x00", T0 + 8 * SEC)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(2))), T0 + 10 * SEC)
        a = x.activity_report()
        self.assertEqual((a["window_s"], a["le_scan"]["enabled_s"], a["le_scan"]["enabled_pct"], a["le_scan"]["starts"]), (10.0, 6.0, 60.0, 1))
        self.assertEqual((a["le_adv"]["enabled_s"], a["inquiry"]["enabled_s"], a["le_connect"]["enabled_s"]), (0.0, 0.0, 0.0))

    def test_extended_scan_enable_counts_as_an_le_scan_and_one_still_on_at_the_end_runs_to_the_last_frame(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(1))), T0)
        issue(x, 0x2042, b"\x01\x01\x00\x00\x00\x00", T0 + 4 * SEC)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(2))), T0 + 10 * SEC)
        self.assertEqual(x.activity_report()["le_scan"]["enabled_pct"], 60.0)

    def test_a_command_the_controller_refused_changes_nothing(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(1))), T0)
        issue(x, 0x200C, b"\x01\x00", T0 + SEC, status=0x0C)                    # Command Disallowed
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(2))), T0 + 5 * SEC)
        self.assertEqual(x.activity_report()["le_scan"]["enabled_s"], 0.0)

    def test_a_scan_that_was_already_on_when_the_meter_started_is_seen_when_it_is_switched_off(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(1))), T0)
        issue(x, 0x200C, b"\x00\x00", T0 + 3 * SEC)                              # the first thing we see is "off"
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(2))), T0 + 10 * SEC)
        self.assertEqual(x.activity_report()["le_scan"]["enabled_s"], 3.0)

    def test_an_le_connection_attempt_lasts_until_the_connection_completes(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(1))), T0)
        issue(x, 0x200D, b"\x00" * 25, T0 + SEC, status_event="status")
        x.feed(m.MON_EVENT_PKT, bytes([0x3E, 19, 0x01, 1]) + b"\x00" * 17, T0 + 5 * SEC)    # LE Connection Complete, status 1: failed
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(2))), T0 + 10 * SEC)
        a = x.activity_report()
        self.assertEqual((a["le_connect"]["enabled_s"], a["le_connect"]["starts"]), (4.0, 1))

    def test_an_le_connection_cancel_ends_the_attempt(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(1))), T0)
        issue(x, 0x200D, b"\x00" * 25, T0, status_event="status")
        issue(x, 0x200E, b"", T0 + 2 * SEC)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(2))), T0 + 10 * SEC)
        self.assertEqual(x.activity_report()["le_connect"]["enabled_s"], 2.0)

    def test_an_inquiry_ends_with_its_complete_event_or_a_cancel(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(1))), T0)
        issue(x, 0x0401, b"\x33\x8b\x9e\x08\x00", T0 + SEC, status_event="status")
        x.feed(m.MON_EVENT_PKT, bytes([0x01, 1, 0]), T0 + 9 * SEC)                # Inquiry Complete
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(2))), T0 + 10 * SEC)
        self.assertEqual(x.activity_report()["inquiry"]["enabled_s"], 8.0)
        issue(x, 0x0401, b"\x33\x8b\x9e\x08\x00", T0 + 11 * SEC, status_event="status")
        issue(x, 0x0402, b"", T0 + 12 * SEC)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(3))), T0 + 20 * SEC)
        self.assertEqual(x.activity_report()["inquiry"]["starts"], 2)

    def test_advertising_is_tracked_and_reports_prove_a_scan(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(1))), T0)
        issue(x, 0x200A, b"\x01", T0 + SEC)
        for i in range(3):
            x.feed(m.MON_EVENT_PKT, bytes([0x3E, 2, 0x0D, 0]), T0 + (2 + i) * SEC)    # LE Extended Advertising Report
        x.feed(m.MON_EVENT_PKT, bytes([0x3E, 2, 0x02, 0]), T0 + 6 * SEC)              # LE Advertising Report
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(2))), T0 + 10 * SEC)
        a = x.activity_report()
        self.assertEqual((a["le_adv"]["enabled_pct"], a["le_reports"]), (90.0, 4))

    def test_no_frames_no_report_and_unrelated_commands_are_ignored(self):
        x = m.AclMeter(0)
        self.assertIsNone(x.activity_report())
        x.feed(m.MON_COMMAND_PKT, hci_cmd(0x0C1A, b"\x03"), T0)                    # Write Scan Enable: not an LE scan
        x.feed(m.MON_COMMAND_PKT, b"\x00", T0)                                    # too short
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(1))), T0 + SEC)
        a = x.activity_report()
        self.assertEqual((a["le_scan"]["enabled_s"], a["inquiry"]["starts"]), (0.0, 0))

    def test_a_completion_for_a_command_we_never_saw_is_ignored(self):
        x = m.AclMeter(0)
        x.feed(m.MON_EVENT_PKT, cmd_complete(0x200C), T0)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(1))), T0 + SEC)
        self.assertEqual(x.activity_report()["le_scan"]["enabled_s"], 0.0)


class Coexistence(unittest.TestCase):
    ACT = {"window_s": 20.0, "le_reports": 0,
           "le_scan": {"enabled_s": 19.0, "enabled_pct": 95.0, "starts": 1},
           "le_adv": {"enabled_s": 20.0, "enabled_pct": 100.0, "starts": 1},
           "le_connect": {"enabled_s": 0.0, "enabled_pct": 0.0, "starts": 0},
           "inquiry": {"enabled_s": 0.0, "enabled_pct": 0.0, "starts": 0}}

    def test_an_le_scan_for_most_of_the_run_is_a_warning_but_advertising_is_only_reported(self):
        c = bc.coexistence_block(self.ACT)
        self.assertEqual(len(c["warnings"]), 1)
        self.assertIn("An LE scan was on for 95 % of the run (19.0 s)", c["warnings"][0])
        self.assertEqual(c["le_adv"]["enabled_pct"], 100.0)

    def test_below_the_threshold_there_is_no_warning(self):
        quiet = dict(self.ACT, le_scan={"enabled_s": 1.0, "enabled_pct": 5.0, "starts": 1})
        self.assertEqual(bc.coexistence_block(quiet)["warnings"], [])
        busy_inquiry = dict(quiet, inquiry={"enabled_s": 1.2, "enabled_pct": 6.0, "starts": 1})
        self.assertIn("inquiry", bc.coexistence_block(busy_inquiry)["warnings"][0])

    def test_no_activity_data_means_no_block(self):
        self.assertIsNone(bc.coexistence_block(None))

    def test_a_failed_limit_says_what_else_was_using_the_radio(self):
        block = dict(GOOD, coexistence=bc.coexistence_block(self.ACT))
        out = bc.delay_failures(block, 15.0, None)
        self.assertEqual(len(out), 2)
        self.assertTrue(out[1].startswith("note: An LE scan was on"))
        self.assertEqual(bc.delay_failures(block, 30.0, None), [])                 # a pass is not decorated

    def test_the_result_block_carries_the_activity_with_or_without_a_measurement(self):
        integ = {"timing": {"clock": "kernel", "ok": True, "notes": []}}
        self.assertEqual(len(bc.link_delay_block({"delay_ms": {"n": 1}}, integ, self.ACT)["coexistence"]["warnings"]), 1)
        self.assertEqual(len(bc.link_delay_block(None, integ, self.ACT)["coexistence"]["warnings"]), 1)
        self.assertIsNone(bc.link_delay_block(None, integ)["coexistence"])

    def test_the_job_puts_the_activity_into_the_result_and_into_a_failure(self):
        mod = fake_module(report=REPORT, interval=INTERVAL, activity=self.ACT)
        rc, sm = tcl.run_job(["a2dp", tcl.ADDR, "-t", "1", "--interval", "0.05", "--max-delay-ms", "15"], [None], drop_at=10 ** 6,
                             acl_module=mod)
        self.assertEqual(rc, 2)
        self.assertEqual(len(sm["link_delay"]["coexistence"]["warnings"]), 1)
        self.assertIn("note: An LE scan was on", " ".join(sm["fail_reasons"]))


# ------------------------------------------------------------------ AVDTP stream control seen on the link
def avdtp_frame(x, tx, body, t, handle=1, cid=0x40, pb=m.PB_START_NOFLUSH):
    x.feed(m.MON_ACL_TX if tx else m.MON_ACL_RX, acl(handle, pb, l2cap(cid, body)), t)


START_CMD = bytes([0xB0, 0x07, 0x38])             # label 11, single packet, command, START, ACP SEID 14
SUSPEND_CMD = bytes([0xC0, 0x09, 0x08])
DELAY_150 = bytes([0x20, 0x0D, 0x04, 0x05, 0xDC])  # DELAYREPORT, delay 0x05DC = 1500 tenths of a millisecond


class StreamControl(unittest.TestCase):
    def test_start_by_the_host_and_suspend_by_the_remote_are_recorded_with_their_origin_and_time(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(1))), T0)                  # the meter's first frame
        avdtp_frame(x, True, START_CMD, T0 + 2 * SEC)
        avdtp_frame(x, False, SUSPEND_CMD, T0 + 8 * SEC)
        r = x.avdtp_report()
        self.assertEqual(r["counts"], {"host_start": 1, "remote_suspend": 1})
        self.assertEqual([(e["t_s"], e["from"], e["signal"]) for e in r["events"]],
                         [(2.0, "host", "START"), (8.0, "remote", "SUSPEND")])
        self.assertIsNone(r["delay_reports_ms"])

    def test_the_sinks_delay_report_is_decoded_in_milliseconds(self):
        x = m.AclMeter(0)
        avdtp_frame(x, False, DELAY_150, T0)
        avdtp_frame(x, False, bytes([0x30, 0x0D, 0x04, 0x03, 0xE8]), T0 + SEC)    # 100.0 ms
        avdtp_frame(x, True, DELAY_150, T0 + 2 * SEC)                              # our own report is not the sink's claim
        d = x.avdtp_report()["delay_reports_ms"]
        self.assertEqual((d["last"], d["min"], d["max"], d["n"]), (100.0, 100.0, 150.0, 2))

    def test_responses_media_avrcp_and_big_frames_are_not_stream_control(self):
        x = m.AclMeter(0)
        avdtp_frame(x, False, bytes([0xB2, 0x07]), T0)                             # a response (accept), not a command
        avdtp_frame(x, False, bytes([0xB3, 0x09, 0x31]), T0)                       # a reject
        avdtp_frame(x, False, bytes([0x10, 0x11, 0x0E, 0x01, 0x48, 0x00, 0x00]), T0, cid=0x44)    # AVCTP (AVRCP)
        avdtp_frame(x, False, rtp(5), T0, cid=0x41)                                # media
        avdtp_frame(x, False, SUSPEND_CMD + b"\0" * 80, T0)                        # far too big to be a signalling frame
        avdtp_frame(x, False, SUSPEND_CMD, T0, cid=0x01)                           # a fixed channel
        avdtp_frame(x, False, bytes([0xC1, 0x09, 0x08]), T0)                       # general reject message type
        avdtp_frame(x, False, bytes([0xC4, 0x09, 0x08]), T0)                       # first packet of a fragmented message
        avdtp_frame(x, False, bytes([0xC0, 0x01, 0x00]), T0)                       # DISCOVER: not tracked
        self.assertIsNone(x.avdtp_report())

    def test_the_report_can_be_limited_to_the_duts_connection(self):
        x = m.AclMeter(0)
        avdtp_frame(x, False, SUSPEND_CMD, T0, handle=1)
        avdtp_frame(x, False, SUSPEND_CMD, T0 + SEC, handle=2)
        self.assertEqual(x.avdtp_report([2])["counts"], {"remote_suspend": 1})
        self.assertEqual(x.avdtp_report()["counts"], {"remote_suspend": 2})
        self.assertIsNone(x.avdtp_report([9]))

    def test_the_record_is_bounded(self):
        x = m.AclMeter(0)
        for i in range(m.AVDTP_KEEP + 50):
            avdtp_frame(x, False, SUSPEND_CMD, T0 + i * MS)
        r = x.avdtp_report()
        self.assertEqual((r["counts"]["remote_suspend"], r["events_total"], len(r["events"])), (m.AVDTP_KEEP, m.AVDTP_KEEP, 50))

    def test_the_time_the_stream_was_held_suspended_is_exact(self):
        x = m.AclMeter(0)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(1))), T0)
        avdtp_frame(x, True, START_CMD, T0 + 5 * SEC)
        avdtp_frame(x, False, SUSPEND_CMD, T0 + 10 * SEC)            # stopped for 2 s ...
        avdtp_frame(x, True, START_CMD, T0 + 12 * SEC)
        avdtp_frame(x, False, SUSPEND_CMD, T0 + 17 * SEC)            # ... and again until the end of monitoring (T0 + 25 s)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(2))), T0 + 25 * SEC)
        r = x.avdtp_report()
        self.assertEqual((r["stream_window_s"], r["suspended_s"], r["suspended_pct"]), (20.0, 10.0, 50.0))

    def test_no_suspend_means_no_suspended_time_and_no_start_means_no_figure(self):
        x = m.AclMeter(0)
        avdtp_frame(x, True, START_CMD, T0 + SEC)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(1))), T0 + 11 * SEC)
        self.assertEqual(x.avdtp_report()["suspended_pct"], 0.0)
        y = m.AclMeter(0)
        avdtp_frame(y, False, SUSPEND_CMD, T0)                       # a suspend with no host START in view: nothing to measure against
        self.assertNotIn("suspended_pct", y.avdtp_report())

    def test_a_suspend_before_the_first_start_does_not_count(self):
        x = m.AclMeter(0)
        avdtp_frame(x, False, SUSPEND_CMD, T0)
        avdtp_frame(x, True, START_CMD, T0 + 2 * SEC)
        x.feed(m.MON_ACL_TX, acl(1, 2, l2cap(0x41, rtp(1))), T0 + 12 * SEC)
        self.assertEqual(x.avdtp_report()["suspended_s"], 0.0)

    def test_a_fragmented_signalling_frame_is_recognised_once_complete(self):
        x = m.AclMeter(0)
        frame = l2cap(0x40, SUSPEND_CMD)
        x.feed(m.MON_ACL_RX, acl(1, m.PB_START_NOFLUSH, frame[:5]), T0)
        self.assertIsNone(x.avdtp_report())
        x.feed(m.MON_ACL_RX, acl(1, m.PB_CONT, frame[5:]), T0 + MS)
        self.assertEqual(x.avdtp_report()["counts"], {"remote_suspend": 1})

    def test_real_captures_show_the_receiver_suspending_and_a_sinks_delay_report(self):
        # resource 1.2 (PulseAudio receiver): START by the host, SUSPEND by the receiving radio 5.8 s later; the capture is the
        # 40 packets either side of a stream restart, which holds neither, so rebuild the sequence from the recorded timings
        x = m.AclMeter(0)
        avdtp_frame(x, True, START_CMD, T0 + int(5.51 * SEC))
        avdtp_frame(x, False, SUSPEND_CMD, T0 + int(11.28 * SEC))
        e = x.avdtp_report()["events"]
        self.assertAlmostEqual(e[1]["t_s"] - e[0]["t_s"], 5.77, places=2)
        self.assertEqual(e[1]["from"], "remote")


class StreamControlInTheJob(unittest.TestCase):
    TAIL = ["a2dp", tcl.ADDR, "-t", "1", "--interval", "0.05"]
    SC = {"counts": {"host_start": 1, "remote_suspend": 2},
          "events": [{"t_s": 2.0, "from": "host", "signal": "START"}, {"t_s": 7.5, "from": "remote", "signal": "SUSPEND"},
                     {"t_s": 14.0, "from": "remote", "signal": "SUSPEND"}],
          "events_total": 3, "delay_reports_ms": None}

    def test_the_record_is_in_the_result_and_each_remote_suspend_is_announced(self):
        mod = fake_module(report=REPORT, interval=INTERVAL, avdtp=self.SC)
        rc, sm, ctx = tcl.run_job_ctx(self.TAIL, [None], drop_at=10 ** 6, acl_module=mod)
        self.assertEqual(sm["stream_control"]["counts"]["remote_suspend"], 2)
        events = [json.loads(l) for l in ctx["out"].splitlines() if l.startswith('{"m":"event"') and '"remote_suspend"' in l]
        self.assertEqual([e["at"] for e in events], [7.5, 14.0])
        self.assertIn("the receiving device suspended the stream (AVDTP SUSPEND)", ctx["out"])
        self.assertEqual(ctx["out"].count("the receiving device suspended the stream"), 1)    # said once, not at every sample

    def test_an_interrupted_stream_names_its_cause_in_the_failure(self):
        mod = fake_module(report=REPORT, interval=INTERVAL, avdtp=self.SC)
        rc, sm = tcl.run_job(self.TAIL + ["--min-active-pct", "101"], [None], drop_at=10 ** 6, acl_module=mod)
        self.assertEqual(rc, 2)
        text = " ".join(sm["fail_reasons"])
        self.assertIn("transport active only", text)
        self.assertIn("the receiving device sent AVDTP SUSPEND 2 time(s), first 7.5 s after monitoring began", text)

    def test_the_exact_suspended_time_decides_when_the_sampled_state_looks_fine(self):
        # sampled every few seconds the transport looked active 100 % of the time, but the receiver held the stream suspended 30 %
        sc = dict(self.SC, stream_window_s=40.0, suspended_s=12.0, suspended_pct=30.0)
        mod = fake_module(report=REPORT, interval=INTERVAL, avdtp=sc)
        rc, sm = tcl.run_job(self.TAIL + ["--min-active-pct", "80"], [None], drop_at=10 ** 6, acl_module=mod)
        self.assertEqual(rc, 2)
        self.assertEqual((sm["active_pct"], sm["active_pct_sampled"]), (70.0, 100.0))
        self.assertIn("transport active only 70.0% of the time (< 80.0%)", " ".join(sm["fail_reasons"]))

    def test_a_small_suspended_share_passes_and_both_figures_are_reported(self):
        sc = dict(self.SC, stream_window_s=40.0, suspended_s=2.0, suspended_pct=5.0)
        mod = fake_module(report=REPORT, interval=INTERVAL, avdtp=sc)
        rc, sm = tcl.run_job(["load", tcl.ADDR, "-t", "1", "--interval", "0.05"], [None], drop_at=10 ** 6, acl_module=mod)
        self.assertEqual(rc, 0, sm.get("fail_reasons"))
        self.assertEqual((sm["active_pct"], sm["active_pct_sampled"]), (95.0, 100.0))

    def test_without_stream_control_the_sampled_figure_stands(self):
        mod = fake_module(report=REPORT, interval=INTERVAL, avdtp=None)
        rc, sm = tcl.run_job(self.TAIL, [None], drop_at=10 ** 6, acl_module=mod)
        self.assertEqual(sm["active_pct"], sm["active_pct_sampled"])

    def test_no_suspend_no_hint(self):
        quiet = {"counts": {"host_start": 1}, "events": [{"t_s": 2.0, "from": "host", "signal": "START"}], "events_total": 1,
                 "delay_reports_ms": None}
        mod = fake_module(report=REPORT, interval=INTERVAL, avdtp=quiet)
        rc, sm = tcl.run_job(self.TAIL + ["--min-active-pct", "101"], [None], drop_at=10 ** 6, acl_module=mod)
        self.assertNotIn("AVDTP SUSPEND", " ".join(sm["fail_reasons"]))

    def test_the_sinks_delay_report_is_used_when_bluez_did_not_give_one(self):
        sc = dict(self.SC, delay_reports_ms={"last": 150.0, "min": 150.0, "max": 150.0, "n": 1})
        mod = fake_module(report=REPORT, interval=INTERVAL, avdtp=sc)
        rc, sm = tcl.run_job(self.TAIL, [None], drop_at=10 ** 6, acl_module=mod)
        d = sm["sink_reported_delay_ms"]
        self.assertEqual((d["last"], d["min"], d["max"]), (150.0, 150.0, 150.0))
        self.assertIn("DELAYREPORT", d["basis"])
        self.assertIn("not measured", d["basis"])

    def test_without_any_delay_report_there_is_none(self):
        mod = fake_module(report=REPORT, interval=INTERVAL, avdtp=None)
        rc, sm = tcl.run_job(self.TAIL, [None], drop_at=10 ** 6, acl_module=mod)
        self.assertIsNone(sm["sink_reported_delay_ms"])
        self.assertIsNone(sm["stream_control"])
        self.assertIsNone(sm["pulse_sink_resumes"])                                  # not a PulseAudio interference run

    def test_the_wording_helper(self):
        self.assertEqual(bc.remote_suspend_text(None), "")
        self.assertEqual(bc.remote_suspend_text({"counts": {"remote_suspend": 0}, "events": []}), "")
        self.assertIn("1 time(s)", bc.remote_suspend_text({"counts": {"remote_suspend": 1}, "events": []}))


if __name__ == "__main__":
    unittest.main()
