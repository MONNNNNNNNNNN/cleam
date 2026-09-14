"""System restore points (Windows) and snapshots (Linux, macOS).

Each call runs the platform tool with the terminal attached, so sudo prompts
and the tool's own messages reach the user. Returns the tool's exit code.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

from .system import OS, is_admin, sudo


def _powershell(script: str) -> list[str]:
    # Windows PowerShell 5.1, not pwsh 7: Checkpoint-Computer does not exist in pwsh.
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]


def _command(action: str, description: str) -> list[str] | str:
    """The command for action ("create" | "list"), or an error message string."""
    if OS == "windows":
        if not is_admin():
            return "restore points need an elevated (Administrator) terminal"
        if action == "list":
            return _powershell(
                "Get-ComputerRestorePoint | Format-Table SequenceNumber, Description,"
                " @{n='Created';e={$_.ConvertToDateTime($_.CreationTime)}} -AutoSize"
            )
        desc = description.replace("'", "''")
        # Windows silently skips a restore point if one was made in the last 24h and
        # still exits 0. -WarningAction Stop turns that warning into a real failure.
        return _powershell(
            f"Checkpoint-Computer -Description '{desc}' -RestorePointType MODIFY_SETTINGS"
            " -WarningAction Stop -ErrorAction Stop"
        )
    if OS == "macos":
        return ["tmutil", "localsnapshot"] if action == "create" else ["tmutil", "listlocalsnapshots", "/"]
    if shutil.which("timeshift"):
        if action == "list":
            return sudo(["timeshift", "--list"])
        return sudo(["timeshift", "--create", "--comments", description, "--scripted"])
    if shutil.which("snapper"):
        if action == "list":
            return sudo(["snapper", "list"])
        return sudo(["snapper", "create", "--description", description])
    return "no snapshot tool found: install timeshift (sudo apt install timeshift) or snapper"


def create(description: str = "Cleam") -> int:
    return _run(_command("create", description))


def list_snapshots() -> int:
    return _run(_command("list", ""))


def _run(cmd: list[str] | str) -> int:
    if isinstance(cmd, str):
        print(f"cleam: {cmd}", file=sys.stderr)
        return 1
    try:
        return subprocess.run(cmd).returncode
    except OSError as e:
        print(f"cleam: {e}", file=sys.stderr)
        return 1
