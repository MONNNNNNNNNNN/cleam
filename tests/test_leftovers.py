import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cleam import leftovers


class Matching(unittest.TestCase):
    def test_the_same_name_is_high_confidence(self):
        aliases = leftovers.aliases_for("Visual Studio Code", "Microsoft Corporation")
        self.assertEqual(leftovers.score("Visual Studio Code", aliases), "high")
        self.assertEqual(leftovers.score("visual-studio-code", aliases), "high")

    def test_a_longer_name_containing_it_is_low_confidence(self):
        aliases = leftovers.aliases_for("Slack")
        self.assertEqual(leftovers.score("Slack Technologies", aliases), "low")

    def test_an_unrelated_name_does_not_match(self):
        aliases = leftovers.aliases_for("Slack")
        self.assertEqual(leftovers.score("Mozilla Firefox", aliases), "")

    def test_generic_words_alone_never_match(self):
        # Removing Teams must not offer %LOCALAPPDATA%\Microsoft, which holds
        # Edge, Office and Windows' own data.
        self.assertFalse(leftovers.aliases_for("", "Microsoft"))
        self.assertEqual(leftovers.score("Microsoft", leftovers.aliases_for("Microsoft Teams")), "")
        self.assertEqual(leftovers.score("Microsoft Teams", leftovers.aliases_for("Microsoft Teams")), "high")

    def test_a_windows_install_path_parses_on_any_platform(self):
        self.assertEqual(leftovers.last_path_part(r"C:\Program Files\Sublime Text"), "Sublime Text")
        self.assertEqual(leftovers.last_path_part("/opt/sublime_text/"), "sublime_text")
        self.assertEqual(leftovers.last_path_part(""), "")

    def test_the_install_folder_name_counts_as_an_alias(self):
        aliases = leftovers.aliases_for("Some Editor 2024", install_dir=r"C:\Program Files\Sublime Text")
        self.assertEqual(leftovers.score("Sublime Text", aliases), "high")


    def test_a_name_with_no_separators_still_matches(self):
        # Remnants are routinely called GoogleChrome or CleamSmokeApp. Token
        # matching sees one token there and lines nothing up, which a real run
        # on Linux exposed.
        self.assertEqual(leftovers.score("CleamSmokeApp", leftovers.aliases_for("Cleam Smoke App")), "high")
        self.assertEqual(leftovers.score("GoogleChrome", leftovers.aliases_for("Google Chrome")), "high")
        self.assertEqual(leftovers.score("SublimeText3", leftovers.aliases_for("Sublime Text")), "low")

    def test_a_two_letter_name_is_too_short_to_match_anything(self):
        # "Go" would claim every folder containing "go".
        self.assertFalse(leftovers.aliases_for("Go"))
        self.assertEqual(leftovers.score("Google Chrome", leftovers.aliases_for("Go")), "")

