"""What this machine is, how full its disks are, and where the space went.

The point is context: "AppData is 33.7 GB" means nothing on its own, so the
big folders carry a note saying what is normal and what actually shrinks them.
Sizing is separate from listing, because walking 190,000 files takes seconds --
callers list first, then measure one folder at a time.
"""
from __future__ import annotations

import os
import platform
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .junk import REPARSE
from .system import OS


@dataclass
class Folder:
    label: str
    path: Path
    note: str = ""
    files: int = 0
    bytes: int = 0
    denied: int = 0  # directories that could not be read; /var/lib/docker is root-only
    measured: bool = False

    @property
    def unreadable(self) -> bool:
        return self.files == 0 and self.denied > 0


@dataclass
class Disk:
    mount: str
    total: int = 0
    used: int = 0
    free: int = 0

    @property
    def percent_used(self) -> int:
        return round(100 * self.used / self.total) if self.total else 0


def os_name() -> str:
    if OS == "windows":
        return _windows_name(_registry_version())
    if OS == "macos":
        return f"macOS {platform.mac_ver()[0]} ({platform.machine()})"
    return f"{_os_release().get('PRETTY_NAME', 'Linux')} (kernel {platform.release()})"


def _registry_version() -> dict[str, str]:
    import winreg

    wanted = ("ProductName", "DisplayVersion", "CurrentBuild", "UBR", "EditionID")
    values: dict[str, str] = {}
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        ) as key:
            for name in wanted:
                try:
                    values[name] = str(winreg.QueryValueEx(key, name)[0])
                except OSError:
                    pass
    except OSError:
        pass
    return values


def _windows_name(values: dict[str, str]) -> str:
    """Windows 11 still reports ProductName "Windows 10 ..."; the build decides."""
    name = values.get("ProductName", "Windows")
    build = int(values.get("CurrentBuild", 0) or 0)
    if build >= 22000:
        name = name.replace("Windows 10", "Windows 11")
    version = values.get("DisplayVersion", "")  # 24H2, 23H2 ...
    full = f"{build}.{values['UBR']}" if values.get("UBR") else str(build)
    return " ".join(part for part in (name, version, f"(build {full})" if build else "")).strip()


def _os_release(path: str = "/etc/os-release") -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key:
                    values[key] = value.strip('"')
    except OSError:
        pass
    return values


def disks() -> list[Disk]:
    return [_usage(m) for m in _mounts()]


def _usage(mount: str) -> Disk:
    try:
        total, used, free = shutil.disk_usage(mount)
    except OSError:
        return Disk(mount)
    return Disk(mount, total, used, free)


def _mounts() -> list[str]:
    if OS == "windows":
        return [f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.isdir(f"{c}:\\")]
    if OS == "linux":
        return parse_mounts(_read("/proc/mounts")) or ["/"]
    return ["/", "/System/Volumes/Data"]


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def parse_mounts(text: str) -> list[str]:
    """Real block devices only: /proc/mounts is mostly overlays, squashfs snaps and tmpfs."""
    skip = {"squashfs", "tmpfs", "devtmpfs", "overlay", "autofs", "ramfs", "fuse.snapfuse"}
    seen: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].startswith("/dev/") or parts[2] in skip:
            continue
        mount = parts[1].replace("\\040", " ")
        if mount not in seen:
            seen.append(mount)
    return seen


def folders() -> list[Folder]:
    """The folders worth explaining, unmeasured. Missing ones are dropped."""
    home = Path.home()
    if OS == "windows":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        windir = Path(os.environ.get("SystemRoot", "C:\\Windows"))
        drive = Path(os.environ.get("SystemDrive", "C:") + "\\")
        candidates = [
            Folder("AppData\\Local", local, "10-30 GB is ordinary: app caches, browsers, Electron apps."),
            Folder("AppData\\Roaming", roaming, "Settings and profiles. Usually under 5 GB."),
            Folder(
                "Windows",
                windir,
                "25-40 GB is normal on Windows 11. Most of it is not yours to delete.",
            ),
            Folder(
                "Windows\\WinSxS",
                windir / "WinSxS",
                "Counted twice: its files are hardlinks into Windows, so Explorer overstates it. "
                "Shrink it with: DISM /Online /Cleanup-Image /StartComponentCleanup",
            ),
            Folder("Program Files", drive / "Program Files"),
            Folder("Users", drive / "Users", "Everyone's profiles, so it includes the folders above."),
            Folder("Downloads", home / "Downloads", "Yours to delete. Cleam never touches it."),
        ]
    elif OS == "macos":
        candidates = [
            Folder("Library", home / "Library", "Caches, containers and app support."),
            Folder("Library/Caches", home / "Library" / "Caches", "Regenerable. The Clean tab handles this."),
            Folder("Applications", Path("/Applications")),
            Folder("Downloads", home / "Downloads", "Yours to delete. Cleam never touches it."),
        ]
    else:
        candidates = [
            Folder("~/.cache", home / ".cache", "Regenerable. The Clean tab handles this."),
            Folder("/var/log", Path("/var/log"), "Shrink with: sudo journalctl --vacuum-size=200M"),
            Folder("/var/lib/docker", Path("/var/lib/docker"), "Shrink with: docker system prune"),
            Folder("/usr", Path("/usr"), "Installed packages. Uninstall from the Programs tab."),
            Folder("Downloads", home / "Downloads", "Yours to delete. Cleam never touches it."),
        ]
    return [f for f in candidates if os.path.isdir(f.path)]


def measure(folder: Folder) -> Folder:
    """Fill in files and bytes. Slow: this is the walk, so call it off the UI thread."""
    folder.files, folder.bytes, folder.denied = size(folder.path)
    folder.measured = True
    return folder


def size(path: Path | str) -> tuple[int, int, int]:
    """(files, bytes, unreadable directories) under path.

    Skips reparse points (junctions, symlinks, OneDrive placeholders) and never
    leaves the filesystem it started on, so a mounted drive under the path is
    not counted as part of it. On POSIX a hardlinked file counts once; on
    Windows the directory entry carries no link count, so WinSxS double-counts
    exactly the way Explorer does -- hence the note on that row.
    """
    try:
        dev = os.lstat(path).st_dev
    except OSError:
        return 0, 0, 1
    seen: set[tuple[int, int]] = set()
    files = total = denied = 0
    stack = [str(path)]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            denied += 1  # report what can be seen, and say the rest was not readable
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if getattr(st, "st_file_attributes", 0) & REPARSE:
                continue
            if stat.S_ISDIR(st.st_mode):
                if st.st_dev in (dev, 0):  # Windows dir entries report st_dev 0
                    stack.append(entry.path)
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            if st.st_nlink > 1:
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
            files += 1
            total += st.st_size
    return files, total, denied


def biggest(root: Path | str, top: int = 10) -> list[Folder]:
    """The largest immediate children of root, biggest first. Answers "where did it go?"."""
    found: list[Folder] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return found
    for entry in entries:
        files, total, denied = size(entry.path)
        found.append(
            Folder(entry.name, Path(entry.path), files=files, bytes=total, denied=denied, measured=True)
        )
    found.sort(key=lambda f: f.bytes, reverse=True)
    return found[:top]
