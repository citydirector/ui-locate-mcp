"""看一小段：连抓几秒，挑出关键帧，只把「变了的地方」拼成一张图交给模型。

很多操作事先不知道会发生什么（拖一下地图会不会动、按一下技能有没有冷却、
怪往哪边走）。最省事的做法是「录几秒看一看，再动手」，循环下去。但把几秒视频
原样交给视觉模型太贵：5fps × 3 秒就是 15 张整屏图。

这里的做法：

1. 按 ``fps`` 连抓 ``duration_ms``（dxgi 一帧 2~3ms，PrintWindow ~28ms）。
2. 网格差分（griddiff）挑关键帧：和上一张关键帧相比变化格够多才算新关键帧。
   画面不动 → 只有第一帧。
3. 先扫一遍相邻帧，找出「几乎每一帧都在变」的格子（水面、粒子、时钟）——
   这是环境噪声，挑关键帧和报区域时都不计，免得每一帧都被当成关键帧。
4. 拼图（``mode="regions"``，默认）：第一帧缩小成全景，之后每个关键帧只贴
   **变化区域的裁片**，标上时间和屏幕坐标。模型看一张图就知道「什么时候、
   哪里、变成了什么样」。变化面积太大（切场景）的关键帧改贴整帧缩略图。
   ``mode="full"``：所有关键帧整帧缩略图平铺。

全部是纯函数加注入的 grab / sleep / clock，不碰屏幕也能测。
"""

from __future__ import annotations

import time
from typing import Any, Callable

import cv2
import numpy as np

from ui_locate import griddiff

MAX_DURATION_MS = 15000
MAX_FPS = 15
# 相邻帧里变化次数占比超过它的格子算「环境在动」
AMBIENT_FRAC = 0.6
# 某个关键帧变化面积超过画面的这个比例，拼图里贴整帧而不是裁片
FULLFRAME_FRAC = 0.35
SHEET_W = 1280
LABEL_H = 18
PAD = 6


