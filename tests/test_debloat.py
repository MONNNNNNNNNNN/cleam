import tempfile
import unittest
from pathlib import Path

from cleam import debloat as d


class FakeRegistry:
    def __init__(self, values=None, keys=()):
        self.values = dict(values or {})  # (hive, key, name) -> (kind, value)
        self.keys = {(h, k.lower()) for h, k in keys} | {(h, k.lower()) for h, k, _ in self.values}

    def key_exists(self, hive, key):
        return (hive, key.lower()) in self.keys

    def get(self, hive, key, name):
        return self.values.get((hive, key, name))

    def set(self, hive, key, name, kind, value):
        self.keys.add((hive, key.lower()))
        self.values[(hive, key, name)] = (kind, value)

    def delete_value(self, hive, key, name):
        self.values.pop((hive, key, name), None)

    def delete_empty_key(self, hive, key):
        if not any(h == hive and k.lower() == key.lower() for h, k, _ in self.values):
            self.keys.discard((hive, key.lower()))


class FakeTasks:
    def __init__(self, states):
        self.s = dict(states)

    def states(self, paths):
        return {p: self.s.get(p, -1) for p in paths}

    def set_enabled(self, path, enabled):
        if path not in self.s:
            return False
        self.s[path] = 3 if enabled else 1
        return True


class FakeApps:
    def __init__(self, names):
        self.present = {n: {"Name": n, "PackageFullName": n + "_1.0_x64__8wekyb3d8bbwe",
                            "PackageFamilyName": n + "_8wekyb3d8bbwe", "NonRemovable": False} for n in names}
        self.staged = dict(self.present)

    def installed(self):
        return dict(self.present)

    def remove(self, full):
        name = full.split("_")[0]
        return self.present.pop(name, None) is not None

    def restore(self, family):
        name = family.split("_")[0]
        if name not in self.staged:
            return False
        self.present[name] = self.staged[name]
        return True


WIN11_PRO_ADMIN = d.Env(26100, "Professional", True)


def tweak(id):
    return next(t for t in d.TWEAKS if t.id == id)


