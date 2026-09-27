"""Protection status, signs of ransomware, and what starts with the computer.

Cleam is not an antivirus and does not pretend to be: a scanner written here
would miss what Defender catches and flag what it should not, on the one
screen where a wrong answer costs files. So the work splits three ways:

- status(): is something actually protecting this machine? The antivirus
  Windows Security Center knows of, Defender's own state, the firewall, UAC,
  Controlled Folder Access (Windows' ransomware shield) and restore points.
- ransom_signs(): read-only look through the user's own folders for what
  ransomware leaves behind: ransom notes, known encrypted-file extensions, and
  many documents renamed to one strange extension (report.docx.qwer).
- startup(): programs that start with the computer, the place malware keeps
  itself alive, each with the reason it looks odd, if it does.

Nothing here deletes, quarantines or disables anything. quick_scan() starts
Defender's own scan; removing what it finds is Defender's job.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .system import NO_WINDOW, OS

OK, WARN, BAD, UNKNOWN, INFO = "ok", "warn", "bad", "unknown", "info"
CHECK_GROUPS = ("Protection", "Updates", "Hardware security", "Network exposure", "Health", "Gaming")
WIN10_ESU_END = (2027, 10, 12)  # Microsoft: consumer ESU "through October 12, 2027"


@dataclass
class Check:
    id: str
    label: str
    state: str  # ok | warn | bad | unknown | info (a fact with a trade-off, not a verdict)
    detail: str
    fix: str = ""
    group: str = "Protection"


def _powershell(script: str, timeout: int = 60) -> str:
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _as_list(value) -> list:
    """ConvertTo-Json turns a one-element array into a bare object, and $null into nothing."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# ------------------------------------------------------------------ status

