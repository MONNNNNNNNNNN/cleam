"""OS detection and the few process helpers every module shares."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

OS = "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux"


def is_admin() -> bool:
    if OS == "windows":
        import ctypes

        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    return os.geteuid() == 0


def sudo(cmd: list[str], noninteractive: bool = False) -> list[str]:
    """Prefix sudo on POSIX when not already root, so the password prompt reaches the terminal.

    noninteractive adds -n, for callers with no terminal (the GUI): sudo then
    fails immediately instead of waiting forever for a password nobody can type.
    """
    if OS != "windows" and not is_admin() and shutil.which("sudo"):
        return ["sudo", "-n", *cmd] if noninteractive else ["sudo", *cmd]
    return cmd


def output(cmd: list[str]) -> str:
    """stdout of cmd, or "" if it is missing or fails. For read-only queries only."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""
