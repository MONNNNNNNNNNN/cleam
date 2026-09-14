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


def list_apps() -> list[App]:
    apps = {"windows": _windows, "macos": _macos}.get(OS, _linux)()
    return sorted(apps, key=lambda a: a.name.lower())


def uninstall(app: App) -> int:
    cmd = app.command
    if app.source in ("apt", "snap"):
        cmd = sudo(cmd)  # flatpak asks polkit itself
    return subprocess.run(cmd).returncode


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


def parse_dpkg(text: str, manual: set[str]) -> list[App]:
    # Manually installed only: listing every library dpkg knows about buries the
    # apps. Cloud images also mark the base system manual, so drop what dpkg
    # calls essential, required or important -- bash is not an app to uninstall.
    apps = []
    for line in text.splitlines():
        pkg, version, priority, essential = (line.split("\t") + ["", "", ""])[:4]
        if pkg in manual and essential != "yes" and priority not in ("required", "important"):
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
        apps += parse_dpkg(
            output(["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Priority}\t${Essential}\n"]),
            set(output(["apt-mark", "showmanual"]).split()),
        )
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