STATUS_SCRIPT = r"""
$r = [ordered]@{}
$r.av = @(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction SilentlyContinue |
  ForEach-Object { [ordered]@{ name = $_.displayName; state = [int]$_.productState } })
try { $m = Get-MpComputerStatus -ErrorAction Stop
  $r.defender = [ordered]@{ realtime = [bool]$m.RealTimeProtectionEnabled; enabled = [bool]$m.AntivirusEnabled;
    signature_age = [int]$m.AntivirusSignatureAge; quick_scan_age = [int]$m.QuickScanAge; mode = "$($m.AMRunningMode)" }
} catch { $r.defender = $null }
$pol = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender'
$r.policy_off = ((Get-ItemProperty $pol -ErrorAction SilentlyContinue).DisableAntiSpyware -eq 1) -or
  ((Get-ItemProperty "$pol\Real-Time Protection" -ErrorAction SilentlyContinue).DisableRealtimeMonitoring -eq 1)
$svc = Get-Service WinDefend -ErrorAction SilentlyContinue
$r.defender_service = if ($svc) { "$($svc.Status)/$($svc.StartType)" } else { "missing" }
try { $r.cfa = [int](Get-MpPreference -ErrorAction Stop).EnableControlledFolderAccess } catch { $r.cfa = $null }
try { $r.shadows = @(Get-CimInstance Win32_ShadowCopy -ErrorAction Stop).Count } catch { $r.shadows = $null }
$r.firewall = @(Get-NetFirewallProfile -ErrorAction SilentlyContinue |
  ForEach-Object { [ordered]@{ name = "$($_.Name)"; on = ("$($_.Enabled)" -eq 'True') } })
$r.uac = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -ErrorAction SilentlyContinue).EnableLUA
# --- updates
$cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction SilentlyContinue
$r.build = [int]$cv.CurrentBuild
$hf = Get-HotFix -ErrorAction SilentlyContinue | Where-Object InstalledOn | Sort-Object InstalledOn -Descending | Select-Object -First 1
$r.last_update = if ($hf) { $hf.InstalledOn.ToString('yyyy-MM-dd') } else { $null }
$r.last_update_id = if ($hf) { "$($hf.HotFixID)" } else { $null }
$r.reboot_pending = (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') -or
  (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')
# --- hardware security
try { $r.secureboot = [bool](Confirm-SecureBootUEFI -ErrorAction Stop) } catch { $r.secureboot = $null }
try { $t = Get-Tpm -ErrorAction Stop; $r.tpm = [ordered]@{ present = [bool]$t.TpmPresent; ready = [bool]$t.TpmReady } } catch { $r.tpm = $null }
try { $b = Get-BitLockerVolume -MountPoint $env:SystemDrive -ErrorAction Stop
  $r.bitlocker = [ordered]@{ status = "$($b.VolumeStatus)"; protection = "$($b.ProtectionStatus)" } } catch { $r.bitlocker = $null }
$dg = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity' -ErrorAction SilentlyContinue
$r.hvci = if ($dg -and $null -ne $dg.Enabled) { [int]$dg.Enabled } else { 0 }
$r.battery = @(Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue).Count
# --- network exposure
try { $r.smb1 = [bool](Get-SmbServerConfiguration -ErrorAction Stop).EnableSMB1Protocol } catch { $r.smb1 = $null }
$r.rdp_deny = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' -ErrorAction SilentlyContinue).fDenyTSConnections
$r.rdp_nla = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' -ErrorAction SilentlyContinue).UserAuthentication
try { $r.guest = [bool](Get-LocalUser -ErrorAction Stop | Where-Object { $_.SID -like '*-501' }).Enabled } catch { $r.guest = $null }
# --- health
try { $r.disks = @(Get-PhysicalDisk -ErrorAction Stop | ForEach-Object {
  $c = $_ | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue
  [ordered]@{ name = "$($_.FriendlyName)"; media = "$($_.MediaType)"; health = "$($_.HealthStatus)";
    wear = if ($c) { $c.Wear } else { $null }; temp = if ($c) { $c.Temperature } else { $null } } }) } catch { $r.disks = $null }
$since = (Get-Date).AddDays(-30)
try { $r.crashes = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 41, 1001, 6008; StartTime = $since } -ErrorAction Stop |
  Group-Object Id | ForEach-Object { [ordered]@{ id = [int]$_.Name; count = $_.Count } }) } catch { $r.crashes = @() }
try { $r.bad_devices = @(Get-PnpDevice -PresentOnly -Status ERROR -ErrorAction Stop | ForEach-Object { "$($_.FriendlyName)" } | Where-Object { $_ }) } catch { $r.bad_devices = $null }
$r.uptime_h = [int]((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalHours
# --- gaming
$gb = Get-ItemProperty 'HKCU:\Software\Microsoft\GameBar' -ErrorAction SilentlyContinue
$r.game_mode = if ($gb -and $null -ne $gb.AutoGameModeEnabled) { [int]$gb.AutoGameModeEnabled } else { 1 }
$r.hags = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\GraphicsDrivers' -ErrorAction SilentlyContinue).HwSchMode
$r.power = ((powercfg /getactivescheme) -join ' ') -replace '.*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}).*', '$1'
$r | ConvertTo-Json -Depth 4 -Compress
"""


def av_state(product_state: int) -> tuple[bool, bool]:
    """(enabled, signatures out of date) from Security Center's productState.

    Undocumented but stable since Vista: bit 12 is "on", bit 4 of the low byte
    is "definitions out of date". 397568 (0x61100) is Defender on and current.
    """
    return bool((product_state >> 12) & 1), bool(product_state & 0x10)


