"""act.py 的单元测试：编排逻辑用纯桩跑，不加载模型、不开端口、不碰屏幕。

为什么这一层最值得测：act.py 里全是「重试、等待、短路、记账」——这些分支
在真机上要么很少触发（找不到文字）、要么要等好几秒才走到（等文字超时）。
靠手工点界面验证，成本高且不可重复；用桩跑，几十毫秒就能把每条分支走一遍。

时钟与 sleep 全部注入：`FakeClock` 让时间按需跳跃，于是「等 8 秒超时」这种
用例也能瞬间跑完。**这里有个坑**：如果 clock 用真实 monotonic 而 sleep 是
空操作，`wait_for_text` 的循环条件永远不成立（时间不前进），会死循环。
所以 sleep 必须推进同一个假时钟。

跑法：
  python -m unittest discover -s tests -p "test_*.py"
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import act as A  # noqa: E402


class FakeClock:
    """可以手动拨动的单调钟。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


def make_probe(hits: dict[str, dict] | None = None, *, found_at: int | None = None):
    """造一个 probe：按文字返回预设命中；`found_at` 表示第 n 次调用才命中。

    返回的对象上带 `.calls`，测试可以断言「找了几次」。
    """
    table = hits or {}
    state = {"n": 0}

    def probe(text: str, spec: dict) -> dict | None:
        state["n"] += 1
        if found_at is not None and state["n"] < found_at:
            return None
        hit = table.get(text)
        return dict(hit) if hit else None

    probe.calls = state  # type: ignore[attr-defined]
    return probe


def hit(cx: int = 100, cy: int = 200, **kw) -> dict:
    base = {"matched": "目标", "cx": cx, "cy": cy, "w": 40, "h": 20, "conf": 0.9}
    base.update(kw)
    return base


class TargetTests(unittest.TestCase):
    def test_text_wins_over_coordinates(self):
        timings: dict = {}
        target, info = A.resolve_target(
            {"text": "保存", "x": 1, "y": 2},
            probe=make_probe({"保存": hit(300, 400)}),
            timings=timings, clock=FakeClock(),
        )
        self.assertEqual(info["reason"], "text")
        self.assertEqual((target["cx"], target["cy"]), (300, 400))
        self.assertIn("find_ms", timings)

    def test_coordinates_without_text(self):
        target, info = A.resolve_target(
            {"x": 10, "y": 20},
            probe=make_probe(), timings={}, clock=FakeClock(),
        )
        self.assertEqual(info["reason"], "coordinates")
        self.assertEqual((target["cx"], target["cy"]), (10, 20))
        self.assertEqual(target["conf"], 1.0)

    def test_reference_element(self):
        target, info = A.resolve_target(
            {"at": {"cx": 55, "cy": 66, "text": "确定"}},
            probe=make_probe(), timings={}, clock=FakeClock(),
        )
        self.assertEqual(info["reason"], "reference")
        self.assertEqual(target["text"], "确定")

    def test_missing_target_returns_none(self):
        target, info = A.resolve_target(
            {"text": "不存在"},
            probe=make_probe(), timings={}, clock=FakeClock(),
        )
        self.assertIsNone(target)
        self.assertEqual(info["reason"], "not-found")

    def test_nothing_given_raises(self):
        with self.assertRaises(A.ActError):
            A.resolve_target({}, probe=make_probe(), timings={}, clock=FakeClock())

    def test_coordinate_is_rounded_to_int(self):
        target, _ = A.resolve_target(
            {"x": 10.6, "y": 20.4},
            probe=make_probe(), timings={}, clock=FakeClock(),
        )
        self.assertIsInstance(target["cx"], int)
        self.assertEqual((target["cx"], target["cy"]), (11, 20))


class BuildActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = {"cx": 50, "cy": 60, "w": 10, "h": 10}

    def test_click_defaults(self):
        name, params = A.build_action({}, self.target)
        self.assertEqual(name, "click")
        self.assertEqual(params["x"], 50)
        self.assertEqual(params["button"], "left")
        self.assertEqual(params["count"], 1)
        self.assertEqual(params["delivery"], "auto")

    def test_double_click_count_is_two(self):
        _n, params = A.build_action({"action": "double_click"}, self.target)
        self.assertEqual(params["count"], 2)

    def test_right_and_middle_map_to_buttons(self):
        _n, right = A.build_action({"action": "right_click"}, self.target)
        _n, middle = A.build_action({"action": "middle_click"}, self.target)
        self.assertEqual(right["button"], "right")
        self.assertEqual(middle["button"], "middle")

    def test_scroll_delta_default_is_down(self):
        name, params = A.build_action({"action": "scroll"}, self.target)
        self.assertEqual(name, "scroll")
        self.assertEqual(params["delta"], -3)
        self.assertFalse(params["horizontal"])

    def test_type_action_aliases(self):
        for alias in ("type", "write"):
            name, params = A.build_action({"action": alias, "value": "你好"}, self.target)
            self.assertEqual(name, "type")
            self.assertEqual(params["text"], "你好")

    def test_press_and_hotkey(self):
        name, params = A.build_action({"action": "press", "key": "escape"}, self.target)
        self.assertEqual((name, params["key"]), ("press", "escape"))
        name, params = A.build_action({"action": "hotkey", "keys": "ctrl+s"}, self.target)
        self.assertEqual((name, params["keys"]), ("hotkey", "ctrl+s"))

    def test_wait_is_noop(self):
        name, params = A.build_action({"action": "wait"}, self.target)
        self.assertEqual(name, "none")
        self.assertEqual(params, {})

    def test_drag_starts_at_target_and_defers_endpoint(self):
        name, params = A.build_action({"action": "drag"}, self.target)
        self.assertEqual(name, "drag")
        self.assertEqual((params["x1"], params["y1"]), (50, 60))
        self.assertIsNone(params["x2"])
        self.assertIsNone(params["y2"])

    def test_unknown_action_lists_choices(self):
        with self.assertRaises(A.ActError) as ctx:
            A.build_action({"action": "explode"}, self.target)
        self.assertIn("click", str(ctx.exception))


class DragEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = {"cx": 50, "cy": 60, "w": 10, "h": 10}

    def test_explicit_to_x_to_y(self):
        params = A.build_action({"action": "drag", "to_x": 9, "to_y": 8}, self.target)[1]
        done = A.finish_drag(params, {"action": "drag", "to_x": 9, "to_y": 8},
                             probe=make_probe(), timings={}, clock=FakeClock())
        self.assertEqual((done["x2"], done["y2"]), (9, 8))

    def test_to_as_text_runs_probe(self):
        spec = {"action": "drag", "to": "终点"}
        params = A.build_action(spec, self.target)[1]
        timings: dict = {}
        done = A.finish_drag(params, spec, probe=make_probe({"终点": hit(7, 7)}),
                             timings=timings, clock=FakeClock())
        self.assertEqual((done["x2"], done["y2"]), (7, 7))
        self.assertIn("find_ms", timings)

    def test_to_as_pair(self):
        spec = {"action": "drag", "to": [3, 4]}
        params = A.build_action(spec, self.target)[1]
        done = A.finish_drag(params, spec, probe=make_probe(), timings={}, clock=FakeClock())
        self.assertEqual((done["x2"], done["y2"]), (3, 4))

    def test_missing_endpoint_raises_instead_of_guessing(self):
        spec = {"action": "drag"}
        params = A.build_action(spec, self.target)[1]
        with self.assertRaises(A.ActError) as ctx:
            A.finish_drag(params, spec, probe=make_probe(), timings={}, clock=FakeClock())
        self.assertIn("需要终点", str(ctx.exception))

    def test_text_endpoint_not_found_raises(self):
        spec = {"action": "drag", "to": "找不到的字"}
        params = A.build_action(spec, self.target)[1]
        with self.assertRaises(A.ActError):
            A.finish_drag(params, spec, probe=make_probe(), timings={}, clock=FakeClock())

    def test_materialize_points_turns_text_into_coordinates(self):
        out = A._materialize_points(
            {"from": "起点", "to": "终点"},
            probe=make_probe({"起点": hit(1, 2), "终点": hit(3, 4)}),
            timings={}, clock=FakeClock(),
        )
        self.assertEqual(out["from"], {"cx": 1, "cy": 2})
        self.assertEqual(out["to"], {"cx": 3, "cy": 4})


