"""Junk targets, and the walk that scans or deletes them.

Scan and clean are the same walk; clean deletes as it goes. Nothing is deleted
from a list built earlier, so a path swapped for a symlink between `scan` and
`clean --yes` is judged where it stands at delete time, not trusted from the
report. On POSIX the walk is os.fwalk, which holds a directory fd and never
follows a symlink, so a tmp cleaner run as root cannot be steered outside its
root by a link planted in /tmp.
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import stat
import string
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import wincleanup
from .system import OS, is_admin, output

HOUR = 3600
DAY = 24
WEEK = 168
COMMAND_TIMEOUT = 600  # a package manager clearing gigabytes on a slow disk takes minutes
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
    # "command": the roots are only measured; `command` does the deleting.
    #          A package manager's cache (npm, pip, uv, go) is structured, and
    #          its own cleaner is the one thing that knows which files belong
    #          together. What was freed is measured before and after.
    mode: str = "files"
    min_age_hours: float = 24
    admin: bool = False
    keep: tuple[str, ...] = ()  # top-level names under a root to leave alone
    # opt_in targets are never selected for the user. The Recycle Bin is the
    # only undo anybody has, so emptying it has to be a deliberate click.
    opt_in: bool = False
    group: str = "System"  # the section the GUI lists it under
    about: str = ""  # what it is and what deleting it costs, in one plain sentence
    # Only names matching one of these (lower-case fnmatch patterns) are
    # touched: files in files mode, top-level entries in entries mode. Log
    # folders hold more than logs; an extensions folder holds more than
    # abandoned installs.
    patterns: tuple[str, ...] = ()
    command: tuple[str, ...] = ()  # mode="command": argv, the program resolved on PATH
    # mode="command": the name of an entry in ESTIMATES, for when the roots are
    # far bigger than what the command removes (the whole driver store versus
    # its superseded packages). Only the scan uses it; a clean is measured.
    estimate: str = ""


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


def _chromium(user_data: Path | None) -> tuple[Path, ...]:
    """The regenerable caches inside a Chromium "User Data" folder, every profile.

    Profiles are "Default", "Profile 1", ...: a machine with only "Profile 1"
    has no Default at all. Cookies, history and Local Storage sit beside these
    folders and are never matched.
    """
    return _glob(
        user_data,
        "*/Cache",
        "*/Code Cache",
        "*/GPUCache",
        "*/DawnCache",
        "*/DawnGraphiteCache",
        "*/DawnWebGPUCache",
        "GrShaderCache",
        "GraphiteDawnCache",
        "ShaderCache",
        # Downloaded installers of components and extensions, kept after install.
        "component_crx_cache",
        "extensions_crx_cache",
    )


def _gecko(profiles: Path | None) -> tuple[Path, ...]:
    """The caches of every profile of a Firefox-based browser (Zen, LibreWolf...).

    On Windows these live under %LOCALAPPDATA%, apart from the profile itself
    (bookmarks, logins) in %APPDATA%, so nothing here can reach them.
    """
    return _glob(profiles, "*/cache2", "*/startupCache")


def _updaters(local: Path | None) -> tuple[Path, ...]:
    """%LOCALAPPDATA%\\<app>-updater folders of electron-updater (Termius, Obsidian...).

    Each holds the last downloaded installer and a pending update, hundreds of
    MB that the app never removes. The layout is required, not just the name:
    a folder that merely ends in "-updater" could be a program.
    """
    return tuple(
        d
        for d in _glob(local, "*-updater")
        if os.path.isdir(d / "pending") or os.path.isfile(d / "installer.exe")
    )


def _electron(base: Path | None) -> tuple[Path, ...]:
    """Chromium caches of Electron apps (Discord, VS Code, Slack...), one level under base."""
    return _glob(
        base,
        "*/Cache/Cache_Data",
        "*/Code Cache",
        "*/GPUCache",
        "*/DawnCache",
        "*/DawnGraphiteCache",
        "*/DawnWebGPUCache",
    )


def owners(roots: tuple[Path, ...], base: Path | None) -> str:
    """ "Code, discord, +2": the apps a set of cache roots belongs to."""
    names: list[str] = []
    for root in roots:
        try:
            name = root.relative_to(base).parts[0] if base else ""
        except (ValueError, IndexError):
            continue
        if name and name not in names:
            names.append(name)
    shown = ", ".join(names[:3])
    return f"{shown}, +{len(names) - 3}" if len(names) > 3 else shown


def _browser(id: str, label: str, roots: tuple[Path, ...]) -> Target:
    return Target(
        id,
        label,
        roots,
        group="Browsers",
        about="Pages and images kept to load sites faster. Logins, history and tabs are not touched.",
    )


EDITOR_HOMES = (".vscode", ".vscode-insiders", ".vscode-oss", ".cursor", ".windsurf",
                # the servers Remote-SSH installs on the far machine keep their own
                ".vscode-server", ".vscode-server-insiders", ".cursor-server", ".windsurf-server")
# VS Code extracts a VSIX into extensions/.<uuid> and renames it into place.
# When the rename fails (the old version's binary was running) the folder
# stays, unregistered in extensions.json, forever. Real extension folders are
# publisher.name-version, which this never matches.
PARTIAL_INSTALL = ".????????-????-????-????-????????????"


def _unreal_saved(local: Path | None, *sub: str) -> tuple[Path, ...]:
    """%LOCALAPPDATA%\\<game>\\Saved\\<sub> of Unreal Engine games (Wukong, Clair Obscur...)."""
    return _glob(local, *(f"*/Saved/{s}" if s else "*/Saved" for s in sub))


def _editors(base: Path | None) -> Target:
    """VS Code and its forks keep bytecode per version, downloaded extension
    packages and a log folder per session. Whole entries, a week unmodified."""
    roots: tuple[Path, ...] = ()
    if base is not None:
        roots = tuple(
            base / name / sub
            for name in ("Code", "Code - Insiders", "Cursor", "VSCodium", "Windsurf")
            for sub in ("CachedData", "CachedExtensionVSIXs", "logs")
        )
    return Target(
        "editor-cache",
        "Code editor leftovers",
        roots,
        mode="entries",
        min_age_hours=WEEK,
        group="Developer tools",
        about="Caches of old VS Code/Cursor versions, downloaded extension installers, old session logs.",
    )


def _partial_installs(home: Path) -> Target:
    return Target(
        "editor-partial-installs",
        "Unfinished extension installs",
        tuple(home / name / "extensions" for name in EDITOR_HOMES),
        mode="entries",
        min_age_hours=DAY,  # never one being installed right now
        patterns=(PARTIAL_INSTALL,),
        group="Developer tools",
        about="Extension updates VS Code/Cursor unpacked but never finished installing. Installed ones stay.",
    )


def _tools(
    npm: Path, pip: Path | None, uv: Path | None, yarn: Path | None, go: Path | None, pub: Path | None
) -> list[Target]:
    """Package-manager caches, each cleared by the tool that owns it.

    Opt-in: all of it is regenerable, but the next install downloads it again,
    which on a slow or metered link is a cost nobody should pay by default.
    """

    def tool(id: str, label: str, root: Path | None, command: tuple[str, ...], about: str) -> Target:
        return Target(id, label, _paths(root), mode="command", command=command, opt_in=True,
                      group="Developer tools", about=about)

    return [
        tool("npm-cache", "npm cache", npm / "_cacache", ("npm", "cache", "clean", "--force"),
             "Packages npm downloaded. The next npm install fetches them again."),
        tool("pip-cache", "pip cache", pip, ("pip", "cache", "purge"),
             "Wheels and downloads pip kept. The next pip install fetches them again."),
        tool("uv-cache", "uv cache", uv, ("uv", "cache", "clean"),
             "Every package uv downloaded or built. Virtual environments are not touched."),
        tool("yarn-cache", "Yarn cache", yarn, ("yarn", "cache", "clean"),
             "Packages Yarn downloaded. The next yarn install fetches them again."),
        tool("go-cache", "Go build cache", go, ("go", "clean", "-cache"),
             "Compiled Go packages. The next build is slower once."),
        tool("pub-cache", "Dart/Flutter pub cache", pub, ("dart", "pub", "cache", "clean", "--force"),
             "Packages pub downloaded. The next flutter pub get fetches them again."),
        # `npm cache clean` leaves _npx alone, and on the dev box it held
        # 2.5 GB against 600 MB in _cacache. Each entry is one npx command's
        # install, so an entry goes whole, and only when unused for a week:
        # an MCP server started with npx runs from here.
        Target("npx-cache", "npx packages", (npm / "_npx",), mode="entries", min_age_hours=WEEK, opt_in=True,
               group="Developer tools",
               # "Unused" cannot be known: running a cached package changes no
               # mtime, and NTFS last-access times are usually off. The text says
               # what the age really measures.
               about="Packages npx installed to run a command. Ones not installed or updated for a week go;"
               " the next npx run downloads them again."),
    ]


BIN_ABOUT = "Everything you deleted. Emptying it is permanent."


def targets() -> list[Target]:
    home = Path.home()
    if OS == "windows":
        local, roaming = _env("LOCALAPPDATA"), _env("APPDATA")
        windir, programdata = _env("SystemRoot"), _env("ProgramData")
        drive = _env("SystemDrive") and Path(os.environ["SystemDrive"] + "\\")
        wer = programdata and programdata / "Microsoft" / "Windows" / "WER"
        # Steam's store runs in an embedded Chromium with the same layout.
        apps = _electron(roaming) + _chromium(local and local / "Steam" / "htmlcache")
        return [
            Target("user-temp", "Your temp files", _paths(_env("TEMP")),
                   about="Files programs left in your temp folder, older than a day."),
            Target("system-temp", "Windows temp files", _paths(windir and windir / "Temp"), admin=True,
                   about="Files installers and services left in the Windows temp folder."),
            Target("update-cache", "Windows Update downloads",
                   _paths(windir and windir / "SoftwareDistribution" / "Download"), admin=True,
                   about="Update packages Windows has already installed."),
            Target(
                "delivery-optimization",
                "Delivery Optimization cache",
                _paths(windir and windir / "ServiceProfiles" / "NetworkService" / "AppData" / "Local"
                       / "Microsoft" / "Windows" / "DeliveryOptimization" / "Cache"),
                mode="command",
                admin=True,
                command=("powershell", "-NoProfile", "-NonInteractive", "-Command",
                         "Delete-DeliveryOptimizationCache -Force"),
                about="Update pieces Windows keeps to share with other PCs. Cleared by Windows' own command.",
            ),
            Target("crash-dumps", "Crash dumps and error reports",
                   _paths(local and local / "CrashDumps", local and local / "Microsoft" / "Windows" / "WER"),
                   about="Memory dumps and reports from programs that crashed. Only their developers use them."),
            Target(
                "system-error-reports",
                "System error reports",
                _paths(wer and wer / "ReportArchive", wer and wer / "ReportQueue", wer and wer / "Temp",
                       windir and windir / "Minidump", windir and windir / "LiveKernelReports",
                       # Services crash as SYSTEM, and their dumps land in its
                       # profile: 126 MB of them on the dev desktop.
                       windir and windir / "System32" / "config" / "systemprofile" / "AppData" / "Local"
                       / "CrashDumps"),
                admin=True,
                about="Windows Error Reporting archives, blue-screen minidumps and crash dumps of Windows services.",
            ),
            Target(
                "windows-logs",
                "Old Windows logs",
                _paths(windir and windir / "Logs"),
                min_age_hours=WEEK,
                admin=True,
                # Measured Boot logs are TPM evidence for device attestation, not diagnostics.
                keep=("MeasuredBoot",),
                patterns=("*.log", "*.etl", "*.cab"),
                about="Setup, servicing and update logs older than a week. Logs in use are skipped.",
            ),
            Target(
                "driver-packages",
                "Old driver versions",
                _paths(windir and windir / "System32" / "DriverStore" / "FileRepository"),
                mode="command",
                admin=True,
                # Opt-in: a removed old version is a driver you can no longer roll back to.
                opt_in=True,
                command=wincleanup.command(("Device Driver Packages",)),
                estimate="old_driver_packages",
                about="Older versions of drivers kept after updates. Removed by Windows' own Disk Cleanup;"
                " drivers in use stay. Afterwards a driver cannot be rolled back.",
            ),
            Target(
                "windows-upgrade-files",
                "Windows upgrade leftovers",
                _paths(*(drive and drive / name for name in ("$WINDOWS.~BT", "$WINDOWS.~WS", "ESD", "Windows.old"))),
                mode="command",
                admin=True,
                opt_in=True,  # Windows.old is the only way back to the previous Windows
                command=wincleanup.command((
                    "Temporary Setup Files",
                    "Windows ESD installation files",
                    "Previous Installations",
                    "Windows Upgrade Log Files",
                    "Upgrade Discarded Files",
                )),
                about="Setup files and the previous Windows (Windows.old) from an upgrade. Removed by Disk Cleanup;"
                " afterwards you cannot go back to the earlier Windows.",
            ),
            Target(
                "shader-cache",
                "GPU shader caches",
                _paths(local and local / "D3DSCache")
                + _glob(local and local / "NVIDIA", "DXCache", "GLCache")
                + _glob(home / "AppData" / "LocalLow" / "NVIDIA", "DXCache", "GLCache", "PerDriverVersion/DXCache")
                + _glob(local and local / "AMD", "DxCache", "DxcCache", "VkCache", "GLCache")
                + _glob(local and local / "Intel", "ShaderCache"),
                min_age_hours=WEEK,
                opt_in=True,
                about="Compiled shaders. Games rebuild them, so the first launch after cleaning may stutter.",
            ),
            Target(
                "game-shader-cache",
                "Game shader caches",
                _unreal_saved(local, ""),
                min_age_hours=WEEK,
                opt_in=True,
                # Only the driver bytecode blob: Saved also holds the save games.
                patterns=("d3ddriverbytecodeblob_*.ushaderprecache",),
                about="Shaders Unreal Engine games compiled for your driver. The game rebuilds them on its next launch.",
            ),
            Target(
                "nvidia-updates",
                "NVIDIA driver update files",
                _paths(programdata and programdata / "NVIDIA Corporation" / "NVIDIA App" / "UpdateFramework"
                       / "ota-artifacts"),
                mode="entries",
                min_age_hours=DAY,  # never mid-install
                admin=True,
                about="Driver packages the NVIDIA App downloaded and never removes after installing them.",
            ),
            _browser("chrome-cache", "Google Chrome", _chromium(local and local / "Google" / "Chrome" / "User Data")),
            _browser("edge-cache", "Microsoft Edge", _chromium(local and local / "Microsoft" / "Edge" / "User Data")),
            _browser("brave-cache", "Brave",
                     _chromium(local and local / "BraveSoftware" / "Brave-Browser" / "User Data")),
            _browser("vivaldi-cache", "Vivaldi", _chromium(local and local / "Vivaldi" / "User Data")),
            _browser("opera-cache", "Opera", _glob(local and local / "Opera Software", "*/Cache")),
            _browser("firefox-cache", "Firefox", _gecko(local and local / "Mozilla" / "Firefox" / "Profiles")),
            _browser("zen-cache", "Zen", _gecko(local and local / "zen" / "Profiles")),
            _browser("librewolf-cache", "LibreWolf", _gecko(local and local / "librewolf" / "Profiles")),
            _browser("floorp-cache", "Floorp", _gecko(local and local / "Floorp" / "Profiles")),
            _browser("waterfox-cache", "Waterfox", _gecko(local and local / "Waterfox" / "Profiles")),
            Target(
                "app-cache",
                f"App caches ({owners(apps, roaming) or 'Discord, Slack, Teams…'})",
                apps,
                group="Apps",
                about="Web caches of Electron apps and Steam. Settings and logins are not touched.",
            ),
            Target(
                "game-reports",
                "Game crash reports and logs",
                _unreal_saved(local, "Crashes", "Logs"),
                min_age_hours=WEEK,
                group="Apps",
                about="Crash reports and old logs Unreal Engine games leave behind. Saves are not touched.",
            ),
            Target(
                "updater-cache",
                "App update downloads",
                _updaters(local) + _paths(local and local / "Microsoft" / "PowerToys" / "Updates"),
                min_age_hours=WEEK,
                group="Apps",
                about="Installers apps downloaded to update themselves and kept afterwards.",
            ),
            _editors(roaming),
            _partial_installs(home),
            Target(
                "jetbrains-client",
                "JetBrains remote client",
                _paths(local and local / "JetBrains" / "JetBrainsClientDist"),
                mode="entries",
                min_age_hours=WEEK,
                # Opt-in: its mtime shows when it was downloaded, not when it
                # was last used, so a daily Gateway user would re-download 2 GB.
                opt_in=True,
                group="Developer tools",
                about="The IDE client JetBrains Gateway and Code With Me download. Versions not updated for a"
                " week go; the next remote session downloads about 2 GB again.",
            ),
            *_tools(
                npm=local / "npm-cache" if local else home / ".npm",
                pip=local and local / "pip" / "cache",
                uv=local and local / "uv" / "cache",
                yarn=local and local / "Yarn" / "Cache",
                go=local and local / "go-build",
                pub=local and local / "Pub" / "Cache",
            ),
            Target(
                "recycle-bin",
                "Recycle Bin",
                _windows_recycle_bins(),
                mode="entries",
                min_age_hours=0,
                keep=("desktop.ini",),
                opt_in=True,
                group="Recycle Bin",
                about=BIN_ABOUT,
            ),
        ]
    if OS == "macos":
        caches = home / "Library" / "Caches"
        developer = home / "Library" / "Developer"
        return [
            Target("user-cache", "App caches", (caches,), mode="entries", min_age_hours=WEEK,
                   keep=("pip", "Yarn", "go-build"),  # left to the tool targets, which clear them properly
                   group="Apps", about="Folders apps keep to start faster, unused for a week."),
            Target("logs", "App logs", (home / "Library" / "Logs",), min_age_hours=WEEK,
                   about="Logs and crash reports older than a week."),
            Target("tmp", "Temp files", _paths(_env("TMPDIR")),
                   about="Files programs left in your temp folder, older than a day."),
            Target(
                "xcode-cache",
                "Xcode build data",
                (developer / "Xcode" / "DerivedData", developer / "CoreSimulator" / "Caches"),
                mode="entries",
                min_age_hours=WEEK,
                group="Developer tools",
                about="Build products and simulator caches of projects untouched for a week.",
            ),
            _editors(home / "Library" / "Application Support"),
            _partial_installs(home),
            *_tools(npm=home / ".npm", pip=caches / "pip", uv=home / ".cache" / "uv",
                    yarn=caches / "Yarn", go=caches / "go-build", pub=home / ".pub-cache"),
            Target("trash", "Trash", (home / ".Trash",), mode="entries", min_age_hours=0, opt_in=True,
                   group="Recycle Bin", about=BIN_ABOUT),
        ]
    cache = _env("XDG_CACHE_HOME") or home / ".cache"
    data = _env("XDG_DATA_HOME") or home / ".local" / "share"
    return [
        # Regenerable by definition, but these re-download gigabytes (browsers
        # for Playwright, model weights), which nobody asking for a clean wants.
        # Package managers' folders are left to the tool targets further down.
        Target(
            "user-cache",
            "App caches",
            (cache,),
            mode="entries",
            min_age_hours=WEEK,
            keep=("ms-playwright", "huggingface", "torch", "pip", "uv", "yarn", "go-build", "thumbnails"),
            group="Apps",
            about="Folders apps keep to start faster, unused for a week.",
        ),
        Target("thumbnails", "Thumbnails", (cache / "thumbnails",), min_age_hours=30 * DAY, group="Apps",
               about="Picture previews the file manager made. Rebuilt when needed."),
        Target("tmp", "Temp files", (Path("/tmp"),),
               about="Files programs left in /tmp, older than a day. Sockets and pipes stay."),
        Target(
            "apt-cache",
            "Downloaded .deb packages",
            (Path("/var/cache/apt/archives"),),
            min_age_hours=0,
            admin=True,
            keep=("lock", "partial"),
            about="Packages apt already installed.",
        ),
        Target(
            "rotated-logs",
            "Old rotated logs",
            (Path("/var/log"),),
            min_age_hours=WEEK,
            admin=True,
            # Rotated copies only. Live logs and the journal (*.journal) stay.
            patterns=("*.gz", "*.xz", "*.old", "*.[0-9]"),
            about="Compressed and rotated copies of system logs, a week or older.",
        ),
        Target(
            "crash-reports",
            "Crash reports",
            (Path("/var/crash"), Path("/var/lib/systemd/coredump")),
            admin=True,
            about="Core dumps and apport reports from programs that crashed.",
        ),
        _editors(_env("XDG_CONFIG_HOME") or home / ".config"),
        _partial_installs(home),
        *_tools(npm=home / ".npm", pip=cache / "pip", uv=cache / "uv", yarn=cache / "yarn", go=cache / "go-build",
                pub=home / ".pub-cache"),
        Target(
            "trash",
            "Trash",
            (data / "Trash" / "files", data / "Trash" / "info"),
            mode="entries",
            min_age_hours=0,
            opt_in=True,
            group="Recycle Bin",
            about=BIN_ABOUT,
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


def present(target: Target) -> bool:
    """Whether this target exists on this machine at all. Cheap: nothing is walked."""
    if target.admin and not is_admin():
        # Its folder may be unreadable (Delivery Optimization's is), which is
        # not the same as absent: let run() say "needs admin".
        return True
    if target.mode == "command" and not shutil.which(target.command[0]):
        return False
    return any(os.path.isdir(r) for r in target.roots)


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
    if target.mode == "command":
        _command([r for r in roots if safe_root(r)], target, delete, res)
        return res
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


def _wanted(name: str, t: Target) -> bool:
    return not t.patterns or any(fnmatch.fnmatchcase(name.lower(), p) for p in t.patterns)


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
                if not stat.S_ISREG(st.st_mode) or st.st_mtime > cutoff or not _wanted(name, t):
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
                if not stat.S_ISREG(st.st_mode) or st.st_mtime > cutoff or not _wanted(e.name, t):
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
        if e.name in t.keep or not _wanted(e.name, t):
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


def _measure(roots: list[Path]) -> tuple[int, int]:
    files = size = 0
    for root in roots:
        try:
            f, b, _ = _tree(str(root))
        except OSError:
            continue
        files += f
        size += b
    return files, size


def _command(roots: list[Path], t: Target, delete: bool, res: Result) -> None:
    """Measure a tool-owned cache; with delete, run the tool's cleaner and report the difference.

    Nothing here unlinks a file. A scan reports the whole cache, which is what
    these cleaners remove; what a delete freed is measured, not assumed.
    """
    exe = shutil.which(t.command[0])
    if not exe:
        res.skipped = f"{t.command[0]} not installed"
        return
    if not roots:
        res.skipped = "not present"
        return
    if not delete:
        res.files, res.bytes = ESTIMATES[t.estimate]() if t.estimate else _measure(roots)
        return
    before = _measure(roots)
    try:
        done = subprocess.run(
            [exe, *t.command[1:]],
            # Never the caller's cwd: Yarn 2+ `cache clean` clears the cache of
            # the project it is run in, and a zero-install repo commits that.
            # Not the cache itself either: Windows cannot remove a process's
            # current directory, so `uv cache clean` emptied it and exited 1.
            cwd=roots[0].parent,
            capture_output=True,
            stdin=subprocess.DEVNULL,  # a cleaner that asks y/n reads EOF instead of hanging
            timeout=COMMAND_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flashing up over the GUI
        )
        failed = done.returncode != 0
    except (OSError, subprocess.SubprocessError):
        failed = True
    files, size = _measure(roots)
    res.files, res.bytes = max(0, before[0] - files), max(0, before[1] - size)
    if failed:
        res.errors += 1


ESTIMATES = {"old_driver_packages": wincleanup.old_driver_packages}