def windows_checks(raw: dict, today=None) -> list[Check]:
    """Turn the raw status into checks. Pure, so every branch is testable off Windows."""
    checks: list[Check] = []
    avs = _as_list(raw.get("av"))
    active = [a["name"] for a in avs if av_state(int(a.get("state", 0)))[0]]
    outdated = [a["name"] for a in avs if av_state(int(a.get("state", 0))) == (True, True)]
    defender = raw.get("defender") or {}
    realtime = bool(defender.get("realtime"))
    if active:
        checks.append(Check("antivirus", "Antivirus", WARN if outdated else OK,
                            f"{', '.join(active)} is on" + (" but its definitions are out of date" if outdated else "."),
                            "Update the antivirus definitions." if outdated else ""))
    elif realtime:
        checks.append(Check("antivirus", "Antivirus", OK, "Microsoft Defender is on."))
    else:
        why = "Microsoft Defender is turned off by a policy" if raw.get("policy_off") else "Microsoft Defender is off"
        checks.append(Check(
            "antivirus", "Antivirus", BAD,
            f"No antivirus is protecting this PC. {why} (service: {raw.get('defender_service', 'unknown')}).",
            "Turn Defender back on: remove DisableAntiSpyware and the Real-Time Protection values under"
            " HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender (or undo the tweak tool that set them),"
            " set the WinDefend service to Automatic and restart. Or install and enable another antivirus.",
        ))
    if defender:
        age = defender.get("signature_age")
        if isinstance(age, int) and age > 7:
            checks.append(Check("signatures", "Virus definitions", WARN, f"{age} days old.",
                                "Update Defender: Windows Security > Virus & threat protection > Check for updates."))
        scan_age = defender.get("quick_scan_age")
        if isinstance(scan_age, int) and scan_age > 14:
            checks.append(Check("last-scan", "Last scan", WARN, f"No quick scan for {scan_age} days.", "Run a quick scan."))
    cfa = raw.get("cfa")
    if cfa is None:
        checks.append(Check("ransomware-shield", "Ransomware protection", UNKNOWN,
                            "Controlled Folder Access cannot be read while Defender is off."))
    else:
        checks.append(Check(
            "ransomware-shield", "Ransomware protection", OK if cfa == 1 else WARN,
            "Controlled Folder Access is on." if cfa == 1 else "Controlled Folder Access is off: any program can"
            " change the files in Documents, Pictures and Desktop.",
            "" if cfa == 1 else "Windows Security > Virus & threat protection > Ransomware protection.",
        ))
    shadows = raw.get("shadows")
    if shadows is not None:
        checks.append(Check(
            "restore-points", "Restore points", OK if shadows else WARN,
            f"{shadows} restore point(s) / shadow copies." if shadows else "No restore points or shadow copies.",
            "" if shadows else "Create one on the Snapshots page. Keep a backup off this PC too: ransomware"
            " deletes shadow copies first.",
        ))
    firewall = _as_list(raw.get("firewall"))
    off = [f["name"] for f in firewall if not f.get("on")]
    if firewall:
        checks.append(Check("firewall", "Firewall", BAD if off else OK,
                            f"Off for: {', '.join(off)}." if off else "On for every network profile.",
                            "Windows Security > Firewall & network protection." if off else ""))
    uac = raw.get("uac")
    if uac is not None:
        checks.append(Check("uac", "User Account Control", OK if uac == 1 else BAD,
                            "On." if uac == 1 else "Off: every program runs with full rights, unasked.",
                            "" if uac == 1 else "Turn UAC back on (EnableLUA = 1) and restart."))
    return checks + system_checks(raw, today)


