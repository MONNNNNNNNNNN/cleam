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
        failed = (1, "System Protection is off")
        with mock.patch.object(cli.snapshot, "create", return_value=failed), contextlib.redirect_stderr(io.StringIO()):
            code, _ = self.run_cli("clean", "--yes", "--snapshot")
        self.assertEqual(code, 1)
        self.assertTrue((self.root / "old.tmp").exists())

    def test_scan_json(self):
        _, out = self.run_cli("scan", "--json")
        self.assertEqual(json.loads(out)[0]["bytes"], 100)

    def test_bare_clean_leaves_opt_in_targets_alone(self):
        bin_ = Path(tempfile.mkdtemp())
        (bin_ / "deleted.docx").write_bytes(b"x")
        both = [junk.Target("t", "test", (self.root,)),
                junk.Target("bin", "bin", (bin_,), mode="entries", min_age_hours=0, opt_in=True)]
        with mock.patch.object(junk, "targets", return_value=both):
            self.run_cli("clean", "--yes")
            self.assertTrue((bin_ / "deleted.docx").exists())
            self.run_cli("clean", "--yes", "--only", "bin")
            self.assertFalse((bin_ / "deleted.docx").exists())

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


class Startup(unittest.TestCase):
    TASK = "\\GoogleUpdaterTaskSystem142.0"

    def setUp(self):
        from cleam import repair, security

        items = [security.StartupItem("GoogleUpdaterTaskSystem142.0", "updater.exe", "Scheduled task",
                                      kind="task", key=self.TASK),
                 security.StartupItem("Malwarebytes Service", "mb.exe", "Service", kind="service", key="MBAMService")]
        patches = [mock.patch.object(security, "startup", return_value=items),
                   mock.patch("cleam.debloat.load_journal", return_value={"tweaks": {}, "apps": {}}),
                   mock.patch.object(repair, "run", return_value=(True, "done"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.ran = repair.run

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_list_shows_ids(self):
        code, out = self.run_cli("startup")
        self.assertEqual(code, 0)
        self.assertIn(f"id: task:{self.TASK}", out)

    def test_off_without_yes_is_a_dry_run(self):
        code, out = self.run_cli("startup", "--off", f"task:{self.TASK}")
        self.assertEqual(code, 0)
        self.assertIn(f"scheduled task {self.TASK}: Disabled", out)
        self.assertIn("dry run", out)
        self.ran.assert_not_called()

    def test_off_yes_runs_the_fix(self):
        code, out = self.run_cli("startup", "--off", f"TASK:{self.TASK.lower()}", "--yes")  # ids ignore case
        self.assertEqual(code, 0)
        self.ran.assert_called_once()

    def test_protective_and_unknown_entries_are_refused(self):
        with self.assertRaises(SystemExit) as e:
            self.run_cli("startup", "--off", "service:MBAMService", "--yes")
        self.assertIn("protective", str(e.exception))
        with self.assertRaises(SystemExit):
            self.run_cli("startup", "--off", "task:\\nope")
        self.ran.assert_not_called()
