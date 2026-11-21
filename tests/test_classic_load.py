"""Realistic Classic interference ("load" mode of the A2DP job): AFH parsing, argument validation, and the
reconnect/verdict behaviour driven through the real job code with fake hardware (no radios, no audio, no root)."""
import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from unittest import mock
import _path  # noqa: F401
import bluforge_classic as bc
import server

ADDR = "AA:BB:CC:DD:EE:FF"


class AfhParser(unittest.TestCase):
    def test_all_channels_in_use(self):
        r = bc.parse_afh("AFH map: 0xffffffffffffffffff7f")
        self.assertEqual((r["enabled"], r["channels"], r["unused"]), (True, 79, []))

    def test_bit_order_is_channel_8i_plus_b(self):
        # octet 0 = 0x0f -> channels 0..3 used; everything else off
        r = bc.parse_afh("AFH map: 0x0f000000000000000000")
        self.assertEqual(r["channels"], 4)
        self.assertEqual(r["unused"][0], 4)
        self.assertEqual(len(r["unused"]), 75)

    def test_bits_above_channel_78_are_ignored(self):
        self.assertEqual(bc.parse_afh("AFH map: 0xffffffffffffffffffff")["channels"], 79)

    def test_afh_disabled_means_all_79(self):
        r = bc.parse_afh("AFH disabled")
        self.assertEqual((r["enabled"], r["channels"]), (False, 79))

    def test_garbage_and_errors_are_none(self):
        for t in ("", "HCI read AFH map request failed: Input/output error", "AFH map: 0xff", "no link"):
            self.assertIsNone(bc.parse_afh(t))


