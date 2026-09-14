# Cleam

Junk cleaner, uninstaller, and restore-point/snapshot tool. The name is
**Cleam** — not a typo for "clean". Target platforms: Windows, Linux, macOS,
Android, iOS. What each can actually support is in `docs/platforms.md`; read it
before promising a mobile feature — the OS sandbox forbids most of them.

## Current state

Command-line core only, in Python, **stdlib only** (no runtime dependencies —
keeps the portable binary small and the supply chain empty). Nothing chosen for
the GUI yet; the user deferred that decision. Wanted distribution forms:
installer `.exe`, portable binary, and command-line scripts.

```
src/cleam/
  junk.py       targets per OS + the scan/delete walk (the dangerous part)
  apps.py       installed programs; uninstall = run the platform's own uninstaller
  snapshot.py   Windows restore points, Timeshift/Snapper, tmutil
  system.py     OS detection, is_admin(), sudo(), output()
  cli.py        argparse; entry point `cleam`
packaging/entry.py  PyInstaller entry (__main__.py's relative import breaks it)
```

Test: `PYTHONPATH=src python3 -m unittest discover -s tests`

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

## Open decisions

- GUI toolkit (drives the installer choice too).
- Code signing — unsigned PyInstaller binaries get flagged by SmartScreen/AV.
  Costs in `docs/platforms.md`.
- Command-based targets (`uv cache prune`, `npm cache clean`,
  `journalctl --vacuum-size`) — the right way to shrink structured caches.
