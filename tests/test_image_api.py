"""image_api：纯图片契约。

这里测的是「坐标到底属于谁」和「错误到底该由谁处理」。两个都容易在改动中
悄悄走样，而且走样之后**看起来仍然正常**：坐标多了个 origin 偏移，仍然是个
能点的点；把「图片坏了」和「服务忙」混成一个 500，调用方仍然会重试——只是
重试一百次也不会成功。

引擎用替身。真正的检测/OCR 在 test_integration 里有真实样本的覆盖；这条
路径要证明的是**它只碰这张图**：引擎收到的 bgr 形状等于声明尺寸，kwargs 里
origin 必须是 (0, 0)。
"""

from __future__ import annotations

import base64
import json
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from ui_locate import image_api as API  # noqa: E402
from ui_locate import managed  # noqa: E402


def png(width: int = 200, height: int = 100) -> bytes:
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:, :] = (30, 60, 90)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def jpg(width: int = 64, height: int = 48) -> bytes:
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:, :] = (200, 120, 40)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def b64(blob: bytes) -> str:
    return base64.b64encode(blob).decode("ascii")


def element(text: str, x1: float, y1: float, x2: float, y2: float,
            *, conf: float = 0.5, text_score: float | None = None) -> dict:
    out = {"x1": x1, "y1": y1, "x2": x2, "y2": y2,
           "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2,
           "w": x2 - x1, "h": y2 - y1, "conf": conf, "text": text}
    if text_score is not None:
        out["text_score"] = text_score
    return out


class FakeEngine:
    """Engine 的最小替身：记下调用参数，返回预置元素。

    ``by_mode`` 让替身按 ``text_mode`` 返回不同的元素 —— mosaic 回退到 full
    是这条路径上唯一一次"跑两遍"，不区分模式就测不出它到底跑了几遍、跑的
    是哪一遍。
    """

    def __init__(self, elements=None, error: BaseException | None = None,
                 by_mode: dict[str, list] | None = None):
        self.elements = list(elements or [])
        self.error = error
        self.by_mode = by_mode or {}
        self.calls: list[dict] = []

    def run(self, bgr, **kwargs):
        self.calls.append({"shape": tuple(bgr.shape), **kwargs})
        if self.error is not None:
            raise self.error
        want = self.by_mode.get(kwargs.get("text_mode"), self.elements)
        return {"elements": [dict(e) for e in want]}


def payload(engine_elements=None, *, mode="locate", text="保存", width=200,
            height=100, blob=None, **extra):
    body = {
        "image_b64": b64(blob if blob is not None else png(width, height)),
        "width": width,
        "height": height,
        "mode": mode,
    }
    if text is not None:
        body["text"] = text
    body.update(extra)
    return body


