"""Flet window over the same core the CLI uses.

Nothing here decides what is junk -- it calls junk.run(), apps.list_apps(),
overview.measure() and snapshot.run() exactly as the CLI does, so both front
ends can never disagree about what a clean will delete.

Two rules hold this together, both learned by breaking them:

- Every slow job goes through _worker(), which refuses to start a second run
  in the same panel. Flet's page.run_thread uses a multi-worker
  ThreadPoolExecutor, so two clicks on Refresh really do run in parallel, and
  two jobs rebuilding one control list corrupt it.
- A destructive control is never pre-selected for the user unless the target
  says it is safe to (junk.Target.opt_in), and the button that deletes stays
  disabled until there is a scan result to delete.
"""
from __future__ import annotations

try:
    import flet as ft
except ModuleNotFoundError:  # pragma: no cover - depends on how Cleam was installed
    raise SystemExit("The Cleam GUI needs flet: pip install 'cleam[gui]'")

from . import __version__, apps as apps_module, junk, overview, snapshot
from .system import OS, human, is_admin, relaunch_as_admin

MONO = "monospace"
UNINSTALL_TIMEOUT = 900  # 15 minutes: long enough for a real uninstaller, short enough to end
APP_ROWS = 200  # a Windows registry can hold hundreds; rendering all of them stalls the filter


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


def _worker(page: ft.Page, busy: ft.ProgressBar, job, *args) -> None:
    """Run job off the UI thread, unless this panel already has a job running."""
    if busy.visible:
        _toast(page, "Still working on the last one.")
        return

    def run() -> None:
        busy.visible = True
        page.update()
        try:
            job(*args)
        finally:
            busy.visible = False
            page.update()

    page.run_thread(run)


def _buttons(*controls: ft.Control) -> ft.Row:
    """A row of fixed-width controls that wraps instead of clipping.

    At 700px or 200% scaling a plain Row cut off its last control, which put
    "Snapshot first" out of reach. Never pass a control with expand=True: a
    wrapping row cannot lay out an expanding child, and Flutter renders the
    whole panel as a grey block when it tries. Use _field_row() for those.
    """
    return ft.Row(list(controls), wrap=True, run_spacing=8, spacing=8)


def _field_row(*controls: ft.Control) -> ft.Row:
    """A row holding an expanding text field: shrinks rather than wraps."""
    return ft.Row(list(controls), spacing=8)


def _admin_banner(page: ft.Page) -> ft.Control | None:
    if is_admin():
        return None
    text = ft.Text(
        "Not elevated: system targets (Windows temp and Update cache, apt's package cache)"
        " and restore points are unavailable.",
        size=12,
        italic=True,
    )
    if OS != "windows":
        return _buttons(text, ft.Text("Start Cleam with sudo to include them.", size=12, italic=True))

    def elevate(_) -> None:
        if relaunch_as_admin():
            _toast(page, "An elevated Cleam is starting. Close this window.")
        else:
            _toast(page, "Windows refused the elevation prompt.")

    return _buttons(text, ft.Button("Restart as Administrator", icon=ft.Icons.SHIELD, on_click=elevate))


