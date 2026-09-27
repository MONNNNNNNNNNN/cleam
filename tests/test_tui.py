import unittest

from cleam import security, tui


class FakeTerminal:
    """Plays a list of keys and keeps the last frame drawn."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.frame = []

    def draw(self, lines):
        self.frame = lines

    def key(self, timeout=None):
        return self.keys.pop(0)


def rows():
    return [
        tui.Row("Privacy", header=True),
        tui.Row("Advertising ID", checked=True, default=True),
        tui.Row("Location", default=False),
        tui.Row("Recall", disabled=True, status="Windows 11 only", default=True),
    ]


class Checklist(unittest.TestCase):
    def test_enter_returns_what_is_ticked(self):
        chosen = tui.checklist(FakeTerminal([tui.ENTER]), "t", "", rows())
        self.assertEqual([r.label for r in chosen], ["Advertising ID"])

    def test_space_ticks_the_highlighted_row_and_headers_are_skipped(self):
        # The cursor starts on the first real row, not on the header.
        chosen = tui.checklist(FakeTerminal([tui.DOWN, tui.SPACE, tui.ENTER]), "t", "", rows())
        self.assertEqual([r.label for r in chosen], ["Advertising ID", "Location"])

    def test_a_disabled_row_cannot_be_ticked_even_by_recommended(self):
        keys = [tui.DOWN, tui.DOWN, tui.SPACE, "a", tui.ENTER]
        chosen = tui.checklist(FakeTerminal(keys), "t", "", rows())
        self.assertEqual([r.label for r in chosen], ["Advertising ID"])

    def test_none_then_escape_returns_nothing(self):
        self.assertIsNone(tui.checklist(FakeTerminal(["n", tui.ESC]), "t", "", rows()))

    def test_the_highlighted_rows_explanation_is_on_screen(self):
        r = rows()
        r[1].detail = "Apps can no longer use a per-user ID"
        term = FakeTerminal([tui.ENTER])
        tui.checklist(term, "t", "", r)
        self.assertTrue(any("per-user ID" in line for line in term.frame))

    def test_confirm_needs_an_explicit_y(self):
        self.assertTrue(tui.confirm(FakeTerminal(["y"]), "t", ["x"], "Go?"))
        for key in (tui.ENTER, tui.SPACE, "n", tui.ESC):
            self.assertFalse(tui.confirm(FakeTerminal([key]), "t", ["x"], "Go?"), key)


class Mouse(unittest.TestCase):
    """Rows are drawn from screen line 2 (line 0 is the title bar), the
    button bar is the last line, its first button starting at column 1."""

    def test_clicking_a_row_ticks_it(self):
        r = rows()
        term = FakeTerminal([tui.Click(6, 4), tui.ENTER])  # line 4 = "Location"
        chosen = tui.checklist(term, "t", "", r)
        self.assertEqual([x.label for x in chosen], ["Advertising ID", "Location"])

    def test_clicking_a_group_title_ticks_the_whole_group_but_not_disabled_rows(self):
        term = FakeTerminal([tui.Click(3, 2), tui.ENTER])  # line 2 = "PRIVACY" header
        chosen = tui.checklist(term, "t", "", rows())
        self.assertEqual([x.label for x in chosen], ["Advertising ID", "Location"])

    def test_the_continue_button_is_clickable(self):
        class Clicker(FakeTerminal):
            def key(self):
                return tui.Click(3, len(self.frame) - 1)  # first button, on the last line drawn

        chosen = tui.checklist(Clicker([]), "t", "", rows())
        self.assertEqual([x.label for x in chosen], ["Advertising ID"])

    def test_the_wheel_moves_the_cursor(self):
        term = FakeTerminal([tui.Wheel(3), tui.SPACE, tui.ENTER])  # to the last selectable row: disabled
        chosen = tui.checklist(term, "t", "", rows())
        self.assertEqual([x.label for x in chosen], ["Advertising ID"])

    def test_confirm_yes_button(self):
        class Clicker(FakeTerminal):
            def key(self):
                return tui.Click(3, len(self.frame) - 1)  # "[ Yes ]" comes first

        self.assertTrue(tui.confirm(Clicker([]), "t", ["x"], "Go?"))


class ProgressBar(unittest.TestCase):
    def test_bar_fills_with_the_steps_done(self):
        plain = lambda d, t: tui.ANSI.sub("", tui.progress_bar(d, t, 60))
        self.assertIn(" 50%  1 of 2", plain(1, 2))
        self.assertIn("100%", plain(3, 3))
        self.assertIn("0%", plain(0, 0))  # no division by zero on an empty job

    def test_progress_draws_the_current_step(self):
        term = FakeTerminal([])
        p = tui.Progress(term, "Debloat", 2)
        p.step("Turn off the advertising ID")
        self.assertTrue(any("Turn off the advertising ID" in line for line in term.frame))
        self.assertTrue(any("0 of 2" in line for line in term.frame))
        p.add("done")
        self.assertTrue(any("1 of 2" in line for line in term.frame))


class Look(unittest.TestCase):
    def test_panel_edges_line_up_at_every_width(self):
        for width in (60, 81, 120):
            widths = {len(tui.ANSI.sub("", line)) for line in tui.panel("About", ["text", "more"], width)}
            self.assertEqual(widths, {width}, width)

    def test_every_symbol_set_has_every_icon(self):
        for name, g in tui.GLYPH_SETS.items():
            self.assertEqual(set(g.icons), set(tui.ICONS), name)

    def test_console_set_uses_only_glyphs_the_console_fonts_have(self):
        # Checked against consola.ttf and lucon.ttf: no fallback in the classic console.
        allowed = set("■√·►×○♦¤≈∞▬±♣▼░◘◄↑█─│┌┐└┘▌!  ")
        g = tui.GLYPH_SETS["console"]
        used = set("".join([g.on, g.off, g.done, g.na, g.cursor, g.ok, g.fail, g.warn, g.full, g.empty,
                            g.tl, g.tr, g.bl, g.br, g.h, g.v, g.sep] + list(g.icons.values())))
        self.assertLessEqual(used, allowed, used - allowed)

    def test_banner_uses_only_block_and_box_characters(self):
        self.assertLessEqual(set("".join(tui.BANNER)), set("█╗╔║═╝╚ "))


class Sync(unittest.TestCase):
    """Debloat rows mirror live state: was == checked at load; only changes return."""

    def rows(self):
        return [
            tui.Row("Privacy", header=True),
            tui.Row("Telemetry off", checked=True, was=True, status="on", default=True),
            tui.Row("Location off", checked=False, was=False, status="off", default=False),
            tui.Row("Ads off", checked=False, was=False, status="off", default=True),
        ]

    def test_nothing_changed_returns_nothing_to_do(self):
        self.assertEqual(tui.checklist(FakeTerminal([tui.ENTER]), "t", "", self.rows()), [])

    def test_unticking_something_on_is_a_turn_off(self):
        chosen = tui.checklist(FakeTerminal([tui.SPACE, tui.ENTER]), "t", "", self.rows())
        self.assertEqual([(r.label, r.checked) for r in chosen], [("Telemetry off", False)])

    def test_recommended_adds_but_never_drops_what_is_on(self):
        chosen = tui.checklist(FakeTerminal(["a", tui.ENTER]), "t", "", self.rows())
        self.assertEqual([r.label for r in chosen], ["Ads off"])

    def test_as_is_resets_to_the_system_state(self):
        chosen = tui.checklist(FakeTerminal([tui.SPACE, tui.DOWN, tui.SPACE, "n", tui.ENTER]), "t", "", self.rows())
        self.assertEqual(chosen, [])

    def test_row_shows_state_and_pending_change(self):
        on, off = self.rows()[1], self.rows()[2]
        self.assertIn("ON", tui.ANSI.sub("", tui.row_line(on, False, 100, 24)))
        self.assertIn("off", tui.ANSI.sub("", tui.row_line(off, False, 100, 24)))
        off.checked = True
        self.assertIn("turn on", tui.ANSI.sub("", tui.row_line(off, False, 100, 24)))


class Hover(unittest.TestCase):
    """Menu items are drawn from line 2: two lines each (name, about)."""

    ITEMS = [("debloat", "Debloat", "a"), ("clean", "Clean junk", "b"), ("security", "System check", "c")]

    def test_pointing_at_an_item_highlights_it_without_opening_it(self):
        term = FakeTerminal([tui.Hover(10, 4), tui.ESC])  # line 4: "Clean junk"
        self.assertIsNone(tui.menu(term, "t", self.ITEMS, []))
        highlighted = [line for line in term.frame if tui.G.cursor in tui.ANSI.sub("", line)]
        self.assertTrue(any("Clean junk" in line for line in highlighted), highlighted)

    def test_hovering_the_about_line_counts_and_a_click_opens(self):
        self.assertEqual(tui.menu(FakeTerminal([tui.Hover(10, 7), tui.ENTER]), "t", self.ITEMS, []), 2)
        self.assertEqual(tui.menu(FakeTerminal([tui.Click(10, 2)]), "t", self.ITEMS, []), 0)
        self.assertEqual(tui.menu(FakeTerminal(["2"]), "t", self.ITEMS, []), 1)

    def test_hover_moves_the_checklist_cursor_but_never_ticks(self):
        r = rows()
        term = FakeTerminal([tui.Hover(6, 4), tui.ENTER])  # line 4 = "Location"
        chosen = tui.checklist(term, "t", "", r)
        self.assertEqual([x.label for x in chosen], ["Advertising ID"])
        text = [tui.ANSI.sub("", line) for line in term.frame]
        self.assertTrue(any("Location" in line and tui.G.cursor in line for line in text), text)


class FakeLive:
    def __init__(self):
        self.calls = []

    def change(self, rows):
        self.calls.append([(r.label, r.checked) for r in rows])
        for r in rows:
            r.was = r.checked
            r.status = "on" if r.checked else "off"

    def note(self):
        return ""

    def buttons(self):
        return [("Back", "back"), ("Recommended", "a")]

    def press(self, action, rows):
        self.calls.append(action)


class Live(unittest.TestCase):
    def rows(self):
        return [
            tui.Row("Privacy", header=True),
            tui.Row("Telemetry off", checked=True, was=True, status="on", tech=["HKLM\\X  A = 1 REG_DWORD"],
                    detail="Sends less."),
            tui.Row("Location off", status="off", tech=["HKLM\\Y  B = 0 REG_DWORD"], detail="No location."),
        ]

    def test_a_tick_is_applied_at_once_with_no_review(self):
        live = FakeLive()
        r = self.rows()
        self.assertIsNone(tui.checklist(FakeTerminal([tui.DOWN, tui.SPACE, tui.ESC]), "t", "", r, live=live))
        self.assertEqual(live.calls, [[("Location off", True)]])
        self.assertTrue(r[2].was)

    def test_enter_and_click_switch_too(self):
        live = FakeLive()
        tui.checklist(FakeTerminal([tui.ENTER, tui.Click(6, 4), tui.ESC]), "t", "", self.rows(), live=live)
        self.assertEqual(live.calls, [[("Telemetry off", False)], [("Location off", True)]])

    def test_panel_is_technical_and_question_mark_gives_plain_words(self):
        term = FakeTerminal([tui.ESC])
        tui.checklist(term, "t", "", self.rows(), live=FakeLive())
        text = [tui.ANSI.sub("", line) for line in term.frame]
        self.assertTrue(any("A = 1 REG_DWORD" in line for line in text))
        self.assertFalse(any("Sends less." in line for line in text))
        term = FakeTerminal(["?", tui.ESC])
        tui.checklist(term, "t", "", self.rows(), live=FakeLive())
        text = [tui.ANSI.sub("", line) for line in term.frame]
        self.assertTrue(any("Sends less." in line for line in text))


class SystemCheck(unittest.TestCase):
    def test_topics_with_an_alert_start_open_and_list_it_first(self):
        checks = [security.Check("a", "Firewall", security.OK, "On", group="Protection"),
                  security.Check("b", "Antivirus", security.BAD, "None", "Turn Defender on", group="Protection"),
                  security.Check("c", "Disks", security.OK, "Healthy", group="Health")]
        topics = tui.check_topics(checks, [], security.RansomReport())
        protection = topics[0]
        self.assertTrue(protection.open)
        self.assertEqual(protection.items[0][1], "Antivirus")
        self.assertFalse(next(t for t in topics if t.title == "Health").open)

    def test_alert_badge_is_a_word_not_only_a_colour(self):
        self.assertIn("ALERT", tui.ANSI.sub("", tui.badge(security.BAD)))
        self.assertIn("CHECK", tui.ANSI.sub("", tui.badge(security.WARN)))
        widths = {len(tui.ANSI.sub("", tui.badge(s))) for s in tui.SEVERITY}
        self.assertEqual(len(widths), 1, widths)


class ReadAhead(unittest.TestCase):
    def test_a_background_read_hands_over_its_result_or_its_error(self):
        bg = tui.Background({"ok": lambda: 42, "bad": lambda: 1 / 0})
        self.assertEqual(bg.get(FakeTerminal([]), "ok", "t", "reading"), 42)
        with self.assertRaises(ZeroDivisionError):
            bg.get(FakeTerminal([]), "bad", "t", "reading")
        self.assertTrue(bg.ready("ok"))


class Clip(unittest.TestCase):
    def test_colour_codes_do_not_count_toward_the_width(self):
        line = tui.style("hello world", tui.GREEN) + " tail"
        clipped = tui.clip(line, 8)
        self.assertEqual(tui.ANSI.sub("", clipped), "hello wo")
        self.assertIn("\x1b[32m", clipped)

    def test_plain_text(self):
        self.assertEqual(tui.clip("abcdef", 3), "abc")
        self.assertEqual(tui.clip("ab", 5), "ab")


if __name__ == "__main__":
    unittest.main()
