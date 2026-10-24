"""探测本机的屏幕几何：物理分辨率、DPI 缩放、各抓屏后端的实际输出尺寸。

为什么要单独探这一件事：坐标能对上，前提是「抓到的图像」和「点击用的坐标」
是同一个坐标系。多屏布局下副屏原点常为负数，
再加上 DPI 缩放，很容易在某一层被悄悄换算一次 —— 那样所有坐标都会偏，
而偏得很小的时候极难发现。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes


def set_dpi_aware() -> str:
    """尽量声明 Per-Monitor V2 DPI 感知，跟 cua_driver 对齐。"""
    try:
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return "PerMonitorV2"
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return "PerMonitor(shcore)"
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
                return "SystemDPI"
            except Exception:
                return "none"


def physical_screen() -> tuple[int, int]:
    """GetSystemMetrics 报的是「DPI 感知后」的像素。"""
    u = ctypes.windll.user32
    return u.GetSystemMetrics(0), u.GetSystemMetrics(1)


def virtual_screen() -> tuple[int, int, int, int]:
    """虚拟桌面（所有显示器并集），左/上可能是负数。"""
    u = ctypes.windll.user32
    return (
        u.GetSystemMetrics(76),  # SM_XVIRTUALSCREEN
        u.GetSystemMetrics(77),  # SM_YVIRTUALSCREEN
        u.GetSystemMetrics(78),  # SM_CXVIRTUALSCREEN
        u.GetSystemMetrics(79),  # SM_CYVIRTUALSCREEN
    )


def monitor_layout() -> list[dict]:
    """枚举每块显示器：物理矩形 + 该屏的 DPI 缩放。"""
    out: list[dict] = []
    try:
        import win32api
        import win32con

        for h in win32api.EnumDisplayMonitors():
            info = win32api.GetMonitorInfo(h)
            r = info["Monitor"]
            out.append(
                {
                    "device": info.get("Device", ""),
                    "primary": bool(info["Flags"] & win32con.MONITORINFOF_PRIMARY),
                    "x": r[0], "y": r[1],
                    "w": r[2] - r[0], "h": r[3] - r[1],
                }
            )
    except Exception as e:
        out.append({"error": f"win32api 不可用: {e}"})
    return out


def per_monitor_dpi() -> list[dict]:
    """每块显示器的有效 DPI（96 = 100%，144 = 150%）。"""
    res: list[dict] = []
    try:
        u = ctypes.windll.user32
        # 枚举显示设备名
        class DISPLAY_DEVICE(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128),
                ("DeviceKey", wintypes.WCHAR * 128),
            ]

        i = 0
        while True:
            dd = DISPLAY_DEVICE()
            dd.cb = ctypes.sizeof(dd)
            if not u.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
                break
            if dd.StateFlags & 0x1:  # DISPLAY_DEVICE_ATTACHED_TO_DESKTOP
                name = dd.DeviceName
                dpi_x = ctypes.c_uint(0)
                dpi_y = ctypes.c_uint(0)
                ok = u.GetDpiForMonitor  # 不存在，用 shcore
                import ctypes as C

                try:
                    shcore = C.windll.shcore
                    hmon = u.MonitorFromPoint(
                        ctypes.wintypes.POINT(0, 0),
                        ctypes.wintypes.DWORD(0),
                    )
                    # MDT_EFFECTIVE_DPI = 0
                    shcore.GetDpiForMonitor(
                        wintypes.HMONITOR(hmon), 0,
                        ctypes.byref(dpi_x), ctypes.byref(dpi_y),
                    )
                except Exception:
                    pass
                res.append(
                    {
                        "device": name,
                        "dpi": dpi_x.value or "?",
                        "scale": (
                            f"{dpi_x.value / 96:.2f}x" if dpi_x.value else "?"
                        ),
                    }
                )
            i += 1
    except Exception as e:
        res.append({"error": str(e)})
    return res


def grab_sizes() -> list[dict]:
    """各抓屏后端实际拿到多大。"""
    out: list[dict] = []

    try:
        import numpy as np

        import dxcam

        cam = dxcam.create(output_color="BGR")
        f = cam.grab()
        out.append({
            "backend": "dxcam(主屏)",
            "shape": None if f is None else f"{f.shape[1]}x{f.shape[0]}",
        })
        del cam
    except Exception as e:
        out.append({"backend": "dxcam", "error": f"{type(e).__name__}: {e}"})

    try:
        import mss
        import numpy as np

        with mss.mss() as sct:
            mons = sct.monitors
            out.append({"backend": "mss 显示器清单", "shape": [
                f"[{i}] {m['left']},{m['top']} {m['width']}x{m['height']}"
                for i, m in enumerate(mons)
            ]})
            shot = sct.grab(mons[1])
            out.append({"backend": "mss(monitors[1])", "shape": f"{shot.width}x{shot.height}"})
            shot_all = sct.grab(mons[0])
            out.append({"backend": "mss(monitors[0] 全部)", "shape": f"{shot_all.width}x{shot_all.height}"})
    except Exception as e:
        out.append({"backend": "mss", "error": f"{type(e).__name__}: {e}"})

    try:
        from PIL import ImageGrab
        import numpy as np

        img = ImageGrab.grab()
        out.append({"backend": "PIL ImageGrab", "shape": f"{img.width}x{img.height}"})
    except Exception as e:
        out.append({"backend": "PIL", "error": f"{type(e).__name__}: {e}"})

    return out


def main() -> None:
    mode = set_dpi_aware()
    print(f"DPI 感知: {mode}")
    print()
    print("GetSystemMetrics(0,1)  =", physical_screen())
    vx, vy, vw, vh = virtual_screen()
    print(f"虚拟桌面               = ({vx},{vy}) {vw}x{vh}")
    print()
    print("显示器布局:")
    for m in monitor_layout():
        print("  ", m)
    print()
    print("每屏 DPI:")
    for d in per_monitor_dpi():
        print("  ", d)
    print()
    print("抓屏后端实际尺寸:")
    for g in grab_sizes():
        print("  ", g)


if __name__ == "__main__":
    main()
