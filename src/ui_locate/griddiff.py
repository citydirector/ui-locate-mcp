"""网格差分：两帧之间哪些地方变了 —— 不跑模型、不花视觉 token，几十毫秒。

把画面切成 ``cell`` 像素（默认 32）的方格，每格算一次「平均像素差」，超过阈值
的格子算变了；相邻的变化格并成矩形区域，按面积排序报出来。

用在三处：

1. ``wait_stable``：动作之后等画面停下来再验证，代替固定的 220ms。界面动画
   长短不一，固定值不是白等就是没等够。
2. 验证：``verify="screen"`` 的结果里带上变化区域，并且「点击点附近有一块
   明显变化」也算生效 —— 小按钮变色在 180px 方块的平均差里常常被稀释掉。
3. ``/diff``：问「上次之后屏幕哪里变了」，回几个矩形而不是一张图。

为什么按格子而不是逐像素：游戏里有粒子、闪烁、光标，逐像素差到处是零星噪点；
按格求平均后，零星噪点被摊薄，真正的界面变化（一块面板、一个高亮）依然突出。
"""

from __future__ import annotations

import time
from typing import Any, Callable

import numpy as np

DEFAULT_CELL = 32
# 一格的平均差（每像素取三通道里差最大的那个，0~255）超过它就算变了。
# 实测静止游戏画面相邻两帧大多数格子 < 2，粒子/水面格子 3~6；面板弹出、
# 按钮高亮 15 以上。取 8：滤掉轻微动画，留住真正的界面变化。
DEFAULT_THRESHOLD = 8.0
# 判定「画面停了」时允许零星几格还在动（光标闪烁、角落的小动画）：
# 变化格不超过总格数的这个比例，且不超过 _QUIET_MAX_CELLS 格。
QUIET_FRAC = 0.004
QUIET_MIN_CELLS = 2


