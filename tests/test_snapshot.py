import unittest
from unittest import mock

from cleam import snapshot, system


def _linux(tool: str | None):
    """Patch snapshot to look like Linux with only `tool` installed."""
    which = mock.patch.object(snapshot.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}" if n == tool else None)
    return (
        mock.patch.object(snapshot, "OS", "linux"),
        which,
        mock.patch.object(snapshot, "sudo", side_effect=lambda cmd, noninteractive=False: cmd),
    )


class Commands(unittest.TestCase):
    def test_linux_without_a_tool_says_what_to_install(self):
        with mock.patch.object(snapshot, "OS", "linux"), mock.patch.object(snapshot.shutil, "which", return_value=None):
            message = snapshot._command("create", "x")
        self.assertIsInstance(message, str)
        self.assertIn("timeshift", message)

    def test_timeshift_create_is_non_interactive(self):
        patches = _linux("timeshift")
        with patches[0], patches[1], patches[2]:
            cmd = snapshot._command("create", "before clean")
        self.assertEqual(cmd, ["timeshift", "--create", "--comments", "before clean", "--scripted"])

    def test_snapper_is_the_fallback(self):
        patches = _linux("snapper")
        with patches[0], patches[1], patches[2]:
            self.assertEqual(snapshot._command("list", ""), ["snapper", "list"])

    def test_windows_restore_point_needs_admin(self):
        with mock.patch.object(snapshot, "OS", "windows"), mock.patch.object(snapshot, "is_admin", return_value=False):
            self.assertIn("Administrator", snapshot._command("create", "x"))

    def test_windows_create_turns_the_24h_throttle_warning_into_a_failure(self):
        with mock.patch.object(snapshot, "OS", "windows"), mock.patch.object(snapshot, "is_admin", return_value=True):
            cmd = snapshot._command("create", "x")
        self.assertTrue(cmd[0].startswith("powershell"))
        self.assertIn("-WarningAction Stop", cmd[-1])

    def test_description_quotes_cannot_break_out_of_the_powershell_string(self):
        with mock.patch.object(snapshot, "OS", "windows"), mock.patch.object(snapshot, "is_admin", return_value=True):
            cmd = snapshot._command("create", "it's here'; Remove-Item C:\\ -Recurse")
        # Every quote is doubled, so the whole description stays one PowerShell
        # string literal and cannot start a second statement.
        self.assertIn("-Description 'it''s here''; Remove-Item C:\\ -Recurse' -RestorePointType", cmd[-1])

    def test_run_passes_the_message_up_when_there_is_no_command(self):
        with mock.patch.object(snapshot, "_command", return_value="nothing installed"):
            self.assertEqual(snapshot.run("create"), (1, "nothing installed"))


class Sudo(unittest.TestCase):
    def test_noninteractive_refuses_to_wait_for_a_password(self):
        with mock.patch.object(system, "OS", "linux"), mock.patch.object(
            system, "is_admin", return_value=False
        ), mock.patch.object(system.shutil, "which", return_value="/usr/bin/sudo"):
            self.assertEqual(system.sudo(["timeshift"], noninteractive=True), ["sudo", "-n", "timeshift"])
            self.assertEqual(system.sudo(["timeshift"]), ["sudo", "timeshift"])

    def test_root_needs_no_sudo(self):
        with mock.patch.object(system, "OS", "linux"), mock.patch.object(system, "is_admin", return_value=True):
            self.assertEqual(system.sudo(["timeshift"]), ["timeshift"])


if __name__ == "__main__":
    unittest.main()
