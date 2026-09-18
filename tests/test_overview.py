import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cleam import overview


class WindowsName(unittest.TestCase):
    def test_build_decides_the_version_not_the_product_name(self):
        # Windows 11 still reports ProductName "Windows 10 ...".
        eleven = {"ProductName": "Windows 10 Pro", "DisplayVersion": "24H2", "CurrentBuild": "26100", "UBR": "2314"}
        self.assertEqual(overview._windows_name(eleven), "Windows 11 Pro 24H2 (build 26100.2314)")

    def test_windows_10_stays_windows_10(self):
        ten = {"ProductName": "Windows 10 Pro", "DisplayVersion": "22H2", "CurrentBuild": "19045", "UBR": "5011"}
        self.assertEqual(overview._windows_name(ten), "Windows 10 Pro 22H2 (build 19045.5011)")

    def test_empty_registry_does_not_crash(self):
        self.assertEqual(overview._windows_name({}), "Windows")


class Mounts(unittest.TestCase):
    def test_only_real_block_devices_are_kept(self):
        text = (
            "/dev/sda1 / ext4 rw,relatime 0 0\n"
            "tmpfs /run tmpfs rw 0 0\n"
            "/dev/loop12 /snap/core22/1122 squashfs ro 0 0\n"
            "/dev/sda16 /boot ext4 rw 0 0\n"
            "overlay /var/lib/docker/overlay2/x overlay rw 0 0\n"
        )
        self.assertEqual(overview.parse_mounts(text), ["/", "/boot"])

    def test_escaped_spaces_are_decoded(self):
        self.assertEqual(overview.parse_mounts("/dev/sdb1 /mnt/my\\040disk ext4 rw 0 0\n"), ["/mnt/my disk"])


class Size(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def write(self, name, size=100):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def test_counts_files_in_subdirectories(self):
        self.write("a", 100)
        self.write("sub/b", 50)
        self.assertEqual(overview.size(self.root), (2, 150, 0))

    @unittest.skipUnless(hasattr(os, "link"), "needs hardlinks")
    def test_a_hardlinked_file_counts_once_where_the_link_count_is_visible(self):
        # WinSxS is a hardlink farm, which is why Explorer overstates it. A
        # Windows directory entry carries no link count (st_nlink is 0 there),
        # so dedup is POSIX-only and the Windows WinSxS row says so.
        original = self.write("a", 100)
        try:
            os.link(original, self.root / "same")
        except OSError:
            self.skipTest("hardlinks not permitted here")
        expected = (2, 200, 0) if os.name == "nt" else (1, 100, 0)
        self.assertEqual(overview.size(self.root), expected)

    def test_symlinks_are_not_counted_or_followed(self):
        outside = self.write("../outside/big", 900)
        try:
            os.symlink(outside, self.root / "link")
            os.symlink(outside.parent, self.root / "dirlink", target_is_directory=True)
        except OSError:
            self.skipTest("symlinks not permitted here")
        self.assertEqual(overview.size(self.root), (0, 0, 0))

    def test_a_missing_path_reports_one_unreadable_directory(self):
        self.assertEqual(overview.size(self.root / "nope"), (0, 0, 1))

    def test_unreadable_folder_is_flagged_rather_than_reported_as_empty(self):
        folder = overview.Folder("docker", self.root / "nope")
        overview.measure(folder)
        self.assertTrue(folder.unreadable)


class SizeLabel(unittest.TestCase):
    def test_a_partial_read_is_marked_so_it_is_not_mistaken_for_the_total(self):
        # C:\Windows always has folders no account can read; a plain number
        # would sit below Explorer's and look like a bug in Cleam.
        folder = overview.Folder("Windows", Path("/w"), files=1000, bytes=2048, denied=12, measured=True)
        self.assertEqual(folder.size_label, "≥ 2.0 KiB")
        self.assertIn("12 folders unreadable", folder.detail)

    def test_a_fully_readable_folder_gets_a_plain_size(self):
        folder = overview.Folder("usr", Path("/usr"), files=10, bytes=1024, measured=True)
        self.assertEqual(folder.size_label, "1.0 KiB")

    def test_nothing_readable_asks_for_admin(self):
        folder = overview.Folder("docker", Path("/d"), denied=1, measured=True)
        self.assertEqual(folder.size_label, "needs admin")

    def test_unmeasured_says_so_rather_than_zero(self):
        self.assertEqual(overview.Folder("x", Path("/x")).size_label, "not measured")


class Folders(unittest.TestCase):
    def test_missing_folders_are_dropped(self):
        for folder in overview.folders():
            self.assertTrue(os.path.isdir(folder.path), folder.path)

    def test_labels_are_unique(self):
        labels = [f.label for f in overview.folders()]
        self.assertEqual(len(labels), len(set(labels)))


class Biggest(unittest.TestCase):
    def test_sorted_by_size_and_capped(self):
        root = Path(tempfile.mkdtemp())
        for name, size in (("small", 10), ("large", 5000), ("medium", 200)):
            (root / name).mkdir()
            (root / name / "f").write_bytes(b"x" * size)
        found = overview.biggest(root, top=2)
        self.assertEqual([f.label for f in found], ["large", "medium"])
        self.assertEqual(found[0].bytes, 5000)

    def test_a_useless_top_still_returns_the_biggest(self):
        root = Path(tempfile.mkdtemp())
        (root / "a").mkdir()
        (root / "a" / "f").write_bytes(b"x" * 10)
        for top in (0, -3):
            self.assertEqual([f.label for f in overview.biggest(root, top=top)], ["a"], top)


class Disks(unittest.TestCase):
    def test_percent_matches_df_which_ignores_reserved_blocks_and_rounds_up(self):
        # df prints ceil(used / (used + avail)). Measured on the dev box:
        # 50.403% shows as 51%, and against total it would read 50%.
        disk = overview.Disk("/", total=154_618_822_656, used=78_061_486_080, free=76_820_000_768)
        self.assertEqual(disk.percent_used, 51)
        self.assertEqual(overview.Disk("/", total=100, used=50, free=50).percent_used, 50)

    def test_percent_used_of_an_unreadable_mount_is_zero_not_a_crash(self):
        with mock.patch.object(overview.shutil, "disk_usage", side_effect=OSError):
            disk = overview._usage("/nope")
        self.assertEqual((disk.total, disk.percent_used), (0, 0))


if __name__ == "__main__":
    unittest.main()
