"""What an uninstaller leaves behind, and how to remove it reversibly.

A Windows uninstaller usually removes the program and forgets the settings
folder, the publisher's registry key and the Start Menu shortcut. apt removes
the package and keeps /etc config unless you purge. This module finds those
remnants by name, and every removal is a *move into a backup folder* plus a
manifest, so `cleam restore` can put it all back.

Two rules keep it honest:

- Only immediate children of known roots are considered, never a deep walk.
  A remnant lives at `%APPDATA%\\Publisher`, not eleven levels down, and a
  name-matching walk of a whole drive is how these tools delete the wrong
  thing.
- A candidate that matches another installed program is dropped. Removing
  "Google Earth" must not offer `%LOCALAPPDATA%\\Google`, which Chrome shares.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .overview import size
from .system import OS, output, sudo

# Words too generic to identify anything. "Microsoft" alone must never match.
NOISE = frozenset(
    {"app", "apps", "bin", "cache", "com", "common", "config", "data", "exe", "files",
     "inc", "limited", "llc", "ltd", "microsoft", "programs", "software", "temp", "the"}
)


@dataclass
class Leftover:
    kind: str  # folder | file | registry | shortcut | package-config
    target: str  # a filesystem path, a registry key, or a package name
    reason: str
    confidence: str  # high (ticked by default) | low (never ticked by default)
    bytes: int = 0
    command: list[str] = field(default_factory=list)  # set for package-config


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def tokens(text: str) -> list[str]:
    return [t for t in normalize(text).split() if len(t) > 2 and t not in NOISE]


def last_path_part(path: str) -> str:
    """The final component of a path from either OS.

    os.path.basename is platform-specific: on Linux it returns a whole Windows
    path unchanged, so an install directory read from the registry produced no
    usable alias at all.
    """
    return re.split(r"[\\/]+", path.strip("\\/ "))[-1] if path.strip("\\/ ") else ""


def aliases_for(name: str, publisher: str = "", install_dir: str = "") -> list[list[str]]:
    """Token lists worth matching a folder or key name against."""
    basename = last_path_part(install_dir)
    found = [tokens(text) for text in (name, publisher, basename) if text]
    return [alias for alias in found if alias]


def score(candidate: str, aliases: list[list[str]]) -> str:
    """"high" for the same name, "low" for a name that contains one, else ""."""
    candidate_tokens = set(tokens(candidate))
    if not candidate_tokens:
        return ""
    for alias in aliases:
        if candidate_tokens == set(alias):
            return "high"
    for alias in aliases:
        if set(alias) <= candidate_tokens or (len(alias) == 1 and alias[0] in candidate_tokens):
            return "low"
    return ""


def _roots() -> list[tuple[Path, str]]:
    home = Path.home()
    if OS == "windows":
        candidates = [
            (os.environ.get("LOCALAPPDATA"), "folder"),
            (os.environ.get("APPDATA"), "folder"),
            (os.environ.get("PROGRAMDATA"), "folder"),
            (os.environ.get("ProgramFiles"), "folder"),
            (os.environ.get("ProgramFiles(x86)"), "folder"),
            (os.environ.get("APPDATA", "") + r"\Microsoft\Windows\Start Menu\Programs", "shortcut"),
        ]
        return [(Path(p), kind) for p, kind in candidates if p and os.path.isdir(p)]
    if OS == "macos":
        library = home / "Library"
        found = [(library / part, "folder") for part in ("Application Support", "Caches", "Logs", "Preferences")]
        found.append((library / "LaunchAgents", "file"))
        return [(p, kind) for p, kind in found if os.path.isdir(p)]
    found = [
        (home / ".config", "folder"),
        (home / ".local" / "share", "folder"),
        (home / ".cache", "folder"),
        (Path("/etc"), "folder"),
        (Path("/opt"), "folder"),
    ]
    return [(p, kind) for p, kind in found if os.path.isdir(p)]


def registry_roots() -> list[tuple[str, str]]:
    """(hive name, subkey) pairs whose immediate children are per-app keys."""
    return [
        ("HKCU", r"Software"),
        ("HKLM", r"SOFTWARE"),
        ("HKLM", r"SOFTWARE\WOW6432Node"),
    ]


def _registry_leftovers(aliases: list[list[str]], blocked: set[str]) -> list[Leftover]:
    if OS != "windows":
        return []
    import winreg

    hives = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
    found: list[Leftover] = []
    for hive_name, subkey in registry_roots():
        try:
            key = winreg.OpenKey(hives[hive_name], subkey, 0, winreg.KEY_READ)
        except OSError:
            continue
        with key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    child = winreg.EnumKey(key, i)
                except OSError:
                    break
                confidence = score(child, aliases)
                if not confidence or normalize(child) in blocked:
                    continue
                found.append(
                    Leftover("registry", f"{hive_name}\\{subkey}\\{child}", "registry key named after the program", confidence)
                )
    return found


def _package_config_leftovers(name: str) -> list[Leftover]:
    """apt keeps config for a removed package until it is purged (dpkg state "rc")."""
    if OS != "linux":
        return []
    found = []
    for line in output(["dpkg", "-l"]).splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] != "rc":
            continue
        package = parts[1].split(":")[0]
        if score(package, [tokens(name)]) or normalize(package) == normalize(name):
            found.append(
                Leftover(
                    "package-config",
                    package,
                    "package removed, its configuration is still installed",
                    "high",
                    command=sudo(["apt-get", "purge", "-y", package]),
                )
            )
    return found


def scan(name: str, publisher: str = "", install_dir: str = "", other_apps: tuple[str, ...] = ()) -> list[Leftover]:
    """Remnants that look like they belong to `name`, high confidence first."""
    aliases = aliases_for(name, publisher, install_dir)
    if not aliases:
        return []
    # Never offer something that is plainly another installed program.
    blocked = {normalize(other) for other in other_apps if normalize(other) != normalize(name)}
    found: list[Leftover] = []
    for root, kind in _roots():
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            confidence = score(entry.name, aliases)
            if not confidence or normalize(entry.name) in blocked:
                continue
            files, total, _ = size(entry.path)
            found.append(
                Leftover(kind, entry.path, f"{root} entry named after the program ({files} files)", confidence, total)
            )
    found += _registry_leftovers(aliases, blocked)
    found += _package_config_leftovers(name)
    found.sort(key=lambda item: (item.confidence != "high", -item.bytes, item.target))
    return found


def safe_label(label: str) -> str:
    """A program name turned into one harmless folder name.

    Program names carry anything -- "Node.js", "C++ Runtime", a crafted name in
    a registry value. Separators become dashes, runs of dots collapse (so no
    ".." ever lands in a path), and the result is never empty.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", label)
    cleaned = re.sub(r"\.{2,}", ".", cleaned).strip("._-")
    return cleaned[:60] or "app"


