#!/usr/bin/env python3
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LIB = Path(os.environ.get("ARMADA_CHARGE_LIMIT_TEST_LIB", ROOT / "system_files/usr/lib/armada/armada_charge_limit.py"))
spec = importlib.util.spec_from_file_location("charge_limit", LIB)
limit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(limit)
REAL_WRITE_TEXT = Path.write_text


class FakeBattmgr:
    # Mirrors upstream qcom_battmgr's threshold setters: both clamp, moving the end
    # keeps the old start threshold, and the firmware limit always stays enabled.
    def __init__(self, battery, start=0, end=0):
        self.end_path = battery / limit.ATTR
        self.start_path = battery / limit.START_ATTR
        self.start, self.end = start, end
        self.sync()

    def sync(self):
        REAL_WRITE_TEXT(self.end_path, f"{self.end}\n")
        REAL_WRITE_TEXT(self.start_path, f"{self.start}\n")

    def set_end(self, end):
        end = min(max(end, 55), 100)
        delta = end - self.start if self.start and end > self.start else 5
        self.start, self.end = end - delta, end

    def set_start(self, start):
        start = min(max(start, 50), 95)
        if start > self.end:
            self.end = min(start + 5, 100)
        self.start = start

    def write_text(self, path, text, *args, **kwargs):
        if path == self.end_path:
            self.set_end(int(text))
        elif path == self.start_path:
            self.set_start(int(text))
        else:
            return REAL_WRITE_TEXT(path, text, *args, **kwargs)
        self.sync()
        return len(text)

    def thresholds(self):
        return self.start, self.end


class ChargeLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for key, name in (("SYS", "sys"), ("PROC", "proc")):
            path = self.root / name
            path.mkdir()
            self.enterContext(patch.object(limit, key, path))
        self.enterContext(patch.object(limit, "SAVED", self.root / "etc/armada/charge-limit"))
        self.supplies = limit.SYS / "class/power_supply"

    def supply(self, name, kind, threshold=None, mode=0o644, start=None, start_mode=0o644):
        path = self.supplies / name
        path.mkdir(parents=True)
        (path / "type").write_text(f"{kind}\n")
        for attr, value, attr_mode in ((limit.ATTR, threshold, mode), (limit.START_ATTR, start, start_mode)):
            if value is not None:
                (path / attr).write_text(f"{value}\n")
                (path / attr).chmod(attr_mode)
        return path

    def battmgr(self, start=0, end=0):
        battery = self.supply("qcom-battmgr-bat", "Battery", threshold=end, start=start)
        fake = FakeBattmgr(battery, start, end)
        self.enterContext(patch.object(Path, "write_text", lambda path, text, *a, **k: fake.write_text(path, text, *a, **k)))
        return battery, fake

    def sysfs_value(self, battery, attr=limit.ATTR):
        return (battery / attr).read_text().strip()

    def test_finds_battery_by_type(self):
        self.supply("usb", "USB")
        self.supply("ac", "Mains", threshold=90)
        self.supply("a-bms", "Battery")
        battery = self.supply("qcom-battmgr-bat", "Battery", threshold=100)
        self.assertEqual(limit.find_battery(), battery)
        self.assertTrue(limit.supported(battery))
        self.assertEqual(limit.current_limit(), 100)

    def test_missing_attribute_is_unsupported(self):
        self.supply("battery", "Battery")
        self.assertIsNone(limit.find_battery())
        self.assertFalse(limit.supported(None))
        self.assertIsNone(limit.current_limit())
        with self.assertRaises(RuntimeError):
            limit.set_limit(80)
        self.assertFalse(limit.SAVED.exists())
        limit.atomic_write(limit.SAVED, "80\n")
        self.assertEqual(limit.restore(), (False, None))

    def test_no_battery_is_unsupported(self):
        self.supply("usb", "USB", threshold=90)
        self.assertIsNone(limit.find_battery())
        self.assertIsNone(limit.current_limit())

    def test_no_power_supply_class(self):
        self.assertIsNone(limit.find_battery())
        self.assertIsNone(limit.current_limit())

    def test_read_only_attribute_is_unsupported(self):
        # Runs as root too: support is decided by the mode bits, not access(2).
        battery = self.supply("battery", "Battery", threshold=100, mode=0o444)
        self.assertEqual(limit.find_battery(), battery)
        self.assertFalse(limit.supported(battery))
        self.assertIsNone(limit.current_limit())
        with self.assertRaises(RuntimeError):
            limit.set_limit(80)
        self.assertEqual(self.sysfs_value(battery), "100")
        self.assertFalse(limit.SAVED.exists())

    def test_write_error_is_runtime_error(self):
        battery = self.supply("battery", "Battery", threshold=100)
        with patch.object(Path, "write_text", side_effect=OSError(22, "Invalid argument")):
            with self.assertRaisesRegex(RuntimeError, "could not update.*Invalid argument"):
                limit.apply(80, battery)

    def test_set_limit_writes_and_persists(self):
        battery = self.supply("battery", "Battery", threshold=100)
        self.assertEqual(limit.set_limit(80), 80)
        self.assertEqual(self.sysfs_value(battery), "80")
        self.assertFalse((battery / limit.START_ATTR).exists())
        self.assertEqual(limit.SAVED.read_text(), "80\n")
        self.assertEqual(limit.saved_limit(), 80)
        self.assertEqual(limit.current_limit(), 80)

    def test_start_threshold_follows_the_limit(self):
        battery = self.supply("battery", "Battery", threshold=100, start=0)
        self.assertEqual(limit.set_limit(80), 80)
        self.assertEqual(self.sysfs_value(battery, limit.START_ATTR), "75")
        self.assertEqual(limit.set_limit(100), 100)
        self.assertEqual(self.sysfs_value(battery, limit.START_ATTR), "95")

    def test_read_only_start_threshold_is_left_alone(self):
        battery = self.supply("battery", "Battery", threshold=100, start=0, start_mode=0o444)
        self.assertEqual(limit.set_limit(80), 80)
        self.assertEqual(self.sysfs_value(battery, limit.START_ATTR), "0")
        self.assertEqual(limit.restore(), (True, None))

    def test_start_threshold_skipped_for_out_of_range_read_back(self):
        battery = self.supply("battery", "Battery", threshold=100, start=0)
        with patch.object(limit, "read_limit", return_value=0):
            self.assertEqual(limit.apply(80, battery), 0)
        self.assertEqual(self.sysfs_value(battery, limit.START_ATTR), "0")

    def test_battmgr_off_after_a_low_limit_resumes_near_full(self):
        _battery, fake = self.battmgr()
        self.assertEqual(limit.set_limit(80), 80)
        self.assertEqual(fake.thresholds(), (75, 80))
        self.assertEqual(limit.set_limit(100), 100)
        self.assertEqual(fake.thresholds(), (95, 100))
        self.assertEqual(limit.set_limit(60), 60)
        self.assertEqual(fake.thresholds(), (55, 60))
        self.assertEqual(limit.set_limit(100), 100)
        self.assertEqual(fake.thresholds(), (95, 100))

    def test_rounding_on_read_back_saves_applied_value(self):
        self.supply("battery", "Battery", threshold=100)
        with patch.object(limit, "read_limit", return_value=75):
            self.assertEqual(limit.set_limit(80), 75)
        self.assertEqual(limit.saved_limit(), 75)

    def test_restore_settles_on_rounding_firmware(self):
        battery = self.supply("battery", "Battery", threshold=100)
        with patch.object(limit, "read_limit", return_value=75):
            limit.set_limit(80)
        (battery / limit.ATTR).write_text("75\n")
        with patch.object(limit, "apply") as apply:
            self.assertEqual(limit.restore(), (True, None))
        apply.assert_not_called()

    def test_clamped_value_is_saved_and_restored(self):
        battery = self.supply("battery", "Battery", threshold=100)
        with patch.object(limit, "read_limit", return_value=72):
            self.assertEqual(limit.set_limit(70), 72)
        self.assertEqual(limit.saved_limit(), 72)
        (battery / limit.ATTR).write_text("100\n")
        self.assertEqual(limit.restore(), (True, (100, None)))
        self.assertEqual(self.sysfs_value(battery), "72")

    def test_restore_reports_what_it_replaced(self):
        battery = self.supply("battery", "Battery", threshold=100, start=0)
        limit.set_limit(85)
        (battery / limit.ATTR).write_text("100\n")
        (battery / limit.START_ATTR).write_text("95\n")
        self.assertEqual(limit.restore(), (True, (100, 95)))
        self.assertEqual(limit.thresholds(battery), (85, 80))
        with patch.object(limit, "apply") as apply:
            self.assertEqual(limit.restore(), (True, None))
        apply.assert_not_called()

    def test_restore_rewrites_a_drifted_start_threshold(self):
        battery = self.supply("battery", "Battery", threshold=100, start=0)
        limit.set_limit(80)
        (battery / limit.START_ATTR).write_text("55\n")
        self.assertEqual(limit.restore(), (True, (80, 55)))
        self.assertEqual(self.sysfs_value(battery, limit.START_ATTR), "75")

    def test_restore_without_saved_value_is_done(self):
        battery = self.supply("battery", "Battery", threshold=90)
        self.assertEqual(limit.restore(), (True, None))
        self.assertEqual(self.sysfs_value(battery), "90")

    def test_restore_waits_for_battery(self):
        limit.atomic_write(limit.SAVED, "80\n")
        self.assertEqual(limit.restore(), (False, None))
        battery = self.supply("battery", "Battery", threshold=100)
        self.assertEqual(limit.restore(), (True, (100, None)))
        self.assertEqual(self.sysfs_value(battery), "80")

    def test_restore_waits_while_battmgr_answers_eagain(self):
        battery = self.supply("battery", "Battery", threshold=100)
        limit.atomic_write(limit.SAVED, "80\n")
        with patch.object(limit, "read_limit", return_value=None), patch.object(limit, "apply") as apply:
            self.assertEqual(limit.restore(), (False, None))
        apply.assert_not_called()
        self.assertEqual(self.sysfs_value(battery), "100")

    def test_invalid_saved_value_is_ignored(self):
        battery = self.supply("battery", "Battery", threshold=100)
        for text in ("abc\n", "50\n", "101\n", ""):
            limit.atomic_write(limit.SAVED, text)
            self.assertIsNone(limit.saved_limit(), text)
            self.assertEqual(limit.restore(), (True, None))
        self.assertEqual(self.sysfs_value(battery), "100")

    def test_restore_raises_write_errors(self):
        battery = self.supply("battery", "Battery", threshold=100)
        limit.atomic_write(limit.SAVED, "80\n")
        with patch.object(Path, "write_text", side_effect=OSError(11, "Resource temporarily unavailable")):
            with self.assertRaises(RuntimeError):
                limit.restore()
        self.assertEqual(self.sysfs_value(battery), "100")

    def test_validate(self):
        for value in (55, 60, 80, 100):
            self.assertEqual(limit.validate(value), value)
        for value in (0, 45, 50, 54, 56, 82, 101, 105, -80, True, False, "80", 80.0, None):
            with self.assertRaises(ValueError, msg=repr(value)):
                limit.validate(value)

    def test_apply_checks_range_only(self):
        battery = self.supply("battery", "Battery", threshold=100)
        self.assertEqual(limit.apply(72, battery), 72)
        for value in (50, 54, 101, True, "80"):
            with self.assertRaises(ValueError, msg=repr(value)):
                limit.apply(value, battery)
        self.assertEqual(self.sysfs_value(battery), "72")
        self.assertFalse(limit.SAVED.exists())

    def test_probe_writes_reads_back_and_restores(self):
        battery = self.supply("battery", "Battery", threshold=100)
        self.supply("usb", "USB")
        (limit.PROC / "device-tree").mkdir()
        (limit.PROC / "device-tree/model").write_text("AYN Odin 3\0")
        out = io.StringIO()
        self.assertEqual(limit.probe(out=out), 0)
        text = out.getvalue()
        self.assertIn("model=AYN Odin 3\n", text)
        self.assertIn("usb type=USB", text)
        self.assertIn("battery/charge_control_end_threshold=100 mode=0o644 writable=yes", text)
        self.assertIn("original_end=100 original_start=none requested=80", text)
        self.assertIn("read_back_end=80 read_back_start=none\n", text)
        self.assertIn("restored_end=100 restored_start=none", text)
        self.assertNotIn("note=", text)
        self.assertIn("result=ok", text)
        self.assertEqual(self.sysfs_value(battery), "100")
        self.assertFalse(limit.SAVED.exists())

    def test_probe_restores_both_battmgr_thresholds(self):
        battery, fake = self.battmgr()
        limit.set_limit(80)
        out = io.StringIO()
        self.assertEqual(limit.probe(out=out), 0)
        text = out.getvalue()
        self.assertIn(f"{battery.name}/charge_control_start_threshold=75", text)
        self.assertIn("original_end=80 original_start=75 requested=75", text)
        self.assertIn("read_back_end=75 read_back_start=70", text)
        self.assertIn("restored_end=80 restored_start=75", text)
        self.assertIn("note=qcom_battmgr cannot turn the firmware limit off", text)
        self.assertEqual(fake.thresholds(), (75, 80))

    def test_probe_never_leaves_an_unset_battmgr_capped(self):
        # Before any limit is set, battmgr reads 0; writing that back would clamp to 55.
        _battery, fake = self.battmgr(start=0, end=0)
        out = io.StringIO()
        self.assertEqual(limit.probe(out=out), 0)
        text = out.getvalue()
        self.assertIn("note=original end 0 is outside 55-100; restoring 100 instead", text)
        self.assertIn("restored_end=100 restored_start=95", text)
        self.assertEqual(fake.thresholds(), (95, 100))

    def test_probe_reports_a_differing_read_back(self):
        self.supply("battery", "Battery", threshold=80)
        out = io.StringIO()
        with patch.object(limit, "read_limit", side_effect=[80, 70, 70, 80]):
            limit.probe(out=out)
        self.assertIn("requested=75", out.getvalue())
        self.assertIn("read_back_end=70 (differs: clamped, rounded or rejected by the firmware)", out.getvalue())

    def test_probe_restores_after_failed_write(self):
        battery = self.supply("battery", "Battery", threshold=90)
        out = io.StringIO()
        with patch.object(limit, "read_limit", side_effect=[90, None, 90]):
            self.assertEqual(limit.probe(value=60, out=out), 1)
        self.assertIn("write_error=could not read", out.getvalue())
        self.assertIn("restored_end=90", out.getvalue())
        self.assertIn("result=failed", out.getvalue())
        self.assertEqual(self.sysfs_value(battery), "90")

    def test_probe_unsupported(self):
        self.supply("battery", "Battery")
        out = io.StringIO()
        self.assertEqual(limit.probe(out=out), 1)
        self.assertIn("battery: no charge_control* files", out.getvalue())
        self.assertIn("result=unsupported", out.getvalue())

    def test_probe_read_only_attribute(self):
        battery = self.supply("battery", "Battery", threshold=100, mode=0o444)
        out = io.StringIO()
        self.assertEqual(limit.probe(out=out), 1)
        self.assertIn("battery/charge_control_end_threshold=100 mode=0o444 writable=no", out.getvalue())
        self.assertIn("result=unsupported", out.getvalue())
        self.assertEqual(self.sysfs_value(battery), "100")

    def test_main_validates_value_and_requires_root(self):
        with patch("sys.stderr", io.StringIO()):
            for value in ("50", "82"):
                with self.assertRaises(SystemExit):
                    limit.main(["--value", value])
            if os.geteuid() != 0:
                with patch.dict(os.environ, {"ARMADA_CHARGE_LIMIT_ALLOW_NONROOT": ""}):
                    with self.assertRaises(SystemExit):
                        limit.main([])


if __name__ == "__main__":
    unittest.main()
