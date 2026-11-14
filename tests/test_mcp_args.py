"""MCP 工具入参校验：字符串形式的布尔 / 数字不能被猜成「真」。

真机现场：宿主给 `ui_locate_marks` 传了 `{"text":"false","cells":"false"}`，
工具不报错，但两个开关都成了**开**——`bool("false")` 是 True。经 JSON-RPC
中转的客户端把布尔序列化成字符串是常见行为，所以 MCP 层必须显式解析。

这些用例不需要服务、不需要模型，只验证「参数怎么被翻译成 payload」。
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

try:
    from ui_locate import mcp_server
except Exception as error:  # mcp / httpx 不在时跳过
    mcp_server = None
    _why = str(error)


def _capture(tool, *args, **kwargs):
    """调用工具并把发给 HTTP 服务的 payload 捞出来（不打网络）。"""
    seen: dict = {}

    def fake_post(path, payload):
        seen["path"] = path
        seen["payload"] = payload
        return {"ok": False, "error": "stub"}

    # 三种投递口都堵住：_post_any（多数工具）、_post（locate/detect 等老路径）、
    # _http（直接 GET 的工具）。一个真实请求都不该发出去。
    with mock.patch.object(mcp_server, "_managed_refusal", lambda name: None), \
            mock.patch.object(mcp_server, "_post_any", fake_post), \
            mock.patch.object(mcp_server, "_post", fake_post), \
            mock.patch.object(mcp_server, "_http", side_effect=AssertionError("测试里不该发 HTTP 请求")):
        tool(*args, **kwargs)
    return seen


@unittest.skipIf(mcp_server is None, "mcp / httpx 依赖不全")
class BoolParsingTests(unittest.TestCase):
    def test_false_words(self):
        for raw in ("false", "0", "no", "off", " FALSE ", "False"):
            with self.subTest(raw=raw):
                self.assertIs(mcp_server._as_bool(raw, "text"), False)

    def test_true_words(self):
        for raw in ("true", "1", "yes", "on", " TRUE ", "On"):
            with self.subTest(raw=raw):
                self.assertIs(mcp_server._as_bool(raw, "text"), True)

    def test_real_bools_pass_through(self):
        self.assertIs(mcp_server._as_bool(True, "text"), True)
        self.assertIs(mcp_server._as_bool(False, "text"), False)

    def test_numeric_zero_and_one(self):
        self.assertIs(mcp_server._as_bool(1, "text"), True)
        self.assertIs(mcp_server._as_bool(0, "text"), False)

    def test_unknown_words_raise_with_field(self):
        for raw in ("maybe", "", "  ", 2, None, [], {}):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as ctx:
                    mcp_server._as_bool(raw, "cells")
                self.assertIn("cells", str(ctx.exception))


@unittest.skipIf(mcp_server is None, "mcp / httpx 依赖不全")
class NumberParsingTests(unittest.TestCase):
    def test_int_accepts_numeric_strings(self):
        self.assertEqual(mcp_server._as_int("5", "max_marks"), 5)
        self.assertEqual(mcp_server._as_int(" 12 ", "max_marks"), 12)
        self.assertEqual(mcp_server._as_int(5.0, "max_marks"), 5)

    def test_int_rejects_bad_values(self):
        for raw in ("abc", "", "5.5", 5.5, True, None):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as ctx:
                    mcp_server._as_int(raw, "max_marks")
                self.assertIn("max_marks", str(ctx.exception))

    def test_float_accepts_numeric_strings(self):
        self.assertEqual(mcp_server._as_float("0.25", "conf"), 0.25)
        self.assertEqual(mcp_server._as_float(2, "conf"), 2.0)

    def test_float_rejects_bad_values(self):
        for raw in ("abc", "", True, None):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as ctx:
                    mcp_server._as_float(raw, "conf")
                self.assertIn("conf", str(ctx.exception))


@unittest.skipIf(mcp_server is None, "mcp / httpx 依赖不全")
class MarksArgumentTests(unittest.TestCase):
    """`ui_locate_marks` 的开关：字符串 "false" 必须真的关掉。"""

    def test_string_false_turns_switches_off(self):
        seen = _capture(mcp_server.ui_locate_marks, text="false", cells="false")
        payload = seen["payload"]
        self.assertIs(payload["text"], False)
        self.assertIs(payload["cells"], False)

    def test_string_false_incremental_off(self):
        seen = _capture(mcp_server.ui_locate_marks, incremental="no")
        self.assertIs(seen["payload"]["incremental"], False)

    def test_string_zero_words_off(self):
        seen = _capture(mcp_server.ui_locate_marks, text="0", cells="off", incremental=" FALSE ")
        payload = seen["payload"]
        self.assertIs(payload["text"], False)
        self.assertIs(payload["cells"], False)
        self.assertIs(payload["incremental"], False)

    def test_defaults_stay_on(self):
        seen = _capture(mcp_server.ui_locate_marks)
        payload = seen["payload"]
        self.assertIs(payload["text"], True)
        self.assertNotIn("cells", payload)          # 默认 True 不下发
        self.assertNotIn("incremental", payload)

    def test_bad_bool_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            _capture(mcp_server.ui_locate_marks, text="maybe")
        self.assertIn("text", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            _capture(mcp_server.ui_locate_marks, cells="maybe")
        self.assertIn("cells", str(ctx.exception))

    def test_numeric_strings_are_parsed(self):
        seen = _capture(mcp_server.ui_locate_marks, max_marks="5", max_side="800", conf="0.3", grid="4")
        payload = seen["payload"]
        self.assertEqual(payload["max_marks"], 5)
        self.assertEqual(payload["max_side"], 800)
        self.assertEqual(payload["conf"], 0.3)
        self.assertEqual(payload["grid"], 4)

    def test_bad_numbers_are_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            _capture(mcp_server.ui_locate_marks, max_marks="many")
        self.assertIn("max_marks", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            _capture(mcp_server.ui_locate_marks, conf="high")
        self.assertIn("conf", str(ctx.exception))


@unittest.skipIf(mcp_server is None, "mcp / httpx 依赖不全")
class OtherToolArgumentTests(unittest.TestCase):
    """同一条坑在其它工具里也要收口。"""

    def test_detect_elements_tiled_and_smart(self):
        seen = _capture(mcp_server.detect_elements, tiled="false", smart="0")
        payload = seen["payload"]
        self.assertIs(payload["tiled"], False)
        self.assertIs(payload["smart"], False)

    def test_locate_text_smart(self):
        seen = _capture(mcp_server.locate_text, "保存", smart="false")
        self.assertIs(seen["payload"]["smart"], False)

    def test_diff_reset_false_sends_nothing(self):
        seen = _capture(mcp_server.ui_locate_diff, reset="false")
        self.assertNotIn("reset", seen["payload"])
        seen = _capture(mcp_server.ui_locate_diff, reset="true")
        self.assertIs(seen["payload"]["reset"], True)

    def test_image_tiled_false(self):
        seen = _capture(
            mcp_server.ui_locate_image, "AAAA", 10, 10, text="保存", tiled="false"
        )
        payload = seen["payload"]
        self.assertIs(payload["tiled"], False)
        self.assertEqual(payload["width"], 10)
        self.assertEqual(payload["height"], 10)

    def test_watch_numbers_from_strings(self):
        seen = _capture(mcp_server.ui_locate_watch, duration_ms="1500", fps="3", max_keyframes="4")
        payload = seen["payload"]
        self.assertEqual(payload["duration_ms"], 1500)
        self.assertEqual(payload["fps"], 3.0)
        self.assertEqual(payload["max_keyframes"], 4)

    def test_act_numbers_from_strings(self):
        seen = _capture(mcp_server.ui_locate_act, text="保存", count="2", delta="-5")
        payload = seen["payload"]
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["delta"], -5)

    def test_act_rejects_bad_count(self):
        with self.assertRaises(ValueError) as ctx:
            _capture(mcp_server.ui_locate_act, text="保存", count="twice")
        self.assertIn("count", str(ctx.exception))

    def test_act_timeout_used_for_text_verify(self):
        seen = _capture(
            mcp_server.ui_locate_act, text="保存", verify="text", verify_text="完成",
            verify_timeout_ms="2500",
        )
        self.assertEqual(seen["payload"]["verify"]["timeout_ms"], 2500)

    def test_frames_limit_is_prepared_before_http(self):
        # /frames 走 GET，不走 _post；坏值必须在发请求前就变成结构化错误。
        with mock.patch.object(mcp_server, "_managed_refusal", lambda name: None), \
                mock.patch.object(mcp_server, "_http", side_effect=AssertionError("不该发请求")):
            out = json.loads(mcp_server.ui_locate_frames(limit="many"))
        self.assertIs(out["ok"], False)
        self.assertIn("limit", out["error"])


if __name__ == "__main__":
    unittest.main()
