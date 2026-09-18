"""Junk targets, and the walk that scans or deletes them.

Scan and clean are the same walk; clean deletes as it goes. Nothing is deleted
from a list built earlier, so a path swapped for a symlink between `scan` and
`clean --yes` is judged where it stands at delete time, not trusted from the
report. On POSIX the walk is os.fwalk, which holds a directory fd and never
follows a symlink, so a tmp cleaner run as root cannot be steered outside its
root by a link planted in /tmp.
"""
from __future__ import annotations

import os
import shutil
import stat
import string
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .system import OS, is_admin, output

HOUR = 3600
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True)
class Target:
    id: str
    label: str
    roots: tuple[Path, ...]
    # "files": regular files older than min_age_hours, anywhere under a root.
    #          Directories are kept, so apps that expect them still find them.
    # "entries": whole top-level entries under a root, each removed only when
    #          nothing inside it is newer than min_age_hours. App caches need
    #          this: deleting the old files of a structured cache (uv, prisma)
    #          while keeping the new ones corrupts it. Trash bins use it with 0.
    mode: str = "files"
    min_age_hours: float = 24
    admin: bool = False
    keep: tuple[str, ...] = ()  # top-level names under a root to leave alone
    # opt_in targets are never selected for the user. The Recycle Bin is the
    # only undo anybody has, so emptying it has to be a deliberate click.
    opt_in: bool = False


@dataclass
class Result:
    id: str
    files: int = 0
    bytes: int = 0
    errors: int = 0  # locked, permission denied, vanished mid-walk
    skipped: str = ""  # why the whole target was skipped, if it was


def _env(name: str) -> Path | None:
    # An unset variable must drop the root, never become Path("") -- that is the cwd.
    value = os.environ.get(name)
    return Path(value) if value else None


def _paths(*candidates: Path | None) -> tuple[Path, ...]:
    return tuple(p for p in candidates if p is not None)


def _glob(base: Path | None, *patterns: str) -> tuple[Path, ...]:
    if base is None or not os.path.isdir(base):
        return ()
    return tuple(p for pattern in patterns for p in base.glob(pattern))


def _windows_recycle_bins() -> tuple[Path, ...]:
    # whoami /user /fo csv /nh  ->  "host\user","S-1-5-21-..."
    sid = output(["whoami", "/user", "/fo", "csv", "/nh"]).strip().split(",")[-1].strip('"')
    if not sid.startswith("S-"):
        return ()
    drives = (f"{c}:\\" for c in string.ascii_uppercase)
    return tuple(Path(d, "$Recycle.Bin", sid) for d in drives if os.path.isdir(d))


def targets() -> list[Target]:
    home = Path.home()
    if OS == "windows":
        local, windir = _env("LOCALAPPDATA"), _env("SystemRoot")
        chromium = ("User Data/*/Cache", "User Data/*/Code Cache", "User Data/*/GPUCache")
        return [
            Target("user-temp", "User temp files", _paths(_env("TEMP"))),
            Target("system-temp", "Windows temp files", _paths(windir and windir / "Temp"), admin=True),
            Target(
                "update-cache",
                "Windows Update downloads",
                _paths(windir and windir / "SoftwareDistribution" / "Download"),
                admin=True,
            ),
            Target("crash-dumps", "Crash dumps", _paths(local and local / "CrashDumps")),
            Target(
                "browser-cache",
                "Browser caches",
                _glob(local and local / "Google" / "Chrome", *chromium)
                + _glob(local and local / "Microsoft" / "Edge", *chromium)
                + _glob(local and local / "Mozilla" / "Firefox" / "Profiles", "*/cache2"),
            ),
            Target(
                "recycle-bin",
                "Recycle Bin",
                _windows_recycle_bins(),
                mode="entries",
                min_age_hours=0,
                keep=("desktop.ini",),
                opt_in=True,
            ),
        ]
    if OS == "macos":
        return [
            Target("user-cache", "App caches", (home / "Library" / "Caches",), mode="entries", min_age_hours=168),
            Target("logs", "App logs", (home / "Library" / "Logs",), min_age_hours=168),
            Target("tmp", "Temp files", _paths(_env("TMPDIR"))),
            Target("trash", "Trash", (home / ".Trash",), mode="entries", min_age_hours=0, opt_in=True),
        ]
    cache = _env("XDG_CACHE_HOME") or home / ".cache"
    data = _env("XDG_DATA_HOME") or home / ".local" / "share"
    return [
        # Regenerable by definition, but these re-download gigabytes (browsers
        # for Playwright, model weights), which nobody asking for a clean wants.
        Target(
            "user-cache",
            "App caches",
            (cache,),
            mode="entries",
            min_age_hours=168,
            keep=("ms-playwright", "huggingface", "torch"),
        ),
        Target("tmp", "Temp files", (Path("/tmp"),)),
        Target(
            "trash",
            "Trash",
            (data / "Trash" / "files", data / "Trash" / "info"),
            mode="entries",
            min_age_hours=0,
            opt_in=True,
        ),
        Target(
            "apt-cache",
            "Downloaded .deb packages",
            (Path("/var/cache/apt/archives"),),
            min_age_hours=0,
            admin=True,
            keep=("lock", "partial"),
        ),
    ]


def _never() -> set[Path]:
    paths = {Path.home().resolve()}
    for name in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        if p := _env(name):
            paths.add(p.resolve())
    if OS != "windows":
        paths |= {Path(p) for p in ("/bin", "/boot", "/etc", "/lib", "/opt", "/usr", "/var",
                                    "/System", "/Library", "/Applications", "/private", "/private/var")}
    return paths


