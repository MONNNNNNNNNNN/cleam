"""Flet window over the same core the CLI uses.

Nothing here decides what is junk -- it calls junk.run(), apps.list_apps() and
snapshot.run() exactly as the CLI does, so both front ends can never disagree
about what a clean will delete. Long work goes through page.run_thread(), since
a scan of a cache tree takes seconds and would otherwise freeze the window.
"""
from __future__ import annotations

try:
    import flet as ft
except ModuleNotFoundError:  # pragma: no cover - depends on how Cleam was installed
    raise SystemExit("The Cleam GUI needs flet: pip install 'cleam[gui]'")

from . import __version__, apps as apps_module, junk, overview, snapshot
from .cli import human
from .system import is_admin

MONO = "monospace"


def _toast(page: ft.Page, message: str) -> None:
    page.show_dialog(ft.SnackBar(ft.Text(message), show_close_icon=True))


def _confirm(page: ft.Page, title: str, body: str, action: str, on_yes) -> None:
    def yes(_):
        page.pop_dialog()
        on_yes()

    page.show_dialog(
        ft.AlertDialog(
            modal=True,
            icon=ft.Icon(ft.Icons.WARNING_AMBER),
            title=ft.Text(title),
            content=ft.Text(body, font_family=MONO, size=12, selectable=True),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                ft.FilledButton(action, on_click=yes),
            ],
        )
    )


def _overview_tab(page: ft.Page) -> tuple[ft.Control, object]:
    rows = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)
    busy = ft.ProgressBar(visible=False)

    def disk_row(disk: overview.Disk) -> ft.Control:
        return ft.ListTile(
            leading=ft.Icon(ft.Icons.STORAGE),
            title=ft.Text(disk.mount),
            subtitle=ft.Column(
                [
                    ft.Text(
                        f"{human(disk.used)} used of {human(disk.total)} · {human(disk.free)} free",
                        size=12,
                    ),
                    ft.ProgressBar(value=disk.percent_used / 100, bar_height=6),
                ],
                spacing=4,
                tight=True,
            ),
            trailing=ft.Text(f"{disk.percent_used}%"),
        )

    def folder_row(folder: overview.Folder) -> ft.Control:
        if not folder.measured:
            size = "measuring…"
        elif folder.unreadable:
            size = "needs admin"
        else:
            size = human(folder.bytes)
        subtitle = [ft.Text(f"{folder.path} · {folder.files} files", size=12)]
        if folder.note:
            subtitle.append(ft.Text(folder.note, size=12, italic=True))
        return ft.ListTile(
            leading=ft.Icon(ft.Icons.FOLDER_OUTLINED),
            title=ft.Text(folder.label),
            subtitle=ft.Column(subtitle, spacing=2, tight=True),
            trailing=ft.Text(size),
        )

    def load() -> None:
        busy.visible = True
        rows.controls = [ft.Text(overview.os_name(), weight=ft.FontWeight.BOLD)]
        page.update()
        rows.controls += [disk_row(d) for d in overview.disks()]
        rows.controls.append(ft.Divider())
        page.update()
        # One folder at a time, each shown as it finishes: a walk of AppData or
        # C:\Windows is hundreds of thousands of files and takes real seconds.
        found = overview.folders()
        placeholders = [folder_row(f) for f in found]
        rows.controls += placeholders
        page.update()
        for folder, placeholder in zip(found, placeholders):
            overview.measure(folder)
            rows.controls[rows.controls.index(placeholder)] = folder_row(folder)
            page.update()
        busy.visible = False
        page.update()

    return ft.Column(
        [
            ft.Row([ft.Button("Refresh", icon=ft.Icons.REFRESH, on_click=lambda _: page.run_thread(load))]),
            busy,
            rows,
        ],
        expand=True,
    ), load


def _clean_tab(page: ft.Page) -> ft.Control:
    rows = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)
    total = ft.Text("Nothing scanned yet", weight=ft.FontWeight.BOLD)
    busy = ft.ProgressBar(visible=False)
    snapshot_first = ft.Switch(label="Snapshot first", value=False)
    boxes: dict[str, ft.Checkbox] = {}
    found: dict[str, junk.Result] = {}

    def render(scanned: list[tuple[junk.Target, junk.Result]]) -> None:
        rows.controls.clear()
        boxes.clear()
        found.clear()
        for target, result in scanned:
            found[target.id] = result
            nothing = bool(result.skipped) or result.files == 0
            box = ft.Checkbox(value=not nothing, disabled=nothing)
            boxes[target.id] = box
            note = result.skipped or (f"{result.errors} in use or denied" if result.errors else "")
            subtitle = f"{result.files} files" + (f" · {note}" if note else "")
            rows.controls.append(
                ft.ListTile(
                    leading=box,
                    title=ft.Text(target.label),
                    subtitle=ft.Text(subtitle, size=12),
                    trailing=ft.Text(human(result.bytes) if result.files else "—"),
                    on_click=lambda _, b=box: (setattr(b, "value", not b.value), page.update()),
                )
            )
        files = sum(r.files for r in found.values())
        total.value = f"{files} files · {human(sum(r.bytes for r in found.values()))} can be freed"

    def scan() -> None:
        busy.visible = True
        total.value = "Scanning…"
        page.update()
        render([(t, junk.run(t)) for t in junk.targets()])
        busy.visible = False
        page.update()

    def clean(chosen: list[junk.Target]) -> None:
        busy.visible = True
        page.update()
        if snapshot_first.value:
            code, text = snapshot.create("Cleam: before clean", capture=True)
            if code != 0:
                busy.visible = False
                page.update()
                _toast(page, f"Nothing deleted — snapshot failed. {text}")
                return
        freed = sum(junk.run(t, delete=True).bytes for t in chosen)
        scan()  # re-scan, so the list shows what is actually left
        _toast(page, f"Freed {human(freed)}")

    def ask_clean(_) -> None:
        chosen = [t for t in junk.targets() if boxes.get(t.id) and boxes[t.id].value]
        if not chosen:
            _toast(page, "Nothing selected. Scan first, then tick what to clean.")
            return
        body = "\n".join(f"{t.label}: {found[t.id].files} files, {human(found[t.id].bytes)}" for t in chosen)
        _confirm(page, "Delete these files?", f"{body}\n\nThis cannot be undone.", "Delete", lambda: page.run_thread(clean, chosen))

    controls = [
        ft.Row(
            [
                ft.Button("Scan", icon=ft.Icons.SEARCH, on_click=lambda _: page.run_thread(scan)),
                ft.FilledButton("Clean selected", icon=ft.Icons.DELETE_SWEEP, on_click=ask_clean),
                snapshot_first,
            ]
        ),
        busy,
        total,
        ft.Divider(),
        rows,
    ]
    if not is_admin():
        controls.insert(
            0,
            ft.Text(
                "Not running elevated: system targets (Windows temp and Update cache, apt's package cache)"
                " are skipped.",
                size=12,
                italic=True,
            ),
        )
    return ft.Column(controls, expand=True)


