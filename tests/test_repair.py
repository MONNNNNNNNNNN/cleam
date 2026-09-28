import datetime as dt
import tempfile
import unittest
from pathlib import Path

from cleam import debloat as d
from cleam import hardware as hw
from cleam import repair, security
from test_debloat import WIN11_PRO_ADMIN, FakeApps, FakeRegistry, FakeTasks

TODAY = dt.date(2026, 9, 27)

# What Mon's desktop reported (2026-09-27): a tweak tool had pointed Windows
# Update at a server that does not exist and disabled its services.
BLOCKED = {
    "services": {"wuauserv": 4, "UsoSvc": 3, "WaaSMedicSvc": 4, "BITS": 3, "DoSvc": 4},
    "wu_policy": {"DisableWindowsUpdateAccess": 1, "DoNotConnectToWindowsUpdateInternetLocations": 1,
                  "WUServer": "localserver.localdomain.wsus", "UseWUServer": 1, "NoAutoUpdate": 1},
    "wsus_resolves": False,
}
BCD = """identifier              {current}
path                    \\Windows\\system32\\winload.efi
isolatedcontext         Yes
tscsyncpolicy           Enhanced
hypervisorlaunchtype    Off
disabledynamictick      Yes"""
THROTTLE = """Power Setting GUID: bc5038f7-23e0-4960-96da-33abaf5935ec  (Maximum processor state)
      Minimum Possible Setting: 0x00000000
      Maximum Possible Setting: 0x00000064
      Possible Settings increment: 0x00000001
      Possible Settings units: %
    Current AC Power Setting Index: 0x00000050
    Current DC Power Setting Index: 0x00000032"""


def by_id(checks, cid):
    return [c for c in checks if c.id == cid]


class UpdateBlocks(unittest.TestCase):
    def test_every_blocking_piece_is_named(self):
        b = security.update_blocks(BLOCKED)
        self.assertEqual(b["services"], ["wuauserv", "WaaSMedicSvc", "DoSvc"])
        self.assertIn("WUServer", b["policy"])
        self.assertIn("DisableWindowsUpdateAccess", b["policy"])
        self.assertEqual(b["wsus"], "localserver.localdomain.wsus")

    def test_a_real_company_update_server_is_left_alone(self):
        b = security.update_blocks(dict(BLOCKED, wsus_resolves=True))
        self.assertNotIn("WUServer", b["policy"])
        self.assertNotIn("DoNotConnectToWindowsUpdateInternetLocations", b["policy"])

    def test_the_check_is_an_alert_with_a_journalled_fix(self):
        check = by_id(security.system_checks(BLOCKED, TODAY), "update-blocked")[0]
        self.assertEqual(check.state, security.BAD)
        fix = repair.fixes_for(check)[0]
        self.assertEqual(fix.kind, repair.CHANGE)
        deleted = {(r.key.rsplit("\\", 1)[-1], r.name) for r in fix.tweak.reg if r.value is None}
        self.assertIn(("WindowsUpdate", "WUServer"), deleted)
        self.assertIn(("AU", "UseWUServer"), deleted)
        starts = {r.key.rsplit("\\", 1)[-1]: r.value for r in fix.tweak.reg if r.name == "Start"}
        self.assertEqual(starts, {"wuauserv": 3, "WaaSMedicSvc": 3, "DoSvc": 2})

    def test_nothing_blocked_means_no_check(self):
        self.assertEqual(by_id(security.system_checks({"services": {"wuauserv": 3}}, TODAY), "update-blocked"), [])


class Engine(unittest.TestCase):
    def test_a_fix_that_deletes_values_is_undone_exactly(self):
        pol = r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
        reg = FakeRegistry({(d.HKLM, pol, "WUServer"): (1, "localserver.localdomain.wsus")})
        journal = Path(tempfile.mkdtemp()) / "j.json"
        b = d.Debloater(reg, FakeTasks({}), FakeApps(()), WIN11_PRO_ADMIN, journal)
        check = by_id(security.system_checks(BLOCKED, TODAY), "update-blocked")[0]
        tweak = repair.fixes_for(check)[0].tweak
        self.assertTrue(b.apply(tweak).ok)
        self.assertIsNone(reg.get(d.HKLM, pol, "WUServer"))
        self.assertEqual(d.load_journal(journal)["tweaks"][tweak.id]["title"], "Unblock Windows Update")
        self.assertTrue(b.undo(tweak.id).ok)
        self.assertEqual(reg.get(d.HKLM, pol, "WUServer"), (1, "localserver.localdomain.wsus"))


