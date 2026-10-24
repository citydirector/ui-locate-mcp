"""验证抓屏覆盖范围与 console 归属 —— 多显示器改造前的事实基线。

要弄清三件事：
  1. PIL ImageGrab 在 all_screens=True/False 下分别抓到多大、覆盖哪些屏；
  2. 各后端能否抓到负坐标副屏（ImageGrab 对负坐标副屏会截黑）；
  3. 主屏在 max_side=960 下会被切成几块、每块多大。
"""

from __future__ import annotations

import ctypes
import sys


def dpi_aware() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def probe_pil() -> None:
    from PIL import ImageGrab

    for alls in (False, True):
        try:
            img = ImageGrab.grab(all_screens=alls)
            print(f"  ImageGrab(all_screens={alls}) -> {img.width}x{img.height}")
        except Exception as e:
            print(f"  ImageGrab(all_screens={alls}) -> 失败 {type(e).__name__}: {e}")

    # 副屏单独截：bbox 用该屏真实的屏幕坐标（原点为负时就是负 bbox）
    from ui_locate.screen import list_monitors

    secondary = next((m for m in list_monitors() if m.x < 0), None)
    box = (secondary.x, secondary.y, secondary.x + secondary.w, secondary.y + secondary.h) if secondary else None
    try:
        if box is None:
            print("  ImageGrab(副屏 bbox) -> 跳过：本机没有原点为负的屏")
            return
        img = ImageGrab.grab(bbox=box)
        arr_is_black = None
        try:
            import numpy as np

            a = np.asarray(img)
            arr_is_black = bool(a.max() < 8)
        except Exception:
            pass
        print(
            f"  ImageGrab(副屏 bbox={box}) -> {img.width}x{img.height}"
            f"  全黑={arr_is_black}"
        )
    except Exception as e:
        print(f"  ImageGrab(副屏) -> 失败 {type(e).__name__}: {e}")


def probe_backends() -> None:
    for mod in ("dxcam", "mss", "screeninfo"):
        try:
            __import__(mod)
            print(f"  {mod}: 已安装")
        except ImportError:
            print(f"  {mod}: 未安装")


def probe_tiles() -> None:
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))
    from ui_locate.tiled import plan_tiles

    for (w, h, ms) in [(2560, 1600, 960), (2560, 1600, 1280), (1920, 1080, 960), (3840, 1600, 960)]:
        plan = plan_tiles(w, h, ms)
        sizes = {(t[2] - t[0], t[3] - t[1]) for t in plan.tiles}
        print(f"  {w}x{h} max_side={ms} -> {len(plan.tiles)} 块，块尺寸 {sorted(sizes)}")


def main() -> None:
    dpi_aware()
    print("DPI 已声明 PerMonitorV2")
    print("\n抓屏后端:")
    probe_backends()
    print("\nPIL 抓屏范围:")
    probe_pil()
    print("\n现有分块结果:")
    probe_tiles()


if __name__ == "__main__":
    main()
