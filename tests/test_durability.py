import contextlib
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock
import _path  # noqa: F401
import durable

os.environ.setdefault("BLUFORGE_LOGS", tempfile.mkdtemp())
import bluforge_classic as bc
import server
from version import RESULT_SCHEMA, __version__


class AtomicWrites(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "data.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_and_sets_mode(self):
        durable.atomic_write_bytes(self.path, b"hello", 0o600)
        self.assertEqual(open(self.path, "rb").read(), b"hello")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_a_failure_mid_write_leaves_the_previous_file_intact_and_no_litter(self):
        durable.atomic_write_bytes(self.path, b"OLD COMPLETE CONTENT")
        with mock.patch("durable.os.fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                durable.atomic_write_bytes(self.path, b"NEW CONTENT THAT NEVER FINISHED")
        self.assertEqual(open(self.path, "rb").read(), b"OLD COMPLETE CONTENT")
        self.assertEqual([n for n in os.listdir(self.tmp.name) if n.startswith(".tmp-")], [])

    def test_a_crash_before_the_rename_never_exposes_a_partial_file(self):
        with mock.patch("durable.os.replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                durable.atomic_write_bytes(self.path, b"data")
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_stale_temp_files_are_swept_but_real_files_are_not(self):
        durable.atomic_write_bytes(self.path, b"keep me")
        stale = os.path.join(self.tmp.name, ".tmp-leftoverdata.json")
        fresh = os.path.join(self.tmp.name, ".tmp-inprogress.json")
        open(stale, "w").close()
        open(fresh, "w").close()
        old = time.time() - 7200
        os.utime(stale, (old, old))
        self.assertEqual(durable.sweep_temp_files(self.tmp.name), 1)
        self.assertTrue(os.path.exists(self.path) and os.path.exists(fresh) and not os.path.exists(stale))


class ResultIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = bc.LOG_DIR
        bc.LOG_DIR = self.tmp.name

    def tearDown(self):
        bc.LOG_DIR = self.old
        self.tmp.cleanup()

    def save(self, samples=3):
        rec = bc.Recorder("a2dp", "AA:BB:CC:DD:EE:FF", 0)
        for i in range(samples):
            rec.sample(state="active", tx_kbps=137.0 + i)
        summary = {"test": "a2dp", "duration_s": 3.0}
        with contextlib.redirect_stdout(io.StringIO()):
            bc.verdict(rec, summary, [])
        return summary

    def test_checksum_matches_the_csv_on_disk(self):
        s = self.save()
        csv_bytes = open(s["saved_as"] + ".csv", "rb").read()
        self.assertEqual(s["csv_sha256"], hashlib.sha256(csv_bytes).hexdigest())
        self.assertEqual(s["csv_rows"], 3)
        self.assertEqual(csv_bytes.count(b"\n"), 4)                      # header + 3 rows

    def test_a_modified_csv_no_longer_matches_its_recorded_checksum(self):
        s = self.save()
        open(s["saved_as"] + ".csv", "ab").write(b"tampered\n")
        self.assertNotEqual(s["csv_sha256"], hashlib.sha256(open(s["saved_as"] + ".csv", "rb").read()).hexdigest())

    def test_file_carries_schema_version_and_provenance(self):
        s = self.save()
        doc = json.load(open(s["saved_as"] + ".json"))
        self.assertEqual(doc["schema"], RESULT_SCHEMA)
        self.assertEqual(doc["tool"]["version"], __version__)
        for k in ("schema", "tool_version", "started_at", "finished_at", "host", "kernel", "saved_as", "csv_sha256"):
            self.assertIn(k, doc["summary"], k)
        self.assertEqual(len(doc["samples"]), 3)
        self.assertTrue(doc["summary"]["started_at"] <= doc["summary"]["finished_at"])

    def test_no_samples_means_no_csv_and_no_dangling_checksum(self):
        s = self.save(samples=0)
        self.assertFalse(os.path.exists(s["saved_as"] + ".csv"))
        self.assertNotIn("csv_sha256", s)
        self.assertTrue(os.path.exists(s["saved_as"] + ".json"))

    def test_unserialisable_values_do_not_lose_the_result(self):
        rec = bc.Recorder("a2dp", "AA:BB:CC:DD:EE:FF", 0)
        summary = {"test": "a2dp", "odd": {1, 2, 3}, "raw": b"\x00\x01"}
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                bc.verdict(rec, summary, [])
            except TypeError:
                pass
        self.assertTrue(any(n.endswith(".json") for n in os.listdir(os.path.join(self.tmp.name, "classic"))))

    def test_listing_ignores_in_progress_temp_files(self):
        d = os.path.join(self.tmp.name, "classic")
        s = self.save()
        open(os.path.join(d, ".tmp-xyz_a2dp.json"), "w").write("{partial")
        old = server.RESULTS_DIR
        server.RESULTS_DIR = d
        try:
            self.assertEqual(len(server.classic_results()), 1)
        finally:
            server.RESULTS_DIR = old


if __name__ == "__main__":
    unittest.main()
