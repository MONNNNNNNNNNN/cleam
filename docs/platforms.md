# What Cleam can do on each platform

## Tested on (2026-09-18)

`tests/platform_smoke.py` runs the installed command and the real platform —
overview, scan, program list, dry-run clean, and a leftovers backup/restore
round-trip in a sandboxed HOME. `.github/workflows/platform-tests.yml` runs it
on each OS.

| Platform | How it was tested | Result |
|---|---|---|
| Linux | Ubuntu 24.04 (dev box) and ubuntu-latest | 20/20 checks |
| Windows | windows-latest, Windows Server 2025 24H2 (build 26100.33296) | 23/23 checks, including the registry key exported, deleted, imported back, value intact |
| macOS | macos-latest | 20/20 checks |
| Windows installer | Built with Inno Setup, installed with `/VERYSILENT`, the installed `cleam.exe` run, then uninstalled silently | Installs to `%LOCALAPPDATA%\Programs\Cleam`, runs, uninstalls leaving nothing |
| Android | `flet build apk` on ubuntu-latest | Builds: a 60 MiB APK. Never run on a device, and the feature table below is why that matters |
| iOS | `flet build ipa` on macos-latest | Builds a `.xcarchive`; **no `.ipa`** — Xcode exports one only for a signed app ($99/year Apple Developer Program) |

The GUI itself is verified separately by driving it headlessly; see
`docs/ux-test-plan.md`.

The three features — clean junk, uninstall programs, take a restore point or
snapshot — map very differently onto each OS. On the desktop they are all
possible. On mobile the OS sandbox forbids most of them, and no framework
choice changes that.

| Feature | Windows | Linux | macOS | Android | iOS |
|---|---|---|---|---|---|
| Temp / cache files | Yes | Yes | Yes (some caches need Full Disk Access) | Own cache only; other apps' caches are off-limits since Android 11 | Own sandbox only |
| Empty trash | Recycle Bin | freedesktop Trash | `~/.Trash` (needs Full Disk Access) | n/a | n/a |
| Find large / duplicate files | Planned | Planned | Planned | Shared storage, with `MANAGE_EXTERNAL_STORAGE` | Photos only, via PhotoKit with user permission |
| List installed programs | Registry `Uninstall` keys | apt (manual, non-base), snap, flatpak | `/Applications`, `~/Applications` | Yes (`QUERY_ALL_PACKAGES` is restricted on Play) | No |
| Uninstall | Program's own `UninstallString` | `apt-get remove` / `snap remove` / `flatpak uninstall` | Move bundle to Trash via Finder | One app at a time, each confirmed by the user | No |
| Leftovers after an uninstall | `%APPDATA%`, `%LOCALAPPDATA%`, `%PROGRAMDATA%`, Program Files, Start Menu, `HKCU`/`HKLM` Software + WOW6432Node | `~/.config`, `~/.local/share`, `~/.cache`, `/etc`, `/opt`, plus apt packages removed but not purged | `~/Library/{Application Support,Caches,Logs,Preferences,LaunchAgents}` | Own sandbox only | No |
| Restore point / snapshot | `Checkpoint-Computer` (admin, System Protection on, max one per 24h) | Timeshift or Snapper | `tmutil localsnapshot` (APFS) | No | No |

## Mobile, honestly

- **Android** can be a real but smaller app: storage analyzer (large files,
  duplicate media, old APKs in Downloads, empty folders), a guided uninstaller
  (it asks the system; the user confirms each app), and a shortcut to the
  system's own "Free up space" screen. It cannot silently clear other apps'
  caches or take a system snapshot. Google Play charges $25 once and restricts
  `MANAGE_EXTERNAL_STORAGE`; a sideloaded APK has no such restriction and costs
  nothing.
- **iOS** allows almost none of it. An app cannot see other apps, their files,
  or their caches, and cannot uninstall anything. The honest iOS app is a photo
  and video cleaner (duplicates, screenshots, large videos). The App Store needs
  the $99/year Apple Developer Program.

## Distribution

| Form | How | Status |
|---|---|---|
| Command line | `pip install .` then `cleam …`; scriptable with `--json` and exit codes | Done |
| GUI | Flet (Flutter), `pip install '.[gui]'` then `cleam-gui` | Done — Clean, Programs, Snapshots tabs |
| Portable binary | PyInstaller for the CLI, `flet pack` for the window, per OS (`.github/workflows/release.yml`) | Built in CI: CLI 8 MiB, window 61 MiB on Windows |
| Installer `.exe` | Inno Setup around both binaries (`packaging/cleam.iss`) | Built in CI. `PrivilegesRequired=lowest`, so a standard account installs into its own profile; PATH is an unchecked opt-in |
| Android / iOS | `flet build apk` / `flet build ipa` — needs the Flutter SDK, never run here | Not started, and the feature set shrinks to the table above |

Unsigned Windows binaries trigger SmartScreen, and a one-file PyInstaller
build that deletes files is exactly what antivirus heuristics flag. Signing
options: SignPath Foundation (free for open-source projects), Azure Trusted
Signing (about $10/month), or a classic OV certificate (about $200+/year).
macOS binaries need Apple notarization ($99/year) or users must right-click →
Open past Gatekeeper.