def _apps_tab(page: ft.Page) -> tuple[ft.Control, object]:
    listing = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)
    busy = ft.ProgressBar(visible=False)
    installed: list[apps_module.App] = []

    def show(_=None) -> None:
        needle = (search.value or "").lower()
        listing.controls = [row(a) for a in installed if needle in a.name.lower()]
        page.update()

    def row(app: apps_module.App) -> ft.Control:
        return ft.ListTile(
            title=ft.Text(app.name),
            subtitle=ft.Text(f"{app.version} · {app.source}", size=12),
            trailing=ft.TextButton("Uninstall", on_click=lambda _, a=app: ask(a)),
        )

    def load() -> None:
        busy.visible = True
        page.update()
        installed[:] = apps_module.list_apps()
        busy.visible = False
        show()

    def ask(app: apps_module.App) -> None:
        command = app.command if isinstance(app.command, str) else " ".join(app.command)
        _confirm(
            page,
            f"Uninstall {app.name}?",
            f"Cleam runs the program's own uninstaller:\n\n{command}",
            "Uninstall",
            lambda: page.run_thread(remove, app),
        )

    def remove(app: apps_module.App) -> None:
        busy.visible = True
        page.update()
        code, text = apps_module.uninstall(app, capture=True)
        last = text.strip().splitlines()[-1] if text.strip() else ""
        _toast(page, last or (f"Uninstalled {app.name}" if code == 0 else f"Failed (exit {code})"))
        load()

    search = ft.TextField(label="Filter", dense=True, expand=True, on_change=show)
    control = ft.Column(
        [
            ft.Row([search, ft.Button("Refresh", icon=ft.Icons.REFRESH, on_click=lambda _: page.run_thread(load))]),
            busy,
            listing,
        ],
        expand=True,
    )
    return control, load


def _snapshot_tab(page: ft.Page) -> ft.Control:
    description = ft.TextField(label="Description", value="Cleam", dense=True, expand=True)
    out = ft.Text("", font_family=MONO, size=12, selectable=True)
    busy = ft.ProgressBar(visible=False)

    def work(action: str) -> None:
        busy.visible = True
        out.value = "Working…"
        page.update()
        code, text = (
            snapshot.list_snapshots(capture=True)
            if action == "list"
            else snapshot.create(description.value or "Cleam", capture=True)
        )
        busy.visible = False
        out.value = text or ("Done" if code == 0 else f"Failed (exit {code})")
        page.update()

    return ft.Column(
        [
            ft.Row(
                [
                    description,
                    ft.FilledButton("Create", icon=ft.Icons.HISTORY, on_click=lambda _: page.run_thread(work, "create")),
                    ft.Button("List", icon=ft.Icons.REFRESH, on_click=lambda _: page.run_thread(work, "list")),
                ]
            ),
            busy,
            ft.Column([out], scroll=ft.ScrollMode.AUTO, expand=True),
        ],
        expand=True,
    )


def main(page: ft.Page) -> None:
    page.title = f"Cleam {__version__}"
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.TEAL)
    page.padding = 16
    overview_view, load_overview = _overview_tab(page)
    apps_view, load_apps = _apps_tab(page)
    loaded: set[int] = set()

    def on_tab(e) -> None:
        # Both lists take a second or more to read, so each loads when its tab
        # is first opened rather than on startup. A junk scan is heavier still,
        # so that stays a button the user presses.
        index = tabs.selected_index
        if index in (0, 2) and index not in loaded:
            loaded.add(index)
            page.run_thread(load_overview if index == 0 else load_apps)

    tabs = ft.Tabs(
        length=4,
        expand=True,
        on_change=on_tab,
        content=ft.Column(
            [
                ft.TabBar(
                    tabs=[
                        ft.Tab(label="Overview", icon=ft.Icons.MONITOR_HEART),
                        ft.Tab(label="Clean", icon=ft.Icons.CLEANING_SERVICES),
                        ft.Tab(label="Programs", icon=ft.Icons.APPS),
                        ft.Tab(label="Snapshots", icon=ft.Icons.HISTORY),
                    ]
                ),
                ft.TabBarView(
                    controls=[overview_view, _clean_tab(page), apps_view, _snapshot_tab(page)],
                    expand=True,
                ),
            ],
            expand=True,
        ),
    )
    page.add(tabs)
    loaded.add(0)
    page.run_thread(load_overview)  # the opening tab, so it fills itself in


def run() -> None:
    ft.run(main)
