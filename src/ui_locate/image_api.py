"""纯图片分析：一张完整图片 + 一句文字 → 图片像素坐标的匹配。

和 `/locate`、`/find` 的区别只有一个，但很关键：**这里没有屏幕**.

``/locate`` 拿到的是"现在屏幕上有什么"，所以它有隐式抓屏、隐式 origin 偏移、
隐式显示器选择。这一条拿到的是调用方**已经给好的像素**，因此：

  * 坐标基于这张图片的左上角，不加 origin、不归一化、不猜屏幕；
  * 不抓屏、不列窗口、不切前台、不读任意本地路径、不注入输入；
  * 同一张图 + 同一段文字，在任何机器上给出同样的坐标。

算法不另写一份：检测、OCR、文字匹配全部复用 ``Engine.run`` 与
``ocr_match.find``。复制一份"简化版"意味着两套排序规则会慢慢分叉，
然后调用方开始抱怨"同一个画面两个接口给的框不一样"。

错误分层（每一层调用方要做的判断不同，所以必须分开）：

    格式/结构错（400/413/415）  → 改请求，别重试
    busy（503）                → 等一下重试
    engine_error（500）        → 服务侧问题，报给 Host

失败不缓存、失败不返回部分匹配：宁可空手，也不要给一个"只对了一半"的坐标。
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import struct

import cv2
import numpy as np

from ui_locate import managed
from ui_locate.ocr_match import find

# ---------------------------------------------------------------------------
# 限额。这些数字不是拍的：它们刚好覆盖"多屏截图 + 一点余量"，再大就说明
# 调用方在往这儿塞别的东西（视频帧、扫描件）。超限宁可拒绝，也不要把内存
# 交给一个不可信的 POST。
# ---------------------------------------------------------------------------
MAX_BODY_BYTES = 32 * 1024 * 1024      # 整个 JSON 请求体
MAX_IMAGE_BYTES = 24 * 1024 * 1024     # 解码后的图片字节
MAX_PIXELS = 40_000_000                # 宽 × 高
MAX_SIDE = 16384                       # 单边
MAX_LIMIT = 500

# 请求字段白名单。多一个字段就 400 —— 这条**故意**严格：允许未知字段等于
# 允许调用方传 ``path`` / ``monitor`` / ``window`` 然后指望我们"顺手"读一下，
# 那正是这套接口要堵死的路。
ALLOWED_FIELDS = frozenset({
    "image_b64", "width", "height", "text", "mode", "limit", "conf", "tiled",
})

MODES = ("locate", "detect")

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


class ImageError(Exception):
    """一次请求的失败，带 HTTP 状态与稳定错误码。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        field: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.field = field
        self.hint = hint

    def body(self) -> dict:
        out: dict = {
            "ok": False,
            "code": self.code,
            "error": self.message,
            "contractVersion": managed.CONTRACT_VERSION,
        }
        if self.field:
            out["field"] = self.field
        if self.hint:
            out["hint"] = self.hint
        return out


def _bad(field: str, message: str, *, code: str = "bad_request", hint: str | None = None) -> ImageError:
    return ImageError(code, message, field=field, hint=hint)


def _as_int(payload: dict, key: str) -> int:
    value = payload.get(key)
    # bool 是 int 的子类，True 会悄悄变成 1。这类请求要拒绝而不是"修正"。
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(key, f"{key} 必须是整数（收到 {type(value).__name__}）")
    return value


def _as_number(payload: dict, key: str, default: float) -> float:
    value = payload.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad(key, f"{key} 必须是数字（收到 {type(value).__name__}）")
    return float(value)


