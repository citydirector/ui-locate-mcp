"""Windows 实测（A–L4 报告，2026-10-31）发现的问题，各一条回归测试。"""

from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
if not hasattr(ctypes, "WINFUNCTYPE"):
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE  # type: ignore[attr-defined]

from ui_locate import act as A  # noqa: E402
from ui_locate.capture import CaptureError, Capturer, Region  # noqa: E402
from ui_locate.marks import build_marks, merge_text_fragments  # noqa: E402
from ui_locate.mosaic import MIN_SHEET_H, build_mosaic  # noqa: E402

REGION = Region("\\\\.\\DISPLAY1", 0, 0, 64, 48)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class EmptyMosaicTests(unittest.TestCase):
    """C4：没有元素时拼图曾是 1x1200，OCR 跑了 108 秒。"""

    def test_no_items_gives_no_slots_and_sane_canvas(self):
        sheet, slots, _ = build_mosaic(np.zeros((270, 240, 3), np.uint8), [], 1.2)
        self.assertEqual(slots, [])
        self.assertGreaterEqual(sheet.shape[0], MIN_SHEET_H)

    def test_single_small_item_is_not_extreme_aspect(self):
        img = np.zeros((100, 100, 3), np.uint8)
        sheet, slots, _ = build_mosaic(img, [{"x1": 10, "y1": 10, "x2": 14, "y2": 13}], 1.2)
        self.assertEqual(len(slots), 1)
        self.assertLessEqual(sheet.shape[1] / sheet.shape[0], 1200 / MIN_SHEET_H)


class StickyRetryTests(unittest.TestCase):
    """B8：一次失败后永远粘在降级后端上。"""

    def setUp(self):
        self.state = {"dxgi_ok": False}
        self.calls = []

        def dxgi(_r):
            self.calls.append("dxgi")
            if not self.state["dxgi_ok"]:
                raise CaptureError("lost")
            return np.full((48, 64, 3), 99, np.uint8)

        def wgc(_r):
            self.calls.append("wgc")
            return np.full((48, 64, 3), 50, np.uint8)

        self.clock = Clock()
        self.cap = Capturer({"dxgi": dxgi, "wgc": wgc, "gdi": wgc}, clock=self.clock)

    def test_recovers_to_head_after_retry_interval(self):
        self.assertEqual(self.cap.grab(REGION, "auto").backend, "wgc")
        self.state["dxgi_ok"] = True
        self.clock.t = 5
        self.calls.clear()
        self.assertEqual(self.cap.grab(REGION, "auto").backend, "wgc")   # 还没到期
        self.assertEqual(self.calls, ["wgc"])
        self.clock.t = 31
        self.assertEqual(self.cap.grab(REGION, "auto").backend, "dxgi")  # 到期重试链首
        self.clock.t = 32
        self.calls.clear()
        self.cap.grab(REGION, "auto")
        self.assertEqual(self.calls, ["dxgi"])

    def test_still_broken_head_restarts_timer(self):
        self.cap.grab(REGION, "auto")
        self.clock.t = 31
        self.calls.clear()
        self.assertEqual(self.cap.grab(REGION, "auto").backend, "wgc")
        self.assertEqual(self.calls, ["dxgi", "wgc"])
        self.clock.t = 40
        self.calls.clear()
        self.cap.grab(REGION, "auto")
        self.assertEqual(self.calls, ["wgc"])


class CanvasCheckTests(unittest.TestCase):
    """A3：画布组装时尺寸不符曾被静默裁剪。"""

    def setUp(self):
        try:
            from ui_locate import screen
        except Exception as error:  # noqa: BLE001
            self.skipTest(f"screen 导入失败：{error}")
        self.screen = screen
        self.mon = SimpleNamespace(device="\\\\.\\DISPLAY1", x=-1234, y=-1080, w=1920, h=1080)

    def test_ok(self):
        self.screen.check_tile((1080, 1920, 3), self.mon, (-1234, -1080, 4114, 2880))

    def test_size_mismatch_raises(self):
        with self.assertRaises(CaptureError):
            self.screen.check_tile((720, 1280, 3), self.mon, (-1234, -1080, 4114, 2880))

    def test_outside_virtual_raises(self):
        with self.assertRaises(CaptureError):
            self.screen.check_tile((1080, 1920, 3), self.mon, (-1234, -1080, 1440, 2057))


class TextFragmentTests(unittest.TestCase):
    """C3：「文件夹」被切成单字各自编号。"""

    def c(self, x1, x2, text, y1=0, y2=20, kind="text"):
        return {"box": (x1, y1, x2, y2), "text": text, "conf": 0.5, "kind": kind}

    def test_adjacent_chars_merge(self):
        out = merge_text_fragments([self.c(0, 18, "文"), self.c(20, 38, "件"), self.c(40, 58, "夹")])
        self.assertEqual([o["text"] for o in out], ["文件夹"])
        self.assertEqual(out[0]["box"], (0, 0, 58, 20))

    def test_separate_words_stay_apart(self):
        out = merge_text_fragments([self.c(0, 36, "文件"), self.c(56, 92, "编辑")])
        self.assertEqual(sorted(o["text"] for o in out), ["文件", "编辑"])

    def test_other_lines_and_icons_untouched(self):
        out = merge_text_fragments([self.c(0, 18, "文"), self.c(20, 38, "件", 30, 50),
                                    self.c(20, 38, "", kind="icon")])
        self.assertEqual(len(out), 3)

    def test_single_ascii_toolbar_glyphs_not_merged(self):
        out = merge_text_fragments([self.c(0, 18, "B"), self.c(22, 40, "I"), self.c(44, 62, "U")])
        self.assertEqual(len(out), 3)

    def test_latin_gets_space(self):
        out = merge_text_fragments([self.c(0, 30, "12"), self.c(34, 60, "MB")])
        self.assertEqual(out[0]["text"], "12 MB")

    def test_build_marks_merges_before_truncating(self):
        els = [{"x1": i * 20, "y1": 0, "x2": i * 20 + 18, "y2": 20, "conf": 0.5, "text": ch}
               for i, ch in enumerate("文件夹大小")]
        marks = build_marks(els, max_marks=2)
        self.assertEqual(marks[0].text, "文件夹大小")


