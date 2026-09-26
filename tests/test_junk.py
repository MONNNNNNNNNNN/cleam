import os
import sys
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

    def test_an_unreadable_admin_target_is_present_so_it_can_say_needs_admin(self):
        t = junk.Target("t", "test", (self.tmp / "unreadable",), admin=True)
        with mock.patch.object(junk, "is_admin", return_value=False):
            self.assertTrue(junk.present(t))
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

    def test_emptying_the_bin_is_opt_in(self):
        # The Recycle Bin / Trash is the only undo anyone has, so the GUI must
        # never pre-tick it.
        opt_in = {t.id for t in junk.targets() if t.opt_in}
        self.assertIn("trash" if junk.OS != "windows" else "recycle-bin", opt_in)

    def test_package_manager_caches_are_opt_in(self):
        # Regenerable, but a clean re-downloads gigabytes on the next install.
        for t in junk.targets():
            if t.group == "Developer tools" and t.mode == "command":
                self.assertTrue(t.opt_in, t.id)

    def test_every_target_explains_itself(self):
        for t in junk.targets():
            self.assertTrue(t.about, t.id)
            self.assertEqual(t.patterns, tuple(p.lower() for p in t.patterns), t.id)
            self.assertEqual(bool(t.command), t.mode == "command", t.id)


class Patterns(unittest.TestCase):
    def test_only_matching_names_go(self):
        root = Path(tempfile.mkdtemp())
        touch(root / "CBS" / "CbsPersist_1.LOG")
        touch(root / "CBS" / "setup.etl")
        touch(root / "CBS" / "config.xml")
        t = junk.Target("logs", "logs", (root,), patterns=("*.log", "*.etl"))
        res = junk.run(t, delete=True)
        self.assertEqual(res.files, 2)  # matched case-insensitively
        self.assertEqual(sorted(p.name for p in (root / "CBS").iterdir()), ["config.xml"])


