"""后台优先通道：不许悄悄升格成抢前台。

这条边界是给"用户正在用这台机器"的场景立的。判断标准很硬：一个要求
``delivery="background"`` 的调用，要么**不碰**真实指针/前台焦点，要么
**明确失败**。中间那条"后台不行就换成 SendInput 试试"的路必须要堵死 ——
它不会报错，只会让光标在别人打字时跳走，而且事后无法从返回值看出来发生过。

测试用替身把 SendInput / 光标移动 / 窗口查找全部封成"一被调用就炸"，这样
"是否真的没走前台"不是靠读代码相信，而是由测试执行路径本身证明。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import act as A  # noqa: E402
from ui_locate import actions  # noqa: E402


def boom(*_a, **_kw):
    raise AssertionError("这条路径不该被调用（前台/指针/抓屏）")


class DeliveryModeTests(unittest.TestCase):
    def test_aliases(self):
        cases = {
            "send_input": "send_input", "sendinput": "send_input",
            "foreground": "send_input", "fg": "send_input", "input": "send_input",
            "message": "message", "post": "message", "postmessage": "message",
            "msg": "message",
            "background": "background", "bg": "background",
            "auto": "auto", "": "auto", None: "auto",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(actions.delivery_mode(raw), want)

    def test_unknown_is_rejected_loudly(self):
        with self.assertRaises(actions.InputError) as ctx:
            actions.delivery_mode("teleport")
        self.assertIn("delivery", str(ctx.exception))

    def test_alias_sets_are_disjoint(self):
        self.assertFalse(actions.BACKGROUND_DELIVERIES & actions.FOREGROUND_DELIVERIES)


class BackgroundClickTests(unittest.TestCase):
    """background = 绑定 hwnd 的后台投递；前台/指针/抓屏一律不许被碰。

    具体怎么投（UIA / PostMessage、几何下探、白名单）在 ``test_background.py``
    里逐条验。这里只管 ``actions.click`` 这一层的分派与失败语义。
    """

    def test_background_delegates_by_hwnd_and_never_touches_foreground(self):
        fake = {"ok": True, "channel": "uia-invoke", "dispatched": True,
                "hwnd": 0x100, "sent": True}
        with mock.patch("ui_locate.background.click", return_value=fake) as bg_click, \
                mock.patch.object(actions, "_send", boom), \
                mock.patch.object(actions, "_cursor_or_none", boom), \
                mock.patch.object(actions, "_goto", boom), \
                mock.patch.object(actions, "_normalize", boom), \
                mock.patch.object(actions, "_click_message", boom):
            report = actions.click(10, 20, delivery="background", hwnd=0x100)
        bg_click.assert_called_once()
        kwargs = bg_click.call_args.kwargs
        self.assertEqual(kwargs["hwnd"], 0x100)
        self.assertEqual(report["route"], "background")
        self.assertEqual(report["delivery"], "background")
        self.assertEqual(report["channel"], "uia-invoke")
        self.assertTrue(report["dispatched"])
        self.assertNotIn("send_input", report)
        self.assertNotIn("message", report)

    def test_background_with_relative_pointer_still_never_pushes(self):
        fake = {"ok": True, "channel": "message", "dispatched": True, "sent": True}
        with mock.patch("ui_locate.background.click", return_value=fake), \
                mock.patch.object(actions, "_goto", boom), \
                mock.patch.object(actions, "_send", boom), \
                mock.patch.object(actions, "_cursor_or_none", boom):
            report = actions.click(10, 20, delivery="bg", pointer="relative", hwnd=0x100)
        self.assertEqual(report["route"], "background")
        self.assertNotIn("relative", report)

    def test_activate_reaches_the_input_layer(self):
        """activate 曾经被这一层吞掉：传 never 实际仍按 auto 发 WM_ACTIVATE。

        声明式开关谎报后果很实际 —— 用户以为「后台绝不碰前台」，实际每次都
        发了一条可能让目标自己抢前台的消息。
        """
        fake = {"ok": True, "channel": "message", "dispatched": True, "sent": True}
        with mock.patch("ui_locate.background.click", return_value=fake) as bg_click, \
                mock.patch.object(actions, "_send", boom), \
                mock.patch.object(actions, "_cursor_or_none", boom):
            actions.click(10, 20, delivery="background", hwnd=0x100, activate="never")
            self.assertEqual(bg_click.call_args.kwargs.get("activate"), "never")

            bg_click.reset_mock()
            actions.click(10, 20, delivery="background", hwnd=0x100, activate="always")
            self.assertEqual(bg_click.call_args.kwargs.get("activate"), "always")

            bg_click.reset_mock()
            actions.click(10, 20, delivery="background", hwnd=0x100)
            # 没表态就不塞，让 background 用它的默认（auto）
            self.assertNotIn("activate", bg_click.call_args.kwargs)

    def test_background_without_hwnd_fails_explicitly(self):
        with mock.patch.object(actions, "_send", boom), \
                mock.patch.object(actions, "_click_message", boom):
            with self.assertRaises(actions.BackgroundUnavailable) as ctx:
                actions.click(10, 20, delivery="background")
        err = ctx.exception
        self.assertEqual(err.code, "BACKGROUND_UNAVAILABLE")
        body = err.body()
        self.assertFalse(body["ok"])
        self.assertFalse(body["dispatched"])
        self.assertIsNone(body["verified"])
        self.assertEqual(body["delivery"], "background")
        # 明确要求 hwnd，而不是拿坐标去猜一个窗口
        self.assertEqual(body["reason"], "hwnd_required")
        self.assertIn("hwnd", body["error"])
        self.assertIn("foreground", body["hint"])

    def test_background_refusal_from_module_propagates(self):
        with mock.patch("ui_locate.background.click",
                        side_effect=actions.BackgroundUnavailable("PostMessage 被拒绝",
                                                                  reason="post_failed")), \
                mock.patch.object(actions, "_send", boom):
            with self.assertRaises(actions.BackgroundUnavailable) as ctx:
                actions.click(1, 2, delivery="background", hwnd=0x100)
        self.assertEqual(ctx.exception.reason, "post_failed")
        self.assertFalse(ctx.exception.body()["dispatched"])

    def test_message_channel_keeps_raw_sent_false(self):
        with mock.patch.object(actions, "_click_message",
                               return_value={"sent": False, "reason": "该坐标上没有窗口"}), \
                mock.patch.object(actions, "_send", boom):
            report = actions.click(1, 2, delivery="message")
        self.assertFalse(report["message"]["sent"])

    def test_foreground_never_falls_back_even_when_cursor_stays(self):
        still = {"x": 5, "y": 5}
        with mock.patch.object(actions, "_send", return_value=2), \
                mock.patch.object(actions, "_cursor_or_none", return_value=still), \
                mock.patch.object(actions, "_normalize", return_value=(5, 5)), \
                mock.patch.object(actions, "_click_message", boom):
            report = actions.click(5, 5, delivery="foreground")
        self.assertEqual(report["send_input"]["sent"], 2)
        self.assertIs(report["send_input"]["cursor_moved"], False)
        self.assertNotIn("message", report)

    def test_auto_still_degrades_when_cursor_visibly_stuck(self):
        """老行为保持不变：auto 只在明确观察到光标没动时才补一次消息。"""
        still = {"x": 5, "y": 5}
        with mock.patch.object(actions, "_send", return_value=2), \
                mock.patch.object(actions, "_cursor_or_none", return_value=still), \
                mock.patch.object(actions, "_normalize", return_value=(5, 5)), \
                mock.patch.object(actions, "_click_message",
                                  return_value={"sent": True}) as msg:
            report = actions.click(5, 5, delivery="auto")
        self.assertEqual(msg.call_count, 1)
        self.assertIn("message", report)

    def test_auto_does_not_degrade_when_cursor_is_unknown(self):
        with mock.patch.object(actions, "_send", return_value=2), \
                mock.patch.object(actions, "_cursor_or_none", return_value=None), \
                mock.patch.object(actions, "_normalize", return_value=(5, 5)), \
                mock.patch.object(actions, "_click_message", boom):
            report = actions.click(5, 5, delivery="auto")
        self.assertIsNone(report["send_input"]["cursor_moved"])
        self.assertNotIn("message", report)


class DeliveryPlanTests(unittest.TestCase):
    def test_click_family_accepts_background(self):
        for action in ("click", "double_click", "right_click", "middle_click"):
            with self.subTest(action=action):
                self.assertEqual(A.delivery_plan(action, {"delivery": "background"}),
                                 ("background", None, None))

    def test_non_click_actions_are_refused_for_background(self):
        for action in ("type", "press", "hotkey", "scroll", "drag", "move"):
            with self.subTest(action=action):
                plan, reason, code = A.delivery_plan(action, {"delivery": "background"})
                self.assertIsNone(plan)
                self.assertEqual(code, "BACKGROUND_UNAVAILABLE")
                self.assertIn("foreground", reason)

    def test_message_on_non_click_is_a_different_code(self):
        plan, reason, code = A.delivery_plan("type", {"delivery": "message"})
        self.assertIsNone(plan)
        self.assertEqual(code, "CHANNEL_UNSUPPORTED")
        self.assertIn("type", reason)

    def test_foreground_explicit(self):
        self.assertEqual(A.delivery_plan("click", {"delivery": "foreground"}),
                         ("send_input", None, None))
        self.assertEqual(A.delivery_plan("type", {"delivery": "foreground"}),
                         (None, None, None))

    def test_default_and_auto_keep_old_behaviour(self):
        self.assertEqual(A.delivery_plan("click", {}), (None, None, None))
        self.assertEqual(A.delivery_plan("click", {"delivery": "auto"}),
                         (None, None, None))

    def test_typo_is_reported_as_bad_delivery(self):
        plan, reason, code = A.delivery_plan("click", {"delivery": "backgrond"})
        self.assertIsNone(plan)
        self.assertEqual(code, "BAD_DELIVERY")
        self.assertIn("foreground", reason)


def run_act(spec, *, perform=None, capture=None, compare=None, differ=None):
    """跑一次 act，默认所有外部能力都是"一碰就炸"。"""
    performed = []
    captured = []

    def _perform(action, params):
        performed.append((action, dict(params)))
        if perform is None:
            raise AssertionError("perform 不该被调用")
        return perform(action, params)

    def _capture(monitor):
        captured.append(monitor)
        if capture is None:
            raise AssertionError("capture 不该被调用")
        return capture(monitor)

    result = A.act(
        spec,
        probe=lambda text, s: {"cx": 10, "cy": 20, "w": 4, "h": 4, "conf": 0.9,
                               "matched": text, "match_tier": "exact"},
        capture=_capture,
        perform=_perform,
        compare=compare or boom,
        sleep=lambda _s: None,
        clock=lambda: 0.0,
        differ=differ,
    )
    return result, performed, captured


class ActBackgroundTests(unittest.TestCase):
    def test_background_typing_is_refused_before_any_screenshot(self):
        result, performed, captured = run_act(
            {"action": "type", "text": "abc", "x": 1, "y": 2,
             "delivery": "background", "verify": "screen"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "act")
        self.assertEqual(result["code"], "BACKGROUND_UNAVAILABLE")
        self.assertIn("foreground", result["error"])
        self.assertIsNone(result["performed"])
        self.assertFalse(result["dispatched"])
        self.assertIsNone(result["verified"])
        self.assertEqual(result["delivery"], "background")
        # 关键：报错不能以"先抓一张图"为代价
        self.assertEqual(captured, [])
        self.assertEqual(performed, [])

    def test_typo_delivery_is_refused_as_bad_delivery(self):
        result, performed, captured = run_act(
            {"action": "click", "x": 1, "y": 2, "delivery": "backgrond"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "BAD_DELIVERY")
        self.assertEqual(performed, [])
        self.assertEqual(captured, [])

    def test_background_click_succeeds_and_reports_dispatched(self):
        result, performed, captured = run_act(
            {"action": "click", "x": 1, "y": 2, "delivery": "background"},
            perform=lambda a, p: {"message": {"sent": True, "accepted": 6, "total": 6}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["delivery"], "background")
        self.assertTrue(result["dispatched"])
        self.assertIsNone(result["verified"])
        self.assertEqual(len(performed), 1)
        self.assertEqual(performed[0][1]["delivery"], "background")
        # 没有 verify=screen 时不该抓图
        self.assertEqual(captured, [])

    def test_dispatched_is_false_when_the_window_did_not_accept(self):
        result, _performed, _captured = run_act(
            {"action": "click", "x": 1, "y": 2, "delivery": "background"},
            perform=lambda a, p: {"message": {"sent": False, "accepted": 0, "total": 6}})
        self.assertTrue(result["ok"])
        self.assertFalse(result["dispatched"])
        self.assertIsNone(result["verified"])

    def test_input_error_from_perform_is_reported_with_code(self):
        err = actions.BackgroundUnavailable("没窗口", x=1, y=2, reason="该坐标上没有窗口")

        def perform(_a, _p):
            raise err

        result, _performed, _captured = run_act(
            {"action": "click", "x": 1, "y": 2, "delivery": "background"},
            perform=perform)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "act")
        self.assertEqual(result["code"], "BACKGROUND_UNAVAILABLE")
        self.assertFalse(result["dispatched"])
        self.assertIsNone(result["performed"])
        self.assertIn("hint", result)

    def test_verified_false_never_replays_the_action(self):
        calls = []

        def perform(action, params):
            calls.append(1)
            return {"send_input": {"sent": 2, "cursor_moved": True}}

        result, performed, captured = run_act(
            {"action": "click", "x": 1, "y": 2, "verify": "screen", "settle": "fixed",
             "settle_ms": 0},
            perform=perform,
            capture=lambda _m: "frame",
            compare=lambda *_a, **_k: 0.0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(performed), 1)
        self.assertIs(result["verified"], False)
        self.assertTrue(result["dispatched"])
        # before + after 两次抓图，但动作只发了一次
        self.assertEqual(len(captured), 2)

    def test_wait_action_has_no_target_and_no_dispatch(self):
        result, performed, captured = run_act({"action": "wait", "timeout_ms": 1})
        self.assertTrue(result["ok"])
        self.assertIsNone(result["performed"])
        self.assertIsNone(result["dispatched"])
        self.assertIsNone(result["verified"])
        self.assertEqual(performed, [])


if __name__ == "__main__":
    unittest.main()