def record(
    grab: Callable[[], tuple[np.ndarray, tuple[int, int]]],
    *,
    duration_ms: int = 3000,
    fps: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[tuple[float, np.ndarray, tuple[int, int]]]:
    """按固定节拍抓帧。返回 [(相对毫秒, bgr, origin)]，至少一帧。"""
    duration_ms = max(0, min(int(duration_ms), MAX_DURATION_MS))
    fps = max(0.5, min(float(fps), MAX_FPS))
    interval = 1.0 / fps
    frames: list[tuple[float, np.ndarray, tuple[int, int]]] = []
    t0 = clock()
    while True:
        t = clock()
        bgr, origin = grab()
        frames.append((round((t - t0) * 1000, 1), bgr, tuple(origin)))
        if (clock() - t0) * 1000 >= duration_ms:
            break
        wait = t0 + len(frames) * interval - clock()
        if wait > 0:
            sleep(wait)
    return frames


def _shrink_factor(w: int) -> int:
    return 4 if w >= 2560 else 2 if w >= 1280 else 1


def _small(img: np.ndarray, f: int) -> np.ndarray:
    if f == 1:
        return img
    return cv2.resize(img, (img.shape[1] // f, img.shape[0] // f), interpolation=cv2.INTER_AREA)


def _scale_regions(regions: list[dict], f: int, origin: tuple[int, int]) -> list[dict]:
    ox, oy = origin
    out = []
    for r in regions:
        x, y, w, h = r["x"] * f, r["y"] * f, r["w"] * f, r["h"] * f
        out.append({**r, "x": x + ox, "y": y + oy, "w": w, "h": h,
                    "cx": x + w // 2 + ox, "cy": y + h // 2 + oy})
    return out


def analyze(
    frames: list[tuple[float, np.ndarray, tuple[int, int]]],
    *,
    cell: int = griddiff.DEFAULT_CELL,
    threshold: float = griddiff.DEFAULT_THRESHOLD,
    min_frac: float = 0.002,
    max_keyframes: int = 6,
    max_regions: int = 6,
) -> dict:
    """挑关键帧。返回 {keyframes:[{index, t_ms, changed_frac, regions}], ambient, ...}。

    差分在缩小图上做（1080p 缩一半、4K 缩到四分之一，格子跟着缩），坐标再放大回去：
    16 帧 1080p 从 1.2 秒降到一两百毫秒。每个关键帧的 regions 都是相对**上一个
    保留下来的**关键帧，拼图里看到的就是两张之间的变化。
    尺寸或原点中途变了（窗口被拖动、改分辨率）的帧当作新关键帧，regions 为整帧。
    """
    if not frames:
        return {"frames": 0, "keyframes": [], "static": True}
    origin = frames[0][2]
    f = _shrink_factor(frames[0][1].shape[1])
    c = max(4, cell // f)
    small = [_small(img, f) for _, img, _ in frames]

    def same(i: int, j: int) -> bool:
        return small[i].shape == small[j].shape and frames[i][2] == frames[j][2]

    # 第一遍：相邻帧，统计每格变化次数 → 环境噪声
    activity = None
    pairs = 0
    for i in range(1, len(frames)):
        if not same(i - 1, i):
            continue
        m = griddiff.cell_scores(small[i - 1], small[i], c) > threshold
        if activity is not None and activity.shape != m.shape:
            continue
        activity = m.astype(np.int32) if activity is None else activity + m
        pairs += 1
    ambient = None
    if activity is not None and pairs >= 3:
        ambient = activity >= max(2, int(np.ceil(pairs * AMBIENT_FRAC)))
        ambient = griddiff.dilate(ambient) if ambient.any() else None

    def compare(i: int, j: int) -> dict:
        """帧 j 相对帧 i。"""
        t, img, org = frames[j]
        if not same(i, j):
            h, w = img.shape[:2]
            return {"index": j, "t_ms": t, "changed_frac": 1.0, "changed_cells": 10 ** 9,
                    "resized": True,
                    "regions": [{"x": org[0], "y": org[1], "w": w, "h": h,
                                 "cx": org[0] + w // 2, "cy": org[1] + h // 2,
                                 "cells": -1, "strength": 255.0}]}
        d = griddiff.diff(small[i], small[j], cell=c, threshold=threshold,
                          max_regions=max_regions, ignore=ambient)
        return {"index": j, "t_ms": t, "changed_frac": d["changed_frac"],
                "changed_cells": d["changed_cells"],
                "regions": _scale_regions(d["regions"], f, org)}

    # 第二遍：和上一张关键帧比
    keys: list[dict] = [{"index": 0, "t_ms": frames[0][0], "changed_frac": 0.0,
                         "changed_cells": 0, "regions": []}]
    for j in range(1, len(frames)):
        k = compare(keys[-1]["index"], j)
        total = small[j].shape[0] // c * (small[j].shape[1] // c) or 1
        if k.get("resized") or k["changed_cells"] >= max(1, int(np.ceil(min_frac * total))):
            keys.append(k)

    total_keys = len(keys)
    if len(keys) > max_keyframes:
        # 第一帧、最后一张（最终状态）必留；中间一半给变化最大的，一半按时间均匀铺开
        middle = keys[1:-1]
        slots = max(0, max_keyframes - 2)
        big = sorted(middle, key=lambda k: -k["changed_cells"])[: slots // 2 + slots % 2]
        rest = [k for k in middle if k not in big]
        n_even = slots - len(big)
        even = [rest[int(round((i + 0.5) * len(rest) / n_even - 0.5))] for i in range(n_even)] \
            if n_even and rest else []
        chosen = sorted({k["index"] for k in big + even})
        idx = [0] + chosen + [keys[-1]["index"]]
        # regions 改成相对上一个保留的关键帧
        keys = [keys[0]] + [compare(a, b) for a, b in zip(idx, idx[1:])]

    for k in keys:
        if k.get("changed_cells") == 10 ** 9:
            k["changed_cells"] = -1

    out: dict = {
        "frames": len(frames),
        "duration_ms": frames[-1][0],
        "fps": round((len(frames) - 1) / (frames[-1][0] / 1000), 2) if frames[-1][0] > 0 else None,
        "origin": list(origin),
        "size": [frames[0][1].shape[1], frames[0][1].shape[0]],
        "keyframes": keys,
        "keyframes_total": total_keys,
        "static": total_keys == 1,
    }
    if ambient is not None:
        out["ambient"] = {"cells_frac": round(float(ambient.mean()), 4),
                          "regions": _scale_regions(_mask_regions(ambient, (0, 0), c), f, origin)}
    return out


def _mask_regions(mask: np.ndarray, origin: tuple[int, int], cell: int, limit: int = 6) -> list[dict]:
    ox, oy = origin
    regions = []
    for group in griddiff._components(mask):
        rs = [g[0] for g in group]
        cs = [g[1] for g in group]
        x1, y1 = min(cs) * cell, min(rs) * cell
        x2, y2 = (max(cs) + 1) * cell, (max(rs) + 1) * cell
        regions.append({"x": x1 + ox, "y": y1 + oy, "w": x2 - x1, "h": y2 - y1,
                        "cx": (x1 + x2) // 2 + ox, "cy": (y1 + y2) // 2 + oy, "cells": len(group)})
    regions.sort(key=lambda r: -r["cells"])
    return regions[:limit]


# ── 拼图 ────────────────────────────────────────────────────────────────────

def _fit(img: np.ndarray, max_w: int, max_h: int) -> np.ndarray:
    h, w = img.shape[:2]
    s = min(max_w / w, max_h / h, 1.5)   # 小裁片最多放大 1.5 倍：放大不加信息，只占地方
    if abs(s - 1.0) < 1e-3:
        return img
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC
    return cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=interp)


def _label(tile: np.ndarray, text: str, color: tuple[int, int, int]) -> np.ndarray:
    w = max(tile.shape[1], 8 * len(text) + 8)
    out = np.full((tile.shape[0] + LABEL_H, w, 3), 32, np.uint8)
    out[LABEL_H:, :tile.shape[1]] = tile
    cv2.putText(out, text, (4, LABEL_H - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
    return out


_COLORS = [(0, 220, 255), (80, 255, 80), (255, 160, 60), (255, 80, 255), (80, 160, 255), (200, 200, 200)]


def merge_close(regions: list[dict], gap: int) -> list[dict]:
    """相距不到 gap 的区域并成一个：东西从 A 挪到 B 时，两处合成一张裁片，一眼看出走向。"""
    boxes = [[r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"]] for r in regions]
    changed = True
    while changed:
        changed = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a[0] - gap < b[2] and b[0] - gap < a[2] and a[1] - gap < b[3] and b[1] - gap < a[3]:
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del boxes[j]
                    changed = True
                    break
            if changed:
                break
    return [{"x": b[0], "y": b[1], "w": b[2] - b[0], "h": b[3] - b[1]} for b in boxes]


def tiles_for(frames: list, info: dict, mode: str = "regions", pad: int = 16) -> list[np.ndarray]:
    """每个关键帧 → 若干带标签的小图。"""
    tiles: list[np.ndarray] = []
    keys = info.get("keyframes") or []
    for n, k in enumerate(keys):
        t, img, org = frames[k["index"]]
        color = _COLORS[n % len(_COLORS)]
        tag = f"K{n} t={t / 1000:.1f}s"
        whole = n == 0 or mode == "full" or k.get("resized") \
            or k["changed_frac"] >= FULLFRAME_FRAC or not k["regions"]
        if whole:
            tile = _fit(img, SHEET_W if n == 0 and mode != "full" else SHEET_W // 2 - PAD,
                        720 if n == 0 and mode != "full" else 360)
            tiles.append(_label(tile, tag + (" full" if n else " start"), color))
            continue
        H, W = img.shape[:2]
        for r in merge_close(k["regions"], gap=max(96, pad * 4))[:4]:
            x1 = max(0, r["x"] - org[0] - pad)
            y1 = max(0, r["y"] - org[1] - pad)
            x2 = min(W, r["x"] - org[0] + r["w"] + pad)
            y2 = min(H, r["y"] - org[1] + r["h"] + pad)
            if x2 - x1 < 2 or y2 - y1 < 2:
                continue
            crop = _fit(img[y1:y2, x1:x2], 420, 300)
            tiles.append(_label(crop, f"{tag} @{x1 + org[0]},{y1 + org[1]} {x2 - x1}x{y2 - y1}", color))
    return tiles


def contact_sheet(tiles: list[np.ndarray], width: int = SHEET_W) -> np.ndarray:
    """小图按行流式排开（行高取该行最高的）。"""
    if not tiles:
        return np.full((32, 32, 3), 32, np.uint8)
    rows: list[list[np.ndarray]] = [[]]
    x = 0
    for t in tiles:
        w = min(t.shape[1], width)
        if rows[-1] and x + w > width:
            rows.append([])
            x = 0
        rows[-1].append(t[:, :width])
        x += w + PAD
    height = sum(max(t.shape[0] for t in r) for r in rows) + PAD * (len(rows) + 1)
    sheet = np.full((height, width, 3), 16, np.uint8)
    y = PAD
    for r in rows:
        x = 0
        for t in r:
            sheet[y:y + t.shape[0], x:x + t.shape[1]] = t
            x += t.shape[1] + PAD
        y += max(t.shape[0] for t in r) + PAD
    return sheet


def watch(
    grab: Callable[[], tuple[np.ndarray, tuple[int, int]]],
    *,
    duration_ms: int = 3000,
    fps: float = 5.0,
    mode: str = "regions",
    max_keyframes: int = 6,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    **analyze_kw: Any,
) -> tuple[dict, np.ndarray, list]:
    """录 → 挑关键帧 → 拼图。返回 (info, 拼图, 原始帧)。"""
    t0 = time.perf_counter()
    frames = record(grab, duration_ms=duration_ms, fps=fps, sleep=sleep, clock=clock)
    t1 = time.perf_counter()
    info = analyze(frames, max_keyframes=max_keyframes, **analyze_kw)
    t2 = time.perf_counter()
    sheet = contact_sheet(tiles_for(frames, info, mode=mode))
    t3 = time.perf_counter()
    info["mode"] = mode
    info["timings"] = {"record_ms": round((t1 - t0) * 1000, 1),
                       "analyze_ms": round((t2 - t1) * 1000, 1),
                       "render_ms": round((t3 - t2) * 1000, 1)}
    return info, sheet, frames