class Regress2Tests(unittest.TestCase):
    """第二轮回归（2026-10-31）发现的问题。"""

    def test_truncation_is_reported(self):
        els = [{"x1": i * 40, "y1": 0, "x2": i * 40 + 20, "y2": 20, "conf": 0.5} for i in range(5)]
        st = {}
        build_marks(els, max_marks=3, stats=st)
        self.assertEqual(st, {"candidates": 5, "truncated": True})
        st = {}
        build_marks(els, max_marks=5, stats=st)
        self.assertFalse(st["truncated"])

    def test_container_cell_dropped(self):
        cells = [{"x1": x, "y1": y, "x2": x + 100, "y2": y + 100, "conf": 0.5, "source": "grid"}
                 for x in (0, 110, 220) for y in (0, 110)]
        big = {"x1": 50, "y1": 50, "x2": 280, "y2": 200, "conf": 0.5, "source": "grid"}
        marks = build_marks(cells + [big])
        self.assertEqual(len(marks), 6)

    def test_capture_choose_overrides_and_restores(self):
        from ui_locate import capture

        before = capture.preferred()
        with capture.choose("gdi"):
            self.assertEqual(capture.preferred(), "gdi")
        with capture.choose("nope"):
            self.assertEqual(capture.preferred(), before)
        self.assertEqual(capture.preferred(), before)

    def test_uipi_verdict(self):
        from ui_locate import window

        v = window.uipi_verdict(0x2000, 0x3000, False)
        self.assertTrue(v["blocked"])
        self.assertEqual((v["self"], v["target"]), ("medium", "high"))
        self.assertIn("管理员", v["hint"])
        self.assertFalse(window.uipi_verdict(0x3000, 0x3000, False)["blocked"])
        self.assertFalse(window.uipi_verdict(0x2000, 0x3000, True)["blocked"])
        self.assertIsNone(window.uipi_verdict(None, 0x3000, False)["blocked"])


class ApiPitfallTests(unittest.TestCase):
    """报告第四节的调用坑。"""

    def run_act(self, spec, probe=None):
        clock = Clock()
        sent = {}

        def perform(a, p):
            sent["a"], sent["p"] = a, p
            return {"sent": 1}

        out = A.act(spec, probe=probe or (lambda *a: None), capture=lambda _m: "IMG",
                    perform=perform, compare=lambda *a: 9.0,
                    sleep=lambda s: setattr(clock, "t", clock.t + s), clock=clock)
        return out, sent

    def test_hotkey_keys_as_list(self):
        _, params = A.build_action({"action": "hotkey", "keys": ["ctrl", "a"]}, {"cx": None, "cy": None})
        self.assertEqual(params["keys"], "ctrl+a")
        _, params = A.build_action({"action": "hold", "keys": ["shift", "w"]}, {"cx": None, "cy": None})
        self.assertEqual(params["keys"], "shift+w")

    def test_drag_with_only_from_xy(self):
        out, sent = self.run_act({"action": "drag", "from_x": 10, "from_y": 20, "to_x": 50, "to_y": 20})
        self.assertTrue(out["ok"], out)
        self.assertEqual((sent["p"]["x1"], sent["p"]["y1"], sent["p"]["x2"]), (10, 20, 50))

    def test_drag_to_coordinate_string(self):
        def probe(*_a):
            raise AssertionError("坐标字符串不该去屏幕上找字")

        out, sent = self.run_act({"action": "drag", "x": 0, "y": 0, "to": "40,-540"}, probe=probe)
        self.assertTrue(out["ok"], out)
        self.assertEqual((sent["p"]["x2"], sent["p"]["y2"]), (40, -540))

    def test_drag_int_steps_is_not_multistep(self):
        try:
            from ui_locate import server
        except Exception as error:  # noqa: BLE001
            self.skipTest(f"server 导入失败：{error}")
        handler = server.Handler.__new__(server.Handler) if hasattr(server, "Handler") else None
        if handler is None:
            self.skipTest("找不到请求处理类")
        captured = {}
        orig = server.act_mod.act

        def fake_act(data, **_kw):
            captured["data"] = data
            return {"ok": True}

        server.act_mod.act = fake_act
        try:
            server.ENGINE = server.ENGINE or SimpleNamespace(
                frames=SimpleNamespace(begin_turn=lambda: 1, turns_of=lambda t: [1], put=lambda *a, **k: None))
            handler._act({"action": "drag", "x": 0, "y": 0, "to": "5,5", "steps": 12})
        finally:
            server.act_mod.act = orig
        self.assertEqual(captured["data"]["steps"], 12)


if __name__ == "__main__":
    unittest.main()
