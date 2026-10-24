"""验证 DPI 感知开关对抓屏尺寸的影响 —— 这决定坐标能不能对上 cua_driver。

背景：当系统缩放不是 100% 时，抓屏可能给物理像素、而点击工具报逻辑像素，
两者相差一个缩放倍数。如果抓屏和点击不在同一坐标系，所有坐标都会成倍偏移
—— 而小偏差极难发现。

做法：同一进程里先后在「未声明 DPI 感知」和「声明 PerMonitorV2」两种状态下
各抓一次 PIL ImageGrab，看拿到多大。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys


def grab_size() -> str:
    from PIL import ImageGrab

    img = ImageGrab.grab()
    return f"{img.width}x{img.height}"


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        # 子进程：只抓，不碰 DPI
        print(f"[未声明 DPI] PIL ImageGrab -> {grab_size()}")
        return

    # 父进程：先抓一次，再声明 DPI，再抓一次
    print(f"[未声明 DPI] PIL ImageGrab -> {grab_size()}")

    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        mode = "PerMonitorV2"
    except Exception:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        mode = "PerMonitor"

    print(f"[声明 {mode}] PIL ImageGrab -> {grab_size()}")
    u = ctypes.windll.user32
    print(f"[声明 {mode}] GetSystemMetrics -> {u.GetSystemMetrics(0)}x{u.GetSystemMetrics(1)}")

    print()
    print("--- 服务进程当前是什么状态（复现它的启动方式）---")
    subprocess.run(
        [sys.executable, __file__, "--child"],
        check=False,
    )


if __name__ == "__main__":
    main()