def _validate(payload: object) -> dict:
    """结构校验。只读 payload，不碰图片字节。"""
    if not isinstance(payload, dict):
        raise ImageError("bad_request", "请求体必须是 JSON 对象")

    unknown = sorted(set(payload) - ALLOWED_FIELDS)
    if unknown:
        raise ImageError(
            "unsupported_field",
            f"不支持的字段：{', '.join(unknown)}",
            field=unknown[0],
            hint=(
                "本接口只分析请求里给的这张图片，不接受 path / monitor / window / "
                "capture / focus 之类的隐式本地读取。"
            ),
        )

    width = _as_int(payload, "width")
    height = _as_int(payload, "height")
    if width <= 0 or height <= 0:
        raise _bad("width", "width / height 必须是正整数")
    if width > MAX_SIDE or height > MAX_SIDE:
        raise ImageError(
            "too_large",
            f"图片单边超过 {MAX_SIDE} 像素（收到 {width}×{height}）",
            status=413,
            field="width" if width > MAX_SIDE else "height",
        )
    if width * height > MAX_PIXELS:
        raise ImageError(
            "too_large",
            f"图片像素数超过 {MAX_PIXELS}（收到 {width * height}）",
            status=413,
            field="width",
        )

    mode = payload.get("mode", "locate")
    if not isinstance(mode, str) or mode not in MODES:
        raise ImageError(
            "bad_mode",
            f"mode 只能是 {' / '.join(MODES)}（收到 {mode!r}）",
            field="mode",
        )

    text = payload.get("text", "")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise _bad("text", f"text 必须是字符串（收到 {type(text).__name__}）")
    text = text.strip()
    if mode == "locate" and not text:
        raise ImageError(
            "missing_text",
            "mode=locate 需要非空的 text",
            field="text",
            hint="只想列出所有元素（不需要文字）请用 mode=detect。",
        )

    limit = payload.get("limit")
    if limit is None:
        limit = 5 if mode == "locate" else 20
    elif isinstance(limit, bool) or not isinstance(limit, int):
        raise _bad("limit", f"limit 必须是整数（收到 {type(limit).__name__}）")
    if limit < 1 or limit > MAX_LIMIT:
        raise _bad("limit", f"limit 必须在 1..{MAX_LIMIT} 之间（收到 {limit}）")

    conf = _as_number(payload, "conf", 0.3)
    if not 0.0 <= conf <= 1.0:
        raise _bad("conf", f"conf 必须在 0..1 之间（收到 {conf}）")

    tiled = payload.get("tiled", False)
    if isinstance(tiled, bool):
        pass
    elif isinstance(tiled, str) and tiled.strip().lower() in ("1", "true", "yes", "on", "0", "false", "no", "off"):
        tiled = tiled.strip().lower() in ("1", "true", "yes", "on")
    else:
        raise _bad("tiled", f"tiled 必须是布尔值（收到 {tiled!r}）")

    image_b64 = payload.get("image_b64")
    if not isinstance(image_b64, str) or not image_b64.strip():
        raise _bad("image_b64", "image_b64 必填，且必须是非空 base64 字符串")

    return {
        "width": width,
        "height": height,
        "mode": mode,
        "text": text,
        "limit": limit,
        "conf": conf,
        "tiled": bool(tiled),
        "image_b64": image_b64,
    }


def _decode_image(image_b64: str, width: int, height: int) -> np.ndarray:
    """base64 → 字节 → BGR。只认 PNG / JPEG。"""
    raw_text = image_b64.strip()
    if raw_text.startswith("data:"):
        raise ImageError(
            "bad_base64",
            "image_b64 必须是纯 base64，不要带 data: 前缀",
            field="image_b64",
            hint="去掉 'data:image/png;base64,' 之后再编码。",
        )

    try:
        blob = base64.b64decode(raw_text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise ImageError(
            "bad_base64",
            f"image_b64 不是合法 base64：{exc}",
            field="image_b64",
        ) from None

    if not blob:
        raise ImageError("bad_image", "图片字节为空", field="image_b64")
    if len(blob) > MAX_IMAGE_BYTES:
        raise ImageError(
            "too_large",
            f"图片字节超过 {MAX_IMAGE_BYTES}（收到 {len(blob)}）",
            status=413,
            field="image_b64",
        )

    if blob.startswith(_PNG_MAGIC):
        pass
    elif blob[:3] == _JPEG_MAGIC:
        pass
    else:
        raise ImageError(
            "unsupported_format",
            "只支持 PNG / JPEG",
            status=415,
            field="image_b64",
            hint="WebP / BMP / GIF 请先转成 PNG 或 JPEG。",
        )

    # 解码**之前**先看文件头：真实尺寸超限、或与声明不符，就直接拒绝，
    # 一个字节的像素内存都不分配。
    header = _probe_size(blob)
    if header is None:
        raise ImageError(
            "bad_image",
            "无法从文件头读出图片尺寸（PNG 缺 IHDR / JPEG 缺 SOF）",
            field="image_b64",
        )
    head_w, head_h = header
    _check_limits(head_w, head_h, field="image_b64")
    if (head_w, head_h) != (width, height):
        raise ImageError(
            "dimension_mismatch",
            f"请求声明 {width}×{height}，图片头声明 {head_w}×{head_h}",
            field="width",
            hint="width / height 必须与图片本身的像素尺寸一致——坐标要靠它解释。",
        )

    bgr = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_COLOR)
    if bgr is None or bgr.size == 0:
        raise ImageError("bad_image", "图片无法解码（数据被截断或已损坏）", field="image_b64")

    real_h, real_w = bgr.shape[:2]
    # 头会撒谎（改过 IHDR 但数据仍是小图）：解码后按真实尺寸再过一遍限额。
    _check_limits(real_w, real_h, field="image_b64")
    if (real_w, real_h) != (width, height):
        raise ImageError(
            "dimension_mismatch",
            f"请求声明 {width}×{height}，实际解码 {real_w}×{real_h}",
            field="width",
            hint="width / height 必须与图片本身的像素尺寸一致——坐标要靠它解释。",
        )
    return bgr


