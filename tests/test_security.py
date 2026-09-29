import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from cleam import security as s
from cleam import wincleanup


class AvState(unittest.TestCase):
    def test_product_state_bits(self):
        self.assertEqual(s.av_state(397568), (True, False))  # 0x61100: Defender on, current
        self.assertEqual(s.av_state(393472), (False, False))  # 0x60100: off
        self.assertEqual(s.av_state(397584), (True, True))  # 0x61110: on, definitions out of date


class WindowsChecks(unittest.TestCase):
    def by_id(self, raw):
        return {c.id: c for c in s.windows_checks(raw)}

    def test_no_antivirus_is_the_worst_finding_and_names_the_policy(self):
        checks = self.by_id({"av": [], "defender": None, "policy_off": True, "defender_service": "Stopped/Disabled"})
        self.assertEqual(checks["antivirus"].state, s.BAD)
        self.assertIn("policy", checks["antivirus"].detail)
        self.assertIn("DisableAntiSpyware", checks["antivirus"].fix)

    def test_third_party_antivirus_counts(self):
        checks = self.by_id({"av": {"name": "Malwarebytes", "state": 397312}})  # a bare object, as PowerShell sends one
        self.assertEqual(checks["antivirus"].state, s.OK)

    def test_out_of_date_definitions_warn(self):
        checks = self.by_id({"av": [{"name": "Defender", "state": 397584}]})
        self.assertEqual(checks["antivirus"].state, s.WARN)

    def test_ransomware_shield_unknown_off_and_on(self):
        self.assertEqual(self.by_id({"cfa": None})["ransomware-shield"].state, s.UNKNOWN)
        self.assertEqual(self.by_id({"cfa": 0})["ransomware-shield"].state, s.WARN)
        self.assertEqual(self.by_id({"cfa": 1})["ransomware-shield"].state, s.OK)

    def test_firewall_off_on_one_profile_is_bad(self):
        raw = {"firewall": [{"name": "Domain", "on": True}, {"name": "Public", "on": False}]}
        check = self.by_id(raw)["firewall"]
        self.assertEqual(check.state, s.BAD)
        self.assertIn("Public", check.detail)

    def test_uac_off_is_bad(self):
        self.assertEqual(self.by_id({"uac": 0})["uac"].state, s.BAD)

    def test_no_restore_points_warns(self):
        self.assertEqual(self.by_id({"shadows": 0})["restore-points"].state, s.WARN)


