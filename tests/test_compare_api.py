"""`/compare-images` 的契约与"不碰桌面"证明。

这里刻意不用替身引擎：比较路径本来就不该需要引擎。HTTP 用例把
``server.ENGINE`` 设成 ``None``，能回 200 才算真的证明"不跑模型"。
"""

from __future__ import annotations

import base64
import json
import os
import struct
import sys
import threading
import unittest
import zlib
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate import compare_api, managed  # noqa: E402
from ui_locate.image_api import ImageError  # noqa: E402


def png(width: int, height: int, color=(10, 20, 30), compression: int = 3) -> bytes:
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:, :] = color
    ok, blob = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, compression])
    assert ok
    return blob.tobytes()


def png_with_text_chunk(blob: bytes, key: str = b"Comment", value: bytes = b"step-1") -> bytes:
    """在 IHDR 之后插入一个合法 tEXt 块 —— 只改元数据，不动像素。"""
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    ihdr_len = struct.unpack(">I", blob[8:12])[0]
    ihdr_end = 8 + 12 + ihdr_len
    data = key + b"\x00" + value
    chunk = (
        struct.pack(">I", len(data))
        + b"tEXt"
        + data
        + struct.pack(">I", zlib.crc32(b"tEXt" + data) & 0xFFFFFFFF)
    )
    return blob[:ihdr_end] + chunk + blob[ihdr_end:]


def b64(blob: bytes) -> str:
    return base64.b64encode(blob).decode("ascii")


def side(blob: bytes, width: int, height: int) -> dict:
    return {"image_b64": b64(blob), "width": width, "height": height}


def payload(before: dict, after: dict) -> dict:
    return {"before": before, "after": after}


class ContractShapeTests(unittest.TestCase):
    def test_response_keys_are_exactly_the_contract(self):
        out = compare_api.compare_images(
            payload(side(png(32, 24), 32, 24), side(png(32, 24), 32, 24))
        )
        self.assertEqual(
            set(out),
            {
                "ok", "contractVersion", "comparable", "changed", "changedPixels",
                "totalPixels", "changedRatio", "maxChannelDelta", "changedBox",
                "image", "before", "after",
            },
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["contractVersion"], managed.CONTRACT_VERSION)

    def test_identical_images_are_unchanged(self):
        blob = png(40, 30)
        out = compare_api.compare_images(payload(side(blob, 40, 30), side(blob, 40, 30)))
        self.assertTrue(out["comparable"])
        self.assertIs(out["changed"], False)
        self.assertEqual(out["changedPixels"], 0)
        self.assertEqual(out["totalPixels"], 1200)
        self.assertEqual(out["changedRatio"], 0.0)
        self.assertEqual(out["maxChannelDelta"], 0)
        self.assertIsNone(out["changedBox"])
        self.assertEqual(out["image"], {"width": 40, "height": 30})

    def test_same_pixels_different_encoding_is_unchanged(self):
        """压缩级别不同 → 字节完全不同，但像素一样。这正是 B 原来被骗的地方。"""
        a = png(48, 32, compression=0)
        b = png(48, 32, compression=9)
        self.assertNotEqual(a, b)
        out = compare_api.compare_images(payload(side(a, 48, 32), side(b, 48, 32)))
        self.assertTrue(out["comparable"])
        self.assertIs(out["changed"], False)
        self.assertEqual(out["changedPixels"], 0)

    def test_same_pixels_different_metadata_is_unchanged(self):
        """多一个 tEXt 块（时间戳 / 工具名之类）也不该被当成"变了"。"""
        a = png(48, 32)
        b = png_with_text_chunk(a)
        self.assertNotEqual(a, b)
        self.assertTrue(b.startswith(a[:33]))
        out = compare_api.compare_images(payload(side(a, 48, 32), side(b, 48, 32)))
        self.assertIs(out["changed"], False)
        self.assertEqual(out["changedPixels"], 0)

    def test_jpeg_round_trip_of_identical_input_is_unchanged(self):
        img = np.zeros((30, 40, 3), dtype=np.uint8)
        img[:, :] = (200, 100, 50)
        ok, blob = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        self.assertTrue(ok)
        raw = blob.tobytes()
        out = compare_api.compare_images(payload(side(raw, 40, 30), side(raw, 40, 30)))
        self.assertTrue(out["comparable"])
        self.assertIs(out["changed"], False)


