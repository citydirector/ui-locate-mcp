"""同名多目标：宁可拒绝，也不替调用方掷骰子。

真实场景：msinfo32 的「系统摘要」既是左树的一个节点，也是右侧结果区的标题，
两者 OCR 都是 exact。以前取第一个就点，点到标题、`verified:false`，但调用方
看到的 `ok:true` 很容易被当成成功。这里钉住三点：不调用 perform、给出候选、
错误码可被程序判断。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import act  # noqa: E402


def _probe_ambiguous(text, spec):
    return {
        "ambiguous": True,
        "text": text,
        "count": 2,
        "candidates": [
            {"text": "系统摘要", "cx": -1075, "cy": -856, "w": 62, "h": 26, "conf": 0.91},
            {"text": "系统摘要", "cx": -996, "cy": -857, "w": 218, "h": 24, "conf": 0.88},
        ],
    }


class AmbiguousTargetTest(unittest.TestCase):
    def test_refuses_without_performing(self):
        called: list[tuple[str, dict]] = []

        def perform(action, params):
            called.append((action, params))
            return {"dispatched": True}

        out = act.act(
            {"text": "系统摘要", "action": "click"},
            probe=_probe_ambiguous,
            capture=lambda monitor: None,
            perform=perform,
            compare=lambda *a, **k: None,
        )

        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "AMBIGUOUS_TARGET")
        self.assertEqual(out["stage"], "find")
        self.assertEqual(out["dispatched"], None)
        self.assertEqual(out["verified"], None)
        self.assertEqual(len(out["candidates"]), 2)
        self.assertEqual(out["candidates"][0]["cx"], -1075)
        self.assertEqual(called, [], "歧义时绝不能发出任何输入")

    def test_unique_target_still_clicks(self):
        def probe(text, spec):
            return {"matched": text, "match_tier": "exact", "cx": 10, "cy": 20,
                    "w": 30, "h": 12, "conf": 0.9}

        out = act.act(
            {"text": "确定", "action": "click"},
            probe=probe,
            capture=lambda monitor: None,
            perform=lambda action, params: {"dispatched": True, "sent": 1},
            compare=lambda *a, **k: None,
        )

        self.assertTrue(out["ok"])
        self.assertEqual(out["matched"] if "matched" in out else out["target"]["text"], "确定")


if __name__ == "__main__":
    unittest.main(verbosity=2)