class SystemChecks(unittest.TestCase):
    import datetime as _dt
    TODAY = _dt.date(2026, 9, 27)

    def by_id(self, raw):
        return {c.id: c for c in s.system_checks(raw, self.TODAY)}

    def test_update_age_thresholds(self):
        self.assertEqual(self.by_id({"last_update": "2026-09-10"})["last-update"].state, s.OK)
        self.assertEqual(self.by_id({"last_update": "2026-07-20"})["last-update"].state, s.WARN)
        self.assertEqual(self.by_id({"last_update": "2026-04-29"})["last-update"].state, s.BAD)  # this PC: 151 days

    def test_windows_10_support_is_esu_until_2027_then_bad(self):
        self.assertEqual(self.by_id({"build": 19045})["windows-10-support"].state, s.INFO)
        import datetime as dt
        after = {c.id: c for c in s.system_checks({"build": 19045}, dt.date(2027, 10, 13))}
        self.assertEqual(after["windows-10-support"].state, s.BAD)
        self.assertNotIn("windows-10-support", self.by_id({"build": 26100}))

    def test_secure_boot_off_warns_and_names_the_games(self):
        c = self.by_id({"secureboot": False})["secure-boot"]
        self.assertEqual(c.state, s.WARN)
        self.assertIn("Battlefield 6", c.detail)
        self.assertEqual(self.by_id({"secureboot": None})["secure-boot"].state, s.UNKNOWN)

    def test_exposure(self):
        self.assertEqual(self.by_id({"smb1": True})["smb1"].state, s.BAD)
        self.assertEqual(self.by_id({"rdp_deny": 0, "rdp_nla": 0})["rdp"].state, s.BAD)
        self.assertEqual(self.by_id({"rdp_deny": 0, "rdp_nla": 1})["rdp"].state, s.WARN)
        self.assertEqual(self.by_id({"rdp_deny": 1})["rdp"].state, s.OK)
        self.assertEqual(self.by_id({"guest": True})["guest"].state, s.WARN)

    def test_encryption_matters_more_on_a_laptop(self):
        off = {"status": "FullyDecrypted", "protection": "Off"}
        self.assertEqual(self.by_id({"bitlocker": off, "battery": 0})["encryption"].state, s.INFO)
        self.assertEqual(self.by_id({"bitlocker": off, "battery": 1})["encryption"].state, s.WARN)

    def test_disk_health_wear_and_heat(self):
        checks = s.system_checks({"disks": [
            {"name": "A", "media": "SSD", "health": "Healthy", "wear": 3, "temp": 40},
            {"name": "B", "media": "SSD", "health": "Healthy", "wear": 95, "temp": 40},
            {"name": "C", "media": "HDD", "health": "Warning", "wear": None, "temp": 70},
        ]}, self.TODAY)
        states = [c.state for c in checks if c.id == "disk"]
        self.assertEqual(states, [s.OK, s.WARN, s.BAD])

    def test_crashes_and_devices(self):
        c = self.by_id({"crashes": [{"id": 1001, "count": 2}, {"id": 41, "count": 1}], "bad_devices": ["Wi-Fi"]})
        self.assertEqual(c["crashes"].state, s.WARN)
        self.assertIn("2 blue screen", c["crashes"].detail)
        self.assertEqual(c["devices"].state, s.WARN)
        self.assertEqual(self.by_id({"crashes": {"id": 41, "count": 1}})["crashes"].state, s.WARN)  # bare object

    def test_memory_integrity_off_is_a_trade_off_not_a_failure(self):
        self.assertEqual(self.by_id({"hvci": 0})["memory-integrity"].state, s.INFO)

    def test_every_check_is_in_a_known_group(self):
        raw = {"build": 19045, "last_update": "2026-04-29", "secureboot": False, "tpm": {"present": True, "ready": True},
               "hvci": 0, "smb1": False, "rdp_deny": 1, "disks": [{"name": "A", "health": "Healthy"}],
               "crashes": [], "bad_devices": [], "uptime_h": 400, "game_mode": 0, "hags": 1,
               "power": "381b4222-f694-41f0-9685-ff5bb260df2e"}
        for c in s.windows_checks(raw, self.TODAY):
            self.assertIn(c.group, s.CHECK_GROUPS, c.id)


