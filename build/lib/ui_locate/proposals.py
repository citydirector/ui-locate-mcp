"""结构化候选框：不靠模型，从画面的「规则结构」里找可点的格子。

**为什么需要**

UI 检测器（OmniParser 那个 YOLO）是拿桌面 / 网页界面训练的。到了游戏里，
HUD 和文字它认得，但物品格、标签页、装备槽、地图上的田块，大半一个框都没有
—— 星露谷背包截图实测：8 个标签页只框到 1 个，物品格一个没有。

这些东西有个共性，跟画风、游戏、软件都无关：**它们成排成列地重复出现，
大小一样、对得很齐**。物品栏、技能栏、工具栏、棋盘、表格、调色板都是这样。
所以不用学任何东西，几何规则就能把它们捞出来：

  1. Canny 边缘 → 闭合轮廓 → 外接矩形，只留「像个格子」的
     （够大、不太扁、轮廓把外接矩形填满八成以上 —— 透视的地面格子是梯形，过不了）
  2. 去重（同一个格子内外两条边会各出一个框）
  3. **只留成排或成列的**：同样大小、与至少两个同伴在同一行或同一列。
     树叶、石头、字母这些零散的轮廓在这一步几乎全被筛掉
  4. 补格：一组格子已经确定了行和列，被浮窗挡住、或者格子里塞满物品导致
     边框没检出来的那几格，按行列交点补上

输出与检测器同形（x1..y2 / cx / cy / w / h / conf），``source = "grid"``，
可以直接并进候选列表。纯 OpenCV，跨平台，1080p 约 0.1 秒（CPU）。
"""

from __future__ import annotations

import cv2
import numpy as np

CONF = 0.15  # 低于检测器的常见分数：两边框重了，留检测器的


def _rect_contours(gray: np.ndarray, min_side: int, max_frac: float) -> list[tuple[int, int, int, int]]:
    H, W = gray.shape
    out: list[tuple[int, int, int, int]] = []
    kernel = np.ones((2, 2), np.uint8)
    for lo, hi in ((30, 90), (60, 160)):
        edges = cv2.dilate(cv2.Canny(gray, lo, hi), kernel)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            if w < min_side or h < min_side or w > W * max_frac or h > H * max_frac:
                continue
            if max(w, h) / min(w, h) > 4:
                continue
            if cv2.contourArea(c) < 0.8 * w * h:  # 轴对齐的矩形；透视地面格是梯形，填不满
                continue
            out.append((x, y, w, h))
    return out


def _dedupe(boxes: list[tuple[int, int, int, int]]) -> np.ndarray:
    if not boxes:
        return np.zeros((0, 4))
    b = np.asarray(boxes, dtype=float)
    order = np.argsort(-(b[:, 2] * b[:, 3]))
    kept: list[np.ndarray] = []
    for i in order:
        x, y, w, h = b[i]
        dup = False
        for k in kept:
            a, bb, c, d = k
            ix = max(0.0, min(x + w, a + c) - max(x, a))
            iy = max(0.0, min(y + h, bb + d) - max(y, bb))
            small, big = min(w * h, c * d), max(w * h, c * d)
            if ix * iy > 0.6 * small and small / big > 0.5:
                dup = True
                break
        if not dup:
            kept.append(b[i])
    return np.asarray(kept)