def system_checks(raw: dict, today=None) -> list[Check]:
    """Updates, hardware security, exposure, health and gaming. Pure, like windows_checks."""
    import datetime as dt

    today = today or dt.date.today()
    out: list[Check] = []

    def add(group: str, *args, **kw) -> None:
        out.append(Check(*args, group=group, **kw))

    # ---- updates
    build = raw.get("build") or 0
    last = raw.get("last_update")
    if last:
        days = (today - dt.date.fromisoformat(last)).days
        state = BAD if days > 90 else WARN if days > 45 else OK
        add("Updates", "last-update", "Last Windows update", state,
            f"{last} ({raw.get('last_update_id') or 'update'}), {days} days ago.",
            "" if state == OK else "Settings > Windows Update > Check for updates. Security fixes arrive monthly.")
    if build and build < 22000:
        end = dt.date(*WIN10_ESU_END)
        state = BAD if today > end else INFO
        add("Updates", "windows-10-support", "Windows 10 support", state,
            "Windows 10 support ended on 14 Oct 2025. Security updates now come only through Extended Security "
            f"Updates (ESU), until {end:%d %b %Y}." if state == INFO else "Windows 10 ESU has ended: no more security updates.",
            "Enroll in ESU (Settings > Windows Update) if updates stopped, or move to Windows 11 when the PC allows.")
    if raw.get("reboot_pending"):
        add("Updates", "reboot", "Restart pending", WARN, "An update is waiting for a restart to finish installing.",
            "Restart when convenient: until then the fix is not active.")
    # ---- hardware security
    sb = raw.get("secureboot")
    add("Hardware security", "secure-boot", "Secure Boot", OK if sb else WARN if sb is False else UNKNOWN,
        "On." if sb else "Off: a boot-level rootkit could load before Windows. Battlefield 6 (EA Javelin) and, "
        "on Windows 11, Valorant refuse to start without it." if sb is False else
        "Not reported: legacy BIOS mode, or Cleam is not running as administrator.",
        "" if sb else "Turn on Secure Boot in the UEFI/BIOS setup (the disk must use GPT).")
    tpm = raw.get("tpm")
    if isinstance(tpm, dict):
        ok = tpm.get("present") and tpm.get("ready")
        add("Hardware security", "tpm", "TPM", OK if ok else WARN,
            "Present and ready." if ok else "Missing or not ready: BitLocker and Windows 11 need it.",
            "" if ok else "Enable fTPM (AMD) or PTT (Intel) in the UEFI setup.")
    bl = raw.get("bitlocker")
    if isinstance(bl, dict):
        on = bl.get("protection") == "On"
        laptop = bool(raw.get("battery"))
        add("Hardware security", "encryption", "Drive encryption (C:)", OK if on else WARN if laptop else INFO,
            "BitLocker is protecting C:." if on else "C: is not encrypted: anyone with the disk can read it."
            + (" On a laptop that can be stolen, that matters." if laptop else ""),
            "" if on else "Settings > Privacy & security > Device encryption, or BitLocker (Pro).")
    if raw.get("hvci") is not None:
        on = raw.get("hvci") == 1
        add("Hardware security", "memory-integrity", "Memory integrity", OK if on else INFO,
            "On: malicious or vulnerable drivers are blocked." if on else "Off. Microsoft notes it can cost "
            "performance in some games, but it blocks malicious and vulnerable drivers.",
            "" if on else "Windows Security > Device security > Core isolation, if you value protection over FPS.")
    # ---- network exposure
    if raw.get("smb1") is not None:
        add("Network exposure", "smb1", "SMBv1", BAD if raw["smb1"] else OK,
            "SMBv1 is ON: the protocol WannaCry spread through." if raw["smb1"] else "Off.",
            "Turn off: Disable-WindowsOptionalFeature -Online -FeatureName SMB1Protocol" if raw["smb1"] else "")
    deny = raw.get("rdp_deny")
    if deny is not None:
        if deny == 1:
            add("Network exposure", "rdp", "Remote Desktop", OK, "Off.")
        else:
            nla = raw.get("rdp_nla") == 1
            add("Network exposure", "rdp", "Remote Desktop", WARN if nla else BAD,
                "On, with Network Level Authentication." if nla else
                "On WITHOUT Network Level Authentication: anyone reaching port 3389 gets a login screen.",
                "Turn it off if you don't use it (Settings > System > Remote Desktop)." if nla
                else "Require Network Level Authentication, or turn Remote Desktop off.")
    if raw.get("guest"):
        add("Network exposure", "guest", "Guest account", WARN, "Enabled.", "net user guest /active:no")
    # ---- health
    for disk in _as_list(raw.get("disks")):
        name = disk.get("name") or "Disk"
        problems = []
        if disk.get("health") not in (None, "", "Healthy"):
            problems.append(f"Windows reports it {disk['health']}")
        if isinstance(disk.get("wear"), int) and disk["wear"] >= 90:
            problems.append(f"{disk['wear']}% worn")
        if isinstance(disk.get("temp"), int) and disk["temp"] >= 60:
            problems.append(f"{disk['temp']} °C")
        state = BAD if disk.get("health") not in (None, "", "Healthy") else WARN if problems else OK
        temp = f", {disk['temp']} °C" if isinstance(disk.get("temp"), int) and disk["temp"] > 0 else ""
        add("Health", "disk", f"Disk: {name}", state,
            ("; ".join(problems) + ".") if problems else f"Healthy ({disk.get('media') or 'disk'}{temp}).",
            "Back up this disk now and plan to replace it." if state == BAD else "")
    crashes = {int(c.get("id", 0)): int(c.get("count", 0)) for c in _as_list(raw.get("crashes"))}
    bsod, power = crashes.get(1001, 0), crashes.get(41, 0) + crashes.get(6008, 0)
    add("Health", "crashes", "Crashes (30 days)", WARN if bsod or power else OK,
        f"{bsod} blue screen(s), {power} unexpected shutdown(s)." if bsod or power else "None.",
        "Look for a pattern: a new driver, overheating, unstable overclock or power supply." if bsod or power else "")
    bad = _as_list(raw.get("bad_devices"))
    if raw.get("bad_devices") is not None:
        add("Health", "devices", "Devices", WARN if bad else OK,
            f"Reporting a problem: {', '.join(bad)}." if bad else "No device reports a problem.",
            "Device Manager: update or reinstall the driver; a device switched off by a tweak shows here too."
            if bad else "")
    uptime = raw.get("uptime_h")
    if isinstance(uptime, int) and uptime >= 168:
        add("Health", "uptime", "Uptime", INFO, f"{uptime // 24} days since the last restart.",
            "Restart now and then: updates and driver resets finish only on a restart.")
    # ---- gaming
    add("Gaming", "game-mode", "Game Mode", OK if raw.get("game_mode", 1) else WARN,
        "On." if raw.get("game_mode", 1) else "Off: Windows Update and background work may interrupt games.",
        "" if raw.get("game_mode", 1) else "Turn it on in Debloat > Gaming & comfort.")
    hags = raw.get("hags")
    if hags is not None:
        add("Gaming", "hags", "GPU scheduling (HAGS)", INFO, "On." if hags == 2 else "Off.",
            "" if hags == 2 else "Settings > Display > Graphics; needs a supporting GPU and driver.")
    power = (raw.get("power") or "").lower()
    if power:
        balanced = power in ("381b4222-f694-41f0-9685-ff5bb260df2e", "a1841308-3541-4fab-bc81-f71556f20b4a")
        add("Gaming", "power-plan", "Power plan", INFO,
            "Balanced or power saver: clocks drop between frames." if balanced else "A performance plan is active.",
            "Debloat > Gaming & comfort > High performance power plan." if balanced else "")
    return out


