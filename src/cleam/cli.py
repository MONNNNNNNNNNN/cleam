"""cleam command line. Every destructive command is a dry run or a prompt unless --yes."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from . import __version__, apps, junk, snapshot


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


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
    if args.snapshot and snapshot.create("Cleam: before clean") != 0:
        print("cleam: snapshot failed, nothing deleted", file=sys.stderr)
        return 1
    results = [junk.run(t, delete=True) for t in targets]
    _report(results, args.json)
    return 1 if any(r.errors for r in results) else 0


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
    if args.snapshot and snapshot.create(f"Cleam: before uninstalling {app.name}") != 0:
        print("cleam: snapshot failed, nothing uninstalled", file=sys.stderr)
        return 1
    return apps.uninstall(app)


def cmd_snapshot(args) -> int:
    return snapshot.list_snapshots() if args.action == "list" else snapshot.create(args.description)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cleam", description="Clean junk files, uninstall programs, take snapshots.")
    p.add_argument("--version", action="version", version=f"cleam {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

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
