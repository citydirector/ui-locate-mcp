"""纯图片比较：两张给定图片 → 真实像素是否变了。

**为什么不能比字节**：PNG 是带元数据的有损编码容器 —— 同一块像素，换一个
编码器、加一条 ``tEXt``、改一下压缩级别，字节就完全不同；反过来，两次
"看起来一样"的截图也可能因为编码器不同而字节相等（罕见，但存在）。用字节
相等当"动作生效"的证据，等于用"文件有没有变"替代"屏幕有没有变"，前者会被
时间戳、EXIF、重编码轻易骗过。

所以这里只做一件事：**解码两张图，逐像素比**。不跑检测、不跑 OCR、不抓屏、
不读本地路径、不注入输入 —— 这条路径连 ``Engine`` 都不 import，托管模式下
它和 ``/locate-image`` 一样可用。

尺寸不同怎么办
--------------
**不判"变了"，也不判"没变"**：尺寸不同说明这两张图根本不可比（换了分辨率、
换了区域、换了缩放），此时 ``changed`` 是 ``null``，``comparable`` 是
``false``。把不可比当成"成功"是这套接口最危险的退化 —— 调用方会就此认定
某个点击生效了；当成"失败"又会诱导它重试。让它自己看 ``comparable``。

为什么不给容差
--------------
这条接口的职责是**给证据**，不是替调用方下判断。加一个 ``tolerance`` 会把
"改了多少像素"藏进一个布尔里，然后调用方开始争论该设多少。现在它拿到的是
``changedPixels`` / ``totalPixels`` / ``changedRatio`` / ``maxChannelDelta``
/ ``changedBox``，判断权在它手里，且证据可复现（同一对图永远同样的数字）。
"""

from __future__ import annotations

import cv2
import numpy as np

from ui_locate import managed
from ui_locate.image_api import (
    MAX_PIXELS,
    MAX_SIDE,
    ImageError,
    _as_int,
    _decode_image,
    parse_body,
)

__all__ = ["ALLOWED_FIELDS", "SIDE_FIELDS", "compare_images", "parse_body"]

# 顶层只认这两项。多一个就 400 —— 允许 ``path`` / ``monitor`` 之类的字段
# 等于允许调用方指望我们"顺手"读一下本地文件，那正是要堵死的路。
ALLOWED_FIELDS = frozenset({"before", "after"})

# 每一侧只认这三项：图片、以及解释它坐标系的尺寸。
SIDE_FIELDS = frozenset({"image_b64", "width", "height"})

_SIDES = ("before", "after")


def _unknown_top(payload: dict) -> None:
    unknown = sorted(set(payload) - ALLOWED_FIELDS)
    if unknown:
        raise ImageError(
            "unsupported_field",
            f"不支持的字段：{', '.join(unknown)}",
            field=unknown[0],
            hint=(
                "本接口只比较请求里给的两张图片（before / after），不接受 path / "
                "monitor / window / screen 之类的隐式本地读取。"
            ),
        )


def _check_side(name: str, side: object) -> tuple[str, int, int]:
    """校验一侧的结构与声明尺寸（不碰图片字节）。"""
    if not isinstance(side, dict):
        raise ImageError(
            "bad_request",
            f"{name} 必须是含 image_b64 / width / height 的对象（收到 "
            f"{type(side).__name__}）",
            field=name,
        )

    unknown = sorted(set(side) - SIDE_FIELDS)
    if unknown:
        raise ImageError(
            "unsupported_field",
            f"{name} 不支持的字段：{', '.join(unknown)}",
            field=f"{name}.{unknown[0]}",
            hint="每一侧只接受 image_b64、width、height。",
        )

    # 复用 image_api 的整数判定（它拒绝 bool），但把字段名带上这一侧，
    # 否则调用方看不出是 before 还是 after 写错了。
    for key in ("width", "height"):
        try:
            _as_int(side, key)
        except ImageError as exc:
            raise ImageError(
                exc.code, f"{name}: {exc.message}", field=f"{name}.{key}", hint=exc.hint
            ) from None

    width = side["width"]
    height = side["height"]
    if width <= 0 or height <= 0:
        raise ImageError(
            "bad_request",
            f"{name}: width / height 必须是正整数（收到 {width}×{height}）",
            field=f"{name}.{'width' if width <= 0 else 'height'}",
        )
    if width > MAX_SIDE or height > MAX_SIDE:
        raise ImageError(
            "too_large",
            f"{name}: 单边超过 {MAX_SIDE} 像素（收到 {width}×{height}）",
            status=413,
            field=f"{name}.{'width' if width > MAX_SIDE else 'height'}",
        )
    if width * height > MAX_PIXELS:
        raise ImageError(
            "too_large",
            f"{name}: 像素数超过 {MAX_PIXELS}（收到 {width * height}）",
            status=413,
            field=f"{name}.width",
        )

    image_b64 = side.get("image_b64")
    if not isinstance(image_b64, str) or not image_b64.strip():
        raise ImageError(
            "bad_request",
            f"{name}: image_b64 必填，且必须是非空 base64 字符串",
            field=f"{name}.image_b64",
        )
    return image_b64, width, height


