"""相对鼠标 / 扫描码 / 按住键 / 无目标键盘动作。不需要 Windows：控制逻辑都可注入。"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import act as A  # noqa: E402
from ui_locate import actions  # noqa: E402


class FakeMouse:
    """系统光标的替身：相对位移乘一个「加速」系数后累加，可以锁死。"""

    def __init__(self, x=500, y=400, gain=1.0, locked=False, w=1920, h=1080):
        self.x, self.y, self.gain, self.locked = x, y, gain, locked
        self.w, self.h = w, h
        self.events = []

    def read(self):
        return {"x": int(self.x), "y": int(self.y)}

    def send(self, item):
        mi = item.u.mi
        self.events.append((mi.dx, mi.dy, mi.dwFlags))
        if not self.locked:
            self.x = max(0, min(self.w - 1, self.x + round(mi.dx * self.gain)))
            self.y = max(0, min(self.h - 1, self.y + round(mi.dy * self.gain)))
        return 1


class SplitTests(unittest.TestCase):
    def test_sums_and_limits(self):
        for d in (0, 1, -1, 119, 120, 121, 500, -733):
            parts = actions._split(d)
            self.assertEqual(sum(parts), d)
            self.assertTrue(all(abs(p) <= actions._MAX_STEP for p in parts))


class SeekTests(unittest.TestCase):
    def test_one_to_one_reaches_target(self):
        m = FakeMouse()
        r = actions.seek_relative(1500, 90, read=m.read, send=m.send, pause_s=0, settle_s=0)
        self.assertTrue(r["tracking"])
        self.assertEqual((m.x, m.y), (1500, 90))
        # 只送相对移动：没有 ABSOLUTE 标志
        self.assertTrue(all(f == actions._MOVE for _dx, _dy, f in m.events))

    def test_acceleration_corrected_by_feedback(self):
        m = FakeMouse(gain=1.6)  # 加速没关掉也要靠回读收敛
        r = actions.seek_relative(200, 900, read=m.read, send=m.send, pause_s=0, settle_s=0)
        self.assertTrue(r["tracking"])
        self.assertLessEqual(abs(m.x - 200), 1)
        self.assertLessEqual(abs(m.y - 900), 1)

    def test_locked_cursor_pushes_once(self):
        m = FakeMouse(locked=True)
        r = actions.seek_relative(1000, 400, read=m.read, send=m.send, pause_s=0, settle_s=0)
        self.assertFalse(r["tracking"])
        self.assertEqual(sum(dx for dx, _dy, _f in m.events), 500)  # 1:1 推了一次，没反复推

    def test_unreadable_cursor(self):
        r = actions.seek_relative(10, 10, read=lambda: None, send=lambda e: 1)
        self.assertIsNone(r["tracking"])
        self.assertEqual(r["sent"], 0)


class ModeTests(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {"UI_LOCATE_POINTER": "", "UI_LOCATE_KEYS": ""}), \
                mock.patch("ui_locate.config.runtime_settings", return_value={}):
            self.assertEqual(actions.pointer_mode(), "absolute")
            self.assertEqual(actions.key_mode(), "vk")

    def test_env_and_override(self):
        with mock.patch.dict(os.environ, {"UI_LOCATE_POINTER": "relative", "UI_LOCATE_KEYS": "scancode"}):
            self.assertEqual(actions.pointer_mode(), "relative")
            self.assertEqual(actions.key_mode(), "scancode")
            self.assertEqual(actions.pointer_mode("absolute"), "absolute")
        with mock.patch.dict(os.environ, {"UI_LOCATE_POINTER": "bogus"}):
            self.assertEqual(actions.pointer_mode(), "absolute")

    def test_runtime_json(self):
        with mock.patch.dict(os.environ, {"UI_LOCATE_POINTER": ""}), \
                mock.patch("ui_locate.config.runtime_settings", return_value={"pointer": "relative"}):
            self.assertEqual(actions.pointer_mode(), "relative")

    def test_scancode_flag(self):
        with mock.patch.object(actions, "_scan_code", return_value=0x1E):
            vk = actions._key_event(0x41)
            self.assertEqual((vk.u.ki.wVk, vk.u.ki.wScan), (0x41, 0x1E))
            self.assertFalse(vk.u.ki.dwFlags & actions._KEYEVENTF_SCANCODE)
            sc = actions._key_event(0x41, up=True, mode="scancode")
            self.assertTrue(sc.u.ki.dwFlags & actions._KEYEVENTF_SCANCODE)
            self.assertTrue(sc.u.ki.dwFlags & actions._KEYEVENTF_KEYUP)


class BuildActionTests(unittest.TestCase):
    T = {"cx": 10, "cy": 20}

    def test_pointer_rides_on_mouse_actions(self):
        name, p = A.build_action({"action": "click", "pointer": "relative"}, self.T)
        self.assertEqual(p["pointer"], "relative")
        name, p = A.build_action({"action": "press", "key": "esc", "pointer": "relative"}, self.T)
        self.assertNotIn("pointer", p)

    def test_hold(self):
        name, p = A.build_action({"action": "hold", "keys": "shift+w", "duration_ms": 800,
                                  "keys_mode": "scancode"}, self.T)
        self.assertEqual(name, "hold")
        self.assertEqual(p, {"keys": "shift+w", "duration_ms": 800, "keys_mode": "scancode"})


class KeyboardWithoutTarget(unittest.TestCase):
    def test_press_needs_no_target_and_compares_whole_screen(self):
        seen = {}

        def compare(a, b, x, y, r):
            seen["xy"] = (x, y)
            return 12.0

        res = A.act({"action": "press", "key": "esc", "verify": "screen", "settle_ms": 1},
                    probe=lambda *a, **k: None, capture=lambda m: object(),
                    perform=lambda n, p: {"sent": 2, "name": n}, compare=compare,
                    sleep=lambda s: None)
        self.assertTrue(res["ok"])
        self.assertEqual(res["performed"]["name"], "press")
        self.assertEqual(seen["xy"], (None, None))
        self.assertEqual(res["verify"]["scope"], "screen")
        self.assertTrue(res["verified"])

    def test_click_still_needs_target(self):
        with self.assertRaises(A.ActError):
            A.act({"action": "click"}, probe=lambda *a, **k: None, capture=lambda m: None,
                  perform=lambda n, p: {}, compare=lambda *a: 0.0, sleep=lambda s: None)


if __name__ == "__main__":
    unittest.main()
