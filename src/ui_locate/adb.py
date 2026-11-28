"""adb 目标：把一台 Android 设备（模拟器或真机）当成一块屏来抓图和输入。

写法：``monitor="adb:127.0.0.1:16384"``（或 ``window`` 同一写法）。``"adb:"`` 不带序列号
时交给 adb 自己挑（只连了一台设备才行）。

为什么单独做一个后端，而不是抓模拟器窗口 + SendInput：

1. **不抢前台**。SendInput 只送给前台窗口；adb 的 input 走设备内部的输入子系统，
   模拟器窗口被挡住、最小化都照样生效，也不会动博士正在用的鼠标。
2. **坐标就是设备坐标**。模拟器窗口有标题栏、侧边工具栏、缩放，窗口坐标和游戏
   坐标之间隔着一层换算；``screencap`` 的像素和 ``input`` 的坐标是同一套（横屏时
   screencap 输出已按显示方向旋转，与 ``input tap`` 一致）。
3. **触摸语义**。很多手游的拖动要「按下后停一会才拿起来」「末端停一会才不回弹」，
   鼠标拖拽表达不了；``input motionevent`` 可以逐段发 DOWN / MOVE / UP。

抓图用 ``exec-out screencap``（不带 -p）：设备端不做 PNG 编码。实测 1600x900
模拟器上原始帧中位 374ms，-p 为 864ms，先存文件再 pull 为 0.9~2s。
原始帧头部在 Android 9 及以前是 12 字节（w, h, format），10 起多一个 colorspace
共 16 字节；按「总长 - w*h*4」反推头长，不按版本猜。

所有子进程调用都带超时；失败抛 `AdbError`，不吞。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from typing import Any

import numpy as np

PREFIX = "adb:"

# 最近一次抓图：给 /locate 等接口的 capture 字段用
LAST: dict[str, Any] = {}

# 按键名 → Android KEYCODE。没列到的：KEYCODE_ 开头原样用，单个字母 / 数字自动换算。
_KEYS = {
    "esc": "KEYCODE_ESCAPE", "escape": "KEYCODE_ESCAPE",
    "back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "menu": "KEYCODE_MENU",
    "enter": "KEYCODE_ENTER", "return": "KEYCODE_ENTER", "tab": "KEYCODE_TAB",
    "space": "KEYCODE_SPACE", "backspace": "KEYCODE_DEL", "delete": "KEYCODE_FORWARD_DEL",
    "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT", "right": "KEYCODE_DPAD_RIGHT",
}


class AdbError(RuntimeError):
    """adb 不在、设备不在、命令失败或超时。"""


def is_adb_spec(spec: object) -> bool:
    return isinstance(spec, str) and spec.strip().lower().startswith(PREFIX)


def parse_spec(spec: object) -> str | None:
    """``"adb:127.0.0.1:16384"`` → ``"127.0.0.1:16384"``；``"adb:"`` → ``""``；其它 → None。"""
    if not is_adb_spec(spec):
        return None
    return str(spec).strip()[len(PREFIX):].strip()


def adb_path() -> str:
    """``UI_LOCATE_ADB`` 环境变量优先，其次 PATH 上的 adb。"""
    env = os.environ.get("UI_LOCATE_ADB", "").strip()
    if env:
        if not os.path.isfile(env):
            raise AdbError(f"UI_LOCATE_ADB 指向的文件不存在：{env}")
        return env
    found = shutil.which("adb")
    if not found:
        raise AdbError("找不到 adb：把它加进 PATH，或设置 UI_LOCATE_ADB 为 adb 的完整路径")
    return found


def _run(serial: str, args: list[str], *, timeout_s: float = 10.0) -> bytes:
    cmd = [adb_path()]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"adb {args[0]} 超时（{timeout_s:.0f}s）") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
        raise AdbError(f"adb {args[0]} 退出码 {proc.returncode}：{err[:300]}")
    return proc.stdout


def shell(serial: str, commands: list[str], *, timeout_s: float = 15.0) -> str:
    """一次 adb shell 跑完一串命令（分号连接），省掉每条约 100ms 的往返。"""
    out = _run(serial, ["shell", "; ".join(commands)], timeout_s=timeout_s)
    return out.decode("utf-8", "replace")


def decode_raw(buf: bytes) -> np.ndarray:
    """``screencap`` 原始帧 → BGR。头长按总长反推（12 或 16 字节）。"""
    if len(buf) < 12:
        raise AdbError(f"screencap 返回 {len(buf)} 字节，不是一帧")
    w, h, _fmt = (int(v) for v in np.frombuffer(buf[:12], dtype="<u4"))
    header = len(buf) - w * h * 4
    if w <= 0 or h <= 0 or header not in (12, 16):
        raise AdbError(f"screencap 原始帧尺寸对不上：{w}x{h}，总长 {len(buf)}")
    rgba = np.frombuffer(buf, dtype=np.uint8, offset=header).reshape(h, w, 4)
    return np.ascontiguousarray(rgba[:, :, 2::-1])


def grab(spec: object) -> tuple[np.ndarray, tuple[int, int]]:
    """抓设备屏幕，返回 (BGR, (0, 0))。原点恒为 0：坐标就是设备坐标。"""
    serial = parse_spec(spec)
    if serial is None:
        raise AdbError(f"不是 adb 写法：{spec!r}")
    t0 = time.perf_counter()
    try:
        img = decode_raw(_run(serial, ["exec-out", "screencap"]))
        LAST.clear()
        LAST.update(backend="adb-raw", device=serial, ms=_ms(t0))
        return img, (0, 0)
    except AdbError as raw_error:
        # 个别设备的原始帧格式不是 RGBA_8888，退回 PNG（慢一倍，但通用）
        import cv2
        png = _run(serial, ["exec-out", "screencap", "-p"])
        img = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise AdbError(f"screencap 原始帧和 PNG 都解不出：{raw_error}") from raw_error
        LAST.clear()
        LAST.update(backend="adb-png", device=serial, ms=_ms(t0), notes=[f"raw: {raw_error}"])
        return img, (0, 0)


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _sleep(ms: int) -> str:
    return f"sleep {max(0, ms) / 1000:.3f}"


def tap(serial: str, x: int, y: int, *, count: int = 1, delay_ms: int = 40) -> dict:
    t0 = time.perf_counter()
    cmds: list[str] = []
    for i in range(max(1, count)):
        if i:
            cmds.append(_sleep(delay_ms))
        cmds.append(f"input tap {int(x)} {int(y)}")
    shell(serial, cmds)
    return {"delivery": "adb", "device": serial, "x": int(x), "y": int(y),
            "count": max(1, count), "elapsed_ms": _ms(t0)}


def long_press(serial: str, x: int, y: int, *, hold_ms: int = 800) -> dict:
    t0 = time.perf_counter()
    shell(serial, [f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {max(1, int(hold_ms))}"])
    return {"delivery": "adb", "device": serial, "x": int(x), "y": int(y),
            "hold_ms": int(hold_ms), "elapsed_ms": _ms(t0)}


def drag_commands(x1: int, y1: int, x2: int, y2: int, *, steps: int = 8,
                  duration_ms: int = 300, hold_start_ms: int = 0,
                  hold_end_ms: int = 0) -> list[str]:
    """DOWN →（停 hold_start_ms）→ 均匀 MOVE → （停 hold_end_ms）→ UP。

    hold_start_ms：很多手游要按住一会才把卡牌 / 干员「拿起来」，直接移动会被当成滑动。
    hold_end_ms：带阻尼的列表在末端停一下才不回弹 / 不带惯性。纯函数，便于测试。
    """
    steps = max(1, int(steps))
    gap = int(duration_ms) // steps
    cmds = [f"input motionevent DOWN {x1} {y1}"]
    if hold_start_ms > 0:
        cmds.append(_sleep(hold_start_ms))
    for i in range(1, steps + 1):
        cmds.append(f"input motionevent MOVE {round(x1 + (x2 - x1) * i / steps)} "
                    f"{round(y1 + (y2 - y1) * i / steps)}")
        if gap > 0 and i < steps:
            cmds.append(_sleep(gap))
    if hold_end_ms > 0:
        cmds.append(_sleep(hold_end_ms))
    cmds.append(f"input motionevent UP {x2} {y2}")
    return cmds


def drag(serial: str, x1: int, y1: int, x2: int, y2: int, *, steps: int = 8,
         duration_ms: int = 300, hold_start_ms: int = 0, hold_end_ms: int = 0) -> dict:
    t0 = time.perf_counter()
    cmds = drag_commands(int(x1), int(y1), int(x2), int(y2), steps=steps,
                         duration_ms=duration_ms, hold_start_ms=hold_start_ms,
                         hold_end_ms=hold_end_ms)
    try:
        out = shell(serial, cmds, timeout_s=15 + (duration_ms + hold_start_ms + hold_end_ms) / 1000)
    except AdbError as exc:
        if "motionevent" not in str(exc):
            raise
        out = str(exc)
    mode = "motionevent"
    if "motionevent" in out and ("Error" in out or "Unknown" in out):
        # Android 9 及以前没有 input motionevent：退回 swipe，丢掉两端的停顿
        shell(serial, [f"input swipe {x1} {y1} {x2} {y2} {max(1, duration_ms + hold_end_ms)}"])
        mode = "swipe"
    return {"delivery": "adb", "device": serial, "from": [int(x1), int(y1)],
            "to": [int(x2), int(y2)], "mode": mode, "hold_start_ms": int(hold_start_ms),
            "hold_end_ms": int(hold_end_ms), "elapsed_ms": _ms(t0)}


def touch(serial: str, phase: str, x: int, y: int, *, hold_ms: int = 0) -> dict:
    """分段触摸：down / move / up 各发一次，中间可以抓图再决定落点。

    手指在两次调用之间一直按着（设备端状态），所以「按下 → 抓图看高亮格 →
    移到目标 → 松手」能拆成几次调用。down 后可选 hold_ms，省一次往返。
    """
    phase = str(phase).strip().lower()
    if phase not in ("down", "move", "up", "cancel"):
        raise AdbError(f"touch 的 phase 只能是 down / move / up / cancel，收到 {phase!r}")
    t0 = time.perf_counter()
    cmds = [f"input motionevent {phase.upper()} {int(x)} {int(y)}"]
    if hold_ms > 0:
        cmds.append(_sleep(hold_ms))
    shell(serial, cmds)
    return {"delivery": "adb", "device": serial, "phase": phase, "x": int(x), "y": int(y),
            "elapsed_ms": _ms(t0)}


def keycode(key: str) -> str:
    k = str(key).strip()
    if not k:
        raise AdbError("press 需要一个按键名")
    if k.upper().startswith("KEYCODE_"):
        return k.upper()
    low = k.lower()
    if low in _KEYS:
        return _KEYS[low]
    if len(k) == 1 and k.isascii() and k.isalnum():
        return f"KEYCODE_{k.upper()}"
    raise AdbError(f"adb 不认识的按键 {key!r}；可写 KEYCODE_XXX")


def press(serial: str, key: str) -> dict:
    t0 = time.perf_counter()
    code = keycode(key)
    shell(serial, [f"input keyevent {code}"])
    return {"delivery": "adb", "device": serial, "key": key, "keycode": code,
            "elapsed_ms": _ms(t0)}


def type_text(serial: str, text: str) -> dict:
    """``input text`` 只收 ASCII；空格要写成 %s（input 自己的约定），其余交给 sh 单引号。"""
    if not text.isascii():
        raise AdbError("adb input text 只支持 ASCII；中文请用输入法或剪贴板")
    t0 = time.perf_counter()
    shell(serial, [f"input text {shlex.quote(text.replace(' ', '%s'))}"])
    return {"delivery": "adb", "device": serial, "chars": len(text), "elapsed_ms": _ms(t0)}


def dispatch(device: object, action: str, params: dict[str, Any]) -> dict:
    """act 层的动作名 → adb 输入。``device`` 是 adb 写法。"""
    serial = parse_spec(device)
    if serial is None:
        raise AdbError(f"不是 adb 写法：{device!r}")
    if action in ("click", "double_click"):
        if str(params.get("button") or "left") != "left":
            raise AdbError("触屏没有右键 / 中键；长按用 action=long_press")
        return tap(serial, params["x"], params["y"], count=int(params.get("count") or 1),
                   delay_ms=int(params.get("delay_ms") or 40))
    if action == "long_press":
        return long_press(serial, params["x"], params["y"],
                          hold_ms=int(params.get("hold_ms") or 800))
    if action == "drag":
        return drag(serial, params["x1"], params["y1"], params["x2"], params["y2"],
                    steps=int(params.get("steps") or 8),
                    duration_ms=int(params.get("duration_ms") or 300),
                    hold_start_ms=int(params.get("hold_start_ms") or 0),
                    hold_end_ms=int(params.get("hold_end_ms") or 0))
    if action in ("touch_down", "touch_move", "touch_up"):
        return touch(serial, action.split("_", 1)[1], params["x"], params["y"],
                     hold_ms=int(params.get("hold_ms") or 0))
    if action == "press":
        return press(serial, str(params.get("key") or ""))
    if action == "type":
        return type_text(serial, str(params.get("text") or ""))
    raise AdbError(f"adb 目标不支持动作 {action!r}（支持 click / double_click / long_press / "
                   "drag / touch_down / touch_move / touch_up / press / type）")