def _overview_tab(page: ft.Page) -> tuple[ft.Control, object]:
    header = ft.Column(spacing=0)
    folder_rows = ft.Column(spacing=0)
    busy = ft.ProgressBar(visible=False)
    found: list[overview.Folder] = []

    def disk_row(disk: overview.Disk) -> ft.Control:
        return ft.ListTile(
            leading=ft.Icon(ft.Icons.STORAGE),
            title=ft.Text(disk.mount),
            subtitle=ft.Column(
                [
                    ft.Text(f"{human(disk.used)} used of {human(disk.total)} · {human(disk.free)} free", size=12),
                    ft.ProgressBar(value=disk.percent_used / 100, bar_height=6),
                ],
                spacing=4,
                tight=True,
            ),
            trailing=ft.Text(f"{disk.percent_used}%"),
        )

    def folder_row(folder: overview.Folder) -> ft.Control:
        subtitle = [ft.Text(folder.detail, size=12)]
        if folder.note:
            subtitle.append(ft.Text(folder.note, size=12, italic=True))
        return ft.ListTile(
            leading=ft.Icon(ft.Icons.FOLDER_OUTLINED),
            title=ft.Text(folder.label),
            subtitle=ft.Column(subtitle, spacing=2, tight=True),
            trailing=ft.Text(folder.size_label),
        )

    def load() -> None:
        """Instant: the OS line, the disks, and the folder list unmeasured."""
        found[:] = overview.folders()
        header.controls = [ft.Text(overview.os_name(), weight=ft.FontWeight.BOLD)]
        header.controls += [disk_row(d) for d in overview.disks()]
        header.controls.append(ft.Divider())
        # Not measured here. On Windows these folders are hundreds of thousands
        # of files, and walking them the moment the window opens is disk load
        # nobody asked for.
        folder_rows.controls = [folder_row(f) for f in found]
        page.update()

    def measure() -> None:
        for i, folder in enumerate(found):
            folder_rows.controls[i] = ft.ListTile(
                leading=ft.ProgressRing(width=18, height=18),
                title=ft.Text(folder.label),
                subtitle=ft.Text(f"Measuring {folder.path}…", size=12),
            )
            page.update()
            overview.measure(folder)
            folder_rows.controls[i] = folder_row(folder)  # by index: never by object identity
            page.update()

    control = ft.Column(
        [
            _buttons(
                ft.Button("Measure folders", icon=ft.Icons.STRAIGHTEN, on_click=lambda _: _worker(page, busy, measure)),
                ft.Button("Refresh", icon=ft.Icons.REFRESH, on_click=lambda _: _worker(page, busy, load)),
            ),
            busy,
            ft.Column([header, folder_rows], scroll=ft.ScrollMode.AUTO, expand=True, spacing=0),
        ],
        expand=True,
    )
    return control, load


def _clean_tab(page: ft.Page) -> ft.Control:
    rows = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)
    total = ft.Text("Nothing scanned yet. Press Scan.", weight=ft.FontWeight.BOLD)
    busy = ft.ProgressBar(visible=False)
    snapshot_first = ft.Switch(label="Snapshot first", value=False)
    scanned: list[tuple[junk.Target, junk.Result, ft.Checkbox]] = []

    def render() -> None:
        rows.controls.clear()
        for target, result, box in scanned:
            note = result.skipped or (f"{result.errors} in use or denied" if result.errors else "")
            subtitle = f"{result.files} files" + (f" · {note}" if note else "")
            if target.opt_in and not result.skipped and result.files:
                subtitle += " · tick to include"
            rows.controls.append(
                ft.ListTile(
                    # The label belongs to the checkbox, not to a separate
                    # title: a screen reader announcing "checkbox, unchecked"
                    # with the name somewhere else is useless.
                    title=box,
                    subtitle=ft.Text(subtitle, size=12),
                    trailing=ft.Text(human(result.bytes) if result.files else "—"),
                )
            )
        files = sum(r.files for _, r, _ in scanned)
        size = human(sum(r.bytes for _, r, _ in scanned))
        total.value = f"{files} files · {size} can be freed" if files else "Nothing to clean."
        clean_button.disabled = not any(r.files for _, r, _ in scanned)

    def scan() -> None:
        total.value = "Scanning…"
        page.update()
        # The target list is captured here and reused for the dialog and the
        # delete. Rebuilding it per click re-ran `whoami` and probed every
        # drive letter, and could hand the delete a root that was never shown.
        scanned.clear()
        for target in junk.targets():
            result = junk.run(target)
            nothing = bool(result.skipped) or result.files == 0
            box = ft.Checkbox(
                label=target.label,
                value=not nothing and not target.opt_in,  # the Recycle Bin is never pre-selected
                disabled=nothing,
            )
            scanned.append((target, result, box))
        render()
        page.update()

    def clean(chosen: list[tuple[junk.Target, junk.Result, ft.Checkbox]]) -> None:
        if snapshot_first.value:
            code, text = snapshot.create("Cleam: before clean", capture=True)
            if code != 0:
                _toast(page, f"Nothing deleted — snapshot failed. {text}")
                return
        freed = errors = 0
        for target, _, _ in chosen:
            result = junk.run(target, delete=True)
            freed += result.bytes
            errors += result.errors
        scan()  # re-scan, so the list shows what is actually left
        message = f"Freed {human(freed)}"
        _toast(page, f"{message}. {errors} files were in use and stayed." if errors else message)

    def ask_clean(_) -> None:
        chosen = [row for row in scanned if row[2].value]
        if not chosen:
            _toast(page, "Tick what to clean first.")
            return
        body = "\n".join(f"{t.label}: {r.files} files, {human(r.bytes)}" for t, r, _ in chosen)
        _confirm(
            page,
            "Delete these files?",
            f"{body}\n\nThis cannot be undone.",
            "Delete",
            lambda: _worker(page, busy, clean, chosen),
        )

    clean_button = ft.FilledButton(
        "Clean selected", icon=ft.Icons.DELETE_SWEEP, disabled=True, on_click=ask_clean
    )
    controls = [
        _buttons(
            ft.Button("Scan", icon=ft.Icons.SEARCH, on_click=lambda _: _worker(page, busy, scan)),
            clean_button,
            snapshot_first,
        ),
        busy,
        total,
        ft.Divider(),
        rows,
    ]
    if banner := _admin_banner(page):
        controls.insert(0, banner)
    return ft.Column(controls, expand=True)


