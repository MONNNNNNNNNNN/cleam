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
