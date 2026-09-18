"""Installed programs, and handing their removal to the platform's own uninstaller.

Cleam never deletes an app's files itself: it runs the command the platform
registered (Windows UninstallString, apt, snap, flatpak, Finder's Trash), so
the uninstaller's own prompts and cleanup still happen.
"""
from __future__ import annotations

import itertools
import plistlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .system import OS, output, sudo


@dataclass
class App:
    id: str
    name: str
    version: str
    source: str  # registry | apt | snap | flatpak | app
    command: list[str] | str  # str = a raw Windows command line, passed to CreateProcess as-is


BIN_DIRS = ("/usr/bin/", "/usr/sbin/", "/bin/", "/sbin/", "/usr/games/", "/opt/")


def is_library(package: str, file_list: str) -> bool:
    """True only for a lib* package that ships no program of its own.

    libxkbcommon0 is a leaf nothing depends on, so the leaf rule alone keeps
    it, and nobody means a shared object when they say "uninstall an app".
    The name check has to be there too: requiring an executable in /usr/bin
    hid wazuh-manager (it installs into /var/ossec) and the docker CLI plugins
    (/usr/libexec). Showing one extra row beats hiding something real.
    """
    if not package.startswith("lib"):
        return False
    return not any(line.startswith(BIN_DIRS) for line in file_list.splitlines())


def _dpkg_file_list(package: str) -> str:
    # Multi-arch packages are recorded as <name>:<arch>.list.
    from glob import glob

    for path in (f"/var/lib/dpkg/info/{package}.list", *glob(f"/var/lib/dpkg/info/{package}:*.list")):
        if text := _read(path):
            return text
    return ""


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def list_apps() -> list[App]:
    apps = {"windows": _windows, "macos": _macos}.get(OS, _linux)()
    return sorted(apps, key=lambda a: a.name.lower())


def uninstall(app: App, capture: bool = False, timeout: float | None = None) -> tuple[int, str]:
    """(exit code, output). capture=True is for the GUI, which has no terminal.

    Without a terminal there is nobody to answer apt's y/n or sudo's password
    prompt, so pass the confirmation up front and let sudo fail fast instead.

    A Windows uninstaller is never captured. It has its own window, writes
    nothing useful to a pipe, and commonly relaunches itself (Au_.exe,
    _isdel.exe); the child inherits the pipe handles, so reading to EOF can
    block long after the uninstaller is done -- which would hang the GUI with
    no way out.
    """
    capture = capture and app.source != "registry"
    cmd = app.command
    if capture and app.source in ("apt", "flatpak"):
        cmd = [*cmd, "-y"]
    if app.source in ("apt", "snap"):
        cmd = sudo(cmd, noninteractive=capture)  # flatpak asks polkit itself
    try:
        if capture:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout + p.stderr).strip()
        return subprocess.run(cmd).returncode, ""
    except subprocess.TimeoutExpired:
        return 1, f"{app.name}: no answer after {timeout:.0f}s. It may still be running."
    except OSError as e:
        return 1, str(e)


# --- Windows -----------------------------------------------------------------

UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


def msi_uninstall(cmd: str) -> str:
    """MsiExec /I{GUID} opens the repair/modify dialog; /X{GUID} is the uninstall."""
    return re.sub(r"(?i)(msiexec(?:\.exe)?\"?\s+)/I(?=\{)", r"\1/X", cmd.strip())


def from_registry(key: str, values: dict) -> App | None:
    name, cmd = values.get("DisplayName"), values.get("UninstallString")
    # SystemComponent hides an entry from Programs and Features; ParentKeyName marks an update/patch.
    if not name or not cmd or values.get("SystemComponent") == 1 or values.get("ParentKeyName"):
        return None
    return App(key, str(name), str(values.get("DisplayVersion", "")), "registry", msi_uninstall(str(cmd)))