def _decode_side(name: str, image_b64: str, width: int, height: int):
    """解码一侧，并把错误里的字段名补充成 ``before.xxx``。"""
    try:
        return _decode_image(image_b64, width, height)
    except ImageError as exc:
        raise ImageError(
            exc.code,
            f"{name}: {exc.message}",
            status=exc.status,
            field=f"{name}.{exc.field}" if exc.field else name,
            hint=exc.hint,
        ) from None


def _changed_box(mask) -> dict | None:
    """变化像素的包围盒；没有任何变化就是 None。"""
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    x1, x2 = int(xs.min()), int(xs.max())
    y1, y2 = int(ys.min()), int(ys.max())
    return {"x": x1, "y": y1, "width": x2 - x1 + 1, "height": y2 - y1 + 1}


def compare_images(payload: object) -> dict:
    """校验 + 解码两侧 + 逐像素比较，返回契约 v1 的证据。

    ``payload`` 是已解析的 JSON 对象。**没有 engine 参数**：这条路径不跑
    检测/OCR，正是它能在托管模式、甚至在模型加载失败时照常工作的原因。
    """
    if not isinstance(payload, dict):
        raise ImageError("bad_request", "请求体必须是 JSON 对象")

    _unknown_top(payload)

    for name in _SIDES:
        if name not in payload:
            raise ImageError(
                "bad_request",
                f"缺少 {name}",
                field=name,
                hint="before / after 各需要 {image_b64, width, height}。",
            )

    sides: dict[str, tuple[str, int, int]] = {}
    for name in _SIDES:
        sides[name] = _check_side(name, payload[name])

    # 两侧分别解码：任何一侧不合法都要说清是哪一侧、错在哪一层。
    before = _decode_side("before", *sides["before"])
    after = _decode_side("after", *sides["after"])

    bw, bh = sides["before"][1], sides["before"][2]
    aw, ah = sides["after"][1], sides["after"][2]

    if (bw, bh) != (aw, ah):
        # 不可比：既不报"变了"，也不报"没变"。200 是因为两张图本身都合法，
        # 不可比是**比较的结论**，不是请求错误。
        return {
            "ok": True,
            "contractVersion": managed.CONTRACT_VERSION,
            "comparable": False,
            "changed": None,
            "changedPixels": None,
            "totalPixels": None,
            "changedRatio": None,
            "maxChannelDelta": None,
            "changedBox": None,
            # 没有可比尺寸可言：null，而不是随便挑一侧的尺寸充数。
            "image": None,
            "before": {"width": bw, "height": bh},
            "after": {"width": aw, "height": ah},
            "reason": "dimension_mismatch",
            "hint": (
                "两张图尺寸不同，无法逐像素比较；这不代表动作成功。"
                "请在同一尺寸、同一区域下重截 before / after。"
            ),
        }

    diff = cv2.absdiff(before, after)
    mask = diff.any(axis=2)
    changed_pixels = int(np.count_nonzero(mask))
    total = int(bw) * int(bh)

    return {
        "ok": True,
        "contractVersion": managed.CONTRACT_VERSION,
        "comparable": True,
        # 一个像素都不一样才算"没变"。判断阈值留给调用方 —— 见模块文档。
        "changed": changed_pixels > 0,
        "changedPixels": changed_pixels,
        "totalPixels": total,
        "changedRatio": round(changed_pixels / total, 6) if total else 0.0,
        "maxChannelDelta": int(diff.max()) if diff.size else 0,
        "changedBox": _changed_box(mask),
        # 可比时两图尺寸一致，这里给一处即可 —— 调用方不必自己去挑。
        "image": {"width": bw, "height": bh},
        "before": {"width": bw, "height": bh},
        "after": {"width": aw, "height": ah},
    }