def _cluster_1d(values: np.ndarray, tol: float) -> list[float]:
    """一维聚类：排序后相邻差 ≤ tol 的归一组，返回各组均值。"""
    if len(values) == 0:
        return []
    vs = np.sort(values)
    groups = [[vs[0]]]
    for v in vs[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [float(np.mean(g)) for g in groups]


def _complete_lattice(cells: np.ndarray) -> list[tuple[float, float, float, float]]:
    """一组同尺寸格子 → 按行列交点补齐缺的格。

    只在「已经有 ≥3 列且 ≥2 行」时补，并且只补**已有行与已有列的交点**，
    不向外延伸 —— 宁可漏，也不在空地上凭空造格子。
    """
    if len(cells) < 4:
        return []
    w = float(np.median(cells[:, 2]))
    h = float(np.median(cells[:, 3]))
    cx = cells[:, 0] + cells[:, 2] / 2
    cy = cells[:, 1] + cells[:, 3] / 2
    cols = _cluster_1d(cx, 0.3 * w)
    rows = _cluster_1d(cy, 0.3 * h)
    if len(cols) < 3 or len(rows) < 2:
        return []
    # 行列间距要规则：相邻间距与中位数差不超过 35%，否则不是一张格子网
    for axis in (cols, rows):
        gaps = np.diff(axis)
        if len(gaps) and (np.abs(gaps - np.median(gaps)) > 0.35 * np.median(gaps)).any():
            return []
    added: list[tuple[float, float, float, float]] = []
    for r in rows:
        for c in cols:
            if np.any((np.abs(cx - c) < 0.3 * w) & (np.abs(cy - r) < 0.3 * h)):
                continue
            added.append((c - w / 2, r - h / 2, w, h))
    return added


def structured_boxes(
    bgr: np.ndarray,
    *,
    min_side: int = 14,
    max_frac: float = 0.3,
    min_peers: int = 3,
) -> list[dict]:
    """找成排成列的格子。``min_peers``：同行或同列里（含自己）至少几个同尺寸格子。"""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    b = _dedupe(_rect_contours(gray, min_side, max_frac))
    if len(b) == 0:
        return []
    cx = b[:, 0] + b[:, 2] / 2
    cy = b[:, 1] + b[:, 3] / 2

    keep = np.zeros(len(b), dtype=bool)
    for i, (_x, _y, w, h) in enumerate(b):
        same = (np.abs(b[:, 2] - w) <= 0.2 * w) & (np.abs(b[:, 3] - h) <= 0.2 * h)
        row = same & (np.abs(cy - cy[i]) < 0.25 * h) & (np.abs(cx - cx[i]) < 6 * w)
        col = same & (np.abs(cx - cx[i]) < 0.25 * w) & (np.abs(cy - cy[i]) < 6 * h)
        keep[i] = row.sum() >= min_peers or col.sum() >= min_peers
    kept = b[keep]

    # 按尺寸分组，每组单独补格
    boxes = [tuple(map(float, k)) for k in kept]
    used = np.zeros(len(kept), dtype=bool)
    for i in range(len(kept)):
        if used[i]:
            continue
        w, h = kept[i, 2], kept[i, 3]
        grp = (np.abs(kept[:, 2] - w) <= 0.2 * w) & (np.abs(kept[:, 3] - h) <= 0.2 * h) & ~used
        used |= grp
        boxes.extend(_complete_lattice(kept[grp]))

    out = []
    H, W = gray.shape
    for x, y, w, h in boxes:
        x1, y1 = max(0.0, x), max(0.0, y)
        x2, y2 = min(float(W), x + w), min(float(H), y + h)
        if x2 - x1 < min_side * 0.5 or y2 - y1 < min_side * 0.5:
            continue
        out.append({
            "x1": round(x1, 1), "y1": round(y1, 1), "x2": round(x2, 1), "y2": round(y2, 1),
            "cx": round((x1 + x2) / 2, 1), "cy": round((y1 + y2) / 2, 1),
            "w": round(x2 - x1, 1), "h": round(y2 - y1, 1),
            "conf": CONF, "source": "grid",
        })
    return out


def drop_inside_text(cands: list[dict], items: list[dict]) -> list[dict]:
    """去掉落在文字框里的候选：一行字的每个字母也是「成排同尺寸」的小轮廓。"""
    texts = [it for it in items if it.get("text")]
    if not texts:
        return cands
    out = []
    for c in cands:
        inside = False
        for t in texts:
            if (t["x1"] - 2 <= c["cx"] <= t["x2"] + 2 and t["y1"] - 2 <= c["cy"] <= t["y2"] + 2
                    and c["w"] * c["h"] < 0.5 * t["w"] * t["h"]
                    # 只算「一行字」：字母高度和行高相当。大面板里带了一段说明文字，
                    # 不能因此把面板里的物品格全删掉。
                    and c["h"] >= 0.3 * t["h"]):
                inside = True
                break
        if not inside:
            out.append(c)
    return out