def status() -> list[Check]:
    if OS == "windows":
        out = _powershell(STATUS_SCRIPT, timeout=180)
        try:
            raw = json.loads(out) if out.strip() else None
        except ValueError:
            raw = None
        if not isinstance(raw, dict):
            return [Check("antivirus", "Antivirus", UNKNOWN, "Windows would not report its protection status.")]
        return windows_checks(raw)
    checks = []
    scanner = shutil.which("clamscan")
    checks.append(Check("antivirus", "Malware scanner", OK if scanner else WARN,
                        "ClamAV is installed." if scanner else "No malware scanner installed.",
                        "" if scanner else "sudo apt install clamav (Linux) / brew install clamav (macOS)."))
    return checks


# --------------------------------------------------------------- scanning

MPCMDRUN = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Windows Defender" / "MpCmdRun.exe"


def can_quick_scan(checks: list[Check]) -> bool:
    """Defender can scan only when it is the one running; MpCmdRun fails when it is disabled."""
    return OS == "windows" and MPCMDRUN.exists() and any(
        c.id == "antivirus" and c.state == OK and "Defender" in c.detail for c in checks
    )


def quick_scan(timeout: int = 3600) -> tuple[int, str]:
    """Defender's quick scan. What it finds, Defender quarantines -- not Cleam."""
    try:
        done = subprocess.run(
            [str(MPCMDRUN), "-Scan", "-ScanType", "1"],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 1, f"The scan could not start: {e}"
    lines = [line.strip() for line in (done.stdout + done.stderr).splitlines() if line.strip()]
    return done.returncode, lines[-1] if lines else ("Scan finished." if done.returncode == 0 else "Scan failed.")


# ------------------------------------------------------ signs of ransomware

# File names ransom notes are known by (lower case, compared without extension
# too). Families rename their notes, so this catches the common ones, not all.
NOTE_NAMES = (
    # "_readme.txt" whole: STOP/Djvu's note. The bare stem would match a
    # developer's _README.md.
    "@please_read_me@", "@wanadecryptor@", "_readme.txt", "readme_for_decrypt", "how_to_decrypt",
    "how_to_decrypt_files", "how_to_recover_files", "how to restore your files", "how_to_restore_files",
    "decrypt_instructions", "decrypt_instruction", "help_decrypt", "help_to_decrypt", "help_your_files",
    "restore_files", "recovery_instructions", "_help_instructions", "your_files_are_encrypted",
    "files_encrypted", "#decrypt my files#", "!!!_readme_!!!", "readme-warning", "info.hta", "decryption_instructions",
    "restore-my-files", "!recovery_instructions!", "read_me_to_decrypt", "how_to_back_files",
)
# Extensions specific to one family, where a single file is a strong sign.
FAMILY_EXTENSIONS = {
    "wncry", "wnry", "wcry", "locky", "zepto", "odin", "thor", "aesir", "cerber", "cerber3", "cryptolocker",
    "crypz", "cryp1", "crysis", "dharma", "ryk", "ryuk", "conti", "lockbit", "phobos", "makop", "hive",
}
# Extensions that also have honest uses; only many of them together mean anything.
GENERIC_EXTENSIONS = {"encrypted", "locked", "crypted", "crypt", "enc", "cry"}
DOC_EXTENSIONS = {
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "pdf", "odt", "txt", "rtf", "csv", "jpg", "jpeg", "png",
    "gif", "bmp", "heic", "psd", "mp3", "mp4", "mov", "zip", "rar", "7z",
}
# Honest second extensions: downloads in progress, backups, signatures, torrents.
BENIGN_OUTER = {
    "part", "crdownload", "download", "opdownload", "tmp", "bak", "old", "orig", "backup", "torrent", "lnk",
    "url", "sig", "asc", "sha256", "md5", "json", "xml", "txt", "ini", "meta", "zip", "7z", "gz", "rar",
    "pdf", "jpg", "png", "sfk", "pek", "cfa", "xmp", "aae", "!ut", "!qb", "db",
    # Sidecars that tools write next to every file: Ableton (.asd), Reaper,
    # Audition (.pkf), DxO, RawTherapee, ON1; subtitles; Chrome's save swap.
    "asd", "reapeaks", "pkf", "dop", "pp3", "on1", "srt", "vtt", "sub", "idx", "crswap",
}
RENAMED = re.compile(r"\.([a-z0-9]{2,6})\.([a-z0-9!]{3,12})$")
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "appdata", ".cache", "site-packages"}
MANY = 20  # renamed documents sharing one extension before it counts


