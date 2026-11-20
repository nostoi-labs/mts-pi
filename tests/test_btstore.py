"""The keeper of the protected card (mtspi/btstore.py): what reaches the card, and when."""
import importlib.util
import os
import pathlib
import tempfile
import unittest

SRC = pathlib.Path(__file__).resolve().parent.parent / "mtspi" / "btstore.py"


def module(live, kept, everything):
    spec = importlib.util.spec_from_file_location("btstore_under_test", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.LIVE, m.KEPT, m.ALL, m.OPEN = str(live), str(kept), everything, ""
    m.MARK = str(pathlib.Path(live).parent / "mark")
    m.say = lambda *a: None
    return m


class Keeper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.live, self.kept = root / "live", root / "kept"
        self.live.mkdir()
        self.kept.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_pairings_only_info_files_cause_a_copy(self):
        m = module(self.live, self.kept, False)
        radio = self.live / "AA:BB:CC:DD:EE:FF"
        radio.mkdir()
        (radio / "settings").write_text("[General]\n")
        self.assertFalse(m.save("test"))                      # BlueZ's own files alone never reach the card
        dev = radio / "11:22:33:44:55:66"
        dev.mkdir()
        (dev / "info").write_text("[LinkKey]\nKey=1\n")
        self.assertTrue(m.save("test"))
        self.assertEqual((self.kept / "AA:BB:CC:DD:EE:FF" / "11:22:33:44:55:66" / "info").read_text(), "[LinkKey]\nKey=1\n")
        self.assertTrue((self.kept / "AA:BB:CC:DD:EE:FF" / "settings").exists())   # copied along
        self.assertFalse(m.save("test"))

    def test_all_every_file_counts_and_half_written_ones_do_not(self):
        m = module(self.live, self.kept, True)
        (self.live / "settings.json").write_text('{"a": 1}')
        (self.live / "phonebook.json.tmp").write_text("half")
        self.assertTrue(m.save("test"))
        self.assertEqual((self.kept / "settings.json").read_text(), '{"a": 1}')
        self.assertFalse((self.kept / "phonebook.json.tmp").exists())
        self.assertFalse(m.save("test"))
        (self.live / "settings.json").write_text('{"a": 2}')
        self.assertTrue(m.save("test"))
        self.assertEqual((self.kept / "settings.json").read_text(), '{"a": 2}')
        (self.live / "settings.json").unlink()
        (self.live / "other").write_text("x")
        self.assertTrue(m.save("test"))
        self.assertFalse((self.kept / "settings.json").exists())

    def test_an_empty_memory_folder_never_wipes_the_card(self):
        for everything in (False, True):
            m = module(self.live, self.kept, everything)
            dev = self.kept / "AA:BB:CC:DD:EE:FF" / "11:22:33:44:55:66"
            dev.mkdir(parents=True, exist_ok=True)
            (dev / "info").write_text("key")
            self.assertFalse(m.save("test"))
            self.assertEqual((dev / "info").read_text(), "key")

    def test_load_then_nothing_to_copy_back(self):
        os.environ["BTSTORE_KEPT"] = str(self.kept)          # tells load() this is not the real data partition
        self.addCleanup(os.environ.pop, "BTSTORE_KEPT", None)
        m = module(self.live, self.kept, True)
        (self.kept / "sub").mkdir()
        (self.kept / "sub" / "f").write_text("1")
        self.assertEqual(m.load(), 0)
        self.assertEqual((self.live / "sub" / "f").read_text(), "1")
        self.assertEqual((self.live / "sub").stat().st_mode & 0o777, (self.kept / "sub").stat().st_mode & 0o777)
        self.assertFalse(m.save("test"))
        self.assertEqual(m.glance(str(self.live)), m.glance(str(self.live)))


if __name__ == "__main__":
    unittest.main()
