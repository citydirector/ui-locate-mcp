"""watch：录几秒 → 关键帧 → 只拼变化区域。不碰屏幕。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import watch as W  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def scripted(make):
    """make(i) → 第 i 帧；抓一次走 10ms。"""
    clock = Clock()
    state = {"i": 0}

    def grab():
        img = make(state["i"])
        state["i"] += 1
        clock.t += 0.01
        return img, (-100, -50)

    return grab, clock


def blank():
    return np.full((256, 320, 3), 40, np.uint8)


class WatchTests(unittest.TestCase):
    def test_static_scene_one_keyframe(self):
        grab, clock = scripted(lambda i: blank())
        info, sheet, frames = W.watch(grab, duration_ms=1000, fps=5, sleep=clock.sleep, clock=clock)
        self.assertTrue(info["static"])
        self.assertEqual(len(info["keyframes"]), 1)
        self.assertGreaterEqual(len(frames), 5)
        self.assertEqual(sheet.ndim, 3)

    def test_moving_box_keyframes_with_screen_coords(self):
        def make(i):
            f = blank()
            if i >= 3:
                f[64:128, 96:160] = 230      # 第 3 帧起出现一个面板
            return f

        grab, clock = scripted(make)
        info, _sheet, _ = W.watch(grab, duration_ms=1200, fps=5, sleep=clock.sleep, clock=clock)
        self.assertFalse(info["static"])
        k = info["keyframes"][1]
        self.assertEqual(k["index"], 3)
        r = k["regions"][0]
        self.assertEqual((r["x"], r["y"]), (96 - 100, 64 - 50))

    def test_ambient_cells_are_ignored(self):
        rng = np.random.default_rng(0)

        def make(i):
            f = blank()
            f[:, 256:] = rng.integers(0, 255, (256, 64, 3), dtype=np.uint8)   # 一直在闪的一条
            return f

        grab, clock = scripted(make)
        info, _s, _ = W.watch(grab, duration_ms=2000, fps=5, sleep=clock.sleep, clock=clock)
        self.assertTrue(info["static"], info["keyframes"])
        self.assertIn("ambient", info)

    def test_keyframe_cap_keeps_first_and_last(self):
        def make(i):
            f = blank()
            f[(i % 8) * 32:(i % 8) * 32 + 32, :] = 255
            return f

        grab, clock = scripted(make)
        info, _s, frames = W.watch(grab, duration_ms=3000, fps=5, max_keyframes=4,
                                   sleep=clock.sleep, clock=clock)
        self.assertEqual(len(info["keyframes"]), 4)
        self.assertEqual(info["keyframes"][0]["index"], 0)
        self.assertGreater(info["keyframes_total"], 4)

    def test_regions_mode_crops_smaller_than_full(self):
        def make(i):
            f = np.random.default_rng(1).integers(0, 255, (720, 1280, 3), dtype=np.uint8)
            if i >= 2:
                f[100:140, 100:180] = 255
            return f

        grab, clock = scripted(make)
        _i, reg, frames = W.watch(grab, duration_ms=800, fps=5, mode="regions",
                                  sleep=clock.sleep, clock=clock)
        grab, clock = scripted(make)
        _i, full, _ = W.watch(grab, duration_ms=800, fps=5, mode="full",
                              sleep=clock.sleep, clock=clock)
        self.assertEqual(reg.shape[1], W.SHEET_W)
        self.assertGreater(full.size, 0)


if __name__ == "__main__":
    unittest.main()


try:
    import ctypes

    if not hasattr(ctypes, "WINFUNCTYPE"):
        ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE  # type: ignore[attr-defined]
    from ui_locate import server
except Exception:  # noqa: BLE001
    server = None


def el(x1, y1, x2, y2, text=""):
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2,
            "w": x2 - x1, "h": y2 - y1, "conf": 0.5, "text": text}


@unittest.skipIf(server is None, "server 依赖不全")
class IncrementalMarksTests(unittest.TestCase):
    def test_plan_grows_over_straddling_element_and_keeps_others(self):
        cached = [el(0, 0, 40, 20, "a"), el(90, 90, 200, 120, "b"), el(400, 400, 440, 420, "c")]
        rects, keep = server.plan_incremental(
            [{"x": 128, "y": 96, "w": 32, "h": 32}], cached, (640, 480), pad=8)
        self.assertEqual(len(rects), 1)
        self.assertLessEqual(rects[0][0], 90)          # 吞下了跨界的元素 b
        self.assertEqual(sorted(e["text"] for e in keep), ["a", "c"])

    def test_reuse_partial_full(self):
        calls = []

        class FakeEngine:
            def run(self, img, conf, tiled, text, text_mode, origin):
                calls.append((img.shape[1], img.shape[0], origin))
                ox, oy = origin
                return {"elements": [el(ox + 2, oy + 2, ox + 12, oy + 12, "x")],
                        "detect_ms": 1.0, "ocr_ms": 2.0}

        old = server.ENGINE
        server.ENGINE = FakeEngine()
        server._MARKS_CACHE.clear()
        try:
            key = ("0", 0.2, True, "mosaic")
            img = np.full((480, 640, 3), 40, np.uint8)
            _r, info = server._detect_incremental(key, img, (0, 0), conf=0.2, want_text=True,
                                                  text_mode="mosaic")
            self.assertEqual(info["mode"], "full")
            _r, info = server._detect_incremental(key, img.copy(), (0, 0), conf=0.2,
                                                  want_text=True, text_mode="mosaic")
            self.assertEqual(info["mode"], "reuse")
            self.assertEqual(len(calls), 1)
            img2 = img.copy()
            img2[200:240, 300:360] = 220
            res, info = server._detect_incremental(key, img2, (0, 0), conf=0.2, want_text=True,
                                                   text_mode="mosaic")
            self.assertEqual(info["mode"], "partial")
            self.assertLess(calls[-1][0], 640)         # 只对一小块重跑
            self.assertLess(info["redone_frac"], 0.1)
            img3 = np.full((480, 640, 3), 200, np.uint8)
            _r, info = server._detect_incremental(key, img3, (0, 0), conf=0.2, want_text=True,
                                                  text_mode="mosaic")
            self.assertEqual(info["mode"], "full")
        finally:
            server.ENGINE = old
            server._MARKS_CACHE.clear()