@dataclass
class RansomReport:
    files_checked: int = 0
    notes: list[str] = field(default_factory=list)
    family: dict[str, list[str]] = field(default_factory=dict)  # ext -> examples
    generic: dict[str, list[str]] = field(default_factory=dict)
    renamed: dict[str, list[str]] = field(default_factory=dict)  # outer ext -> examples
    renamed_count: dict[str, int] = field(default_factory=dict)
    truncated: bool = False

    @property
    def state(self) -> str:
        if self.notes or self.family or any(n >= MANY for n in self.renamed_count.values()):
            return BAD
        if any(len(v) >= MANY for v in self.generic.values()):
            return WARN
        return OK

    def findings(self) -> list[str]:
        out = [f"Ransom note: {p}" for p in self.notes[:20]]
        out += [f"{len(v)}+ files ending .{ext} (a known ransomware family), e.g. {v[0]}" for ext, v in self.family.items()]
        out += [f"{self.renamed_count[ext]} documents renamed to *.{ext}, e.g. {v[0]}"
                for ext, v in self.renamed.items() if self.renamed_count[ext] >= MANY]
        out += [f"{len(v)} files ending .{ext}, e.g. {v[0]}" for ext, v in self.generic.items() if len(v) >= MANY]
        return out


def personal_folders() -> list[Path]:
    home = Path.home()
    names = ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music")
    found = [home / n for n in names] + sorted(home.glob("OneDrive*"))
    return [p for p in found if os.path.isdir(p)]


def _is_note(name: str) -> bool:
    lower = name.lower()
    stem = lower.rsplit(".", 1)[0] if "." in lower else lower
    return lower in NOTE_NAMES or stem in NOTE_NAMES


