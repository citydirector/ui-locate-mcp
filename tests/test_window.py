"""窗口写法解析与挑选 —— 纯逻辑部分，任何平台都能跑。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import window as W  # noqa: E402


def win(h, title, w=800, hgt=600, fg=False, mini=False):
    return W.WinInfo(h, title, "C", 1, (0, 0, w, hgt), foreground=fg, minimized=mini)


class SpecTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(W.parse_spec("window:原神"), {"title": "原神"})
        self.assertEqual(W.parse_spec("hwnd:1234"), {"hwnd": 1234})
        self.assertEqual(W.parse_spec("HWND:0x10"), {"hwnd": 16})
        self.assertIsNone(W.parse_spec("primary"))
        self.assertIsNone(W.parse_spec(1))
        with self.assertRaises(W.WindowError):
            W.parse_spec("window:")
        with self.assertRaises(W.WindowError):
            W.parse_spec("hwnd:abc")

    def test_is_window_spec(self):
        self.assertTrue(W.is_window_spec("window:x"))
        self.assertTrue(W.is_window_spec("hwnd:bad"))   # 写错也算窗口写法，交给 resolve 报错
        self.assertFalse(W.is_window_spec("0"))
        self.assertFalse(W.is_window_spec(None))

    def test_as_spec(self):
        self.assertEqual(W.as_spec("Stardew Valley"), "window:Stardew Valley")
        self.assertEqual(W.as_spec("1234"), "hwnd:1234")
        self.assertEqual(W.as_spec(1234), "hwnd:1234")
        self.assertEqual(W.as_spec("window:a"), "window:a")
        self.assertEqual(W.as_spec(""), "")


class PickTests(unittest.TestCase):
    def test_exact_beats_contains(self):
        ws = [win(1, "Blender - untitled"), win(2, "blender")]
        self.assertEqual(W.pick(ws, {"title": "Blender"}).hwnd, 2)

    def test_prefix_beats_contains(self):
        ws = [win(1, "Notes about Stardew"), win(2, "Stardew Valley")]
        self.assertEqual(W.pick(ws, {"title": "stardew"}).hwnd, 2)

    def test_same_tier_prefers_foreground_then_not_minimized_then_larger(self):
        ws = [win(1, "Game", 100, 100), win(2, "Game", 1920, 1080, mini=True),
              win(3, "Game", 800, 600)]
        self.assertEqual(W.pick(ws, {"title": "game"}).hwnd, 3)
        ws.append(win(4, "Game", 10, 10, fg=True))
        self.assertEqual(W.pick(ws, {"title": "game"}).hwnd, 4)

    def test_no_match_and_hwnd(self):
        ws = [win(1, "A"), win(2, "B")]
        self.assertIsNone(W.pick(ws, {"title": "zzz"}))
        self.assertEqual(W.pick(ws, {"hwnd": 2}).title, "B")

    def test_capture_preference(self):
        import os
        os.environ["UI_LOCATE_WINDOW_CAPTURE"] = "screen"
        try:
            self.assertEqual(W.preferred_capture(), "screen")
            os.environ["UI_LOCATE_WINDOW_CAPTURE"] = "bogus"
            self.assertEqual(W.preferred_capture(), "auto")
        finally:
            del os.environ["UI_LOCATE_WINDOW_CAPTURE"]


if __name__ == "__main__":
    unittest.main()