class CommandMode(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "cache"
        touch(self.root / "a" / "blob", 100)
        touch(self.root / "b", 50, old=False)

    def target(self, script: str) -> junk.Target:
        return junk.Target("tool", "tool", (self.root,), mode="command",
                           command=(sys.executable, "-c", script))

    def test_scan_measures_and_runs_nothing(self):
        marker = self.root.parent / "ran"
        res = junk.run(self.target(f"open(r'{marker}', 'w')"))
        self.assertEqual((res.files, res.bytes, res.errors), (2, 150, 0))
        self.assertFalse(marker.exists())

    def test_clean_reports_what_the_tool_actually_freed(self):
        res = junk.run(self.target(f"import shutil; shutil.rmtree(r'{self.root / 'a'}')"), delete=True)
        self.assertEqual((res.files, res.bytes, res.errors), (1, 100, 0))
        self.assertTrue((self.root / "b").exists())

    def test_tool_runs_beside_the_cache_not_in_the_callers_cwd(self):
        # Yarn 2+ `cache clean` clears the project cache of its cwd.
        marker = self.root.parent / "cwd.txt"
        junk.run(self.target(f"import os; open(r'{marker}', 'w').write(os.getcwd())"), delete=True)
        self.assertEqual(os.path.realpath(marker.read_text()), os.path.realpath(self.root.parent))

    def test_a_tool_that_removes_its_whole_cache_succeeds(self):
        # uv removes the cache root itself; Windows refuses that for a cwd.
        res = junk.run(self.target(f"import shutil; shutil.rmtree(r'{self.root}')"), delete=True)
        self.assertEqual((res.files, res.bytes, res.errors), (2, 150, 0))

    def test_a_runner_replaces_the_command_and_its_refusals_are_errors(self):
        def runner():
            import shutil as sh
            sh.rmtree(self.root / "a")
            return 2  # e.g. two driver packages still in use
        t = junk.Target("drv", "drv", (self.root,), mode="command", command=(sys.executable,), runner="fake")
        with mock.patch.dict(junk.RUNNERS, {"fake": runner}):
            res = junk.run(t, delete=True)
        self.assertEqual((res.files, res.bytes, res.errors), (1, 100, 2))

    def test_failing_tool_is_an_error_not_a_success(self):
        res = junk.run(self.target("raise SystemExit(3)"), delete=True)
        self.assertEqual((res.bytes, res.errors), (0, 1))

    def test_missing_tool_is_skipped(self):
        t = junk.Target("tool", "tool", (self.root,), mode="command", command=("no-such-cleaner-xyz",))
        self.assertEqual(junk.run(t).skipped, "no-such-cleaner-xyz not installed")

    def test_unsafe_root_is_never_measured_or_cleaned(self):
        t = junk.Target("tool", "tool", (Path.home(),), mode="command", command=(sys.executable, "-c", ""))
        self.assertEqual(junk.run(t, delete=True).skipped, "not present")


class Narrowed(unittest.TestCase):
    """Targets whose roots hold things that must survive: installed
    extensions, save games."""

    def test_only_abandoned_extension_extractions_go(self):
        ext = Path(tempfile.mkdtemp())
        abandoned = ".61e521a5-2191-4f40-9a69-3bfa2ff515ca"
        for name in (abandoned, "openai.chatgpt-26.825.41651-win32-x64", "ms-python.python-2026.1.0"):
            touch(ext / name / "package.json")
            os.utime(ext / name, (OLD, OLD))
        touch(ext / ".obsolete")
        touch(ext / "extensions.json")
        t = next(t for t in junk.targets() if t.id == "editor-partial-installs")
        junk.run(junk.Target(t.id, t.label, (ext,), mode=t.mode, min_age_hours=t.min_age_hours,
                             patterns=t.patterns), delete=True)
        self.assertEqual(sorted(p.name for p in ext.iterdir()),
                         [".obsolete", "extensions.json", "ms-python.python-2026.1.0",
                          "openai.chatgpt-26.825.41651-win32-x64"])

    def test_unreal_shader_blob_goes_and_save_games_stay(self):
        saved = Path(tempfile.mkdtemp()) / "b1" / "Saved"
        touch(saved / "D3DDriverByteCodeBlob_V4318_D10115_S1363088482_R161.ushaderprecache", 500)
        touch(saved / "SaveGames" / "ArchiveSaveFile.1.sav", 70)
        touch(saved / "Config" / "Windows" / "GameUserSettings.ini", 5)
        t = junk.Target("g", "g", (saved,), patterns=("d3ddriverbytecodeblob_*.ushaderprecache",))
        res = junk.run(t, delete=True)
        self.assertEqual((res.files, res.bytes), (1, 500))
        self.assertTrue((saved / "SaveGames" / "ArchiveSaveFile.1.sav").exists())
        self.assertTrue((saved / "Config" / "Windows" / "GameUserSettings.ini").exists())


class Discovery(unittest.TestCase):
    def test_updater_folders_need_the_electron_updater_layout(self):
        local = Path(tempfile.mkdtemp())
        touch(local / "termius-updater" / "installer.exe")
        (local / "obsidian-updater" / "pending").mkdir(parents=True)
        touch(local / "vendor-updater" / "vendor-updater.exe")  # a program, not a download
        found = sorted(p.name for p in junk._updaters(local))
        self.assertEqual(found, ["obsidian-updater", "termius-updater"])

    def test_gecko_matches_caches_not_profiles(self):
        profiles = Path(tempfile.mkdtemp())
        (profiles / "abc.default" / "cache2").mkdir(parents=True)
        (profiles / "abc.default" / "storage").mkdir()
        self.assertEqual([p.name for p in junk._gecko(profiles)], ["cache2"])

    def test_npx_entries_go_whole_and_only_when_unused(self):
        npx = Path(tempfile.mkdtemp())
        touch(npx / "old" / "node_modules" / "a.js")
        os.utime(npx / "old" / "node_modules", (OLD, OLD))
        os.utime(npx / "old", (OLD, OLD))
        touch(npx / "live" / "node_modules" / "old.js")
        touch(npx / "live" / "package.json", old=False)
        t = next(t for t in junk.targets() if t.id == "npx-cache")
        t = junk.Target(t.id, t.label, (npx,), mode=t.mode, min_age_hours=t.min_age_hours)
        junk.run(t, delete=True)
        self.assertEqual([p.name for p in npx.iterdir()], ["live"])


class Owners(unittest.TestCase):
    def test_names_the_apps_behind_the_roots(self):
        base = Path("/r")
        roots = tuple(base / n / "GPUCache" for n in ("discord", "Code", "discord", "Slack", "Teams"))
        self.assertEqual(junk.owners(roots, base), "discord, Code, Slack, +1")


if __name__ == "__main__":
    unittest.main()