def ransom_signs(roots: list[Path] | None = None, limit: int = 400_000, seconds: float = 120) -> RansomReport:
    """Read-only walk of the user's folders for what ransomware leaves behind.

    No link or junction is followed and nothing is opened: names only, so a
    folder of a million photos takes seconds, not hours.
    """
    report = RansomReport()
    deadline = time.monotonic() + seconds
    stack = [str(p) for p in (roots if roots is not None else personal_folders())]
    while stack:
        if report.files_checked >= limit or time.monotonic() > deadline:
            report.truncated = True
            break
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if getattr(st, "st_file_attributes", 0) & 0x400 or stat.S_ISLNK(st.st_mode):
                continue
            if stat.S_ISDIR(st.st_mode):
                if e.name.lower() not in SKIP_DIRS:
                    stack.append(e.path)
                continue
            report.files_checked += 1
            lower = e.name.lower()
            if _is_note(e.name):
                report.notes.append(e.path)
            ext = lower.rsplit(".", 1)[-1] if "." in lower else ""
            if ext in FAMILY_EXTENSIONS:
                report.family.setdefault(ext, []).append(e.path)
            elif ext in GENERIC_EXTENSIONS:
                report.generic.setdefault(ext, []).append(e.path)
            m = RENAMED.search(lower)
            if m and m.group(1) in DOC_EXTENSIONS and m.group(2) not in BENIGN_OUTER | DOC_EXTENSIONS:
                outer = m.group(2)
                report.renamed_count[outer] = report.renamed_count.get(outer, 0) + 1
                if len(report.renamed.setdefault(outer, [])) < 3:
                    report.renamed[outer].append(e.path)
    return report


# ---------------------------------------------------------------- startup


@dataclass
class StartupItem:
    name: str
    command: str
    location: str
    path: str = ""  # the program it runs, when that could be worked out
    enabled: bool = True
    signed: str = ""  # Valid | NotSigned | HashMismatch | ... (Windows), "" if unknown
    publisher: str = ""
    reasons: list[str] = field(default_factory=list)

    @property
    def suspicious(self) -> bool:
        return bool(self.reasons)