class Hosts(unittest.TestCase):
    TEXT = ("127.0.0.1 localhost\n0.0.0.0 ssl-delivery.adobe.com\n0.0.0.0 fe2.update.microsoft.com  # telemetry\n"
            "0.0.0.0 wdcp.microsoft.com\n# 0.0.0.0 download.windowsupdate.com\n")

    def test_only_microsoft_update_and_security_names_count(self):
        self.assertEqual(security.hosts_blocks(self.TEXT), ["fe2.update.microsoft.com", "wdcp.microsoft.com"])

    def test_unblocking_comments_out_just_those_lines_and_keeps_a_backup(self):
        path = Path(tempfile.mkdtemp()) / "hosts"
        path.write_text(self.TEXT, encoding="utf-8")
        self.assertEqual(repair.unblock_hosts(security.hosts_blocks(self.TEXT), path), "")
        self.assertEqual(security.hosts_blocks(path.read_text(encoding="utf-8")), [])
        self.assertIn("0.0.0.0 ssl-delivery.adobe.com", path.read_text(encoding="utf-8").splitlines())
        self.assertEqual((path.parent / "hosts.cleam-backup").read_text(encoding="utf-8"), self.TEXT)


class Parsers(unittest.TestCase):
    def test_bcd_values_left_by_tweak_tools(self):
        self.assertEqual(hw.bcd_tweaks(BCD), {"tscsyncpolicy": "Enhanced", "disabledynamictick": "Yes"})

    def test_trim(self):
        self.assertTrue(hw.trim_off("NTFS DisableDeleteNotify = 1  (Enabled)\nReFS DisableDeleteNotify = 0"))
        self.assertFalse(hw.trim_off("NTFS DisableDeleteNotify = 0  (Disabled)"))
        self.assertIsNone(hw.trim_off(""))

    def test_max_processor_state_reads_the_ac_value_by_position(self):
        self.assertEqual(hw.max_state(THROTTLE), 80)


class HardwareChecks(unittest.TestCase):
    def state(self, raw, cid):
        return [c.state for c in hw.hardware_checks(raw, TODAY) if c.id == cid]

    def test_memory(self):
        slow = [{"rated": 3200, "configured": 2133, "gb": 8, "type": hw.DDR4}] * 2
        self.assertEqual(self.state({"ram": slow}, "ram-speed"), [security.WARN])
        jedec = [{"rated": 4800, "configured": 4800, "gb": 16, "type": hw.DDR5}] * 2
        self.assertEqual(self.state({"ram": jedec}, "ram-speed"), [security.INFO])
        expo = [{"rated": 4800, "configured": 6000, "gb": 16, "type": hw.DDR5}] * 2
        self.assertEqual(self.state({"ram": expo}, "ram-speed"), [security.OK])
        self.assertEqual(self.state({"ram": expo[:1]}, "ram-channels"), [security.WARN])

    def test_display_below_its_best_refresh_has_a_fix(self):
        disp = {"device": "\\\\.\\DISPLAY2", "monitor": "Generic PnP Monitor", "width": 1440, "height": 2560,
                "hz": 59, "max_hz": 144}
        check = [c for c in hw.hardware_checks({"displays": [disp]}, TODAY) if c.id == "refresh"][0]
        self.assertEqual(check.state, security.WARN)
        fix = repair.fixes_for(check)[0]
        self.assertEqual((fix.kind, fix.hz, fix.target), (repair.REFRESH, 144, "\\\\.\\DISPLAY2"))

    def test_storage_and_timers(self):
        raw = {"trim": "NTFS DisableDeleteNotify = 1", "bcd": BCD, "throttle": THROTTLE,
               "volumes": [{"letter": "D", "size": 400, "free": 14}, {"letter": "C", "size": 100, "free": 25}]}
        checks = {c.id: c for c in hw.hardware_checks(raw, TODAY)}
        self.assertEqual(checks["trim"].state, security.WARN)
        self.assertEqual(checks["free-space"].state, security.BAD)  # D: 3.5 %
        self.assertEqual(checks["cpu-cap"].state, security.WARN)
        cmds = repair.fixes_for(checks["boot-timers"])[0].tech
        self.assertEqual(cmds, ("bcdedit /deletevalue {current} tscsyncpolicy",
                                "bcdedit /deletevalue {current} disabledynamictick"))

    def test_old_gpu_driver_and_basic_display(self):
        gpus = [{"name": "NVIDIA GeForce RTX 3060", "driver": "31.0", "date": "2024-01-01"}]
        self.assertEqual(self.state({"gpus": gpus}, "gpu-driver"), [security.WARN])
        basic = [{"name": "Microsoft Basic Display Adapter", "driver": "10.0", "date": "2006-06-21"}]
        self.assertEqual(self.state({"gpus": basic}, "gpu-driver"), [security.BAD])


