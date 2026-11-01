"""regress3（267ab62）的修复：watch 与动作同步、sticky 只认 auto、incremental 恒为对象、
/watch timings、regions 末帧全景、空白框过滤。不碰屏幕。"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import marks as M  # noqa: E402
from ui_locate import watch as W  # noqa: E402
from ui_locate.capture import Capturer, Region  # noqa: E402

try:
    import ctypes

    if not hasattr(ctypes, "WINFUNCTYPE"):
        ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE  # type: ignore[attr-defined]
    from ui_locate import server
except Exception:  # noqa: BLE001
    server = None


class Screen:
    """「屏幕」：动作把一个方块从左挪到右，其余时间静止。"""

    def __init__(self):
        self.x = 20
        self.lock = threading.Lock()

    def grab(self):
        f = np.full((360, 640, 3), 40, np.uint8)
        with self.lock:
            x = self.x
        f[150:200, x:x + 50] = 255
        return f, (0, 0)

    def drag(self):
        for x in range(20, 560, 60):     # 约 0.45 秒的拖动，然后 settle 0.3 秒
            with self.lock:
                self.x = x
            time.sleep(0.05)
        time.sleep(0.3)


class RecorderTests(unittest.TestCase):
    def test_motion_during_action_is_captured(self):
        scr = Screen()
        rec = W.Recorder(scr.grab, min_ms=300, fps=15).start()
        self.assertTrue(rec.frames, "start() 返回前必须已有动作前的第一帧")
        scr.drag()
        frames = rec.finish()
        info, sheet = W.summarize(frames, record_ms=rec.record_ms)
        self.assertFalse(info["static"])
        self.assertGreater(len(info["keyframes"]), 1)
        # 录制覆盖了整个动作（> min_ms），不是只录 min_ms
        self.assertGreater(rec.record_ms, 600)
        self.assertEqual(frames[0][1][150, 25].tolist(), [255, 255, 255])

    def test_min_ms_respected_when_action_is_instant(self):
        scr = Screen()
        rec = W.Recorder(scr.grab, min_ms=400, fps=10).start()
        rec.finish()
        self.assertGreaterEqual(rec.record_ms, 400)

    def test_grab_error_surfaces(self):
        def bad():
            raise RuntimeError("boom")
        rec = W.Recorder(bad, min_ms=100).start()
        with self.assertRaises(RuntimeError):
            rec.finish()


class RegionsEndFrameTests(unittest.TestCase):
    def test_regions_mode_appends_full_last_frame(self):
        scr = Screen()
        frames = []
        for i, x in enumerate([20, 20, 140, 260, 380]):
            scr.x = x
            f, o = scr.grab()
            frames.append((i * 200.0, f, o))
        info = W.analyze(frames)
        tiles = W.tiles_for(frames, info, mode="regions")
        tiles_full = W.tiles_for(frames, info, mode="full")
        self.assertGreater(len(info["keyframes"]), 1)
        self.assertEqual(len(tiles_full), len(info["keyframes"]))
        # 最后一块是整帧（比例同原图）
        h, w = tiles[-1].shape[:2]
        self.assertAlmostEqual((h - W.LABEL_H) / w, 360 / 640, delta=0.02)


class StickyTests(unittest.TestCase):
    def test_explicit_backend_does_not_become_sticky(self):
        calls = []

        def mk(name):
            def g(_region):
                calls.append(name)
                return np.full((20, 20, 3), 100, np.uint8)
            return g
        cap = Capturer({"dxgi": mk("dxgi"), "wgc": mk("wgc"), "gdi": mk("gdi")})
        region = Region("\\\\.\\DISPLAY1", 0, 0, 20, 20)
        cap.grab(region, "gdi")
        calls.clear()
        shot = cap.grab(region, "auto")
        self.assertEqual(shot.backend, "dxgi")
        self.assertEqual(calls, ["dxgi"])


class BlankTests(unittest.TestCase):
    def test_blank_boxes_dropped_bordered_cells_kept(self):
        img = np.full((300, 800, 3), 60, np.uint8)
        img[:, :, 0] = np.linspace(40, 90, 800, dtype=np.uint8)   # 渐变背景
        img[200:270, 100:165] = 60
        import cv2
        cv2.rectangle(img, (300, 200), (365, 270), (200, 200, 200), 2)   # 空物品格（有边框）
        els = [
            {"x1": 100, "y1": 20, "x2": 690, "y2": 90, "text": ""},     # 590×70 空白
            {"x1": 100, "y1": 200, "x2": 165, "y2": 270, "text": ""},   # 65×70 空白
            {"x1": 300, "y1": 200, "x2": 365, "y2": 270, "text": ""},   # 有边框的空格
            {"x1": 400, "y1": 20, "x2": 500, "y2": 60, "text": "确定"},  # 有文字
        ]
        st: dict = {}
        out = M.drop_blank(els, img, (0, 0), stats=st)
        self.assertEqual(st["blank_dropped"], 2)
        self.assertEqual([e["x1"] for e in out], [300, 400])


@unittest.skipIf(server is None, "server 依赖不全")
class ServerShapeTests(unittest.TestCase):
    def test_watch_screen_has_timings(self):
        scr = Screen()
        orig = server.screen.grab
        server.screen.grab = lambda _m: scr.grab()
        try:
            res = server.watch_screen({"duration_ms": 200, "fps": 10})
        finally:
            server.screen.grab = orig
        for k in ("record_ms", "analyze_ms", "render_ms", "encode_ms", "total_ms"):
            self.assertIn(k, res["timings"])

    def test_act_with_watch_records_during_action(self):
        scr = Screen()
        orig = server.screen.grab
        server.screen.grab = lambda _m: scr.grab()
        try:
            res = server.act_with_watch({"watch_ms": 300, "watch_fps": 15},
                                        lambda: (scr.drag(), {"ok": True})[1])
        finally:
            server.screen.grab = orig
        w = res["watch"]
        self.assertTrue(w["ok"])
        self.assertFalse(w["static"])
        self.assertEqual(w["started"], "before_action")
        self.assertIn("total_ms", w["timings"])

    def test_incremental_always_object(self):
        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn('{"mode": "full", "forced": True}', src)


if __name__ == "__main__":
    unittest.main()
