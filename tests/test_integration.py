"""进程内集成测试：真 Handler + 真 HTTP，只把抓屏与输入换成桩。

为什么要这一层：`tests/test_frames.py` 只测 `FrameStore` 的纯逻辑，
`tests/test_act.py` 只测编排；它们都证明不了「走完一趟 HTTP，帧真的存进去了、
`/frame` 真的回一张 PNG」。而真机 e2e 在这台受限进程里跑不动 ——
`screen.grab` 抛 `OSError: screen grab failed`、`SendInput` 报
`last_error=5`（ERROR_ACCESS_DENIED），于是 before 图根本进不了 store。

所以这里把两条外部能力换成桩，其余全部走真路：
  · 真 `ThreadingHTTPServer` + 真 `Handler` + 真 urllib 请求
  · 真 `Engine`（检测/OCR 用不上，桩不触发它们）
  · 真 `FrameStore`、真 `act.act` / `act.run_steps`、真 PNG 编码

这样既验证了接线（路由、参数、响应头、drain），又不依赖本进程有没有桌面权限。
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np

from ui_locate import screen
from ui_locate import server as S


def make_frame(w: int = 120, h: int = 90, value: int = 40) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


class _FakeEngine:
    """只提供 Handler 用到的那几个成员，不加载模型。"""

    model = "fake.onnx"
    provider = "cpu"
    warmup_ms = 1.0

    def __init__(self) -> None:
        from ui_locate import frames as frames_mod

        self.frames = frames_mod.FrameStore()
        self.lock = threading.Lock()
        self.ocr = None

    def run(self, *_a, **_kw):  # pragma: no cover - 本文件不该走到检测
        raise AssertionError("集成测试不该跑真实检测")

    def ready(self) -> dict:
        return {"ok": True, "ready": True, "provider": "cpu", "active": "CPUExecutionProvider"}


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = _FakeEngine()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        # 不走环境代理：本机 HTTP_PROXY 会把 127.0.0.1 的请求也送过去
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self) -> None:
        S.ENGINE = self.engine
        self.engine.frames.clear()
        # 抓屏与输入都换成桩：一个稳定的灰画面 + 什么都不做的动作
        self._orig = {
            "grab": screen.grab,
            "click": S.actions.click,
            "move": S.actions.move,
            "drag": S.actions.drag,
            "scroll": S.actions.scroll,
            "type_text": S.actions.type_text,
            "press": S.actions.press,
            "hotkey": S.actions.hotkey,
        }
        self._n = 0

        def fake_grab(monitor=None):
            self._n += 1
            # 每次抓屏颜色微变，让 verify 的像素比对能看出「变了」
            return make_frame(value=40 + self._n * 20), (0, 0)

        screen.grab = fake_grab
        S.actions.click = lambda *a, **k: {"sent": 3, "cursor_moved": True}
        S.actions.move = lambda *a, **k: {"sent": 1, "cursor_moved": True}
        S.actions.drag = lambda *a, **k: {"sent": 5, "cursor_moved": True}
        S.actions.scroll = lambda *a, **k: {"sent": 3, "cursor_moved": None}
        S.actions.type_text = lambda *a, **k: {"sent": 4, "cursor_moved": None}
        S.actions.press = lambda *a, **k: {"sent": 2, "cursor_moved": None}
        S.actions.hotkey = lambda *a, **k: {"sent": 4, "cursor_moved": None}

    def tearDown(self) -> None:
        screen.grab = self._orig["grab"]
        S.actions.click = self._orig["click"]
        S.actions.move = self._orig["move"]
        S.actions.drag = self._orig["drag"]
        S.actions.scroll = self._orig["scroll"]
        S.actions.type_text = self._orig["type_text"]
        S.actions.press = self._orig["press"]
        S.actions.hotkey = self._orig["hotkey"]

    # -- helpers ---------------------------------------------------------

    def call(self, path, payload=None, method="GET", raw=False):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=20) as r:
                body = r.read()
                if raw:
                    return r.status, dict(r.headers), body
                return r.status, dict(r.headers), json.loads(body or b"{}")
        except urllib.error.HTTPError as e:
            body = e.read()
            if raw:
                return e.code, dict(e.headers), body
            try:
                return e.code, dict(e.headers), json.loads(body or b"{}")
            except ValueError:
                return e.code, dict(e.headers), {"raw": body[:200]}

    # -- tests -----------------------------------------------------------

    def test_act_records_frames_that_frame_endpoint_can_serve(self):
        code, _h, acted = self.call(
            "/act", {"action": "hover", "x": 10, "y": 10, "verify": "screen"}, method="POST")
        self.assertEqual(code, 200)
        self.assertTrue(acted["ok"], acted)
        turn = acted["turn"]
        self.assertIsInstance(turn, int)
        self.assertTrue(acted["verified"], "画面确实变了，应该判 verified")

        code, _h, body = self.call("/frames")
        self.assertEqual(code, 200)
        mine = next((t for t in body["turns"] if t["turn"] == turn), None)
        self.assertIsNotNone(mine, "act 的回合必须出现在 /frames 里")
        labels = [f["label"] for f in mine["frames"]]
        self.assertEqual(labels[:2], ["before", "after"], labels)

        code, headers, blob = self.call(f"/frame?turn={turn}&frame=0", raw=True)
        self.assertEqual(code, 200)
        self.assertTrue(headers.get("Content-Type", "").startswith("image/"), headers)
        self.assertEqual(blob[:8], b"\x89PNG\r\n\x1a\n", "必须是真的 PNG")
        self.assertEqual(headers.get("X-Frame-Turn"), str(turn))
        self.assertEqual(headers.get("X-Frame-Index"), "0")

    def test_frame_crop_uses_screen_coordinates(self):
        code, _h, acted = self.call("/act", {"action": "move", "x": 30, "y": 20}, method="POST")
        self.assertEqual(code, 200)
        turn = acted["turn"]

        _c, full_h, full = self.call(f"/frame?turn={turn}&frame=0", raw=True)
        code, crop_h, crop = self.call(
            f"/frame?turn={turn}&frame=0&box=20,10,60,50", raw=True)
        self.assertEqual(code, 200)
        self.assertEqual(crop_h.get("X-Frame-Box"), "20,10,60,50", "响应头回绝对坐标")
        self.assertLess(len(crop), len(full), "裁片应该比整帧小")

    def test_frame_without_turn_returns_latest(self):
        self.call("/act", {"action": "move", "x": 1, "y": 1}, method="POST")
        code, headers, blob = self.call("/frame", raw=True)
        self.assertEqual(code, 200)
        self.assertEqual(blob[:8], b"\x89PNG\r\n\x1a\n")
        self.assertTrue(headers.get("X-Frame-Turn"))

    def test_unknown_turn_is_404_and_lists_available(self):
        self.call("/act", {"action": "move", "x": 1, "y": 1}, method="POST")
        code, _h, body = self.call("/frame?turn=99999&frame=0")
        self.assertEqual(code, 404)
        self.assertIn("available", body, "404 要告诉调用方还有哪些回合可取")

    def test_bad_parameters_are_400(self):
        code, _h, body = self.call("/frame?turn=abc")
        self.assertEqual(code, 400)
        self.assertIn("error", body)

        code, _h, body = self.call("/frame?turn=1&frame=0&box=1,2,3")
        self.assertEqual(code, 400)

        code, _h, body = self.call("/frame?turn=1&frame=0&box=a,b,c,d")
        self.assertEqual(code, 400)

    def test_steps_runs_multiple_actions_in_one_turn(self):
        code, _h, out = self.call("/steps", {"steps": [
            {"action": "move", "x": 5, "y": 5},
            {"action": "hover", "x": 40, "y": 40, "verify": "none"},
        ]}, method="POST")
        self.assertEqual(code, 200, out)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["completed"], 2)
        turn = out["turn"]
        mine = next(t for t in self.call("/frames")[2]["turns"] if t["turn"] == turn)
        # 两步都带 verify:"none"，本来一帧都不会抓；靠回合结束的补抓兜底，
        # 这一回合仍然留得下东西可回看。
        self.assertGreaterEqual(
            len(mine["frames"]), 1,
            "没有验证的动作也要留下可回看的帧")

    def test_step_without_text_is_a_400(self):
        """`{"action":"wait"}` 缺 text 是调用方写错了，该 400 而不是 500。"""
        code, _h, body = self.call("/steps", {"steps": [
            {"action": "move", "x": 1, "y": 1},
            {"action": "wait"},
        ]}, method="POST")
        self.assertEqual(code, 400)
        self.assertIn("error", body)

    def test_keepalive_survives_image_then_json(self):
        """拿完 PNG 之后同一条连接上的 JSON 请求不能被污染。

        图片响应不走 `_send`，所以以前这里有过「残留字节被当成下一条请求行」
        的隐患（实测形态 `501 Unsupported method ('{}POST')`）。
        """
        self.call("/act", {"action": "move", "x": 1, "y": 1}, method="POST")
        # 同一个 opener 会复用连接
        for _ in range(3):
            code, _h, blob = self.call("/frame", raw=True)
            self.assertEqual(code, 200)
            code, _h, body = self.call("/frames")
            self.assertEqual(code, 200, "取完图之后的 JSON 请求必须正常")
            self.assertIn("turns", body)

    def test_unknown_body_then_act_on_same_connection(self):
        """404 分支不读请求体时，残留字节会毒化后续请求（实测 501/400）。"""
        code, _h, _b = self.call("/nope", {"a": 1}, method="POST")
        self.assertEqual(code, 404)
        code, _h, out = self.call(
            "/act", {"action": "move", "x": 7, "y": 7}, method="POST")
        self.assertEqual(code, 200, "上一条 404 的请求体必须被排空")
        self.assertTrue(out["ok"], out)


if __name__ == "__main__":
    unittest.main()
