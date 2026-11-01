"""抓屏后端选择（`ui_locate.capture.Capturer`）的测试。

不碰真实屏幕：每个后端都换成假的抓取函数，只测选择、回落、黑帧与记忆
这几条规则。真机上 DXGI / WGC / GDI 各自能不能抓到，另外在 Windows 上验。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

import numpy as np

from ui_locate.capture import Capturer, CaptureError, Region, is_black, preferred

REGION = Region("\\\\.\\DISPLAY1", -1920, 0, 32, 16)


def img(value: int) -> np.ndarray:
    return np.full((16, 32, 3), value, dtype=np.uint8)


def fake(behaviors: dict) -> tuple[dict, list]:
    """behaviors: 后端名 → 图 或 Exception。返回 (grabbers, 调用记录)。"""
    calls: list[str] = []

    def make(name):
        def grab(region):
            calls.append(name)
            value = behaviors[name]
            if isinstance(value, Exception):
                raise value
            return value

        return grab

    return {name: make(name) for name in ("dxgi", "wgc", "gdi")}, calls


class ChainTests(unittest.TestCase):
    def test_auto_prefers_dxgi(self):
        grabbers, calls = fake({"dxgi": img(100), "wgc": img(100), "gdi": img(100)})
        shot = Capturer(grabbers).grab(REGION, "auto")
        self.assertEqual(shot.backend, "dxgi")
        self.assertEqual(calls, ["dxgi"])

    def test_auto_falls_back_on_error(self):
        # 混合显卡上 DXGI 报 UNSUPPORTED 的典型情况
        grabbers, calls = fake({"dxgi": CaptureError("unsupported"), "wgc": CaptureError("no winrt"), "gdi": img(90)})
        shot = Capturer(grabbers).grab(REGION, "auto")
        self.assertEqual(shot.backend, "gdi")
        self.assertEqual(calls, ["dxgi", "wgc", "gdi"])

    def test_auto_skips_black_frame(self):
        # GDI 抓独占全屏游戏 = 全黑；若 GDI 被记成上次成功的后端，下一次要能换掉它
        grabbers, calls = fake({"dxgi": img(120), "wgc": img(120), "gdi": img(0)})
        cap = Capturer(grabbers)
        cap._sticky[REGION.device.lower()] = "gdi"
        shot = cap.grab(REGION, "auto")
        self.assertEqual(shot.backend, "dxgi")
        self.assertEqual(calls, ["gdi", "dxgi"])

    def test_all_black_returns_first_black_frame(self):
        # 屏幕真的是黑的（过场动画）不是错误，照样交图
        grabbers, _ = fake({"dxgi": img(0), "wgc": img(0), "gdi": img(0)})
        shot = Capturer(grabbers).grab(REGION, "auto")
        self.assertEqual(shot.backend, "dxgi")
        self.assertTrue(is_black(shot.bgr))

    def test_sticky_backend_goes_first(self):
        grabbers, calls = fake({"dxgi": CaptureError("lost"), "wgc": img(50), "gdi": img(50)})
        cap = Capturer(grabbers)
        cap.grab(REGION, "auto")
        calls.clear()
        cap.grab(REGION, "auto")
        self.assertEqual(calls, ["wgc"])

    def test_named_backend_never_falls_back(self):
        grabbers, calls = fake({"dxgi": CaptureError("unsupported"), "wgc": img(50), "gdi": img(50)})
        with self.assertRaises(CaptureError) as ctx:
            Capturer(grabbers).grab(REGION, "dxgi")
        self.assertIn("unsupported", str(ctx.exception))
        self.assertEqual(calls, ["dxgi"])

    def test_named_backend_returns_black_frame_as_is(self):
        grabbers, _ = fake({"dxgi": img(50), "wgc": img(50), "gdi": img(0)})
        shot = Capturer(grabbers).grab(REGION, "gdi")
        self.assertEqual(shot.backend, "gdi")

    def test_all_fail_reports_every_backend(self):
        grabbers, _ = fake({"dxgi": CaptureError("a"), "wgc": CaptureError("b"), "gdi": CaptureError("c")})
        with self.assertRaises(CaptureError) as ctx:
            Capturer(grabbers).grab(REGION, "auto")
        for word in ("dxgi", "wgc", "gdi"):
            self.assertIn(word, str(ctx.exception))

    def test_status_records_last_shot(self):
        grabbers, _ = fake({"dxgi": CaptureError("x"), "wgc": img(9), "gdi": img(9)})
        cap = Capturer(grabbers)
        cap.grab(REGION, "auto")
        self.assertEqual(cap.last["backend"], "wgc")
        self.assertEqual(cap.last["tried"], ["dxgi", "wgc"])


class PreferenceTests(unittest.TestCase):
    def test_env_wins(self):
        with mock.patch.dict(os.environ, {"UI_LOCATE_CAPTURE": "GDI"}):
            self.assertEqual(preferred(), "gdi")

    def test_bad_value_means_auto(self):
        with mock.patch.dict(os.environ, {"UI_LOCATE_CAPTURE": "bitblt2"}):
            self.assertEqual(preferred(), "auto")

    def test_runtime_json(self):
        with mock.patch.dict(os.environ, {"UI_LOCATE_CAPTURE": ""}), mock.patch(
            "ui_locate.config.runtime_settings", return_value={"capture": "wgc"}
        ):
            self.assertEqual(preferred(), "wgc")


class BlackTests(unittest.TestCase):
    def test_dark_theme_is_not_black(self):
        frame = img(0)
        frame[7, 14] = (200, 200, 200)  # 落在采样点上的一点亮字
        self.assertFalse(is_black(frame))

    def test_empty_is_black(self):
        self.assertTrue(is_black(np.zeros((0, 0, 3), dtype=np.uint8)))


if __name__ == "__main__":
    unittest.main()
