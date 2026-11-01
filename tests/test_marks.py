"""编号标注（`ui_locate.marks`）的测试：去重、排序、渲染、编号解析。纯计算，不抓屏。"""

from __future__ import annotations

import unittest

import cv2
import numpy as np

from ui_locate.marks import MarkStore, build_marks, grid_cells, is_cell_ref, is_mark_ref, render


def el(x1, y1, x2, y2, conf=0.5, text="", text_only=False):
    d = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "conf": conf, "text": text,
         "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2, "w": x2 - x1, "h": y2 - y1}
    if text_only:
        d["text_only"] = True
    return d


class BuildTests(unittest.TestCase):
    def test_reading_order(self):
        # 两行，每行乱序给入；编号应当先上后下、行内从左到右
        items = [el(300, 10, 330, 30), el(10, 12, 40, 32), el(150, 100, 180, 120), el(20, 98, 50, 118)]
        marks = build_marks(items)
        self.assertEqual([(int(m.x1), int(m.y1)) for m in marks], [(10, 12), (300, 10), (20, 98), (150, 100)])
        self.assertEqual([m.id for m in marks], [1, 2, 3, 4])

    def test_duplicates_collapse_and_text_wins(self):
        items = [el(10, 10, 50, 30, conf=0.9), el(11, 10, 50, 31, conf=0.3, text="保存")]
        marks = build_marks(items)
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0].text, "保存")

    def test_nested_small_inside_big_kept(self):
        # 工具栏（大）里的按钮（小）不是重复
        items = [el(0, 0, 400, 40, conf=0.6), el(10, 8, 30, 28, conf=0.6)]
        self.assertEqual(len(build_marks(items, frame_area=1920 * 1080)), 2)

    def test_huge_panels_dropped(self):
        items = [el(0, 0, 1000, 800), el(10, 10, 30, 30)]
        marks = build_marks(items, frame_area=1000 * 900)
        self.assertEqual(len(marks), 1)

    def test_degenerate_dropped(self):
        self.assertEqual(build_marks([el(5, 5, 6, 40)]), [])

    def test_cap_keeps_text_first(self):
        items = [el(i * 20, 0, i * 20 + 10, 10, conf=0.9) for i in range(10)]
        items.append(el(500, 0, 520, 10, conf=0.1, text="确定"))
        marks = build_marks(items, max_marks=3)
        self.assertEqual(len(marks), 3)
        self.assertIn("确定", [m.text for m in marks])

    def test_kind(self):
        marks = build_marks([el(0, 0, 20, 20), el(40, 0, 80, 20, text="OK"),
                             el(100, 0, 160, 20, text="hi", text_only=True)])
        self.assertEqual([m.kind for m in marks], ["icon", "text", "ocr"])

    def test_negative_origin_coordinates_pass_through(self):
        marks = build_marks([el(-1900, 10, -1870, 40)])
        self.assertEqual(marks[0].brief()["cx"], -1885)


class RenderTests(unittest.TestCase):
    def test_render_downscales_and_draws(self):
        img = np.full((1080, 1920, 3), 255, np.uint8)
        marks = build_marks([el(100, 100, 140, 130), el(1800, 1000, 1830, 1030)])
        data, scale = render(img, marks, (0, 0), max_side=960)
        self.assertAlmostEqual(scale, 0.5)
        out = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(out.shape[:2], (540, 960))
        self.assertLess(int(out.min()), 200)  # 画上了东西

    def test_render_upscales_region_only_when_allowed(self):
        img = np.full((100, 200, 3), 255, np.uint8)
        marks = build_marks([el(1010, 510, 1030, 530)])
        _, s1 = render(img, marks, (1000, 500), max_side=800)
        _, s2 = render(img, marks, (1000, 500), max_side=800, upscale_max=4.0)
        self.assertEqual(s1, 1.0)
        self.assertEqual(s2, 4.0)

    def test_png(self):
        img = np.zeros((50, 50, 3), np.uint8)
        data, _ = render(img, [], (0, 0), fmt="png")
        self.assertTrue(data.startswith(b"\x89PNG"))


class StoreTests(unittest.TestCase):
    def test_resolve_forms(self):
        store = MarkStore()
        s = store.put(build_marks([el(0, 0, 20, 20), el(50, 0, 70, 20)]), (0, 0))
        for ref in (2, "2", "#2"):
            m, got = store.resolve(ref)
            self.assertEqual((m.id, got.id), (2, s.id))

    def test_stale_set_rejected(self):
        store = MarkStore()
        old = store.put(build_marks([el(0, 0, 20, 20)]), (0, 0))
        store.put(build_marks([el(0, 0, 20, 20)]), (0, 0))
        with self.assertRaises(KeyError) as ctx:
            store.resolve(1, old.id)
        self.assertIn("过期", str(ctx.exception))

    def test_missing(self):
        store = MarkStore()
        with self.assertRaises(KeyError):
            store.resolve(1)
        store.put(build_marks([el(0, 0, 20, 20)]), (0, 0))
        with self.assertRaises(KeyError):
            store.resolve(9)

    def test_mark_ref(self):
        self.assertTrue(is_mark_ref("#12"))
        self.assertFalse(is_mark_ref("12"))
        self.assertFalse(is_mark_ref("#a"))


class GridTests(unittest.TestCase):
    def test_landscape_grid_names_and_coords(self):
        cells = grid_cells((-1920, 0), (1920, 1080), 8)
        self.assertEqual(len(cells), 8 * 4)  # 1080/1920*8 = 4.5 → 4 行
        self.assertEqual(cells["A1"][:2], (-1920, 0))
        x1, y1, x2, y2 = cells["H4"]
        self.assertAlmostEqual(x2, 0)
        self.assertAlmostEqual(y2, 1080)

    def test_zero_means_no_grid(self):
        self.assertEqual(grid_cells((0, 0), (100, 100), 0), {})

    def test_cell_ref(self):
        self.assertTrue(is_cell_ref("b3"))
        self.assertFalse(is_cell_ref("#3"))
        self.assertFalse(is_cell_ref("保存"))
        self.assertFalse(is_cell_ref("B"))

    def test_store_resolves_cells(self):
        store = MarkStore()
        cells = grid_cells((0, 0), (800, 400), 4)
        store.put([], (0, 0), cells=cells)
        box, _ = store.resolve_cell("b2")
        self.assertEqual(box, (200, 200, 400, 400))
        with self.assertRaises(KeyError):
            store.resolve_cell("Z9")

    def test_render_with_grid(self):
        img = np.zeros((400, 800, 3), np.uint8)
        data, _ = render(img, [], (0, 0), cells=grid_cells((0, 0), (800, 400), 4))
        out = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        self.assertGreater(int(out.max()), 100)  # 网格线和标签画上了


if __name__ == "__main__":
    unittest.main()
