import unittest

from cleam import system


class Human(unittest.TestCase):
    def test_units(self):
        self.assertEqual(system.human(0), "0 B")
        self.assertEqual(system.human(512), "512 B")
        self.assertEqual(system.human(1536), "1.5 KiB")
        self.assertEqual(system.human(5 * 1024**3), "5.0 GiB")


class Relaunch(unittest.TestCase):
    def test_a_frozen_build_does_not_repeat_its_own_path(self):
        program, arguments = system.relaunch_parts(r"C:\cleam.exe", [r"C:\cleam.exe", "--json"], frozen=True)
        self.assertEqual((program, arguments), (r"C:\cleam.exe", "--json"))

    def test_running_from_source_passes_the_script_to_the_interpreter(self):
        program, arguments = system.relaunch_parts("python.exe", [r"C:\Scripts\cleam-gui", "scan"], frozen=False)
        self.assertEqual(program, "python.exe")
        self.assertEqual(arguments, r"C:\Scripts\cleam-gui scan")

    def test_a_path_with_spaces_stays_one_argument(self):
        _, arguments = system.relaunch_parts("python.exe", [r"C:\Program Files\cleam\go.py"], frozen=False)
        self.assertEqual(arguments, '"C:\\Program Files\\cleam\\go.py"')

    def test_elevating_is_a_windows_only_no_op_elsewhere(self):
        if system.OS != "windows":
            self.assertFalse(system.relaunch_as_admin())


if __name__ == "__main__":
    unittest.main()