class ProgramOf(unittest.TestCase):
    def test_quoted(self):
        self.assertEqual(s.program_of('"C:\\A B\\x.exe" -background'), "C:\\A B\\x.exe")

    def test_unquoted_path_with_spaces_and_arguments(self):
        cmd = r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe /onboot"
        self.assertEqual(s.program_of(cmd), r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe")

    def test_environment_variables_are_expanded(self):
        os.environ["CLEAM_TEST_DIR"] = r"C:\Tools"
        self.assertEqual(s.program_of(r"%CLEAM_TEST_DIR%\a.exe"), r"C:\Tools\a.exe")


class Judge(unittest.TestCase):
    ENV = {"TEMP": r"C:\Users\u\AppData\Local\Temp", "APPDATA": r"C:\Users\u\AppData\Roaming",
           "LOCALAPPDATA": r"C:\Users\u\AppData\Local", "ProgramData": r"C:\ProgramData"}

    def reasons(self, command, signed=""):
        item = s.StartupItem("x", command, "HKCU\\Run", s.program_of(command), signed=signed)
        return " | ".join(s.judge(item, self.ENV))

    def test_an_ordinary_signed_program_is_not_flagged(self):
        item = s.StartupItem("py", sys.executable, "HKCU\\Run", sys.executable, signed="Valid")
        self.assertEqual(s.judge(item, self.ENV), [])

    def test_each_warning_sign(self):
        self.assertIn("does not exist", self.reasons(r"C:\gone\nothing.exe"))
        self.assertIn("temp folder", self.reasons(r"C:\Users\u\AppData\Local\Temp\x.exe"))
        short_temp = dict(self.ENV, TEMP=r"C:\Users\ADMINI~1\AppData\Local\Temp")
        item = s.StartupItem("x", "", "", r"C:\Users\Administrator\AppData\Local\Temp\x.exe")
        self.assertIn("runs from the temp folder", s.judge(item, short_temp))
        self.assertIn("Downloads", self.reasons(r"C:\Users\u\Downloads\setup.exe"))
        self.assertIn("%APPDATA%", self.reasons(r"C:\Users\u\AppData\Roaming\svchost.exe"))
        self.assertIn("script host", self.reasons(r"wscript.exe C:\Users\u\a.vbs"))
        self.assertIn("encoded PowerShell", self.reasons("powershell -w hidden -enc SQBFAFgA"))
        self.assertIn("DLL", self.reasons(r"rundll32.exe C:\Users\u\AppData\Local\x.dll,Run"))
        self.assertIn("HashMismatch", self.reasons(r"C:\Tools\patched.exe", signed="HashMismatch"))

    def test_a_program_in_its_own_vendor_folder_is_not_loose(self):
        self.assertNotIn("loose", self.reasons(r"C:\Users\u\AppData\Local\Discord\Update.exe --processStart"))


class RansomSigns(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def touch(self, rel):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    def test_a_clean_folder_is_ok(self):
        for i in range(50):
            self.touch(f"Pictures/IMG_{i}.jpg")
            self.touch(f"Downloads/movie{i}.mp4.part")  # downloads in progress are not ransomware
            self.touch(f"Documents/report{i}.pdf")
        report = s.ransom_signs([self.root])
        self.assertEqual(report.state, s.OK, report.findings())
        self.assertEqual(report.files_checked, 150)

    def test_a_ransom_note_is_found(self):
        self.touch("Desktop/_readme.txt")
        report = s.ransom_signs([self.root])
        self.assertEqual(report.state, s.BAD)
        self.assertTrue(report.notes[0].endswith("_readme.txt"))

    def test_a_family_extension_is_found(self):
        self.touch("Documents/budget.xlsx.WNCRY")
        self.assertEqual(s.ransom_signs([self.root]).state, s.BAD)

    def test_many_documents_renamed_to_one_extension(self):
        # STOP/Djvu appends a random 4 letters to every file it encrypts.
        for i in range(s.MANY):
            self.touch(f"Documents/file{i}.docx.mbtf")
        report = s.ransom_signs([self.root])
        self.assertEqual(report.state, s.BAD)
        self.assertEqual(report.renamed_count["mbtf"], s.MANY)

    def test_sidecar_files_are_not_ransomware(self):
        # Ableton writes song.mp3.asd beside every sample; Chrome saves through file.docx.crswap.
        for i in range(s.MANY + 5):
            self.touch(f"Music/Samples/kick{i}.mp3.asd")
            self.touch(f"Documents/draft{i}.docx.crswap")
        self.assertEqual(s.ransom_signs([self.root]).state, s.OK)

    def test_a_developers_readme_is_not_a_ransom_note(self):
        self.touch("Documents/project/_README.md")
        self.assertEqual(s.ransom_signs([self.root]).notes, [])

    def test_a_few_renamed_files_are_not_enough(self):
        for i in range(3):
            self.touch(f"Documents/file{i}.docx.qwer")
        self.assertEqual(s.ransom_signs([self.root]).state, s.OK)

    def test_links_are_not_followed(self):
        outside = Path(tempfile.mkdtemp())
        (outside / "_readme.txt").write_bytes(b"x")
        try:
            os.symlink(outside, self.root / "link", target_is_directory=True)
        except OSError:
            self.skipTest("symlinks not permitted here")
        self.assertEqual(s.ransom_signs([self.root]).notes, [])


class NoConsoleWindows(unittest.TestCase):
    """The window build has no console, so every captured child must ask for
    none, or Windows opens a PowerShell window over Cleam (seen for the
    restore point)."""

    def test_captured_children_ask_for_no_window(self):
        from unittest import mock
        from cleam import snapshot, system

        done = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=done) as run, \
                mock.patch.object(snapshot, "_command", return_value=["powershell", "-c", "x"]):
            snapshot.create("t", capture=True)
            system.output(["whoami"])
            with mock.patch.object(wincleanup, "list_drivers", return_value=[
                {"Driver": "oem1.inf", "OriginalFileName": r"C:\R\a.inf_1\a.inf", "Version": "1", "ProviderName": "p", "ClassName": "c"},
                {"Driver": "oem2.inf", "OriginalFileName": r"C:\R\a.inf_2\a.inf", "Version": "2", "ProviderName": "p", "ClassName": "c"},
            ]):
                self.assertEqual(wincleanup.delete_old_driver_packages(), 0)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs.get("creationflags"), system.NO_WINDOW, call.args)
        self.assertIn(["pnputil", "/delete-driver", "oem1.inf"], [c.args[0] for c in run.call_args_list])

    def test_disk_cleanup_is_started_hidden(self):
        self.assertIn("-WindowStyle Hidden", wincleanup.sagerun_script(("Temporary Setup Files",)))