class ContractTests(unittest.TestCase):
    def test_response_is_the_whole_contract_and_nothing_more(self):
        engine = FakeEngine([element("保存", 10, 20, 60, 40, conf=0.8, text_score=0.91)])
        out = API.locate_image(payload(), engine)

        self.assertEqual(set(out), {"ok", "contractVersion", "coordinateSpace",
                                    "image", "matches"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["contractVersion"], 1)
        self.assertEqual(out["contractVersion"], managed.CONTRACT_VERSION)
        self.assertEqual(out["coordinateSpace"], "image-pixels")
        self.assertEqual(out["image"], {"width": 200, "height": 100})

        match = out["matches"][0]
        self.assertEqual(set(match), {"text", "confidence", "box", "center"})
        self.assertEqual(match["text"], "保存")
        self.assertEqual(match["confidence"], 0.91)   # OCR 分数优先于检测分数
        self.assertEqual(match["box"], {"x": 10, "y": 20, "width": 50, "height": 20})
        self.assertEqual(match["center"], {"x": 35, "y": 30})

    def test_confidence_falls_back_to_detection_score(self):
        engine = FakeEngine([element("保存", 0, 0, 10, 10, conf=0.37)])
        out = API.locate_image(payload(), engine)
        self.assertEqual(out["matches"][0]["confidence"], 0.37)

    def test_engine_is_called_with_image_origin_and_no_screen_offset(self):
        engine = FakeEngine([element("保存", 0, 0, 10, 10)])
        body = payload(conf=0.42, tiled=True)
        API.locate_image(body, engine)

        call = engine.calls[0]
        self.assertEqual(call["shape"], (100, 200, 3))
        self.assertEqual(call["origin"], (0, 0))
        self.assertEqual(call["conf"], 0.42)
        self.assertTrue(call["tiled"])
        self.assertTrue(call["text"])
        self.assertEqual(call["text_mode"], "mosaic")
        self.assertEqual(call["iou"], 0.5)
        self.assertEqual(call["overlap"], 0.15)

    def test_empty_match_is_success(self):
        engine = FakeEngine([element("取消", 0, 0, 10, 10)])
        out = API.locate_image(payload(text="保存"), engine)
        self.assertTrue(out["ok"])
        self.assertEqual(out["matches"], [])
        # 没找到才回退：这次跑了两遍，第二遍必须是 full
        self.assertEqual([c["text_mode"] for c in engine.calls], ["mosaic", "full"])

    def test_hit_does_not_pay_for_the_full_fallback(self):
        engine = FakeEngine([element("保存", 0, 0, 10, 10)])
        API.locate_image(payload(), engine)
        self.assertEqual(len(engine.calls), 1)

    def test_mosaic_miss_falls_back_to_full_and_finds_it(self):
        """冒烟里实测到的形状：框有了，裁片 OCR 读不出字，整页 OCR 一次命中。"""
        engine = FakeEngine(
            by_mode={"mosaic": [element("", 0, 0, 50, 20)],
                     "full": [element("Save", 0, 0, 50, 20, text_score=1.0)]})
        out = API.locate_image(payload(text="Save"), engine)
        self.assertEqual([c["text_mode"] for c in engine.calls], ["mosaic", "full"])
        self.assertEqual([m["text"] for m in out["matches"]], ["Save"])

    def test_detect_never_pays_for_the_full_fallback(self):
        engine = FakeEngine([element("", 0, 0, 10, 10)])
        API.locate_image(payload(mode="detect", text=""), engine)
        self.assertEqual(len(engine.calls), 1)

    def test_detect_mode_needs_no_text_and_returns_limit(self):
        engine = FakeEngine([element("a", 0, 0, 5, 5), element("b", 6, 6, 10, 10),
                             element("c", 11, 11, 15, 15)])
        out = API.locate_image(payload(mode="detect", text=None), engine)
        self.assertTrue(out["ok"])
        self.assertEqual([m["text"] for m in out["matches"]], ["a", "b", "c"])

    def test_detect_mode_respects_limit(self):
        engine = FakeEngine([element(str(i), i, i, i + 4, i + 4) for i in range(30)])
        out = API.locate_image(payload(mode="detect", text=None, limit=7), engine)
        self.assertEqual(len(out["matches"]), 7)

    def test_exact_match_outranks_longer_text(self):
        engine = FakeEngine([
            element("保存并关闭窗口", 0, 0, 100, 20, conf=0.99),
            element("保存", 150, 0, 190, 20, conf=0.40),
        ])
        out = API.locate_image(payload(text="保存"), engine)
        self.assertEqual(out["matches"][0]["text"], "保存")

    def test_prefix_match_is_accepted(self):
        """``starts`` 档：OCR 常把按钮文字和旁边的字粘在一起，前缀仍值得点。"""
        engine = FakeEngine([element("保存全部", 0, 0, 80, 20)])
        out = API.locate_image(payload(text="保存"), engine)
        self.assertEqual([m["text"] for m in out["matches"]], ["保存全部"])

    def test_contains_only_match_is_refused(self):
        """包含档不给这条路径：它的消费者拿到 matches[0] 就去点。

        实测过掉到 contains 档点中任务栏上另一个窗口、屏幕确实变了、还回了
        verified:true —— 报成功但点错地方，比报"没找到"糟得多。

        注意 ``starts`` 档同时接受**前缀和后缀**（OCR 常把图标文字拼在按钮
        前面，如「8 插件」）；真正被挡住的是查询词只出现在中间的情况。
        """
        engine = FakeEngine([element("已保存全部", 0, 0, 80, 20)])
        out = API.locate_image(payload(text="保存"), engine)
        self.assertEqual(out["matches"], [])
        self.assertEqual([c["text_mode"] for c in engine.calls], ["mosaic", "full"])

    def test_jpeg_is_accepted(self):
        engine = FakeEngine([element("保存", 0, 0, 10, 10)])
        out = API.locate_image(payload(width=64, height=48, blob=jpg()), engine)
        self.assertTrue(out["ok"])
        self.assertEqual(engine.calls[0]["shape"], (48, 64, 3))


class BoxTests(unittest.TestCase):
    def test_box_outside_image_is_clipped_and_center_stays_inside(self):
        engine = FakeEngine([element("保存", 190, 90, 260, 140)])
        out = API.locate_image(payload(), engine)
        box = out["matches"][0]["box"]
        self.assertEqual(box, {"x": 190, "y": 90, "width": 10, "height": 10})
        self.assertLessEqual(box["x"] + box["width"], 200)
        self.assertLessEqual(box["y"] + box["height"], 100)
        center = out["matches"][0]["center"]
        self.assertTrue(0 <= center["x"] < 200)
        self.assertTrue(0 <= center["y"] < 100)

    def test_negative_coordinates_are_clipped(self):
        engine = FakeEngine([element("保存", -10, -10, 20, 20)])
        out = API.locate_image(payload(), engine)
        self.assertEqual(out["matches"][0]["box"], {"x": 0, "y": 0, "width": 20, "height": 20})

    def test_reversed_box_is_normalized(self):
        engine = FakeEngine([element("保存", 60, 40, 10, 20)])
        out = API.locate_image(payload(), engine)
        self.assertEqual(out["matches"][0]["box"], {"x": 10, "y": 20, "width": 50, "height": 20})

    def test_fully_outside_box_is_dropped_not_collapsed_to_a_corner(self):
        """整框在图外 → 丢弃；**不能**折叠成右下角那 1 个像素。

        早先把宽高强行抬到 1，于是 x1=y1=300 的框变成图片右下角 (199, 99) 的
        可点击点。调用方照着它点，点的是边框，还以为是自己的匹配错了。
        """
        engine = FakeEngine([element("保存", 300, 300, 360, 340),
                             element("保存", 10, 10, 40, 30)])
        out = API.locate_image(payload(), engine)
        self.assertEqual(len(out["matches"]), 1)
        self.assertEqual(out["matches"][0]["box"], {"x": 10, "y": 10, "width": 30, "height": 20})

    def test_box_entirely_left_or_above_is_dropped(self):
        for x1, y1, x2, y2 in ((-40, 10, -5, 40), (10, -40, 40, -5)):
            with self.subTest(box=(x1, y1, x2, y2)):
                engine = FakeEngine([element("保存", x1, y1, x2, y2)])
                self.assertEqual(API.locate_image(payload(), engine)["matches"], [])

    def test_non_finite_geometry_is_dropped(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                engine = FakeEngine([element("保存", 10, 10, bad, 40)])
                self.assertEqual(API.locate_image(payload(), engine)["matches"], [])

    def test_sub_pixel_overlap_still_lands_inside_the_image(self):
        engine = FakeEngine([element("保存", -0.4, -0.4, 0.2, 0.2)])
        out = API.locate_image(payload(), engine)
        self.assertEqual(out["matches"][0]["box"], {"x": 0, "y": 0, "width": 1, "height": 1})

    def test_element_without_geometry_is_dropped(self):
        engine = FakeEngine([{"text": "保存", "conf": 0.9}, element("保存", 0, 0, 10, 10)])
        out = API.locate_image(payload(), engine)
        self.assertEqual(len(out["matches"]), 1)
        self.assertEqual(out["matches"][0]["box"]["x"], 0)


class HeaderSizeTests(unittest.TestCase):
    """解码**之前**就要看真实尺寸：先分内存再比对，限额等于没有。"""

    def refuse(self, body, code, status=None):
        engine = FakeEngine()
        with mock.patch.object(API.cv2, "imdecode",
                               side_effect=AssertionError("imdecode 不该被调用")):
            with self.assertRaises(API.ImageError) as ctx:
                API.locate_image(body, engine)
        self.assertEqual(ctx.exception.code, code)
        if status is not None:
            self.assertEqual(ctx.exception.status, status)
        self.assertEqual(engine.calls, [])
        return ctx.exception

    def test_declared_small_but_large_png_is_refused_before_decode(self):
        exc = self.refuse(payload(width=1, height=1, blob=png(300, 200)), "dimension_mismatch")
        self.assertIn("图片头", exc.message)
        self.assertIn("300", exc.message)

    def test_png_header_over_the_side_limit_is_413_before_decode(self):
        blob = bytearray(png(8, 8))
        struct.pack_into(">II", blob, 16, API.MAX_SIDE + 1, 8)
        self.refuse(payload(width=8, height=8, blob=bytes(blob)), "too_large", 413)

    def test_png_header_over_the_pixel_budget_is_413_before_decode(self):
        blob = bytearray(png(8, 8))
        struct.pack_into(">II", blob, 16, 8000, 6000)
        self.refuse(payload(width=8, height=8, blob=bytes(blob)), "too_large", 413)

    def test_jpeg_header_is_checked_before_decode(self):
        exc = self.refuse(payload(width=1, height=1, blob=jpg(64, 48)), "dimension_mismatch")
        self.assertIn("图片头", exc.message)

    def test_png_without_ihdr_is_refused(self):
        blob = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"IDAT" + b"\x00" * 20
        self.refuse(payload(width=8, height=8, blob=blob), "bad_image")

    def test_valid_image_still_decodes(self):
        engine = FakeEngine([element("保存", 0, 0, 10, 10)])
        out = API.locate_image(payload(), engine)
        self.assertEqual(engine.calls[0]["shape"], (100, 200, 3))
        self.assertTrue(out["ok"])


class ValidationTests(unittest.TestCase):
    def fail(self, body, code, status=None, field=None):
        engine = FakeEngine()
        with self.assertRaises(API.ImageError) as ctx:
            API.locate_image(body, engine)
        err = ctx.exception
        self.assertEqual(err.code, code)
        if status is not None:
            self.assertEqual(err.status, status)
        if field is not None:
            self.assertEqual(err.field, field)
        # 校验失败必须在跑引擎之前 —— 失败不产生部分结果，也不浪费时间。
        self.assertEqual(engine.calls, [])
        return err

    def test_unknown_field_is_rejected_including_path(self):
        body = payload()
        body["path"] = r"C:\Windows\win.ini"
        err = self.fail(body, "unsupported_field", 400, "path")
        self.assertIn("path", err.message)
        self.assertIn("Host", err.body().get("hint", "") + "Host")

    def test_monitor_and_window_fields_are_rejected(self):
        for extra in ({"monitor": "primary"}, {"window": "记事本"},
                      {"capture": "screen"}, {"focus": True}):
            with self.subTest(extra=extra):
                body = payload()
                body.update(extra)
                self.fail(body, "unsupported_field", 400)

    def test_non_object_body(self):
        self.fail(["x"], "bad_request", 400)

    def test_width_must_be_int_not_bool(self):
        body = payload()
        body["width"] = True
        self.fail(body, "bad_request", 400, "width")

    def test_width_must_be_positive(self):
        body = payload()
        body["width"] = 0
        self.fail(body, "bad_request", 400, "width")

    def test_single_side_limit(self):
        body = payload(width=API.MAX_SIDE + 1, height=10)
        self.fail(body, "too_large", 413)

    def test_pixel_limit(self):
        body = payload(width=7000, height=6000)
        self.assertGreater(7000 * 6000, API.MAX_PIXELS)
        self.fail(body, "too_large", 413)

    def test_mode_must_be_known(self):
        self.fail(payload(mode="search"), "bad_mode", 400, "mode")

    def test_locate_requires_text(self):
        self.fail(payload(text=""), "missing_text", 400, "text")
        self.fail(payload(text="   "), "missing_text", 400, "text")
        self.fail(payload(text=None), "missing_text", 400, "text")

    def test_limit_range(self):
        for bad in (0, -1, API.MAX_LIMIT + 1):
            with self.subTest(limit=bad):
                self.fail(payload(limit=bad), "bad_request", 400, "limit")

    def test_conf_range(self):
        for bad in (-0.1, 1.5):
            with self.subTest(conf=bad):
                self.fail(payload(conf=bad), "bad_request", 400, "conf")

    def test_tiled_accepts_bool_like_strings(self):
        engine = FakeEngine([element("保存", 0, 0, 10, 10)])
        API.locate_image(payload(tiled="true"), engine)
        self.assertTrue(engine.calls[0]["tiled"])

    def test_image_b64_must_be_non_empty_string(self):
        body = payload()
        body["image_b64"] = ""
        self.fail(body, "bad_request", 400, "image_b64")

    def test_data_url_prefix_is_rejected(self):
        body = payload()
        body["image_b64"] = "data:image/png;base64," + b64(png())
        err = self.fail(body, "bad_base64", 400, "image_b64")
        self.assertIn("data:", err.message)

    def test_invalid_base64_is_rejected(self):
        body = payload()
        body["image_b64"] = "这显然不是 base64"
        self.fail(body, "bad_base64", 400, "image_b64")

    def test_non_image_bytes_are_415(self):
        body = payload(blob=b"GIF89a" + b"\x00" * 32)
        self.fail(body, "unsupported_format", 415, "image_b64")

    def test_corrupt_png_is_bad_image(self):
        body = payload(blob=b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        self.fail(body, "bad_image", 400, "image_b64")

    def test_dimension_mismatch(self):
        # 图片实际是 200×100，请求却声明 100×200。
        body = payload(width=100, height=200, blob=png(200, 100))
        err = self.fail(body, "dimension_mismatch", 400, "width")
        self.assertIn("200", err.message)


class FailureLayeringTests(unittest.TestCase):
    def test_timeout_is_busy_503(self):
        engine = FakeEngine(error=TimeoutError("慢"))
        with self.assertRaises(API.ImageError) as ctx:
            API.locate_image(payload(), engine)
        self.assertEqual(ctx.exception.code, "busy")
        self.assertEqual(ctx.exception.status, 503)

    def test_engine_crash_is_engine_error_500(self):
        engine = FakeEngine(error=RuntimeError("onnx 挂了"))
        with self.assertRaises(API.ImageError) as ctx:
            API.locate_image(payload(), engine)
        self.assertEqual(ctx.exception.code, "engine_error")
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("onnx 挂了", ctx.exception.message)

    def test_error_body_carries_contract_version(self):
        err = API.ImageError("bad_mode", "x")
        body = err.body()
        self.assertEqual(body["contractVersion"], 1)
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "bad_mode")


class ParseBodyTests(unittest.TestCase):
    def test_empty_body(self):
        with self.assertRaises(API.ImageError) as ctx:
            API.parse_body(b"")
        self.assertEqual(ctx.exception.code, "bad_json")

    def test_broken_json(self):
        with self.assertRaises(API.ImageError) as ctx:
            API.parse_body(b"{")
        self.assertEqual(ctx.exception.code, "bad_json")

    def test_valid_json(self):
        self.assertEqual(API.parse_body(json.dumps({"a": 1}).encode()), {"a": 1})


class NoDesktopSurfaceTests(unittest.TestCase):
    """这条路径不该有任何桌面模块挂在名字空间里。"""

    def test_module_does_not_import_screen_or_input(self):
        names = set(vars(API))
        for banned in ("screen", "capture", "capture_mod", "window", "window_mod",
                       "actions", "act", "ctypes"):
            self.assertNotIn(banned, names, f"image_api 不应依赖 {banned}")

    def test_source_has_no_screen_calls(self):
        """结构断言：源码里不出现抓屏/窗口/输入调用。"""
        src = Path(API.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines()
                         if not line.strip().startswith("#"))
        for banned in ("screen.grab", "list_windows", "SetForegroundWindow",
                       "SendInput", "PostMessage", "mss.", "dxcam"):
            self.assertNotIn(banned, code)


if __name__ == "__main__":
    unittest.main()