class LoadArguments(unittest.TestCase):
    def test_defaults_are_an_unattended_noise_stream(self):
        a = server.build_classic_argv("c_load", 1, {"addr": ADDR})
        self.assertEqual(a[a.index("load") + 1], ADDR)
        self.assertEqual(a[a.index("-t") + 1], "0")
        self.assertEqual(a[a.index("--source") + 1], "noise")

    def test_values_are_passed_through(self):
        a = server.build_classic_argv("c_load", 1, {"addr": ADDR.lower(), "duration": "3600", "source": "sweep", "codec": "AAC",
                                                    "latency": "150", "min_active_pct": "90"})
        self.assertIn(ADDR, a)
        self.assertEqual(a[a.index("-t") + 1], "3600")
        self.assertEqual(a[a.index("--codec") + 1], "aac")
        self.assertEqual(a[a.index("--latency") + 1], "150")
        self.assertEqual(a[a.index("--min-active-pct") + 1], "90.0")

    def test_bad_input_is_refused(self):
        for bad in ({"addr": "nope"}, {"addr": ADDR, "duration": "-1"}, {"addr": ADDR, "duration": "999999999"},
                    {"addr": ADDR, "codec": "a;b"}, {"addr": ADDR, "source": "file:/does/not/exist.wav"},
                    {"addr": ADDR, "source": "rm -rf"}, {"addr": ADDR, "min_active_pct": "101"},
                    {"addr": ADDR, "latency": "1"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                server.build_classic_argv("c_load", 1, bad)

    def test_it_is_a_classic_job_that_needs_a_reserved_radio(self):
        self.assertIn("c_load", server.CLASSIC_JOB_KINDS)
        self.assertNotIn("c_load", server.BLE_KINDS)

    def test_job_status_names_the_device_for_classic_jobs_only(self):
        st = server.Job("j", "c_load", 1, ["p", "-u", "c", "--hci", "1", "load", ADDR, "-t", "0"]).status()
        self.assertEqual(st["addr"], ADDR)
        self.assertIsNone(server.Job("j", "adv", 1, ["p", "-u", "c", "--dev", "1", "adv", "nonconn"]).status()["addr"])

    def test_cli_load_defaults(self):
        a = bc.build_parser().parse_args(["--hci", "1", "load", ADDR])
        self.assertTrue(a.load and a.reconnect)
        self.assertEqual((a.duration, a.source, a.min_active_pct), (0, "noise", 80.0))
        self.assertFalse(bc.build_parser().parse_args(["--hci", "1", "a2dp", ADDR]).load)


# ------------------------------------------------------------------ fake hardware
class FakeBZ:
    hci = 1
    adapter = "/org/bluez/hci1"

    def __init__(self):
        self.connected = True
        self.cb = None
        self.calls = []

    def dev_path(self, addr):
        return "/org/bluez/hci1/dev_" + addr.replace(":", "_")

    def get(self, path, iface, prop):
        return {"Paired": True, "Connected": self.connected}.get(prop, "x")

    def subscribe(self, cb):
        self.cb = cb

    def call(self, path, iface, method, params=None, timeout_ms=0):
        self.calls.append(method)
        if method == "Disconnect":
            self.connected = False

    def drop(self):
        self.connected = False
        self.cb("props", self.dev_path(ADDR), None, {"Connected": False})


class FakeAudio:
    ok = True
    user = "u"
    backend = "pipewire"
    card_misses = 0                 # card_for returns None this many times first (the post-pairing race)
    loop_results = [0]              # what pa_break_local_loops returns, call by call (the last one repeats)
    instances = []

    def __init__(self, *a):
        self.card_calls = 0
        self.loop_calls = 0
        self.state_calls = 0
        self.resumed = []
        self.drained = False
        FakeAudio.instances.append(self)

    def card_for(self, addr):
        self.card_calls += 1
        if self.card_calls <= type(self).card_misses:
            return None
        return {"name": "c", "profiles": {"a2dp-sink": {}}, "active_profile": "a2dp-sink"}

    def pa_break_local_loops(self, tags):
        r = type(self).loop_results[min(self.loop_calls, len(type(self).loop_results) - 1)]
        self.loop_calls += 1
        return r

    def pa_drain_cleanup(self):
        self.drained = True

    sink_states = ["RUNNING"]           # what pa_sink_state returns, call by call (the last one repeats)

    def pa_sink_state(self, name):
        i = min(self.state_calls, len(type(self).sink_states) - 1)
        self.state_calls += 1
        return type(self).sink_states[i]

    def pa_resume_sink(self, name):
        self.resumed.append(name)
        return 0

    def sink_for(self, addr):
        return {"name": "sink"}

    def set_profile(self, card, prof):
        return 0, ""


class FakePulseAudio(FakeAudio):
    backend = "pulse"


class FakeStreamer:
    error = None

    def __init__(self, *a, **k):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def alive(self):
        return True


def make_fake_xrun(errors):
    class FakeXrun:
        err = errors
        seen = True

        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

        def stop(self):
            pass
    return FakeXrun


class FakeKeys:
    dev = None

    def __init__(self, *a, **k):
        pass

    def start(self):
        pass

    def stop(self):
        pass


def run_job(argv_tail, connect_outcomes, drop_at=5, xruns=0, **kw):
    rc, sm, _ = run_job_ctx(argv_tail, connect_outcomes, drop_at, xruns, **kw)
    return rc, sm


def run_job_ctx(argv_tail, connect_outcomes, drop_at=5, xruns=0, audio_cls=FakeAudio, local=(), instant_waits=False,
                acl_module=None):
    """Run job_a2dp on fake hardware. connect_outcomes: one entry per do_connect call (the device starts connected, so the first call is the
    first RECONNECT attempt), an Exception to raise or None to succeed (the last entry repeats). Returns (exit code, summary dict)."""
    bz = FakeBZ()
    FakeAudio.instances = []
    calls = {"connect": 0, "pump": 0}

    def do_connect(b, addr, timeout, **kw):
        i = min(calls["connect"], len(connect_outcomes) - 1)
        calls["connect"] += 1
        if connect_outcomes[i] is not None:
            raise connect_outcomes[i]
        b.connected = True
        return 12.0

    def pump(sec):
        calls["pump"] += 1
        if calls["pump"] == drop_at:
            bz.drop()
        import time
        time.sleep(0.02)

    a = bc.build_parser().parse_args(["--hci", "1"] + argv_tail)
    tmp = tempfile.mkdtemp()
    out = io.StringIO()
    with contextlib.ExitStack() as st:
        st.enter_context(mock.patch.dict(sys.modules, {"acl_meter": acl_module}))     # None = the meter is unavailable
        if instant_waits:
            st.enter_context(mock.patch.object(bc, "wait_for", lambda pred, timeout, step=0.25: pred() or None))
        st.enter_context(mock.patch.object(bc, "local_adapter_tags", lambda b: set(local)))
        for name, val in (("Audio", audio_cls), ("Streamer", FakeStreamer), ("XrunMonitor", make_fake_xrun(xruns)),
                          ("KeyWatcher", FakeKeys), ("do_connect", do_connect), ("pump", pump),
                          ("ensure_powered", lambda *x: None), ("ensure_device", lambda *x: None),
                          ("hci_counters", lambda h: None), ("acl_links", lambda h: []),
                          ("link_stats", lambda h, ad: {"rssi": 0, "tx_power": None, "lq": 255}),
                          ("afh_info", lambda h, ad: {"enabled": True, "channels": 60, "map_hex": "ff" * 10, "unused": [1, 2]}),
                          ("transport_info", lambda b, ad: {"state": "active", "codec": "SBC", "volume": None}),
                          ("gen_pcm", lambda kind: b"\0\0\0\0"), ("LOG_DIR", tmp),
                          ("LOAD_RETRY_S", 0.05), ("AFH_EVERY_S", 0.05)):
            st.enter_context(mock.patch.object(bc, name, val))
        st.enter_context(mock.patch.object(bc.shutil, "which", lambda t: "/usr/bin/" + t))
        bc.STOP.clear()
        st.enter_context(contextlib.redirect_stdout(out))
        try:
            rc = bc.job_a2dp(a, bz)
        except bc.ClassicError as e:
            rc = e
    summary = None
    for line in out.getvalue().splitlines():
        if line.startswith('{"m":"summary"'):
            summary = json.loads(line)
    return rc, summary, {"bz": bz, "audio": FakeAudio.instances[0] if FakeAudio.instances else None, "out": out.getvalue()}


class LoadMode(unittest.TestCase):
    TAIL = ["load", ADDR, "-t", "2", "--interval", "0.02"]

    def test_failed_reconnect_is_retried_and_the_run_still_passes(self):
        rc, sm = run_job(self.TAIL, [bc.ClassicError("boom"), None], xruns=99)
        self.assertEqual(rc, 0, sm and sm.get("fail_reasons"))
        self.assertEqual(sm["test"], "load")
        self.assertEqual(sm["role"], "interference source")
        self.assertEqual(sm["reconnect_failures"], 1)
        self.assertEqual(len(sm["reconnect_ms"]), 1)
        self.assertGreaterEqual(sm["duration_s"], 1.9)                       # it ran to the end, it did not give up
        self.assertGreater(sm["active_pct"], 80)

    def test_load_mode_does_not_judge_the_device(self):
        rc, sm = run_job(self.TAIL, [None], xruns=500)                       # 500 xruns and a disconnect: not a failure here
        self.assertEqual(rc, 0, sm and sm.get("fail_reasons"))
        self.assertEqual(sm["xruns"], 500)

    def test_afh_channel_count_is_reported(self):
        rc, sm = run_job(self.TAIL, [None])
        self.assertEqual((sm["afh"]["channels_min"], sm["afh"]["channels_max"], sm["afh"]["enabled"]), (60, 60, True))

    def test_outage_counts_against_the_run(self):
        # the link drops early and never comes back: the interference was NOT delivered, and the run says so
        rc, sm = run_job(["load", ADDR, "-t", "1", "--interval", "0.02"], [bc.ClassicError("gone")], drop_at=4)
        self.assertEqual(rc, 2)
        self.assertLess(sm["active_pct"], 80)
        self.assertGreaterEqual(sm["reconnect_failures"], 2)
        self.assertGreaterEqual(sm["duration_s"], 0.9)                       # kept trying until the end
        self.assertIn("interference stream was active only", " ".join(sm["fail_reasons"]))

    def test_a2dp_test_still_fails_on_a_failed_reconnect(self):
        rc, sm = run_job(["a2dp", ADDR, "-t", "2", "--interval", "0.02", "--reconnect"], [bc.ClassicError("boom")])
        self.assertEqual(rc, 2)
        self.assertEqual(sm["test"], "a2dp")
        self.assertIn("reconnect failed", " ".join(sm["fail_reasons"]))

    def test_a2dp_test_still_judges_xruns(self):
        rc, sm = run_job(["a2dp", ADDR, "-t", "1", "--interval", "0.02"], [None], drop_at=10 ** 6, xruns=50)
        self.assertEqual(rc, 2)
        self.assertIn("xruns", " ".join(sm["fail_reasons"]))


PA_CARDS = """Card #0
\tName: alsa_card.pci-0000_00_1f.3
\tProfiles:
\t\toff: Off (sinks: 0, sources: 0, priority: 0, available: yes)
\tActive Profile: off
Card #2
\tName: bluez_card.80_84_89_08_E3_EF
\tDriver: module-bluez5-device.c
\tProfiles:
\t\ta2dp_sink: High Fidelity Playback (A2DP Sink) (sinks: 1, sources: 0, priority: 40, available: yes)
\t\ta2dp_source: High Fidelity Capture (A2DP Source) (sinks: 0, sources: 1, priority: 20, available: no)
\t\toff: Off (sinks: 0, sources: 0, priority: 0, available: yes)
\tActive Profile: a2dp_sink
\tPorts:
"""
TAG = "80_84_89_08_e3_ef"
# shapes captured from `pactl --format=json list ...` (PulseAudio 16.1) with two of this unit's radios linked
PA_SOURCES = [{"index": 4, "name": "bluez_source.80_84_89_08_E2_0A.a2dp_source", "owner_module": 27},
              {"index": 5, "name": "bluez_sink.80_84_89_08_E3_EF.a2dp_sink.monitor", "owner_module": 29}]
PA_OUTPUTS = [{"index": 1, "driver": "module-loopback.c", "owner_module": "28", "source": 4}]
PA_SINKS = [{"index": 3, "name": "bluez_sink.80_84_89_08_E3_EF.a2dp_sink", "owner_module": 29}]
PA_INPUTS = [{"index": 6, "driver": "module-loopback.c", "owner_module": "28", "sink": 3},
             {"index": 7, "driver": "protocol-native.c", "owner_module": "11", "sink": 3}]
LOCAL = {"80_84_89_08_e2_0a", "80_84_89_08_e3_ef"}


class PulseAudioBackend(unittest.TestCase):
    def test_card_parser(self):
        c = bc.parse_pa_card(PA_CARDS, TAG)
        self.assertEqual((c["name"], c["active_profile"], c["backend"]), ("bluez_card.80_84_89_08_E3_EF", "a2dp-sink", "pulse"))
        self.assertEqual(c["profiles"]["a2dp-sink"]["pa"], "a2dp_sink")
        self.assertFalse(c["profiles"]["a2dp-source"]["available"])
        self.assertIsNone(bc.parse_pa_card(PA_CARDS, "11_22_33_44_55_66"))
        self.assertIsNone(bc.parse_pa_card("", TAG))
        self.assertEqual(bc.pick_a2dp_profile(c), "a2dp-sink")             # the existing picker works on it unchanged

    def test_sink_parser(self):
        txt = "3\tbluez_sink.80_84_89_08_E3_EF.a2dp_sink\tmodule-bluez5-device.c\ts16le 2ch 44100Hz\tRUNNING\n0\talsa_output.x\tm\ts\tIDLE\n"
        self.assertEqual(bc.parse_pa_sinks(txt, TAG)["name"], "bluez_sink.80_84_89_08_E3_EF.a2dp_sink")
        self.assertIsNone(bc.parse_pa_sinks(txt, "11_22_33_44_55_66"))
        self.assertIsNone(bc.parse_pa_sinks("", TAG))

    def test_only_loopbacks_fed_by_our_own_radios_are_moved(self):
        self.assertEqual(bc.loopback_moves(PA_SOURCES, PA_OUTPUTS, PA_SINKS, PA_INPUTS, LOCAL, "bluforge_drain"), [6])

    def test_a_loopback_from_someone_elses_device_is_left_alone(self):
        src = [{"index": 4, "name": "bluez_source.11_22_33_44_55_66.a2dp_source", "owner_module": 27}]
        self.assertEqual(bc.loopback_moves(src, PA_OUTPUTS, PA_SINKS, PA_INPUTS, LOCAL, "bluforge_drain"), [])

    def test_already_parked_loopbacks_are_not_moved_again(self):
        sinks = PA_SINKS + [{"index": 9, "name": "bluforge_drain"}]
        inputs = [{"index": 6, "driver": "module-loopback.c", "owner_module": "28", "sink": 9}]
        self.assertEqual(bc.loopback_moves(PA_SOURCES, PA_OUTPUTS, sinks, inputs, LOCAL, "bluforge_drain"), [])

    def test_missing_or_odd_fields_never_raise(self):
        self.assertEqual(bc.loopback_moves([], [], [], [], LOCAL, "d"), [])
        self.assertEqual(bc.loopback_moves(PA_SOURCES, [{"source": 4, "owner_module": "n/a"}], PA_SINKS, PA_INPUTS, LOCAL, "d"), [])

    def test_local_adapter_tags_come_from_bluez(self):
        class B:
            def managed(self):
                return {"/org/bluez/hci0": {bc.IF_ADAPTER: {"Address": "80:84:89:08:E2:0A"}},
                        "/org/bluez/hci1": {bc.IF_ADAPTER: {"Address": "80:84:89:08:E3:EF"}},
                        "/org/bluez/hci0/dev_x": {bc.IF_DEVICE: {"Address": "11:22:33:44:55:66"}}}
        self.assertEqual(bc.local_adapter_tags(B()), LOCAL)

        class Broken:
            def managed(self):
                raise RuntimeError("no bus")
        self.assertEqual(bc.local_adapter_tags(Broken()), set())

    def test_playback_command_per_sound_server(self):
        a = bc.Audio.__new__(bc.Audio)
        a.backend = "pulse"
        cmd = a.playback_cmd("sink1", 48000, 2, 100, "n")
        self.assertEqual((cmd[0], cmd[cmd.index("--device") + 1], "--format=s16le" in cmd), ("pacat", "sink1", True))
        a.backend = "pipewire"
        self.assertEqual(a.playback_cmd("sink1", 48000, 2, 100, "n")[0], "pw-cat")


class InternalReceiver(unittest.TestCase):
    TAIL = ["load", ADDR, "-t", "1", "--interval", "0.02"]

    def setUp(self):
        FakeAudio.card_misses = 0
        FakeAudio.loop_results = [0]

    def test_loopback_is_parked_before_streaming_and_the_link_is_dropped_after(self):
        FakeAudio.loop_results = [1, 0]
        rc, sm, ctx = run_job_ctx(self.TAIL, [None], drop_at=10 ** 6, audio_cls=FakePulseAudio, local={ADDR.replace(":", "_").lower()})
        self.assertEqual(rc, 0, sm and sm.get("fail_reasons"))
        self.assertTrue(sm["internal_receiver"])
        self.assertEqual((sm["sound_server"], sm["xruns_measured"]), ("pulse", False))
        self.assertIn("parked 1 PulseAudio loopback", ctx["out"])
        self.assertGreater(ctx["audio"].loop_calls, 1)                    # and it keeps checking while streaming
        self.assertIn("Disconnect", ctx["bz"].calls)                      # an internal receiver never stays connected
        self.assertTrue(ctx["audio"].drained)

    def test_it_refuses_to_stream_when_the_loop_cannot_be_prevented(self):
        FakeAudio.loop_results = [None]
        rc, sm, ctx = run_job_ctx(self.TAIL, [None], drop_at=10 ** 6, audio_cls=FakePulseAudio, local={ADDR.replace(":", "_").lower()})
        self.assertIsInstance(rc, bc.ClassicError)
        self.assertIn("feedback loop", str(rc))
        self.assertIn("Disconnect", ctx["bz"].calls)                      # nothing left connected to start looping
        self.assertNotIn("streaming noise", ctx["out"])

    def test_no_loop_guard_for_a_real_device(self):
        rc, sm, ctx = run_job_ctx(self.TAIL, [None], drop_at=10 ** 6, audio_cls=FakePulseAudio, local={"11_22_33_44_55_66"})
        self.assertEqual(rc, 0)
        self.assertFalse(sm["internal_receiver"])
        self.assertEqual(ctx["audio"].loop_calls, 0)
        self.assertNotIn("Disconnect", ctx["bz"].calls)

    def test_no_loop_guard_on_pipewire(self):
        rc, sm, ctx = run_job_ctx(self.TAIL, [None], drop_at=10 ** 6, audio_cls=FakeAudio, local={ADDR.replace(":", "_").lower()})
        self.assertEqual(rc, 0)
        self.assertEqual((sm["sound_server"], sm["xruns_measured"]), ("pipewire", True))
        self.assertEqual(ctx["audio"].loop_calls, 0)

    def test_the_a2dp_test_is_refused_on_pulseaudio(self):
        rc, sm, ctx = run_job_ctx(["a2dp", ADDR, "-t", "1", "--interval", "0.02"], [None], audio_cls=FakePulseAudio)
        self.assertIsInstance(rc, bc.ClassicError)
        self.assertIn("PulseAudio", str(rc))

    def test_a_failed_start_never_leaves_an_internal_receiver_connected(self):
        # a bad audio file fails AFTER the link is up and the loopback parked: the pair must still be dropped
        rc, sm, ctx = run_job_ctx(["load", ADDR, "-t", "1", "--source", "file:/no/such/file.wav", "--interval", "0.02"],
                                  [None], drop_at=10 ** 6, audio_cls=FakePulseAudio, local={ADDR.replace(":", "_").lower()})
        self.assertIsInstance(rc, bc.ClassicError)
        self.assertIn("Disconnect", ctx["bz"].calls)
        self.assertTrue(ctx["audio"].drained)

    def test_a_failed_bring_up_drops_an_internal_receiver(self):
        FakeAudio.card_misses = 99
        rc, sm, ctx = run_job_ctx(self.TAIL, [None], drop_at=10 ** 6, audio_cls=FakePulseAudio,
                                  local={ADDR.replace(":", "_").lower()}, instant_waits=True)
        self.assertIsInstance(rc, bc.ClassicError)
        self.assertEqual(ctx["bz"].calls[-1], "Disconnect")

    def test_codec_choice_on_pulseaudio_falls_back_to_the_negotiated_one(self):
        card = bc.parse_pa_card(PA_CARDS, TAG)
        self.assertEqual(bc.pick_a2dp_profile(card, "sbc"), "a2dp-sink")
        self.assertEqual(bc.pick_a2dp_profile(card, "aac"), "a2dp-sink")
        pw = {"backend": "pipewire", "profiles": {"a2dp-sink": {}, "a2dp-sink-sbc": {}, "a2dp-sink-aac": {}}}
        self.assertEqual(bc.pick_a2dp_profile(pw, "aac"), "a2dp-sink-aac")      # PipeWire still honours the choice
        with self.assertRaises(bc.ClassicError):
            bc.pick_a2dp_profile({"backend": "pipewire", "profiles": {"a2dp-sink": {}}}, "ldac")

    def test_a_missing_audio_card_gets_one_clean_reconnect(self):
        FakeAudio.card_misses = 1
        rc, sm, ctx = run_job_ctx(self.TAIL, [None], drop_at=10 ** 6, instant_waits=True)
        self.assertEqual(rc, 0, sm and sm.get("fail_reasons"))
        self.assertIn("reconnecting once", ctx["out"])
        self.assertIn("Disconnect", ctx["bz"].calls)

    def test_it_gives_up_after_that_one_retry(self):
        FakeAudio.card_misses = 99
        rc, sm, ctx = run_job_ctx(self.TAIL, [None], drop_at=10 ** 6, instant_waits=True)
        self.assertIsInstance(rc, bc.ClassicError)
        self.assertIn("no audio card", str(rc))


ADDR_IN_CARDS = "80:84:89:08:E3:EF"             # the radio whose card PA_CARDS lists


class SoundServerChoice(unittest.TestCase):
    """The PulseAudio engine may only be chosen on a unit that really runs PulseAudio. On PipeWire's compatibility server the
    Bluetooth card shows up in `pactl` and in `pw-dump` a moment apart; asking pactl when pw-dump has just missed the card made
    about every second run pick the PulseAudio engine and refuse to run (seen on this unit once the connect got faster)."""

    PW_INFO = "Server String: /run/user/1000/pulse/native\nServer Name: PulseAudio (on PipeWire 1.0.9)\nServer Version: 15.0.0\n"
    PA_INFO = "Server String: /run/user/1000/pulse/native\nServer Name: pulseaudio\nServer Version: 16.1\n"

    def audio(self, info, rc_info=0):
        a = bc.Audio.__new__(bc.Audio)
        a.user, a.uid, a.backend, a.drain_module, a._pa_pw = "u", 1000, None, None, None
        calls = []

        def run(cmd, timeout=8):
            calls.append(cmd[:3])
            if cmd[0] == "pw-dump":
                return 0, "[]"                                            # PipeWire does not (yet) have the card
            if cmd[:2] == ["pactl", "info"]:
                return rc_info, info
            if cmd[:3] == ["pactl", "list", "cards"]:
                return 0, PA_CARDS                                        # ... but pactl already lists it
            return 1, ""
        a.run = run
        return a, calls

    def test_on_pipewire_a_card_not_yet_in_the_graph_is_not_found_instead_of_taken_from_pulse(self):
        a, calls = self.audio(self.PW_INFO)
        self.assertIsNone(a.card_for(ADDR_IN_CARDS))
        self.assertIsNone(a.backend)                                      # no engine chosen from a half-seen state
        self.assertNotIn(["pactl", "list", "cards"], calls)

    def test_on_real_pulseaudio_the_pulse_card_is_used(self):
        a, _calls = self.audio(self.PA_INFO)
        c = a.card_for(ADDR_IN_CARDS)
        self.assertEqual((c["backend"], a.backend), ("pulse", "pulse"))

    def test_when_the_server_cannot_be_asked_the_older_behaviour_stands(self):
        a, _calls = self.audio("", rc_info=1)
        self.assertEqual(a.card_for(ADDR_IN_CARDS)["backend"], "pulse")

    def test_the_answer_is_asked_once(self):
        a, calls = self.audio(self.PW_INFO)
        for _ in range(3):
            a.card_for(ADDR_IN_CARDS)
        self.assertEqual(sum(1 for c in calls if c[:2] == ["pactl", "info"]), 1)


class PulseSinkKeepAlive(unittest.TestCase):
    """PulseAudio + internal receiver: the receiving radio's device is not an audio device for PulseAudio, BlueZ there suspends the
    stream about 5 s after every START and the sender's sink goes SUSPENDED. The interference stream resumes it and counts the resumes."""

    class Stub:
        def __init__(self, states, fail_first=False):
            self.states, self.calls, self.resumed, self.fail_first = list(states), 0, [], fail_first

        def pa_sink_state(self, name):
            self.calls += 1
            if self.fail_first and self.calls == 1:
                raise OSError("pactl hiccup")
            return self.states[min(self.calls - 1, len(self.states) - 1)]

        def pa_resume_sink(self, name):
            self.resumed.append(name)
            return 0

    def run_keeper(self, stub, seconds=0.25, sink="bluez_sink.X"):
        k = bc.PulseSinkKeeper(stub, period=0.01)
        k.sink = sink
        k.start()
        time.sleep(seconds)
        k.stop()
        return k

    def test_a_suspended_sink_is_resumed_and_counted(self):
        stub = self.Stub(["RUNNING", "SUSPENDED", "RUNNING"])
        k = self.run_keeper(stub)
        self.assertEqual(stub.resumed, ["bluez_sink.X"])
        self.assertEqual(len(k.resumes), 1)

    def test_nothing_is_touched_while_the_sink_runs_or_idles(self):
        for state in ("RUNNING", "IDLE", None):
            stub = self.Stub([state])
            k = self.run_keeper(stub, 0.1)
            self.assertEqual((stub.resumed, k.resumes), ([], []), state)

    def test_nothing_happens_before_a_sink_is_named(self):
        stub = self.Stub(["SUSPENDED"])
        k = bc.PulseSinkKeeper(stub, period=0.01)
        k.start()
        time.sleep(0.1)
        k.stop()
        self.assertEqual((stub.calls, stub.resumed), (0, []))

    def test_a_failed_look_or_resume_does_not_end_the_keeper(self):
        stub = self.Stub(["SUSPENDED", "SUSPENDED"], fail_first=True)
        k = self.run_keeper(stub, 0.15)
        self.assertGreaterEqual(k.errors, 1)
        self.assertGreaterEqual(len(k.resumes), 1)                      # it carried on after the first failure
        self.assertFalse(k.is_alive())

        class Refusing(self.Stub):
            def pa_resume_sink(self, name):
                return 1
        stub = Refusing(["SUSPENDED"])
        k = self.run_keeper(stub, 0.1)
        self.assertEqual((len(k.resumes), k.errors > 0), (0, True))      # a refusal is not counted as a resume

    def test_stop_returns_promptly(self):
        k = bc.PulseSinkKeeper(self.Stub(["RUNNING"]), period=30)       # a long period must not delay stopping
        k.sink = "x"
        k.start()
        t0 = time.time()
        k.stop()
        self.assertLess(time.time() - t0, 2)

    LOAD = ["load", ADDR, "-t", "1", "--interval", "0.05"]

    def run_load(self, audio_cls, states):
        audio_cls.sink_states = states
        try:
            with mock.patch.object(bc, "PA_KEEP_PERIOD_S", 0.02):
                return run_job_ctx(self.LOAD, [None], drop_at=10 ** 6, audio_cls=audio_cls)
        finally:
            audio_cls.sink_states = ["RUNNING"]

    def test_the_interference_stream_on_pulseaudio_resumes_the_sink_and_reports_it(self):
        rc, sm, ctx = self.run_load(FakePulseAudio, ["SUSPENDED", "RUNNING", "RUNNING", "SUSPENDED", "RUNNING"])
        self.assertEqual(rc, 0, sm and sm.get("fail_reasons"))
        self.assertGreaterEqual(sm["pulse_sink_resumes"], 1)
        self.assertEqual(set(ctx["audio"].resumed), {"sink"})

    def test_on_pipewire_there_is_no_keeper(self):
        rc, sm, ctx = self.run_load(FakeAudio, ["SUSPENDED"])
        self.assertIsNone(sm["pulse_sink_resumes"])
        self.assertEqual(ctx["audio"].resumed, [])

    def test_a_pulseaudio_run_that_never_suspends_reports_zero_resumes(self):
        rc, sm, ctx = self.run_load(FakePulseAudio, ["RUNNING"])
        self.assertEqual(sm["pulse_sink_resumes"], 0)

    def test_the_sink_state_parser(self):
        text = ("0\talsa_output.pci-0000_00_1f.3.analog-stereo\tmodule-alsa-card.c\ts16le 2ch 44100Hz\tSUSPENDED\n"
                "17\tbluez_sink.80_84_89_08_E3_EF.a2dp_sink\tmodule-bluez5-device.c\ts16le 2ch 44100Hz\tRunning\n")
        self.assertEqual(bc.parse_pa_sink_state(text, "bluez_sink.80_84_89_08_E3_EF.a2dp_sink"), "RUNNING")
        self.assertEqual(bc.parse_pa_sink_state(text, "alsa_output.pci-0000_00_1f.3.analog-stereo"), "SUSPENDED")
        self.assertIsNone(bc.parse_pa_sink_state(text, "bluez_sink.nope"))
        self.assertIsNone(bc.parse_pa_sink_state("", "x"))
        self.assertIsNone(bc.parse_pa_sink_state("garbage line without tabs", "garbage"))


class ConnectFallback(unittest.TestCase):
    """Device.Connect over the LE bearer times out on a dual-mode device; do_connect must force BR/EDR and retry."""

    class BZ:
        def __init__(self, connect_results, uuids=None, profile_connects=True):
            self.connect_results = list(connect_results)
            self.calls = []
            self.connected = False
            self.uuids = uuids
            self.profile_connects = profile_connects

        def dev_path(self, addr):
            return "/dev"

        def get(self, path, iface, name):
            if name == "UUIDs":
                return self.uuids
            return self.connected

        def call_loop(self, path, iface, method, params, timeout_ms):
            self.calls.append(method)
            if method == "Disconnect":     # cancels the pending LE attempt; nothing to connect to yet
                return None
            if method == "ConnectProfile":
                if self.profile_connects:
                    self.connected = True  # the ACL comes up even though the profile is refused
                raise bc.ClassicError("br-connection-unknown")
            res = self.connect_results.pop(0)
            if isinstance(res, str):
                raise bc.ClassicError(res)
            self.connected = True

    def run_connect(self, bz, **kw):
        with mock.patch.object(bc, "say"):
            return bc.do_connect(bz, "80:84:89:08:E3:EF", 30, **kw)

    SINK = ["0000110b-0000-1000-8000-00805f9b34fb", "0000110e-0000-1000-8000-00805f9b34fb"]

    def test_audio_tests_force_bredr_first_so_a_dual_mode_device_is_not_tried_over_le(self):
        bz = self.BZ([None], uuids=self.SINK)                                   # a plain Connect would be refused: LE bearer
        self.run_connect(bz, prefer_bredr=True)
        self.assertEqual(bz.calls, ["ConnectProfile"])

    def test_no_plain_connect_follows_a_successful_profile_connect(self):
        # A plain Connect on a dual-mode device that is already up makes BlueZ start an LE connection scan that stays on for the
        # whole stream and degrades the Classic link (jitter 2.3 -> 3.7 ms, worst packet 18 -> 40-100 ms on an idle pair).
        bz = self.BZ(["must not be called"], uuids=self.SINK)
        self.run_connect(bz, prefer_bredr=True)
        self.assertNotIn("Connect", bz.calls)

    def test_a_refused_profile_falls_back_to_the_generic_path(self):
        bz = self.BZ([None], uuids=self.SINK, profile_connects=False)
        self.run_connect(bz, prefer_bredr=True)
        self.assertEqual(bz.calls, ["ConnectProfile", "Connect"])                # generic Connect after the refused profile

    def test_a_device_without_the_a2dp_sink_uses_the_generic_connect(self):
        for uuids in (None, [], ["0000110e-0000-1000-8000-00805f9b34fb"], "x"):
            bz = self.BZ([None], uuids=uuids)
            self.run_connect(bz, prefer_bredr=True)
            self.assertEqual(bz.calls, ["Connect"], uuids)

    def test_the_default_is_unchanged(self):
        bz = self.BZ([None], uuids=self.SINK)
        self.run_connect(bz)
        self.assertEqual(bz.calls, ["Connect"])

    def test_an_already_connected_device_is_left_alone(self):
        bz = self.BZ([None], uuids=self.SINK)
        bz.connected = True
        self.assertEqual(self.run_connect(bz, prefer_bredr=True), 0.0)
        self.assertEqual(bz.calls, [])

    def test_first_try_works(self):
        bz = self.BZ([None])
        self.run_connect(bz)
        self.assertEqual(bz.calls, ["Connect"])

    def test_timeout_forces_bredr_then_connect_finds_link_up(self):
        bz = self.BZ(["g-io-error-quark: Timeout was reached (24)"])
        self.run_connect(bz)
        # the pending LE attempt must be cancelled before BR/EDR is forced, or BlueZ answers "Operation already in progress"
        self.assertEqual(bz.calls, ["Connect", "Disconnect", "ConnectProfile"])

    def test_still_failing_raises(self):
        bz = self.BZ(["Timeout", "Timeout"])
        bz.call_loop_orig = bz.call_loop

        def no_link(path, iface, method, params, timeout_ms):
            if method == "ConnectProfile":
                bz.calls.append(method)
                raise bc.ClassicError("br-connection-unknown")
            return bz.call_loop_orig(path, iface, method, params, timeout_ms)
        bz.call_loop = no_link
        with self.assertRaises(bc.ClassicError):
            self.run_connect(bz)
        self.assertEqual(bz.calls, ["Connect", "Disconnect", "ConnectProfile", "Connect"])


class LinkLocalArguments(unittest.TestCase):
    def test_argv(self):
        a = server.build_classic_argv("link-local", 0, {"peer": "1"})
        self.assertEqual(a[a.index("link-local"):], ["link-local", "1"])

    def test_bad_receivers_are_refused(self):
        for bad in ({}, {"peer": "0"}, {"peer": "x"}, {"peer": "99"}, {"peer": "-1"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                server.build_classic_argv("link-local", 0, bad)

    def test_server_checks_the_receiver_radio(self):
        with mock.patch.object(server, "all_hci", lambda: [0, 1, 2]):
            server.JOBS.reserved = {0}
            code, r = server.run_classic_action(0, "link-local", {"peer": 1})
            self.assertEqual(code, 409)                                   # receiver not reserved for Classic
            self.assertIn("not reserved", r["error"])
            code, r = server.run_classic_action(0, "link-local", {"peer": 0})
            self.assertEqual(code, 400)                                   # same radio
            code, r = server.run_classic_action(0, "link-local", {"peer": 7})
            self.assertEqual(code, 400)                                   # not a controller (builder refuses 99; 7 is absent)
            server.JOBS.reserved = set()


if __name__ == "__main__":
    unittest.main()
