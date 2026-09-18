"""System restore points (Windows) and snapshots (Linux, macOS).

Two calling modes. The CLI runs the tool with the terminal attached, so a sudo
prompt and the tool's own output reach the user. The GUI has no terminal, so it
captures output instead and asks sudo not to prompt -- an unanswerable password
prompt would hang the window.
"""
from __future__ import annotations

import shutil
import subprocess

from .system import OS, is_admin, sudo


def _powershell(script: str) -> list[str]:
    # Windows PowerShell 5.1, not pwsh 7: Checkpoint-Computer does not exist in pwsh.
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]


def _command(action: str, description: str, noninteractive: bool = False) -> list[str] | str:
    """The command for action ("create" | "list"), or an error message string."""
    if OS == "windows":
        if not is_admin():
            return "Restore points need an elevated (Administrator) Cleam."
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
        args = ["timeshift", "--list"] if action == "list" else [
            "timeshift", "--create", "--comments", description, "--scripted"
        ]
        return sudo(args, noninteractive)
    if shutil.which("snapper"):
        args = ["snapper", "list"] if action == "list" else ["snapper", "create", "--description", description]
        return sudo(args, noninteractive)
    return "No snapshot tool found. Install timeshift (sudo apt install timeshift) or snapper."


def run(action: str, description: str = "Cleam", capture: bool = False) -> tuple[int, str]:
    """(exit code, output). Output is empty unless capture=True or the command could not be built."""
    cmd = _command(action, description, noninteractive=capture)
    if isinstance(cmd, str):
        return 1, cmd
    try:
        if capture:
            p = subprocess.run(cmd, capture_output=True, text=True)
            return p.returncode, (p.stdout + p.stderr).strip()
        return subprocess.run(cmd).returncode, ""
    except OSError as e:
        return 1, str(e)


def create(description: str = "Cleam", capture: bool = False) -> tuple[int, str]:
    return run("create", description, capture)


def list_snapshots(capture: bool = False) -> tuple[int, str]:
    return run("list", "", capture)
