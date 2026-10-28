"""帧记忆（`ui_locate.frames`）的测试。

只测纯逻辑那一层，不开服务、不抓屏 —— `FrameStore` 的所有输入都是显式
传入的数组和坐标，所以能这样测。真机上的抓屏与 HTTP 往返另外验。

重点守三件事：
  1. 回合号单调递增、永不复用（复用会让「第 3 回合」在不同时刻指不同画面）
  2. 超上限按**整回合**淘汰（丢单帧会让回合内序号错位，比少一帧更难查）
  3. crop 的 box 用**屏幕绝对坐标**，副屏原点为负时也要算对
"""

from __future__ import annotations

import unittest

import numpy as np

from ui_locate.frames import FrameStore


def make(w: int = 64, h: int = 48, value: int = 0) -> np.ndarray:
    """造一张纯色 BGR 图。"""
    return np.full((h, w, 3), value, dtype=np.uint8)


class TurnTests(unittest.TestCase):
    def test_turn_numbers_are_monotonic_and_never_reused(self):
        store = FrameStore(max_turns=2)
        a = store.begin_turn()
        b = store.begin_turn()
        c = store.begin_turn()          # a 会被淘汰
        self.assertEqual((a, b, c), (1, 2, 3))
        # 淘汰之后新回合仍然接着 3 往上，不会回到 1
        d = store.begin_turn()
        self.assertEqual(d, 4)

    def test_put_returns_none_for_evicted_turn(self):
        store = FrameStore(max_turns=1)
        old = store.begin_turn()
        store.begin_turn()              # old 被淘汰
        self.assertIsNone(store.put(old, make()))

    def test_frames_per_turn_is_capped(self):
        store = FrameStore(max_frames_per_turn=2)
        turn = store.begin_turn()
        self.assertIsNotNone(store.put(turn, make(), label="before"))
        self.assertIsNotNone(store.put(turn, make(), label="after"))
        self.assertIsNone(store.put(turn, make(), label="third"))


class EvictionTests(unittest.TestCase):
    def test_bytes_cap_drops_whole_oldest_turn(self):
        # 每帧 64x48x3 = 9216 字节。上限设成只装得下两个回合。
        store = FrameStore(max_turns=100, max_frames_per_turn=4, max_bytes=9216 * 4)
        t1 = store.begin_turn()
        for _ in range(2):
            store.put(t1, make())
        t2 = store.begin_turn()
        for _ in range(2):
            store.put(t2, make())
        # 现在恰好 4 帧 = 上限，两个回合都在
        self.assertEqual(store.stats()["turns"], 2)

        t3 = store.begin_turn()
        store.put(t3, make())
        # 超了：最老的 t1 整批丢掉（两帧一起走），不是只丢一帧
        turns = [t["turn"] for t in store.turns()]
        self.assertNotIn(t1, turns)
        self.assertIn(t2, turns)
        self.assertIn(t3, turns)

    def test_single_turn_over_cap_drops_its_oldest_frames(self):
        store = FrameStore(max_turns=1, max_frames_per_turn=10, max_bytes=9216 * 2)
        turn = store.begin_turn()
        for _ in range(4):
            store.put(turn, make())
        stats = store.stats()
        self.assertEqual(stats["turns"], 1)
        self.assertLessEqual(stats["frames"], 2)

    def test_peak_bytes_is_remembered(self):
        store = FrameStore(max_turns=1, max_frames_per_turn=10, max_bytes=9216)
        turn = store.begin_turn()
        for _ in range(3):
            store.put(turn, make())
        # 峰值要记到淘汰之前，否则「到底涨到过多大」这个问题永远答不了
        self.assertGreaterEqual(store.stats()["peak_bytes"], 9216)


