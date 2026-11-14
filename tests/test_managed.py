"""托管模式（UI_LOCATE_CUA_MANAGED=1）：HTTP 与 MCP 两条入口都必须封死桌面能力。

为什么这些测试要真的起一个 HTTP 服务：托管判定是**进程级**的，而这条边界
最容易被做假的地方就是"只在 MCP 那侧设了环境变量，HTTP 却是上一次留下的
legacy 进程"。那种情况下工具看起来被隐藏了，直接 POST 却照样能抓屏。
所以这里起一个真实服务（端口 0，临时端口，绝不碰 7788），把屏幕/窗口/抓屏
三个模块换成**会记录调用的替身**：只要有一条守卫走漏，替身就会被叫到，
测试当场失败——而不是等某天有人在托管环境里按了一下鼠标才发现。

替身不是 mock 掉被测对象：被测对象是"路由分派前的守卫"，它属于 server.py，
真实存在、真的在跑。
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import image_api, managed  # noqa: E402

import ctypes  # noqa: E402

if not hasattr(ctypes, "WINFUNCTYPE"):     # screen.py 顶层就要它
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE  # type: ignore[attr-defined]

try:
    from ui_locate import server
except Exception as error:  # pragma: no cover - 缺少模型/图像依赖时
    server = None
    _WHY = str(error)

try:
    from ui_locate import mcp_server
except Exception as error:  # pragma: no cover
    mcp_server = None
    _WHY_MCP = str(error)


ENV = {managed.MANAGED_ENV: "1"}


class Spy:
    """记录每一次调用的替身。任何属性访问都返回一个记录用的函数。"""

    def __init__(self, value=None):
        self.calls: list[str] = []
        self.value = {} if value is None else value

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            return self.value
        return call


class FakeDet:
    active = "fake"


class FakeEngine:
    ready = True
    provider = "fake"
    use_ocr = True
    warmup_ms = 1.0
    det = FakeDet()

    def __init__(self, elements=None):
        self.elements = list(elements or [])
        self.calls: list[dict] = []

    def run(self, bgr, **kwargs):
        self.calls.append({"shape": tuple(bgr.shape), **kwargs})
        return {"elements": [dict(e) for e in self.elements]}


def tiny_png_b64() -> tuple[str, int, int]:
    import base64

    import cv2
    import numpy as np

    img = np.zeros((40, 80, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return base64.b64encode(buf.tobytes()).decode("ascii"), 80, 40


@unittest.skipIf(server is None, "server 依赖不全")
class ServerCase(unittest.TestCase):
    """起一个临时端口上的真服务；三个桌面模块全部换成替身。"""

    def setUp(self):
        self.engine = FakeEngine([{
            "x1": 4, "y1": 6, "x2": 44, "y2": 26, "conf": 0.7,
            "cx": 24, "cy": 16, "w": 40, "h": 20, "text": "保存", "text_score": 0.8,
        }])
        self._saved = (server.ENGINE, server.screen, server.window_mod,
                       server.capture_mod)
        self.screen = Spy()
        self.window = Spy()
        self.capture = Spy()
        server.ENGINE = self.engine
        server.screen = self.screen
        server.window_mod = self.window
        server.capture_mod = self.capture

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        (server.ENGINE, server.screen, server.window_mod,
         server.capture_mod) = self._saved

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            data = None
            hdrs = dict(headers or {})
            if body is not None:
                if isinstance(body, (dict, list)):
                    data = json.dumps(body).encode("utf-8")
                    hdrs.setdefault("Content-Type", "application/json")
                elif isinstance(body, str):
                    data = body.encode("utf-8")
                else:
                    data = body
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                payload = raw
            return resp.status, payload
        finally:
            conn.close()


class ManagedModuleTests(unittest.TestCase):
    def test_enabled_reads_env_every_time(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(managed.MANAGED_ENV, None)
            self.assertFalse(managed.enabled())
        for value in ("1", "true", "TRUE", "yes", "on", " 1 "):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {managed.MANAGED_ENV: value}):
                    self.assertTrue(managed.enabled())
        for value in ("0", "false", "no", "off", ""):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {managed.MANAGED_ENV: value}):
                    self.assertFalse(managed.enabled())

    def test_refusal_shape(self):
        with mock.patch.dict(os.environ, ENV):
            body = managed.refusal("/locate", method="POST")
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "CUA_MANAGED")
        self.assertTrue(body["managed"])
        self.assertEqual(body["contractVersion"], managed.CONTRACT_VERSION)
        self.assertEqual(body["path"], "/locate")
        self.assertEqual(body["method"], "POST")
        # hint 要指向真正能用的入口，且不能把被拒的工具名再念一遍
        hint = body["hint"]
        self.assertIn("standalone", hint)
        self.assertIn('delivery="background"', hint)
        self.assertIn("/compare-images", hint)
        for refused in ("locate_text", "detect_elements", "ui_locate_marks",
                        "ui_locate_windows", "ui_locate_watch"):
            self.assertNotIn(refused, hint)

    def test_blocked_covers_every_desktop_route(self):
        with mock.patch.dict(os.environ, ENV):
            for path in managed.DESKTOP_GET_ROUTES:
                self.assertTrue(managed.blocked(path, "GET"), path)
            for path in managed.DESKTOP_POST_ROUTES:
                self.assertTrue(managed.blocked(path, "POST"), path)
            # 纯图片与健康检查必须放行
            self.assertFalse(managed.blocked("/locate-image", "POST"))
            self.assertFalse(managed.blocked("/health", "GET"))
            # 未知路径不能被报成权限问题：那会让排查方向完全跑偏
            self.assertFalse(managed.blocked("/nope", "GET"))

    def test_standalone_blocks_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(managed.MANAGED_ENV, None)
            for path in managed.DESKTOP_GET_ROUTES | managed.DESKTOP_POST_ROUTES:
                self.assertFalse(managed.blocked(path, "GET"))
                self.assertFalse(managed.blocked(path, "POST"))

    def test_health_fields(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(managed.MANAGED_ENV, None)
            free = managed.health_fields()
        self.assertEqual(free["contractVersion"], 1)
        self.assertFalse(free["managed"])
        self.assertEqual(free["capabilities"],
                         {"image": True, "compare": True, "desktop": True})
        with mock.patch.dict(os.environ, ENV):
            tied = managed.health_fields()
        self.assertTrue(tied["managed"])
        self.assertEqual(tied["capabilities"],
                         {"image": True, "compare": True, "desktop": False})


class ManagedHttpTests(ServerCase):
    def test_health_does_not_touch_the_desktop(self):
        with mock.patch.dict(os.environ, ENV):
            status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["managed"])
        self.assertEqual(body["contractVersion"], 1)
        self.assertEqual(body["capabilities"],
                         {"image": True, "compare": True, "desktop": False})
        # 关键：连"有几块屏"都不说 —— 那本身就是一次桌面发现
        self.assertNotIn("screen", body)
        self.assertNotIn("integrity", body)
        self.assertEqual(self.screen.calls, [])
        self.assertEqual(self.window.calls, [])

    def test_every_desktop_route_is_refused_with_structured_code(self):
        cases = [("GET", p) for p in sorted(managed.DESKTOP_GET_ROUTES)]
        cases += [("POST", p) for p in sorted(managed.DESKTOP_POST_ROUTES)]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                with mock.patch.dict(os.environ, ENV):
                    status, body = self.request(method, path) if method == "GET" \
                        else self.request(method, path, body={})
                self.assertEqual(status, 403, path)
                self.assertEqual(body["code"], "CUA_MANAGED")
                self.assertFalse(body["ok"])
                self.assertEqual(body["path"], path)
                self.assertEqual(body["method"], method)
        # 一条都不许落到桌面替身上
        self.assertEqual(self.screen.calls, [])
        self.assertEqual(self.window.calls, [])
        self.assertEqual(self.capture.calls, [])

    def test_refusal_happens_before_body_parsing(self):
        """坏 JSON 也必须先被托管守卫挡住，而不是先报 400。"""
        with mock.patch.dict(os.environ, ENV):
            status, body = self.request("POST", "/act", body="{这不是 JSON")
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "CUA_MANAGED")

    def test_locate_image_is_allowed_and_uses_image_origin(self):
        b64, width, height = tiny_png_b64()
        with mock.patch.dict(os.environ, ENV):
            status, body = self.request("POST", "/locate-image", body={
                "image_b64": b64, "width": width, "height": height,
                "text": "保存", "mode": "locate",
            })
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["coordinateSpace"], "image-pixels")
        self.assertEqual(body["image"], {"width": width, "height": height})
        self.assertEqual(body["matches"][0]["text"], "保存")
        self.assertEqual(body["matches"][0]["box"],
                         {"x": 4, "y": 6, "width": 40, "height": 20})
        self.assertEqual(body["matches"][0]["center"], {"x": 24, "y": 16})
        self.assertEqual(self.engine.calls[0]["origin"], (0, 0))
        self.assertEqual(self.engine.calls[0]["shape"], (height, width, 3))
        # 纯图片路径不得碰抓屏
        self.assertEqual(self.capture.calls, [])
        self.assertEqual(self.screen.calls, [])

    def test_locate_image_rejects_unknown_fields_in_managed_too(self):
        b64, width, height = tiny_png_b64()
        with mock.patch.dict(os.environ, ENV):
            status, body = self.request("POST", "/locate-image", body={
                "image_b64": b64, "width": width, "height": height,
                "text": "保存", "path": r"C:\Windows\win.ini",
            })
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "unsupported_field")
        self.assertEqual(body["field"], "path")
        self.assertEqual(self.engine.calls, [])

    def test_oversize_body_is_refused_without_running_engine(self):
        b64, width, height = tiny_png_b64()
        body = {"image_b64": b64, "width": width, "height": height, "text": "保存"}
        limit = 64
        self.assertGreater(len(json.dumps(body)), limit)   # 前提：这条请求真的超限
        with mock.patch.object(image_api, "MAX_BODY_BYTES", limit), \
                mock.patch.dict(os.environ, ENV):
            status, payload = self.request("POST", "/locate-image", body=body)
        self.assertEqual(status, 413)
        self.assertEqual(payload["code"], "too_large")
        self.assertEqual(self.engine.calls, [])

    def test_unknown_path_still_404(self):
        with mock.patch.dict(os.environ, ENV):
            status, body = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertNotEqual(body.get("code"), "CUA_MANAGED")

    def test_gate_follows_the_server_process_env_not_a_startup_snapshot(self):
        """托管开关是每次请求读一遍的：服务进程自己的环境才是防线。"""
        with mock.patch.dict(os.environ, ENV):
            status, _ = self.request("GET", "/windows")
        self.assertEqual(status, 403)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(managed.MANAGED_ENV, None)
            status, body = self.request("GET", "/windows")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], 0)


class StandaloneHttpTests(ServerCase):
    def test_health_keeps_screen_info_when_not_managed(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(managed.MANAGED_ENV, None)
            status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertFalse(body["managed"])
        self.assertEqual(body["capabilities"],
                         {"image": True, "compare": True, "desktop": True})
        self.assertIn("screen", body)
        self.assertIn("describe", self.screen.calls)

    def test_desktop_routes_still_work_when_not_managed(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(managed.MANAGED_ENV, None)
            status, body = self.request("GET", "/monitors")
        self.assertEqual(status, 200)
        self.assertIn("describe", self.screen.calls)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(managed.MANAGED_ENV, None)
            status, body = self.request("GET", "/windows")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_locate_image_works_standalone_too(self):
        b64, width, height = tiny_png_b64()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(managed.MANAGED_ENV, None)
            status, body = self.request("POST", "/locate-image", body={
                "image_b64": b64, "width": width, "height": height,
                "text": "保存",
            })
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])


@unittest.skipIf(mcp_server is None, "mcp_server 依赖不全")
class McpContractTests(unittest.TestCase):
    def test_standalone_passes_any_health_through(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(managed.MANAGED_ENV, None)
            h = {"ready": True}
            self.assertIs(mcp_server._check_contract(h), h)

    def test_managed_requires_managed_server(self):
        with mock.patch.dict(os.environ, ENV):
            with self.assertRaises(RuntimeError) as ctx:
                mcp_server._check_contract({"ready": True, "managed": False,
                                            "contractVersion": 1})
        message = str(ctx.exception)
        self.assertIn("legacy", message)
        self.assertIn("重启", message)

    def test_managed_requires_matching_contract(self):
        with mock.patch.dict(os.environ, ENV):
            with self.assertRaises(RuntimeError) as ctx:
                mcp_server._check_contract({"ready": True, "managed": True,
                                            "contractVersion": 0})
        self.assertIn("contractVersion", str(ctx.exception))

    def test_managed_accepts_matching_handshake(self):
        with mock.patch.dict(os.environ, ENV):
            h = {"ready": True, "managed": True, "contractVersion": 1}
            self.assertIs(mcp_server._check_contract(h), h)

    def test_desktop_tools_refused_with_json_code_in_managed(self):
        tools = ["locate_text", "detect_elements", "list_monitors", "find_all",
                 "ui_locate_marks", "ui_locate_act", "ui_locate_windows",
                 "ui_locate_diff", "ui_locate_watch", "ui_locate_steps",
                 "ui_locate_frames", "ui_locate_frame"]
        with mock.patch.dict(os.environ, ENV):
            for name in tools:
                with self.subTest(tool=name):
                    raw = mcp_server._managed_refusal(name)
                    self.assertIsNotNone(raw, name)
                    body = json.loads(raw)
                    self.assertEqual(body["code"], "CUA_MANAGED")
                    self.assertEqual(body["method"], "MCP")

    def test_standalone_tools_are_not_refused(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(managed.MANAGED_ENV, None)
            self.assertIsNone(mcp_server._managed_refusal("locate_text"))

    def test_image_tool_posts_the_contract(self):
        """纯图片工具只发契约字段，且 400/413 的结构化信息要原样透出。"""
        captured = {}

        def fake_post_any(path, payload):
            captured["path"] = path
            captured["payload"] = payload
            return {"ok": True, "contractVersion": 1,
                    "coordinateSpace": "image-pixels",
                    "image": {"width": 10, "height": 10}, "matches": []}

        with mock.patch.object(mcp_server, "_post_any", fake_post_any):
            out = json.loads(mcp_server.ui_locate_image("QUJD", 10, 10, "保存"))
        self.assertEqual(captured["path"], "/locate-image")
        self.assertEqual(captured["payload"], {
            "image_b64": "QUJD", "width": 10, "height": 10,
            "text": "保存", "mode": "locate", "conf": 0.3, "tiled": False,
        })
        self.assertNotIn("limit", captured["payload"])
        self.assertTrue(out["ok"])

    def test_image_tool_passes_limit_when_positive(self):
        captured = {}

        def fake_post_any(path, payload):
            captured["payload"] = payload
            return {"ok": True}

        with mock.patch.object(mcp_server, "_post_any", fake_post_any):
            mcp_server.ui_locate_image("QUJD", 10, 10, "", mode="detect", limit=9)
        self.assertEqual(captured["payload"]["limit"], 9)
        self.assertEqual(captured["payload"]["mode"], "detect")


if __name__ == "__main__":
    unittest.main()
