"""把检测到的元素裁出来拼成带编号的总览图，供视觉模型（我）一次性选号。

为什么需要这个：
  检测器只给出「那里有个框」，不知道框里是什么。要判断"哪个是保存按钮"，
  必须读框里的文字/图标。与其让每个框单独截图逐张看（N 次往返），
  不如拼成一张图，一次看完直接报编号，坐标立刻从 JSON 里取到精确值。

用法：
  uv run python -m ui_locate.annotate --image shot.png --json out.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

TILE_H = 30          # 每个裁片统一缩放到这个高度
TILE_MAX_W = 220     # 裁片最大宽度，超出则等比压缩
SHEET_W = 1400       # 拼图画布宽度
GAP = 6              # 裁片间距
PAD_TOP = 16         # 裁片上方留给编号的空间
BG = (250, 250, 250)


def _scaled(crop: np.ndarray) -> np.ndarray:
    """等比缩放到高度 TILE_H；若宽度超过 TILE_MAX_W 则再按宽度压回。"""
    h, w = crop.shape[:2]
    if h == 0 or w == 0:
        return np.full((TILE_H, 40, 3), BG, dtype=np.uint8)

    scale = TILE_H / h
    if w * scale > TILE_MAX_W:
        scale = TILE_MAX_W / w
    nw = max(8, int(round(w * scale)))
    nh = max(8, int(round(h * scale)))
    return cv2.resize(crop, (nw, nh), interpolation=cv2.INTER_AREA)


def build_sheet(
    bgr: np.ndarray, items: list[dict], dst: Path, max_per_sheet: int = 70
) -> list[Path]:
    """把 items 裁片拼成若干张总览图，返回写出的文件列表。"""
    h, w = bgr.shape[:2]
    out_paths: list[Path] = []

    for page, start in enumerate(range(0, len(items), max_per_sheet), start=1):
        chunk = items[start : start + max_per_sheet]
        cells: list[tuple[int, np.ndarray, int]] = []

        for idx, it in enumerate(chunk, start=start):
            x1 = max(0, int(it["x1"]))
            y1 = max(0, int(it["y1"]))
            x2 = min(w, int(np.ceil(it["x2"])))
            y2 = min(h, int(np.ceil(it["y2"])))
            if x2 <= x1 or y2 <= y1:
                continue
            crop = bgr[y1:y2, x1:x2]
            cells.append((idx, _scaled(crop), it["conf"]))

        if not cells:
            continue

        # 简单的流式布局：从左到右排，超宽换行
        rows: list[list[tuple[int, np.ndarray]]] = [[]]
        cur_w = 0
        for idx, img, _conf in cells:
            cw = img.shape[1] + GAP
            if cur_w + cw > SHEET_W and rows[-1]:
                rows.append([])
                cur_w = 0
            rows[-1].append((idx, img))
            cur_w += cw

        row_h = TILE_H + PAD_TOP + GAP
        canvas = np.full((max(1, len(rows)) * row_h + GAP, SHEET_W, 3), BG, dtype=np.uint8)

        for r, row in enumerate(rows):
            x = GAP
            y = r * row_h + GAP
            for idx, img in row:
                ih, iw = img.shape[:2]
                y0 = y + PAD_TOP
                if y0 + ih > canvas.shape[0]:
                    break
                canvas[y0 : y0 + ih, x : x + iw] = img
                cv2.rectangle(
                    canvas, (x - 1, y0 - 1), (x + iw, y0 + ih), (200, 200, 200), 1
                )
                cv2.putText(
                    canvas,
                    str(idx),
                    (x, y + 12),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.42,
                    (30, 30, 200),
                    1,
                    cv2.LINE_AA,
                )
                x += iw + GAP

        p = dst.with_name(f"{dst.stem}_p{page}{dst.suffix}")
        cv2.imwrite(str(p), canvas)
        out_paths.append(p)

    return out_paths


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", type=Path, required=True)
    ap.add_argument("--json", type=Path, required=True, help="detect.py 输出的 JSON")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--max", type=int, default=70)
    args = ap.parse_args()

    data = json.loads(args.json.read_text(encoding="utf-8"))
    items = data["elements"]
    bgr = cv2.imread(str(args.image), cv2.IMREAD_COLOR)
    if bgr is None:
        raise SystemExit(f"无法读取图片: {args.image}")

    out = args.out or args.json.with_name(args.json.stem + "_sheet.png")
    paths = build_sheet(bgr, items, out, args.max)
    print(f"elements : {len(items)}")
    for p in paths:
        print(f"sheet    : {p}")


if __name__ == "__main__":
    main()
