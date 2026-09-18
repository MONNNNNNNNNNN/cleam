# UX research, personas, scenarios and test cases

Cleam deletes files and uninstalls software, so its interface has one job
before it has any other: never let someone destroy something they did not
choose to destroy, and never leave them guessing what a number means. This
plan is the checklist that keeps that honest.

Test cases marked **[run]** were executed against the running app (Flet in web
mode, driven by headless Chromium, screenshots inspected). Cases marked
**[read]** were traced through the code only — the Windows and macOS paths
cannot execute on the Linux development box.

## Personas

**P1 — Ploy, 19, second-year DME student.** Windows 11 laptop, 256 GB SSD,
9 GB free, Windows Update refuses to install. Non-technical: has never opened
a terminal, does not know what AppData is. Has heard that "cleaner" apps can
break your PC and is scared of that. Success = more free space and certainty
that nothing of hers was deleted.

**P2 — Mon, engineer.** Ubuntu server and a Windows desktop. Wants
`cleam scan --json` in a cron job and a one-line answer to "what is eating
the disk". Will run elevated deliberately. Success = no prompts, no window,
exit codes that mean something.

**P3 — Somchai, 45, office PC on a standard (non-admin) account.** 40 apps
installed, most of them preinstalled bloat. Cannot elevate without calling IT.
Success = removes what he is allowed to remove, and is told plainly when
something needs IT rather than being shown a silent failure.

**P4 — Nut, low vision.** 200% display scaling, high-contrast preference,
navigates by keyboard and screen reader. Success = every control is labelled
and reachable, and nothing depends on colour alone.

## Scenarios and test cases

### S1 — Ploy opens Cleam for the first time to free space (P1)

| ID | Test | Expected |
|---|---|---|
| TC1 **[run]** | Open the app cold | Something readable appears within ~2s; no unrequested disk churn |
| TC2 **[run]** | Read the Overview | She can tell whether her folder sizes are normal without knowing what AppData is |
| TC3 **[run]** | Press "Clean selected" before scanning | The app does not pretend to work; the button is unavailable until there is something to clean |
| TC4 **[run]** | Scan, then read the list | Each row says what it is, how big it is, and whether it is safe |
| TC5 **[read]** | Her Recycle Bin holds a file she may still want | Emptying the bin is never pre-selected for her |
| TC6 **[run]** | Confirm the delete | The dialog names every target and its size, and Cancel leaves everything untouched |
| TC7 **[read]** | A file is locked by a running app | It is reported as skipped, not as an error she must act on |

### S2 — Mon wants the disk answer from a terminal (P2)

| ID | Test | Expected |
|---|---|---|
| TC8 **[run]** | `cleam overview` | OS, build, disks and the big folders, with sizes that match `df` |
| TC9 **[run]** | `cleam biggest ~/.cache --top 5` | The actual culprits, biggest first |
| TC10 **[run]** | `cleam biggest . --top 0` / `--top -3` | Sensible output, never a silently truncated list |
| TC11 **[run]** | `cleam clean` with no flags | A dry run, clearly labelled, deleting nothing |

### S3 — Somchai removes bloat from a standard account (P3)

| ID | Test | Expected |
|---|---|---|
| TC12 **[run]** | Open Programs | He is told it is loading, not shown a blank panel |
| TC13 **[run]** | Filter for an app name | The list narrows without stalling |
| TC14 **[read]** | Uninstall an app whose uninstaller relaunches itself | The app does not hang forever waiting for output |
| TC15 **[read]** | A target needs admin | He is told so, and offered a way to elevate rather than a dead end |
| TC16 **[read]** | Create a restore point without admin | Refused with a reason, not a silent failure |

### S4 — Nut uses Cleam with scaling and a screen reader (P4)

| ID | Test | Expected |
|---|---|---|
| TC17 **[run]** | Narrow / scaled window (700×620) | No control is clipped or unreachable |
| TC18 **[run]** | System dark mode | Readable contrast, no white-on-white |
| TC19 **[read]** | Screen reader on the clean list | Every checkbox announces which target it belongs to |
| TC20 **[read]** | Status conveyed without colour | "needs admin" and "in use" are words, not just a colour |

### S5 — Anyone gets impatient (all personas)

| ID | Test | Expected |
|---|---|---|
| TC21 **[run]** | Press Refresh twice quickly | No broken rows, no lost state |
| TC22 **[run]** | Press Scan while a scan is running | Ignored or queued; never two walks fighting over the list |

## Results — first run (before fixes)

