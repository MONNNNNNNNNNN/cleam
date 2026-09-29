# Cleam

Junk cleaner, uninstaller, and restore-point/snapshot tool. The name is
**Cleam** — not a typo for "clean". Target platforms: Windows, Linux, macOS,
Android, iOS. What each can actually support is in `docs/platforms.md`; read it
before promising a mobile feature — the OS sandbox forbids most of them.

## Current state

CLI plus the terminal menu (`tui.py`), and nothing else. The one entry for
users is `irm https://raw.githubusercontent.com/MONNNNNNNNNNN/cleam/main/menu.ps1 | iex`:
it fetches the CLI build of the latest release and opens the menu. The core
is **stdlib only** (no runtime dependencies — keeps the portable binary small
and the supply chain empty). Also built in CI: a portable binary per OS and an
Inno Setup installer around `cleam.exe` (its shortcut runs `cleam.exe menu`).

```
src/cleam/
  overview.py   OS/build, disk usage, big-folder sizes with "is this normal" notes
  junk.py       targets per OS + the scan/delete walk (the dangerous part)
  apps.py       installed programs; uninstall = run the platform's own uninstaller
  leftovers.py  remnants an uninstaller left; move-to-backup + restore
  snapshot.py   Windows restore points, Timeshift/Snapper, tmutil
  system.py     OS detection, is_admin(), sudo(), output()
  cli.py        argparse; entry point `cleam` (bare `cleam` in a TTY opens the menu)
  tui.py        the terminal menu: Overview, Clean, Programs, Debloat, System check, Snapshots, Undo
menu.ps1            the one-line Windows entry (irm | iex)
packaging/entry.py  PyInstaller entry (__main__.py's relative import breaks it)
```

Test: `PYTHONPATH=src python3 -m unittest discover -s tests`

## Overview (read-only, deletes nothing)

Mon's ask (2026-09-18): a folder size like "AppData 33.7 GB" means nothing
without context, so every big folder carries what is normal and what actually
shrinks it. `overview.py` is read-only by construction.

- **`ProductName` lies.** Windows 11 still reports "Windows 10 Pro" in
  `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion`. `CurrentBuild >= 22000`
  is what makes it 11. `DisplayVersion` gives 24H2, `UBR` the patch number.
- **WinSxS is a hardlink farm.** Its files are hardlinks into `C:\Windows`, so
  any per-file sum counts them twice, Explorer included. Windows directory
  entries carry no link count (`DirEntry.stat().st_nlink` is 0), so dedup by
  `(st_dev, st_ino)` works on POSIX only, and the Windows row says so instead
  of pretending. Never suggest deleting WinSxS by hand —
  `DISM /Online /Cleanup-Image /StartComponentCleanup`.
- **`biggest()` skips junctions and sizes files.** `C:\Documents and
  Settings`, `%USERPROFILE%\Application Data` and `Local Settings` are
  junctions: following them counted C:\Users twice and AppData\Local three
  times. `pagefile.sys` is a top-level file and was reported as 0 B.
- **What was actually on Mon's C: (2026-09-26, 100 GB, 85% full):** NVIDIA App
  `UpdateFramework\ota-artifacts` 3.7 GB (driver payloads it never deletes),
  `npm-cache\_npx` 2.5 GB, JetBrains remote client 1.9 GB (opt-in: an
  entry's mtime is when it was downloaded, never when it was last used — the
  same holds for npx, so neither can promise "unused"), Zen cache 0.9 GB,
  `termius-updater` 0.6 GB. Not Cleam's to delete: `C:\Windows\ConfigSetRoot`
  (4.5 GB copy of the install media from Setup), Malwarebytes quarantine,
  `Windows\LastGood*` (driver rollback). WinSxS had 0 reclaimable.
  A second full walk (13 s, 542k files, nothing unreadable as admin) added:
  UE `Saved\D3DDriverByteCodeBlob_*.ushaderprecache` 786 MB (Saved also holds
  save games, hence a name pattern), abandoned `.vscode\extensions\.<uuid>`
  extractions 296 MB (unregistered in extensions.json), SYSTEM-profile
  CrashDumps 126 MB, `LocalLow\NVIDIA\DXCache`, PowerToys `Updates`.
  Rejected on evidence: GoogleUpdater `crx_cache` (one installer per app,
  kept for differential updates, so it regrows), Zoom `zoom_install_src` (no
  source says it is safe while Zoom is installed), Adobe `ARM`
  (repair/rollback patches), `ProgramData\Package Cache` (WiX needs it to
  uninstall and repair), `System32\LogFiles\WMI\RtBackup` and SleepStudy
  (live ETW sessions). Old DriverStore packages exist (AMD OpenCL 2024, five
  nvhda versions), but `pnputil` output is localized, so parsing it is not
  safe; report only.
