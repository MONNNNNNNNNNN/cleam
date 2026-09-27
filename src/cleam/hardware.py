"""Gaming hardware: is the PC running the way its parts allow?

Tweak packs promise FPS from registry values; the frames people actually
lose come from set-up mistakes, and those can be read:

- **Memory** at its JEDEC fallback speed because XMP/EXPO is off, or one
  module (single channel) -- the largest avoidable loss on a CPU-bound game.
- **Display** left at 60 Hz on a faster monitor, or the GPU in a slot or
  mode that runs below its PCIe width; Resizable BAR off (NVIDIA).
- **CPU** capped below 100 % by a power plan, and boot-timer values tweak
  tools leave behind (useplatformclock measurably adds latency; the others
  have no measured gain).
- **Storage** with TRIM switched off, the weekly optimize task disabled, or a
  drive nearly full (an SSD slows down and games fail to patch).

Everything here reads; repair.py makes the changes, on request.
Output of fsutil, bcdedit and powercfg is parsed by value names and numbers,
never by their (translated) labels.
"""
from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess

from .security import BAD, INFO, OK, WARN, Check
from .system import NO_WINDOW, OS

GAMING_GROUPS = ("CPU & mainboard", "Memory", "Graphics & display", "Storage")

# bcdedit values tweak packs set. Absent is Windows' default for every one.
BCD_TWEAKS = {
    "useplatformclock": "forces the HPET timer for all timekeeping: measurably higher input latency and lower FPS",
    "useplatformtick": "forces the platform clock as the tick source",
    "disabledynamictick": "keeps the timer ticking while idle: more power and heat, no measured gain",
    "tscsyncpolicy": "changes how CPU timestamp counters are synchronised",
    "x2apicpolicy": "changes the interrupt controller mode",
    "uselegacyapicmode": "changes the interrupt controller mode",
    "usephysicaldestination": "changes interrupt delivery",
    "usefirmwarepcisettings": "changes PCI resource assignment",
}
DDR4, DDR5 = 26, 34  # SMBIOS memory types
JEDEC_TOP = {DDR4: 2666, DDR5: 4800}  # at or below: likely the no-XMP fallback


def bcd_tweaks(text: str) -> dict[str, str]:
    """{name: value} for every tweak-pack value present in `bcdedit /enum {current}`."""
    found = {}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in BCD_TWEAKS:
            found[parts[0].lower()] = parts[1].strip()
    return found


def trim_off(text: str) -> bool | None:
    """From `fsutil behavior query DisableDeleteNotify`: True when TRIM is off for NTFS."""
    m = re.search(r"NTFS DisableDeleteNotify\s*=\s*(\d)", text) or re.search(r"DisableDeleteNotify\s*=\s*(\d)", text)
    return None if not m else m.group(1) == "1"


def max_state(text: str) -> int | None:
    """Maximum processor state on AC, in %, from `powercfg /query ... PROCTHROTTLEMAX`.

    The hex values come in a fixed order (min, max, increment, AC, DC)
    whatever language the labels are in.
    """
    values = re.findall(r"0x([0-9a-fA-F]{8})", text)
    return int(values[-2], 16) if len(values) >= 2 else None


