"""把检测框裁片拼成一张大图，一次 OCR，再按布局反查每个元素。

为什么这样做：
  全屏 1920x1080 直接 OCR 要 1.6 秒，因为检测器负责的只是"哪里可能有元素"，
  而 OCR 却把整屏都扫一遍——大片空白、纯色背景、被检测器已排除的区域都白扫了。
  逐框单独 OCR 更糟：12 个框要 1389ms，每次调用的固定开销远大于识别本身。
  折中方案：把所有裁片按网格拼到一张图上，一次 OCR 拿全部文字，
  再按拼图时的 (x, y) 偏移把结果映射回原图坐标。

收益实测（1920x1080，约 130 个元素）：1600ms → 250ms 量级。
代价：拼图里的裁片必须留边距，且不能缩放太狠（否则小字认不出）。
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ui_locate.ocr_match import OcrLine

PAD = 6          # 裁片四周留白，避免相邻文字粘连
GAP = 10         # 裁片之间间隙
EDGE_PAD = 4     # 裁片向外扩张的像素，把检测框边缘被切掉的字捡回来
SHEET_W = 1200   # 单页拼图宽度（实测 OCR 在 1200 宽时最快）
SHEET_MAX_H = 900  # 单页高度上限，超了就分页
MIN_H = 18       # 裁片高度不足时补到这个值，否则小按钮的字太扁


@dataclass
class Slot:
    """一个裁片在拼图里的位置，以及它对应的原图区域。"""

    item_index: int
    sheet_x: int      # 裁片左上角在拼图中的坐标
    sheet_y: int
    w: int            # 裁片宽高
    h: int
    src_x: int        # 裁片左上角在原图中的坐标
    src_y: int


def _crop_box(
    it: dict, img_w: int, img_h: int, edge_pad: int
) -> tuple[int, int, int, int] | None:
    """取裁片在原图中的区域，向外扩 edge_pad 像素。

    为什么外扩：检测框常常贴着字形边缘，正好把第一个字或最后一笔切掉。
    实测「搜索」这个按钮在 pad=0 时认不出，pad=4 就认出来了；
    而 pad 再加大（22px）就会把相邻元素的字也带进来，误配增多。
    """
    x1 = max(0, int(it["x1"]) - edge_pad)
    y1 = max(0, int(it["y1"]) - edge_pad)
    x2 = min(img_w, int(np.ceil(it["x2"])) + edge_pad)
    y2 = min(img_h, int(np.ceil(it["y2"])) + edge_pad)
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _layout(
    items: list[dict],
    scale: float,
    img_w: int,
    img_h: int,
    edge_pad: int = EDGE_PAD,
) -> tuple[int, int, list[Slot], np.ndarray]:
    """算出拼图布局并画出拼图本体。"""
    slots: list[Slot] = []
    x = y = 0
    row_h = 0

    for i, it in enumerate(items):
        box = _crop_box(it, img_w, img_h, edge_pad)
        if box is None:
            continue
        x1, y1, x2, y2 = box

        w = max(1, int(round((x2 - x1) * scale)))
        h = max(MIN_H, int(round((y2 - y1) * scale)))

        if x + w + PAD * 2 > SHEET_W and x > 0:
            x = 0
            y += row_h + GAP
            row_h = 0

        slots.append(
            Slot(
                item_index=i,
                sheet_x=x + PAD,
                sheet_y=y + PAD,
                w=w,
                h=h,
                src_x=x1,
                src_y=y1,
            )
        )
        x += w + PAD * 2 + GAP
        row_h = max(row_h, h + PAD * 2)

    sheet_h = y + row_h + GAP if slots else 1
    canvas = np.full((max(1, sheet_h), SHEET_W, 3), 255, dtype=np.uint8)
    return SHEET_W, sheet_h, slots, canvas


def paginate(items: list[dict], scale: float, img_w: int, img_h: int) -> list[list[dict]]:
    """把元素按顺序切成若干页。

    ⚠ 实测这条路线是错的，保留仅供对照：分页后每页都要付一次 OCR 的
    固定开销，页数越多越慢。1920x1080 桌面 49 个元素实测——
      单页 1200x569  → 579 ms
      6 页（max_h=400）→ 4948 ms
      4 页（max_h=650）→ 2921 ms
    所以默认走单页 build_mosaic，不要分页。
    """
    pages: list[list[dict]] = []
    cur: list[dict] = []
    cur_h = 0
    row_h = 0

    for it in items:
        h = max(MIN_H, int(round((it["y2"] - it["y1"]) * scale)))
        w = max(1, int(round((it["x2"] - it["x1"]) * scale)))
        w = min(w, SHEET_W - PAD * 2)

        need_h = h + PAD * 2
        fits_row = (
            cur
            and _row_width(cur, scale, SHEET_W) + w + PAD * 2 + GAP <= SHEET_W
        )
        if not fits_row:
            if cur and cur_h + need_h + GAP > SHEET_MAX_H:
                pages.append(cur)
                cur, cur_h, row_h = [], 0, 0
            cur_h += need_h + GAP
            row_h = need_h

        cur.append(it)

    if cur:
        pages.append(cur)
    return pages


def _row_width(items: list[dict], scale: float, limit: int) -> int:
    """粗估一批元素横排的总宽度，用于判断换行。"""
    total = 0
    for it in items:
        w = max(1, int(round((it["x2"] - it["x1"]) * scale)))
        total += min(w, limit - PAD * 2) + PAD * 2 + GAP
    return total


def build_mosaic(
    bgr: np.ndarray,
    items: list[dict],
    scale: float = 1.2,
    edge_pad: int = EDGE_PAD,
) -> tuple[np.ndarray, list[Slot], float]:
    """把 items 的裁片拼成一张图。返回 (拼图, 槽位表, 实际缩放比)。"""
    img_h, img_w = bgr.shape[:2]
    sheet_w, sheet_h, slots, canvas = _layout(items, scale, img_w, img_h, edge_pad)

    for s in slots:
        it = items[s.item_index]
        box = _crop_box(it, img_w, img_h, edge_pad)
        if box is None:
            continue
        x1, y1, x2, y2 = box
        crop = bgr[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        try:
            resized = cv2.resize(
                crop, (s.w, s.h), interpolation=cv2.INTER_CUBIC
            )
        except cv2.error:
            continue
        # 拼图里贴的位置不能超出画布
        ey, ex = min(sheet_h, s.sheet_y + s.h), min(sheet_w, s.sheet_x + s.w)
        if ey <= s.sheet_y or ex <= s.sheet_x:
            continue
        canvas[s.sheet_y:ey, s.sheet_x:ex] = resized[: ey - s.sheet_y, : ex - s.sheet_x]

    return canvas, slots, scale


def map_back(lines: list[OcrLine], slots: list[Slot], scale: float) -> list[OcrLine]:
    """把拼图坐标系里的文字映射回原图坐标系。

    每条 OCR 文字归给它中心点所在的裁片；落到裁片外（跨片、或拼图留白）
    就按最近裁片算，宁可偏移几像素也不丢字。
    """
    out: list[OcrLine] = []
    for line in lines:
        best: Slot | None = None
        best_d = float("inf")
        for s in slots:
            if (
                s.sheet_x <= line.cx <= s.sheet_x + s.w
                and s.sheet_y <= line.cy <= s.sheet_y + s.h
            ):
                best = s
                break
            dx = max(s.sheet_x - line.cx, 0, line.cx - (s.sheet_x + s.w))
            dy = max(s.sheet_y - line.cy, 0, line.cy - (s.sheet_y + s.h))
            d = dx * dx + dy * dy
            if d < best_d:
                best_d, best = d, s
        if best is None:
            continue

        out.append(
            OcrLine(
                text=line.text,
                score=line.score,
                x1=best.src_x + (line.x1 - best.sheet_x) / scale,
                y1=best.src_y + (line.y1 - best.sheet_y) / scale,
                x2=best.src_x + (line.x2 - best.sheet_x) / scale,
                y2=best.src_y + (line.y2 - best.sheet_y) / scale,
            )
        )
    return out
