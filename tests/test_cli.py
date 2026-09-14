import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cleam import cli, junk


class Clean(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        f = self.root / "old.tmp"
        f.write_bytes(b"x" * 100)
        old = time.time() - 30 * 86400
        os.utime(f, (old, old))
        patcher = mock.patch.object(junk, "targets", return_value=[junk.Target("t", "test", (self.root,))])
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_clean_without_yes_is_a_dry_run(self):
        code, out = self.run_cli("clean")
        self.assertEqual(code, 0)
        self.assertIn("Dry run", out)
        self.assertTrue((self.root / "old.tmp").exists())

    def test_clean_yes_deletes(self):
        code, _ = self.run_cli("clean", "--yes")
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "old.tmp").exists())

    def test_failed_snapshot_aborts_clean(self):
        with mock.patch.object(cli.snapshot, "create", return_value=1), contextlib.redirect_stderr(io.StringIO()):
            code, _ = self.run_cli("clean", "--yes", "--snapshot")
        self.assertEqual(code, 1)
        self.assertTrue((self.root / "old.tmp").exists())

    def test_scan_json(self):
        _, out = self.run_cli("scan", "--json")
        self.assertEqual(json.loads(out)[0]["bytes"], 100)

    def test_unknown_target_is_rejected(self):
        with self.assertRaises(SystemExit):
            cli.main(["scan", "--only", "nope"])


class Human(unittest.TestCase):
    def test_units(self):
        self.assertEqual(cli.human(512), "512 B")
        self.assertEqual(cli.human(1536), "1.5 KiB")
        self.assertEqual(cli.human(3 * 1024**4), "3072.0 GiB")


if __name__ == "__main__":
    unittest.main()