| ID | Result | What happened |
|---|---|---|
| TC1 | **FAIL** | `main()` measured every folder at startup. On Ploy's machine that is AppData (191,701 files) + Windows (123,279) + WinSxS + Users, unasked, before she has clicked anything |
| TC2 | PASS | Notes name the normal range and what shrinks each folder |
| TC3 | **FAIL** | "Clean selected" was enabled with nothing scanned; clicking produced only a toast |
| TC4 | PASS | Label, size, file count and note per row |
| TC5 | **FAIL** | Any target with files was pre-ticked, the Recycle Bin included |
| TC6 | PASS | Verified: dialog listed both targets with sizes; Cancel deleted nothing |
| TC7 | PASS | `12 in use or denied` on the `/tmp` row |
| TC8 | **FAIL (minor)** | `50% used` where `df` says `51%`: percentage was computed against total, not against used + free |
| TC9 | PASS | Found `ms-playwright` 1.6 GiB, `uv` 1.3 GiB |
| TC10 | **FAIL** | `--top 0` returned nothing; `--top -3` silently dropped the biggest entries |
| TC11 | PASS | "Dry run: nothing deleted" |
| TC12 | **FAIL** | Blank panel for the first second or so while the list loaded |
| TC13 | PASS (slow) | Rebuilds every row on every keystroke; fine at 69 apps, laggy at 300 |
| TC14 | **FAIL** | `subprocess.run(capture_output=True)` with no timeout; an uninstaller that relaunches itself holds the pipe and the thread never returns |
| TC15 | **FAIL** | Told, but with no way to elevate |
| TC16 | PASS | "Restore points need an elevated (Administrator) Cleam." |
| TC17 | **FAIL** | At 700px the button row was clipped — "Snapshot first" unreachable |
| TC18 | **FAIL** | No dark theme was set, so dark mode fell back to an unstyled palette |
| TC19 | **FAIL** | The checkbox was a bare `leading` control; its label lived in a separate `title` |
| TC20 | PASS | Both are words in the subtitle |
| TC21 | **FAIL** | `ValueError: ListTile(...) is not in list` in the server log; the row stayed "measuring…" forever |
| TC22 | **FAIL** | Same race: two scans mutating one list |

## Results — after the fixes in this pass

| ID | Result | Verified by |
|---|---|---|
| TC1 | PASS | Screenshot: startup shows the OS line and disks, every folder reads "not measured". No walk until "Measure folders" |
| TC3 | PASS | Screenshot: "Clean selected" greyed out, "Nothing scanned yet. Press Scan." |
| TC5 | PASS (unit) | `Target.opt_in`; `test_junk` asserts trash / recycle-bin is the only opt-in target. This box has no Trash folder, so it could not be shown on screen |
| TC8 | PASS | `cleam overview` prints 51% for `/`, matching `df`. `df` uses `ceil(used / (used + avail))`; measured 50.403% here, which `df` shows as 51% |
| TC10 | PASS (unit) | `--top 0` and `--top -3` both return the biggest entry |
| TC12 | PASS (code) | The status line is set before the read. The read finished in under 900 ms here, so the placeholder could not be photographed |
| TC13 | PASS | Screenshot: "69 of 69 programs"; the list caps at `APP_ROWS` with a "+N more" row |
| TC14 | PASS (unit) | `registry` uninstalls are never captured; a captured run that times out returns "no answer after 900s" |
| TC15 | PASS (code) | "Restart as Administrator" (ShellExecuteW runas) on Windows, "Start Cleam with sudo" elsewhere — visible in the screenshots. The Windows branch cannot run on this box |
| TC17 | PASS | Screenshot at 700×620: tabs, buttons, filter and description field all reachable, nothing clipped |
| TC18 | PASS | Screenshot with `colorScheme: dark`: readable contrast throughout |
| TC19 | PASS | Screenshot: each checkbox carries its target name as its own label |
| TC21/TC22 | PASS | Double-clicked "Measure folders" and "Scan"; no `ValueError` in the log (it was there before), totals consistent |
| TC16 | PASS | Screenshot: the Snapshots tab reports "No snapshot tool found. Install timeshift (sudo apt install timeshift) or snapper." |

### Regression caught during this pass

Wrapping the button rows (TC17) put an `expand=True` `TextField` inside a
wrapping row. Flutter cannot lay out an expanding child inside a `Wrap`, and
the whole Programs and Snapshots panel rendered as a grey block — worse than
the clipping it fixed. Fixed by splitting the helper in two: `_buttons()`
wraps fixed-width controls, `_field_row()` holds an expanding field and
shrinks instead. Both panels re-verified at 1100×900 light and 700×620 dark.

This is the argument for driving the real window rather than reading the
diff: the grey block produced no exception and no console error, so only a
screenshot could catch it.