class VerifyTests(unittest.TestCase):
    def test_detects_clear_change(self):
        res = A.verify_screen(None, None, x=0, y=0,
                              compare=lambda *_a: 9.0)
        self.assertTrue(res["changed_enough"])
        self.assertEqual(res["kind"], "screen")

    def test_small_change_is_not_enough(self):
        res = A.verify_screen(None, None, x=0, y=0, compare=lambda *_a: 1.0)
        self.assertFalse(res["changed_enough"])

    def test_threshold_boundary_is_exclusive(self):
        res = A.verify_screen(None, None, x=0, y=0,
                              compare=lambda *_a: A._DEFAULT_CHANGE_THRESHOLD)
        self.assertFalse(res["changed_enough"], "恰好等于阈值不算变化")

    def test_uncomparable_is_unknown_not_success(self):
        res = A.verify_screen(None, None, x=0, y=0, compare=lambda *_a: None)
        self.assertIsNone(res["changed_enough"])
        self.assertIn("note", res)


class WaitForTextTests(unittest.TestCase):
    def test_returns_on_first_hit(self):
        clock = FakeClock()
        res = A.wait_for_text("确定", {}, probe=make_probe({"确定": hit(11, 22)}),
                              timeout_ms=1000, poll_ms=150, sleep=clock.advance, clock=clock)
        self.assertTrue(res["found"])
        self.assertEqual((res["cx"], res["cy"]), (11, 22))
        self.assertEqual(res["polls"], 1)

    def test_times_out_after_deadline(self):
        clock = FakeClock()
        res = A.wait_for_text("不会出现", {}, probe=make_probe(),
                              timeout_ms=600, poll_ms=150, sleep=clock.advance, clock=clock)
        self.assertFalse(res["found"])
        self.assertEqual(res["timeout_ms"], 600)
        self.assertGreaterEqual(res["elapsed_ms"], 600)
        self.assertGreater(res["polls"], 1, "应该轮询了不止一次")

    def test_hit_on_later_poll(self):
        clock = FakeClock()
        res = A.wait_for_text("慢", {}, probe=make_probe({"慢": hit()}, found_at=3),
                              timeout_ms=2000, poll_ms=150, sleep=clock.advance, clock=clock)
        self.assertTrue(res["found"])
        self.assertEqual(res["polls"], 3)

    def test_zero_timeout_still_probes_once(self):
        clock = FakeClock()
        res = A.wait_for_text("x", {}, probe=make_probe({"x": hit()}),
                              timeout_ms=0, poll_ms=150, sleep=clock.advance, clock=clock)
        self.assertTrue(res["found"])
        self.assertEqual(res["polls"], 1)