def safe_root(root: Path) -> bool:
    """Refuse a root that is a drive/filesystem root, home, an ancestor of home, or a system dir.

    Targets are built-in, so this guards against the environment: a TEMP or
    XDG_CACHE_HOME pointing somewhere it should not.
    """
    if not root.is_absolute():
        return False
    root = root.resolve()
    home = Path.home().resolve()
    return root != Path(root.anchor) and root not in home.parents and root not in _never()


def _excluded() -> set[str]:
    # A PyInstaller one-file build unpacks itself into the temp dir it is about to clean.
    meipass = getattr(sys, "_MEIPASS", None)
    # realpath, not abspath: Windows may hand out an 8.3 short temp path (RUNNER~1).
    return {os.path.normcase(os.path.realpath(meipass))} if meipass else set()


def run(target: Target, delete: bool = False) -> Result:
    """Scan target, deleting what matches when delete=True. Never raises for per-file errors."""
    res = Result(target.id)
    if target.admin and not is_admin():
        res.skipped = "needs admin"
        return res
    # os.path.isdir, not Path.is_dir: the latter raises on a permission error
    # (another user's Recycle Bin, a TCC-protected folder on macOS).
    roots = [r.resolve() for r in target.roots if os.path.isdir(r)]
    if not roots:
        res.skipped = "not present"
        return res
    cutoff = time.time() - target.min_age_hours * HOUR
    for root in roots:
        if not safe_root(root):
            res.errors += 1
            res.skipped = f"refused unsafe root {root}"
            continue
        if target.mode == "entries":
            _entries(root, target, cutoff, delete, res)
        elif OS == "windows":
            _files_nt(root, target, cutoff, delete, res)
        else:
            _files_posix(root, target, cutoff, delete, res)
    return res


def _count(res: Result, size: int) -> None:
    res.files += 1
    res.bytes += size


def _files_posix(root: Path, t: Target, cutoff: float, delete: bool, res: Result) -> None:
    dev = os.lstat(root).st_dev
    excluded = _excluded()

    def onerror(_: OSError) -> None:
        res.errors += 1

    for dirpath, dirs, files, dfd in os.fwalk(root, onerror=onerror):
        keep = t.keep if dirpath == str(root) else ()

        def descend(name: str) -> bool:
            if name in keep or os.path.normcase(os.path.join(dirpath, name)) in excluded:
                return False
            try:  # stay on the root's filesystem: no wandering into a mount under /tmp
                return os.stat(name, dir_fd=dfd, follow_symlinks=False).st_dev == dev
            except OSError:
                return False

        dirs[:] = [d for d in dirs if descend(d)]
        for name in files:
            if name in keep:
                continue
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                # Regular files only: symlinks, sockets (tmux lives in /tmp) and fifos stay.
                if not stat.S_ISREG(st.st_mode) or st.st_mtime > cutoff:
                    continue
                if delete:
                    os.unlink(name, dir_fd=dfd)
            except OSError:
                res.errors += 1
                continue
            _count(res, st.st_size)


def _files_nt(root: Path, t: Target, cutoff: float, delete: bool, res: Result) -> None:
    # No fwalk on Windows. Junctions and symlinks are reparse points and never
    # descended; each file is re-checked with lstat immediately before unlink.
    dev = os.lstat(root).st_dev  # DirEntry.stat() zeroes st_dev on Windows; lstat does not
    excluded = _excluded()
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            res.errors += 1
            continue
        for e in entries:
            if current == str(root) and e.name in t.keep:
                continue
            try:
                st = os.lstat(e.path)
                if st.st_file_attributes & REPARSE:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if st.st_dev == dev and os.path.normcase(e.path) not in excluded:
                        stack.append(e.path)
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_mtime > cutoff:
                    continue
                if delete:
                    os.unlink(e.path)  # locked (in use) files raise here and are counted as errors
            except OSError:
                res.errors += 1
                continue
            _count(res, st.st_size)


def _tree(path: str) -> tuple[int, int, float]:
    """(files, bytes, newest mtime) under path, without following links."""
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or getattr(st, "st_file_attributes", 0) & REPARSE:
        return 1, st.st_size, st.st_mtime
    files = size = 0
    newest = st.st_mtime
    for dirpath, dirs, names in os.walk(path):
        for name in dirs + names:
            try:
                s = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            newest = max(newest, s.st_mtime)  # a dir's mtime moves when entries are added or removed
            if not stat.S_ISDIR(s.st_mode):
                files += 1
                size += s.st_size
    return files, size, newest


def _entries(root: Path, t: Target, cutoff: float, delete: bool, res: Result) -> None:
    try:
        entries = list(os.scandir(root))
    except OSError:
        res.errors += 1
        return
    for e in entries:
        if e.name in t.keep:
            continue
        try:
            files, size, newest = _tree(e.path)
            if newest > cutoff:  # still in use: take all of it or none of it
                continue
            if delete:
                st = os.lstat(e.path)
                if stat.S_ISDIR(st.st_mode) and not getattr(st, "st_file_attributes", 0) & REPARSE:
                    shutil.rmtree(e.path)  # fd-based on POSIX: does not follow links inside
                elif stat.S_ISDIR(st.st_mode):
                    os.rmdir(e.path)  # a Windows junction: remove the link, not its target
                else:
                    os.unlink(e.path)
        except OSError:
            res.errors += 1
            continue
        res.files += files
        res.bytes += size
