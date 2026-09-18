import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cleam import junk

OLD = time.time() - 30 * 86400


def touch(path: Path, size: int = 10, old: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if old:
        os.utime(path, (OLD, OLD))
    return path


class FilesMode(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.target = junk.Target("t", "test", (self.root,), keep=("keepme",))

    def test_scan_counts_old_files_and_deletes_nothing(self):
        touch(self.root / "a", 10)
        touch(self.root / "sub" / "b", 20)
        res = junk.run(self.target)
        self.assertEqual((res.files, res.bytes, res.errors), (2, 30, 0))
        self.assertTrue((self.root / "a").exists())

    def test_clean_deletes_old_keeps_new_and_keeps_dirs(self):
        touch(self.root / "sub" / "old")
        touch(self.root / "sub" / "new", old=False)
        res = junk.run(self.target, delete=True)
        self.assertEqual(res.files, 1)
        self.assertFalse((self.root / "sub" / "old").exists())
        self.assertTrue((self.root / "sub" / "new").exists())
        self.assertTrue((self.root / "sub").is_dir())

    def test_keep_names_are_untouched(self):
        touch(self.root / "keepme" / "model.bin")
        touch(self.root / "keepme2")
        junk.run(self.target, delete=True)
        self.assertTrue((self.root / "keepme" / "model.bin").exists())
        self.assertFalse((self.root / "keepme2").exists())

    def test_symlinks_are_never_followed(self):
        outside = touch(self.tmp / "outside" / "precious")
        try:
            os.symlink(self.tmp / "outside", self.root / "dirlink", target_is_directory=True)
            os.symlink(outside, self.root / "filelink")
        except OSError:
            self.skipTest("symlinks not permitted here")
        res = junk.run(self.target, delete=True)
        self.assertEqual(res.files, 0)
        self.assertTrue(outside.exists())
        self.assertTrue(os.path.islink(self.root / "filelink"))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX only")
    def test_fifos_and_sockets_are_left_alone(self):
        os.mkfifo(self.root / "pipe")
        os.utime(self.root / "pipe", (OLD, OLD))
        res = junk.run(self.target, delete=True)
        self.assertEqual(res.files, 0)
        self.assertTrue((self.root / "pipe").exists())

    def test_pyinstaller_unpack_dir_is_excluded(self):
        touch(self.root / "_MEI123" / "python.dll")
        with mock.patch.object(junk.sys, "_MEIPASS", str(self.root / "_MEI123"), create=True):
            junk.run(self.target, delete=True)
        self.assertTrue((self.root / "_MEI123" / "python.dll").exists())

    def test_admin_target_is_skipped_without_admin(self):
        t = junk.Target("t", "test", (self.root,), admin=True)
        with mock.patch.object(junk, "is_admin", return_value=False):
            self.assertEqual(junk.run(t).skipped, "needs admin")

    def test_missing_root_is_skipped(self):
        t = junk.Target("t", "test", (self.tmp / "nope",))
        self.assertEqual(junk.run(t).skipped, "not present")


class EntriesMode(unittest.TestCase):
    def test_trash_removes_every_top_level_entry_regardless_of_age(self):
        root = Path(tempfile.mkdtemp())
        touch(root / "file", 5, old=False)
        touch(root / "dir" / "inner", 7, old=False)
        touch(root / "desktop.ini", 1)
        t = junk.Target("bin", "bin", (root,), mode="entries", min_age_hours=0, keep=("desktop.ini",))
        self.assertEqual((junk.run(t).files, junk.run(t).bytes), (2, 12))
        junk.run(t, delete=True)
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["desktop.ini"])

    def test_cache_entry_goes_whole_or_not_at_all(self):
        root = Path(tempfile.mkdtemp())
        touch(root / "stale" / "a")
        touch(root / "stale" / "b")
        touch(root / "live" / "old")
        touch(root / "live" / "fresh", old=False)  # one recent file keeps the whole cache
        os.utime(root / "stale", (OLD, OLD))
        os.utime(root / "live", (OLD, OLD))
        t = junk.Target("cache", "cache", (root,), mode="entries", min_age_hours=168)
        res = junk.run(t, delete=True)
        self.assertEqual(res.files, 2)
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["live"])
        self.assertTrue((root / "live" / "old").exists())


class SafeRoot(unittest.TestCase):
    def test_refuses_dangerous_roots(self):
        home = Path.home()
        for bad in (Path(""), Path("relative"), Path(home.anchor), home, home.parent):
            self.assertFalse(junk.safe_root(bad), bad)

    def test_accepts_ordinary_dirs(self):
        self.assertTrue(junk.safe_root(Path(tempfile.mkdtemp())))

    def test_unsafe_root_is_refused_by_run(self):
        t = junk.Target("t", "test", (Path.home(),))
        res = junk.run(t)
        self.assertIn("refused", res.skipped)
        self.assertEqual(res.files, 0)

    def test_unset_env_var_drops_the_root_instead_of_using_cwd(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(junk._env("TEMP"))


class BuiltinTargets(unittest.TestCase):
    def test_every_builtin_root_is_safe(self):
        for t in junk.targets():
            for root in t.roots:
                if os.path.isdir(root):
                    self.assertTrue(junk.safe_root(root), f"{t.id}: {root}")

    def test_ids_are_unique(self):
        ids = [t.id for t in junk.targets()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_emptying_the_bin_is_opt_in_and_nothing_else_is(self):
        # The Recycle Bin / Trash is the only undo anyone has, so the GUI must
        # never pre-tick it. Everything else is regenerable.
        opt_in = {t.id for t in junk.targets() if t.opt_in}
        self.assertEqual(opt_in, {"trash"} if junk.OS != "windows" else {"recycle-bin"})


if __name__ == "__main__":
    unittest.main()