def _windows() -> list[App]:
    import winreg

    views = [
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),  # WOW6432Node: 32-bit installers
        (winreg.HKEY_CURRENT_USER, 0),
    ]
    found: dict[str, App] = {}
    for hive, view in views:
        access = winreg.KEY_READ | view
        try:
            root = winreg.OpenKey(hive, UNINSTALL_KEY, 0, access)
        except OSError:
            continue
        with root:
            for i in itertools.count():
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(root, sub, 0, access) as key:
                        values = {}
                        for j in itertools.count():
                            try:
                                n, v, _ = winreg.EnumValue(key, j)
                            except OSError:
                                break
                            values[n] = v
                except OSError:
                    continue
                if (app := from_registry(sub, values)) and sub not in found:
                    found[sub] = app
    return list(found.values())


# --- Linux -------------------------------------------------------------------


def needed_by_others(status: str) -> set[str]:
    """Every package named in another installed package's Depends or Pre-Depends.

    Being manually installed is not enough to call something an app: cloud
    images mark half the base system manual, so `acl` and `libssl` sat in the
    list next to Firefox. A package nothing else depends on is a leaf -- the
    thing somebody actually chose to install.
    """
    needed: set[str] = set()
    for line in status.splitlines():
        field, _, value = line.partition(":")
        if field.strip() not in ("Depends", "Pre-Depends"):
            continue
        for clause in value.split(","):
            for alternative in clause.split("|"):
                name = alternative.strip().split()[0] if alternative.strip() else ""
                if name:
                    needed.add(name.split(":")[0])  # strip a :arch qualifier
    return needed


def parse_dpkg(text: str, manual: set[str], needed: set[str] = frozenset()) -> list[App]:
    # Manually installed leaves only. Dropping what dpkg calls essential,
    # required or important removes the base system (bash is not an app to
    # uninstall); dropping anything another package depends on removes the
    # libraries and build tools that came along for the ride.
    apps = []
    for line in text.splitlines():
        pkg, version, priority, essential = (line.split("\t") + ["", "", ""])[:4]
        if pkg not in manual or essential == "yes" or priority in ("required", "important"):
            continue
        if pkg in needed:
            continue
        apps.append(App(pkg, pkg, version, "apt", ["apt-get", "remove", pkg]))
    return apps


def parse_snap(text: str) -> list[App]:
    apps = []
    for line in text.splitlines()[1:]:  # header: Name Version Rev Tracking Publisher Notes
        parts = line.split()
        if len(parts) < 6 or parts[0] == "snapd" or parts[-1] in ("base", "core", "snapd"):
            continue
        apps.append(App(parts[0], parts[0], parts[1], "snap", ["snap", "remove", parts[0]]))
    return apps


def parse_flatpak(text: str) -> list[App]:
    apps = []
    for line in text.splitlines():  # --columns=application,name,version, tab-separated when piped
        app_id, name, version = (line.split("\t") + ["", ""])[:3]
        if app_id:
            apps.append(App(app_id, name or app_id, version, "flatpak", ["flatpak", "uninstall", app_id]))
    return apps


def _linux() -> list[App]:
    apps: list[App] = []
    if shutil.which("apt-mark"):
        leaves = parse_dpkg(
            output(["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Priority}\t${Essential}\n"]),
            set(output(["apt-mark", "showmanual"]).split()),
            needed_by_others(_read("/var/lib/dpkg/status")),
        )
        apps += [a for a in leaves if not is_library(a.id, _dpkg_file_list(a.id))]
    if shutil.which("snap"):
        apps += parse_snap(output(["snap", "list"]))
    if shutil.which("flatpak"):
        apps += parse_flatpak(output(["flatpak", "list", "--app", "--columns=application,name,version"]))
    return apps


# --- macOS -------------------------------------------------------------------


def _macos() -> list[App]:
    apps = []
    for base in (Path("/Applications"), Path.home() / "Applications"):
        for bundle in base.glob("*.app"):
            try:
                with open(bundle / "Contents" / "Info.plist", "rb") as f:
                    version = str(plistlib.load(f).get("CFBundleShortVersionString", ""))
            except (OSError, plistlib.InvalidFileException):
                version = ""
            # Finder's delete goes to the Trash, so Put Back still works.
            quoted = str(bundle).replace("\\", "\\\\").replace('"', '\\"')
            script = f'tell application "Finder" to delete POSIX file "{quoted}"'
            apps.append(App(str(bundle), bundle.stem, version, "app", ["osascript", "-e", script]))
    return apps