class RealChangeTests(unittest.TestCase):
    def test_changed_block_is_counted_and_boxed(self):
        before = png(64, 48)
        img = np.zeros((48, 64, 3), dtype=np.uint8)
        img[:, :] = (10, 20, 30)
        img[10:20, 5:15] = (255, 0, 0)          # 10×10
        ok, blob = cv2.imencode(".png", img)
        self.assertTrue(ok)
        out = compare_api.compare_images(payload(side(before, 64, 48), side(blob.tobytes(), 64, 48)))

        self.assertTrue(out["comparable"])
        self.assertTrue(out["changed"])
        self.assertEqual(out["changedPixels"], 100)
        self.assertEqual(out["totalPixels"], 64 * 48)
        self.assertEqual(out["changedRatio"], round(100 / (64 * 48), 6))
        self.assertEqual(out["maxChannelDelta"], 245)   # 255 - 10
        self.assertEqual(out["changedBox"], {"x": 5, "y": 10, "width": 10, "height": 10})

    def test_single_pixel_change_at_origin(self):
        before = png(16, 16)
        img = np.zeros((16, 16, 3), dtype=np.uint8)
        img[:, :] = (10, 20, 30)
        img[0, 0] = (11, 20, 30)                # 只差 1 个通道 1 级
        ok, blob = cv2.imencode(".png", img)
        self.assertTrue(ok)
        out = compare_api.compare_images(payload(side(before, 16, 16), side(blob.tobytes(), 16, 16)))
        # 没有"容差"这回事：差 1 级也算变了，判断权交给调用方。
        self.assertTrue(out["changed"])
        self.assertEqual(out["changedPixels"], 1)
        self.assertEqual(out["maxChannelDelta"], 1)
        self.assertEqual(out["changedBox"], {"x": 0, "y": 0, "width": 1, "height": 1})

    def test_change_is_order_independent(self):
        a = png(20, 20)
        img = np.zeros((20, 20, 3), dtype=np.uint8)
        img[:, :] = (99, 99, 99)
        ok, blob = cv2.imencode(".png", img)
        forward = compare_api.compare_images(payload(side(a, 20, 20), side(blob.tobytes(), 20, 20)))
        backward = compare_api.compare_images(payload(side(blob.tobytes(), 20, 20), side(a, 20, 20)))
        self.assertEqual(forward["changedPixels"], backward["changedPixels"])
        self.assertEqual(forward["changedBox"], backward["changedBox"])


class NotComparableTests(unittest.TestCase):
    def test_dimension_change_is_not_success(self):
        out = compare_api.compare_images(
            payload(side(png(64, 48), 64, 48), side(png(64, 49), 64, 49))
        )
        self.assertTrue(out["ok"])
        self.assertIs(out["comparable"], False)
        # 关键：不是 False，是 None —— "不知道，因为不可比"。
        self.assertIsNone(out["changed"])
        self.assertIsNone(out["changedPixels"])
        self.assertIsNone(out["changedRatio"])
        self.assertIsNone(out["changedBox"])
        self.assertEqual(out["reason"], "dimension_mismatch")
        self.assertIn("不代表动作成功", out["hint"])
        self.assertIsNone(out["image"])
        self.assertEqual(out["before"], {"width": 64, "height": 48})
        self.assertEqual(out["after"], {"width": 64, "height": 49})