class ReadTests(unittest.TestCase):
    def test_get_by_turn_and_frame(self):
        store = FrameStore()
        turn = store.begin_turn()
        store.put(turn, make(value=10), label="before")
        store.put(turn, make(value=20), label="after")
        self.assertEqual(int(store.get(turn, 0).bgr[0, 0, 0]), 10)
        self.assertEqual(int(store.get(turn, 1).bgr[0, 0, 0]), 20)
        # 负索引从后往前数，和列表一致
        self.assertEqual(int(store.get(turn, -1).bgr[0, 0, 0]), 20)
        self.assertIsNone(store.get(turn, 5))
        self.assertIsNone(store.get(999, 0))

    def test_latest_returns_last_frame_of_last_turn(self):
        store = FrameStore()
        t1 = store.begin_turn()
        store.put(t1, make(value=1))
        t2 = store.begin_turn()
        store.put(t2, make(value=2))
        store.put(t2, make(value=3))
        self.assertEqual(store.latest().ref.turn, t2)
        self.assertEqual(int(store.latest().bgr[0, 0, 0]), 3)

    def test_latest_turn_reports_even_empty_turn(self):
        store = FrameStore()
        store.begin_turn()
        self.assertEqual(store.latest_turn(), 1)

    def test_empty_turn_is_listed_with_zero_frames(self):
        # 有回合但没抓到帧（比如纯 wait 动作），列表里应该能看到它
        store = FrameStore()
        store.begin_turn()
        turns = store.turns()
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["frames"], [])


class CropTests(unittest.TestCase):
    def test_origin_is_honoured_for_negative_offsets(self):
        """副屏原点为负时，box 用绝对坐标也必须裁对位置。"""
        store = FrameStore()
        turn = store.begin_turn()
        img = make(100, 80)
        img[:, 50:] = 200               # 右半边亮，用来判断裁到哪了
        store.put(turn, img, origin=(-1234, -1080))
        frame = store.get(turn, 0)

        # 绝对坐标 (-1234+50, -1080) 起，取右半边
        piece, meta = store.crop(frame, box=(-1234 + 50, -1080, -1234 + 100, -1080 + 80))
        self.assertIsNotNone(piece)
        self.assertGreater(int(piece[0, 0, 0]), 150)
        # 元信息里的 box 必须是**屏幕绝对坐标**，调用方据此换算
        self.assertEqual(meta["box"][0], -1234 + 50)
        self.assertEqual(meta["box"][1], -1080)
        self.assertEqual(meta["origin"], [-1234 + 50, -1080])

    def test_box_outside_frame_is_clipped_not_rejected(self):
        store = FrameStore()
        turn = store.begin_turn()
        store.put(turn, make(100, 80))
        frame = store.get(turn, 0)
        # 压着右下边缘：能裁出来，只是小
        piece, meta = store.crop(frame, box=(90, 70, 200, 200))
        self.assertIsNotNone(piece)
        self.assertEqual(meta["box"], [90, 70, 100, 80])

    def test_fully_outside_box_returns_error(self):
        store = FrameStore()
        turn = store.begin_turn()
        store.put(turn, make(100, 80))
        frame = store.get(turn, 0)
        piece, meta = store.crop(frame, box=(500, 500, 600, 600))
        self.assertIsNone(piece)
        self.assertIn("error", meta)

    def test_scale_is_capped(self):
        """自动缩放最多 6 倍 —— 再大只是插值出来的糊图，对读字没帮助。"""
        store = FrameStore()
        turn = store.begin_turn()
        store.put(turn, make(20, 20))
        frame = store.get(turn, 0)
        _piece, meta = store.crop(frame, box=(0, 0, 20, 20))
        self.assertLessEqual(meta["scale"], 6.0)

    def test_explicit_scale_is_respected(self):
        store = FrameStore()
        turn = store.begin_turn()
        store.put(turn, make(40, 40))
        frame = store.get(turn, 0)
        piece, meta = store.crop(frame, box=(0, 0, 40, 40), scale=2.0)
        self.assertEqual(piece.shape[:2], (80, 80))
        self.assertEqual(meta["scale"], 2.0)


class ClearTests(unittest.TestCase):
    def test_clear_empties_store_but_keeps_counter(self):
        store = FrameStore()
        t1 = store.begin_turn()
        store.put(t1, make())
        store.clear()
        self.assertEqual(store.stats()["turns"], 0)
        self.assertEqual(store.latest_turn(), 1)
        # 清空之后再开回合仍然接着编号，不复用 1
        self.assertEqual(store.begin_turn(), 2)


if __name__ == "__main__":
    unittest.main()
