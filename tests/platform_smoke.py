"""End-to-end checks that only mean something on a real machine.

Run it on each OS: `python tests/platform_smoke.py`. The unit tests mock the
platform away, which is exactly why they pass on Linux while a Windows path is
broken. This script calls the installed `cleam` command and the real registry,
so CI on windows-latest and macos-latest is the only place the Windows and
macOS code has ever actually run.

It is safe to run anywhere. HOME is redirected to a temporary directory for the
leftovers round-trip, so every file and folder it creates is its own, and the
one registry key it touches is HKCU\\Software\\CleamSmokeTest.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cleam import leftovers, overview  # noqa: E402
from cleam.system import OS  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""), flush=True)


def command() -> list[str]:
    """The installed console script if it exists, so the entry point is covered too.

    Looked up next to this interpreter rather than on PATH: a venv that was
    never activated still has its scripts directory.
    """
    scripts = Path(sys.executable).parent
    for candidate in (scripts / "cleam", scripts / "cleam.exe", scripts / "Scripts" / "cleam.exe"):
        if candidate.exists():
            return [str(candidate)]
    return [sys.executable, "-m", "cleam"]


CLEAM = command()


def cleam(*args: str, expect: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    done = subprocess.run([*CLEAM, *args], capture_output=True, text=True, timeout=900)
    ok = done.returncode in expect and "Traceback" not in done.stderr
    check(f"cleam {' '.join(args)}", ok, f"exit {done.returncode} {done.stderr.strip()[:160]}")
    return done


def smoke_cli() -> None:
    out = cleam("overview", "--json").stdout
    try:
        data = json.loads(out)
        check("overview names this OS", bool(data["os"]), data.get("os", ""))
        check("overview found a disk", bool(data["disks"]), f"{len(data.get('disks', []))} mounts")
        check("overview measured a folder", any(f["measured"] for f in data["folders"]))
    except (ValueError, KeyError) as e:
        check("overview --json parses", False, str(e))

    listings = cleam("scan", "--json").stdout
    try:
        targets = json.loads(listings)
        check("scan returned every target", len(targets) >= 3, f"{len(targets)} targets")
        check("scan deleted nothing", all("skipped" in t for t in targets))
    except ValueError as e:
        check("scan --json parses", False, str(e))

    programs = cleam("apps", "--json").stdout
    try:
        found = json.loads(programs)
        check("apps listed installed programs", len(found) > 0, f"{len(found)} programs")
    except ValueError as e:
        check("apps --json parses", False, str(e))

    cleam("clean")  # a dry run must never delete or fail
    cleam("biggest", tempfile.gettempdir(), "--top", "3")
    # No snapshot tool on a CI runner: it must say so and exit 1, not crash.
    cleam("snapshot", "list", expect=(0, 1))


def smoke_leftovers() -> None:
    """Create remnants of a program that never existed, then find and undo them."""
    sandbox = Path(tempfile.mkdtemp())
    environment = dict(os.environ)
    for variable in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        environment.pop(variable, None)
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(sandbox)
    if OS == "windows":
        os.environ["APPDATA"] = str(sandbox / "AppData" / "Roaming")
        os.environ["LOCALAPPDATA"] = str(sandbox / "AppData" / "Local")

    # Create the user root first, then only ever write inside the sandbox:
    # _roots() also returns /etc and /opt, which this must never touch.
    user_root = sandbox / {"windows": "AppData/Roaming", "macos": "Library/Application Support"}.get(OS, ".config")
    user_root.mkdir(parents=True, exist_ok=True)
    roots = [Path(root) for root, _ in leftovers._roots() if sandbox in Path(root).parents or Path(root) == user_root]
    check("the sandbox root is the one being scanned", bool(roots), str(user_root))
    remnant = (roots[0] if roots else user_root) / "CleamSmokeApp"
    remnant.mkdir(parents=True, exist_ok=True)
    (remnant / "settings.ini").write_bytes(b"x" * 64)

    found = leftovers.scan("Cleam Smoke App")
    high = [item for item in found if item.confidence == "high"]
    check("leftovers found the remnant", any(Path(i.target) == remnant for i in high), f"{len(found)} candidates")

    backup, errors = leftovers.remove(high, label="Cleam Smoke App")
    check("leftovers removal reported no errors", not errors, "; ".join(errors)[:160])
    check("remnant was moved, not deleted", not remnant.exists() and backup.exists(), str(backup))

    restore_errors = leftovers.restore(backup)
    check("restore put it back", not restore_errors and (remnant / "settings.ini").exists(), "; ".join(restore_errors)[:160])


def smoke_registry() -> None:
    if OS != "windows":
        return
    import winreg

    key_path = r"Software\CleamSmokeTest"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "Marker", 0, winreg.REG_SZ, "cleam")

    found = leftovers._registry_leftovers(leftovers.aliases_for("CleamSmokeTest"), set())
    mine = [item for item in found if item.target.lower().endswith("cleamsmoketest")]
    check("registry scan found the key", bool(mine), f"{len(found)} keys matched")

    if mine:
        backup, errors = leftovers.remove(mine, label="CleamSmokeTest")
        gone = False
        try:
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path).Close()
        except OSError:
            gone = True
        check("registry key was exported and deleted", gone and not errors, "; ".join(errors)[:160])

        restore_errors = leftovers.restore(backup)
        back = False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                back = winreg.QueryValueEx(key, "Marker")[0] == "cleam"
        except OSError:
            back = False
        check("reg import restored the key", back and not restore_errors, "; ".join(restore_errors)[:160])
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        except OSError:
            pass


def smoke_platform_facts() -> None:
    name = overview.os_name()
    if OS == "windows":
        # Not just "Windows 1": a CI runner reports Windows Server 2025, and
        # the rename rule only rewrites the desktop editions that lie.
        ok = name.startswith("Windows") and "build" in name and any(c.isdigit() for c in name)
        check("Windows edition, release and build are named", ok, name)
    elif OS == "macos":
        check("macOS version is a number", name.startswith("macOS") and any(c.isdigit() for c in name), name)
    else:
        check("Linux names the distribution and kernel", "kernel" in name, name)

    disks = overview.disks()
    check("every disk reports a percentage in range", all(0 <= d.percent_used <= 100 for d in disks))
    targets = json.loads(subprocess.run([*CLEAM, "scan", "--json"], capture_output=True, text=True).stdout)
    ids = {t["id"] for t in targets}
    expected = {
        "windows": {"user-temp", "recycle-bin"},
        "macos": {"user-cache", "trash"},
        "linux": {"tmp", "trash"},
    }[OS]
    check(f"{OS} targets present", expected <= ids, f"missing {expected - ids}" if not expected <= ids else "")
    listed = json.loads(subprocess.run([*CLEAM, "targets", "--json"], capture_output=True, text=True).stdout)
    check("`cleam targets` lists what scan scans", {t["id"] for t in listed} == ids)
    check("every target explains itself", all(t["about"] and t["group"] for t in listed))


def smoke_startup() -> None:
    out = cleam("startup", "--json").stdout
    try:
        items = json.loads(out)
        check("startup --json parses", isinstance(items, list), f"{len(items)} entries")
    except ValueError as e:
        check("startup --json parses", False, str(e))
        return
    if OS != "windows":
        return
    from cleam import security

    raw = security._tasks_and_services()
    tasks, services = security._as_list(raw.get("tasks")), security._as_list(raw.get("services"))
    # A bare Windows has well over a hundred of each: the PowerShell ran and parsed.
    check("PowerShell read the scheduled tasks", len(tasks) > 50, f"{len(tasks)} tasks")
    check("PowerShell read the services", len(services) > 50, f"{len(services)} services")
    check("svchost services are not listed", not any(i["kind"] == "service" and i["path"].lower().endswith(
        "svchost.exe") for i in items))
    # Windows' own tasks that name a bare program ("BthUdTask.exe") must not read as missing.
    missing = [i["key"] for i in items if i["kind"] == "task" and i["key"].lower().startswith("\\microsoft\\")
               and i["reasons"] == ["the program it starts does not exist"]]
    check("no Windows task is flagged only as missing", not missing, ", ".join(missing)[:160])

    # A throwaway task, switched off and back on through the journal.
    name = "CleamSmokeTest"
    ps = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    subprocess.run(ps + [f"Register-ScheduledTask -TaskName {name} -Force -Trigger (New-ScheduledTaskTrigger -AtLogOn)"
                         " -Action (New-ScheduledTaskAction -Execute cmd.exe -Argument '/c exit') | Out-Null"],
                   capture_output=True, text=True, timeout=120)

    def state() -> str:
        return subprocess.run(ps + [f"[int](Get-ScheduledTask -TaskName {name}).State"],
                              capture_output=True, text=True, timeout=120).stdout.strip()

    try:
        listed = json.loads(cleam("startup", "--json").stdout)
        mine = next((i for i in listed if i["id"] == f"task:\\{name}"), None)
        check("a new logon task is listed", mine is not None and "at sign-in" in mine["location"],
              mine["location"] if mine else "missing")
        dry = cleam("startup", "--off", f"task:\\{name}")
        check("--off without --yes changes nothing", "dry run" in dry.stdout and state() == "3", state())
        cleam("startup", "--off", f"task:\\{name}", "--yes")
        check("--off --yes disables the task", state() == "1", state())
        cleam("startup", "--on", f"task:\\{name}", "--yes")
        check("--on puts it back from the journal", state() == "3", state())
    finally:
        subprocess.run(ps + [f"Unregister-ScheduledTask -TaskName {name} -Confirm:$false"],
                       capture_output=True, text=True, timeout=120)


if __name__ == "__main__":
    print(f"Cleam platform smoke test on {OS} ({sys.platform}, python {sys.version.split()[0]})\n", flush=True)
    smoke_cli()
    smoke_platform_facts()
    smoke_leftovers()
    smoke_registry()
    smoke_startup()
    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed on {OS}")
    if failed:
        print("failed: " + ", ".join(failed))
    raise SystemExit(1 if failed else 0)