class ValidationTests(unittest.TestCase):
    def assertCode(self, body, code, field=None):  # noqa: N802
        with self.assertRaises(ImageError) as ctx:
            compare_api.compare_images(body)
        self.assertEqual(ctx.exception.code, code)
        if field is not None:
            self.assertEqual(ctx.exception.field, field)
        return ctx.exception

    def test_body_must_be_object(self):
        self.assertCode([], "bad_request")

    def test_unknown_top_level_field_is_refused(self):
        body = payload(side(png(8, 8), 8, 8), side(png(8, 8), 8, 8))
        body["path"] = "C:/shot.png"
        exc = self.assertCode(body, "unsupported_field", "path")
        self.assertIn("path", exc.message)
        self.assertIn("before / after", exc.body()["hint"])

    def test_unknown_side_field_is_refused(self):
        a = side(png(8, 8), 8, 8)
        a["text"] = "Save"
        self.assertCode(payload(a, side(png(8, 8), 8, 8)), "unsupported_field", "before.text")

    def test_missing_side(self):
        self.assertCode({"before": side(png(8, 8), 8, 8)}, "bad_request", "after")

    def test_side_must_be_object(self):
        self.assertCode(payload(side(png(8, 8), 8, 8), "x"), "bad_request", "after")

    def test_bool_is_not_a_dimension(self):
        a = side(png(8, 8), 8, 8)
        a["width"] = True
        self.assertCode(payload(a, side(png(8, 8), 8, 8)), "bad_request", "before.width")

    def test_zero_dimension(self):
        a = side(png(8, 8), 8, 8)
        a["height"] = 0
        self.assertCode(payload(a, side(png(8, 8), 8, 8)), "bad_request", "before.height")

    def test_side_too_large(self):
        a = {"image_b64": b64(png(8, 8)), "width": compare_api.MAX_SIDE + 1, "height": 8}
        exc = self.assertCode(payload(a, side(png(8, 8), 8, 8)), "too_large", "before.width")
        self.assertEqual(exc.status, 413)

    def test_pixel_budget(self):
        # 单边都在限内，但 8000×6000 = 48M > 40M
        a = {"image_b64": b64(png(8, 8)), "width": 8000, "height": 6000}
        exc = self.assertCode(payload(a, side(png(8, 8), 8, 8)), "too_large", "before.width")
        self.assertEqual(exc.status, 413)

    def test_bad_base64(self):
        a = {"image_b64": "!!!not-base64!!!", "width": 8, "height": 8}
        self.assertCode(payload(a, side(png(8, 8), 8, 8)), "bad_base64", "before.image_b64")

    def test_data_url_prefix_refused(self):
        a = {"image_b64": "data:image/png;base64," + b64(png(8, 8)), "width": 8, "height": 8}
        self.assertCode(payload(a, side(png(8, 8), 8, 8)), "bad_base64", "before.image_b64")

    def test_gif_refused(self):
        gif = b"GIF89a" + b"\x00" * 32
        a = {"image_b64": b64(gif), "width": 8, "height": 8}
        exc = self.assertCode(payload(a, side(png(8, 8), 8, 8)), "unsupported_format", "before.image_b64")
        self.assertEqual(exc.status, 415)

    def test_declared_size_must_match_decoded_size(self):
        a = {"image_b64": b64(png(8, 8)), "width": 9, "height": 8}
        exc = self.assertCode(payload(a, side(png(8, 8), 8, 8)), "dimension_mismatch", "before.width")
        self.assertIn("before:", exc.message)
        self.assertEqual(exc.status, 400)

    def test_empty_image(self):
        a = {"image_b64": "   ", "width": 8, "height": 8}
        self.assertCode(payload(a, side(png(8, 8), 8, 8)), "bad_request", "before.image_b64")

    def test_error_body_carries_contract(self):
        with self.assertRaises(ImageError) as ctx:
            compare_api.compare_images({"before": 1, "after": 2})
        body = ctx.exception.body()
        self.assertFalse(body["ok"])
        self.assertEqual(body["contractVersion"], managed.CONTRACT_VERSION)
        self.assertEqual(body["code"], "bad_request")
        self.assertEqual(body["field"], "before")

    def test_parse_body(self):
        with self.assertRaises(ImageError) as ctx:
            compare_api.parse_body(b"")
        self.assertEqual(ctx.exception.code, "bad_json")
        with self.assertRaises(ImageError) as ctx:
            compare_api.parse_body(b"{oops")
        self.assertEqual(ctx.exception.code, "bad_json")
        self.assertEqual(compare_api.parse_body(b'{"a":1}'), {"a": 1})


class DecodeGuardTests(unittest.TestCase):
    """compare 复用 image_api 的解码闸门：头尺寸超限/撒谎都在解码前拒。"""

    def test_declared_small_but_large_image_is_refused_before_decode(self):
        big = png(300, 200)
        body = payload(side(big, 1, 1), side(png(8, 8), 8, 8))
        with mock.patch.object(compare_api.cv2, "imdecode",
                               side_effect=AssertionError("imdecode 不该被调用")):
            with self.assertRaises(ImageError) as ctx:
                compare_api.compare_images(body)
        self.assertEqual(ctx.exception.code, "dimension_mismatch")
        self.assertEqual(ctx.exception.field, "before.width")
        self.assertIn("图片头", ctx.exception.message)

    def test_header_over_limit_is_413_with_the_side_named(self):
        blob = bytearray(png(8, 8))
        struct.pack_into(">II", blob, 16, 8000, 6000)
        body = payload(side(bytes(blob), 8, 8), side(png(8, 8), 8, 8))
        with self.assertRaises(ImageError) as ctx:
            compare_api.compare_images(body)
        self.assertEqual(ctx.exception.code, "too_large")
        self.assertEqual(ctx.exception.status, 413)
        self.assertEqual(ctx.exception.field, "before.image_b64")


