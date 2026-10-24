"""验证 all_screens=True 抓到的整张虚拟桌面里，负坐标副屏那块到底有没有内容。

如果也是黑的，说明整条 PIL 路线都抓不到副屏，必须换 mss。
这决定多显示器方案走哪条路。
"""

from __future__ import annotations

import ctypes

import numpy as np


def dpi_aware() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def main() -> None:
    dpi_aware()
    from PIL import ImageGrab

    img = ImageGrab.grab(all_screens=True)
    a = np.asarray(img)
    print(f"all_screens=True -> {img.width}x{img.height}  shape={a.shape}")

    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))
    from ui_locate.screen import list_monitors

    u = ctypes.windll.user32
    vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
    print(f"虚拟桌面原点 = ({vx},{vy})")

    mons = list_monitors()
    print(f"共 {len(mons)} 块屏")
    for m in mons:
        # 图内坐标 = 屏幕坐标 - 虚拟桌面原点
        x0, y0 = m.x - vx, m.y - vy
        x1, y1 = x0 + m.w, y0 + m.h
        print(f"  [{m.index}] {m.device} {m.w}x{m.h}@{m.x},{m.y} -> 图内 ({x0},{y0})-({x1},{y1})")
        if not (0 <= x0 < img.width and 0 <= y0 < img.height):
            print("       起点不在图内，跳过")
            continue
        patch = a[max(0, y0):y1, max(0, x0):x1]
        print(
            f"       shape={patch.shape}  max={patch.max()}  mean={patch.mean():.1f}"
            f"  全黑={bool(patch.max() < 8)}"
        )

    # 整图非黑区域占比，看看有没有大量空白
    gray = a[:, :, :3].max(axis=2)
    print(f"整图非黑像素占比 = {float((gray >= 8).mean()) * 100:.1f}%")


if __name__ == "__main__":
    main()