- **`size()` shares junk.py's rules:** skip reparse points, never cross a
  filesystem, never follow a symlink. It returns unreadable-directory count as
  a third value, so a root-owned folder shows "needs admin" rather than 0 B.
- **Listing and measuring are separate.** `folders()` returns rows instantly;
  `measure()` is the walk. The menu shows the rows at once and walks them only
  on M, because `C:\Windows` and `AppData` are hundreds of thousands of files.
- `/proc/mounts` is mostly squashfs snaps, tmpfs and overlays;
  `parse_mounts()` keeps real `/dev/*` devices only.

## One entry: menu.ps1 (2026-09-29)

Mon asked for the GUI to go and for `irm .../menu.ps1 | iex` to be the only
way in. The Flet window (`gui.py`), `run.ps1`, the `[gui]` extra, the GUI
release asset and the Flet-based Android/iOS builds were deleted; git history
has them. Everything the window did is in the terminal menu now, and the menu
decides nothing either: it calls the same core as the CLI.

- **Overview**: disks and the explained folders; nothing is walked until M
  (Measure) -- `C:\Windows` and AppData are hundreds of thousands of files.
- **Programs**: a checklist, so several programs go in one pass (the BCU
  idea). A program whose uninstaller is missing is listed apart and
  force-removed through `leftovers.force_plan` (the CLI's plan); the others
  run their own uninstaller, captured (`sudo -n`, `-y`) since a password
  prompt inside the menu would hang it. High-confidence leftovers of what
  uninstalled are offered once at the end, into a restorable backup.
- **Snapshots**: create/list, captured the same way.
- **Nothing destructive is pre-selected**: `junk.Target.opt_in` (Recycle Bin,
  package caches, shader caches) is never ticked for the user.
- `message()` returns a button's action only for a click; a key comes back
  as itself, so every button there also has a letter (M, C, L).
- Confirm texts for fixes come from `repair.explain(fix)`.
- **How to see it from this Linux box:** run `python -m cleam menu` in a pty
  (`pty.fork`, COLUMNS/LINES set), send keys, strip ANSI from the output and
  grep the frames.

Development happens on a Linux ARM box. The Windows and macOS code paths
cannot run there — only CI executes them. Say so when reporting a change to
them as done.

`tests/platform_smoke.py` is the end-to-end check that the unit tests cannot
be: it calls the installed command and the real platform (the real registry,
the real `/Applications`), with a leftovers round-trip inside a sandboxed
HOME. `.github/workflows/platform-tests.yml` runs it on all three OSes, and
builds and silently installs/uninstalls the Windows installer. Run it after touching anything platform-specific — the unit
tests mock the platform away, which is why they stay green while a Windows
path is broken.

A trap that cost a CI round, in the harness rather than in Cleam: PowerShell
returns a bare string when `Where-Object` matches once, so `$found[0]` is the
first character — wrap it in `@()`.

## Safety invariants — do not weaken

Cleam deletes files. Each rule below exists because the naive version destroys
something:

- **Dry run unless `--yes`.** `uninstall` prompts unless `--yes`. A bare
  `clean --yes` skips opt-in targets (bin, package caches); they need
  `--only <id>` or `--all`, so a cron job never empties the Recycle Bin.
- **Command targets run with `cwd` inside the cache**, never the caller's:
  Yarn 2+ `cache clean` wipes the cwd project's (possibly committed) cache.
- **Scan and delete are one walk** (`junk.run(target, delete=...)`). Never
  delete from a previously built list: a path can be swapped for a symlink
  between the report and the delete.
- **POSIX uses `os.fwalk` + `dir_fd` unlink.** Race-safe against symlinks
  planted in `/tmp` when run as root. Do not replace it with `os.walk` +
  path-based `unlink`.
- **Windows skips every reparse point** (symlinks, junctions) and re-`lstat`s
  right before unlink. `DirEntry.stat()` zeroes `st_dev` on Windows — use
  `os.lstat` for the device check.
- **Never cross filesystems** (`st_dev` of the root).
- **Temp dirs: regular files only.** tmux's socket lives in `/tmp`.
- **Tool-owned caches are cleared by the tool** (`mode="command"`: npm, pip,
  uv, yarn, go, dart pub). Cleam only measures the roots, runs the cleaner
  with stdin closed and a timeout, and reports the before/after difference —
  a nonzero exit is an error, never a success. `npm cache clean` does not
  touch `_npx` (2.5 GB on Mon's desktop against 0.6 GB in `_cacache`), so
  `npx-cache` is an entries target; MCP servers run from there, hence a week.
- **`patterns` narrows a files target by name** (`Windows\Logs` holds more
  than logs). Lower-case fnmatch, matched case-insensitively.
- **App caches go whole or not at all** (`mode="entries"`). Age-deleting single
  files inside uv's or prisma's cache corrupts it. On the dev box, `~/.cache`
  had 2.4 GiB of uv files older than 7 days inside a cache used daily.
- **An unset env var drops the root.** `Path("")` is the cwd.
- **`safe_root()`** refuses drive roots, home and its ancestors, and system
  dirs. Every root is resolved before walking (macOS `/tmp` is a symlink, and
  `fwalk` does not descend a symlinked top).
- **Exclude `sys._MEIPASS`**: a one-file PyInstaller build unpacks into the
  temp dir it cleans.
- **Uninstall never deletes app files directly** — it runs the registered
  uninstaller, so the app's own cleanup happens. The one exception is
  `uninstall --force`, for an uninstaller that is gone: it *moves* the
  program's folder, its Installed-apps key and its high-confidence leftovers
  into a backup (`leftovers.forced`), never deletes, and stops at the first
  failure so a locked folder never ends with its entry deleted.

## Leftovers (the Revo-style part)

Mon asked for Revo Uninstaller's depth (2026-09-18). `leftovers.py` covers the
part that matters: remnants after an uninstall, and a backup that makes
accepting them safe.

- **Nothing is deleted, everything is moved.** Files go into
  `backup_dir()/<label>-<stamp>/files`, registry keys are `reg export`ed then
  deleted, and `manifest.json` holds the original path of each item. `restore()`
  reverses it and refuses to overwrite a path that exists again. apt `purge` is
  the one irreversible action, and the manifest says so.
- **Immediate children of known roots only, never a deep walk.** A remnant is
  at `%APPDATA%\Publisher`. Name-matching an entire drive is how this class of
  tool deletes someone's project folder.
- **`score()` is the whole safety model.** Exact token-set match is "high"
  (ticked by default); containing the alias is "low" (never ticked). `NOISE`
  drops generic words, so publisher "Microsoft" matches nothing by itself —
  otherwise removing Teams would offer `%LOCALAPPDATA%\Microsoft`, which holds
  Edge and Office.
- **Candidates matching another installed program are dropped** (`other_apps`),
  because `%LOCALAPPDATA%\Google` belongs to Chrome as much as to Earth.
- **`last_path_part()`, not `os.path.basename`.** basename is
  platform-specific, so a Windows install directory parsed on Linux yielded no
  alias at all.
- **Forced uninstall (2026-09-29):** the folder is `InstallLocation`, else
  the folder of `DisplayIcon`/`UninstallString` -- never Windows\Installer,
  the MSI Package Cache or System32, where every MSI's icon and uninstaller
  live. Guards: `safe_root`, not a link, one Cleam worked out must be named
  after the program (else `--install-dir` names it), and it must not hold
  another installed program's folder (living inside one, like a Steam game,
  is fine). A plain `uninstall` whose uninstaller file is missing says so
  and points at `--force` instead of failing inside CreateProcess.
  `remove()` backs a folder up **on its own drive** (`<mount>\.cleam-backups`)
  when it is not on the backup's: a cross-drive `shutil.move` is copy+delete,
  which would fill C: with a D: game and could half-delete on a locked file.
- Still unbuilt from Revo: traced installation (a full registry/filesystem
  snapshot diff). Hunter mode's crosshair overlay needs a transparent always-on-top
  window with global mouse hooks — not reachable from a terminal; the
  substitute is picking from running processes.

## Platform facts worth not relearning

- `Checkpoint-Computer` exists only in Windows PowerShell 5.1 (`powershell.exe`),
  not pwsh 7. It needs admin and System Protection enabled, and it silently
  skips (exit 0) if a restore point exists from the last 24h —
  `-WarningAction Stop` turns that into a failure.
- `MsiExec /I{GUID}` in an `UninstallString` opens repair/modify; `/X` uninstalls.
- Registry entries with `SystemComponent=1` or a `ParentKeyName` are hidden
  components/patches, not apps.
- Ubuntu cloud images mark base packages (bash, base-files) as manually
  installed; `apps.parse_dpkg` drops required/important/essential ones.

## Security (2026-09-26): checks and explains (v2 below adds fixes)

Mon asked for malware and ransomware scanning. `security.py` deliberately
does not scan file contents: a home-made scanner misses what Defender catches
and flags what it should not, on the one screen where a wrong answer costs
files. Nothing in it deletes, quarantines or disables anything.

- **`status()`** is one PowerShell call returning JSON (Security Center's
  AntiVirusProduct, Get-MpComputerStatus, the Defender policy keys, CFA,
  shadow copies, firewall, UAC). `windows_checks()` is pure, so every branch
  is tested off Windows. Mon's desktop: Defender disabled by policy
  (`DisableAntiSpyware=1`, WinDefend Disabled, wscsvc Disabled), Malwarebytes
  service stopped, nothing registered in Security Center: **no real-time
  antivirus**. Cleam reports how to undo it; it never changes security
  settings itself.
- **`ransom_signs()`** reads names only in the personal folders: known note
  names, family-specific extensions (one file is enough), generic ones like
  `.encrypted` (20+), and 20+ documents renamed to one unknown extension
  (STOP/Djvu's `file.docx.mbtf`). Sidecar formats (Ableton `.asd`, Chrome
  `.crswap`, `.xmp`, `.part`…) are whitelisted because each writes one per
  file and would read as a mass rename. `.djvu` is a real document format,
  not a family marker.
- **`startup()`** lists Run/RunOnce keys, the Startup folders (Windows),
  autostart `.desktop` files (Linux) and LaunchAgents/Daemons (macOS), with
  Task Manager's on/off state from `StartupApproved` and Authenticode status
  from one batched `Get-AuthenticodeSignature`. On the dev desktop it found
  `IDMan.exe` with `HashMismatch` (modified after signing: a patched/cracked
  binary) and two entries pointing at programs that no longer exist.
- **`quick_scan()`** runs `MpCmdRun -Scan -ScanType 1`, only offered when
  Defender is the active antivirus; MpCmdRun fails while it is disabled.
- **Tasks and services (2026-09-28)** join `startup()` from one PowerShell
  call (`TASKS_AND_SERVICES`); `task_items()` / `service_items()` /
  `hide_microsoft()` filter in Python so they are tested off Windows. Tasks:
  all outside `\Microsoft\`, inside it only a program outside Windows/Program
  Files or one `judge()` flags (fake Windows tasks are a classic hiding
  place). Microsoft-signed root tasks stay: the Edge/OneDrive updaters are
  what people want off. Services: Auto only, svchost-hosted and
  Microsoft-signed dropped. Unquoted service paths with spaces are flagged.
- **Turning off:** tasks and services are per-item `Tweak`s
  (`startup-task-<path>`, `startup-service-<name>`) through
  `Debloater.apply`, so the journal is the undo; a service goes to Manual
  (3), not Disabled, so a program that needs it can still start it. "Turn
  back on" exists only with a journal entry: without one there is no exact
  state to restore. `StartupItem.protective` (AV/firewall/backup words,
  word-boundary regex so "reset" is not ESET) is never offered. CLI: `cleam
  startup [--off|--on ID] [--yes]`; the menu's System check has the same
  switches.
- **`program_of()` resolves a bare name on PATH** (`shutil.which`): Windows
  tasks like `BthUdTask.exe` and Run entries like `rundll32.exe x.dll,Entry`
  name no folder, and read as "does not exist" (a false WARN with a Turn off
  button on Windows' own task) before this.
- **Deeper persistence (2026-09-29), read-only:** Winlogon Shell/Userinit
  that are not Windows' own (plus any HKCU override), IFEO `Debugger` and
  `SilentProcessExit\MonitorProcess` (both registry views; Process Explorer
  replacing taskmgr.exe is the one legitimate case), `AppInit_DLLs` while
  `LoadAppInit_DLLs=1`, and WMI `CommandLine`/`ActiveScript` event consumers
  (read in the same PowerShell call; `enabled` = bound to a filter). Only what
  differs from a clean Windows is listed, so a clean PC shows none. No switch:
  a wrong Winlogon write locks everyone out; removal is the antivirus's job.
  `startup()` merges `judge()` into reasons already set, never replaces them.
- `security._powershell` forces UTF-8 output: the default OEM code page read
  as ANSI raised UnicodeDecodeError on a service name like "für".

## Disk Cleanup handlers (`wincleanup.py`)

Old driver packages must be unregistered from the driver store, not deleted,
and Windows.old carries TrustedInstaller ACLs, so those two targets drive
cleanmgr's own handlers: `StateFlags0619 = 2` on the named handlers only
(the slot is cleared on every handler first), `cleanmgr /sagerun:619`, then
the values are removed again, with the exit code returned after `finally`.
The driver target's scan size comes from `Get-WindowsDriver -Online` (property
names are not localized, unlike `pnputil`'s output): every package with a
newer version of the same INF/provider/class. It is an upper bound, since
Windows keeps any a device still uses; the clean's result is measured.
Mon's "driver management 2.4 GB" in Disk Cleanup was this handler; 509 MB
remained afterwards.

## Debloat (`debloat.py`) and the terminal menu (`tui.py`), 2026-09-26

Mon asked for an ASCII UI with checkboxes and deep Windows debloat.

- **The journal is the undo.** `Debloater.apply` saves each value's previous
  state (absent, or kind+data, and whether its key existed) *before* writing,
  and a re-apply never overwrites the first record. Undo restores that, not
  a default -- the .reg undo files of Win11Debloat restore defaults, which is
  wrong on any machine with its own setting. A key Cleam created is deleted
  on undo: the Win11 classic context menu is switched on by the
  `{86ca1aa0...}\InprocServer32` key existing at all.
- **Services are registry values** (`Services\<name>\Start`, `if_key_exists`),
  so they share the journal; a missing service is never created. Tasks are
  read as the TaskState enum's number through PowerShell (schtasks text is
  localized). Apps: `Remove-AppxPackage` for the current user only, undo with
  `Add-AppxPackage -RegisterByFamilyName`.
- **Edition truth, from Microsoft's Policy CSP pages:**
  AllowWindowsConsumerFeatures, AllowWindowsTips and AllowWindowsSpotlight
  are "❌ Pro"; AllowTelemetry=0 is Enterprise/Education/Server only;
  TurnOffWindowsCopilot is deprecated and does not control the Copilot app
  (the April 2026 RemoveMicrosoftCopilotApp policy / removing the app does).
  WinUtil's telemetry tweak runs `Set-Service wermgr`, which is not a service.
- **Mon's desktop was already debloated** (almost every tweak "applied"
  before Cleam touched it), so `cleam debloat list` there is mostly greyed out.
- **Known limit:** HKCU tweaks go to the account Cleam runs as. Elevating a
  standard user with an admin's password writes the admin's HKCU.
- **TUI:** stdlib only (msvcrt / termios + ANSI, alternate screen, one write
  per frame, `clip()` counts visible characters so a long line never wraps
  and shifts the frame). Bare `cleam` opens it when stdin/stdout are a TTY.
  **Symbols (measured, not assumed):** a PowerShell GlyphTypeface check of
  consola.ttf and lucon.ttf found box drawing, blocks, ╔╗╚╝║═ and
  ■ √ · × ○ ♦ ¤ ≈ ∞ ▬ ± ♣ ▼ ░ ◘ ◄ ↑, but no ✓ ✗ ☐ ☑ ⚠ ⚙ ★. The classic
  console has no font fallback, so those print as boxes; Windows Terminal
  (WT_SESSION) falls back and gets them. `GLYPH_SETS` rich/console/ascii,
  `CLEAM_GLYPHS` overrides; a test pins the console set to the measured
  list. No emoji: two cells wide. Bold black on the cyan accent renders
  grey in the classic console, so accent backgrounds are never bold.
  Mon's desktop has no Cascadia font (Windows 10).
  **Actions:** reserved storage and hibernation are Windows commands, not
  values: `ACTIONS` objects with applied()/apply()/undo(); the journal keeps
  `action_was_applied`, and undo only reverses what Cleam changed.
  Reserved storage was ~7.1 GB on Mon's C: (fsutil storagereserve query).
  **Sync (2026-09-27):** Debloat rows start as the live state (Row.was ==
  checked); checklist() returns only rows whose box changed; ticked =
  apply, unticked = `Debloater.revert()`: the journal's exact undo when
  Cleam made it, else Windows' default (`Reg.original`, None = delete the
  value; services carry their shipped Start type). The list is re-read after
  every run.
  **Actions v2:** snapshot() -> journal, undo(snapshot), reset() to default.
  RegistryField edits one part of a shared value: DirectXUserGlobalSettings
  ("SwapEffectUpgradeEnable=1;VRROptimizeEnable=0;...", shared with VRR and
  Auto HDR) and StickyKeys Flags (bit 0x4 = hotkey; the common "506" also
  clears the Sticky-Keys-on bit for people who use it). Mon's flags were 498.
  **Gaming evidence:** in -- Game Mode, windowed-games flip model (Microsoft
  support), HAGS (DirectX blog; DLSS FG needs it), mouse acceleration off,
  performance power plan (GUIDs, not names, from powercfg), VMP off
  (Microsoft's gaming guide). Out -- NetworkThrottlingIndex,
  SystemResponsiveness, Win32PrioritySeparation, HPET/SysMain disabling,
  timer resolution: no reproducible gain, documented DPC/audio regressions.
  Memory integrity off is shown as an INFO trade-off, never offered as a
  tweak (a cleaner does not lower security).
  **System check (security.status):** one PowerShell pass, ~4.5 s, 21 checks
  in CHECK_GROUPS. Mon's desktop on 2026-09-27: last update 2026-04-29 (151
  days, BAD), Windows 10 on ESU (until 12 Oct 2027 per microsoft.com),
  Secure Boot off (Battlefield 6 / Valorant-on-11 refuse), HPET and the
  RZ616 Wi-Fi in error in Device Manager, disks healthy, no crashes.
  **Mouse:** Windows input is read with ReadConsoleInputW (msvcrt cannot
  see mouse events), with ENABLE_MOUSE_INPUT on and Quick Edit off (it eats
  clicks for text selection); the old input mode is restored on exit. Mouse
  Y is a buffer coordinate, so the window's top row is subtracted. Every
  frame records the spans it drew (rows, group titles, `[ buttons ]`) and a
  click is hit-tested against them; POSIX uses xterm SGR mouse reporting.
  Long jobs draw a progress bar in the menu; their children all run with
  NO_WINDOW, so no other window appears.
  To drive it in tests on Windows, SendKeys does *not* work for arrows
  (conhost drops arrow events without scan code + ENHANCED_KEY); inject
  KEY_EVENT records with WriteConsoleInputW instead -- they arrive as
  `'\xe0' 'P'` exactly like a keyboard.

## Menu v2 (2026-09-27): live, pre-read, Basic/Advanced

Mon tested v0.1.3's menu and asked for hover, live changes, no waiting,
dropdown system check, Basic/Advanced, and a deeper catalogue than
Optimizer 16.7 and Stix Tweaker.

- **The "ticked but unticked after reopen" bug was not a registry bug:**
  there was no journal at all, i.e. nothing was ever applied -- the old
  list only applied after a Review step. Plus `device-companion-apps`
  checked only the policy while Settings writes
  `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Device Metadata`; both
  are now in the tweak (it reads PARTLY on Mon's PC until applied).
- **Live mode** (`checklist(..., live=DebloatLive)`): a tick calls
  `apply`/`revert` at once, then `engine.state()` reads Windows back and
  the row shows that ("not kept" when a policy puts it back). App removal
  keeps one confirm (it deletes the app's data). Undo is live too. Clean
  junk stays tick-then-confirm: it deletes files.
- **Pre-read:** `Background` starts daemon-thread `Job`s for debloat, the
  system check and the junk scan when the menu opens (daemon: quitting never
  waits); the menu shows "reading..." / "ready" and refreshes itself via
  `term.key(timeout)` (WaitForSingleObject / select). Debloat updates
  `data["states"]` in place instead of re-reading. `states()` went from
  6.8 s to 1.7 s: one `Get-ScheduledTask` for every task instead of one per
  path, and action probes (DISM, optional features) in parallel threads.
- **Hover:** Windows MOUSE_MOVED (flags 1) and xterm mode 1003 give `Hover`;
  `Terminal.draw` skips a frame identical to the last, so mouse moves cost
  nothing. **Conhost trap:** after writing the last column the cursor is
  wrap-pending and `ESC[K` erases that column -- every panel's right border
  was missing; `ESC[K` is now only sent on short lines.
- **Detail panel:** `debloat.tech(t)` spells out registry path, value and
  type, service start type (with the default), task, or the action's
  command (`Action.tech`); `?` switches to the plain `about` text.
- **Tiers:** `tier()` -- advanced if moderate, in `ADVANCED_GROUPS`, an app
  that is not default-ticked, or `level=ADVANCED` (app-specific telemetry).
- **From Optimizer/Stix (read as .NET string tables, never run):** taken --
  Edge StartupBoost/BackgroundMode policies, LetAppsRunInBackground=2,
  ExcludeWUDriversInQualityUpdate, WSearch, Xbox Live services (not
  XboxGipSvc: controller accessories), MenuShowDelay, transparency, Game Bar
  on the guide button, LLMNR, NoDriveTypeAutoRun/NoAutorun, SMB1Protocol and
  MicrosoftWindowsPowerShellV2Root (OptionalFeature actions), Remote
  Assistance, VerboseStatus, CrashControl DisplayParameters, LongPaths,
  Office SendTelemetry=3, VS SQM OptIn=0, Firefox policies, NVIDIA NvTmRep
  tasks (absent with the new NVIDIA App), .NET/PowerShell opt-out env vars.
  Rejected -- Stix disables wuauserv/UsoSvc/BITS/WaaSMedic and sets
  DisableWindowsUpdateAccess, turns VBS/HVCI off, disables HPET and a dozen
  system devices, sets DisabledComponents=255 (Microsoft: use 0x20),
  disables Spooler/SysMain/memory compression, and ships timer/MMCSS/AFD
  values with no reproducible gain; Chrome's MetricsReportingEnabled only
  applies on managed (domain/Azure AD/CBCM) Windows machines.
- **menu.ps1** skips the GitHub API check (1.4 s in Windows PowerShell) when
  the cached copy was checked in the last 6 hours; the check has a 5 s cap.
- Verified in a real (minimized) console by injecting MOUSE_EVENT records
  with WriteConsoleInputW and reading the screen back with
  ReadConsoleOutputCharacterW -- text, no screenshots.

## System check v2 (2026-09-27): it fixes, and it checks the gaming set-up

Mon asked for the system check to fix, not just display. That reverses the
2026-09-26 "never changes security settings" stance, at Mon's request; the
rules now: a fix only runs when clicked (or `cleam security --fix ID --yes`),
its confirm lists the exact change (`Fix.tech`), registry/service fixes go
through `Debloater.apply` (journal first, Undo debloat reverses; a Reg with
`value=None` means "delete this value"), and nothing lowers protection.

- **Found on Mon's desktop, invisible to v1:** Windows Update blocked four
  ways -- `WUServer=localserver.localdomain.wsus` + `UseWUServer=1` (a fake
  WSUS), `DisableWindowsUpdateAccess`, `NoAutoUpdate`,
  `DoNotConnectToWindowsUpdateInternetLocations`, and wuauserv / WaaSMedicSvc
  / DoSvc Disabled -- which is why the last update was 151 days old. A WSUS
  server is only called fake when its name does not resolve (a company's real
  one is not ours to remove). Also: SmartScreen off by policy, TRIM off
  (`NTFS DisableDeleteNotify = 1`), boot values `tscsyncpolicy Enhanced`,
  `disabledynamictick Yes` and three APIC/PCI ones, the second monitor at
  59 Hz though it offers 144 Hz (verified with ChangeDisplaySettingsEx
  CDS_TEST, not applied), HPET and the Wi-Fi card disabled (problem code 22),
  D: 4 % free. Fine: EXPO on (DDR5-5600 over rated 4800), dual channel, RTX
  4070 SUPER at x16 with Resizable BAR (BAR1 16 GiB), driver and BIOS recent.
- **Parsing without translated labels:** fsutil by `DisableDeleteNotify = N`,
  bcdedit by value names, powercfg PROCTHROTTLEMAX by the position of its hex
  values (min, max, increment, AC, DC).
- **Win32_PhysicalMemory.Speed is the JEDEC/SPD speed**, not the XMP one:
  configured > rated means XMP/EXPO is on; configured == a JEDEC top speed
  (DDR4 2666, DDR5 4800) is reported as INFO "if the kit is sold faster".
- **Monitors:** EnumDisplaySettings over the modes at the current resolution
  and depth (interlaced excluded), not Win32_VideoController.MaxRefreshRate.
- **Hosts:** Mon's has 949 lines (Adobe blocks); only Microsoft
  update/Defender/SmartScreen names count. The fix comments lines out after a
  one-time `hosts.cleam-backup`.
- **Startup:** "Turn off at startup" writes Task Manager's own
  StartupApproved value (03 + FILETIME), so Task Manager can turn it back on.
- More debloat from this round: VRR for windowed games (VRROptimizeEnable in
  the shared DirectX string), animations, NTFS last-access
  (0x80000001, default 0x80000002), Start recommendations / account
  notifications (Win11), recent files, Edge sidebar + Copilot button
  policies, Defender PUA blocking, LSA protection as RunAsPPL=2 (1 would add
  a UEFI lock that cannot be undone from Windows), WDigest pinned off.

## AI: advisory only, and not built yet

Mon asked about AI deciding junk vs important (2026-09-18). The answer stayed
"rules decide, AI explains", for reasons worth keeping:

- Location already settles ~95% of it, offline and free. A model adds nothing
  there and is wrong sometimes, on an irreversible action.
- Paths are personal data (`~/Documents/kku-thesis-final.docx`). A local model
  that would keep them private is a 1-2 GB download inside an app that sells
  itself on freeing disk space.
- Duplicate and large-file finding is hashing and sorting, not AI.

If it gets built: metadata only (path, size, age, extension, owning package),
never file contents; off by default; every suggestion carries a reason; it can
only propose things inside roots Cleam already scans; and it can never tick a
checkbox by itself.

## Open decisions

- Code signing — unsigned PyInstaller binaries get flagged by SmartScreen/AV.
  Costs in `docs/platforms.md`.
- Command-based targets exist now (`mode="command"`); `journalctl
  --vacuum-size` and `docker system prune` are not targets yet — both need
  root and the second deletes images someone may want.
- Whether the Programs list should hide library-ish apt packages (`acl`,
  `debhelper` still show; only essential/required/important are dropped).
