"""proposals.structured_boxes：成排成列的格子不靠模型也能找出来。"""

from __future__ import annotations

import unittest

import cv2
import numpy as np

from ui_locate.proposals import drop_inside_text, structured_boxes


def inventory(cols=8, rows=3, size=40, gap=6, hide=()):
    """画一张「物品栏」：浅底、深色边框的格子，部分格子被浮窗挡住。"""
    img = np.full((400, 600, 3), (60, 110, 60), np.uint8)
    rng = np.random.default_rng(0)
    img += rng.integers(0, 30, img.shape, dtype=np.uint8)  # 背景纹理
    x0, y0 = 50, 80
    for r in range(rows):
        for c in range(cols):
            x, y = x0 + c * (size + gap), y0 + r * (size + gap)
            cv2.rectangle(img, (x, y), (x + size, y + size), (200, 220, 240), -1)
            cv2.rectangle(img, (x, y), (x + size, y + size), (40, 60, 120), 2)
    for (c, r) in hide:
        x, y = x0 + c * (size + gap), y0 + r * (size + gap)
        cv2.rectangle(img, (x - 3, y - 3), (x + size + 3, y + size + 3), (60, 110, 60), -1)
    return img, (x0, y0, size, gap)


def near(boxes, x, y, tol=8):
    return any(abs(b["cx"] - x) <= tol and abs(b["cy"] - y) <= tol for b in boxes)


class StructuredTests(unittest.TestCase):
    def test_finds_every_cell(self):
        img, (x0, y0, s, g) = inventory()
        boxes = structured_boxes(img)
        for r in range(3):
            for c in range(8):
                self.assertTrue(near(boxes, x0 + c * (s + g) + s / 2, y0 + r * (s + g) + s / 2), (c, r))
        self.assertTrue(all(b["source"] == "grid" for b in boxes))

    def test_completes_hidden_cells(self):
        img, (x0, y0, s, g) = inventory(hide=[(4, 1), (5, 1)])
        boxes = structured_boxes(img)
        self.assertTrue(near(boxes, x0 + 4 * (s + g) + s / 2, y0 + 1 * (s + g) + s / 2))

    def test_lone_rectangles_ignored(self):
        img = np.full((300, 300, 3), 90, np.uint8)
        cv2.rectangle(img, (40, 40), (90, 90), (250, 250, 250), 2)
        cv2.rectangle(img, (180, 150), (260, 200), (250, 250, 250), 2)
        self.assertEqual(structured_boxes(img), [])

    def test_text_letters_dropped_but_panel_cells_kept(self):
        letters = [{"cx": 105, "cy": 20, "w": 10, "h": 14}]
        cells = [{"cx": 105, "cy": 200, "w": 40, "h": 40}]
        line = {"text": "Total", "x1": 100, "y1": 10, "x2": 200, "y2": 30, "w": 100, "h": 20}
        panel = {"text": "Used to chop wood", "x1": 0, "y1": 0, "x2": 800, "y2": 400, "w": 800, "h": 400}
        self.assertEqual(drop_inside_text(letters, [line]), [])
        self.assertEqual(drop_inside_text(cells, [panel]), cells)


if __name__ == "__main__":
    unittest.main()
