"""cleam command line. Every destructive command is a dry run or a prompt unless --yes."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from . import __version__, apps, junk, overview, snapshot
from .system import human


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


def _selected(only: str | None) -> list[junk.Target]:
    all_targets = junk.targets()
    if not only:
        return all_targets
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
    print(f"{'Target':<16}{'Files':>8}{'Size':>12}  Note")
    for r in results:
        if r.skipped:
            print(f"{r.id:<16}{'-':>8}{'-':>12}  {r.skipped}")
        else:
            note = f"{r.errors} in use or denied" if r.errors else ""
            print(f"{r.id:<16}{r.files:>8}{human(r.bytes):>12}  {note}")
    print(f"{'Total':<16}{sum(r.files for r in results):>8}{human(sum(r.bytes for r in results)):>12}")


def cmd_scan(args) -> int:
    _report([junk.run(t) for t in _selected(args.only)], args.json)
    return 0


def cmd_clean(args) -> int:
    targets = _selected(args.only)
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
    matches = [a for a in apps.list_apps() if a.id == args.app] or [
        a for a in apps.list_apps() if a.name.lower() == args.app.lower()
    ]
    if len(matches) != 1:
        print(f"cleam: {len(matches)} apps match {args.app!r}; use the id from `cleam apps`", file=sys.stderr)
        for a in matches:
            print(f"  {a.id}  ({a.name} {a.version})", file=sys.stderr)
        return 2
    app = matches[0]
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
    sub = p.add_subparsers(dest="command", required=True)

    o = sub.add_parser("overview", help="OS, disk usage and where the space went")
    o.add_argument("--json", action="store_true")
    o.set_defaults(fn=cmd_overview)

    b = sub.add_parser("biggest", help="largest folders directly inside a path")
    b.add_argument("path")
    b.add_argument("--top", type=int, default=10)
    b.add_argument("--json", action="store_true")
    b.set_defaults(fn=cmd_biggest)

    s = sub.add_parser("scan", help="report junk per target (read-only)")
    s.add_argument("--only", metavar="IDS", help="comma-separated target ids")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_scan)

    c = sub.add_parser("clean", help="delete junk (dry run unless --yes)")
    c.add_argument("--only", metavar="IDS", help="comma-separated target ids")
    c.add_argument("--yes", action="store_true", help="actually delete")
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
    u.set_defaults(fn=cmd_uninstall)

    sn = sub.add_parser("snapshot", help="create or list restore points/snapshots")
    sn.add_argument("action", choices=("create", "list"))
    sn.add_argument("--description", default="Cleam")
    sn.set_defaults(fn=cmd_snapshot)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.fn(args)