def hardware_checks(raw: dict, today: dt.date | None = None) -> list[Check]:
    """Pure: every branch is testable off Windows."""
    today = today or dt.date.today()
    out: list[Check] = []

    def add(group: str, *args, **kw) -> None:
        out.append(Check(*args, group=group, **kw))

    # ---- CPU & mainboard
    cpu = raw.get("cpu") or {}
    if cpu.get("name"):
        add("CPU & mainboard", "cpu", "Processor", OK, f"{cpu['name']} ({cpu.get('cores')} cores, "
            f"{cpu.get('threads')} threads).")
    cap = max_state(raw.get("throttle") or "")
    if cap is not None:
        add("CPU & mainboard", "cpu-cap", "Maximum processor state", OK if cap >= 100 else WARN,
            "100 % on mains power." if cap >= 100 else
            f"Capped at {cap} % on mains power by the power plan: the CPU never reaches its full clock.",
            "" if cap >= 100 else "Set it back to 100 % in the active plan.", data={"cap": cap})
    tweaks = bcd_tweaks(raw.get("bcd") or "")
    if tweaks:
        worst = WARN if "useplatformclock" in tweaks or "useplatformtick" in tweaks else INFO
        add("CPU & mainboard", "boot-timers", "Boot timer tweaks", worst,
            "Set by a tweak tool: " + "; ".join(f"{k} {v} ({BCD_TWEAKS[k]})" for k, v in tweaks.items()) + ".",
            "Reset them to Windows' defaults (bcdedit /deletevalue).", data={"values": tweaks})
    board = raw.get("board") or {}
    if board.get("bios_date"):
        age = (today - dt.date.fromisoformat(board["bios_date"])).days
        name = " ".join(x for x in (board.get("maker"), board.get("product")) if x)
        add("CPU & mainboard", "bios", "BIOS", INFO if age > 730 else OK,
            f"{name}, BIOS {board.get('bios')} from {board['bios_date']}"
            + (f" ({age // 365} years old): board makers ship stability and performance fixes (memory "
               "compatibility, AMD AGESA / Intel microcode)." if age > 730 else "."),
            "Check the board maker's support page for a newer BIOS." if age > 730 else "")

    # ---- Memory
    ram = [m for m in raw.get("ram") or [] if isinstance(m, dict)]
    if ram:
        total = sum(m.get("gb") or 0 for m in ram)
        configured = min((m.get("configured") or 0) for m in ram)
        rated = min((m.get("rated") or 0) for m in ram)
        kind = ram[0].get("type")
        label = {DDR4: "DDR4", DDR5: "DDR5"}.get(kind, "RAM")
        if configured and rated and configured < rated * 0.95:
            add("Memory", "ram-speed", "Memory speed", WARN,
                f"{label} runs at {configured} MT/s, below its rated {rated} MT/s.",
                "Turn on XMP / EXPO / DOCP in the UEFI setup.", data={"uefi": True})
        elif configured and kind in JEDEC_TOP and configured <= JEDEC_TOP[kind]:
            add("Memory", "ram-speed", "Memory speed", INFO,
                f"{label} at {configured} MT/s, the standard fallback speed. Gaming kits are sold faster "
                "(DDR4-3200+, DDR5-6000): they only run that fast with XMP / EXPO switched on.",
                "If the box or label says faster, turn on XMP / EXPO / DOCP in the UEFI setup.", data={"uefi": True})
        elif configured:
            add("Memory", "ram-speed", "Memory speed", OK,
                f"{label} at {configured} MT/s" + (" (XMP / EXPO on)." if configured > rated else "."))
        if len(ram) == 1:
            add("Memory", "ram-channels", "Memory channels", WARN,
                "One module: the CPU gets half the memory bandwidth (single channel), which costs frames in "
                "CPU-heavy games.", "Add a matching second module (dual channel).")
        elif len(ram) % 2:
            add("Memory", "ram-channels", "Memory channels", INFO,
                f"{len(ram)} modules: an odd number leaves part of the memory single channel.")
        else:
            add("Memory", "ram-channels", "Memory channels", OK, f"{len(ram)} modules, {total} GB.")
        if total and total < 16:
            add("Memory", "ram-size", "Memory size", INFO,
                f"{total} GB: current games expect 16 GB; with less, Windows pages to disk mid-game.")

    # ---- Graphics & display
    for d in raw.get("displays") or []:
        number = re.sub(r"\D", "", d.get("device") or "")
        d = dict(d, monitor=f"display {number} ({d.get('monitor')})" if number else d.get("monitor"))
        if d.get("hz") and d.get("max_hz") and d["hz"] < d["max_hz"]:
            add("Graphics & display", "refresh", f"Refresh rate: {d.get('monitor') or d.get('device')}", WARN,
                f"Runs at {d['hz']} Hz; this monitor can do {d['max_hz']} Hz at {d['width']}x{d['height']}.",
                f"Switch it to {d['max_hz']} Hz.", data={"display": d})
        elif d.get("hz"):
            add("Graphics & display", "refresh", f"Refresh rate: {d.get('monitor') or d.get('device')}", OK,
                f"{d['hz']} Hz at {d['width']}x{d['height']}, the highest it offers.")
    for g in raw.get("gpus") or []:
        name = g.get("name") or "GPU"
        if "basic display" in name.lower() or "basic render" in name.lower():
            add("Graphics & display", "gpu-driver", "Graphics driver", BAD,
                f"{name}: no graphics driver is installed, so games run without the GPU's acceleration.",
                "Install the driver from NVIDIA, AMD or Intel.", data={"url": vendor_url(name)})
            continue
        if not g.get("date"):
            continue
        age = (today - dt.date.fromisoformat(g["date"])).days
        if any(v in name.lower() for v in ("nvidia", "amd", "radeon", "intel arc")):
            add("Graphics & display", "gpu-driver", f"Driver: {name}", WARN if age > 365 else OK,
                f"Version {g.get('driver')} from {g['date']}"
                + (f", {age // 30} months old: new games get their fixes and performance in newer drivers."
                   if age > 365 else "."),
                "Update the graphics driver." if age > 365 else "", data={"url": vendor_url(name)})
    nv = raw.get("nvidia") or {}
    if nv.get("width_max"):
        slow = nv["width_now"] < nv["width_max"]
        add("Graphics & display", "pcie", "GPU PCIe link", WARN if slow else OK,
            f"x{nv['width_now']} of x{nv['width_max']}: the card is in a slower slot, or sharing lanes with an "
            "M.2 drive." if slow else f"x{nv['width_now']}, full width.",
            "Move the card to the top x16 slot; check the board manual for lanes shared with M.2." if slow else "")
    if nv.get("bar1_mib"):
        on = nv["bar1_mib"] > 256
        add("Graphics & display", "rebar", "Resizable BAR", OK if on else INFO,
            "On: the CPU can reach all of the GPU's memory at once." if on else
            "Off: some games run a few percent faster with it on (RTX 30 and newer).",
            "" if on else "Turn on Above 4G Decoding and Re-Size BAR in the UEFI setup (needs CSM off).",
            data={"uefi": True} if not on else {})

    # ---- Storage
    off = trim_off(raw.get("trim") or "")
    if off is not None:
        add("Storage", "trim", "SSD TRIM", WARN if off else OK,
            "Off for NTFS: the SSD is never told which blocks are free, so writes slow down and it wears "
            "faster. A tweak tool usually turned it off." if off else "On.",
            "Turn TRIM back on." if off else "")
    if raw.get("optimize_task") == 1:
        add("Storage", "optimize-task", "Weekly drive optimization", WARN,
            "The ScheduledDefrag task is disabled: SSDs get no retrim and hard disks no defrag.",
            "Turn the task back on.")
    for v in raw.get("volumes") or []:
        size, free = v.get("size") or 0, v.get("free") or 0
        if not size:
            continue
        pct = 100 * free / size
        if pct < 10:
            add("Storage", "free-space", f"Free space on {v['letter']}:", BAD if pct < 5 else WARN,
                f"{free / 1e9:.0f} GB free of {size / 1e9:.0f} GB ({pct:.0f} %): an SSD this full slows down, "
                "and game updates need room to unpack.",
                "Clean junk" if v["letter"].upper() == "C" else "Move or uninstall games you no longer play.",
                data={"letter": v["letter"]})
    pf = raw.get("pagefile") or {}
    if pf and not pf.get("auto") and not pf.get("files"):
        add("Storage", "pagefile", "Page file", WARN,
            "Off: games that run out of memory crash instead of slowing down.",
            "Let Windows manage the page file.")
    return out


