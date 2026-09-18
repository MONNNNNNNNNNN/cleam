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


class Disks(unittest.TestCase):
    def test_percent_used_of_an_unreadable_mount_is_zero_not_a_crash(self):
        with mock.patch.object(overview.shutil, "disk_usage", side_effect=OSError):
            disk = overview._usage("/nope")
        self.assertEqual((disk.total, disk.percent_used), (0, 0))


if __name__ == "__main__":
    unittest.main()
