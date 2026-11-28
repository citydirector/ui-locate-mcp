"""adb 目标的单元测试：不需要设备，子进程全部打桩。

覆盖三件事：原始帧解码（头长 12 / 16 字节、尺寸对不上要拒绝）、拖动命令序列
（两端停顿有就加、没有就不加）、act 层把 adb 写法路由到 device 而不是 window。

跑法：
  python -m unittest discover -s tests -p "test_*.py"
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import act as A  # noqa: E402
from ui_locate import adb  # noqa: E402


def _frame(w: int, h: int, header: int) -> tuple[bytes, np.ndarray]:
    rng = np.random.default_rng(w * h + header)
    rgba = rng.integers(0, 256, size=(h, w, 4), dtype=np.uint8)
    head = struct.pack("<III", w, h, 1) + (b"\0\0\0\0" if header == 16 else b"")
    return head + rgba.tobytes(), rgba


class DecodeRaw(unittest.TestCase):
    def test_header_12_and_16(self):
        for header in (12, 16):
            buf, rgba = _frame(7, 5, header)
            bgr = adb.decode_raw(buf)
            self.assertEqual(bgr.shape, (5, 7, 3))
            # 通道顺序：RGBA → BGR
            np.testing.assert_array_equal(bgr[..., 0], rgba[..., 2])
            np.testing.assert_array_equal(bgr[..., 2], rgba[..., 0])
            self.assertTrue(bgr.flags["C_CONTIGUOUS"])

    def test_rejects_truncated_and_wrong_size(self):
        buf, _ = _frame(7, 5, 16)
        with self.assertRaises(adb.AdbError):
            adb.decode_raw(buf[:-1])        # 少一个字节 → 头长 15
        with self.assertRaises(adb.AdbError):
            adb.decode_raw(buf + b"\0" * 8)  # 多 8 字节 → 头长 24
        with self.assertRaises(adb.AdbError):
            adb.decode_raw(b"short")


class Spec(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(adb.parse_spec("adb:127.0.0.1:16384"), "127.0.0.1:16384")
        self.assertEqual(adb.parse_spec(" ADB: emulator-5554 "), "emulator-5554")
        self.assertEqual(adb.parse_spec("adb:"), "")
        self.assertIsNone(adb.parse_spec("window:adb"))
        self.assertIsNone(adb.parse_spec(None))

    def test_keycode(self):
        self.assertEqual(adb.keycode("esc"), "KEYCODE_ESCAPE")
        self.assertEqual(adb.keycode("a"), "KEYCODE_A")
        self.assertEqual(adb.keycode("keycode_back"), "KEYCODE_BACK")
        with self.assertRaises(adb.AdbError):
            adb.keycode("ctrl+s")


class DragCommands(unittest.TestCase):
    def test_no_holds(self):
        cmds = adb.drag_commands(0, 0, 100, 0, steps=4, duration_ms=0)
        self.assertEqual(cmds[0], "input motionevent DOWN 0 0")
        self.assertEqual(cmds[-1], "input motionevent UP 100 0")
        self.assertFalse(any(c.startswith("sleep") for c in cmds))
        self.assertEqual([c for c in cmds if "MOVE" in c][-1], "input motionevent MOVE 100 0")

    def test_holds_at_both_ends(self):
        cmds = adb.drag_commands(0, 0, 100, 50, steps=2, duration_ms=0,
                                 hold_start_ms=700, hold_end_ms=500)
        self.assertEqual(cmds[1], "sleep 0.700")       # 按下后先停，再移动
        self.assertEqual(cmds[-2], "sleep 0.500")      # 末端停，再松手
        self.assertIn("MOVE", cmds[2])

    def test_motionevent_missing_falls_back_to_swipe(self):
        calls: list[list[str]] = []

        def fake_shell(serial, cmds, timeout_s=15.0):
            calls.append(cmds)
            if len(calls) == 1:
                raise adb.AdbError("adb shell 退出码 255：Error: Unknown command: motionevent")
            return ""

        with mock.patch.object(adb, "shell", fake_shell):
            out = adb.drag("x", 0, 0, 10, 10, hold_start_ms=300)
        self.assertEqual(out["mode"], "swipe")
        self.assertTrue(calls[1][0].startswith("input swipe 0 0 10 10"))

    def test_other_errors_are_not_swallowed(self):
        def fake_shell(serial, cmds, timeout_s=15.0):
            raise adb.AdbError("adb shell 退出码 1：device offline")

        with mock.patch.object(adb, "shell", fake_shell):
            with self.assertRaises(adb.AdbError):
                adb.drag("x", 0, 0, 10, 10)


class Dispatch(unittest.TestCase):
    def test_right_click_rejected(self):
        with self.assertRaises(adb.AdbError):
            adb.dispatch("adb:x", "click", {"x": 1, "y": 2, "button": "right"})

    def test_type_quotes_for_sh(self):
        seen: list[str] = []
        with mock.patch.object(adb, "shell", lambda s, c, timeout_s=15.0: seen.extend(c) or ""):
            adb.type_text("x", "a b;rm")
        self.assertEqual(seen, ["input text 'a%sb;rm'"])
        with self.assertRaises(adb.AdbError):
            adb.type_text("x", "中文")


class ActRouting(unittest.TestCase):
    TARGET = {"text": None, "cx": 10, "cy": 20, "w": 0, "h": 0, "conf": 1.0, "score": None}

    def test_adb_monitor_goes_to_device_not_window(self):
        action, params = A.build_action({"action": "click", "monitor": "adb:127.0.0.1:16384"},
                                        self.TARGET)
        self.assertEqual(params["device"], "adb:127.0.0.1:16384")
        self.assertNotIn("window", params)

    def test_window_spec_unchanged(self):
        _, params = A.build_action({"action": "click", "monitor": "window:原神"}, self.TARGET)
        self.assertEqual(params.get("window"), "window:原神")
        self.assertNotIn("device", params)

    def test_drag_holds_pass_through(self):
        _, params = A.build_action({"action": "drag", "monitor": "adb:x",
                                    "hold_start_ms": 700, "hold_end_ms": 400}, self.TARGET)
        self.assertEqual((params["hold_start_ms"], params["hold_end_ms"]), (700, 400))

    def test_touch_actions(self):
        action, params = A.build_action({"action": "touch_down", "monitor": "adb:x",
                                         "hold_ms": 600}, self.TARGET)
        self.assertEqual(action, "touch_down")
        self.assertEqual((params["x"], params["y"], params["hold_ms"]), (10, 20, 600))


if __name__ == "__main__":
    unittest.main()