class Engine(unittest.TestCase):
    def setUp(self):
        self.journal = Path(tempfile.mkdtemp()) / "journal.json"

    def make(self, registry=None, tasks=None, apps=(), env=WIN11_PRO_ADMIN):
        return d.Debloater(registry or FakeRegistry(), tasks or FakeTasks({}), FakeApps(apps), env, self.journal)

    def test_undo_restores_the_previous_value_not_a_default(self):
        # This PC had its own setting: advertising ID on, as DWORD 1. Undo
        # must put 1 back, not delete the value.
        reg = FakeRegistry({(d.HKCU, r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo", "Enabled"): (4, 1)})
        b = self.make(reg)
        self.assertTrue(b.apply(tweak("advertising-id")).ok)
        self.assertEqual(b.state(tweak("advertising-id")), "applied")
        self.assertTrue(b.undo("advertising-id").ok)
        self.assertEqual(reg.get(d.HKCU, r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo", "Enabled"), (4, 1))

    def test_undo_removes_a_value_that_was_not_there(self):
        b = self.make()
        b.apply(tweak("web-search"))
        b.undo("web-search")
        self.assertEqual(b.registry.values, {})

    def test_a_key_cleam_created_is_removed_on_undo(self):
        # The classic context menu is switched on by the key existing at all.
        b = self.make()
        t = tweak("classic-context-menu")
        b.apply(t)
        key = t.reg[0].key
        self.assertTrue(b.registry.key_exists(d.HKCU, key))
        b.undo(t.id)
        self.assertFalse(b.registry.key_exists(d.HKCU, key))

    def test_applying_twice_keeps_the_original_state_for_undo(self):
        reg = FakeRegistry({(d.HKCU, d.ADV, "HideFileExt"): (4, 1)})
        b = self.make(reg)
        b.apply(tweak("file-extensions"))
        b.apply(tweak("file-extensions"))  # the second one must not record 0 as "before"
        b.undo("file-extensions")
        self.assertEqual(reg.get(d.HKCU, d.ADV, "HideFileExt"), (4, 1))

    def test_journal_is_written_before_the_registry_changes(self):
        class Exploding(FakeRegistry):
            def set(self, *a):
                raise OSError(5, "Access is denied")
        b = self.make(Exploding())
        out = b.apply(tweak("advertising-id"))
        self.assertFalse(out.ok)
        self.assertIn("advertising-id", d.load_journal(self.journal)["tweaks"])

    def test_services_only_when_the_service_exists(self):
        key = r"SYSTEM\CurrentControlSet\Services\DiagTrack"
        with_service = self.make(FakeRegistry({(d.HKLM, key, "Start"): (4, 2)}))
        with_service.apply(tweak("telemetry-service"))
        self.assertEqual(with_service.registry.get(d.HKLM, key, "Start"), (4, 4))
        without = self.make()
        self.assertEqual(without.state(tweak("telemetry-service")), "n/a")
        without.apply(tweak("telemetry-service"))
        self.assertFalse(without.registry.key_exists(d.HKLM, key))  # never creates a fake service

    def test_tasks_are_disabled_and_re_enabled(self):
        t = tweak("telemetry-tasks")
        tasks = FakeTasks({p: 3 for p in t.tasks})
        b = self.make(tasks=tasks)
        b.apply(t)
        self.assertTrue(all(s == 1 for s in tasks.s.values()))
        self.assertEqual(b.state(t), "applied")
        b.undo(t.id)
        self.assertTrue(all(s == 3 for s in tasks.s.values()))

    def test_a_task_that_was_already_off_stays_off_after_undo(self):
        t = tweak("telemetry-tasks")
        tasks = FakeTasks({p: 1 for p in t.tasks})  # the user had disabled them already
        b = self.make(tasks=tasks)
        b.apply(t)
        b.undo(t.id)
        self.assertTrue(all(s == 1 for s in tasks.s.values()))

    def test_apps_come_back_from_the_staged_copy(self):
        b = self.make(apps=["Clipchamp.Clipchamp", "Microsoft.WindowsStore"])
        offered = [a.id for a, _ in b.removable_apps()]
        self.assertEqual(offered, ["Clipchamp.Clipchamp"])  # the Store is never offered
        app = next(a for a in d.APPS if a.id == "Clipchamp.Clipchamp")
        self.assertTrue(b.remove_app(app).ok)
        self.assertEqual(b.removable_apps(), [])
        self.assertTrue(b.restore_app(app.id).ok)
        self.assertIn("Clipchamp.Clipchamp", b.apps.present)
        self.assertEqual(d.load_journal(self.journal)["apps"], {})


class FakeAction:
    def __init__(self, applied):
        self.on = applied
        self.calls = []

    def applied(self):
        return self.on

    def snapshot(self):
        return self.on

    def apply(self):
        self.calls.append("apply")
        self.on = True
        return ""

    def undo(self, snap):
        # Same contract as the real ones: only reverse what was not so before.
        if snap is False:
            self.calls.append("undo")
            self.on = False
        return ""

    def reset(self):
        self.calls.append("reset")
        self.on = False
        return ""


class Actions(unittest.TestCase):
    def make(self, action):
        journal = Path(tempfile.mkdtemp()) / "journal.json"
        return d.Debloater(FakeRegistry(), FakeTasks({}), FakeApps([]), WIN11_PRO_ADMIN, journal,
                           actions={"reserved-storage": action})

    def test_apply_and_undo_through_the_windows_command(self):
        action = FakeAction(applied=False)  # reserved storage on
        b = self.make(action)
        t = tweak("reserved-storage")
        self.assertEqual(b.state(t), "not applied")
        self.assertTrue(b.apply(t).ok)
        self.assertEqual(b.state(t), "applied")
        self.assertTrue(b.undo(t.id).ok)
        self.assertEqual(action.calls, ["apply", "undo"])

    def test_undo_leaves_it_off_if_it_was_off_before_cleam(self):
        action = FakeAction(applied=True)  # someone had turned it off already
        b = self.make(action)
        b.apply(tweak("reserved-storage"))
        b.undo("reserved-storage")
        self.assertNotIn("undo", action.calls)

    def test_a_windows_without_the_feature_is_n_a(self):
        b = self.make(FakeAction(applied=None))
        self.assertEqual(b.state(tweak("reserved-storage")), "n/a")

    def test_a_refusal_is_reported(self):
        action = FakeAction(applied=False)
        action.apply = lambda: "reserved storage is in use"
        out = self.make(action).apply(tweak("reserved-storage"))
        self.assertFalse(out.ok)
        self.assertIn("in use", out.message)


class Revert(unittest.TestCase):
    """Turning off a tweak Cleam did not apply: back to Windows' defaults."""

    def make(self, registry, tasks=None, actions=None):
        journal = Path(tempfile.mkdtemp()) / "journal.json"
        return d.Debloater(registry, tasks or FakeTasks({}), FakeApps([]), WIN11_PRO_ADMIN, journal,
                           actions=actions)

    def test_a_policy_set_by_another_tool_is_removed(self):
        reg = FakeRegistry({(d.HKLM, rf"{d.POL}\DataCollection", "AllowTelemetry"): (4, 0)})
        b = self.make(reg)
        self.assertEqual(b.state(tweak("telemetry")), "applied")
        self.assertTrue(b.revert(tweak("telemetry")).ok)
        self.assertEqual(reg.values, {})

    def test_a_service_goes_back_to_its_windows_startup_type(self):
        key = r"SYSTEM\CurrentControlSet\Services\DiagTrack"
        reg = FakeRegistry({(d.HKLM, key, "Start"): (4, 4)})
        self.make(reg).revert(tweak("telemetry-service"))
        self.assertEqual(reg.get(d.HKLM, key, "Start"), (4, 2))  # Automatic, as Windows ships it

    def test_a_setting_with_a_known_default_gets_that_default(self):
        reg = FakeRegistry({(d.HKCU, r"Control Panel\Mouse", n): (1, "0")
                            for n in ("MouseSpeed", "MouseThreshold1", "MouseThreshold2")})
        self.make(reg).revert(tweak("mouse-acceleration"))
        self.assertEqual(reg.get(d.HKCU, r"Control Panel\Mouse", "MouseThreshold2"), (1, "10"))

    def test_disabled_tasks_are_enabled_again(self):
        t = tweak("telemetry-tasks")
        tasks = FakeTasks({p: 1 for p in t.tasks})
        self.make(FakeRegistry(), tasks).revert(t)
        self.assertTrue(all(s == 3 for s in tasks.s.values()))

    def test_an_action_is_reset(self):
        action = FakeAction(applied=True)
        self.make(FakeRegistry(), actions={"reserved-storage": action}).revert(tweak("reserved-storage"))
        self.assertEqual(action.calls, ["reset"])

    def test_cleams_own_change_is_undone_from_the_journal_instead(self):
        reg = FakeRegistry({(d.HKCU, d.ADV, "HideFileExt"): (4, 7)})  # an unusual value of the user's
        b = self.make(reg)
        b.apply(tweak("file-extensions"))
        b.revert(tweak("file-extensions"))
        self.assertEqual(reg.get(d.HKCU, d.ADV, "HideFileExt"), (4, 7))  # not the default, the user's own


class MergedValues(unittest.TestCase):
    """Settings that share one registry value with others."""

    def field(self, name, reg):
        a = d.ACTIONS[name]
        return d.RegistryField(a.key, a.name, a._on, a._off, a._is_on, registry=reg)

    def test_windowed_games_keeps_vrr_and_auto_hdr(self):
        key = r"Software\Microsoft\DirectX\UserGpuPreferences"
        reg = FakeRegistry({(d.HKCU, key, "DirectXUserGlobalSettings"): (1, "VRROptimizeEnable=0;AutoHDREnable=1;")})
        f = self.field("windowed-games", reg)
        self.assertFalse(f.applied())
        snap = f.snapshot()
        f.apply()
        self.assertEqual(reg.get(d.HKCU, key, "DirectXUserGlobalSettings")[1],
                         "VRROptimizeEnable=0;AutoHDREnable=1;SwapEffectUpgradeEnable=1;")
        self.assertTrue(f.applied())
        f.undo(snap)
        self.assertEqual(reg.get(d.HKCU, key, "DirectXUserGlobalSettings")[1], "VRROptimizeEnable=0;AutoHDREnable=1;")

    def test_sticky_keys_hotkey_bit_only(self):
        key = r"Control Panel\Accessibility\StickyKeys"
        reg = FakeRegistry({(d.HKCU, key, "Flags"): (1, "511")})  # Sticky Keys itself ON
        f = self.field("sticky-keys-hotkey", reg)
        f.apply()
        self.assertEqual(reg.get(d.HKCU, key, "Flags")[1], "507")  # hotkey off, Sticky Keys still on
        f.reset()
        self.assertEqual(reg.get(d.HKCU, key, "Flags")[1], "511")


@unittest.skipUnless(d.OS == "windows", "the real registry")
class RealRegistry(unittest.TestCase):
    """WinRegistry against the real registry, in a throwaway HKCU key Cleam
    owns -- the fakes above cannot catch a wrong winreg flag or call."""

    KEY = r"Software\CleamTest\Nested"

    def tearDown(self):
        r = d.WinRegistry()
        r.delete_empty_key(d.HKCU, self.KEY)
        r.delete_empty_key(d.HKCU, r"Software\CleamTest")

    def test_round_trip_and_undo_removes_the_created_key(self):
        journal = Path(tempfile.mkdtemp()) / "journal.json"
        tw = d.Tweak("test", "Privacy", "t", "t", (d.Reg(d.HKCU, self.KEY, "Flag", 1),
                                                    d.Reg(d.HKCU, self.KEY, "", "", d.SZ)))
        b = d.Debloater(d.WinRegistry(), FakeTasks({}), FakeApps([]), WIN11_PRO_ADMIN, journal)
        self.assertEqual(b.state(tw), "not applied")
        self.assertTrue(b.apply(tw).ok)
        self.assertEqual(b.state(tw), "applied")
        self.assertEqual(b.registry.get(d.HKCU, self.KEY, "Flag"), (d.DWORD, 1))
        self.assertTrue(b.undo("test").ok)
        self.assertIsNone(b.registry.get(d.HKCU, self.KEY, "Flag"))
        self.assertFalse(b.registry.key_exists(d.HKCU, self.KEY))


class Availability(unittest.TestCase):
    def test_edition_limited_policies_are_not_offered_on_pro(self):
        why = d.availability(tweak("consumer-features"), d.Env(19045, "Professional", True))
        self.assertIn("not on Pro", why)
        self.assertEqual(d.availability(tweak("consumer-features"), d.Env(19045, "Enterprise", True)), "")

    def test_version_and_build_gates(self):
        win10 = d.Env(19045, "Professional", True)
        self.assertIn("Windows 11", d.availability(tweak("recall"), win10))
        self.assertIn("build 26100", d.availability(tweak("recall"), d.Env(22631, "Professional", True)))
        self.assertIn("Windows 10", d.availability(tweak("cortana"), d.Env(26100, "Professional", True)))

    def test_machine_wide_changes_need_admin(self):
        user = d.Env(26100, "Professional", False)
        self.assertEqual(d.availability(tweak("advertising-id"), user), "")  # per-user: fine
        self.assertEqual(d.availability(tweak("telemetry"), user), "needs admin")


class Catalogue(unittest.TestCase):
    def test_ids_are_unique_and_groups_known(self):
        ids = [t.id for t in d.TWEAKS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(t.group in d.GROUPS for t in d.TWEAKS))

    def test_nothing_lowers_security(self):
        # A cleaner must never switch these off, whatever other debloaters do.
        forbidden = ("windefend", "wuauserv", "smartscreen", "disableantispyware", "enablelua", "mpssvc",
                     "securityhealthservice", "wscsvc", "usoSvc".lower())
        for t in d.TWEAKS:
            text = " ".join(f"{r.key} {r.name}" for r in t.reg).lower()
            for word in forbidden:
                self.assertNotIn(word, text, t.id)

    def test_apps_that_other_things_depend_on_are_never_offered(self):
        ids = {a.id for a in d.APPS}
        for unsafe in ("Microsoft.WindowsStore", "Microsoft.MicrosoftEdge.Stable", "Microsoft.WindowsTerminal",
                       "Microsoft.XboxIdentityProvider", "Microsoft.Xbox.TCUI", "Microsoft.GetHelp",
                       "Microsoft.DesktopAppInstaller", "Microsoft.SecHealthUI"):
            self.assertNotIn(unsafe, ids)

    def test_apps_whose_data_lives_on_this_pc_are_never_pre_ticked(self):
        # Removing a package deletes its data; undo only brings back the app.
        for app in d.APPS:
            if "deleted with it" in app.about:
                self.assertFalse(app.default, app.id)
        self.assertFalse(next(a for a in d.APPS if a.id == "Microsoft.MicrosoftStickyNotes").default)

    def test_every_tweak_explains_itself(self):
        for t in d.TWEAKS:
            self.assertTrue(t.about and t.title, t.id)
            self.assertTrue(t.reg or t.tasks or t.action, t.id)


if __name__ == "__main__":
    unittest.main()
