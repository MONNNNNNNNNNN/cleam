"""Windows' own cleaners, driven from Cleam: Disk Cleanup handlers, and pnputil.

Some Windows leftovers are only safe to remove through Windows: old driver
packages have to be unregistered from the driver store, not deleted from
disk, and Windows.old / $WINDOWS.~BT carry TrustedInstaller ACLs. Disk
Cleanup (cleanmgr) already knows how, and its handlers run unattended with
`/sagerun:N` once the handler's StateFlagsNNNN registry value is 2.

Cleam uses its own slot, clears it on every handler first (a crashed earlier
run must not leave some other handler armed), arms only the handlers the
target names, and removes the values again afterwards.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .system import NO_WINDOW

SLOT = 619  # cleanmgr sageset slots are 0-9999; this one is Cleam's
HANDLERS_KEY = r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches"
TIMEOUT = 600


def _ps_list(items: tuple[str, ...]) -> str:
    return ",".join("'" + i.replace("'", "''") + "'" for i in items)


def sagerun_script(handlers: tuple[str, ...]) -> str:
    """PowerShell that runs exactly `handlers` through cleanmgr and leaves no flags behind."""
    flag = f"StateFlags{SLOT:04d}"
    return (
        f"$base='{HANDLERS_KEY}'; $flag='{flag}'; $want=@({_ps_list(handlers)});"
        "Get-ChildItem $base | ForEach-Object { Remove-ItemProperty -Path $_.PSPath -Name $flag"
        " -ErrorAction SilentlyContinue };"
        # The exit code is kept and returned after `finally`, so disarming the
        # flags never depends on how PowerShell treats `exit` inside `try`.
        "$code = 1; try { foreach ($h in $want) { if (-not (Test-Path \"$base\\$h\")) { throw \"no handler $h\" };"
        " New-ItemProperty -Path \"$base\\$h\" -Name $flag -Value 2 -PropertyType DWord -Force | Out-Null };"
        f" $p = Start-Process cleanmgr.exe -ArgumentList '/sagerun:{SLOT}' -WindowStyle Hidden -Wait -PassThru; $code = $p.ExitCode }}"
        " finally { foreach ($h in $want) { Remove-ItemProperty -Path \"$base\\$h\" -Name $flag"
        " -ErrorAction SilentlyContinue } }; exit $code"
    )


def command(handlers: tuple[str, ...]) -> tuple[str, ...]:
    return ("powershell", "-NoProfile", "-NonInteractive", "-Command", sagerun_script(handlers))


def _version(text: str) -> tuple[int, ...]:
    parts = []
    for piece in str(text).split("."):
        digits = "".join(c for c in piece if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def superseded(drivers: list[dict]) -> list[dict]:
    """Driver packages for which a newer version of the same package is also in the store.

    A package is its INF name plus provider plus class. Everything but the
    newest version is what Disk Cleanup's "Device driver packages" offers to
    remove; Windows still refuses any that a device is using, so this is an
    upper bound, and the result of a clean is measured, not assumed.
    """
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for d in drivers:
        inf = os.path.basename(str(d.get("OriginalFileName") or "")).lower()
        if not inf:
            continue
        key = (inf, str(d.get("ProviderName") or ""), str(d.get("ClassName") or ""))
        groups.setdefault(key, []).append(d)
    old: list[dict] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        newest = max(_version(m.get("Version", "")) for m in members)
        old += [m for m in members if _version(m.get("Version", "")) < newest]
    return old


def list_drivers() -> list[dict]:
    """Third-party driver packages in the store, from DISM. Needs admin; [] on any failure.

    Get-WindowsDriver's property names are not localized, unlike pnputil's
    output, which reads "Published Name" on English Windows only.
    """
    script = (
        "Get-WindowsDriver -Online | Select-Object Driver, OriginalFileName, Version, ProviderName, ClassName"
        " | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        ).stdout
        data = json.loads(out) if out.strip() else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return data if isinstance(data, list) else [data]


def old_driver_packages() -> tuple[int, int]:
    """(files, bytes) of superseded driver packages: what a clean could free at most."""
    from .junk import _tree  # the same no-links walk every other size uses

    files = size = 0
    for d in superseded(list_drivers()):
        folder = Path(str(d["OriginalFileName"])).parent
        try:
            f, b, _ = _tree(str(folder))
        except OSError:
            continue
        files += f
        size += b
    return files, size


def delete_old_driver_packages() -> int:
    """Remove every superseded driver package with pnputil; returns how many Windows refused.

    What Disk Cleanup's "Device driver packages" does, without its dialog
    (cleanmgr draws one that no window flag hides). No /force: Windows refuses
    a package a device still uses, and that refusal is counted, not overridden.
    pnputil's output is localized, so only its exit code is read.
    """
    refused = 0
    for d in superseded(list_drivers()):
        try:
            done = subprocess.run(
                ["pnputil", "/delete-driver", str(d["Driver"])],
                capture_output=True,
                text=True,
                timeout=120,
                stdin=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
            )
            refused += done.returncode != 0
        except (OSError, subprocess.SubprocessError, KeyError):
            refused += 1
    return refused
