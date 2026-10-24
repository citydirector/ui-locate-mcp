"""动作原语：把「点一下」「拖一下」「敲个键」变成可调用的一行。

为什么要有这一层，而不是在需要的时候随手调 SendInput：
  1. SendInput 的 `INPUT` 结构体在 x86_64 上是 40 字节、在 x86 上是 28 字节，
     手写一次就容易错；写错的表现是「调用成功但什么都没发生」，很难查。
  2. 鼠标坐标要归一化到 0..65535 且区分虚拟桌面坐标与主屏坐标。副屏原点
     是负数时（本机实测 -1234），这一步算错就会点到别处去。
  3. 键盘要处理扩展键与 Unicode 输入。游戏/安装器里没有输入法，用
     KEYEVENTF_UNICODE 逐码点送比模拟击键可靠。

三条通道，各有各的适用面：

  SendInput    走系统输入队列，最像真人操作，游戏和 DirectInput 程序只认它。
               要求目标窗口在前台。
  PostMessage  把带 lPar​am 坐标的 WM_* 消息直接投给窗口，不需要焦点、不动
               光标，后台也能用。代价是现代框架（Chromium、WPF）通常会忽略
               合成消息。
  SendMessage  与 PostMessage 同上，但同步等目标线程处理完。目标卡住时会一起
               挂，只用于确实需要确认送达的场合。

`delivery="auto"` 的规则：先 SendInput；若返回值显示一条都没送出去，再退回
PostMessage。**不把「没抛异常」当作成功** —— 这两个 API 都可能返回成功而
什么都没发生，真正的判据在 `ui_locate.act` 的验证阶段。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time

__all__ = [
    "InputError",
    "available",
    "cursor",
    "move",
    "click",
    "scroll",
    "drag",
    "press",
    "hotkey",
    "type_text",
    "window_at",
    "describe",
]


class InputError(RuntimeError):
    """输入通道不可用，或参数组合没有意义。"""


_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:  # pragma: no cover - 只在 Windows 上走真实现
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
else:  # pragma: no cover
    _user32 = None  # type: ignore[assignment]


# ── 结构体 ──────────────────────────────────────────────────────────────────
# 字段顺序与类型必须与 winuser.h 完全一致。任何一个字段写错，SendInput 都会
# 返回「已发送」的数量，但系统拿到的是一堆垃圾 —— 这正是它难查的地方。

_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wt.DWORD),
        ("wParamL", wt.WORD),
        ("wParamH", wt.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1

# 鼠标事件标志
_MOVE = 0x0001
_LEFTDOWN = 0x0002
_LEFTUP = 0x0004
_RIGHTDOWN = 0x0008
_RIGHTUP = 0x0010
_MIDDLEDOWN = 0x0020
_MIDDLEUP = 0x0040
_WHEEL = 0x0800
_HWHEEL = 0x1000
_ABSOLUTE = 0x8000
_VIRTUALDESK = 0x4000

# 键盘事件标志
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

# 消息参数
_MK_LBUTTON = 0x0001
_MK_RBUTTON = 0x0002
_MK_MBUTTON = 0x0010

_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_RBUTTONDOWN = 0x0204
_WM_RBUTTONUP = 0x0205
_WM_MBUTTONDOWN = 0x0207
_WM_MBUTTONUP = 0x0208
_WM_MOUSEWHEEL = 0x020A
_WM_MOUSEHWHEEL = 0x020E


# ── 虚拟键码 ────────────────────────────────────────────────────────────────
# 只收常用键。写全表没有意义，反而让人以为 0x00 到 0xFF 都在这儿 —— 用不到
# 的键请直接传十六进制串，比如 "0x5D"。

_VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10,
    "ctrl": 0x11, "alt": 0x12, "pause": 0x13, "capslock": 0x14,
    "escape": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "insert": 0x2D, "delete": 0x2E,
    "win": 0x5B,
    "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64,
    "num5": 0x65, "num6": 0x66, "num7": 0x67, "num8": 0x68, "num9": 0x69,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    "numlock": 0x90, "scrolllock": 0x91,
    "shiftleft": 0xA0, "shiftright": 0xA1,
    "ctrlleft": 0xA2, "ctrlright": 0xA3,
    "altleft": 0xA4, "altright": 0xA5,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
for _i in range(24):
    _VK[f"f{_i + 1}"] = 0x70 + _i
for _c in "abcdefghijklmnopqrstuvwxyz0123456789":
    _VK[_c] = ord(_c.upper())

# 这些键在扫描码层面是「扩展键」，不标会被当成小键盘上的同一个键
_EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x21, 0x22, 0x23, 0x24, 0x2D, 0x2E, 0x5B, 0x5D}

# 常见别名
_ALIAS = {"esc": "escape", "del": "delete", "return": "enter", "control": "ctrl",
          "ins": "insert", "pgup": "pageup", "pgdn": "pagedown", "meta": "win",
          "super": "win", "cmd": "win"}


def available() -> bool:
    """当前平台能不能注入输入。"""
    return _IS_WINDOWS


def _require() -> None:
    if not _IS_WINDOWS:
        raise InputError(f"输入注入只支持 Windows，当前是 {sys.platform}")


# ── 基础发送 ────────────────────────────────────────────────────────────────

def _send(*items: _INPUT) -> int:
    """把一串事件一次性交给 SendInput，返回系统接受了几个。

    必须一次送完：单击的按下与抬起之间若被别的调用插入，目标可能把它当成
    两次独立单击（双击判定窗口通常 500ms 内，但中间插入的事件会打断）。
    """
    _require()
    if not items:
        return 0
    array = (_INPUT * len(items))(*items)
    ctypes.set_last_error(0)
    sent = int(_user32.SendInput(len(items), array, ctypes.sizeof(_INPUT)))
    if sent == 0:
        raise InputError(
            f"SendInput 一条事件都没送出去（{len(items)} 条全部失败，"
            f"last_error={ctypes.get_last_error()}）。"
            "通常是会话被锁、UIPI 拦截，或进程不在交互式桌面里。"
        )
    return sent


def _mouse_event(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _INPUT:
    item = _INPUT()
    item.type = _INPUT_MOUSE
    item.u.mi = _MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return item


def _key_event(vk: int, up: bool = False, *, extended: bool = False,
               unicode: bool = False) -> _INPUT:
    item = _INPUT()
    item.type = _INPUT_KEYBOARD
    flags = 0
    if up:
        flags |= _KEYEVENTF_KEYUP
    if extended:
        flags |= _KEYEVENTF_EXTENDEDKEY
    if unicode:
        flags |= _KEYEVENTF_UNICODE
    # unicode 模式下 wVk 必须为 0，字符放在 wScan 里
    item.u.ki = _KEYBDINPUT(0 if unicode else vk, vk if unicode else 0, flags, 0, 0)
    return item


# ── 坐标换算 ────────────────────────────────────────────────────────────────

def _virtual_rect() -> tuple[int, int, int, int]:
    """虚拟桌面矩形（含所有显示器）。副屏在主屏左侧时 x 是负数。"""
    _require()
    get = _user32.GetSystemMetrics
    return (
        get(76), get(77), get(78), get(79),  # SM_X/Y/CX/CYVIRTUALSCREEN
    )


def _normalize(x: int, y: int) -> tuple[int, int]:
    """屏幕绝对坐标 → SendInput 要的 0..65535 归一化值。

    用虚拟桌面（而不是主屏）做基准，并配上 MOUSEEVENTF_VIRTUALDESK：否则
    副屏上的点会被映射主屏范围内，整体偏移一整块屏。
    """
    vx, vy, vw, vh = _virtual_rect()
    if vw <= 1 or vh <= 1:
        raise InputError(f"虚拟桌面尺寸异常：{vw}x{vh}")
    nx = int(round((x - vx) * 65535 / (vw - 1)))
    ny = int(round((y - vy) * 65535 / (vh - 1)))
    # 允许贴边，但不允许越界 —— 越界会被系统夹到边上，看起来像"点偏了"
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def cursor() -> dict:
    """读回光标位置。目标程序把光标锁住时这个值不会变，只能作参考。"""
    _require()
    pt = wt.POINT()
    if not _user32.GetCursorPos(ctypes.byref(pt)):
        raise InputError(f"GetCursorPos 失败（last_error={ctypes.get_last_error()}）")
    return {"x": int(pt.x), "y": int(pt.y)}


def _cursor_or_none() -> dict | None:
    """尽力读一次光标；读不到返回 None。

    为什么要有这个宽容版本：`cursor()` 在诊断场景下应该报错（读不到就是
    读不到，别装没事），但点击流程里读光标只是为了「报告光标动没动」——
    这是个观测，不是前置条件。本机实测这个调用会偶发失败（同一个进程里
    前一次成功、后一次 err=5），让它把整次点击带崩毫无道理。
    """
    try:
        return cursor()
    except InputError:
        return None


def window_at(x: int, y: int) -> dict:
    """这个屏幕坐标下面是哪个窗口。用来确认「我要点的地方确实是它」。"""
    _require()
    hwnd = int(_user32.WindowFromPoint(wt.POINT(int(x), int(y))) or 0)
    if not hwnd:
        return {"hwnd": 0, "class": None, "title": None}
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    cls = buf.value
    _user32.GetWindowTextW(hwnd, buf, 256)
    return {"hwnd": hwnd, "class": cls, "title": buf.value}


# ── 鼠标 ────────────────────────────────────────────────────────────────────

def move(x: int, y: int, *, settle_ms: int = 0) -> dict:
    """把光标移到屏幕绝对坐标。

    `Moved` 只是「事件送出去了」；本机实测某些进程里 SendInput 返回成功但
    光标不动（被外层拦），所以这里额外回读一次光标，把两件事分开报。
    """
    _require()
    nx, ny = _normalize(x, y)
    before = _cursor_or_none()
    sent = _send(_mouse_event(_MOVE | _ABSOLUTE | _VIRTUALDESK, nx, ny))
    if settle_ms:
        time.sleep(max(0, settle_ms) / 1000.0)
    after = _cursor_or_none()
    moved = None
    if before is not None and after is not None:
        moved = (after["x"], after["y"]) != (before["x"], before["y"])
    return {
        "sent": sent,
        "x": x, "y": y,
        "cursor": after,
        "cursor_moved": moved,
    }


_BUTTON_FLAGS = {
    "left": (_LEFTDOWN, _LEFTUP, _MK_LBUTTON, _WM_LBUTTONDOWN, _WM_LBUTTONUP),
    "right": (_RIGHTDOWN, _RIGHTUP, _MK_RBUTTON, _WM_RBUTTONDOWN, _WM_RBUTTONUP),
    "middle": (_MIDDLEDOWN, _MIDDLEUP, _MK_MBUTTON, _WM_MBUTTONDOWN, _WM_MBUTTONUP),
}


def click(x: int, y: int, *, button: str = "left", count: int = 1,
          delivery: str = "auto", delay_ms: int = 40) -> dict:
    """在屏幕绝对坐标点一下。

    delivery:
      "send_input"  只走系统队列（最像真人，要求前台）
      "message"     只投窗口消息（不动光标、不需焦点，但很多框架不认）
      "auto"        先试 send_input；光标没动过就再投一次消息（默认）
    """
    _require()
    btn = str(button or "left").strip().lower()
    if btn not in _BUTTON_FLAGS:
        raise InputError(f"不认识的鼠标键 {button!r}；可用 left / right / middle")
    count = max(1, int(count))

    mode = str(delivery or "auto").strip().lower()
    if mode in ("sendinput", "send", "input"):
        mode = "send_input"
    if mode in ("post", "postmessage", "msg"):
        mode = "message"
    if mode not in ("send_input", "message", "auto"):
        raise InputError(f"不认识的 delivery {delivery!r}；可用 send_input / message / auto")

    down, up, mkflag, down_msg, up_msg = _BUTTON_FLAGS[btn]
    report: dict = {"x": x, "y": y, "button": btn, "count": count}

    if mode in ("send_input", "auto"):
        nx, ny = _normalize(x, y)
        before = _cursor_or_none()
        events: list[_INPUT] = [_mouse_event(_MOVE | _ABSOLUTE | _VIRTUALDESK, nx, ny)]
        for _ in range(count):
            events.append(_mouse_event(down))
            events.append(_mouse_event(up))
        sent = _send(*events)
        after = _cursor_or_none()
        moved = None
        if before is not None and after is not None:
            moved = (after["x"], after["y"]) != (before["x"], before["y"])
        report["send_input"] = {"sent": sent, "cursor_moved": moved}
        # 只有**明确观察到**光标没动，才认为这条通道没生效、继续试消息通道。
        # moved is None（光标读不到）时必须当作"不知道"而不是"没动"：
        # 本机实测读光标偶发失败，据此退回 message 会白白丢掉一次已经成功的点击。
        if mode == "send_input" or moved is not False or count > 1:
            return report

    report["message"] = _click_message(x, y, mkflag, down_msg, up_msg, count, delay_ms)
    return report


def _click_message(x: int, y: int, mkflag: int, down: int, up: int,
                   count: int, delay_ms: int) -> dict:
    """把带 lParam 坐标的鼠标消息直接投给那个坐标上的窗口。"""
    target = window_at(x, y)
    hwnd = target["hwnd"]
    if not hwnd:
        return {"sent": False, "reason": "该坐标上没有窗口"}

    pt = wt.POINT(int(x), int(y))
    _user32.ScreenToClient(hwnd, ctypes.byref(pt))
    lparam = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)

    delay = max(0, int(delay_ms)) / 1000.0
    accepted = 0
    total = 0
    for _ in range(count):
        for msg, wparam in ((_WM_MOUSEMOVE, 0), (down, mkflag), (up, 0)):
            total += 1
            if _user32.PostMessageW(hwnd, msg, wparam, lparam):
                accepted += 1
            if delay:
                time.sleep(delay)
    return {
        "sent": accepted == total,
        "accepted": accepted,
        "total": total,
        "window": {"hwnd": hwnd, "class": target["class"], "title": target["title"]},
        "client": {"x": int(pt.x), "y": int(pt.y)},
    }


def scroll(x: int, y: int, *, delta: int = -3, horizontal: bool = False) -> dict:
    """在坐标处滚轮。delta 为负是向下/向右滚，一档通常是 120。"""
    _require()
    nx, ny = _normalize(x, y)
    amount = int(delta) * 120
    flag = _HWHEEL if horizontal else _WHEEL
    sent = _send(
        _mouse_event(_MOVE | _ABSOLUTE | _VIRTUALDESK, nx, ny),
        _mouse_event(flag, nx, ny, amount),
    )
    return {"sent": sent, "x": x, "y": y, "delta": delta, "horizontal": bool(horizontal)}


def drag(x1: int, y1: int, x2: int, y2: int, *, button: str = "left",
         steps: int = 12, duration_ms: int = 300) -> dict:
    """从 (x1,y1) 拖到 (x2,y2)。

    中间必须插 MOUSEMOVE：只在起点按下、终点抬起，很多程序当成「没动过」
    而忽略整个手势。`steps` 决定插几个中间点。
    """
    _require()
    btn = str(button or "left").strip().lower()
    if btn not in _BUTTON_FLAGS:
        raise InputError(f"不认识的鼠标键 {button!r}；可用 left / right / middle")
    down, up, _mkflag, _dm, _um = _BUTTON_FLAGS[btn]

    steps = max(1, int(steps))
    pause = max(0, int(duration_ms)) / 1000.0 / steps

    nx1, ny1 = _normalize(x1, y1)
    nx2, ny2 = _normalize(x2, y2)
    sent = _send(_mouse_event(_MOVE | _ABSOLUTE | _VIRTUALDESK, nx1, ny1),
                 _mouse_event(down))

    # 中间点一律用「绝对移动」：相对移动在 MOUSEEVENTF_ABSOLUTE 未设时按
    # 归一化单位解释，跟像素不是一回事，很容易累积出几像素到几十像素的偏差。
    for i in range(1, steps + 1):
        t = i / steps
        nx = int(round(nx1 + (nx2 - nx1) * t))
        ny = int(round(ny1 + (ny2 - ny1) * t))
        sent += _send(_mouse_event(_MOVE | _ABSOLUTE | _VIRTUALDESK, nx, ny))
        if pause:
            time.sleep(pause)

    sent += _send(_mouse_event(up))
    return {"sent": sent, "from": [x1, y1], "to": [x2, y2],
            "button": btn, "steps": steps}


# ── 键盘 ────────────────────────────────────────────────────────────────────

def _parse_key(name: str) -> tuple[int, bool]:
    """键名 → (虚拟键码, 是否扩展键)。认不出就抛 InputError。"""
    raw = str(name or "").strip()
    if not raw:
        raise InputError("键名不能为空")
    if raw.lower().startswith("0x"):
        try:
            code = int(raw, 16)
        except ValueError as exc:
            raise InputError(f"看不懂的十六进制键码 {name!r}") from exc
        return code, code in _EXTENDED
    key = _ALIAS.get(raw.lower(), raw.lower())
    code = _VK.get(key)
    if code is None:
        raise InputError(f"不认识的键 {name!r}。常用键名见 actions._VK，或用 '0x1B' 这种写法")
    return code, code in _EXTENDED


def _parse_combo(keys: str) -> list[str]:
    """"ctrl+shift+s" → ["ctrl", "shift", "s"]。"""
    text = str(keys or "").strip()
    if not text:
        raise InputError("快捷键不能为空")
    parts = [p.strip() for p in text.replace(" ", "+").split("+")]
    parts = [p for p in parts if p]
    if not parts:
        raise InputError(f"快捷键 {keys!r} 解析后没有按键")
    return parts


def press(key: str) -> dict:
    """敲一下某个键（按下再抬起）。"""
    _require()
    code, extended = _parse_key(key)
    sent = _send(_key_event(code, extended=extended), _key_event(code, up=True, extended=extended))
    return {"sent": sent, "key": key, "vk": code}


def hotkey(keys: str) -> dict:
    """敲一组组合键，如 "ctrl+shift+s"。

    修饰键先按、后放，且**逆序**抬起：先放主键再放修饰键，否则在浏览器里
    常会触发「修饰键单独按下」的其他行为。
    """
    _require()
    parts = _parse_combo(keys)
    parsed = [(_parse_key(p)) for p in parts]

    sent = 0
    for code, extended in parsed:
        sent += _send(_key_event(code, extended=extended))
    for code, extended in reversed(parsed):
        sent += _send(_key_event(code, up=True, extended=extended))
    return {"sent": sent, "keys": keys, "vk": [c for c, _e in parsed]}


def type_text(text: str, *, delay_ms: int = 12) -> dict:
    """逐字符输入文本。

    走 KEYEVENTF_UNICODE：直接把 UTF-16 码元塞进按键事件，不经过键盘布局
    和输入法。对中文、符号、以及没装对应输入法的机器都能用。
    """
    _require()
    chunk = str(text or "")
    if not chunk:
        return {"sent": 0, "length": 0}

    delay = max(0, int(delay_ms)) / 1000.0
    sent = 0
    for ch in chunk:
        # 非 BMP 字符（emoji 等）在 Python 里是单个码点，但 UTF-16 需要代理对。
        # 按 utf-16-le 拆成码元逐个送，目标程序拼回来是一样的。
        raw = ch.encode("utf-16-le")
        units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
        for unit in units:
            sent += _send(_key_event(unit, unicode=True), _key_event(unit, up=True, unicode=True))
            if delay:
                time.sleep(delay)
    return {"sent": sent, "length": len(chunk)}


# ── 诊断 ────────────────────────────────────────────────────────────────────

def describe() -> dict:
    """当前平台的输入能力概况，给排障用。"""
    info: dict = {"platform": sys.platform, "inject": _IS_WINDOWS}
    if not _IS_WINDOWS:
        return info
    try:
        vx, vy, vw, vh = _virtual_rect()
        info["virtual"] = {"x": vx, "y": vy, "w": vw, "h": vh}
    except InputError as exc:
        info["virtual"] = f"不可用：{exc}"
    try:
        info["cursor"] = cursor()
    except InputError as exc:
        info["cursor"] = f"不可用：{exc}"
    try:
        from ui_locate import screen

        info["dpi_awareness"] = screen.set_dpi_aware()
    except Exception as exc:  # noqa: BLE001 - 诊断函数不该因为探针失败而失败
        info["dpi_awareness"] = f"未知：{exc}"
    return info