class Fixes(unittest.TestCase):
    def test_every_non_ok_check_on_a_blocked_pc_offers_something_or_is_plain_advice(self):
        raw = dict(BLOCKED, uac=0, firewall=[{"name": "Public", "on": False}], smartscreen={"policy": 0},
                   hosts_blocked=["wdcp.microsoft.com"], smb1=True, trim="NTFS DisableDeleteNotify = 1")
        checks = security.windows_checks(raw, TODAY)
        for cid in ("uac", "firewall", "smartscreen", "hosts", "smb1", "update-blocked", "trim"):
            check = next(c for c in checks if c.id == cid)
            self.assertTrue(repair.fixes_for(check), cid)

    def test_a_disabled_device_is_switched_on_by_its_instance_id(self):
        devices = [{"name": "High precision event timer", "id": "ACPI\\PNP0103\\2&DABA3FF&0", "code": 22}]
        check = [c for c in security.system_checks({"bad_devices": devices}, TODAY) if c.id == "devices"][0]
        fix = repair.fixes_for(check)[0]
        self.assertIn("Enable-PnpDevice -InstanceId 'ACPI\\PNP0103\\2&DABA3FF&0'", fix.script)

    def test_ok_checks_have_no_fix(self):
        self.assertEqual(repair.fixes_for(security.Check("uac", "UAC", security.OK, "On.")), [])

    def test_startup_switch_goes_where_task_manager_looks(self):
        item = security.StartupItem("IDMan", "x.exe", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run")
        self.assertEqual(repair.approved_key(item)[1].rsplit("\\", 1)[-1], "Run")
        wow = security.StartupItem("x", "x.exe", r"HKLM\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run")
        self.assertEqual(repair.approved_key(wow), ("HKLM", repair.approved_key(wow)[1]))
        self.assertTrue(repair.approved_key(wow)[1].endswith("Run32"))
        once = security.StartupItem("x", "x.exe", r"HKCU\Software\Microsoft\Windows\CurrentVersion\RunOnce")
        self.assertIsNone(repair.approved_key(once))


class StartupSwitch(unittest.TestCase):
    TASK = "\\GoogleUpdaterTaskSystem142.0"
    SVC_KEY = r"SYSTEM\CurrentControlSet\Services\Steam"

    def setUp(self):
        self.journal = Path(tempfile.mkdtemp()) / "journal.json"

    def engine(self, reg=None, tasks=None):
        return d.Debloater(reg or FakeRegistry(), tasks or FakeTasks({}), FakeApps(()), WIN11_PRO_ADMIN, self.journal)

    def task(self, enabled=True):
        return security.StartupItem("GoogleUpdaterTaskSystem142.0", "updater.exe --wake", "Scheduled task",
                                    enabled=enabled, kind="task", key=self.TASK)

    def service(self, enabled=True):
        return security.StartupItem("Steam Client Service", "steamservice.exe", "Service", enabled=enabled,
                                    kind="service", key="Steam")

    def test_a_task_is_switched_off_through_the_journal_and_comes_back(self):
        tasks = FakeTasks({self.TASK: 3})
        engine = self.engine(tasks=tasks)
        [fix] = repair.startup_fixes(self.task(), {"tweaks": {}})
        self.assertEqual((fix.title, fix.kind), (repair.TURN_OFF, repair.CHANGE))
        self.assertIn(f"scheduled task {self.TASK}: Disabled", fix.tech)
        self.assertIn("updates only when you open it", fix.warn)  # an updater says what switching it off costs
        self.assertTrue(engine.apply(fix.tweak).ok)
        self.assertEqual(tasks.s[self.TASK], 1)
        [back] = repair.startup_fixes(self.task(enabled=False), d.load_journal(self.journal))
        self.assertEqual(back.title, repair.TURN_ON)
        self.assertTrue(engine.undo(fix.tweak.id).ok)
        self.assertEqual(tasks.s[self.TASK], 3)

    def test_a_service_goes_to_manual_and_undo_restores_its_own_start_type(self):
        reg = FakeRegistry({(d.HKLM, self.SVC_KEY, "Start"): (4, 2)})
        engine = self.engine(reg)
        [fix] = repair.startup_fixes(self.service(), {"tweaks": {}})
        self.assertIn("service Steam: Start = 3 (Manual)", fix.tech[0])
        self.assertEqual(fix.warn, "")
        self.assertTrue(engine.apply(fix.tweak).ok)
        self.assertEqual(reg.get(d.HKLM, self.SVC_KEY, "Start"), (4, 3))
        self.assertIn("startup-service-Steam", d.load_journal(self.journal)["tweaks"])
        self.assertTrue(engine.undo("startup-service-Steam").ok)
        self.assertEqual(reg.get(d.HKLM, self.SVC_KEY, "Start"), (4, 2))

    def test_undo_after_the_service_was_uninstalled_does_not_recreate_its_key(self):
        reg = FakeRegistry({(d.HKLM, self.SVC_KEY, "Start"): (4, 2)})
        engine = self.engine(reg)
        [fix] = repair.startup_fixes(self.service(), {"tweaks": {}})
        engine.apply(fix.tweak)
        reg.values.clear()
        reg.keys.clear()  # uninstalled
        self.assertTrue(engine.undo("startup-service-Steam").ok)
        self.assertFalse(reg.key_exists(d.HKLM, self.SVC_KEY))
        self.assertNotIn("startup-service-Steam", d.load_journal(self.journal)["tweaks"])

    def test_switched_off_by_someone_else_offers_nothing(self):
        # Without a journal entry there is no exact state to go back to.
        self.assertEqual(repair.startup_fixes(self.service(enabled=False), {"tweaks": {}}), [])

    def test_protective_software_is_never_offered(self):
        av = security.StartupItem("Malwarebytes Service", "mbamservice.exe", "Service", kind="service",
                                  key="MBAMService")
        self.assertEqual(repair.startup_fixes(av, {"tweaks": {}}), [])


if __name__ == "__main__":
    unittest.main()
