"""cleam command line. Every destructive command is a dry run or a prompt unless --yes."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from . import __version__, apps, debloat, junk, leftovers, overview, repair, security, snapshot
from .system import OS, human


def cmd_overview(args) -> int:
    if args.json:
        print(
            json.dumps(
                {
                    "os": overview.os_name(),
                    "disks": [asdict(d) for d in overview.disks()],
                    "folders": [asdict(overview.measure(f)) for f in overview.folders()],
                },
                indent=2,
                default=str,
            )
        )
        return 0
    print(f"{overview.os_name()}\n")
    print(f"{'Disk':<26}{'Size':>10}{'Used':>10}{'Free':>10}")
    for d in overview.disks():
        print(f"{d.mount:<26}{human(d.total):>10}{human(d.used):>10}{human(d.free):>10}  {d.percent_used}% used")
    print(f"\n{'Folder':<22}{'Files':>9}{'Size':>15}")
    for f in overview.folders():  # measured one at a time: each is a full walk
        overview.measure(f)
        print(f"{f.label:<22}{f.files:>9}{f.size_label:>15}")
        if f.denied and f.files:
            print(f"  {f.denied} folders could not be read, so the real total is larger")
        if f.note:
            print(f"  {f.note}")
    return 0


def cmd_biggest(args) -> int:
    rows = overview.biggest(args.path, args.top)
    if args.json:
        print(json.dumps([asdict(f) for f in rows], indent=2, default=str))
        return 0
    print(f"{'Name':<40}{'Files':>9}{'Size':>15}")
    for f in rows:
        print(f"{f.label[:38]:<40}{f.files:>9}{f.size_label:>15}")
    return 0


def _selected(only: str | None, opt_in: bool = True) -> list[junk.Target]:
    """Targets named by --only, else all of them; opt_in=False leaves out the
    opt-in ones (the bin, package caches) unless they are named."""
    all_targets = junk.targets()
    if not only:
        return [t for t in all_targets if opt_in or not t.opt_in]
    wanted = set(only.split(","))
    unknown = wanted - {t.id for t in all_targets}
    if unknown:
        ids = ", ".join(t.id for t in all_targets)
        raise SystemExit(f"cleam: unknown target {', '.join(sorted(unknown))} (have: {ids})")
    return [t for t in all_targets if t.id in wanted]


def _report(results: list[junk.Result], as_json: bool) -> None:
    if as_json:
        print(json.dumps([asdict(r) for r in results], indent=2))
        return
    print(f"{'Target':<24}{'Files':>8}{'Size':>12}  Note")
    for r in results:
        if r.skipped:
            print(f"{r.id:<24}{'-':>8}{'-':>12}  {r.skipped}")
        else:
            note = f"{r.errors} in use or denied" if r.errors else ""
            print(f"{r.id:<24}{r.files:>8}{human(r.bytes):>12}  {note}")
    print(f"{'Total':<24}{sum(r.files for r in results):>8}{human(sum(r.bytes for r in results)):>12}")


def cmd_targets(args) -> int:
    found = junk.targets()
    if args.json:
        print(json.dumps([asdict(t) for t in found], indent=2, default=str))
        return 0
    group = ""
    for t in found:
        if t.group != group:
            group = t.group
            print(f"\n{group}")
        flags = ", ".join(f for f, on in (("admin", t.admin), ("opt-in", t.opt_in)) if on)
        print(f"  {t.id:<24}{t.label}" + (f"  [{flags}]" if flags else ""))
        print(f"  {'':<24}{t.about}")
    return 0


def cmd_scan(args) -> int:
    _report([junk.run(t) for t in _selected(args.only)], args.json)
    return 0


def cmd_clean(args) -> int:
    # A bare `clean --yes` (a cron job) must not empty the Recycle Bin or wipe
    # every package cache daily: the opt-in targets need --only or --all.
    targets = _selected(args.only, opt_in=args.all)
    if not args.yes:
        _report([junk.run(t) for t in targets], args.json)
        if not args.json:
            print("\nDry run: nothing deleted. Re-run with --yes to delete.")
        return 0
    if args.snapshot and not _snapshot("Cleam: before clean"):
        print("cleam: snapshot failed, nothing deleted", file=sys.stderr)
        return 1
    results = [junk.run(t, delete=True) for t in targets]
    _report(results, args.json)
    return 1 if any(r.errors for r in results) else 0


def _snapshot(description: str) -> bool:
    code, text = snapshot.create(description)
    if text:
        print(f"cleam: {text}", file=sys.stderr if code else sys.stdout)
    return code == 0


def cmd_apps(args) -> int:
    found = [a for a in apps.list_apps() if not args.filter or args.filter.lower() in a.name.lower()]
    if args.json:
        print(json.dumps([asdict(a) for a in found], indent=2))
    else:
        for a in found:
            print(f"{a.name[:40]:<42}{a.version[:18]:<20}{a.source:<10}{a.id}")
    return 0


def cmd_uninstall(args) -> int:
    installed = apps.list_apps()
    matches = [a for a in installed if a.id == args.app] or [
        a for a in installed if a.name.lower() == args.app.lower()
    ]
    if args.force and not matches and args.install_dir:
        # Not listed at all (its entry is gone too): the folder the user named is all there is.
        matches = [apps.App(args.app, args.app, "", "forced", [])]
    if len(matches) != 1:
        print(f"cleam: {len(matches)} apps match {args.app!r}; use the id from `cleam apps`", file=sys.stderr)
        for a in matches:
            print(f"  {a.id}  ({a.name} {a.version})", file=sys.stderr)
        return 2
    app = matches[0]
    if args.force:
        return _force_uninstall(app, installed, args)
    if apps.uninstaller_missing(app):
        print(f"cleam: {app.name}'s uninstaller is missing ({apps.program_in(app.command)}).\n"
              f"  `cleam uninstall {app.id} --force` moves the program to a backup instead", file=sys.stderr)
        return 1
    shown = app.command if isinstance(app.command, str) else " ".join(app.command)
    print(f"Uninstall {app.name} {app.version} ({app.source})\n  {shown}")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        return 1
    if args.snapshot and not _snapshot(f"Cleam: before uninstalling {app.name}"):
        print("cleam: snapshot failed, nothing uninstalled", file=sys.stderr)
        return 1
    code, text = apps.uninstall(app)
    if text:
        print(text)
    return code


def _force_uninstall(app, installed, args) -> int:
    """The uninstaller is gone or broken: move the program's folder, its entry
    and its high-confidence leftovers into one backup that `cleam restore` undoes."""
    others = [a for a in installed if a is not app]
    items, why = leftovers.forced(
        app.name, args.install_dir or app.install_dir, app.key,
        other_names=tuple(a.name for a in others),
        other_dirs=tuple(a.install_dir for a in others if a.install_dir),
        explicit=bool(args.install_dir),
    )
    if why:
        print(f"cleam: {why}", file=sys.stderr)
        return 1
    chosen = [i for i in items if i.confidence == "high"]
    print(f"Forced uninstall of {app.name}: its own uninstaller is not run. These move to a backup:")
    for item in chosen:
        print(f"  {item.kind:<10}{human(item.bytes):>11}  {item.target}")
    if len(items) > len(chosen):
        print(f"  ({len(items) - len(chosen)} uncertain matches left alone; see `cleam leftovers`)")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        return 1
    if args.snapshot and not _snapshot(f"Cleam: before force-uninstalling {app.name}"):
        print("cleam: snapshot failed, nothing removed", file=sys.stderr)
        return 1
    # Folder first, and stop at the first failure: a folder still in use must
    # not end with its Installed-apps entry deleted.
    backup, errors = leftovers.remove(chosen, label=app.name, stop_on_error=True)
    for error in errors:
        print(f"cleam: {error}", file=sys.stderr)
    if errors:
        print("cleam: stopped. Close the program (and its tray icon or service), then run this again.",
              file=sys.stderr)
    print(f"Backed up to {backup}\nUndo with: cleam restore {backup}")
    return 1 if errors else 0


def cmd_leftovers(args) -> int:
    installed = apps.list_apps()
    match = next((a for a in installed if a.id == args.app or a.name.lower() == args.app.lower()), None)
    name = match.name if match else args.app
    found = leftovers.scan(
        name,
        publisher=args.publisher,
        install_dir=args.install_dir,
        other_apps=tuple(a.name for a in installed),
    )
    if args.json:
        print(json.dumps([asdict(item) for item in found], indent=2))
        return 0
    if not found:
        print(f"Nothing left behind by {name}.")
        return 0
    print(f"{'Confidence':<12}{'Kind':<16}{'Size':>11}  Target")
    for item in found:
        print(f"{item.confidence:<12}{item.kind:<16}{human(item.bytes):>11}  {item.target}")
    chosen = found if args.include_low else [i for i in found if i.confidence == "high"]
    if not args.remove:
        print(f"\n{len(chosen)} would be removed. Add --remove to move them to a backup.")
        return 0
    if not chosen:
        print("\nNothing at high confidence. Add --include-low to remove the uncertain ones too.")
        return 0
    if not args.yes and input(f"\nMove {len(chosen)} items to a backup and remove them? [y/N] ").strip().lower() != "y":
        return 1
    backup, errors = leftovers.remove(chosen, label=name)
    print(f"Backed up to {backup}")
    print(f"Undo with: cleam restore {backup}")
    for error in errors:
        print(f"cleam: {error}", file=sys.stderr)
    return 1 if errors else 0


def cmd_restore(args) -> int:
    errors = leftovers.restore(args.backup)
    for error in errors:
        print(f"cleam: {error}", file=sys.stderr)
    if not errors:
        print(f"Restored everything in {args.backup}")
    return 1 if errors else 0


SYMBOL = {"ok": "ok  ", "warn": "WARN", "bad": "BAD ", "unknown": "?   "}


def cmd_security(args) -> int:
    """Protection status, startup programs and signs of ransomware. Changes nothing unless --fix."""
    checks = security.status()
    if args.fix is not None:
        return _fix(checks, args.fix, args.yes)
    items = security.startup()
    signs = security.ransom_signs()
    if args.json:
        print(json.dumps({"checks": [asdict(c) for c in checks], "startup": [asdict(i) for i in items],
                          "ransomware": {**asdict(signs), "state": signs.state}}, indent=2))
    else:
        print("Protection")
        for c in checks:
            print(f"  {SYMBOL[c.state]} {c.label}: {c.detail}")
            if c.fix:
                print(f"       -> {c.fix}")
            for f in repair.fixes_for(c):
                print(f"       fix: cleam security --fix {c.id}   ({f.title})")
        print(f"\nSigns of ransomware ({signs.files_checked} files in your folders"
              + (", stopped early" if signs.truncated else "") + ")")
        for line in signs.findings() or ["none found"]:
            print(f"  {SYMBOL[signs.state]} {line}")
        print("\nStartup programs")
        for i in items:
            mark = "WARN" if i.suspicious else "    "
            state = "" if i.enabled else " (disabled)"
            print(f"  {mark} {i.name}{state}  [{i.publisher or i.signed or '-'}]\n         {i.command}")
            for reason in i.reasons:
                print(f"         -> {reason}")
        if items:
            print("\n  Switch one off: cleam startup")
    if args.scan:
        if not security.can_quick_scan(checks):
            print("\ncleam: Microsoft Defender is not running, so there is nothing to scan with.", file=sys.stderr)
            return 1
        print("\nRunning Microsoft Defender quick scan…", flush=True)
        code, text = security.quick_scan()
        print(text)
        if code:
            return code
    bad = any(c.state == "bad" for c in checks) or signs.state == "bad"
    return 1 if bad else 0


def _fix(checks, wanted: str, yes: bool) -> int:
    """List the fixes (no id), or run the ones for one check id."""
    found = [(c, f) for c in checks for f in repair.fixes_for(c)]
    if not wanted:
        for c, f in found:
            print(f"  {c.id:20} {f.title}  [{f.kind}]")
        print("\nRun one: cleam security --fix <id> --yes" if found else "Nothing to fix.")
        return 0
    chosen = [(c, f) for c, f in found if c.id == wanted]
    if not chosen:
        raise SystemExit(f"cleam: no fix for '{wanted}' (see `cleam security --fix`)")
    failed = 0
    for c, f in chosen:
        print(f"{f.title}:")
        for line in f.tech:
            print(f"    {line}")
        if f.warn:
            print(f"  ! {f.warn}")
        if not yes:
            print("  (dry run: add --yes to do it)")
            continue
        ok, said = repair.run(f)
        failed += not ok
        print(f"  {'OK' if ok else 'FAILED'}: {said}")
    return 1 if failed else 0


def cmd_startup(args) -> int:
    """What starts by itself; --off/--on switch one entry (dry run unless --yes)."""
    items = security.startup()
    wanted = args.off or args.on
    if not wanted:
        if args.json:
            print(json.dumps([{**asdict(i), "id": i.id, "protective": i.protective} for i in items], indent=2))
            return 0
        for i in items:
            mark = "WARN" if i.suspicious else "    "
            state = "" if i.enabled else " (off)"
            print(f"  {mark} {i.name}{state}  [{i.publisher or i.signed or '-'}]  {i.location}\n"
                  f"         id: {i.id}\n         {i.command}")
            for reason in i.reasons:
                print(f"         -> {reason}")
        print("\nSwitch one: cleam startup --off <id> --yes   (--on puts back what Cleam switched off)")
        return 0
    item = next((i for i in items if i.id.lower() == wanted.lower()), None)
    if item is None:
        raise SystemExit(f"cleam: no startup entry '{wanted}' (see `cleam startup`)")
    title = repair.TURN_OFF if args.off else repair.TURN_ON
    fix = next((f for f in repair.startup_fixes(item) if f.title == title), None)
    if fix is None:
        why = ("protective software is never switched off" if item.protective
               else f"it is already {'off' if args.off else 'on'}" if item.enabled != bool(args.off)
               else "turn it back on in Task Manager > Startup" if args.on and item.kind in ("run", "folder")
               else "only entries Cleam switched off can be put back here" if args.on
               else "a RunOnce entry deletes itself after one run" if item.location.endswith("RunOnce")
               else "Cleam has no switch for this kind of entry yet")
        raise SystemExit(f"cleam: cannot {title.lower()} {item.name}: {why}")
    print(f"{fix.title}: {item.name}")
    for line in fix.tech:
        print(f"    {line}")
    if fix.warn:
        print(f"  ! {fix.warn}")
    if not args.yes:
        print("  (dry run: add --yes to do it)")
        return 0
    ok, said = repair.run(fix)
    print(f"  {'OK' if ok else 'FAILED'}: {said}")
    return 0 if ok else 1


def cmd_snapshot(args) -> int:
    code, text = (
        snapshot.list_snapshots() if args.action == "list" else snapshot.create(args.description)
    )
    if text:
        print(f"cleam: {text}", file=sys.stderr if code else sys.stdout)
    return code


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cleam", description="Clean junk files, uninstall programs, take snapshots.")
    p.add_argument("--version", action="version", version=f"cleam {__version__}")
    sub = p.add_subparsers(dest="command")

    m = sub.add_parser("menu", help="the interactive menu (also what bare `cleam` opens)")
    m.set_defaults(fn=cmd_menu)

    d = sub.add_parser("debloat", help="Windows privacy, ads, AI and app debloat; every change can be undone")
    d.add_argument("action", choices=("list", "apply", "undo", "revert"),
                   help="revert: turn tweaks off whoever turned them on (Cleam's own are undone exactly)")
    d.add_argument("ids", nargs="*", help="tweak ids from `cleam debloat list`")
    d.add_argument("--recommended", action="store_true", help="apply: add every recommended tweak")
    d.add_argument("--all", action="store_true", help="undo: everything Cleam changed")
    d.add_argument("--yes", action="store_true", help="apply without asking")
    d.add_argument("--snapshot", action="store_true", help="create a restore point first, abort if it fails")
    d.add_argument("--json", action="store_true")
    d.set_defaults(fn=cmd_debloat)

    o = sub.add_parser("overview", help="OS, disk usage and where the space went")
    o.add_argument("--json", action="store_true")
    o.set_defaults(fn=cmd_overview)

    b = sub.add_parser("biggest", help="largest folders directly inside a path")
    b.add_argument("path")
    b.add_argument("--top", type=int, default=10)
    b.add_argument("--json", action="store_true")
    b.set_defaults(fn=cmd_biggest)

    t = sub.add_parser("targets", help="what each junk target is, and which need admin or a deliberate opt-in")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_targets)

    s = sub.add_parser("scan", help="report junk per target (read-only)")
    s.add_argument("--only", metavar="IDS", help="comma-separated target ids")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_scan)

    c = sub.add_parser("clean", help="delete junk (dry run unless --yes)")
    c.add_argument("--only", metavar="IDS", help="comma-separated target ids")
    c.add_argument("--yes", action="store_true", help="actually delete")
    c.add_argument("--all", action="store_true",
                   help="include opt-in targets (Recycle Bin/Trash, package caches, shader caches)")
    c.add_argument("--snapshot", action="store_true", help="take a restore point/snapshot first, abort if it fails")
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_clean)

    a = sub.add_parser("apps", help="list installed programs")
    a.add_argument("--filter", metavar="TEXT")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_apps)

    u = sub.add_parser("uninstall", help="run a program's own uninstaller")
    u.add_argument("app", help="id or exact name from `cleam apps`")
    u.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    u.add_argument("--snapshot", action="store_true", help="take a restore point/snapshot first")
    u.add_argument("--force", action="store_true",
                   help="its uninstaller is missing or broken: move its folder and entry to a backup instead "
                        "(cleam restore undoes it)")
    u.add_argument("--install-dir", default="", metavar="DIR",
                   help="with --force: the program's folder, when Cleam cannot work it out or it is not listed")
    u.set_defaults(fn=cmd_uninstall)

    lo = sub.add_parser("leftovers", help="what an uninstaller left behind, and remove it reversibly")
    lo.add_argument("app", help="program name or id, even one already uninstalled")
    lo.add_argument("--publisher", default="", help="helps match the publisher's own folder")
    lo.add_argument("--install-dir", default="", help="the folder it used to live in")
    lo.add_argument("--remove", action="store_true", help="move the matches to a backup and remove them")
    lo.add_argument("--include-low", action="store_true", help="also remove uncertain matches")
    lo.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    lo.add_argument("--json", action="store_true")
    lo.set_defaults(fn=cmd_leftovers)

    re_ = sub.add_parser("restore", help="put a leftovers backup back")
    re_.add_argument("backup", help="a folder printed by `cleam leftovers --remove`")
    re_.set_defaults(fn=cmd_restore)

    se = sub.add_parser("security", help="system check: protection, updates, hardware, gaming set-up, startup, ransomware; --fix repairs")
    se.add_argument("--scan", action="store_true", help="also run a Microsoft Defender quick scan")
    se.add_argument("--json", action="store_true")
    se.add_argument("--fix", nargs="?", const="", metavar="ID",
                    help="list the fixes, or show (and with --yes run) the fix for one check id")
    se.add_argument("--yes", action="store_true", help="with --fix: really do it")
    se.set_defaults(fn=cmd_security)

    st = sub.add_parser("startup", help="what starts by itself (programs, tasks, services); switch one off or back on")
    how = st.add_mutually_exclusive_group()
    how.add_argument("--off", metavar="ID", help="switch this entry off (ids from `cleam startup`)")
    how.add_argument("--on", metavar="ID", help="put back an entry Cleam switched off")
    st.add_argument("--yes", action="store_true", help="really do it")
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=cmd_startup)

    sn = sub.add_parser("snapshot", help="create or list restore points/snapshots")
    sn.add_argument("action", choices=("create", "list"))
    sn.add_argument("--description", default="Cleam")
    sn.set_defaults(fn=cmd_snapshot)
    return p


def cmd_menu(args) -> int:
    from . import tui

    return tui.run()


def _pick(ids: list[str], recommended: bool) -> list[debloat.Tweak]:
    by_id = {t.id: t for t in debloat.TWEAKS}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise SystemExit(f"cleam: unknown tweak {', '.join(unknown)} (see `cleam debloat list`)")
    chosen = [by_id[i] for i in ids]
    if recommended:
        chosen += [t for t in debloat.recommended(debloat.current_env()) if t not in chosen]
    return chosen


def cmd_debloat(args) -> int:
    if OS != "windows":
        print("cleam: debloat is for Windows 10 and 11.", file=sys.stderr)
        return 2
    engine = debloat.Debloater()
    if args.action == "list":
        states = engine.states()
        rows = [{**{k: v for k, v in debloat.as_dict(t).items() if k in ("id", "group", "title", "about", "default",
                                                                           "risk", "restart")},
                 "level": debloat.tier(t), "tech": debloat.tech(t),
                 "state": states[t.id], "unavailable": debloat.availability(t, engine.env)} for t in debloat.TWEAKS]
        if args.json:
            print(json.dumps({"tweaks": rows, "apps": [{"id": a.id, "name": a.name, "default": a.default}
                                                        for a, _ in engine.removable_apps()],
                              "changed": engine.applied()}, indent=2))
            return 0
        group = ""
        for r in rows:
            if r["group"] != group:
                group = r["group"]
                print(f"\n{group}")
            mark = "*" if r["default"] else " "
            print(f"  {mark} {r['id']:<24}{r['state']:<13}{r['unavailable'] or r['title']}")
        apps_left = engine.removable_apps()
        if apps_left:
            print("\nRemovable apps: " + ", ".join(a.id for a, _ in apps_left))
        print("\n* = recommended.  cleam debloat apply --recommended   /   cleam debloat undo --all")
        return 0
    if args.action == "apply":
        tweaks = _pick(args.ids, args.recommended)
        if not tweaks:
            print("cleam: name tweaks to apply, or --recommended", file=sys.stderr)
            return 2
        if not args.yes:
            for t in tweaks:
                print(f"  {t.id:<24}{t.title}")
            if input(f"Apply {len(tweaks)} changes? [y/N] ").strip().lower() != "y":
                return 1
        if args.snapshot and not _snapshot("Cleam: before debloat"):
            print("cleam: snapshot failed, nothing changed", file=sys.stderr)
            return 1
        results = [engine.apply(t) for t in tweaks]
    elif args.action == "revert":
        results = [engine.revert(t) for t in _pick(args.ids, False)]
    else:  # undo
        changed = engine.applied()
        ids = list(changed["tweaks"]) if args.all else args.ids
        results = [engine.undo(i) for i in ids]
    failed = 0
    for out in results:
        failed += not out.ok
        print(f"  {'ok  ' if out.ok else 'FAIL'} {out.id}" + ("" if out.ok else f": {out.message}"))
    restarts = sorted({o.restart for o in results if o.ok and o.restart})
    if restarts:
        print(f"Takes effect after: {', '.join(restarts)}.")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command is None:  # bare `cleam`: the menu, when there is someone to use it
        return cmd_menu(args)
    return args.fn(args)
