"""`ocr_match.find` 的单元测试：匹配档位与排序。

为什么单独成文件、而不是并进 `test_act.py`：
  `test_act.py` 测的是「编排」（重试、等待、短路、记账），这里的被测对象是
  「匹配」。两者都纯、都零依赖，但一个改动不会牵动另一个，分开跑更容易定位。

这些用例守的是一个真实事故（见 `find` 的 docstring）：
  查「插件」，那一帧 mosaic 漏了检测框、按钮文字根本没进 OCR，于是掉到
  contains 档，命中任务栏上「视觉定位MCP插件开发」。点下去、窗口获得焦点、
  屏幕确实变了，`verified: true`。**工具报告成功，实际点错了地方。**

所以重点不在"能找到"，而在"找不到时不许凑合"。

跑法：
  python -m unittest discover -s tests -p "test_*.py"
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate.ocr_match import find  # noqa: E402


def item(text: str, cx: float = 100.0, cy: float = 50.0, conf: float = 0.8) -> dict:
    """造一个最简元素。只需要 find 会读的那几个字段。"""
    return {"text": text, "cx": cx, "cy": cy, "w": 40.0, "h": 20.0, "conf": conf}


class TierTests(unittest.TestCase):
    """三档的判定与 min_tier 的收口。"""

    def test_exact_beats_starts(self):
        items = [item("添加插件", conf=0.99), item("插件", conf=0.40)]
        hits = find(items, "插件")
        self.assertEqual(hits[0]["text"], "插件")
        self.assertEqual(hits[0]["match_tier"], "exact")

    def test_starts_beats_contains(self):
        # 「8 插件」剥掉图标噪声后是「插件」→ exact；这里换成真正的前缀形态
        items = [item("插件设置", conf=0.30), item("打开插件面板", conf=0.99)]
        hits = find(items, "插件")
        self.assertEqual(hits[0]["text"], "插件设置")
        self.assertEqual(hits[0]["match_tier"], "starts")

    def test_contains_is_last_resort(self):
        items = [item("视觉定位MCP插件开发", conf=0.99)]
        hits = find(items, "插件")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["match_tier"], "contains")

    def test_min_tier_starts_rejects_contains(self):
        """这正是那次点错的形态：只剩 contains 时，动手必须拒绝。"""
        items = [item("视觉定位MCP插件开发", conf=0.99)]
        self.assertEqual(find(items, "插件", min_tier="starts"), [])

    def test_min_tier_starts_accepts_starts(self):
        items = [item("插件设置", conf=0.3)]
        hits = find(items, "插件", min_tier="starts")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["match_tier"], "starts")

    def test_min_tier_exact_only(self):
        items = [item("插件设置"), item("插件")]
        hits = find(items, "插件", min_tier="exact")
        self.assertEqual([h["text"] for h in hits], ["插件"])

    def test_unknown_min_tier_returns_empty(self):
        # 拼错了不能悄悄退化成"什么都接受"
        self.assertEqual(find([item("插件")], "插件", min_tier="whatever"), [])

    def test_empty_query_returns_empty(self):
        self.assertEqual(find([item("插件")], "   "), [])


class RankingTests(unittest.TestCase):
    """同档内排序：长度差 > conf。"""

    def test_shorter_text_wins_over_higher_conf(self):
        """实测依据：正文里那句「这次"插件"没有点中…」conf 0.99，
        而真按钮 conf 只有 0.46。只看 conf 会点到正文中间去。"""
        items = [
            item("这次插件没有点中正文中间那一段", cx=10, conf=0.99),
            item("插件设置", cx=20, conf=0.46),
        ]
        hits = find(items, "插件")
        self.assertEqual(hits[0]["cx"], 20)

    def test_icon_noise_is_stripped_before_ranking(self):
        """「8 插件」剥完是「插件」，应当进 exact 档而不是被长度差拖到后面。"""
        items = [item("8 插件", cx=1, conf=0.3), item("插件说明文档", cx=2, conf=0.9)]
        hits = find(items, "插件")
        self.assertEqual(hits[0]["cx"], 1)
        self.assertEqual(hits[0]["match_tier"], "exact")

    def test_limit_is_respected(self):
        items = [item(f"插件{i}") for i in range(10)]
        self.assertEqual(len(find(items, "插件", limit=3)), 3)

    def test_items_without_text_are_ignored(self):
        items = [{"cx": 1, "cy": 2, "w": 3, "h": 4, "conf": 0.9}, item("插件")]
        hits = find(items, "插件")
        self.assertEqual([h["text"] for h in hits], ["插件"])


class PurityTests(unittest.TestCase):
    """`find` 不该改调用方的数据 —— 调用方常常要拿同一份 elements 反复查。"""

    def test_input_items_are_not_mutated(self):
        items = [item("插件")]
        before = dict(items[0])
        find(items, "插件")
        self.assertEqual(items[0], before)

    def test_match_tier_lands_on_the_result_not_the_source(self):
        items = [item("插件")]
        hits = find(items, "插件")
        self.assertEqual(hits[0]["match_tier"], "exact")
        self.assertNotIn("match_tier", items[0])


if __name__ == "__main__":
    unittest.main()
