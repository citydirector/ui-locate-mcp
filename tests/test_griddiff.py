"""网格差分、等画面静止、按变化区域判定生效 —— 纯 numpy，任何平台都能跑。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import act as A  # noqa: E402
from ui_locate import griddiff as G  # noqa: E402


def blank(h=256, w=320, v=40):
    return np.full((h, w, 3), v, dtype=np.uint8)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += max(0.0, s)


class GridDiffTests(unittest.TestCase):
    def test_identical_frames_have_no_change(self):
        d = G.diff(blank(), blank())
        self.assertEqual(d["changed_cells"], 0)
        self.assertEqual(d["regions"], [])
        self.assertTrue(G.is_quiet(d))

    def test_one_block_becomes_one_region_in_screen_coords(self):
        a, b = blank(), blank()
        b[64:128, 96:160] = 220            # 2x2 格的面板弹出
        d = G.diff(a, b, origin=(-1000, 50))
        self.assertEqual(d["changed_cells"], 4)
        self.assertEqual(len(d["regions"]), 1)
        r = d["regions"][0]
        self.assertEqual((r["x"], r["y"], r["w"], r["h"]), (96 - 1000, 64 + 50, 64, 64))
        self.assertGreater(r["strength"], 100)

    def test_faint_noise_is_ignored(self):
        rng = np.random.default_rng(0)
        a = blank()
        b = np.clip(a.astype(int) + rng.integers(-4, 5, a.shape), 0, 255).astype(np.uint8)
        self.assertEqual(G.diff(a, b)["changed_cells"], 0)

    def test_separate_changes_are_separate_regions_sorted_by_size(self):
        a, b = blank(), blank()
        b[0:32, 0:32] = 200
        b[128:224, 192:288] = 200          # 3x3
        regs = G.diff(a, b)["regions"]
        self.assertEqual([r["cells"] for r in regs], [9, 1])

    def test_ignore_mask(self):
        a, b = blank(), blank()
        b[0:32, 0:32] = 200
        d0 = G.diff(a, b)
        d = G.diff(a, b, ignore=d0["_mask"])
        self.assertEqual(d["changed_cells"], 0)

    def test_near(self):
        a, b = blank(), blank()
        b[64:96, 64:96] = 200
        d = G.diff(a, b)
        self.assertIsNotNone(G.near(d, 80, 80, 10))
        self.assertIsNone(G.near(d, 300, 240, 10))

    def test_shape_mismatch_raises(self):
        with self.assertRaises(ValueError):
            G.cell_scores(blank(256, 320), blank(200, 320))

    def test_wait_stable_stops_after_motion_ends(self):
        clock = FakeClock()
        seq = [blank(v=v) for v in (10, 60, 120, 120, 120, 120)]
        it = iter(seq)
        report, last = G.wait_stable(lambda: (next(it), (0, 0)), sleep=clock.advance, clock=clock,
                                     timeout_ms=2000)
        self.assertTrue(report["stable"])
        self.assertEqual(report["frames"], 5)
        self.assertEqual(int(last[0][0, 0, 0]), 120)

    def test_wait_stable_times_out_when_always_moving(self):
        clock = FakeClock()
        n = {"i": 0}

        def grab():
            n["i"] += 1
            return blank(v=(n["i"] * 50) % 250), (0, 0)

        report, _ = G.wait_stable(grab, sleep=clock.advance, clock=clock, timeout_ms=300)
        self.assertFalse(report["stable"])


def differ(a, b, ignore=None):
    (ia, oa), (ib, ob) = a, b
    if ia.shape != ib.shape:
        return None
    return G.diff(ia, ib, origin=oa, ignore=ignore)


class ActWithDifferTests(unittest.TestCase):
    def run_act(self, spec, frames, *, compare=lambda *a: 0.5):
        clock = FakeClock()
        frames = [frames[0], frames[0]] + list(frames)   # 动作前量噪声多抓的两帧
        it = iter(frames)
        last = {"f": frames[-1]}

        def capture(_m):
            try:
                last["f"] = next(it)
            except StopIteration:
                pass
            return last["f"]

        return A.act(spec, probe=lambda t, s: {"cx": 80, "cy": 80, "w": 20, "h": 20, "conf": 1},
                     capture=capture, perform=lambda a, p: {"sent": 1}, compare=compare,
                     sleep=clock.advance, clock=clock, differ=differ)

    def test_settle_auto_waits_for_stable_and_region_verifies_small_change(self):
        before = (blank(), (0, 0))
        changed = blank()
        changed[64:96, 64:96] = 200        # 按钮那一格亮了，平均差被稀释（compare 给 0.5）
        after = (changed, (0, 0))
        out = self.run_act({"text": "x", "verify": "screen"}, [before, after, after, after])
        self.assertEqual(out["settle"]["mode"], "stable")
        self.assertTrue(out["settle"]["stable"])
        self.assertTrue(out["verified"])
        self.assertEqual(out["verify"]["by"], "region")
        self.assertIn("settle_ms", out["timings"])

    def test_far_away_change_does_not_verify(self):
        before = (blank(), (0, 0))
        changed = blank()
        changed[192:256, 256:320] = 200
        out = self.run_act({"text": "x", "verify": "screen"}, [before, (changed, (0, 0))])
        self.assertFalse(out["verified"])
        self.assertEqual(len(out["verify"]["regions"]), 1)

    def test_explicit_settle_ms_is_fixed(self):
        out = self.run_act({"text": "x", "verify": "screen", "settle_ms": 100},
                           [(blank(), (0, 0))] * 3)
        self.assertNotIn("settle", out)

    def test_settle_mode_rules(self):
        self.assertEqual(A.settle_mode({}, "screen", can_diff=True), "stable")
        self.assertEqual(A.settle_mode({}, "screen", can_diff=False), "fixed")
        self.assertEqual(A.settle_mode({"settle_ms": 300}, "screen", can_diff=True), "fixed")
        self.assertEqual(A.settle_mode({"settle": "stable"}, None, can_diff=True), "stable")
        self.assertEqual(A.settle_mode({"settle": "none"}, "screen", can_diff=True), "none")
        self.assertEqual(A.settle_mode({}, {"text": "a"}, can_diff=True), "fixed")

    def test_wait_without_text_waits_for_stable(self):
        f = (blank(), (0, 0))
        out = self.run_act({"action": "wait"}, [f, f, f])
        self.assertTrue(out["ok"])
        self.assertTrue(out["settle"]["stable"])

    def test_keyboard_screen_scope_uses_changed_fraction(self):
        before = (blank(), (0, 0))
        menu = blank()
        menu[0:128, 0:160] = 200           # 菜单弹出：大面积变化
        clock = FakeClock()
        frames = iter([before, before, before, (menu, (0, 0)), (menu, (0, 0)), (menu, (0, 0))])
        state = {"f": before}

        def cap(_m):
            state["f"] = next(frames, state["f"])
            return state["f"]

        out = A.act({"action": "press", "key": "esc", "verify": "screen"},
                    probe=lambda *a: None, capture=cap, perform=lambda a, p: {"sent": 2},
                    compare=lambda *a: 1.0, sleep=clock.advance, clock=clock, differ=differ)
        self.assertTrue(out["verified"])
        self.assertEqual(out["verify"]["by"], "region")

    def test_animated_scene_click_on_nothing_is_not_verified(self):
        # 水面 / 粒子：静止时右半边一直在变，点左边空白处之后也只有右半边在变
        rng = np.random.default_rng(1)

        def frame():
            f = blank()
            f[:, 160:] = rng.integers(0, 255, (256, 160, 3), dtype=np.uint8)
            return f, (0, 0)

        clock = FakeClock()
        out = A.act({"x": 170, "y": 100, "verify": "screen"}, probe=lambda *a: None,
                    capture=lambda _m: frame(), perform=lambda a, p: {"sent": 1},
                    compare=lambda a, b, x, y, r: float(np.abs(a[0].astype(int) - b[0].astype(int)).mean()),
                    sleep=clock.advance, clock=clock, differ=differ)
        self.assertTrue(out["verify"]["noise"]["animated"])
        self.assertFalse(out["verified"])
        self.assertGreater(out["verify"]["threshold"], 3.5)

    def test_animated_scene_real_change_still_verifies(self):
        rng = np.random.default_rng(2)
        state = {"n": 0}

        def frame():
            state["n"] += 1
            f = blank()
            f[:, 192:] = rng.integers(0, 255, (256, 128, 3), dtype=np.uint8)
            if state["n"] > 3:                 # 动作之后左上弹出面板
                f[32:128, 32:160] = 230
            return f, (0, 0)

        clock = FakeClock()
        out = A.act({"x": 80, "y": 80, "verify": "screen"}, probe=lambda *a: None,
                    capture=lambda _m: frame(), perform=lambda a, p: {"sent": 1},
                    compare=lambda a, b, x, y, r: 0.0, sleep=clock.advance, clock=clock,
                    differ=differ)
        self.assertTrue(out["verified"])
        self.assertEqual(out["verify"]["by"], "region")

    def test_dilate(self):
        m = np.zeros((5, 5), bool)
        m[2, 2] = True
        self.assertEqual(int(G.dilate(m).sum()), 9)

    def test_window_spec_passes_to_input_layer(self):
        name, params = A.build_action({"action": "press", "key": "esc", "monitor": "window:原神"},
                                      {"cx": None, "cy": None})
        self.assertEqual(params["window"], "window:原神")
        name, params = A.build_action({"action": "click", "focus": False}, {"cx": 1, "cy": 2})
        self.assertIs(params["focus"], False)
        self.assertNotIn("window", params)


if __name__ == "__main__":
    unittest.main()