def backup_dir() -> Path:
    if OS == "windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Cleam" / "backups"
    elif OS == "macos":
        base = Path.home() / "Library" / "Application Support" / "Cleam" / "backups"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "cleam" / "backups"
    return base


def remove(items: list[Leftover], label: str = "app") -> tuple[Path, list[str]]:
    """Move every item into a fresh backup folder and record how to undo it.

    Nothing is deleted outright. Files and folders are moved, registry keys are
    exported with `reg export` before they are removed, and package configs are
    purged (apt has no backup, so the manifest says so).
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup_dir() / f"{safe_label(label)}-{stamp}"
    (backup / "files").mkdir(parents=True, exist_ok=True)
    (backup / "registry").mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    errors: list[str] = []
    for index, item in enumerate(items):
        try:
            if item.kind == "registry":
                export = backup / "registry" / f"{index}.reg"
                done = subprocess.run(
                    ["reg", "export", item.target, str(export), "/y"], capture_output=True, text=True
                )
                if done.returncode != 0:
                    errors.append(f"{item.target}: could not export, left in place")
                    continue
                deleted = subprocess.run(["reg", "delete", item.target, "/f"], capture_output=True, text=True)
                if deleted.returncode != 0:
                    errors.append(f"{item.target}: {deleted.stderr.strip() or 'could not delete'}")
                    continue
                manifest.append({**asdict(item), "backup": str(export)})
            elif item.kind == "package-config":
                done = subprocess.run(item.command, capture_output=True, text=True)
                if done.returncode != 0:
                    errors.append(f"{item.target}: {done.stderr.strip() or 'purge failed'}")
                    continue
                manifest.append({**asdict(item), "backup": "", "note": "apt purge cannot be undone"})
            else:
                # Computed outside the f-string: a backslash in an f-string
                # expression is a syntax error before Python 3.12.
                stem = os.path.basename(item.target.rstrip("\\/")) or "item"
                destination = backup / "files" / f"{index}-{stem}"
                shutil.move(item.target, destination)
                manifest.append({**asdict(item), "backup": str(destination)})
        except (OSError, subprocess.SubprocessError) as e:
            errors.append(f"{item.target}: {e}")
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return backup, errors


def restore(backup: Path | str) -> list[str]:
    """Put a backup back. Returns whatever could not be restored."""
    backup = Path(backup)
    try:
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"{backup}: {e}"]
    errors: list[str] = []
    for entry in manifest:
        source, target, kind = entry.get("backup", ""), entry["target"], entry["kind"]
        try:
            if kind == "registry":
                done = subprocess.run(["reg", "import", source], capture_output=True, text=True)
                if done.returncode != 0:
                    errors.append(f"{target}: {done.stderr.strip() or 'import failed'}")
            elif kind == "package-config":
                errors.append(f"{target}: apt purge cannot be undone; reinstall the package")
            elif os.path.exists(target):
                errors.append(f"{target}: already exists, left the backup alone")
            else:
                shutil.move(source, target)
        except (OSError, subprocess.SubprocessError) as e:
            errors.append(f"{target}: {e}")
    return errors