def _probe_size(blob: bytes) -> tuple[int, int] | None:
    """从文件头读出真实像素尺寸，**不解码像素**。

    为什么必须有这一步：`cv2.imdecode` 会按图片自己声明的尺寸分配内存。请求
    里写 ``width=1,height=1`` 但塞一张 50000×50000 的图时，先解码再比对等于
    先把内存花掉 —— 40MP 的上限就形同虚设。PNG 读 IHDR，JPEG 扫到第一个
    SOF 段；两者都只看几十个字节。
    """
    if blob.startswith(_PNG_MAGIC):
        if len(blob) < 24 or blob[12:16] != b"IHDR":
            return None
        w, h = struct.unpack(">II", blob[16:24])
        return int(w), int(h)

    if blob[:3] == _JPEG_MAGIC:
        i, n = 2, len(blob)
        while i + 4 <= n:
            if blob[i] != 0xFF:
                i += 1
                continue
            marker = blob[i + 1]
            # 无长度的标记：SOI、TEM、RSTn
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker == 0xD9:            # EOI：没找到 SOF
                return None
            seg_len = struct.unpack(">H", blob[i + 2:i + 4])[0]
            if seg_len < 2:
                return None
            # SOF0..SOF15（除 DHT/JPG/DAC）里带尺寸
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if i + 9 > n:
                    return None
                h, w = struct.unpack(">HH", blob[i + 5:i + 9])
                return int(w), int(h)
            i += 2 + seg_len
    return None


def _check_limits(width: int, height: int, *, field: str) -> None:
    """单边/总像素限额。解码前后都要过一遍（头里的数字也可能太小而撒谎）。"""
    if width <= 0 or height <= 0:
        raise ImageError("bad_image", f"图片尺寸非法：{width}×{height}", field=field)
    if width > MAX_SIDE or height > MAX_SIDE:
        raise ImageError(
            "too_large",
            f"图片单边超过 {MAX_SIDE} 像素（收到 {width}×{height}）",
            status=413,
            field=field,
        )
    if width * height > MAX_PIXELS:
        raise ImageError(
            "too_large",
            f"图片像素数超过 {MAX_PIXELS}（收到 {width * height}）",
            status=413,
            field=field,
        )


