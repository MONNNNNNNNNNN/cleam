# What Cleam can do on each platform

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
| Portable binary | PyInstaller one-file build per OS, from CI (`.github/workflows/ci.yml`) | CI job written, not yet run |
| Installer `.exe` | Inno Setup (free) around the portable build, or the GUI toolkit's own bundler | Not started; decide with the GUI |
| GUI | Not chosen | Open decision |

Unsigned Windows binaries trigger SmartScreen, and a one-file PyInstaller
build that deletes files is exactly what antivirus heuristics flag. Signing
options: SignPath Foundation (free for open-source projects), Azure Trusted
Signing (about $10/month), or a classic OV certificate (about $200+/year).
macOS binaries need Apple notarization ($99/year) or users must right-click →
Open past Gatekeeper.
