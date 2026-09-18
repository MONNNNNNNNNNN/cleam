"""OS detection and the few process helpers every module shares."""
from __future__ import annotations

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


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def relaunch_parts(executable: str, argv: list[str], frozen: bool) -> tuple[str, str]:
    """(program, command-line arguments) needed to start this same Cleam again.

    Frozen builds are the executable itself, so argv[0] repeats it; running
    from source, argv[0] is the script and has to be passed to the interpreter.
    """
    import subprocess as sp

    return (executable, sp.list2cmdline(argv[1:] if frozen else argv))


def relaunch_as_admin() -> bool:
    """Start an elevated copy of Cleam (Windows only). True if UAC accepted.

    The elevated copy is a new process: the caller keeps running and should
    tell the user to close it.
    """
    if OS != "windows" or is_admin():
        return False
    import ctypes

    program, arguments = relaunch_parts(sys.executable, sys.argv, bool(getattr(sys, "frozen", False)))
    try:  # ShellExecuteW returns > 32 on success; 5 is SW_SHOW
        return int(ctypes.windll.shell32.ShellExecuteW(None, "runas", program, arguments, None, 5)) > 32
    except (AttributeError, OSError):
        return False


def output(cmd: list[str]) -> str:
    """stdout of cmd, or "" if it is missing or fails. For read-only queries only."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""