class ActTests(unittest.TestCase):
    def _act(self, spec, *, probe=None, perform=None, compare=None,
             capture=None, clock=None):
        clock = clock or FakeClock()
        return A.act(
            spec,
            probe=probe or make_probe({"确定": hit(30, 40)}),
            capture=capture or (lambda _m: "IMG"),
            perform=perform or (lambda _a, _p: {"sent": 1}),
            compare=compare or (lambda *_a: 9.0),
            sleep=clock.advance,
            clock=clock,
        )

    def test_find_failure_short_circuits_before_any_action(self):
        touched = []
        out = self._act({"text": "没有这个"},
                        perform=lambda a, p: touched.append(a))
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "find")
        self.assertEqual(touched, [], "找不到目标时不该动手")

    def test_successful_click_and_verify(self):
        # verify 必须显式给。act.py 是底层编排，不替调用方假设「要不要验证」；
        # MCP 工具层（ui_locate_act）才默认 verify="screen"。
        out = self._act({"text": "确定", "verify": "screen"}, compare=lambda *_a: 12.0)
        self.assertTrue(out["ok"])
        self.assertTrue(out["verified"])
        self.assertEqual(out["target"]["cx"], 30)
        self.assertEqual(out["verify"]["kind"], "screen")
        self.assertIn("act_ms", out["timings"])

    def test_perform_exception_becomes_structured_failure(self):
        def boom(_a, _p):
            raise RuntimeError("SendInput 一条事件都没送出去")

        out = self._act({"text": "确定"}, perform=boom)
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "act")
        self.assertIn("SendInput", out["error"])

    def test_no_change_means_not_verified_but_still_ok(self):
        out = self._act({"text": "确定"}, compare=lambda *_a: 0.5)
        self.assertTrue(out["ok"], "流程本身走完了")
        self.assertFalse(out["verified"], "但屏幕上没看出变化")

    def test_verify_none_reports_unknown(self):
        out = self._act({"text": "确定", "verify": None})
        self.assertIsNone(out["verified"])
        self.assertIsNone(out["verify"])

    def test_verify_by_text(self):
        out = self._act({"text": "确定", "verify": {"text": "购买成功", "timeout_ms": 500}},
                        probe=make_probe({"确定": hit(), "购买成功": hit(1, 1)}))
        self.assertTrue(out["verified"])
        self.assertEqual(out["verify"]["kind"], "text")

    def test_verify_by_text_timeout_is_failure(self):
        out = self._act({"text": "确定", "verify": {"text": "永远不出现", "timeout_ms": 300}})
        self.assertFalse(out["verified"])

    def test_coordinate_only_click_skips_find(self):
        probe = make_probe()
        out = self._act({"x": 5, "y": 6}, probe=probe)
        self.assertEqual(probe.calls["n"], 0, "给了坐标就不该再跑检测")
        self.assertEqual(out["target_source"], "coordinates")

    def test_wait_action_does_not_perform(self):
        touched = []
        out = self._act({"action": "wait"}, perform=lambda a, p: touched.append(a))
        self.assertTrue(out["ok"])
        self.assertEqual(touched, [])

    def test_settle_delays_before_verify(self):
        clock = FakeClock()
        out = self._act({"text": "确定", "settle_ms": 500}, clock=clock)
        self.assertGreaterEqual(out["timings"]["total_ms"], 500)

    def test_drag_with_missing_endpoint_raises_before_acting(self):
        """缺终点是指令写错，不是运行期失败 —— 抛 ActError，让服务层回 400。

        为什么不在缺终点时「原地拖一下」了事：那等于替调用方猜意图，而猜错的
        后果是屏幕上随机拖了个东西。宁可当场报错。
        """
        touched = []
        with self.assertRaises(A.ActError) as ctx:
            self._act({"text": "确定", "action": "drag"},
                      perform=lambda a, p: touched.append(a))
        self.assertIn("需要终点", str(ctx.exception))
        self.assertEqual(touched, [], "报错发生在动手之前")


