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
  junk.py       targets per OS + the scan/delete walk (the dangerous part)
  apps.py       installed programs; uninstall = run the platform's own uninstaller
  snapshot.py   Windows restore points, Timeshift/Snapper, tmutil
  system.py     OS detection, is_admin(), sudo(), output()
  cli.py        argparse; entry point `cleam`
  gui.py        Flet window; entry point `cleam-gui`
packaging/entry.py  PyInstaller entry (__main__.py's relative import breaks it)
```

Test: `PYTHONPATH=src python3 -m unittest discover -s tests`

## GUI

`gui.py` decides nothing. It calls `junk.run()`, `apps.list_apps()` and
`snapshot.run()` exactly as the CLI does, so the two front ends cannot disagree
about what a clean will delete. Keep new behaviour in the core, not in a tab.

- **Two calling modes, because the GUI has no terminal.** `snapshot.run()` and
  `apps.uninstall()` take `capture`: the CLI leaves stdio attached so a sudo
  prompt and apt's y/n reach the user, while the GUI captures output, passes
  `-y` up front, and uses `sudo -n` so an unanswerable password prompt fails
  instead of hanging the window.
- **Long work goes through `page.run_thread()`.** A scan of `~/.cache` takes
  seconds and freezes the window otherwise.
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
cannot run there — only CI (`.github/workflows/ci.yml`) executes them. Say so
when reporting a change to them as done.

## Safety invariants — do not weaken

Cleam deletes files. Each rule below exists because the naive version destroys
something:

- **Dry run unless `--yes`.** `uninstall` prompts unless `--yes`.
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
- Command-based targets (`uv cache prune`, `npm cache clean`,
  `journalctl --vacuum-size`) — the right way to shrink structured caches.
- Whether the Programs list should hide library-ish apt packages (`acl`,
  `debhelper` still show; only essential/required/important are dropped).
