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

Layout: a navigation rail and one page at a time. Pages scroll as a whole
(one scrolling Column, nothing inside it expands), except Programs, whose
list is a virtualised ListView because a registry can hold hundreds of rows.
"""
from __future__ import annotations

import time

try:
    import flet as ft
except ModuleNotFoundError:  # pragma: no cover - depends on how Cleam was installed
    raise SystemExit("The Cleam GUI needs flet: pip install 'cleam[gui]'")

from . import __version__, apps as apps_module, junk, leftovers as leftovers_module, overview, security, snapshot
from .system import OS, human, is_admin, relaunch_as_admin

MONO = "monospace"
UNINSTALL_TIMEOUT = 900  # 15 minutes: long enough for a real uninstaller, short enough to end
APP_ROWS = 200  # a Windows registry can hold hundreds; rendering all of them stalls the filter
SEED = "#0F766E"
MUTED = ft.Colors.ON_SURFACE_VARIANT
GROUPS = {  # Clean page sections, in order
    "System": ft.Icons.COMPUTER,
    "Browsers": ft.Icons.PUBLIC,
    "Apps": ft.Icons.WIDGETS_OUTLINED,
    "Developer tools": ft.Icons.CODE,
    "Recycle Bin": ft.Icons.DELETE_OUTLINE,
}


def _toast(page: ft.Page, message: str) -> None:
    page.show_dialog(ft.SnackBar(ft.Text(message), show_close_icon=True))


def _confirm(page: ft.Page, title: str, body: str | ft.Control, action: str, on_yes, danger: bool = False) -> None:
    def yes(_):
        page.pop_dialog()
        on_yes()

    content = body if isinstance(body, ft.Control) else ft.Text(body, font_family=MONO, size=12, selectable=True)
    style = ft.ButtonStyle(bgcolor=ft.Colors.ERROR, color=ft.Colors.ON_ERROR) if danger else None
    page.show_dialog(
        ft.AlertDialog(
            modal=True,
            icon=ft.Icon(ft.Icons.WARNING_AMBER),
            title=ft.Text(title),
            content=content,
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                ft.FilledButton(action, on_click=yes, style=style),
            ],
        )
    )


def _worker(page: ft.Page, busy: ft.ProgressBar, job, *args) -> None:
    """Run job off the UI thread, unless this panel already has a job running."""
    if busy.visible:
        _toast(page, "Still working on the last one.")
        return
    # Claimed here, on the caller's thread, not inside run(): set there, a
    # second call made right after this one found the panel still idle.
    busy.value = None  # indeterminate until the job reports progress
    busy.visible = True
    page.update()

    def run() -> None:
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
    """A row holding an expanding control: shrinks rather than wraps."""
    return ft.Row(list(controls), spacing=8)


def _card(*controls: ft.Control, spacing: float = 12, bgcolor=ft.Colors.SURFACE_CONTAINER_LOW) -> ft.Container:
    return ft.Container(
        ft.Column(list(controls), spacing=spacing, tight=True),
        padding=16,
        border_radius=16,
        bgcolor=bgcolor,
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
    )


def _header(title: str, subtitle: ft.Text | str) -> ft.Control:
    if isinstance(subtitle, str):
        subtitle = _muted(subtitle)
    return ft.Column([ft.Text(title, size=24, weight=ft.FontWeight.W_700), subtitle], spacing=0, tight=True)


def _muted(value: str, **kwargs) -> ft.Text:
    return ft.Text(value, size=12, color=MUTED, **kwargs)


def _admin_banner(page: ft.Page) -> ft.Control | None:
    if is_admin():
        return None
    text = ft.Text(
        "Some system items (Windows temp, Update downloads, error reports, logs) and restore points"
        " need administrator rights, so they are skipped.",
        size=12,
        expand=True,
    )
    if OS == "windows":

        def elevate(_) -> None:
            if relaunch_as_admin():
                _toast(page, "An elevated Cleam is starting. Close this window.")
            else:
                _toast(page, "Windows refused the elevation prompt.")

        action: ft.Control = ft.Button("Restart as administrator", icon=ft.Icons.SHIELD, on_click=elevate)
    else:
        action = ft.Text("Start Cleam with sudo to include them.", size=13, italic=True)
    # One row, not a block: it sits above content that has to fit one screen.
    return ft.Container(
        _field_row(ft.Icon(ft.Icons.ADMIN_PANEL_SETTINGS), text, action),
        padding=10,
        border_radius=12,
        bgcolor=ft.Colors.TERTIARY_CONTAINER,
    )


# ---------------------------------------------------------------- Overview


def _overview_page(page: ft.Page, on_scan) -> tuple[ft.Control, object]:
    os_line = _muted("")
    disks = ft.Row(wrap=True, spacing=10, run_spacing=10)  # fixed-width tiles only: it wraps
    folder_rows = ft.Column(spacing=0)
    busy = ft.ProgressBar(visible=False, border_radius=2)
    found: list[overview.Folder] = []

    def disk_tile(disk: overview.Disk) -> ft.Control:
        pct = disk.percent_used
        color = ft.Colors.ERROR if pct >= 90 else ft.Colors.TERTIARY if pct >= 75 else ft.Colors.PRIMARY
        ring = ft.Stack(
            [
                ft.ProgressRing(value=pct / 100, width=44, height=44, stroke_width=5,
                                stroke_cap=ft.StrokeCap.ROUND, color=color,
                                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                                semantics_label=f"{disk.mount} {pct}% used"),
                ft.Container(ft.Text(f"{pct}%", size=11, weight=ft.FontWeight.W_700),
                             alignment=ft.Alignment.CENTER, width=44, height=44),
            ],
            width=44,
            height=44,
        )
        return ft.Container(
            # Short lines: "24.0 GiB free of 100.3 GiB" was cut off at this width.
            ft.Row([ring, ft.Column([ft.Text(disk.mount, weight=ft.FontWeight.W_600),
                                     ft.Text(f"{human(disk.free)} free", size=12),
                                     _muted(f"of {human(disk.total)}")],
                                    spacing=0, tight=True)], spacing=10),
            width=190,
            padding=10,
            border_radius=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            tooltip=f"{disk.mount}: {human(disk.used)} used of {human(disk.total)}",
        )

    def folder_row(folder: overview.Folder, biggest: int, measuring: bool = False) -> ft.Control:
        size: ft.Control = (
            ft.ProgressRing(width=14, height=14, stroke_width=2)
            if measuring
            else ft.Text(folder.size_label, weight=ft.FontWeight.W_600, color=None if folder.measured else MUTED)
        )
        # Two lines at most: the path and file count live in the tooltip, the
        # "is this normal" note stays on screen -- it is the point of the page.
        lines: list[ft.Control] = [
            _field_row(ft.Text(folder.label, weight=ft.FontWeight.W_600, expand=True), size)
        ]
        if folder.measured and biggest:
            lines.append(ft.ProgressBar(value=folder.bytes / biggest, bar_height=4, border_radius=2))
        if folder.note:
            lines.append(_muted(folder.note, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS))
        return ft.Container(ft.Column(lines, spacing=3, tight=True), padding=ft.Padding.symmetric(vertical=6),
                            tooltip=folder.detail)

    def render(measuring: int = -1) -> None:
        biggest = max((f.bytes for f in found if f.measured), default=0)
        folder_rows.controls = [folder_row(f, biggest, i == measuring) for i, f in enumerate(found)]

    def load() -> None:
        """Instant: the OS line, the disks, and the folder list unmeasured."""
        found[:] = overview.folders()
        os_line.value = overview.os_name()
        disks.controls = [disk_tile(d) for d in overview.disks()]
        # Not measured here. On Windows these folders are hundreds of thousands
        # of files, and walking them the moment the window opens is disk load
        # nobody asked for.
        render()
        page.update()

    def measure() -> None:
        # Rebuilt from `found` each step (never reconciled by object identity),
        # so a second job, had one slipped past _worker, could not corrupt it.
        for i, folder in enumerate(found):
            busy.value = i / len(found)
            render(measuring=i)
            page.update()
            overview.measure(folder)
        render()
        page.update()

    scan_card = _card(
        _field_row(
            ft.Icon(ft.Icons.CLEANING_SERVICES, color=ft.Colors.ON_PRIMARY_CONTAINER),
            ft.Column([ft.Text("Free up space", size=16, weight=ft.FontWeight.W_600),
                       ft.Text("Caches, temp files, crash dumps, old logs. Scanning only reads.", size=12)],
                      spacing=0, tight=True, expand=True),
            ft.FilledButton("Scan", icon=ft.Icons.SEARCH, on_click=lambda _: on_scan()),
        ),
        bgcolor=ft.Colors.PRIMARY_CONTAINER,
    )
    folders_card = _card(
        _field_row(
            ft.Column([ft.Text("Where the space went", size=16, weight=ft.FontWeight.W_600),
                       _muted("What is normal, and what shrinks it.")], spacing=0, tight=True, expand=True),
            ft.Button("Measure", icon=ft.Icons.STRAIGHTEN, on_click=lambda _: _worker(page, busy, measure)),
        ),
        busy,
        folder_rows,
        spacing=6,
    )
    # Two columns side by side on a normal window, so the page fits without
    # scrolling; stacked below 768 px, where scrolling is the lesser evil.
    body = ft.ResponsiveRow(
        [
            ft.Column([disks, scan_card], spacing=12, col={"xs": 12, "md": 5}),
            ft.Column([folders_card], col={"xs": 12, "md": 7}),
        ],
        spacing=16,
        run_spacing=12,
    )
    control = ft.Column(
        [_header("Overview", os_line), body],
        spacing=12,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )
    return control, load


# ---------------------------------------------------------------- Clean


def _clean_page(page: ft.Page) -> tuple[ft.Control, object, object]:
    busy = ft.ProgressBar(visible=False, border_radius=2)
    headline = ft.Text("Not scanned yet", size=28, weight=ft.FontWeight.W_700)
    detail = _muted("Scanning only reads. Nothing is deleted until you confirm.")
    status = _muted("")
    outcome = ft.Container(visible=False, padding=10, border_radius=12)
    groups_col = ft.Column(spacing=0)
    footnote = _muted("")
    snapshot_first = ft.Switch(label="Restore point first", value=False)
    # (target, result or None before it is scanned, checkbox). The target list
    # is captured once per scan and reused for the dialog and the delete.
    # Rebuilding it per click re-ran `whoami` and probed every drive letter,
    # and could hand the delete a root that was never shown.
    rows: list[tuple[junk.Target, junk.Result | None, ft.Checkbox]] = []
    missing: list[junk.Target] = []
    group_boxes: list[tuple[ft.Checkbox, list[ft.Checkbox]]] = []  # (section checkbox, its live rows)
    # Groups start folded so the page fits on one screen; one the user opened
    # stays open while a scan re-renders the list.
    opened: set[str] = set()

    def selected() -> list[tuple[junk.Target, junk.Result | None, ft.Checkbox]]:
        return [row for row in rows if row[2].value and row[1] and row[1].files]

    def totals() -> None:
        found = sum(r.bytes for _, r, _ in rows if r)
        chosen = sum(r.bytes for _, r, _ in selected() if r)
        if any(r for _, r, _ in rows):
            headline.value = human(found) if found else "All clean"
            files = sum(r.files for _, r, _ in rows if r)
            detail.value = f"found in {files:,} files · {human(chosen)} selected" if found else "Nothing to remove."
        clean_button.disabled = not chosen
        clean_button.content = f"Clean {human(chosen)}" if chosen else "Clean selected"
        for header, live in group_boxes:
            ticked = sum(1 for box in live if box.value)
            # tristate only while mixed: a tristate box whose value is False
            # (Flet's default, so never sent) rendered as the mixed dash.
            header.tristate = 0 < ticked < len(live)
            header.value = None if header.tristate else bool(live) and ticked == len(live)

    def box_for(target: junk.Target, result: junk.Result | None) -> ft.Checkbox:
        nothing = result is None or bool(result.skipped) or result.files == 0
        return ft.Checkbox(
            # The label belongs to the checkbox, not to a separate title: a
            # screen reader announcing "checkbox, unchecked" with the name
            # somewhere else is useless.
            label=target.label,
            value=not nothing and not target.opt_in,  # the Recycle Bin is never pre-selected
            disabled=nothing,
            on_change=on_tick,
        )

    def on_tick(_=None) -> None:
        # Totals and section boxes only. Rebuilding the rows here would take
        # keyboard focus off the checkbox that was just toggled.
        totals()
        page.update()

    def state_of(target: junk.Target, result: junk.Result | None, current: bool) -> str:
        if current:
            return "scanning…"
        if result is None:
            return "not scanned yet"
        if result.skipped:
            return result.skipped
        if not result.files:
            return "nothing to clean"
        state = f"{result.files:,} files"
        if result.errors:
            state += f", {result.errors} in use"
        return state + (" · tick to include" if target.opt_in else "")

    def target_row(target: junk.Target, result: junk.Result | None, box: ft.Checkbox, current: bool) -> ft.Control:
        has = bool(result and result.files)
        size: ft.Control = (
            ft.ProgressRing(width=14, height=14, stroke_width=2) if current
            else ft.Text(human(result.bytes) if has else "—", weight=ft.FontWeight.W_600 if has else None,
                         color=None if has else MUTED)
        )
        return ft.ListTile(
            title=box,
            subtitle=ft.Container(  # indented to sit under the label, not under the box
                _muted(f"{state_of(target, result, current)} — {target.about}", max_lines=2,
                       overflow=ft.TextOverflow.ELLIPSIS),
                padding=ft.Padding.only(left=34),
            ),
            trailing=size,
            dense=True,
            content_padding=ft.Padding.only(left=8, right=8),
            tooltip=target.about,
        )

    def group_box(group: str, members: list) -> ft.Checkbox:
        live = [box for _, _, box in members if not box.disabled]

        def toggle(_) -> None:
            # Decided from the rows, not from the header's new value: Flutter
            # cycles a tristate box false -> true -> null, which says nothing
            # useful about what the click meant.
            on = not all(box.value for box in live)
            for box in live:
                box.value = on
            on_tick()

        header = ft.Checkbox(label=group, label_style=ft.TextStyle(weight=ft.FontWeight.W_600),
                             disabled=not live, on_change=toggle)
        group_boxes.append((header, live))
        return header

    def render(current: int = -1) -> None:
        groups_col.controls.clear()
        group_boxes.clear()
        for group, icon in GROUPS.items():
            members = [(i, row) for i, row in enumerate(rows) if row[0].group == group]
            if not members:
                continue
            size = sum(r.bytes for _, (_, r, _) in members if r)
            with_junk = sum(1 for _, (_, r, _) in members if r and r.files)
            scanning = any(i == current for i, _ in members)

            def fold(e, g=group) -> None:
                (opened.add if e.data in (True, "true") else opened.discard)(g)

            groups_col.controls.append(ft.ExpansionTile(
                leading=ft.Icon(icon, color=ft.Colors.PRIMARY),
                title=_field_row(
                    group_box(group, [row for _, row in members]),
                    ft.Container(expand=True),
                    ft.ProgressRing(width=14, height=14, stroke_width=2) if scanning
                    else ft.Text(human(size) if size else "", weight=ft.FontWeight.W_600),
                ),
                subtitle=_muted(f"{len(members)} place{'s' * (len(members) != 1)}"
                                + (f", {with_junk} with something to remove"
                                                             if with_junk else "")),
                controls=[target_row(*row, current=(i == current)) for i, row in members],
                expanded=group in opened,
                maintain_state=True,
                on_change=fold,
                dense=True,
            ))
        clean = [t.label for t, r, _ in rows if r and not r.skipped and not r.files]
        parts = []
        if clean:
            parts.append(f"{len(clean)} places already clean")
        if missing:
            parts.append(f"{len(missing)} not on this computer")
        footnote.value = " · ".join(parts)
        footnote.tooltip = "\n".join(
            ([f"Already clean: {', '.join(clean)}"] if clean else [])
            + ([f"Not on this computer: {', '.join(t.label for t in missing)}"] if missing else [])
        ) or None
        totals()

    def split(found: list[junk.Target]) -> list[junk.Target]:
        missing[:] = [t for t in found if not junk.present(t)]
        return [t for t in found if junk.present(t)]

    def preview() -> None:
        """What a scan will look at, before anything is walked."""
        status.value = "Looking for what is installed…"
        page.update()
        rows[:] = [(t, None, box_for(t, None)) for t in split(junk.targets())]
        status.value = f"{len(rows)} places to check on this computer."
        render()
        page.update()

    def scan(keep_outcome: bool = False) -> None:
        outcome.visible = outcome.visible and keep_outcome  # a new scan makes an old "Freed" stale
        status.value = "Looking for what is installed…"
        page.update()
        rows[:] = [(t, None, box_for(t, None)) for t in split(junk.targets())]
        started = time.monotonic()
        for i, (target, _, _) in enumerate(rows):
            busy.value = i / max(1, len(rows))
            status.value = f"Scanning {target.label}… ({i + 1} of {len(rows)})"
            render(current=i)
            page.update()
            result = junk.run(target)
            rows[i] = (target, result, box_for(target, result))
        status.value = f"Checked {len(rows)} places in {time.monotonic() - started:.0f} s."
        render()
        page.update()

    def show_outcome(message: str, tone: str) -> None:
        """tone: ok (something freed), warn (nothing could be), bad (nothing was tried)."""
        bg, fg, icon = {
            "ok": (ft.Colors.PRIMARY_CONTAINER, ft.Colors.ON_PRIMARY_CONTAINER, ft.Icons.CHECK_CIRCLE),
            "warn": (ft.Colors.TERTIARY_CONTAINER, ft.Colors.ON_TERTIARY_CONTAINER, ft.Icons.WARNING_AMBER),
            "bad": (ft.Colors.ERROR_CONTAINER, ft.Colors.ON_ERROR_CONTAINER, ft.Icons.ERROR_OUTLINE),
        }[tone]
        outcome.bgcolor = bg
        outcome.content = _field_row(ft.Icon(icon, color=fg), ft.Text(message, color=fg, expand=True))
        outcome.visible = True

    def clean(chosen: list[tuple[junk.Target, junk.Result | None, ft.Checkbox]]) -> None:
        outcome.visible = False
        if snapshot_first.value:
            status.value = "Creating a restore point… (Windows takes a minute or two)"
            page.update()
            code, text = snapshot.create("Cleam: before clean", capture=True)
            if code != 0:
                show_outcome(f"Nothing deleted: the restore point failed. {text}", "bad")
                status.value = ""
                page.update()
                return
        freed = errors = 0
        for i, (target, _, _) in enumerate(chosen):
            busy.value = i / len(chosen)
            status.value = f"Cleaning {target.label}… ({i + 1} of {len(chosen)})"
            page.update()
            result = junk.run(target, delete=True)
            freed += result.bytes
            errors += result.errors
        if freed:
            message = f"Freed {human(freed)}."
            if errors:
                message += f" {errors:,} files were in use and stayed."
            show_outcome(message, "ok")
        elif errors:
            # Not a success: "Freed 0 B" on a green banner read as one.
            show_outcome(f"Nothing could be removed: {errors:,} files are in use. Close the programs using them"
                         " (browsers, games, the app itself) and clean again.", "warn")
        else:
            show_outcome("Nothing was left to remove.", "ok")
        scan(keep_outcome=True)  # re-scan, so the list shows what is actually left

    def ask_clean(_) -> None:
        if busy.visible:  # mid-scan the button is live, but the list is not final
            _toast(page, "Wait for the scan to finish.")
            return
        chosen = selected()
        if not chosen:
            _toast(page, "Tick what to clean first.")
            return
        lines: list[ft.Control] = [
            _field_row(ft.Text(t.label, expand=True), ft.Text(human(r.bytes), weight=ft.FontWeight.W_600))
            for t, r, _ in chosen
            if r
        ]
        lines += [
            ft.Divider(height=1),
            _field_row(
                ft.Text("Total", weight=ft.FontWeight.W_600, expand=True),
                ft.Text(human(sum(r.bytes for _, r, _ in chosen if r)), weight=ft.FontWeight.W_700),
            ),
        ]
        if any(t.group == "Recycle Bin" for t, _, _ in chosen):
            lines.append(ft.Text("Includes the Recycle Bin: those files will be gone for good.", color=ft.Colors.ERROR))
        lines.append(_muted("This cannot be undone. Files in use are skipped."))
        _confirm(
            page,
            "Delete these files?",
            ft.Column(lines, spacing=8, tight=True, width=420, scroll=ft.ScrollMode.AUTO),
            "Delete",
            lambda: _worker(page, busy, clean, chosen),
            danger=True,
        )

    def start_scan() -> None:
        _worker(page, busy, scan)

    clean_button = ft.FilledButton("Clean selected", icon=ft.Icons.DELETE_SWEEP, disabled=True, on_click=ask_clean)
    summary = _card(
        ft.ResponsiveRow(
            [
                ft.Column([headline, detail], spacing=0, tight=True, col={"xs": 12, "md": 5}),
                ft.Container(
                    _buttons(ft.FilledTonalButton("Scan", icon=ft.Icons.SEARCH, on_click=lambda _: start_scan()),
                             clean_button, snapshot_first),
                    col={"xs": 12, "md": 7},
                    alignment=ft.Alignment.CENTER_RIGHT,
                ),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            run_spacing=8,
        ),
        busy,
        status,
        spacing=6,
    )
    groups_card = ft.Container(
        groups_col,
        border_radius=16,
        bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
    )
    controls: list[ft.Control] = [
        _header("Clean", "Caches, temp files and crash leftovers that are safe to remove."),
        summary,
        outcome,
        groups_card,
        footnote,
    ]
    if banner := _admin_banner(page):
        controls.insert(1, banner)
    control = ft.Column(
        controls, spacing=12, scroll=ft.ScrollMode.AUTO, expand=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH
    )
    return control, start_scan, lambda: _worker(page, busy, preview)


# ---------------------------------------------------------------- Programs


def _initials(name: str) -> str:
    words = [w for w in name.replace("-", " ").split() if w[:1].isalnum()]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def _programs_page(page: ft.Page) -> tuple[ft.Control, object]:
    listing = ft.ListView(expand=True, spacing=0)
    status = _muted("Reading installed programs…")
    busy = ft.ProgressBar(visible=False, border_radius=2)
    installed: list[apps_module.App] = []

    def show(_=None) -> None:
        needle = (search.value or "").lower()
        matches = [a for a in installed if needle in a.name.lower()]
        listing.controls = [row(a) for a in matches[:APP_ROWS]]
        if len(matches) > APP_ROWS:
            listing.controls.append(ft.ListTile(title=_muted(f"+{len(matches) - APP_ROWS} more. Type to narrow the list.")))
        status.value = f"{len(matches)} of {len(installed)} programs" if installed else "No programs found."
        page.update()

    def row(app: apps_module.App) -> ft.Control:
        return ft.ListTile(
            leading=ft.CircleAvatar(
                content=ft.Text(_initials(app.name), size=13, weight=ft.FontWeight.W_600),
                bgcolor=ft.Colors.SECONDARY_CONTAINER,
                color=ft.Colors.ON_SECONDARY_CONTAINER,
            ),
            title=ft.Text(app.name, weight=ft.FontWeight.W_500),
            subtitle=_muted(" · ".join(part for part in (app.version, app.source) if part)),
            trailing=ft.OutlinedButton("Uninstall", on_click=lambda _, a=app: ask(a)),
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
        # An uninstaller takes the program and leaves the settings folder, the
        # publisher's registry key and the Start Menu shortcut behind.
        offer_leftovers(app)

    def offer_leftovers(app: apps_module.App) -> None:
        install_dir = app.command if isinstance(app.command, str) else ""
        found = leftovers_module.scan(
            app.name,
            install_dir=install_dir,
            other_apps=tuple(a.name for a in installed if a.id != app.id),
        )
        if not found:
            return
        boxes = [
            (item, ft.Checkbox(label=f"{item.target}  ({human(item.bytes)})", value=item.confidence == "high"))
            for item in found
        ]
        body = ft.Column(
            [ft.Text(f"{app.name} left these behind. Unticked ones are uncertain matches.", size=12)]
            + [box for _, box in boxes],
            tight=True,
            scroll=ft.ScrollMode.AUTO,
        )

        def wipe(_) -> None:
            page.pop_dialog()
            chosen = [item for item, box in boxes if box.value]
            if chosen:
                _worker(page, busy, clear_leftovers, app, chosen)

        page.show_dialog(
            ft.AlertDialog(
                modal=True,
                icon=ft.Icon(ft.Icons.CLEANING_SERVICES),
                title=ft.Text("Remove what was left behind?"),
                content=body,
                actions=[
                    ft.TextButton("Keep them", on_click=lambda _: page.pop_dialog()),
                    ft.FilledButton("Move to backup", on_click=wipe),
                ],
            )
        )

    def clear_leftovers(app: apps_module.App, chosen: list) -> None:
        backup, errors = leftovers_module.remove(chosen, label=app.name)
        # Moved, not deleted: the backup path is the undo, so say it out loud.
        message = f"Moved {len(chosen)} items to {backup}. Undo: cleam restore \"{backup}\""
        _toast(page, f"{message} ({len(errors)} could not be moved)" if errors else message)

    search = ft.TextField(
        hint_text="Search programs",
        prefix_icon=ft.Icons.SEARCH,
        dense=True,
        filled=True,
        border_radius=24,
        expand=True,
        on_change=show,
    )
    control = ft.Column(
        [
            _header("Programs", "Uninstall runs each program's own uninstaller, then offers to clear what it left."),
            _field_row(
                search,
                ft.IconButton(ft.Icons.REFRESH, tooltip="Refresh", on_click=lambda _: _worker(page, busy, load)),
            ),
            busy,
            status,
            ft.Container(
                listing,
                expand=True,
                border_radius=16,
                bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                padding=ft.Padding.symmetric(vertical=4),
            ),
        ],
        spacing=12,
        expand=True,
    )
    return control, load


# ---------------------------------------------------------------- Security

STATE_ICON = {
    security.OK: (ft.Icons.CHECK_CIRCLE, ft.Colors.PRIMARY),
    security.WARN: (ft.Icons.WARNING_AMBER, ft.Colors.TERTIARY),
    security.BAD: (ft.Icons.ERROR, ft.Colors.ERROR),
    security.UNKNOWN: (ft.Icons.HELP_OUTLINE, MUTED),
}
STATE_WORD = {security.OK: "OK", security.WARN: "Check", security.BAD: "Problem", security.UNKNOWN: "Unknown"}


def _state_row(state: str, title: str, lines: list[str], tooltip: str | None = None) -> ft.Control:
    icon, color = STATE_ICON[state]
    body: list[ft.Control] = [
        # The state is a word as well as a colour: "Problem", not only red.
        ft.Text(f"{title} · {STATE_WORD[state]}", weight=ft.FontWeight.W_600, size=13),
    ]
    body += [_muted(line, max_lines=3, overflow=ft.TextOverflow.ELLIPSIS) for line in lines if line]
    return ft.Container(
        _field_row(
            ft.Icon(icon, color=color, size=20, semantics_label=STATE_WORD[state]),
            ft.Column(body, spacing=1, tight=True, expand=True),
        ),
        padding=ft.Padding.symmetric(vertical=5),
        tooltip=tooltip,
    )


def _security_page(page: ft.Page) -> tuple[ft.Control, object]:
    busy = ft.ProgressBar(visible=False, border_radius=2)
    summary = ft.Text("Checking…", size=24, weight=ft.FontWeight.W_700)
    summary_detail = _muted("")
    checks_col = ft.Column(spacing=0)
    startup_col = ft.Column(spacing=0)
    ransom_col = ft.Column(spacing=0)
    ransom_status = _muted("Not checked yet. This only reads file names in your own folders.")
    current: list[list[security.Check]] = [[]]

    def render_checks() -> None:
        checks = current[0]
        bad = [c for c in checks if c.state == security.BAD]
        warn = [c for c in checks if c.state == security.WARN]
        summary.value = "Not protected" if bad else "Needs attention" if warn else "Protected"
        summary.color = ft.Colors.ERROR if bad else None
        summary_detail.value = (
            f"{len(bad)} problem(s), {len(warn)} to check." if bad or warn else "Nothing needs your attention."
        )
        checks_col.controls = [
            _state_row(c.state, c.label, [c.detail, c.fix and f"How to fix: {c.fix}"],
                       tooltip=c.fix or None)  # the whole fix, where three lines cut it short
            for c in checks
        ]
        scan_button.disabled = not security.can_quick_scan(checks)
        scan_button.tooltip = None if not scan_button.disabled else "Microsoft Defender is not running"

    def load() -> None:
        current[0] = security.status()
        render_checks()
        page.update()
        items = security.startup()
        flagged = sum(i.suspicious for i in items)
        startup_col.controls = [
            _muted(f"{len(items)} programs start with Windows; {flagged} look odd." if items else "Nothing found.")
        ] + [
            _state_row(
                security.WARN if i.suspicious else security.OK,
                i.name + ("" if i.enabled else " (switched off)") + (f" — {i.publisher}" if i.publisher else ""),
                i.reasons,
                tooltip=i.command,  # full command lines are long paths; hover shows them
            )
            for i in items
        ]
        page.update()

    def ransom() -> None:
        ransom_status.value = "Looking through Desktop, Documents, Downloads, Pictures, Videos, Music, OneDrive…"
        page.update()
        report = security.ransom_signs()
        found = report.findings()
        ransom_status.value = f"{report.files_checked:,} files checked" + (" (stopped early)" if report.truncated else "")
        ransom_col.controls = [
            _state_row(
                report.state,
                "Signs of ransomware found" if found else "No signs of ransomware",
                found or ["No ransom notes, no known encrypted-file extensions, no mass-renamed documents."],
            )
        ]
        page.update()

    def scan() -> None:
        summary_detail.value = "Microsoft Defender is running a quick scan…"
        page.update()
        code, text = security.quick_scan()
        _toast(page, text)
        current[0] = security.status()
        render_checks()
        page.update()

    scan_button = ft.FilledButton("Quick scan with Defender", icon=ft.Icons.SHIELD, disabled=True,
                                  on_click=lambda _: _worker(page, busy, scan))
    protection = _card(
        ft.Column([summary, summary_detail], spacing=0, tight=True),
        _buttons(scan_button, ft.Button("Check again", icon=ft.Icons.REFRESH,
                                        on_click=lambda _: _worker(page, busy, load))),
        busy,
        checks_col,
    )
    ransom_card = _card(
        _field_row(
            ft.Column([ft.Text("Ransomware", size=16, weight=ft.FontWeight.W_600), ransom_status],
                      spacing=2, tight=True, expand=True),
            ft.Button("Look for signs", icon=ft.Icons.SEARCH, on_click=lambda _: _worker(page, busy, ransom)),
        ),
        ransom_col,
    )
    startup_card = _card(
        ft.Text("Starts with Windows" if OS == "windows" else "Starts at login", size=16, weight=ft.FontWeight.W_600),
        _muted("Where malware keeps itself running. Odd ones first; hover for the command."),
        startup_col,
    )
    body = ft.ResponsiveRow(
        [
            ft.Column([protection, ransom_card], spacing=12, col={"xs": 12, "md": 7}),
            ft.Column([startup_card], col={"xs": 12, "md": 5}),
        ],
        spacing=16,
        run_spacing=12,
    )
    control = ft.Column(
        [
            _header("Security", "Cleam checks and explains; your antivirus removes threats. Nothing here deletes anything."),
            body,
        ],
        spacing=12,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )
    return control, lambda: _worker(page, busy, load)


# ---------------------------------------------------------------- Snapshots


SNAPSHOT_HELP = {
    "windows": "A restore point rolls Windows settings, drivers and programs back if something breaks."
    " Windows allows one every 24 hours.",
    "macos": "A local Time Machine snapshot, kept for about 24 hours.",
    "linux": "A Timeshift or Snapper snapshot of the system.",
}


def _snapshot_page(page: ft.Page) -> ft.Control:
    description = ft.TextField(label="Description", value="Cleam", dense=True, expand=True)
    out = ft.Text("Nothing listed yet.", font_family=MONO, size=12, selectable=True, color=MUTED)
    busy = ft.ProgressBar(visible=False, border_radius=2)

    def work(action: str) -> None:
        out.value = "Working…"
        page.update()
        code, text = (
            snapshot.list_snapshots(capture=True)
            if action == "list"
            else snapshot.create(description.value or "Cleam", capture=True)
        )
        out.value = text or ("Done" if code == 0 else f"Failed (exit {code})")
        out.color = None if code == 0 else ft.Colors.ERROR
        page.update()

    create_card = _card(
        ft.Text("Create", size=18, weight=ft.FontWeight.W_600),
        _field_row(
            description,
            ft.FilledButton("Create", icon=ft.Icons.BACKUP_OUTLINED, on_click=lambda _: _worker(page, busy, work, "create")),
        ),
    )
    list_card = _card(
        _field_row(
            ft.Text("Existing", size=18, weight=ft.FontWeight.W_600, expand=True),
            ft.Button("List", icon=ft.Icons.LIST, on_click=lambda _: _worker(page, busy, work, "list")),
        ),
        busy,
        ft.Container(
            out,
            padding=12,
            border_radius=8,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
        ),
    )
    controls: list[ft.Control] = [
        _header("Snapshots", SNAPSHOT_HELP.get(OS, "")),
        create_card,
        list_card,
    ]
    if banner := _admin_banner(page):
        controls.insert(1, banner)
    return ft.Column(
        controls, spacing=16, scroll=ft.ScrollMode.AUTO, expand=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH
    )


# ---------------------------------------------------------------- window


def main(page: ft.Page) -> None:
    page.title = f"Cleam {__version__}"
    # Both themes are set explicitly: with only `theme`, a machine in dark mode
    # fell back to an unstyled palette.
    page.theme = ft.Theme(color_scheme_seed=SEED)
    page.dark_theme = ft.Theme(color_scheme_seed=SEED)
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.padding = 0
    page.window.width, page.window.height = 1120, 780
    page.window.min_width, page.window.min_height = 640, 520

    def scan_from_overview() -> None:
        loaded.add(1)  # the scan replaces the preview; both would fight over one panel
        go(1)
        start_scan()

    overview_view, load_overview = _overview_page(page, scan_from_overview)
    clean_view, start_scan, preview_clean = _clean_page(page)
    security_view, load_security = _security_page(page)
    programs_view, load_programs = _programs_page(page)
    views = [overview_view, clean_view, security_view, programs_view, _snapshot_page(page)]
    # What a page loads the first time it is opened. Junk scans and folder
    # measurement are heavier, so those stay buttons the user presses.
    first_open = {1: preview_clean, 2: load_security, 3: lambda: page.run_thread(load_programs)}
    loaded: set[int] = set()
    body = ft.Container(views[0], expand=True, padding=ft.Padding.symmetric(horizontal=28, vertical=20))

    def go(index: int) -> None:
        rail.selected_index = index
        body.content = views[index]
        page.update()
        if index not in loaded:
            loaded.add(index)
            if index in first_open:
                first_open[index]()

    def toggle_theme(_) -> None:
        dark = page.theme_mode == ft.ThemeMode.DARK or (
            page.theme_mode == ft.ThemeMode.SYSTEM and page.platform_brightness == ft.Brightness.DARK
        )
        page.theme_mode = ft.ThemeMode.LIGHT if dark else ft.ThemeMode.DARK
        page.update()

    rail = ft.NavigationRail(
        selected_index=0,
        label_type=ft.NavigationRailLabelType.ALL,
        min_width=88,
        group_alignment=-0.85,
        leading=ft.Container(
            ft.Column(
                [
                    ft.Icon(ft.Icons.CLEANING_SERVICES, color=ft.Colors.PRIMARY, size=28),
                    ft.Text("Cleam", weight=ft.FontWeight.W_700),
                ],
                spacing=2,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.only(top=12, bottom=12),
        ),
        trailing=ft.IconButton(ft.Icons.DARK_MODE_OUTLINED, tooltip="Light / dark", on_click=toggle_theme),
        destinations=[
            ft.NavigationRailDestination(
                icon=ft.Icons.SPACE_DASHBOARD_OUTLINED, selected_icon=ft.Icons.SPACE_DASHBOARD, label="Overview"
            ),
            ft.NavigationRailDestination(
                icon=ft.Icons.CLEANING_SERVICES_OUTLINED, selected_icon=ft.Icons.CLEANING_SERVICES, label="Clean"
            ),
            ft.NavigationRailDestination(icon=ft.Icons.SHIELD_OUTLINED, selected_icon=ft.Icons.SHIELD, label="Security"),
            ft.NavigationRailDestination(icon=ft.Icons.APPS_OUTLINED, selected_icon=ft.Icons.APPS, label="Programs"),
            ft.NavigationRailDestination(icon=ft.Icons.HISTORY, selected_icon=ft.Icons.RESTORE, label="Snapshots"),
        ],
        on_change=lambda e: go(e.control.selected_index),
    )
    page.add(ft.Row([rail, ft.VerticalDivider(width=1), body], expand=True, spacing=0))
    load_overview()  # cheap: no folder walking, so the first screen is never blank


def run() -> None:
    ft.run(main)
