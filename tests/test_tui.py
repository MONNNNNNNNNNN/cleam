import unittest

from cleam import tui


class FakeTerminal:
    """Plays a list of keys and keeps the last frame drawn."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.frame = []

    def draw(self, lines):
        self.frame = lines

    def key(self):
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
