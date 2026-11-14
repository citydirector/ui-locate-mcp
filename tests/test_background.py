"""后台投递的单元测试：几何、白名单、拒绝、暂停、预算。

这里**不**碰真实桌面：Win32 那几个薄封装被换成假的"窗口树"，于是"下探到哪
一层""该不该拒"这类逻辑可以逐条断言。真实窗口的验证放在冒烟脚本里（需要
一个真的 WinForms 窗口），不进单元测试 —— 单元测试不该依赖当前桌面上有什么。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import background as bg  # noqa: E402

# setUp 里会把 _activate 换成"什么都不发"，需要它的用例自己换回来。
_REAL_ACTIVATE = bg._activate
from ui_locate.actions import BackgroundUnavailable, InputError, Paused  # noqa: E402


class FakeWinModule:
    """window 模块的替身：只提供 background 会用的那两三个函数。"""

    def __init__(self, *, shell=(), uipi_blocked=(), resolve_hwnd=0):
        self.shell = set(shell)
        self.uipi_blocked = set(uipi_blocked)
        self.resolve_hwnd = resolve_hwnd

    def is_shell(self, hwnd):
        return hwnd in self.shell

    def uipi(self, hwnd):
        return {"blocked": hwnd in self.uipi_blocked}

    def resolve(self, spec):
        if not self.resolve_hwnd:
            raise RuntimeError(f"找不到窗口 {spec!r}")
        info = mock.Mock()
        info.hwnd = self.resolve_hwnd
        return info


class World:
    """一棵假窗口树：hwnd → 属性，父 → 直接子。"""

    def __init__(self):
        self.windows: dict[int, dict] = {}
        self.children: dict[int, int] = {}
        self.posted: list[tuple[int, int, int, int]] = []
        self.post_ok = True
        self.screen_to_client = (10, 20)

    def add(self, hwnd, cls, pid=7, *, visible=True, minimized=False, hung=False,
            rect=(0, 0, 400, 300), child=None):
        self.windows[hwnd] = {"cls": cls, "pid": pid, "visible": visible,
                              "minimized": minimized, "hung": hung, "rect": rect}
        if child:
            self.children[hwnd] = child
        return hwnd

    # ── background 依赖的薄封装 ──
    def alive(self, hwnd):
        return hwnd in self.windows

    def visible(self, hwnd):
        return self.windows[hwnd]["visible"]

    def minimized(self, hwnd):
        return self.windows[hwnd]["minimized"]

    def hung(self, hwnd):
        return self.windows[hwnd]["hung"]

    def cls(self, hwnd):
        return self.windows[hwnd]["cls"]

    def pid(self, hwnd):
        return self.windows[hwnd]["pid"]

    def rect(self, hwnd):
        return self.windows[hwnd]["rect"]

    def child_at(self, hwnd, x, y):
        return self.children.get(hwnd, 0)

    def s2c(self, hwnd, x, y):
        return self.screen_to_client

    def post(self, hwnd, msg, wp, lp):
        self.posted.append((hwnd, msg, wp, lp))
        return self.post_ok


class BackgroundCase(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.win = FakeWinModule(shell=(0x999,), uipi_blocked=(0x998,))
        patches = [
            mock.patch.object(bg, "_require", lambda: None),
            mock.patch.object(bg, "_alive", self.world.alive),
            mock.patch.object(bg, "_visible", self.world.visible),
            mock.patch.object(bg, "_minimized", self.world.minimized),
            mock.patch.object(bg, "_hung", self.world.hung),
            mock.patch.object(bg, "_cls", self.world.cls),
            mock.patch.object(bg, "_pid", self.world.pid),
            mock.patch.object(bg, "_title", lambda hwnd: "fake"),
            mock.patch.object(bg, "_client_rect", self.world.rect),
            mock.patch.object(bg, "_child_at", self.world.child_at),
            mock.patch.object(bg, "_screen_to_client", self.world.s2c),
            mock.patch.object(bg, "_post", self.world.post),
            mock.patch.object(bg, "_win_module", lambda: self.win),
            mock.patch.object(bg.uia, "available", lambda: True),
            # 默认不发 WM_ACTIVATE：绝大多数用例关心的是鼠标序列本身。
            mock.patch.object(bg, "_activate",
                              lambda hwnd, mode: {"sent": False, "skipped": "test"}),
            mock.patch.object(bg, "_foreground", lambda: 0),
            mock.patch.object(bg, "_root_owner", lambda hwnd: hwnd),
            mock.patch.object(bg, "_last_active_popup", lambda root: 0),
            mock.patch.object(bg, "_sleep", lambda _s: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.resume_cleanup()

    def resume_cleanup(self):
        bg.resume()
        self.addCleanup(bg.resume)

    def dialog(self, *, child=None, cls="#32770"):
        return self.world.add(0x100, cls, child=child)


class ResolveTests(BackgroundCase):
    def test_hwnd_is_required_and_nothing_is_guessed(self):
        with self.assertRaises(BackgroundUnavailable) as ctx:
            bg.resolve()
        self.assertEqual(ctx.exception.reason, "hwnd_required")
        self.assertIn("hwnd", str(ctx.exception))
        self.assertIn("hwnd", ctx.exception.body()["hint"])

    def test_window_spec_can_stand_in_for_hwnd(self):
        self.dialog()
        self.win.resolve_hwnd = 0x100
        target = bg.resolve(window="window:Demo")
        self.assertEqual(target["hwnd"], 0x100)
        self.assertEqual(target["cls"], "#32770")
        self.assertEqual(target["client"], (0, 0, 400, 300))

    def test_window_spec_that_cannot_be_resolved_is_refused(self):
        with self.assertRaises(BackgroundUnavailable) as ctx:
            bg.resolve(window="window:Missing")
        self.assertEqual(ctx.exception.reason, "window_not_found")

    def test_dead_hwnd_is_refused(self):
        with self.assertRaises(BackgroundUnavailable) as ctx:
            bg.resolve(hwnd=0xDEAD)
        self.assertEqual(ctx.exception.reason, "window_gone")


class CheckTargetTests(BackgroundCase):
    def test_visible_dialog_passes(self):
        self.dialog()
        bg.check_target(bg.resolve(hwnd=0x100))  # 不抛就是通过

    def test_invisible_window_is_refused(self):
        self.world.add(0x100, "#32770", visible=False)
        with self.assertRaises(BackgroundUnavailable) as ctx:
            bg.check_target(bg.resolve(hwnd=0x100))
        self.assertEqual(ctx.exception.reason, "not_visible")

    def test_minimized_window_is_refused(self):
        self.world.add(0x100, "#32770", minimized=True)
        with self.assertRaises(BackgroundUnavailable) as ctx:
            bg.check_target(bg.resolve(hwnd=0x100))
        self.assertEqual(ctx.exception.reason, "minimized")

    def test_hung_window_is_refused(self):
        self.world.add(0x100, "#32770", hung=True)
        with self.assertRaises(BackgroundUnavailable) as ctx:
            bg.check_target(bg.resolve(hwnd=0x100))
        self.assertEqual(ctx.exception.reason, "hung")

    def test_shell_window_is_refused(self):
        self.world.add(0x999, "#32770")
        with self.assertRaises(BackgroundUnavailable) as ctx:
            bg.check_target(bg.resolve(hwnd=0x999))
        self.assertEqual(ctx.exception.reason, "shell_window")

    def test_chromium_class_is_refused_before_anything_else(self):
        self.world.add(0x100, "Chrome_WidgetWin_1")
        with self.assertRaises(BackgroundUnavailable) as ctx:
            bg.check_target(bg.resolve(hwnd=0x100))
        self.assertEqual(ctx.exception.reason, "class_denied")
        self.assertIn("Chromium", str(ctx.exception))

    def test_winui_and_uwp_classes_are_refused(self):
        for cls in ("ApplicationFrameWindow", "Windows.UI.Core.CoreWindow",
                    "Microsoft.UI.Content.DesktopChildSiteBridge"):
            with self.subTest(cls=cls):
                world = World()
                world.add(0x200, cls)
                with mock.patch.object(bg, "_alive", world.alive), \
                        mock.patch.object(bg, "_cls", world.cls), \
                        mock.patch.object(bg, "_pid", world.pid), \
                        mock.patch.object(bg, "_client_rect", world.rect), \
                        mock.patch.object(bg, "_visible", world.visible), \
                        mock.patch.object(bg, "_minimized", world.minimized), \
                        mock.patch.object(bg, "_hung", world.hung), \
                        mock.patch.object(bg, "_win_module", lambda: self.win):
                    with self.assertRaises(BackgroundUnavailable) as ctx:
                        bg.check_target(bg.resolve(hwnd=0x200))
                self.assertEqual(ctx.exception.reason, "class_denied")

    def test_uipi_blocked_target_is_refused(self):
        self.world.add(0x998, "#32770")
        with self.assertRaises(BackgroundUnavailable) as ctx:
            bg.check_target(bg.resolve(hwnd=0x998))
        self.assertEqual(ctx.exception.reason, "uipi_blocked")


class DescendTests(BackgroundCase):
    def test_two_levels_are_followed(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "ComboBox", child=0x102)
        self.world.add(0x102, "Edit")
        target = bg.resolve(hwnd=0x100)
        found = bg.descend(target, 10, 20)
        self.assertEqual([n["hwnd"] for n in found["chain"]], [0x100, 0x101, 0x102])
        self.assertEqual(found["leaf"]["cls"], "Edit")
        self.assertEqual(found["depth"], 2)

    def test_foreign_process_child_stops_the_walk(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button", pid=99)  # 别的进程：不能顺着走
        found = bg.descend(bg.resolve(hwnd=0x100), 10, 20)
        self.assertEqual([n["hwnd"] for n in found["chain"]], [0x100])
        self.assertEqual(found["depth"], 0)

    def test_depth_is_capped(self):
        self.dialog(child=0x101)
        for i in range(20):
            self.world.add(0x101 + i, "Static", child=0x102 + i)
        found = bg.descend(bg.resolve(hwnd=0x100), 10, 20)
        self.assertEqual(found["depth"], bg.MAX_DEPTH)


class ClickTests(BackgroundCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            bg.uia, "invoke", return_value={"ok": True, "pattern": "invoke"})
        self.uia_invoke = patcher.start()
        self.addCleanup(patcher.stop)

    def test_uia_success_does_not_post_any_message(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        out = bg.click(10, 20, hwnd=0x100)
        self.assertEqual(out["channel"], "uia-invoke")
        self.assertTrue(out["dispatched"])
        self.assertEqual(self.world.posted, [])
        self.uia_invoke.assert_called_once()

    def test_uia_missing_pattern_falls_back_to_messages(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        self.uia_invoke.return_value = {"ok": False, "reason": "no_pattern"}
        out = bg.click(10, 20, hwnd=0x100)
        self.assertEqual(out["channel"], "message")
        self.assertTrue(out["dispatched"])
        msgs = [m for (_h, m, _w, _l) in self.world.posted]
        self.assertEqual(msgs, [bg.WM_MOUSEMOVE, bg.WM_LBUTTONDOWN,
                                bg.WM_MOUSEMOVE, bg.WM_LBUTTONUP])
        # 「按住移动」那一步的 wParam 必须带键位，否则控件不认这是按着的拖动
        self.assertEqual(self.world.posted[2][2], bg.MK_LBUTTON)

    def test_uia_unavailable_still_uses_messages(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        with mock.patch.object(bg.uia, "available", lambda: False):
            out = bg.click(10, 20, hwnd=0x100)
        self.assertEqual(out["channel"], "message")

    def test_uia_no_element_falls_back_to_messages(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        self.uia_invoke.return_value = {"ok": False, "reason": "no_element"}
        out = bg.click(10, 20, hwnd=0x100)
        self.assertEqual(out["channel"], "message")

    def test_uia_timeout_never_reposts(self):
        # UIA 超时 = 不知道有没有点成。这时再投一遍消息就可能点两下。
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        self.uia_invoke.return_value = {"ok": False, "reason": "timeout"}
        with self.assertRaises(BackgroundUnavailable) as ctx:
            bg.click(10, 20, hwnd=0x100)
        self.assertEqual(ctx.exception.reason, "uia_unknown")
        self.assertEqual(self.world.posted, [])
        body = ctx.exception.body()
        self.assertIsNone(body["dispatched"])  # 不知道，不是"没发生"
        self.assertEqual(body["reason"], "uia_unknown")
        self.assertEqual(body["code"], "BACKGROUND_UNKNOWN")
        self.assertIn("点两下", body["error"])

    def test_uia_busy_and_error_are_also_unknown(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        for reason in ("busy", "error"):
            with self.subTest(reason=reason):
                self.world.posted.clear()
                self.uia_invoke.return_value = {"ok": False, "reason": reason}
                with self.assertRaises(BackgroundUnavailable) as ctx:
                    bg.click(10, 20, hwnd=0x100)
                self.assertEqual(ctx.exception.reason, "uia_unknown")
                self.assertEqual(self.world.posted, [])
                self.assertIsNone(ctx.exception.body()["dispatched"])

    def test_uia_only_mode_refuses_instead_of_posting(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        self.uia_invoke.return_value = {"ok": False, "reason": "no_pattern"}
        with self.assertRaises(BackgroundUnavailable) as ctx:
            bg.click(10, 20, hwnd=0x100, prefer="uia")
        self.assertEqual(ctx.exception.reason, "uia_unavailable")
        self.assertEqual(self.world.posted, [])
        self.assertFalse(ctx.exception.body()["dispatched"])

    def test_point_outside_client_area_is_refused(self):
        # 根窗口客户区很小，点落在它外面：只报"点不在窗口内"，不去猜别的窗口。
        self.world.add(0x100, "#32770", rect=(0, 0, 5, 5))
        with self.assertRaises(BackgroundUnavailable) as ctx:
            bg.click(10, 20, hwnd=0x100)
        self.assertEqual(ctx.exception.reason, "point_outside_window")
        self.assertEqual(self.world.posted, [])

    def test_non_whitelisted_control_is_refused_not_foregrounded(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "SomeCanvas")
        self.uia_invoke.return_value = {"ok": False, "reason": "no_pattern"}
        with self.assertRaises(BackgroundUnavailable) as ctx:
            bg.click(10, 20, hwnd=0x100)
        self.assertEqual(ctx.exception.reason, "class_not_allowed")
        self.assertEqual(self.world.posted, [])
        self.assertIn("foreground", ctx.exception.body()["hint"])

    def test_post_failure_is_reported_as_not_dispatched(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        self.world.post_ok = False
        self.uia_invoke.return_value = {"ok": False, "reason": "no_pattern"}
        with self.assertRaises(BackgroundUnavailable) as ctx:
            bg.click(10, 20, hwnd=0x100)
        self.assertEqual(ctx.exception.reason, "post_failed")
        self.assertFalse(ctx.exception.body()["dispatched"])

    def test_winforms_and_msctls_classes_are_allowed(self):
        self.dialog(child=0x101, cls="WindowsForms10.Window.8.app.0.1")
        self.world.add(0x101, "WindowsForms10.BUTTON.app.0.1")
        self.uia_invoke.return_value = {"ok": False, "reason": "no_pattern"}
        out = bg.click(10, 20, hwnd=0x100)
        self.assertEqual(out["channel"], "message")

    def test_click_count_posts_full_sequences(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        self.uia_invoke.return_value = {"ok": False, "reason": "no_pattern"}
        bg.click(10, 20, hwnd=0x100, count=2, delay_ms=0)
        self.assertEqual(len(self.world.posted), 8)


class PauseTests(BackgroundCase):
    def test_pause_blocks_background_click(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        bg.pause("演示中")
        with self.assertRaises(Paused) as ctx:
            bg.click(10, 20, hwnd=0x100)
        body = ctx.exception.body()
        self.assertEqual(body["code"], "PAUSED")
        self.assertTrue(body["paused"])
        self.assertEqual(self.world.posted, [])

    def test_pause_blocks_foreground_send_input_guard(self):
        from ui_locate import actions
        bg.pause()
        with self.assertRaises(Paused):
            actions._paused_guard()

    def test_resume_clears_state(self):
        bg.pause("x")
        out = bg.resume()
        self.assertFalse(out["paused"])
        self.assertTrue(out["was_paused"])
        self.assertFalse(bg.state()["paused"])

    def test_state_reports_reason_and_since(self):
        bg.pause("演示中")
        st = bg.state()
        self.assertTrue(st["paused"])
        self.assertEqual(st["reason"], "演示中")
        self.assertIsInstance(st["since"], float)


class BudgetTests(unittest.TestCase):
    def test_mark_raises_when_budget_is_spent(self):
        budget = bg._Budget(50)
        time.sleep(0.08)
        with self.assertRaises(BackgroundUnavailable) as ctx:
            budget.mark("after_sleep")
        self.assertEqual(ctx.exception.reason, "budget_exhausted")

    def test_left_never_goes_negative(self):
        budget = bg._Budget(50)
        time.sleep(0.08)
        self.assertEqual(budget.left(), 0)


class NoForegroundSurfaceTests(unittest.TestCase):
    """模块文档里会**讲到**这些名字，但代码里不许真的调用它们。

    所以扫的是 AST 里的属性/名字，不是原文 —— 否则说明性文字会被当成违规。
    """

    FORBIDDEN_MODULE = ("SetForegroundWindow", "SetCursorPos", "SendInput",
                        "mouse_event", "keybd_event", "AttachThreadInput")
    FORBIDDEN_UIA = ("SetForegroundWindow", "SetCursorPos", "SendInput",
                     "ElementFromPoint")

    @staticmethod
    def _used_names(source: str) -> set[str]:
        import ast
        names: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
        return names

    def test_module_never_touches_foreground_or_pointer(self):
        used = self._used_names(Path(bg.__file__).read_text(encoding="utf-8"))
        for forbidden in self.FORBIDDEN_MODULE:
            self.assertNotIn(forbidden, used, forbidden)

    def test_uia_adapter_has_no_foreground_calls(self):
        from ui_locate import uia
        used = self._used_names(Path(uia.__file__).read_text(encoding="utf-8"))
        for forbidden in self.FORBIDDEN_UIA:
            self.assertNotIn(forbidden, used, forbidden)


class DescribeTests(BackgroundCase):
    def test_describe_reports_refusal_without_raising(self):
        self.world.add(0x100, "Chrome_WidgetWin_1")
        out = bg.describe(hwnd=0x100)
        self.assertFalse(out["allowed"])
        self.assertEqual(out["reason"], "class_denied")

    def test_describe_reports_chain_and_uia_patterns(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        with mock.patch.object(bg.uia, "probe", return_value={"ok": True, "patterns": ["invoke"]}):
            out = bg.describe(hwnd=0x100, x=10, y=20)
        self.assertTrue(out["allowed"])
        self.assertEqual(out["leaf"]["hwnd"], 0x101)
        self.assertEqual(out["uia"]["patterns"], ["invoke"])



class ActivateTests(BackgroundCase):
    """发送前那条 WM_ACTIVATE：三种取值、只发一条、不碰真实前台。"""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(bg, "_activate", _REAL_ACTIVATE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _click(self, **kw):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        with mock.patch.object(bg.uia, "available", lambda: False):
            return bg.click(10, 20, hwnd=0x100, **kw)

    def test_never_sends_nothing(self):
        out = self._click(activate="never")
        self.assertFalse(out["activation"]["sent"])
        self.assertEqual(out["activation"]["skipped"], "never")
        self.assertEqual([m for (_h, m, _w, _l) in self.world.posted].count(
            bg.WM_ACTIVATE), 0)

    def test_always_sends_one_activate_before_the_mouse(self):
        out = self._click(activate="always")
        self.assertTrue(out["activation"]["sent"])
        msgs = [m for (_h, m, _w, _l) in self.world.posted]
        self.assertEqual(msgs[0], bg.WM_ACTIVATE)
        self.assertEqual(self.world.posted[0][2], bg.WA_ACTIVE)
        self.assertEqual(msgs.count(bg.WM_ACTIVATE), 1)

    def test_auto_skips_when_target_is_already_foreground(self):
        with mock.patch.object(bg, "_foreground", lambda: 0x100):
            out = self._click(activate="auto")
        self.assertFalse(out["activation"]["sent"])
        self.assertEqual(out["activation"]["skipped"], "already_foreground")

    def test_auto_sends_when_target_is_not_foreground(self):
        with mock.patch.object(bg, "_foreground", lambda: 0x777):
            out = self._click(activate="auto")
        self.assertTrue(out["activation"]["sent"])

    def test_unknown_mode_is_refused(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        with self.assertRaises(BackgroundUnavailable) as ctx:
            bg.click(10, 20, hwnd=0x100, activate="sometimes")
        self.assertEqual(ctx.exception.reason, "bad_activate")
        self.assertEqual(self.world.posted, [])


class PopupTests(BackgroundCase):
    """模态弹窗出现时，消息要发给弹窗（并且坐标按弹窗换算）。"""

    def setUp(self):
        super().setUp()
        with mock.patch.object(bg.uia, "available", lambda: False):
            self.addCleanup(lambda: None)

    def test_active_popup_takes_over(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        self.world.add(0x200, "#32770", rect=(0, 0, 400, 300), child=0x201)
        self.world.add(0x201, "Button")
        with mock.patch.object(bg, "_root_owner", lambda hwnd: 0x100), \
                mock.patch.object(bg, "_last_active_popup", lambda root: 0x200), \
                mock.patch.object(bg.uia, "available", lambda: False):
            out = bg.click(10, 20, hwnd=0x100)
        self.assertEqual(out["popup"], {"from": 0x100, "to": 0x200, "cls": "#32770"})
        self.assertEqual(out["hwnd"], 0x200)
        self.assertTrue(all(h == 0x201 for (h, _m, _w, _l) in self.world.posted))

    def test_popup_outside_the_point_is_ignored(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        self.world.add(0x200, "#32770", rect=(500, 500, 40, 40))
        with mock.patch.object(bg, "_root_owner", lambda hwnd: 0x100), \
                mock.patch.object(bg, "_last_active_popup", lambda root: 0x200), \
                mock.patch.object(bg.uia, "available", lambda: False):
            out = bg.click(10, 20, hwnd=0x100)
        self.assertIsNone(out["popup"])
        self.assertEqual(out["hwnd"], 0x100)

    def test_popup_is_never_guessed_when_invisible(self):
        self.dialog(child=0x101)
        self.world.add(0x101, "Button")
        self.world.add(0x200, "#32770", visible=False)
        with mock.patch.object(bg, "_root_owner", lambda hwnd: 0x100), \
                mock.patch.object(bg, "_last_active_popup", lambda root: 0x200), \
                mock.patch.object(bg.uia, "available", lambda: False):
            out = bg.click(10, 20, hwnd=0x100)
        self.assertIsNone(out["popup"])


class KeyLparamTests(BackgroundCase):
    """keydown / keyup 的 lParam 位：bit30/31 不能少。"""

    def test_keydown_bits(self):
        self.assertEqual(bg._key_lparam(0x1C), 1 | (0x1C << 16))

    def test_keyup_sets_previous_state_and_transition_bits(self):
        value = bg._key_lparam(0x1C, up=True)
        self.assertEqual(value, 1 | (0x1C << 16) | (1 << 30) | (1 << 31))

    def test_scan_code_is_masked_to_one_byte(self):
        self.assertEqual(bg._key_lparam(0x11C), 1 | (0x1C << 16))

    def test_key_messages_post_down_then_up(self):
        out = bg._key_messages(0x300, 0x0D, 0x1C)
        self.assertTrue(out["sent"])
        self.assertEqual([m for (_h, m, _w, _l) in self.world.posted],
                         [bg.WM_KEYDOWN, bg.WM_KEYUP])
        self.assertEqual(self.world.posted[0][2], 0x0D)
        self.assertEqual(self.world.posted[1][2], 0x0D)
        self.assertEqual(self.world.posted[0][3], 1 | (0x1C << 16))
        self.assertEqual(self.world.posted[1][3],
                         1 | (0x1C << 16) | (1 << 30) | (1 << 31))

    def test_key_messages_report_refusal(self):
        self.world.post_ok = False
        out = bg._key_messages(0x300, 0x0D, 0x1C)
        self.assertFalse(out["sent"])
        self.assertEqual(out["reason"], "WM_KEYDOWN 被拒绝")


if __name__ == "__main__":
    unittest.main()
