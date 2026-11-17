"""盯着宿主进程：它一走，本服务就跟着走。

常驻服务是 MCP 进程拉起来的，可它的生命不该挂在 MCP 上 —— 插件重载、MCP
重连都会换一个新的 MCP 进程，服务最好继续活着，省掉一次预热（CUDA 首次
约 35 秒）。真正该跟的是 **Host**：Host 在，服务就在；Host 退出，服务立刻
退出。

没有这一层时，`DETACHED_PROCESS` 会把服务彻底从父进程的生命周期里摘出来，
DSH 关掉之后 python / pythonw 一直留在进程列表里，只能手动结束。

实现刻意做得很窄：只认一个 pid、只判「还在 / 不在了」，连续几次都判死才
收工 —— 句柄抖动不该误杀一个正在预热的服务。Windows 上拿 `SYNCHRONIZE`
句柄等它变成有信号，一次调用就能等到退出；其它平台退回 `os.kill(pid, 0)`
轮询。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable

ENV = "UI_LOCATE_OWNER_PID"

# 轮询间隔与「连几次才算死」。1 秒 × 3 次：宿主退出后 2–3 秒内收工，又不会
# 因为一次权限抖动就把服务杀掉。
INTERVAL_S = 1.0
STRIKES = 3


def parse_owner(value: object) -> int | None:
    """把环境变量读成 pid；空值、非数字、非正数一律当成「没配置」。

    没配置时不启用监控 —— 手动跑 `python -m ui_locate.server` 的人按 Ctrl+C
    自己停，服务不该自作主张退出。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text.isdigit():
        return None
    pid = int(text)
    return pid if pid > 0 else None


def _windows_is_alive(pid: int) -> bool:
    """`OpenProcess` 拿不到句柄且错误是「参数无效」才算不在。

    拒绝访问（错误 5）说明进程还在、只是本进程无权打开它 —— 这种情况必须
    当作活着，否则跨完整性级别启动时服务会立刻自杀。
    """
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    ERROR_INVALID_PARAMETER = 87
    ERROR_ACCESS_DENIED = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def probe(pid: int) -> bool:
    """这个 pid 现在还活着吗。"""
    if sys.platform == "win32":
        return _windows_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start(
    owner_pid: int,
    on_gone: Callable[[], None],
    *,
    is_alive: Callable[[int], bool] = probe,
    interval: float = INTERVAL_S,
    strikes: int = STRIKES,
) -> threading.Thread:
    """起一个 daemon 线程盯住 owner；它不在了就调 `on_gone`。

    `is_alive` 与 `interval` 可注入，测试因此不需要真的杀进程。
    """
    stop = threading.Event()

    def loop() -> None:
        misses = 0
        while not stop.wait(interval):
            if is_alive(owner_pid):
                misses = 0
                continue
            misses += 1
            if misses >= strikes:
                on_gone()
                return

    thread = threading.Thread(target=loop, name="ui-locate-owner-watch", daemon=True)
    thread.start()
    return thread