class DriverPackages(unittest.TestCase):
    def test_only_older_versions_of_the_same_package(self):
        drivers = [
            {"Driver": "oem11.inf", "OriginalFileName": r"C:\R\nvhda.inf_a\nvhda.inf", "Version": "1.4.3.2",
             "ProviderName": "NVIDIA", "ClassName": "MEDIA"},
            {"Driver": "oem70.inf", "OriginalFileName": r"C:\R\nvhda.inf_b\nvhda.inf", "Version": "1.4.6.3",
             "ProviderName": "NVIDIA", "ClassName": "MEDIA"},
            {"Driver": "oem50.inf", "OriginalFileName": r"C:\R\nvhda.inf_c\nvhda.inf", "Version": "1.4.10.0",
             "ProviderName": "NVIDIA", "ClassName": "MEDIA"},
            {"Driver": "oem1.inf", "OriginalFileName": r"C:\R\e3x.inf_a\e3x.inf", "Version": "10.0",
             "ProviderName": "Killer", "ClassName": "Net"},
        ]
        old = sorted(d["Driver"] for d in wincleanup.superseded(drivers))
        self.assertEqual(old, ["oem11.inf", "oem70.inf"])  # 1.4.10 beats 1.4.6 numerically, not as text

    def test_sagerun_script_clears_arms_and_disarms(self):
        script = wincleanup.sagerun_script(("Device Driver Packages",))
        clear, arm, disarm = script.find("Remove-ItemProperty"), script.find("New-ItemProperty"), script.rfind("finally")
        self.assertTrue(0 <= clear < arm < disarm)
        self.assertIn("/sagerun:619", script)
        self.assertTrue(script.rstrip().endswith("exit $code"))  # after finally, never inside try
        self.assertIn("'Device Driver Packages'", script)