def cell_scores(a: np.ndarray, b: np.ndarray, cell: int = DEFAULT_CELL) -> np.ndarray:
    """每格的平均差，形状 (rows, cols)。边上不满一格的部分并进最后一格之外 —— 丢掉。

    为了快先隔点采样（步长 2），4K 一帧也只算 200 万像素。
    """
    if a.shape != b.shape:
        raise ValueError(f"两帧尺寸不同：{a.shape} vs {b.shape}")
    step = 2 if min(a.shape[:2]) >= 4 * cell else 1
    sa = a[::step, ::step]
    sb = b[::step, ::step]
    d = np.abs(sa.astype(np.int16) - sb.astype(np.int16))
    if d.ndim == 3:
        d = d.max(axis=2)
    c = max(1, cell // step)
    rows, cols = d.shape[0] // c, d.shape[1] // c
    if rows == 0 or cols == 0:
        return np.array([[float(d.mean())]], dtype=np.float32)
    d = d[:rows * c, :cols * c].reshape(rows, c, cols, c)
    return d.mean(axis=(1, 3)).astype(np.float32)


def _components(mask: np.ndarray) -> list[list[tuple[int, int]]]:
    """8 连通的变化格分组。格子数最多几千，纯 Python 足够快。"""
    rows, cols = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    groups = []
    for r0, c0 in zip(*np.nonzero(mask)):
        if seen[r0, c0]:
            continue
        stack = [(int(r0), int(c0))]
        seen[r0, c0] = True
        group = []
        while stack:
            r, c = stack.pop()
            group.append((r, c))
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < rows and 0 <= cc < cols and mask[rr, cc] and not seen[rr, cc]:
                        seen[rr, cc] = True
                        stack.append((rr, cc))
        groups.append(group)
    return groups


def diff(
    a: np.ndarray,
    b: np.ndarray,
    *,
    origin: tuple[int, int] = (0, 0),
    cell: int = DEFAULT_CELL,
    threshold: float = DEFAULT_THRESHOLD,
    max_regions: int = 8,
    ignore: np.ndarray | None = None,
) -> dict:
    """两帧比一次。区域坐标是屏幕绝对坐标（图内坐标 + origin）。

    ignore：同形状的布尔格子掩码，为 True 的格子不计（已知在动的地方）。
    """
    scores = cell_scores(a, b, cell)
    mask = scores > threshold
    if ignore is not None and ignore.shape == mask.shape:
        mask &= ~ignore
    total = int(mask.size)
    n = int(mask.sum())
    ox, oy = origin
    regions = []
    for group in _components(mask):
        rs = [g[0] for g in group]
        cs = [g[1] for g in group]
        x1, y1 = min(cs) * cell, min(rs) * cell
        x2, y2 = (max(cs) + 1) * cell, (max(rs) + 1) * cell
        regions.append({
            "x": x1 + ox, "y": y1 + oy, "w": x2 - x1, "h": y2 - y1,
            "cx": (x1 + x2) // 2 + ox, "cy": (y1 + y2) // 2 + oy,
            "cells": len(group),
            "strength": round(float(max(scores[r, c] for r, c in group)), 1),
        })
    regions.sort(key=lambda r: (-r["cells"], -r["strength"]))
    return {
        "changed_cells": n,
        "total_cells": total,
        "changed_frac": round(n / total, 4) if total else 0.0,
        "cell": cell,
        "threshold": threshold,
        "regions": regions[:max_regions],
        "more_regions": max(0, len(regions) - max_regions),
        "_mask": mask,
    }


def public(result: dict) -> dict:
    """去掉内部字段（掩码）再给调用方。"""
    return {k: v for k, v in result.items() if not k.startswith("_")}


def is_quiet(result: dict) -> bool:
    limit = max(QUIET_MIN_CELLS, int(result["total_cells"] * QUIET_FRAC))
    return result["changed_cells"] <= limit


def near(result: dict, x: float, y: float, radius: int) -> dict | None:
    """离 (x, y) 不超过 radius 的最强变化区域（区域与方框相交即可）。"""
    best = None
    for r in result.get("regions", []):
        if (r["x"] <= x + radius and r["x"] + r["w"] >= x - radius
                and r["y"] <= y + radius and r["y"] + r["h"] >= y - radius):
            if best is None or r["strength"] > best["strength"]:
                best = r
    return best


def wait_stable(
    capture: Callable[[], Any],
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout_ms: int = 1500,
    min_ms: int = 60,
    poll_ms: int = 50,
    quiet_frames: int = 2,
    cell: int = DEFAULT_CELL,
    threshold: float = DEFAULT_THRESHOLD,
    unpack: Callable[[Any], tuple[np.ndarray, tuple[int, int]]] | None = None,
) -> tuple[dict, Any]:
    """反复抓帧，直到连续 ``quiet_frames`` 次相邻两帧几乎没有变化。

    返回 (报告, 最后一帧)。最后一帧直接拿去当「动作之后」那张，不用再抓一次。
    一直在动（下雨、过场动画）就到 ``timeout_ms`` 为止，报告里 ``stable: false``。
    capture 每次返回一帧；unpack 把它拆成 (bgr, origin)，默认它本身就是这个二元组。
    """
    unpack = unpack or (lambda f: f)
    t0 = clock()
    if min_ms > 0:
        sleep(min_ms / 1000.0)
    prev = capture()
    frames = 1
    quiet = 0
    last = None
    deadline = t0 + max(timeout_ms, 0) / 1000.0
    while True:
        if clock() >= deadline:
            break
        sleep(max(poll_ms, 5) / 1000.0)
        cur = capture()
        frames += 1
        a, _ = unpack(prev)
        b, _ = unpack(cur)
        if a.shape != b.shape:
            quiet = 0           # 分辨率变了（切全屏）：从这一帧重新数
        else:
            last = diff(a, b, cell=cell, threshold=threshold, max_regions=0)
            quiet = quiet + 1 if is_quiet(last) else 0
        prev = cur
        if quiet >= quiet_frames:
            break
    report = {
        "stable": quiet >= quiet_frames,
        "elapsed_ms": round((clock() - t0) * 1000, 1),
        "frames": frames,
        "last_changed_cells": last["changed_cells"] if last else None,
    }
    return report, prev