def _clip_box(it: dict, width: int, height: int) -> dict | None:
    """把检测框夹进图片范围内，转成整数像素。**完全落在图外则丢弃**。

    这里有两个"宁可丢掉"的规矩，都是踩过的坑：

    * **非有限坐标直接丢**：NaN 与任何数比较都是假，``max(0, min(w, nan))``
      不会报错，最后会得到一个看似合法的角点坐标；
    * **没有交集就丢**：早先版本把宽度/高度强行抬到 1，于是一个整框在右下
      图外的检测结果会变成"图片右下角那 1 个像素"—— 调用方照着它点，点的是
      边框，还以为是自己的匹配错了。
    """
    try:
        x1 = float(it["x1"])
        y1 = float(it["y1"])
        x2 = float(it["x2"])
        y2 = float(it["y2"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
        return None
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1

    # 先判交集：这一步必须在取整之前，否则负数会被夹成 0、右边界被夹成 width，
    # 反而"制造"出一个合法框。
    if x2 <= 0 or y2 <= 0 or x1 >= width or y1 >= height:
        return None

    left = max(0.0, min(float(width), x1))
    top = max(0.0, min(float(height), y1))
    right = max(0.0, min(float(width), x2))
    bottom = max(0.0, min(float(height), y2))

    bx = int(round(left))
    by = int(round(top))
    bw = int(round(right)) - bx
    bh = int(round(bottom)) - by

    # 亚像素级交集：框确实在图内，但取整后不足 1px。抬到 1px 是合理的；上面
    # 已经把"图外"剔掉了，所以这里抬出来的 1px 一定落在图内。
    if bw < 1:
        bw = 1
    if bh < 1:
        bh = 1
    if bx >= width:
        bx = width - 1
    if by >= height:
        by = height - 1
    bw = min(bw, width - bx)
    bh = min(bh, height - by)
    if bw < 1 or bh < 1:
        return None
    return {"x": bx, "y": by, "width": bw, "height": bh}


def _confidence(it: dict) -> float:
    """置信度：有 OCR 分数就用它，否则用检测分数。"""
    score = it.get("text_score")
    try:
        if score is not None and float(score) > 0:
            return round(float(score), 4)
    except (TypeError, ValueError):
        pass
    try:
        return round(float(it.get("conf") or 0.0), 4)
    except (TypeError, ValueError):
        return 0.0


def _match(it: dict, width: int, height: int) -> dict | None:
    box = _clip_box(it, width, height)
    if box is None:
        return None
    text = it.get("text")
    return {
        "text": text if isinstance(text, str) else "",
        "confidence": _confidence(it),
        "box": box,
        # 中心点由**裁剪后**的框算：如果框有一半在图外，中心仍须落在图内，
        # 否则调用方会拿一个负坐标去点击。
        "center": {
            "x": box["x"] + box["width"] // 2,
            "y": box["y"] + box["height"] // 2,
        },
    }


def _run_engine(engine, bgr, req: dict, text_mode: str) -> list[dict]:
    """跑一次引擎，把异常翻成稳定错误码。"""
    try:
        result = engine.run(
            bgr,
            conf=req["conf"],
            iou=0.5,
            tiled=req["tiled"],
            overlap=0.15,
            text=True,
            text_mode=text_mode,
            smart=True,
            # 图片左上角就是坐标原点。这里**不能**带屏幕 origin。
            origin=(0, 0),
        )
    except TimeoutError as exc:
        raise ImageError(
            "busy",
            f"检测/OCR 超时：{exc}",
            status=503,
            hint="服务正在处理别的请求，稍后重试。",
        ) from None
    except Exception as exc:  # noqa: BLE001 - 引擎失败要转成稳定错误码
        raise ImageError(
            "engine_error",
            f"{type(exc).__name__}: {exc}",
            status=500,
        ) from None
    return result.get("elements") or []


def locate_image(payload: object, engine) -> dict:
    """校验 + 解码 + 跑现有引擎 + 按模式整理结果。

    ``engine`` 必须是 ``server.Engine`` 那样的对象（``.run(...)``）。传进来
    而不是在这里 import server：那样会顺带把抓屏模块拖进这条"无桌面"路径。
    """
    req = _validate(payload)
    width, height = req["width"], req["height"]
    bgr = _decode_image(req["image_b64"], width, height)

    elements = _run_engine(engine, bgr, req, "mosaic")
    if req["mode"] == "locate":
        # **动手前比只查时更严**：绝不接受 contains 档。
        # 这条契约的消费者（Host 桥接）拿到 matches[0] 就去点，和 `/act`
        # 同级；实测过一次掉到 contains 档点中任务栏上另一个窗口、屏幕确实
        # 变了、还回了 verified:true。宁可报"没找到"。
        picked = find(elements, req["text"], req["limit"], min_tier="starts")
        if not picked:
            # mosaic 只 OCR **检测框内**的裁片：检测器漏了框，那段文字根本
            # 没进 OCR —— 冒烟里白底黑字的按钮被框住了，但裁片 OCR 读不出
            # 来，全图 OCR 却一次命中。只有马上要动手的路径值得付这份代价，
            # 而这条路径正是。代价只在"没找到"时支付，找到就只跑一次。
            picked = find(_run_engine(engine, bgr, req, "full"),
                          req["text"], req["limit"], min_tier="starts")
    else:
        picked = list(elements)[: req["limit"]]

    matches: list[dict] = []
    for it in picked:
        m = _match(it, width, height)
        if m is not None:
            matches.append(m)
    matches = matches[: req["limit"]]

    return {
        "ok": True,
        "contractVersion": managed.CONTRACT_VERSION,
        "coordinateSpace": "image-pixels",
        "image": {"width": width, "height": height},
        # 空匹配也是成功：没找到 ≠ 出错。调用方据此决定要不要换个 query，
        # 而不是去查服务日志。
        "matches": matches,
    }


def parse_body(raw: bytes) -> dict:
    """HTTP 层用：bytes → dict，JSON 错误单独成码。"""
    if not raw:
        raise ImageError("bad_json", "请求体为空")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ImageError("bad_json", f"请求体不是合法 JSON：{exc}") from None
