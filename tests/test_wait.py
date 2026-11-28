"""act 的 wait：带文字时轮询、「A|B」命中哪一个、超时与截断、until=change。

时钟与 sleep 注入（见 test_act 的说明）：sleep 推进同一个假时钟，长等待瞬间跑完。
until=change 用真的 griddiff 和合成帧：左上角一块一直在闪（模拟粒子），
要证明它不会触发「变了」，而一大块真变化会。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import act as A  # noqa: E402
from ui_locate import griddiff  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, s)


def unused(*_a, **_k):
    raise AssertionError("不该被调用")


def run(spec, *, probe=unused, capture=unused, differ=None, clock=None):
    clock = clock or FakeClock()
    return A.act(spec, probe=probe, capture=capture, perform=unused, compare=unused,
                 sleep=clock.sleep, clock=clock, differ=differ), clock


class WaitText(unittest.TestCase):
    def test_polls_until_found(self):
        n = {"calls": 0}

        def probe(text, spec):
            n["calls"] += 1
            return {"matched": "准备就绪", "cx": 10, "cy": 20, "w": 5, "h": 5, "conf": 0.9} \
                if n["calls"] >= 3 else None

        out, _ = run({"action": "wait", "text": "准备就绪", "timeout_ms": 60000}, probe=probe)
        self.assertTrue(out["ok"])
        self.assertEqual(n["calls"], 3)
        self.assertEqual((out["target"]["cx"], out["target"]["cy"]), (10, 20))
        self.assertIsNone(out["performed"])

    def test_alternative_reports_query(self):
        probe = lambda t, s: {"query": "确认", "matched": "确认", "cx": 1, "cy": 2, "conf": 1}  # noqa: E731
        out, _ = run({"action": "wait", "text": "准备就绪|确认", "timeout_ms": 1000}, probe=probe)
        self.assertEqual(out["target"]["query"], "确认")
        self.assertEqual(out["wait"]["query"], "确认")

    def test_timeout(self):
        out, clock = run({"action": "wait", "text": "x", "timeout_ms": 5000}, probe=lambda t, s: None)
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "wait")
        self.assertIn("超时", out["error"])
        self.assertGreaterEqual(clock.t - 1000.0, 5.0)

    def test_clamped_to_http_budget(self):
        out, clock = run({"action": "wait", "text": "x", "timeout_ms": 600000}, probe=lambda t, s: None)
        self.assertEqual(out["wait"]["clamped_from_ms"], 600000)
        self.assertLess(clock.t - 1000.0, 175.0)


H, W = 320, 640  # 20 x 10 = 200 格，安静上限 2 格


def frame(anim: int, block: bool = False) -> tuple[np.ndarray, tuple[int, int]]:
    img = np.full((H, W, 3), 90, dtype=np.uint8)
    img[0:64, 0:64] = 40 if anim % 2 == 0 else 220   # 一直在闪的 4 格
    if block:
        img[128:256, 256:512] = 250                  # 真变化：32 格
    return img, (0, 0)


def differ(a, b, ignore=None):
    return griddiff.diff(a[0], b[0], origin=tuple(a[1]), ignore=ignore)


class WaitChange(unittest.TestCase):
    def test_flicker_alone_would_trigger_without_mask(self):
        # 反例前提：不屏蔽噪声的话，光是闪烁就超过安静上限
        self.assertFalse(griddiff.is_quiet(differ(frame(0), frame(1))))

    def test_flicker_ignored_then_real_change_detected(self):
        seq = iter([frame(0), frame(1), frame(0), frame(1), frame(0), frame(1, block=True)])
        out, _ = run({"action": "wait", "until": "change", "timeout_ms": 30000},
                     capture=lambda m: next(seq), differ=differ)
        self.assertTrue(out["ok"])
        self.assertEqual(out["change"]["frames"], 6)
        self.assertEqual(out["change"]["noise_cells"], 4)

    def test_slow_drift_not_a_change(self):
        # 背景整体缓慢变亮（实测主界面光效）：变化格很多、但强度低，不算界面切换
        k = {"i": 0}

        def cap(_m):
            k["i"] += 1
            img = np.full((H, W, 3), 90 + min(k["i"] * 4, 30), dtype=np.uint8)
            return img, (0, 0)

        out, _ = run({"action": "wait", "until": "change", "timeout_ms": 10000},
                     capture=cap, differ=differ)
        self.assertFalse(out["ok"])
        self.assertFalse(griddiff.is_quiet(differ(cap(None), (np.full((H, W, 3), 90, np.uint8), (0, 0)))))

    def test_no_change_times_out(self):
        k = {"i": 0}

        def cap(_m):
            k["i"] += 1
            return frame(k["i"])

        out, clock = run({"action": "wait", "until": "change", "timeout_ms": 10000},
                         capture=cap, differ=differ)
        self.assertFalse(out["ok"])
        self.assertFalse(out["change"]["changed"])
        self.assertGreaterEqual(clock.t - 1000.0, 10.0)


if __name__ == "__main__":
    unittest.main()