def vendor_url(name: str) -> str:
    low = name.lower()
    if "nvidia" in low:
        return "https://www.nvidia.com/Download/index.aspx"
    if "amd" in low or "radeon" in low:
        return "https://www.amd.com/en/support/download/drivers.html"
    if "intel" in low:
        return "https://www.intel.com/content/www/us/en/support/detect.html"
    return "https://support.microsoft.com/windows/update-drivers-manually-in-windows-ec62f46c-ff14-c91d-eead-d7126dc1f7b6"


# ------------------------------------------------------------------ live reads (Windows)


def nvidia() -> dict:
    """PCIe width and BAR1 size from nvidia-smi, when an NVIDIA driver is installed."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {}
    try:
        q = subprocess.run([exe, "--query-gpu=pcie.link.width.current,pcie.link.width.max",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=20,
                           creationflags=NO_WINDOW).stdout
        mem = subprocess.run([exe, "-q", "-d", "MEMORY"], capture_output=True, text=True, timeout=20,
                             creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    out: dict = {}
    nums = re.findall(r"\d+", q.splitlines()[0]) if q.strip() else []
    if len(nums) >= 2:
        out["width_now"], out["width_max"] = int(nums[0]), int(nums[1])
    m = re.search(r"BAR1 Memory Usage\s+Total\s*:\s*(\d+)\s*MiB", mem)
    if m:
        out["bar1_mib"] = int(m.group(1))
    return out


def _devmode():
    import ctypes
    from ctypes import wintypes

    class DEVMODEW(ctypes.Structure):
        _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                    ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                    ("dmFields", wintypes.DWORD), ("dmPosition", ctypes.c_byte * 16), ("dmColor", ctypes.c_short),
                    ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
                    ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32), ("dmLogPixels", wintypes.WORD),
                    ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
                    ("dmDisplayFlags", wintypes.DWORD), ("dmDisplayFrequency", wintypes.DWORD),
                    ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD),
                    ("dmDitherType", wintypes.DWORD), ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                    ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]

    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32), ("DeviceString", wintypes.WCHAR * 128),
                    ("StateFlags", wintypes.DWORD), ("DeviceID", wintypes.WCHAR * 128),
                    ("DeviceKey", wintypes.WCHAR * 128)]

    return ctypes, DEVMODEW, DISPLAY_DEVICEW


def _modes(device: str):
    ctypes, DEVMODEW, _ = _devmode()
    user32 = ctypes.windll.user32
    i = 0
    while True:
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if not user32.EnumDisplaySettingsW(device, i, ctypes.byref(dm)):
            return
        yield dm
        i += 1


def displays() -> list[dict]:
    """Every active monitor: current mode, and the highest refresh at that resolution."""
    if OS != "windows":
        return []
    ctypes, DEVMODEW, DISPLAY_DEVICEW = _devmode()
    user32 = ctypes.windll.user32
    out = []
    i = 0
    while True:
        dd = DISPLAY_DEVICEW()
        dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            break
        i += 1
        if not dd.StateFlags & 0x1:  # DISPLAY_DEVICE_ATTACHED_TO_DESKTOP
            continue
        cur = DEVMODEW()
        cur.dmSize = ctypes.sizeof(DEVMODEW)
        if not user32.EnumDisplaySettingsW(dd.DeviceName, -1, ctypes.byref(cur)):  # ENUM_CURRENT_SETTINGS
            continue
        best = max((m.dmDisplayFrequency for m in _modes(dd.DeviceName)
                    if m.dmPelsWidth == cur.dmPelsWidth and m.dmPelsHeight == cur.dmPelsHeight
                    and m.dmBitsPerPel == cur.dmBitsPerPel and not m.dmDisplayFlags & 0x2),  # not interlaced
                   default=cur.dmDisplayFrequency)
        mon = DISPLAY_DEVICEW()
        mon.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        name = dd.DeviceName
        if user32.EnumDisplayDevicesW(dd.DeviceName, 0, ctypes.byref(mon), 0) and mon.DeviceString:
            name = mon.DeviceString
        out.append({"device": dd.DeviceName, "monitor": name, "width": cur.dmPelsWidth, "height": cur.dmPelsHeight,
                    "hz": cur.dmDisplayFrequency, "max_hz": best})
    return out


def set_refresh(device: str, hz: int, test: bool = False) -> str:
    """Switch a monitor to `hz` at its current resolution; "" on success.
    test=True only asks Windows whether the mode would work (CDS_TEST)."""
    ctypes, DEVMODEW, _ = _devmode()
    user32 = ctypes.windll.user32
    cur = DEVMODEW()
    cur.dmSize = ctypes.sizeof(DEVMODEW)
    if not user32.EnumDisplaySettingsW(device, -1, ctypes.byref(cur)):
        return "the monitor did not report its mode"
    for m in _modes(device):
        if (m.dmPelsWidth, m.dmPelsHeight, m.dmBitsPerPel, m.dmDisplayFrequency) == (
                cur.dmPelsWidth, cur.dmPelsHeight, cur.dmBitsPerPel, hz) and not m.dmDisplayFlags & 0x2:
            m.dmFields = 0x00400000 | 0x00080000 | 0x00100000 | 0x00040000  # frequency, width, height, bpp
            flags = 0x2 if test else 0x1  # CDS_TEST / CDS_UPDATEREGISTRY
            code = user32.ChangeDisplaySettingsExW(device, ctypes.byref(m), None, flags, None)
            return "" if code == 0 else f"Windows refused the mode (code {code})"
    return f"{hz} Hz is not offered at this resolution"
