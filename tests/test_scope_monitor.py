"""定位范围的规则：显式 monitor 优先，其次是绑定的窗口。

这一条是纯参数转换，但错了会「在 A 窗口找到坐标、投递进 B 窗口」——
`verified` 可能还是 true（B 窗口确实变了），所以必须靠单测钉住。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import act  # noqa: E402


class ScopeMonitorTest(unittest.TestCase):
    def test_explicit_monitor_wins(self):
        self.assertEqual(
            act.scope_monitor({"monitor": "primary", "window": 3015060}),
            "primary",
        )

    def test_hwnd_and_numeric_string_become_handles(self):
        self.assertEqual(act.scope_monitor({"window": 3015060}), "hwnd:3015060")
        self.assertEqual(act.scope_monitor({"window": "3015060"}), "hwnd:3015060")
        self.assertEqual(act.scope_monitor({"window": "0x2E00AE"}), "hwnd:0x2E00AE")

    def test_title_becomes_title_spec(self):
        self.assertEqual(act.scope_monitor({"window": "系统信息"}), "window:系统信息")

    def test_explicit_spec_passes_through(self):
        self.assertEqual(act.scope_monitor({"window": "hwnd:42"}), "hwnd:42")
        self.assertEqual(act.scope_monitor({"window": "window:记事本"}), "window:记事本")

    def test_nothing_to_scope(self):
        self.assertIsNone(act.scope_monitor({}))
        self.assertIsNone(act.scope_monitor({"window": ""}))
        self.assertIsNone(act.scope_monitor({"window": 0}))

    def test_window_as_spec_agrees(self):
        """window.as_spec 与 scope_monitor 必须同一套规则（不允许两处漂移）。"""
        from ui_locate import window as window_mod

        for value in (3015060, "3015060", "系统信息", "hwnd:42", "window:记事本"):
            with self.subTest(value=value):
                self.assertEqual(
                    window_mod.as_spec(value),
                    act.scope_monitor({"window": value}) or "",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
