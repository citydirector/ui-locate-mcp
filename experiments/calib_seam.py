"""验证「切缝对齐」是否真的把切缝挪到了元素之间的空隙。

做法：造一张在纯几何切缝位置正好放一个元素的图，看 smart 模式是否把切缝
移到元素之外。对照组 smart=False（纯几何网格）。

判据（这里踩过一次坑）：要检查**实际切缝位置** xb[i]，而不是 tile 的 x0/x1。
tile 的 x0 = xb[i] - ox，带重叠量，用它判断会得出错误结论。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui_locate.tiled import _uniform_axis, plan_tiles  # noqa: E402


def make_image(w: int, h: int, split_x: int) -> np.ndarray:
    """在 split_x 居中放一个高对比"按钮"，其余留白背景。"""
    img = np.full((h, w, 3), 90, dtype=np.uint8)
    bw, bh = 160, 80
    x0 = max(0, split_x - bw // 2)
    y0 = h // 2 - bh // 2
    cv2.rectangle(img, (x0, y0), (x0 + bw, y0 + bh), (240, 240, 240), -1)
    cv2.rectangle(img, (x0, y0), (x0 + bw, y0 + bh), (30, 30, 30), 3)
    return img


def seams_of(bgr: np.ndarray, w: int, h: int, max_side: int, overlap: float) -> list[int]:
    """复用 plan_tiles 的对齐逻辑，但直接拿切缝位置而不是 tile 边界。"""
    plan = plan_tiles(w, h, max_side, overlap, bgr=bgr, min_edge_ratio=0.0)
    # plan 里拿不到 xb，重新算一遍对齐后的切缝
    xb, ox = _uniform_axis(w, max_side, overlap)
    from ui_locate.tiled import _ink_profile, _snap

    col_frac, _ = _ink_profile(bgr)
    for i in range(1, len(xb) - 1):
        xb[i] = _snap(xb[i], col_frac, ox, xb[i - 1] + 1, xb[i + 1] - 1)
    _ = plan
    return xb


def straddles(seams: list[int], span: tuple[int, int], margin: int = 4) -> list[int]:
    """返回落在 span 内部（不贴边）的切缝。"""
    return [x for x in seams if span[0] + margin < x < span[1] - margin]


def main() -> None:
    # 用一块 16:10 的高分屏做样本，尺寸本身不重要，只要够大、能切出多条缝
    w, h, max_side, overlap = 2560, 1600, 960, 0.15

    xb0, _ox = _uniform_axis(w, max_side, overlap)
    print(f"纯几何切缝: {xb0}")

    # 把元素放在第一条内部切缝正中
    seam_x = xb0[1]
    bw = 160
    elem = (seam_x - bw // 2, seam_x + bw // 2)
    img = make_image(w, h, seam_x)
    print(f"元素横向区间: {elem}（中心在切缝 x={seam_x} 上）")

    geo = plan_tiles(w, h, max_side, overlap, bgr=None)
    smart_seams = seams_of(img, w, h, max_side, overlap)
    smart = plan_tiles(w, h, max_side, overlap, bgr=img)

    g_bad = straddles(xb0, elem)
    s_bad = straddles(smart_seams, elem)

    print(f"\n纯几何: 切缝 {xb0}")
    print(f"  穿过元素: {g_bad}  → {len(g_bad)} 条")
    print(f"\nsmart : 切缝 {smart_seams}")
    print(f"  穿过元素: {s_bad}  → {len(s_bad)} 条")
    print(f"  切块 {len(smart.tiles)} 块，跳过空白 {smart.skipped} 块")

    shift = [b - a for a, b in zip(xb0, smart_seams)]
    print(f"\n切缝位移: {shift}")

    ok = len(s_bad) == 0
    print(f"\n结论: {'OK 切缝成功避开元素' if ok else 'FAIL 仍有切缝穿过元素'}")


if __name__ == "__main__":
    main()
