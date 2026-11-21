import contextlib
import io
import json
import os
import signal
import tempfile
import unittest
import _path  # noqa: F401
import bluforge_cli as cli

os.environ.setdefault("BLUFORGE_LOGS", tempfile.mkdtemp())
import bluforge_classic as bc


class FakeHci:
    def __init__(self, restore=(True, "ok")):
        self.restore, self.closed = restore, 0
    def close(self):
        self.closed += 1
        return self.restore


class RadioGuardTests(unittest.TestCase):
    def run_report(self, restore):
        g = cli.RadioGuard(FakeHci(restore), 1, disable=lambda: None)
        g.close()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            g.report("advertising stopped")
        return g, out.getvalue() + err.getvalue()

    def test_restored_message_only_when_the_restore_worked(self):
        g, text = self.run_report((True, "ok"))
        self.assertTrue(g.restored())
        self.assertIn("hci1 restored", text)

    def test_failed_restore_is_reported_with_the_recovery_command(self):
        g, text = self.run_report((False, "mgmt power on failed"))
        self.assertFalse(g.restored())
        self.assertIn("NOT confirmed restored", text)
        self.assertIn("btmgmt --index 1 power on", text)
        self.assertNotIn("hci1 restored", text)

    def test_close_runs_once_and_disable_exceptions_do_not_block_the_restore(self):
        h = FakeHci()
        def boom():
            raise RuntimeError("radio gone")
        g = cli.RadioGuard(h, 1, disable=boom)
        g.close(); g.close(); g.close()
        self.assertEqual(h.closed, 1)
        self.assertTrue(g.restored())

    def test_close_exception_becomes_a_failed_result_not_a_crash(self):
        class Bad:
            def close(self): raise OSError("usb gone")
        g = cli.RadioGuard(Bad(), 1)
        g.close()
        self.assertFalse(g.restored())

    def test_second_signal_during_restore_is_ignored(self):
        calls = []
        g = cli.RadioGuard(FakeHci(), 1)
        old = signal.getsignal(signal.SIGUSR1)
        try:
            g.install(lambda: calls.append(1))
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)
            g.closing = True                         # a restore is in progress
            handler(signal.SIGTERM, None)
            self.assertEqual(calls, [1])
        finally:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGUSR1, old)


class ResultFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = bc.LOG_DIR
        bc.LOG_DIR = self.tmp.name

    def tearDown(self):
        bc.LOG_DIR = self.old
        self.tmp.cleanup()

    def test_saved_as_is_in_both_the_live_summary_and_the_saved_file(self):
        rec = bc.Recorder("cycle", "AA:BB:CC:DD:EE:FF", 0)
        rec.sample(n=1, ok=True, connect_ms=5)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bc.verdict(rec, {"test": "cycle", "duration_s": 1.0}, [])
        self.assertEqual(rc, 0)
        live = next(json.loads(l) for l in buf.getvalue().splitlines() if l.startswith('{"m":"summary"') or l.startswith('{"m": "summary"'))
        self.assertIn("saved_as", live)
        saved = json.load(open(live["saved_as"] + ".json"))
        self.assertEqual(saved["summary"]["saved_as"], live["saved_as"])
        self.assertTrue(os.path.exists(live["saved_as"] + ".csv"))

    def test_failed_save_does_not_raise_and_does_not_claim_a_file(self):
        bc.LOG_DIR = "/proc/definitely/not/writable"
        rec = bc.Recorder("cycle", "AA:BB:CC:DD:EE:FF", 0)
        summary = {"test": "cycle"}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            bc.verdict(rec, summary, ["x"])
        self.assertNotIn("saved_as", summary)

    def test_stats_helper_handles_empty_and_none(self):
        self.assertIsNotNone(bc.stats([1, 2, 3]))
        bc.stats([])
        bc.stats([None, None])


if __name__ == "__main__":
    unittest.main()