def _apps_tab(page: ft.Page) -> tuple[ft.Control, object]:
    listing = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)
    status = ft.Text("Reading installed programs…", size=12)
    busy = ft.ProgressBar(visible=False)
    installed: list[apps_module.App] = []

    def show(_=None) -> None:
        needle = (search.value or "").lower()
        matches = [a for a in installed if needle in a.name.lower()]
        listing.controls = [row(a) for a in matches[:APP_ROWS]]
        if len(matches) > APP_ROWS:
            listing.controls.append(
                ft.ListTile(title=ft.Text(f"+{len(matches) - APP_ROWS} more. Type to narrow the list.", size=12))
            )
        status.value = f"{len(matches)} of {len(installed)} programs" if installed else "No programs found."
        page.update()

    def row(app: apps_module.App) -> ft.Control:
        return ft.ListTile(
            title=ft.Text(app.name),
            subtitle=ft.Text(f"{app.version} · {app.source}", size=12),
            trailing=ft.TextButton("Uninstall", on_click=lambda _, a=app: ask(a)),
        )

    def load() -> None:
        status.value = "Reading installed programs…"
        page.update()
        installed[:] = apps_module.list_apps()
        show()

    def ask(app: apps_module.App) -> None:
        command = app.command if isinstance(app.command, str) else " ".join(app.command)
        _confirm(
            page,
            f"Uninstall {app.name}?",
            f"Cleam runs the program's own uninstaller:\n\n{command}",
            "Uninstall",
            lambda: _worker(page, busy, remove, app),
        )

    def remove(app: apps_module.App) -> None:
        code, text = apps_module.uninstall(app, capture=True, timeout=UNINSTALL_TIMEOUT)
        last = text.strip().splitlines()[-1] if text.strip() else ""
        _toast(page, last or (f"Uninstalled {app.name}" if code == 0 else f"Failed (exit {code})"))
        load()

    search = ft.TextField(label="Filter", dense=True, expand=True, on_change=show)
    control = ft.Column(
        [
            _field_row(
                search, ft.Button("Refresh", icon=ft.Icons.REFRESH, on_click=lambda _: _worker(page, busy, load))
            ),
            busy,
            status,
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
        out.value = "Working…"
        page.update()
        code, text = (
            snapshot.list_snapshots(capture=True)
            if action == "list"
            else snapshot.create(description.value or "Cleam", capture=True)
        )
        out.value = text or ("Done" if code == 0 else f"Failed (exit {code})")
        page.update()

    controls = [
        _field_row(
            description,
            ft.FilledButton("Create", icon=ft.Icons.HISTORY, on_click=lambda _: _worker(page, busy, work, "create")),
            ft.Button("List", icon=ft.Icons.REFRESH, on_click=lambda _: _worker(page, busy, work, "list")),
        ),
        busy,
        ft.Column([out], scroll=ft.ScrollMode.AUTO, expand=True),
    ]
    if banner := _admin_banner(page):
        controls.insert(0, banner)
    return ft.Column(controls, expand=True)


def main(page: ft.Page) -> None:
    page.title = f"Cleam {__version__}"
    # Both themes are set explicitly: with only `theme`, a machine in dark mode
    # fell back to an unstyled palette.
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.TEAL)
    page.dark_theme = ft.Theme(color_scheme_seed=ft.Colors.TEAL)
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.padding = 16
    overview_view, load_overview = _overview_tab(page)
    apps_view, load_apps = _apps_tab(page)
    loaded: set[int] = set()

    def on_tab(e) -> None:
        # Reading the program list takes a second, so it happens when the tab
        # is first opened. Junk scans and folder measurement are heavier still,
        # so those stay buttons the user presses.
        index = tabs.selected_index
        if index == 2 and index not in loaded:
            loaded.add(index)
            page.run_thread(load_apps)

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
    load_overview()  # cheap: no folder walking, so the first screen is never blank


def run() -> None:
    ft.run(main)
