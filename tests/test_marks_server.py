"""server.apply_marks：编号引用 → act 能吃的坐标引用。不开服务。"""

from __future__ import annotations

import ctypes
import unittest

# screen.py 在导入时定义 Win32 回调类型；非 Windows 上借 CFUNCTYPE 顶一下，只为能导入。
if not hasattr(ctypes, "WINFUNCTYPE"):
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE  # type: ignore[attr-defined]

try:
    from ui_locate import server
except Exception as error:  # onnxruntime / rapidocr 不在时跳过
    server = None
    _why = str(error)

from ui_locate.marks import build_marks


def el(x1, y1, x2, y2, text=""):
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "conf": 0.5, "text": text}


@unittest.skipIf(server is None, "server 依赖不全")
class ApplyMarksTests(unittest.TestCase):
    def setUp(self):
        self.store = server.marks_mod.MarkStore()
        server.MARKS = self.store
        self.set = self.store.put(build_marks([el(0, 0, 20, 20, "背包"), el(100, 0, 140, 40)]), (0, 0))

    def test_mark_becomes_at(self):
        out = server.apply_marks({"mark": 2, "action": "click", "x": 5, "y": 5})
        self.assertNotIn("x", out)
        self.assertEqual((out["at"]["cx"], out["at"]["cy"]), (120, 20))
        self.assertEqual(out["_mark"]["id"], 2)

    def test_drag_to_mark(self):
        out = server.apply_marks({"mark": 1, "action": "drag", "to": "#2"})
        self.assertEqual(out["to"]["cx"], 120)

    def test_bare_number_to_is_text(self):
        out = server.apply_marks({"text": "x", "to": "2"})
        self.assertEqual(out["to"], "2")

    def test_cell_becomes_at(self):
        from ui_locate.marks import grid_cells

        self.store.put([], (0, 0), cells=grid_cells((0, 0), (800, 400), 4))
        out = server.apply_marks({"cell": "b2", "action": "click"})
        self.assertEqual((out["at"]["cx"], out["at"]["cy"]), (300, 300))
        self.assertEqual(out["_mark"]["cell"], "B2")
        self.assertNotIn("cell", out)

    def test_bad_mark_is_act_error(self):
        with self.assertRaises(server.act_mod.ActError):
            server.apply_marks({"mark": 9})

    def test_stale_set_is_act_error(self):
        self.store.put(build_marks([el(0, 0, 20, 20)]), (0, 0))
        with self.assertRaises(server.act_mod.ActError):
            server.apply_marks({"mark": 1, "mark_set": self.set.id})


if __name__ == "__main__":
    unittest.main()