RUN_KEYS = (
    ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
    ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
    ("HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
    ("HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\RunOnce"),
)
SCRIPT_HOSTS = ("wscript.exe", "cscript.exe", "mshta.exe", "regsvr32.exe")


def program_of(command: str) -> str:
    """The executable a Run-key command line starts, with %VARS% expanded."""
    command = os.path.expandvars(command.strip())
    if command.startswith('"'):
        end = command.find('"', 1)
        return command[1:end] if end > 0 else command[1:]
    lower = command.lower()
    for ext in (".exe", ".bat", ".cmd", ".com", ".vbs", ".js", ".ps1", ".lnk"):
        at = lower.find(ext + " ")
        if at < 0 and lower.endswith(ext):
            at = len(lower) - len(ext)
        if at >= 0:
            return command[: at + len(ext)]
    return command.split(" ", 1)[0]


def judge(item: StartupItem, env: dict[str, str] | None = None) -> list[str]:
    """Why a startup entry deserves a second look. Pure: env is injectable for tests."""
    env = env if env is not None else dict(os.environ)
    reasons: list[str] = []
    path = item.path
    low = path.lower().replace("/", "\\")
    cmd = item.command.lower()
    if path and not os.path.exists(path):
        reasons.append("the program it starts does not exist")
    temp = (env.get("TEMP") or "").lower()
    # %TEMP% is often an 8.3 short path (ADMINI~1) while commands use the long one.
    if (temp and low.startswith(temp.rstrip("\\") + "\\")) or "\\appdata\\local\\temp\\" in low:
        reasons.append("runs from the temp folder")
    if "\\downloads\\" in low:
        reasons.append("runs from Downloads")
    for var in ("APPDATA", "LOCALAPPDATA", "ProgramData"):
        base = (env.get(var) or "").lower().rstrip("\\")
        if base and os.path.dirname(low) == base:
            reasons.append(f"program sits loose in %{var}%, not in a program folder")
    if any(host in cmd for host in SCRIPT_HOSTS):
        reasons.append("starts a script host")
    if "powershell" in cmd and any(flag in cmd for flag in (" -enc", " -encodedcommand", " -e ", "frombase64string")):
        reasons.append("runs hidden, encoded PowerShell")
    if "rundll32" in cmd and ("\\appdata\\" in cmd or "\\temp\\" in cmd or "\\programdata\\" in cmd):
        reasons.append("loads a DLL from a user-writable folder")
    if item.signed and item.signed not in ("Valid", "UnknownError") and low.endswith(".exe"):
        reasons.append("not signed by its publisher" if item.signed == "NotSigned" else f"signature: {item.signed}")
    return reasons


def _windows_startup() -> list[StartupItem]:
    import winreg

    items: list[StartupItem] = []
    hives = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
    approved = _startup_approved(winreg)
    for hive, key in RUN_KEYS:
        try:
            with winreg.OpenKey(hives[hive], key) as k:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    if not isinstance(value, str) or not value.strip():
                        continue
                    items.append(StartupItem(name, value, f"{hive}\\{key}", program_of(value),
                                             enabled=approved.get(name.lower(), True)))
        except OSError:
            continue
    folders = [
        (Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup", "Startup folder"),
        (Path(os.environ.get("ProgramData", "")) / r"Microsoft\Windows\Start Menu\Programs\StartUp",
         "Startup folder (all users)"),
    ]
    for folder, where in folders:
        if not os.path.isdir(folder):
            continue
        for entry in os.scandir(folder):
            if entry.is_file() and entry.name.lower() != "desktop.ini":
                items.append(StartupItem(entry.name, entry.path, where, entry.path,
                                         enabled=approved.get(entry.name.lower(), True)))
    _signatures(items)
    return items


def _startup_approved(winreg) -> dict[str, bool]:
    """Task Manager's Startup tab switch: the first byte of each value is 2 (on) or 3 (off)."""
    state: dict[str, bool] = {}
    base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for sub in ("Run", "Run32", "StartupFolder"):
            try:
                with winreg.OpenKey(hive, f"{base}\\{sub}") as k:
                    i = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(k, i)
                        except OSError:
                            break
                        i += 1
                        if isinstance(value, bytes) and value:
                            state[name.lower()] = value[0] % 2 == 0
            except OSError:
                continue
    return state


def _signatures(items: list[StartupItem]) -> None:
    """Authenticode status and signer of every startup program, in one PowerShell call."""
    paths = sorted({i.path for i in items if i.path and os.path.isfile(i.path)})
    if not paths:
        return
    listed = ",".join("'" + p.replace("'", "''") + "'" for p in paths)
    script = (
        f"@({listed}) | ForEach-Object {{ $s = Get-AuthenticodeSignature -LiteralPath $_;"
        " [ordered]@{ path = $_; status = \"$($s.Status)\";"
        " signer = if ($s.SignerCertificate) { $s.SignerCertificate.GetNameInfo('SimpleName', $false) } else { '' } }"
        " } | ConvertTo-Json -Compress"
    )
    try:
        found = {r["path"]: r for r in _as_list(json.loads(_powershell(script, timeout=120) or "null"))}
    except (ValueError, TypeError, KeyError):
        return
    for item in items:
        info = found.get(item.path)
        if info:
            item.signed, item.publisher = info.get("status", ""), info.get("signer", "")


def _xdg_startup() -> list[StartupItem]:
    items = []
    for folder in (Path.home() / ".config" / "autostart", Path("/etc/xdg/autostart")):
        for entry in sorted(folder.glob("*.desktop")) if folder.is_dir() else []:
            try:
                text = entry.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))
            command = fields.get("Exec", "")
            items.append(StartupItem(fields.get("Name", entry.stem), command, str(folder),
                                     shutil.which(command.split(" ", 1)[0]) or "",
                                     enabled=fields.get("Hidden", "false").lower() != "true"))
    return items


def _launchd_startup() -> list[StartupItem]:
    import plistlib

    items = []
    for folder in (Path.home() / "Library/LaunchAgents", Path("/Library/LaunchAgents"), Path("/Library/LaunchDaemons")):
        for entry in sorted(folder.glob("*.plist")) if folder.is_dir() else []:
            try:
                data = plistlib.loads(entry.read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            args = data.get("ProgramArguments") or [data.get("Program", "")]
            items.append(StartupItem(data.get("Label", entry.stem), " ".join(map(str, args)), str(folder),
                                     str(args[0]) if args else "", enabled=not data.get("Disabled", False)))
    return items


def startup() -> list[StartupItem]:
    """Everything that starts with the computer, oddest first."""
    items = _windows_startup() if OS == "windows" else _launchd_startup() if OS == "macos" else _xdg_startup()
    for item in items:
        item.reasons = judge(item)
    return sorted(items, key=lambda i: (not i.suspicious, i.name.lower()))