class Scan(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / ".config" / "myapp").mkdir(parents=True)
        (self.home / ".config" / "myapp" / "settings.ini").write_bytes(b"x" * 40)
        (self.home / ".config" / "myapp-companion").mkdir()
        (self.home / ".config" / "unrelated").mkdir()
        patcher = mock.patch.object(
            leftovers, "_roots", return_value=[(self.home / ".config", "folder")]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_finds_the_settings_folder_and_sizes_it(self):
        found = leftovers.scan("MyApp")
        high = [item for item in found if item.confidence == "high"]
        self.assertEqual([Path(item.target).name for item in high], ["myapp"])
        self.assertEqual(high[0].bytes, 40)

    def test_a_similar_name_is_offered_but_never_at_high_confidence(self):
        found = {Path(i.target).name: i.confidence for i in leftovers.scan("MyApp")}
        self.assertEqual(found.get("myapp-companion"), "low")
        self.assertNotIn("unrelated", found)

    def test_a_folder_belonging_to_another_installed_app_is_dropped(self):
        found = leftovers.scan("MyApp", other_apps=("MyApp Companion", "Unrelated"))
        self.assertEqual([Path(i.target).name for i in found], ["myapp"])

    def test_an_unnameable_app_scans_nothing(self):
        self.assertEqual(leftovers.scan(""), [])


class BackupAndRestore(unittest.TestCase):
    def test_removal_is_a_move_into_a_backup_and_restore_puts_it_back(self):
        root = Path(tempfile.mkdtemp())
        folder = root / "myapp"
        folder.mkdir()
        (folder / "settings.ini").write_bytes(b"keep me")
        item = leftovers.Leftover("folder", str(folder), "test", "high", 7)
        with mock.patch.object(leftovers, "backup_dir", return_value=root / "backups"):
            backup, errors = leftovers.remove([item], label="MyApp")
        self.assertEqual(errors, [])
        self.assertFalse(folder.exists())  # moved, not deleted
        manifest = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(manifest[0]["target"], str(folder))

        self.assertEqual(leftovers.restore(backup), [])
        self.assertEqual((folder / "settings.ini").read_bytes(), b"keep me")

    def test_restore_refuses_to_overwrite_something_that_came_back(self):
        root = Path(tempfile.mkdtemp())
        folder = root / "myapp"
        folder.mkdir()
        item = leftovers.Leftover("folder", str(folder), "test", "high")
        with mock.patch.object(leftovers, "backup_dir", return_value=root / "backups"):
            backup, _ = leftovers.remove([item], label="MyApp")
        folder.mkdir()  # the program was reinstalled in the meantime
        errors = leftovers.restore(backup)
        self.assertIn("already exists", errors[0])

    def test_a_missing_target_is_reported_not_raised(self):
        root = Path(tempfile.mkdtemp())
        item = leftovers.Leftover("folder", str(root / "gone"), "test", "high")
        with mock.patch.object(leftovers, "backup_dir", return_value=root / "backups"):
            _, errors = leftovers.remove([item])
        self.assertEqual(len(errors), 1)

    def test_the_label_cannot_escape_the_backup_folder(self):
        root = Path(tempfile.mkdtemp())
        with mock.patch.object(leftovers, "backup_dir", return_value=root / "backups"):
            backup, _ = leftovers.remove([], label="../../etc/evil name")
        self.assertEqual(backup.parent, root / "backups")
        self.assertNotIn("..", backup.name)


class Forced(unittest.TestCase):
    """A forced uninstall takes the program's folder and entry; the guards decide when it may."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.app = self.root / "Programs" / "Broken App"
        (self.app / "bin").mkdir(parents=True)
        (self.app / "bin" / "app.exe").write_bytes(b"x" * 10)
        for p in (mock.patch.object(leftovers, "_roots", return_value=[]),
                  mock.patch.object(leftovers, "_registry_leftovers", return_value=[]),
                  mock.patch.object(leftovers, "backup_dir", return_value=self.root / "backups")):
            p.start()
            self.addCleanup(p.stop)

    def test_the_folder_and_the_entry_are_taken(self):
        key = "HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Broken App"
        items, why = leftovers.forced("Broken App", str(self.app), key)
        self.assertEqual(why, "")
        self.assertEqual([(i.kind, i.target, i.confidence) for i in items],
                         [("folder", str(self.app), "high"), ("registry", key, "high")])
        self.assertEqual(items[0].bytes, 10)

    def test_a_folder_not_named_after_the_program_needs_the_user_to_name_it(self):
        _, why = leftovers.forced("Something Else", str(self.app))
        self.assertIn("--install-dir", why)
        items, why = leftovers.forced("Something Else", str(self.app), explicit=True)
        self.assertEqual((why, items[0].target), ("", str(self.app)))

    def test_unsafe_folders_are_refused(self):
        for folder in (str(Path.home()), Path(self.root).anchor, "relative\\path", str(self.root / "nope")):
            items, why = leftovers.forced("Broken App", folder, explicit=True)
            self.assertEqual(items, [], folder)
            self.assertTrue(why, folder)
        self.assertIn("could not tell", leftovers.forced("Broken App", "")[1])

    def test_shared_folders_and_your_own_folders_are_refused_even_when_named(self):
        shared = self.root / "AppData-Local"
        shared.mkdir()
        with mock.patch.object(leftovers, "_roots", return_value=[(shared, "folder")]):
            self.assertIn("shared", leftovers.forced("AppData Local", str(shared), explicit=True)[1])
        documents = Path.home() / "cleam-test-documents"
        documents.mkdir(exist_ok=True)
        self.addCleanup(documents.rmdir)
        self.assertIn("your own", leftovers.forced("Documents", str(documents), explicit=True)[1])

    def test_a_folder_holding_another_program_is_refused(self):
        _, why = leftovers.forced("Broken App", str(self.app), other_dirs=(str(self.app / "bin"),))
        self.assertIn("another installed program", why)
        # Living inside another program's folder (a Steam game) is fine.
        items, why = leftovers.forced("Broken App", str(self.app), other_dirs=(str(self.root / "Programs"),))
        self.assertEqual(why, "")

    def test_a_locked_folder_stops_before_the_entry_is_deleted(self):
        items = [leftovers.Leftover("folder", str(self.root / "gone"), "t", "high"),
                 leftovers.Leftover("registry", "HKCU\\Software\\X", "t", "high")]
        with mock.patch.object(leftovers.subprocess, "run") as run:
            _, errors = leftovers.remove(items, stop_on_error=True)
        self.assertEqual(len(errors), 1)
        run.assert_not_called()  # reg export / reg delete never ran

    def test_a_folder_on_another_drive_is_backed_up_on_that_drive(self):
        backup = self.root / "backups" / "x-1"
        backup.mkdir(parents=True)
        real = os.stat

        def fake(path, *a, **k):  # the app's tree is "drive 2", everything else "drive 1"
            st = real(path, *a, **k)
            dev = 2 if str(path).startswith(str(self.root / "Programs")) else 1
            return os.stat_result((st.st_mode, st.st_ino, dev) + tuple(st)[3:])

        with mock.patch.object(leftovers.os, "stat", side_effect=fake):
            where = leftovers.files_backup(str(self.app), backup)
        self.assertEqual(where, self.root / "Programs" / ".cleam-backups" / "x-1" / "files")
        self.assertEqual(leftovers.files_backup(str(self.root / "backups"), backup), backup / "files")


class PackageConfig(unittest.TestCase):
    @unittest.skipUnless(leftovers.OS == "linux", "dpkg only")
    def test_a_removed_but_not_purged_package_is_offered(self):
        listing = "rc  mysql-server  8.0  amd64  MySQL\nii  caddy  2.11  arm64  web server\n"
        with mock.patch.object(leftovers, "output", return_value=listing), mock.patch.object(
            leftovers, "sudo", side_effect=lambda cmd, noninteractive=False: cmd
        ):
            found = leftovers._package_config_leftovers("mysql-server")
        self.assertEqual(found[0].target, "mysql-server")
        self.assertEqual(found[0].command, ["apt-get", "purge", "-y", "mysql-server"])


class RegistryRoots(unittest.TestCase):
    def test_both_bitness_views_are_searched(self):
        roots = leftovers.registry_roots()
        self.assertIn(("HKLM", r"SOFTWARE\WOW6432Node"), roots)
        self.assertIn(("HKCU", "Software"), roots)

    @unittest.skipIf(os.name == "nt", "the empty path is the non-Windows one")
    def test_registry_scan_is_empty_off_windows(self):
        self.assertEqual(leftovers._registry_leftovers(leftovers.aliases_for("myapp"), set()), [])


if __name__ == "__main__":
    unittest.main()
