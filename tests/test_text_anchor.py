"""文字与检测框的对应：按行匹配、框中心不在字上时改用字的位置。

用例的几何取自真机截图（1600x900 模拟器，卫戍协议主界面和商店）：
「联合模拟」被挂到一个大框上，框中心偏出 340px；「个人模拟」「联合模拟」被框成
一个元素；正常按钮的框中心落在字上，必须保持原样。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate.ocr_match import OcrLine, attach_text, find  # noqa: E402


def box(x1, y1, x2, y2):
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2,
            "w": x2 - x1, "h": y2 - y1, "conf": 0.8}


class Anchor(unittest.TestCase):
    def test_big_box_uses_text_center(self):
        # 大框 (700..1552, 566..704) 中心 (1126,635)，字在 (1460,704)
        items = [box(700, 566, 1552, 704 + 10)]
        attach_text(items, [OcrLine("联合模拟", 0.9, 1410, 690, 1510, 718)])
        hit = find(items, "联合模拟", min_tier="starts")[0]
        self.assertEqual((hit["cx"], hit["cy"]), (1460, 704))
        self.assertEqual(hit["anchor"], "text")
        self.assertEqual(hit["box_center"], [items[0]["cx"], items[0]["cy"]])

    def test_button_box_center_on_text_unchanged(self):
        items = [box(1380, 210, 1456, 254)]
        attach_text(items, [OcrLine("物资调配处", 0.9, 1370, 220, 1466, 245)])
        hit = find(items, "物资调配处", min_tier="starts")[0]
        self.assertEqual((hit["cx"], hit["cy"]), (1418, 232))
        self.assertNotIn("anchor", hit)

    def test_merged_box_matches_single_line_exact(self):
        # 「个人模拟」「联合模拟」被框成一个元素：按行匹配应升到 exact 并给那一行的位置
        items = [box(1130, 680, 1530, 730)]
        attach_text(items, [OcrLine("个人模拟", 0.9, 1200, 692, 1290, 716),
                            OcrLine("联合模拟", 0.9, 1410, 692, 1510, 716)])
        self.assertEqual(items[0]["text"], "个人模拟 联合模拟")
        hit = find(items, "联合模拟", min_tier="starts")[0]
        self.assertEqual(hit["match_tier"], "exact")
        self.assertEqual(hit["text"], "联合模拟")
        self.assertEqual((hit["cx"], hit["cy"]), (1460, 704))

    def test_internal_lines_not_leaked(self):
        items = [box(0, 0, 100, 40)]
        attach_text(items, [OcrLine("确认", 0.9, 30, 10, 70, 30)])
        hit = find(items, "确认")[0]
        self.assertNotIn("_lines", hit)
        self.assertIn("_lines", items[0])   # 原件保留，供下一次 find 用

    def test_orphan_text_still_found(self):
        items: list[dict] = []
        attach_text(items, [OcrLine("规程", 0.9, 870, 330, 914, 358)])
        hit = find(items, "规程", min_tier="starts")[0]
        self.assertEqual((hit["cx"], hit["cy"]), (892, 344))

    def test_contains_still_rejected_for_act(self):
        items = [box(0, 0, 400, 40)]
        attach_text(items, [OcrLine("视觉定位MCP插件开发", 0.9, 10, 10, 390, 30)])
        self.assertEqual(find(items, "插件", min_tier="starts"), [])


if __name__ == "__main__":
    unittest.main()