class NoDesktopSurfaceTests(unittest.TestCase):
    def test_module_does_not_import_desktop_or_engine(self):
        source = Path(compare_api.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "screen.grab", "capture_mod", "window_mod", "SetForegroundWindow",
            "SendInput", "PostMessage", "mss.", "dxcam", "server import", "from ui_locate import server",
        ):
            self.assertNotIn(forbidden, source, forbidden)
        self.assertNotIn("engine", getattr(compare_api, "compare_images").__code__.co_varnames)
        self.assertFalse(hasattr(compare_api, "Engine"))


class HttpTests(unittest.TestCase):
    """真 Handler + 真路由；``ENGINE`` 置 None 以证明比较不需要模型。"""

    @classmethod
    def setUpClass(cls):
        from ui_locate import server

        cls.server = server
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self._engine = self.server.ENGINE
        self.server.ENGINE = None
        self._env = mock.patch.dict("os.environ", {}, clear=False)
        self._env.start()
        self._had_managed = os.environ.pop(managed.MANAGED_ENV, None)
        self._screen = mock.MagicMock()
        self._capture = mock.MagicMock()
        self._window = mock.MagicMock()
        self._patches = [
            mock.patch.object(self.server, "screen", self._screen),
            mock.patch.object(self.server, "capture_mod", self._capture),
            mock.patch.object(self.server, "window_mod", self._window),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        if self._had_managed is not None:
            os.environ[managed.MANAGED_ENV] = self._had_managed
        self._env.stop()
        self.server.ENGINE = self._engine

    def request(self, method: str, path: str, body: dict | None = None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def assertNoDesktopCalls(self):  # noqa: N802
        for spy in (self._screen, self._capture, self._window):
            self.assertEqual(spy.mock_calls, [])

    def test_compare_works_without_an_engine(self):
        blob = png(32, 24)
        status, body = self.request("POST", "/compare-images", payload(side(blob, 32, 24), side(blob, 32, 24)))
        self.assertEqual(status, 200)
        self.assertIs(body["changed"], False)
        self.assertEqual(body["changedPixels"], 0)
        self.assertNoDesktopCalls()

    def test_compare_reports_a_real_change_over_http(self):
        before = png(16, 16)
        img = np.zeros((16, 16, 3), dtype=np.uint8)
        img[:, :] = (10, 20, 30)
        img[4:8, 4:8] = (250, 250, 250)
        ok, blob = cv2.imencode(".png", img)
        status, body = self.request(
            "POST", "/compare-images", payload(side(before, 16, 16), side(blob.tobytes(), 16, 16))
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["changed"])
        self.assertEqual(body["changedPixels"], 16)
        self.assertNoDesktopCalls()

    def test_managed_mode_allows_compare(self):
        os.environ[managed.MANAGED_ENV] = "1"
        blob = png(8, 8)
        status, body = self.request("POST", "/compare-images", payload(side(blob, 8, 8), side(blob, 8, 8)))
        self.assertEqual(status, 200)
        self.assertIs(body["changed"], False)
        self.assertNoDesktopCalls()

        status, health = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["capabilities"]["compare"])
        self.assertTrue(health["capabilities"]["image"])
        self.assertFalse(health["capabilities"]["desktop"])
        self.assertNotIn("screen", health)

    def test_managed_still_blocks_desktop_routes(self):
        os.environ[managed.MANAGED_ENV] = "1"
        status, body = self.request("POST", "/detect", {})
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "CUA_MANAGED")

    def test_validation_errors_keep_their_status(self):
        blob = png(8, 8)
        bad = side(blob, 9, 8)
        status, body = self.request("POST", "/compare-images", payload(bad, side(blob, 8, 8)))
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "dimension_mismatch")
        self.assertEqual(body["field"], "before.width")

        status, body = self.request("POST", "/compare-images", {"before": side(blob, 8, 8)})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "bad_request")

        status, body = self.request("POST", "/compare-images", {"path": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "unsupported_field")
        self.assertNoDesktopCalls()

    def test_oversize_body_is_refused_before_reading(self):
        big = {"before": {"image_b64": "A" * 200, "width": 8, "height": 8},
               "after": {"image_b64": "A" * 200, "width": 8, "height": 8}}
        old = self.server.image_api.MAX_BODY_BYTES
        self.server.image_api.MAX_BODY_BYTES = 64
        try:
            status, body = self.request("POST", "/compare-images", big)
        finally:
            self.server.image_api.MAX_BODY_BYTES = old
        self.assertEqual(status, 413)
        self.assertEqual(body["code"], "too_large")


if __name__ == "__main__":
    unittest.main(verbosity=2)
