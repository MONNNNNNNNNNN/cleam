import unittest
from unittest import mock

from cleam import apps


class Registry(unittest.TestCase):
    def test_msiexec_install_switch_becomes_uninstall(self):
        self.assertEqual(
            apps.msi_uninstall("MsiExec.exe /I{1234-ABCD}"),
            "MsiExec.exe /X{1234-ABCD}",
        )
        self.assertEqual(
            apps.msi_uninstall('"C:\\Windows\\System32\\msiexec" /i{AB}'),
            '"C:\\Windows\\System32\\msiexec" /X{AB}',
        )

    def test_non_msi_command_is_untouched(self):
        cmd = '"C:\\Program Files\\App\\unins000.exe" /I'
        self.assertEqual(apps.msi_uninstall(cmd), cmd)

    def test_hidden_and_patch_entries_are_skipped(self):
        base = {"DisplayName": "App", "UninstallString": "u.exe"}
        self.assertIsNotNone(apps.from_registry("k", base))
        self.assertIsNone(apps.from_registry("k", {**base, "SystemComponent": 1}))
        self.assertIsNone(apps.from_registry("k", {**base, "ParentKeyName": "Office"}))
        self.assertIsNone(apps.from_registry("k", {"DisplayName": "No uninstaller"}))


class Parsers(unittest.TestCase):
    def test_dpkg_lists_manual_non_base_packages_only(self):
        text = (
            "bash\t5.2\trequired\tyes\n"
            "ca-certificates\t2026\timportant\tno\n"
            "caddy\t2.11\toptional\tno\n"
            "libfoo\t1.0\toptional\tno\n"
        )
        found = apps.parse_dpkg(text, {"bash", "ca-certificates", "caddy"})
        self.assertEqual([a.id for a in found], ["caddy"])

    def test_snap_skips_bases_and_snapd(self):
        text = (
            "Name     Version  Rev   Tracking       Publisher   Notes\n"
            "core22   2024     1122  latest/stable  canonical✓  base\n"
            "snapd    2.61     2100  latest/stable  canonical✓  snapd\n"
            "firefox  130.0    4800  latest/stable  mozilla✓    -\n"
        )
        found = apps.parse_snap(text)
        self.assertEqual([a.id for a in found], ["firefox"])
        self.assertEqual(found[0].command, ["snap", "remove", "firefox"])

    def test_flatpak_keeps_names_with_spaces(self):
        found = apps.parse_flatpak("com.visualstudio.code\tVisual Studio Code\t1.93\n")
        self.assertEqual((found[0].id, found[0].name, found[0].version), ("com.visualstudio.code", "Visual Studio Code", "1.93"))


class Uninstall(unittest.TestCase):
    APT = apps.App("caddy", "caddy", "2.11", "apt", ["apt-get", "remove", "caddy"])

    def _run(self, capture):
        sudo = lambda cmd, noninteractive=False: (["sudo", "-n", *cmd] if noninteractive else ["sudo", *cmd])
        with mock.patch.object(apps, "sudo", side_effect=sudo), mock.patch.object(apps.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="removed\n", stderr="")
            result = apps.uninstall(self.APT, capture=capture)
        return run.call_args[0][0], result

    def test_gui_mode_answers_prompts_up_front_and_never_waits(self):
        cmd, (code, text) = self._run(capture=True)
        self.assertEqual(cmd, ["sudo", "-n", "apt-get", "remove", "caddy", "-y"])
        self.assertEqual((code, text), (0, "removed"))

    def test_cli_mode_keeps_the_prompts_and_the_terminal(self):
        cmd, (_, text) = self._run(capture=False)
        self.assertEqual(cmd, ["sudo", "apt-get", "remove", "caddy"])
        self.assertEqual(text, "")

    def test_a_windows_uninstaller_is_never_captured(self):
        # Its pipes can outlive it (Au_.exe, _isdel.exe relaunch themselves),
        # which would hang the GUI thread forever.
        app = apps.App("{GUID}", "Some App", "1.0", "registry", "MsiExec.exe /X{GUID}")
        with mock.patch.object(apps.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            apps.uninstall(app, capture=True, timeout=5)
        self.assertNotIn("capture_output", run.call_args.kwargs)

    def test_a_captured_uninstall_that_never_answers_reports_instead_of_hanging(self):
        with mock.patch.object(apps, "sudo", side_effect=lambda cmd, noninteractive=False: cmd), mock.patch.object(
            apps.subprocess, "run", side_effect=apps.subprocess.TimeoutExpired("apt-get", 900)
        ):
            code, text = apps.uninstall(self.APT, capture=True, timeout=900)
        self.assertEqual(code, 1)
        self.assertIn("no answer after 900s", text)


if __name__ == "__main__":
    unittest.main()
