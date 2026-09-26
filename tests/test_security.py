import os
import sys
import tempfile
import unittest
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
        self.assertIn("HashMismatch", self.reasons(sys.executable, signed="HashMismatch"))

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


if __name__ == "__main__":
    unittest.main()