class CaptureFailureTests(unittest.TestCase):
    """抓屏失败 ≠ 动作失败。这两件事必须分开报。"""

    def test_capture_error_does_not_abort_the_call(self):
        def broken(_monitor):
            raise OSError("screen grab failed")

        out = A.act(
            {"text": "确定", "verify": "screen"},
            probe=make_probe({"确定": hit()}),
            capture=broken,
            perform=lambda _a, _p: {"sent": 1},
            compare=lambda *_a: 9.0,
            sleep=lambda _s: None,
            clock=FakeClock(),
        )
        self.assertTrue(out["ok"], "动作已经执行了，不能因为没法验证就报失败")
        self.assertIsNone(out["verified"], "验证不了就是不知道，不是 false")
        self.assertIn("screen grab failed", out["verify_unavailable"])
        self.assertEqual(out["performed"], {"sent": 1}, "动作本身的结果要留住")

    def test_capture_error_after_action_is_also_tolerated(self):
        calls = {"n": 0}

        def flaky(_monitor):
            calls["n"] += 1
            if calls["n"] == 1:
                return "IMG"
            raise OSError("grab died mid-flight")

        out = A.act(
            {"text": "确定", "verify": "screen"},
            probe=make_probe({"确定": hit()}),
            capture=flaky,
            perform=lambda _a, _p: {"sent": 1},
            compare=lambda *_a: 9.0,
            sleep=lambda _s: None,
            clock=FakeClock(),
        )
        self.assertTrue(out["ok"])
        self.assertIsNone(out["verified"])
        self.assertIn("mid-flight", out["verify_unavailable"])

    def test_act_failure_also_reports_capture_error(self):
        """动作失败 + 抓屏失败同时发生：两件事都要说清楚。

        只报「动作失败」会把人引去查目标窗口，而真相可能是本进程既抓不了屏、
        也注入不了输入 —— 该查的方向完全相反。这一条守的就是早退路径别把
        capture_error 吞掉。
        """

        def broken_capture(_monitor):
            raise OSError("screen grab failed")

        def broken_perform(_action, _params):
            raise RuntimeError("SendInput 全部失败，last_error=5")

        out = A.act(
            {"text": "确定", "verify": "screen"},
            probe=make_probe({"确定": hit()}),
            capture=broken_capture,
            perform=broken_perform,
            compare=lambda *_a: 9.0,
            sleep=lambda _s: None,
            clock=FakeClock(),
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "act")
        self.assertIn("SendInput", out["error"])
        self.assertIn(
            "screen grab failed", out.get("verify_unavailable", ""),
            "动作早退时也必须带上抓屏失败的原因",
        )

    def test_verify_none_has_no_capture_error_key(self):
        out = A.act(
            {"text": "确定", "verify": None},
            probe=make_probe({"确定": hit()}),
            capture=lambda _m: (_ for _ in ()).throw(RuntimeError("不该被调用")),
            perform=lambda _a, _p: {"sent": 1},
            compare=lambda *_a: 9.0,
            sleep=lambda _s: None,
            clock=FakeClock(),
        )
        self.assertTrue(out["ok"])
        self.assertNotIn("verify_unavailable", out, "不验证时不该出现这个键")


