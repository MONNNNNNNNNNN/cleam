# Cleam

Junk cleaner, uninstaller, and restore-point/snapshot tool. The name is
**Cleam** — not a typo for "clean". Target platforms: Windows, Linux, macOS,
Android, iOS. What each can actually support is in `docs/platforms.md`; read it
before promising a mobile feature — the OS sandbox forbids most of them.

## Current state

CLI plus a Flet window. The core is **stdlib only** (no runtime dependencies —
keeps the portable binary small and the supply chain empty); Flet is an extra,
`pip install '.[gui]'`. Wanted distribution forms: installer `.exe`, portable
binary, and command-line scripts. The installer is not started.

```
src/cleam/
  overview.py   OS/build, disk usage, big-folder sizes with "is this normal" notes
  junk.py       targets per OS + the scan/delete walk (the dangerous part)
  apps.py       installed programs; uninstall = run the platform's own uninstaller
  leftovers.py  remnants an uninstaller left; move-to-backup + restore
  snapshot.py   Windows restore points, Timeshift/Snapper, tmutil
  system.py     OS detection, is_admin(), sudo(), output()
  cli.py        argparse; entry point `cleam`
  gui.py        Flet window; entry point `cleam-gui`
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
  `measure()` is the walk. The GUI paints the rows, then fills them one at a
  time, because `C:\Windows` and `AppData` are hundreds of thousands of files.
- `/proc/mounts` is mostly squashfs snaps, tmpfs and overlays;
  `parse_mounts()` keeps real `/dev/*` devices only.

## GUI

`docs/ux-test-plan.md` holds the personas, scenarios and test cases, with what
was verified on screen versus traced in code. Re-run it after touching
`gui.py`; several of these defects produced no exception and no console error,
so only a screenshot caught them.

`gui.py` decides nothing. It calls `junk.run()`, `apps.list_apps()` and
`snapshot.run()` exactly as the CLI does, so the two front ends cannot disagree
about what a clean will delete. Keep new behaviour in the core, not in a tab.

- **Two calling modes, because the GUI has no terminal.** `snapshot.run()` and
  `apps.uninstall()` take `capture`: the CLI leaves stdio attached so a sudo
  prompt and apt's y/n reach the user, while the GUI captures output, passes
  `-y` up front, and uses `sudo -n` so an unanswerable password prompt fails
  instead of hanging the window.
- **Long work goes through `_worker()`, never `page.run_thread()` directly.**
  `run_thread` uses a multi-worker `ThreadPoolExecutor` (`flet/app.py`), so two
  clicks on Refresh really do run in parallel. Two jobs rebuilding one control
  list corrupted it: reconciling rows by object identity
  (`controls.index(placeholder)`) raised `ValueError: ... is not in list` and
  left the row stuck on "measuring…" with nothing shown to the user. Rows are
  now replaced by index, and `_worker` refuses a second job per panel.
- **A wrapping `ft.Row` cannot hold an `expand=True` child.** Flutter renders
  the entire panel as a featureless grey block — no exception, no console
  error. `_buttons()` wraps fixed-width controls; `_field_row()` is for rows
  containing a text field.
- **Nothing destructive is pre-selected.** `junk.Target.opt_in` marks the
  Recycle Bin / Trash, package-manager caches (a re-download) and shader
  caches (a stutter), which the GUI never ticks for the user, and
  "Clean selected" is disabled until a scan returns something.
- **Layout (2026-09-26 redesign):** a `NavigationRail` and one page at a time
  in `body.content`. Pages are one scrolling `Column` with
  `horizontal_alignment=STRETCH` (without it cards shrink to their content);
  Programs is a `ListView`. Every target carries `group` and `about`; the Clean
  page renders sections from `GROUPS` in gui.py, so a new group needs an entry
  there or its targets vanish. Opening Clean runs `preview()` (cheap:
  `junk.present()` only), a scan fills rows one by one.
- **A section checkbox is tristate only while mixed.** With `tristate=True`
  and value `False` (Flet's default, so never sent to Flutter) it rendered the
  mixed dash on an empty section. Its click is decided from the rows, not from
  the value Flutter cycles to.
- **Ticking a row must not rebuild the rows** (`on_tick` → `totals()` only):
  a rebuild takes keyboard focus off the box that was just toggled.
- **`_worker` claims the panel before starting the thread.** Set inside the
  thread, two clicks in quick succession both saw an idle panel.
- **Driving it from Claude in Chrome (Windows box):** clicks work, mouse-wheel
  and keyboard scrolling do not reach the Flutter canvas (a 3-line Flet probe
  failed the same way), and the canvas draws at 80% of the viewport in
  screenshots — divide screenshot coordinates accordingly. The first load
  takes ~20 s. `resize_window` has no effect on a maximized window.
- **A checkbox's label is the target name** (`ListTile(title=checkbox)`), not a
  separate `title`: a screen reader announcing "checkbox, unchecked" with the
  name elsewhere tells a blind user nothing about what is about to be deleted.
- **Flet 1.0 API, which differs from every 0.x tutorial:** `ft.run(main)` (no
  `ft.app`), `ft.Button` (no `ElevatedButton`), tabs are
  `ft.Tabs(length=N, content=Column([TabBar(tabs=[...]), TabBarView(controls=[...])]))`,
  and dialogs/snackbars go through `page.show_dialog()` / `page.pop_dialog()`
  (`SnackBar` is a `DialogControl` too). Check with `dataclasses.fields()`
  against the installed version rather than trusting a tutorial.
- **How to see it from this Linux box:** run it in web mode
  (`ft.run(main, view=ft.AppView.WEB_BROWSER, port=8951)`) and drive it with
  the cached Playwright Chromium. Flutter paints to a canvas, so clicks are by
  coordinate, not selector. Use a **fresh browser context** per run: Flet
  restores the previous session on reload, dialog included, which silently
  swallows later clicks.
- CI only import-checks `gui.py` (`pip install -e '.[gui]'`), which is how a
  Flet rename would show up. `flet build` for `.apk`/`.ipa`/`.exe` needs the
  Flutter SDK and has never been run here.

Development happens on a Linux ARM box. The Windows and macOS code paths
cannot run there — only CI executes them. Say so when reporting a change to
them as done.

`tests/platform_smoke.py` is the end-to-end check that the unit tests cannot
be: it calls the installed command and the real platform (the real registry,
the real `/Applications`), with a leftovers round-trip inside a sandboxed
HOME. `.github/workflows/platform-tests.yml` runs it on all three OSes, builds
and silently installs/uninstalls the Windows installer, and attempts the
mobile builds. Run it after touching anything platform-specific — the unit
tests mock the platform away, which is why they stay green while a Windows
path is broken.

Traps that cost a CI round each, all in the harness rather than in Cleam:
`flet pack` clears `dist/` (give each build its own `--distpath`);
`flet build apk/ipa` prompts about its Flutter SDK and a prompt in CI is an
`EOFError` (pass `--yes`); and PowerShell returns a bare string when
`Where-Object` matches once, so `$found[0]` is the first character — wrap it
in `@()`.

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
  uninstaller, so the app's own cleanup happens.

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
- Still unbuilt from Revo: traced installation (a full registry/filesystem
  snapshot diff), forced uninstall for broken uninstallers, and an autorun
  manager. Hunter mode's crosshair overlay needs a transparent always-on-top
  window with global mouse hooks — not reachable in Flet; the substitute is
  picking from running processes.

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

## Security (2026-09-26): checks and explains, never removes

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
- Not built: scheduled tasks and services in the startup list, and turning
  entries off (a persistent change that needs an undo first).

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

- Installer format (Inno Setup around the portable build, or `flet build`).
- Code signing — unsigned PyInstaller binaries get flagged by SmartScreen/AV.
  Costs in `docs/platforms.md`.
- Command-based targets exist now (`mode="command"`); `journalctl
  --vacuum-size` and `docker system prune` are not targets yet — both need
  root and the second deletes images someone may want.
- Whether the Programs list should hide library-ish apt packages (`acl`,
  `debhelper` still show; only essential/required/important are dropped).