class TasksAndServices(unittest.TestCase):
    def setUp(self):
        # A "Windows folder" whose program exists, so judge() has nothing to say
        # about it: the interpreter's own. Not a temp dir -- on Windows that is
        # under AppData\Local\Temp, and judge() rightly flags programs there.
        self.windir = os.path.dirname(sys.executable)
        self.defrag = sys.executable
        self.env = {"SystemRoot": self.windir, "ProgramFiles": r"C:\Program Files",
                    "LOCALAPPDATA": r"C:\Users\u\AppData\Local", "TEMP": r"C:\Users\u\AppData\Local\Temp"}

    def test_a_vendor_task_is_listed_with_when_it_runs(self):
        rows = [{"path": "\\GoogleUpdaterTaskSystem142.0", "state": 3,
                 "exec": '"C:\\Program Files (x86)\\Google\\GoogleUpdater\\updater.exe"', "args": "--wake",
                 "triggers": ["LogonTrigger", "DailyTrigger", "DailyTrigger"]}]
        [item] = s.task_items(rows, self.env)
        self.assertEqual((item.kind, item.key, item.name), ("task", rows[0]["path"], "GoogleUpdaterTaskSystem142.0"))
        self.assertEqual(item.location, "Scheduled task (at sign-in, daily)")
        self.assertEqual(item.path, r"C:\Program Files (x86)\Google\GoogleUpdater\updater.exe")
        self.assertTrue(item.enabled)
        self.assertEqual(item.id, "task:\\GoogleUpdaterTaskSystem142.0")

    def test_disabled_state_and_powershell_unwrapping(self):
        # ConvertTo-Json turns a one-element list into the element itself.
        row = {"path": "\\Opera scheduled Autoupdate", "state": 1, "exec": r"C:\Opera\launcher.exe",
               "triggers": "TimeTrigger"}
        [item] = s.task_items(row, self.env)
        self.assertFalse(item.enabled)
        self.assertEqual(item.location, "Scheduled task (on a schedule)")

    def test_windows_own_tasks_are_hidden_but_an_odd_one_in_their_folder_is_not(self):
        rows = [
            {"path": "\\Microsoft\\Windows\\Defrag\\ScheduledDefrag", "state": 3, "exec": self.defrag},
            {"path": "\\Microsoft\\Windows\\Shell\\CreateObjectTask", "state": 3, "clsid": "{8F8C8B7B-...}"},
            # A fake Windows task: the classic hiding place.
            {"path": "\\Microsoft\\Windows\\Maintenance\\Sync", "state": 3,
             "exec": r"C:\Users\u\AppData\Local\sync.exe"},
            {"path": "\\Microsoft\\Windows\\Wininet\\Cache", "state": 3, "exec": self.defrag,
             "args": "& powershell -w hidden -enc SQBFAFgA"},
            {"path": "\\Vendor\\Helper", "state": 3, "clsid": "{1234}"},
            {"path": "\\Nothing to run", "state": 3},
        ]
        names = [i.name for i in s.task_items(rows, self.env)]
        self.assertEqual(names, ["Sync", "Cache", "Helper"])

    def test_a_bare_program_name_is_looked_up_on_path_not_called_missing(self):
        # \\Microsoft\\Windows\\Bluetooth\\UninstallDeviceTask runs "BthUdTask.exe", no folder.
        row = {"path": "\\Microsoft\\Windows\\Bluetooth\\UninstallDeviceTask", "state": 3,
               "exec": "BthUdTask.exe", "args": "$(Arg0)"}
        with mock.patch.object(s.shutil, "which", return_value=self.defrag):
            self.assertEqual(s.task_items([row], self.env), [])  # Windows' own: hidden
            self.assertEqual(s.program_of("rundll32.exe x.dll,Run"), self.defrag)
        with mock.patch.object(s.shutil, "which", return_value=None):
            self.assertEqual(s.program_of("nothing-here.exe --x"), "nothing-here.exe")  # unknown stays as written

    def test_services_outside_windows_are_listed_svchost_ones_are_not(self):
        rows = [
            {"name": "Dnscache", "display": "DNS Client", "mode": "Auto",
             "path": os.path.join(self.windir, "system32", "svchost.exe") + " -k NetworkService"},
            {"name": "NVDisplay.ContainerLocalSystem", "display": "NVIDIA Display Container LS", "mode": "Auto",
             "delayed": True, "path": '"C:\\Windows\\System32\\DriverStore\\nvlt.inf\\NVDisplay.Container.exe" -s'},
            {"name": "AdobeARMservice", "display": "Adobe Acrobat Update Service", "mode": "Manual",
             "path": '"C:\\Program Files (x86)\\Common Files\\Adobe\\ARM\\1.0\\armsvc.exe"'},
            {"name": "gupdate", "display": "Google Updater", "mode": "Manual", "path": r"C:\G\updater.exe"},
        ]
        items = s.service_items(rows, self.env, cleam_off={"gupdate"})
        self.assertEqual([(i.key, i.enabled) for i in items],
                         [("NVDisplay.ContainerLocalSystem", True), ("gupdate", False)])
        self.assertEqual(items[0].location, "Service (delayed start)")
        self.assertEqual(items[0].path, r"C:\Windows\System32\DriverStore\nvlt.inf\NVDisplay.Container.exe")

    def test_an_unquoted_service_path_with_spaces_is_flagged(self):
        cmd = r"C:\Program Files\Vendor App\svc.exe -run"
        item = s.StartupItem("svc", cmd, "Service", s.program_of(cmd), kind="service", key="svc")
        self.assertIn("unquoted", " ".join(s.judge(item, self.env)))
        quoted = s.StartupItem("svc", f'"{s.program_of(cmd)}" -run', "Service", s.program_of(cmd), kind="service")
        self.assertNotIn("unquoted", " ".join(s.judge(quoted, self.env)))

    def test_microsoft_signed_services_hide_microsoft_signed_root_tasks_stay(self):
        ms = dict(signed="Valid", publisher="Microsoft Corporation")
        items = [
            s.StartupItem("Edge Update Service", "x", "Service", sys.executable, kind="service", key="edgeupdate", **ms),
            s.StartupItem("MicrosoftEdgeUpdateTaskMachineCore", "x", "Scheduled task", sys.executable, kind="task",
                          key="\\MicrosoftEdgeUpdateTaskMachineCore", **ms),
            s.StartupItem("OfficeTelemetryAgentLogOn", "x", "Scheduled task", sys.executable, kind="task",
                          key="\\Microsoft\\Office\\OfficeTelemetryAgentLogOn", **ms),
            s.StartupItem("Steam Client Service", "x", "Service", sys.executable, kind="service", key="Steam",
                          signed="Valid", publisher="Valve Corp."),
        ]
        self.assertEqual([i.name for i in s.hide_microsoft(items)],
                         ["MicrosoftEdgeUpdateTaskMachineCore", "Steam Client Service"])

    def test_an_unreadable_signature_hides_a_windows_service_rather_than_offer_it(self):
        spooler = s.StartupItem("Print Spooler", "", "Service", self.defrag, kind="service", key="Spooler")
        vendor = s.StartupItem("Steam Client Service", "", "Service", r"C:\Steam\steamservice.exe", kind="service",
                               key="Steam")
        self.assertEqual([i.key for i in s.hide_microsoft([spooler, vendor], self.env)], ["Steam"])

    def test_malware_cannot_hide_behind_a_protective_name(self):
        fake = s.StartupItem("Windows Security Update", "", "HKCU\\Run", r"C:\Users\u\AppData\Local\Temp\wsu.exe",
                             kind="run", reasons=["runs from the temp folder"])
        self.assertFalse(fake.protective)
        real = s.StartupItem("x", "", "Service", kind="service", key="WinDefend", reasons=["signature: Unknown"])
        self.assertTrue(real.protective)  # Defender's own services stay protected, flagged or not

    def test_protective_software_is_recognised_and_reset_is_not_eset(self):
        self.assertTrue(s.StartupItem("Malwarebytes Service", "", "Service", kind="service", key="MBAMService").protective)
        self.assertTrue(s.StartupItem("x", "", "Service", kind="service", key="WinDefend").protective)
        self.assertTrue(s.StartupItem("ESET Service", "", "Service", kind="service", key="ekrn").protective)
        self.assertFalse(s.StartupItem("ResetHelper", "", "Scheduled task", r"C:\Tools\reset.exe").protective)


if __name__ == "__main__":
    unittest.main()
