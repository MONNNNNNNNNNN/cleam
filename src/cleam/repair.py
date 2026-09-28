"""Fixes for what the system check finds.

The check reads; this module changes things, and only when asked, one fix at
a time. Every fix says exactly what it will do (`tech`) before it runs:

- **change**: registry values and service start types, made through the
  debloat engine -- so the previous state is journalled first and Undo
  debloat puts it back. Unblocking Windows Update and turning Defender back
  on are this kind: they undo what a tweak tool did, and can be redone.
- **run**: a Windows command (enable a device, turn TRIM on, reset boot timer
  values, update definitions). `undo` says how to reverse it.
- **open**: a Windows settings page or a vendor download page, for what only
  the user can decide (encryption, ESU enrolment, a driver download).
- **uefi**: restart into the firmware setup, for settings that live there
  (Secure Boot, TPM, XMP/EXPO, Resizable BAR).
- **refresh**: switch a monitor to its highest refresh rate.
- **screen**: another page of Cleam (Clean junk for a full C:).

Nothing here removes an antivirus, lowers a protection, or runs unasked.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import debloat, security
from .debloat import HKLM, SZ, Reg, Tweak, service
from .system import NO_WINDOW, OS, is_admin

CHANGE, RUN, OPEN, UEFI, REBOOT, REFRESH, SCREEN = "change", "run", "open", "uefi", "reboot", "refresh", "screen"
POL = r"SOFTWARE\Policies\Microsoft\Windows"
DEFENDER_POL = r"SOFTWARE\Policies\Microsoft\Windows Defender"
# Windows 10/11 defaults (Start: 2 automatic, 3 manual).
SERVICE_DEFAULTS = {"wuauserv": 3, "UsoSvc": 2, "WaaSMedicSvc": 3, "BITS": 3, "DoSvc": 2,
                    "WinDefend": 2, "WdNisSvc": 3, "SecurityHealthService": 3, "wscsvc": 2}
UPDATE_POLICY_VALUES = {
    "DisableWindowsUpdateAccess": [(POL + r"\WindowsUpdate", "DisableWindowsUpdateAccess")],
    "SetDisableUXWUAccess": [(POL + r"\WindowsUpdate", "SetDisableUXWUAccess")],
    "NoAutoUpdate": [(POL + r"\WindowsUpdate\AU", "NoAutoUpdate")],
    "DoNotConnectToWindowsUpdateInternetLocations": [(POL + r"\WindowsUpdate",
                                                      "DoNotConnectToWindowsUpdateInternetLocations")],
    "WUServer": [(POL + r"\WindowsUpdate", "WUServer"), (POL + r"\WindowsUpdate", "WUStatusServer"),
                 (POL + r"\WindowsUpdate", "UpdateServiceUrlAlternate"), (POL + r"\WindowsUpdate\AU", "UseWUServer")],
}


@dataclass(frozen=True)
class Fix:
    title: str  # the button: "Unblock Windows Update"
    kind: str
    tech: tuple[str, ...] = ()  # exactly what happens
    about: str = ""  # plain words
    tweak: Tweak | None = None  # CHANGE: journalled through the debloat engine
    script: str = ""  # RUN: PowerShell
    call: object = None  # RUN: a Python callable returning "" or an error
    target: str = ""  # OPEN: URI or program; SCREEN: page name; REFRESH: device
    hz: int = 0  # REFRESH
    restart: str = ""  # "restart" when it takes effect only after one
    undo: str = ""  # how to reverse a RUN
    warn: str = ""  # said before it runs
    admin: bool = True
    extra: dict = field(default_factory=dict)


def _tweak(fid: str, title: str, reg: tuple[Reg, ...], restart: str = "") -> Tweak:
    return Tweak(f"fix-{fid}", "Repairs", title, title, reg, restart=restart)


def _ps(script: str, timeout: int = 600) -> str:
    """Run PowerShell hidden; "" on success, else its error text."""
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                               f"$ErrorActionPreference = 'Stop'; try {{ {script}; 'CLEAM-OK' }}"
                               " catch { $_.Exception.Message }"],
                              capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    out = done.stdout.strip()
    return "" if out.endswith("CLEAM-OK") else (out.splitlines()[-1] if out else "failed")


def fixes_for(check: security.Check) -> list[Fix]:
    """What can be done about one check. [] for a check that is fine or has no software fix."""
    if check.state == security.OK:
        return []
    d = check.data or {}
    cid = check.id
    if cid == "antivirus" and (d.get("policy") or d.get("services")):
        reg = tuple(Reg(HKLM, DEFENDER_POL + ("\\" + p.rsplit("\\", 1)[0] if "\\" in p else ""),
                        p.rsplit("\\", 1)[-1], None) for p in d.get("policy", []))
        reg += tuple(service(n, SERVICE_DEFAULTS[n], SERVICE_DEFAULTS[n]) for n in d.get("services", []))
        t = _tweak("defender", "Turn Microsoft Defender back on", reg, "restart")
        return [Fix("Turn Microsoft Defender back on", CHANGE, tuple(debloat.tech(t)), tweak=t, restart="restart",
            about="Removes the policies that switch Defender off and sets its services back to Windows' "
                  "defaults. Defender starts after a restart.",
            warn="Defender will scan everything once it runs, and quarantines cracked or patched programs "
                 "(a program with a broken signature, like a patched IDMan.exe, is one).")]
    if cid == "update-blocked":
        reg: tuple[Reg, ...] = ()
        for name in d.get("policy", []):
            reg += tuple(Reg(HKLM, key, value, None) for key, value in UPDATE_POLICY_VALUES.get(name, []))
        reg += tuple(service(n, SERVICE_DEFAULTS[n], SERVICE_DEFAULTS[n]) for n in d.get("services", []))
        t = _tweak("updates", "Unblock Windows Update", reg, "restart")
        return [Fix("Unblock Windows Update", CHANGE, tuple(debloat.tech(t)), tweak=t, restart="restart",
                    about="Removes the policies that block updates" + (f" (including the made-up update server "
                    f"{d['wsus']})" if d.get("wsus") else "") + " and sets the update services back to "
                    "Windows' defaults. After a restart, Windows Update checks by itself; Update Medic "
                    "re-enables the update tasks.",
                    warn="Windows will download and install the updates it missed, and may restart to finish.")]
    if cid == "signatures":
        return [Fix("Update virus definitions", RUN, ("Update-MpSignature",), script="Update-MpSignature")]
    if cid == "last-scan":
        return [Fix("Run a quick scan", RUN, (str(security.MPCMDRUN) + " -Scan -ScanType 1",),
                    call=lambda: "" if security.quick_scan()[0] == 0 else "the scan did not finish",
                    about="Defender scans the places malware usually hides (a few minutes).")]
    if cid == "ransomware-shield" and check.state == security.WARN:
        return [Fix("Turn on Controlled Folder Access", RUN,
                    ("Set-MpPreference -EnableControlledFolderAccess Enabled",),
                    script="Set-MpPreference -EnableControlledFolderAccess Enabled",
                    undo="Set-MpPreference -EnableControlledFolderAccess Disabled",
                    warn="Programs Windows does not know may be blocked from saving into Documents, Pictures or "
                         "Desktop (some games save there); allow them in Windows Security > Ransomware protection.")]
    if cid == "restore-points":
        return [Fix("Turn on System Protection and make a restore point", RUN,
                    ('Enable-ComputerRestore -Drive "$env:SystemDrive\\"', 'Checkpoint-Computer -Description "Cleam"'),
                    script='Enable-ComputerRestore -Drive "$env:SystemDrive\\"; '
                           'Checkpoint-Computer -Description "Cleam" -RestorePointType MODIFY_SETTINGS')]
    if cid == "smartscreen":
        t = _tweak("smartscreen", "Turn SmartScreen back on",
                   (Reg(HKLM, POL + r"\System", "EnableSmartScreen", None),
                    Reg(HKLM, POL + r"\System", "ShellSmartScreenLevel", None),
                    Reg(HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer", "SmartScreenEnabled", "Warn", SZ)))
        return [Fix("Turn SmartScreen back on", CHANGE, tuple(debloat.tech(t)), tweak=t,
                    about="Unknown downloaded programs get a warning before they start; you can still run them.")]
    if cid == "hosts":
        return [Fix("Unblock Microsoft in the hosts file", RUN,
                    (f"back up {security.HOSTS} to hosts.cleam-backup", "comment out: " + ", ".join(d["hosts"][:4])
                     + ("..." if len(d["hosts"]) > 4 else "")),
                    call=lambda: unblock_hosts(d["hosts"]), undo=f"copy hosts.cleam-backup over {security.HOSTS}")]
    if cid == "firewall":
        return [Fix("Turn the firewall on", RUN, ("Set-NetFirewallProfile -All -Enabled True",),
                    script="Set-NetFirewallProfile -All -Enabled True")]
    if cid == "uac":
        t = _tweak("uac", "Turn UAC back on", (Reg(HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
                                                   "EnableLUA", 1),), "restart")
        return [Fix("Turn UAC back on", CHANGE, tuple(debloat.tech(t)), tweak=t, restart="restart")]
    if cid == "last-update":
        return [Fix("Check for updates", OPEN, ("ms-settings:windowsupdate-action",),
                    target="ms-settings:windowsupdate-action", admin=False)]
    if cid == "windows-10-support":
        return [Fix("Open Windows Update (ESU enrolment)", OPEN, ("ms-settings:windowsupdate",),
                    target="ms-settings:windowsupdate", admin=False)]
    if cid in ("reboot", "uptime"):
        return [Fix("Restart now", REBOOT, ("shutdown /r /t 60",), warn="Save your work: Windows restarts in "
                    "60 seconds (shutdown /a cancels).")]
    if cid in ("secure-boot", "tpm") or d.get("uefi"):
        what = {"secure-boot": "Secure Boot (Boot > Secure Boot > Enabled; the disk is already GPT, since "
                               "Windows booted in UEFI mode)",
                "tpm": "fTPM (AMD) or PTT (Intel), under Advanced / Security",
                "ram-speed": "XMP / EXPO / DOCP, under the memory or overclocking (OC Tweaker) page",
                "rebar": "Above 4G Decoding and Re-Size BAR Support (with CSM off)"}.get(cid, "the setting")
        return [Fix("Restart into the UEFI setup", UEFI, ("shutdown /r /fw /t 10",),
                    about=f"The PC restarts straight into its firmware setup. Turn on {what}, then save and exit "
                          "(usually F10).",
                    warn="Save your work: the PC restarts in 10 seconds.")]
    if cid == "encryption":
        return [Fix("Open drive encryption", OPEN, ("ms-settings:deviceencryption",),
                    target="ms-settings:deviceencryption", admin=False,
                    about="Save the recovery key somewhere other than this PC before you turn it on.")]
    if cid == "memory-integrity":
        return [Fix("Open Core isolation", OPEN, ("windowsdefender://coreisolation",),
                    target="windowsdefender://coreisolation", admin=False)]
    if cid == "smb1":
        t = next(t for t in debloat.TWEAKS if t.id == "smb1")
        return [Fix("Remove SMB 1.0", CHANGE, tuple(debloat.tech(t)), tweak=t, restart="restart")]
    if cid == "rdp":
        base = r"SYSTEM\CurrentControlSet\Control\Terminal Server"
        if check.state == security.BAD:
            t = _tweak("rdp-nla", "Require Network Level Authentication",
                       (Reg(HKLM, base + r"\WinStations\RDP-Tcp", "UserAuthentication", 1),))
        else:
            t = _tweak("rdp-off", "Turn Remote Desktop off", (Reg(HKLM, base, "fDenyTSConnections", 1),))
        return [Fix(t.title, CHANGE, tuple(debloat.tech(t)), tweak=t)]
    if cid == "guest":
        script = "Get-LocalUser | Where-Object { $_.SID -like '*-501' } | Disable-LocalUser"
        return [Fix("Disable the Guest account", RUN, (script,), script=script,
                    undo="Get-LocalUser | Where-Object { $_.SID -like '*-501' } | Enable-LocalUser")]
    if cid == "crashes":
        return [Fix("Open Reliability Monitor", OPEN, ("perfmon /rel",), target="perfmon.exe", admin=False,
                    extra={"args": "/rel"})]
    if cid == "devices":
        out = []
        for dev in d.get("devices", []):
            name, iid = dev.get("name") or dev.get("id"), dev.get("id", "")
            if dev.get("code") == 22 and iid:
                script = f"Enable-PnpDevice -InstanceId '{iid.replace(chr(39), chr(39) * 2)}' -Confirm:$false"
                out.append(Fix(f"Turn on: {name}", RUN, (script,), script=script,
                               undo=f"Disable-PnpDevice -InstanceId '{iid}' -Confirm:$false",
                               about="Device Manager shows it switched off -- by you, or by a tweak tool."))
        if any(dev.get("code") != 22 for dev in d.get("devices", [])):
            out.append(Fix("Look for missing drivers", RUN, ("pnputil /scan-devices",),
                           call=lambda: _cmd(["pnputil", "/scan-devices"])))
        return out
    if cid == "game-mode":
        t = next(t for t in debloat.TWEAKS if t.id == "game-mode")
        return [Fix("Turn Game Mode on", CHANGE, tuple(debloat.tech(t)), tweak=t, admin=False)]
    if cid == "power-plan" and "Balanced" in check.detail:
        t = next(t for t in debloat.TWEAKS if t.id == "power-plan")
        return [Fix("Switch to High performance", CHANGE, tuple(debloat.tech(t)), tweak=t)]
    if cid == "cpu-cap":
        script = ("powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMAX 100; "
                  "powercfg /setactive SCHEME_CURRENT")
        return [Fix("Let the CPU reach 100 %", RUN, tuple(script.split("; ")), script=script,
                    undo=f"powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMAX {d.get('cap')}")]
    if cid == "boot-timers":
        values = d.get("values", {})
        cmds = tuple(f"bcdedit /deletevalue {{current}} {k}" for k in values)
        return [Fix("Reset boot timer values to Windows' defaults", RUN, cmds, restart="restart",
                    call=lambda: "; ".join(e for e in (_cmd(["bcdedit", "/deletevalue", "{current}", k])
                                                       for k in values) if e),
                    undo="; ".join(f"bcdedit /set {{current}} {k} {v}" for k, v in values.items()))]
    if cid == "refresh":
        disp = d.get("display", {})
        return [Fix(f"Switch to {disp.get('max_hz')} Hz", REFRESH,
                    (f"ChangeDisplaySettingsEx {disp.get('device')} {disp.get('width')}x{disp.get('height')} "
                     f"@ {disp.get('max_hz')} Hz",), target=disp.get("device", ""), hz=disp.get("max_hz", 0),
                    admin=False, undo="Settings > System > Display > Advanced display > refresh rate")]
    if cid == "gpu-driver" and d.get("url"):
        return [Fix("Open the driver download page", OPEN, (d["url"],), target=d["url"], admin=False)]
    if cid == "trim":
        return [Fix("Turn TRIM back on", RUN, ("fsutil behavior set DisableDeleteNotify NTFS 0",),
                    call=lambda: _cmd(["fsutil", "behavior", "set", "DisableDeleteNotify", "NTFS", "0"]),
                    undo="fsutil behavior set DisableDeleteNotify NTFS 1")]
    if cid == "optimize-task":
        script = r"Enable-ScheduledTask -TaskPath '\Microsoft\Windows\Defrag\' -TaskName ScheduledDefrag"
        return [Fix("Turn weekly optimization back on", RUN, (script,), script=script)]
    if cid == "free-space" and str(d.get("letter", "")).upper() == "C":
        return [Fix("Clean junk", SCREEN, ("open Clean junk",), target="clean", admin=False)]
    if cid == "pagefile":
        script = ("$cs = Get-CimInstance Win32_ComputerSystem; "
                  "Set-CimInstance -InputObject $cs -Property @{ AutomaticManagedPagefile = $true }")
        return [Fix("Let Windows manage the page file", RUN, (script,), script=script, restart="restart")]
    return []


TURN_OFF, TURN_ON = "Turn off at startup", "Turn back on"


def startup_tweak(item: security.StartupItem) -> Tweak | None:
    """A scheduled task or service switched off through the debloat engine, so
    its state is journalled first and Undo debloat (or Turn back on) restores
    exactly that. A service goes to manual, not disabled: it stops starting
    with Windows, but a program that needs it can still start it."""
    if item.kind == "task":
        return Tweak(f"startup-task-{item.key}", "Startup", f"Startup: task {item.name} off",
                     f"Scheduled task {item.key} disabled.", tasks=(item.key,))
    if item.kind == "service":
        return Tweak(f"startup-service-{item.key}", "Startup", f"Startup: service {item.name} to manual",
                     f"Service {item.key} no longer starts with Windows.", (service(item.key, 3, 2),))
    return None


def startup_fixes(item: security.StartupItem, journal: dict | None = None) -> list[Fix]:
    """Switch one startup entry off, or back on when Cleam switched it off.

    Run keys and Startup folders use Task Manager's own switch
    (StartupApproved), so Task Manager shows it and can turn it back on.
    Tasks and services go through the journal. Nothing protective (an
    antivirus, a firewall, a backup agent) is ever offered."""
    if item.protective:
        return []
    tweak = startup_tweak(item)
    if tweak:
        if item.enabled:
            updater = "update" in f"{item.name} {item.key}".lower()
            return [Fix(TURN_OFF, CHANGE, tuple(debloat.tech(tweak)), tweak=tweak,
                        about=f"{item.name} stops starting by itself." + (
                            " A service set to manual still starts when a program asks for it."
                            if item.kind == "service" else ""),
                        warn="This keeps a program up to date: switched off, it updates only when you open it, "
                             "so security fixes can arrive later." if updater else "")]
        journal = journal if journal is not None else debloat.load_journal()
        if tweak.id in journal["tweaks"]:
            return [Fix(TURN_ON, RUN, (f"put back what the journal recorded for {item.key}",),
                        call=lambda: debloat.Debloater().undo(tweak.id).message,
                        about=f"Cleam switched {item.name} off; this restores it exactly as it was.")]
        return []
    where = approved_key(item)
    if not item.enabled or not where:
        return []
    hive, key = where
    return [Fix(TURN_OFF, RUN, (f"{hive}\\{key}  {item.name} = 03 00 00 00 <time> (disabled)",),
                call=lambda: security.set_startup_enabled(item, False), admin=hive == HKLM,
                undo="Task Manager > Startup > Enable")]


def approved_key(item: security.StartupItem) -> tuple[str, str] | None:
    base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
    loc = item.location
    if loc.startswith("HKCU\\") and loc.endswith("\\Run"):
        return "HKCU", base + r"\Run"
    if loc.startswith("HKLM\\") and loc.endswith("\\Run"):
        return "HKLM", base + (r"\Run32" if "WOW6432Node" in loc else r"\Run")
    if loc == "Startup folder":
        return "HKCU", base + r"\StartupFolder"
    if loc == "Startup folder (all users)":
        return "HKLM", base + r"\StartupFolder"
    return None  # RunOnce: runs once and deletes itself


def _cmd(args: list[str]) -> str:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return "" if done.returncode == 0 else ((done.stdout + done.stderr).strip().splitlines() or ["failed"])[-1]


def unblock_hosts(names: list[str], path: Path | None = None) -> str:
    """Comment out the hosts lines that name a Microsoft address; a backup is written first."""
    path = path or security.HOSTS
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        backup = path.with_name("hosts.cleam-backup")
        if not backup.exists():
            backup.write_text(text, encoding="utf-8")
        wanted = set(names)
        lines = []
        for line in text.splitlines():
            parts = line.split("#", 1)[0].split()
            if len(parts) > 1 and any(p.lower().rstrip(".") in wanted for p in parts[1:]):
                line = "# cleam: " + line
            lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        return str(e.strerror or e)
    return ""


def run(fix: Fix, engine: debloat.Debloater | None = None) -> tuple[bool, str]:
    """Carry out one fix. (ok, what to tell the user)."""
    if fix.admin and OS == "windows" and not is_admin():
        return False, "needs administrator: restart Cleam as administrator"
    if fix.kind == CHANGE:
        engine = engine or debloat.Debloater()
        out = engine.apply(fix.tweak)
        if not out.ok:
            return False, out.message
        return True, "done" + (", takes effect after a restart" if fix.restart else "") + " (Undo debloat can reverse it)"
    if fix.kind == RUN:
        error = fix.call() if fix.call else _ps(fix.script)
        if error:
            return False, error
        return True, "done" + (", takes effect after a restart" if fix.restart else "")
    if fix.kind == OPEN:
        try:
            if fix.extra.get("args"):
                subprocess.Popen([fix.target, fix.extra["args"]], creationflags=NO_WINDOW)
            else:
                os.startfile(fix.target)  # type: ignore[attr-defined]  (Windows only)
        except OSError as e:
            return False, str(e)
        return True, "opened"
    if fix.kind in (UEFI, REBOOT):
        args = ["shutdown", "/r", "/fw", "/t", "10"] if fix.kind == UEFI else ["shutdown", "/r", "/t", "60"]
        error = _cmd(args)
        return (False, error) if error else (True, "restarting soon -- shutdown /a cancels")
    if fix.kind == REFRESH:
        from .hardware import set_refresh

        error = set_refresh(fix.target, fix.hz)
        time.sleep(0.5)
        return (False, error) if error else (True, f"now {fix.hz} Hz")
    return False, "nothing to do"