class RunStepsTests(unittest.TestCase):
    def _run(self, steps, *, probe=None, perform=None, compare=None, clock=True):
        clock = FakeClock()
        touched = []
        res = A.run_steps(
            steps,
            probe=probe or make_probe({"确定": hit()}),
            capture=lambda _m: "IMG",
            perform=perform or (lambda a, p: (touched.append(a), {"sent": 1})[1]),
            compare=compare or (lambda *_a: 9.0),
            sleep=clock.advance,
            clock=clock,
        )
        return res, touched

    def test_all_steps_pass(self):
        res, touched = self._run([{"text": "确定"}, {"text": "确定"}])
        self.assertTrue(res["ok"])
        self.assertEqual(res["completed"], 2)
        self.assertEqual(touched, ["click", "click"])

    def test_stops_at_first_failure(self):
        res, touched = self._run([{"text": "确定"}, {"text": "不存在"}, {"text": "确定"}])
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed_index"], 1)
        self.assertEqual(res["completed"], 1)
        self.assertEqual(len(touched), 1, "失败之后不该再动手")

    def test_on_fail_continue_keeps_going(self):
        res, touched = self._run([
            {"text": "不存在", "on_fail": "continue"},
            {"text": "确定"},
        ])
        self.assertTrue(res["ok"])
        self.assertEqual(len(touched), 1)

    def test_wait_before_uses_text_not_sleep(self):
        clock = FakeClock()
        res = A.run_steps(
            [{"wait_before": "界面出来了", "text": "确定"}],
            probe=make_probe({"界面出来了": hit(), "确定": hit()}),
            capture=lambda _m: "IMG",
            perform=lambda _a, _p: {"sent": 1},
            compare=lambda *_a: 9.0,
            sleep=clock.advance, clock=clock,
        )
        self.assertTrue(res["ok"])
        self.assertTrue(res["steps"][0]["wait_before"]["found"])

    def test_wait_before_timeout_is_failure(self):
        clock = FakeClock()
        res = A.run_steps(
            [{"wait_before": {"text": "永远不来", "timeout_ms": 300}, "text": "确定"}],
            probe=make_probe({"确定": hit()}),
            capture=lambda _m: "IMG",
            perform=lambda _a, _p: {"sent": 1},
            compare=lambda *_a: 9.0,
            sleep=clock.advance, clock=clock,
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "wait_before")

    def test_pure_wait_step(self):
        clock = FakeClock()
        res = A.run_steps(
            [{"action": "wait", "text": "加载完", "timeout_ms": 800}],
            probe=make_probe({"加载完": hit()}),
            capture=lambda _m: "IMG",
            perform=lambda _a, _p: {"sent": 1},
            compare=lambda *_a: 9.0,
            sleep=clock.advance, clock=clock,
        )
        self.assertTrue(res["ok"])
        self.assertTrue(res["steps"][0]["wait"]["found"])

    def test_pure_wait_without_text_raises(self):
        with self.assertRaises(A.ActError):
            A.run_steps([{"action": "wait"}], probe=make_probe(),
                        capture=lambda _m: "IMG",
                        perform=lambda _a, _p: {"sent": 1},
                        compare=lambda *_a: 9.0,
                        sleep=lambda _s: None, clock=FakeClock())

    def test_empty_steps_rejected(self):
        with self.assertRaises(A.ActError):
            A.run_steps([], probe=make_probe(), capture=lambda _m: "IMG",
                        perform=lambda _a, _p: {}, compare=lambda *_a: 0.0,
                        sleep=lambda _s: None, clock=FakeClock())

    def test_too_many_steps_rejected(self):
        with self.assertRaises(A.ActError) as ctx:
            A.run_steps([{"text": "x"}] * 41, probe=make_probe(), capture=lambda _m: "IMG",
                        perform=lambda _a, _p: {}, compare=lambda *_a: 0.0,
                        sleep=lambda _s: None, clock=FakeClock())
        self.assertIn("40", str(ctx.exception))

    def test_non_dict_step_rejected(self):
        with self.assertRaises(A.ActError):
            A.run_steps(["click"], probe=make_probe(), capture=lambda _m: "IMG",
                        perform=lambda _a, _p: {}, compare=lambda *_a: 0.0,
                        sleep=lambda _s: None, clock=FakeClock())

    def test_each_step_keeps_its_own_result(self):
        res, _t = self._run([{"text": "确定"}, {"text": "不存在", "on_fail": "continue"}])
        self.assertEqual(len(res["steps"]), 2)
        self.assertTrue(res["steps"][0]["ok"])
        self.assertFalse(res["steps"][1]["ok"])
        self.assertEqual(res["steps"][1]["stage"], "find")


class ActionsTests(unittest.TestCase):
    """actions.py 的参数校验：不碰 Win32，纯查错路径。"""

    def test_unknown_button_rejected(self):
        from ui_locate import actions

        with self.assertRaises(actions.InputError):
            actions.click(0, 0, button="thumb")

    def test_unknown_delivery_rejected(self):
        from ui_locate import actions

        with self.assertRaises(actions.InputError):
            actions.click(0, 0, delivery="telepathy")

    def test_empty_key_rejected(self):
        from ui_locate import actions

        with self.assertRaises(actions.InputError):
            actions.press("")

    def test_empty_combo_rejected(self):
        from ui_locate import actions

        with self.assertRaises(actions.InputError):
            actions.hotkey("++")

    def test_hex_key_code_accepted(self):
        from ui_locate import actions

        code, _ext = actions._parse_key("0x1B")
        self.assertEqual(code, 0x1B)

    def test_key_alias_resolved(self):
        from ui_locate import actions

        code, _ext = actions._parse_key("esc")
        self.assertEqual(code, 0x1B)

    def test_extended_key_flagged(self):
        from ui_locate import actions

        _code, ext = actions._parse_key("delete")
        self.assertTrue(ext, "delete 是扩展键，不标会被当成小键盘上的小数点")

    def test_combo_split(self):
        from ui_locate import actions

        self.assertEqual(actions._parse_combo("ctrl+shift+s"), ["ctrl", "shift", "s"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
