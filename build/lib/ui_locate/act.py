"""把「找」和「做」缝成一次调用 —— P0 的核心。

**为什么要有这一层**

之前的用法是：模型调 ``locate_text`` 拿坐标 → 模型自己决定点哪儿 → 模型调
cua_driver 点击 → 模型再截图确认。每一步都是一次完整的模型往返（实测 8~12
秒）。真实跑一次「买 20 个助剂」花了 9 分半，其中 90% 的时间花在这些往返上，
而不是花在推理上 —— 检测 10ms、OCR 420ms，本来够快。

所以这一层的职责是：**让模型只发一道指令，中间所有步骤在本地跑完**。

    act({text: "确认购买", action: "click", verify: "screen"})

一次调用内部完成：定位 → 点击 → 等 settle → 截图比对 → 回报。

**为什么不在这里直接实现视觉和输入**

两个外部依赖都通过参数注入（``probe`` / ``capture`` / ``perform`` / ``compare``）。
理由是可测性：本模块全是编排逻辑 —— 重试、等待、短路、超时记账 —— 这些恰恰
是最容易写错、也最该被单测覆盖的部分。把 ``onnxruntime`` / ``cv2`` / ``ctypes``
挡在参数后面，测试就能用纯 Python 桩跑完所有分支，不必加载模型、不必开端口、
也不必有一块真屏幕。

**这里不猜，也不假装成功**

``ok: true`` 只表示「这条指令走完了」。动作有没有被目标程序吃下，由
``verify`` 字段独立回答：

  - ``verify: "screen"`` —— 点击点周围的像素变了没有（变了≠点对了，但没变一定有问题）
  - ``verify: {"text": "..."}`` —— 动作后在限定时间内有没有出现这段文字（更强的判据）

两者都给时先比对再等文字，两个都过才叫 ``verified: true``。
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from ui_locate import griddiff

# 一次动作结束后到「画面稳定」的默认等待。200ms 覆盖大多数界面切换动画的
# 首帧到可见变化；再长就是白等 —— 需要更久的场景由调用方显式给 settle_ms。
_DEFAULT_SETTLE_MS = 220

# 轮询间隔。太密会拖慢服务（每次 poll 都要跑一次检测+OCR），太疏会错过
# 一闪而过的中间态。实测 150ms 在「点完等界面」这类场景里够用。
_DEFAULT_POLL_MS = 150

# 判定「画面变了」的阈值（平均像素差，0~255）。
# 参考实测：游戏里静止画面连抓两张差 2.8（背景动画/粒子），敲 ESC 差 4.3。
# 所以阈值必须高于纯噪声，又不能高到把「按钮变灰」这种小变化漏掉。
_DEFAULT_CHANGE_THRESHOLD = 3.5

# 「等画面停下来」的上限。多数界面动画 100~400ms；过场 / 加载更久的由调用方给
# settle_timeout_ms，或者用 verify_text 等具体的字。
_DEFAULT_STABLE_TIMEOUT_MS = 1500
_STABLE_MIN_MS = 60
_STABLE_POLL_MS = 50

# 网格差分里「点击点附近有一块变化」算生效的强度下限（格内平均差，0~255）。
# 远高于粒子/水面的 3~6，按钮高亮、弹出面板一般 15 以上。
_REGION_STRENGTH = 16.0
# 整屏比对（纯键盘动作）时，变化格占比超过它也算生效：开关菜单、切换页签。
_SCREEN_FRAC = 0.05

# 动作前先量一下「什么都不做时画面自己变多少」：连抓三帧、间隔 40ms。
# 水面、粒子、视频背景下，静止画面相邻帧就有一半格子在变、平均差 8 以上，
# 绝对阈值 3.5 在这种画面上等于永远判「生效」（实测点空白水面 verified:true）。
_NOISE_GAP_MS = 40
# 噪声帧里变化格占比超过它就当「动态画面」：此后判据全部相对噪声来定。
_ANIMATED_FRAC = 0.01


class ActError(RuntimeError):
    """指令本身写错了（缺参数、动作名不认识）。"""


def _now(clock: Callable[[], float]) -> float:
    return clock()


def _ms(start: float, clock: Callable[[], float]) -> float:
    return round((clock() - start) * 1000, 1)


# ---------------------------------------------------------------------------
# 单步：定位
# ---------------------------------------------------------------------------

def resolve_target(
    spec: dict,
    *,
    probe: Callable[..., dict | None],
    timings: dict,
    clock: Callable[[], float],
) -> tuple[dict | None, dict]:
    """把 spec 变成「要点哪个像素」。

    三种来源，优先级从高到低：

      1. ``text``  —— 屏幕上按文字找（最常用；界面一变坐标就变，文字不变）
      2. ``x`` + ``y`` —— 直接给坐标（已知位置、或上一次调用返回的坐标）
      3. ``at``    —— 上一次结果里的元素引用（``steps`` 内部用）

    返回 ``(target, info)``；找不到时 target 为 None，info 里带原因与候选数。
    """
    t0 = _now(clock)
    text = spec.get("text")
    if text:
        hit = probe(text, spec)
        timings["find_ms"] = _ms(t0, clock)
        if hit is None:
            return None, {"reason": "not-found", "text": text}
        return {
            "text": hit.get("matched") or text,
            "cx": int(round(float(hit["cx"]))),
            "cy": int(round(float(hit["cy"]))),
            "w": int(round(float(hit.get("w") or 0))),
            "h": int(round(float(hit.get("h") or 0))),
            "conf": float(hit.get("conf") or 0),
            "score": hit.get("score"),
            "match_tier": hit.get("match_tier"),
        }, {"reason": "text", "match_tier": hit.get("match_tier")}

    if spec.get("x") is not None and spec.get("y") is not None:
        timings["find_ms"] = _ms(t0, clock)
        return {
            "text": None,
            "cx": int(round(float(spec["x"]))),
            "cy": int(round(float(spec["y"]))),
            "w": 0,
            "h": 0,
            "conf": 1.0,
            "score": None,
        }, {"reason": "coordinates"}

    at = spec.get("at")
    if isinstance(at, dict) and at.get("cx") is not None:
        timings["find_ms"] = _ms(t0, clock)
        return {
            "text": at.get("text"),
            "cx": int(round(float(at["cx"]))),
            "cy": int(round(float(at["cy"]))),
            "w": int(round(float(at.get("w") or 0))),
            "h": int(round(float(at.get("h") or 0))),
            "conf": float(at.get("conf") or 1.0),
            "score": None,
        }, {"reason": "reference"}

    timings["find_ms"] = _ms(t0, clock)
    raise ActError("需要 text（按文字找）或 x 与 y（直接给坐标）")


def _point_from(value: Any, *, probe: Callable[..., dict | None], spec: dict,
                timings: dict, clock: Callable[[], float],
                what: str) -> tuple[int, int] | None:
    """把「一个点」的三种写法解成坐标：``{cx, cy}`` / 数字 / 屏幕上的文字。"""
    if value is None:
        return None
    if isinstance(value, dict) and value.get("cx") is not None:
        return int(round(float(value["cx"]))), int(round(float(value["cy"])))
    if isinstance(value, (int, float)):
        # 只给了一个数，无法构成点 —— 报错比猜好
        raise ActError(f"{what} 只给了半个坐标：{value!r}")
    if isinstance(value, list) and len(value) == 2:
        return int(round(float(value[0]))), int(round(float(value[1])))
    if isinstance(value, str) and _XY.match(value):
        # "40,-540" 是坐标，不是要在屏幕上找的文字（MCP 文档里就这么写的）
        a, b = value.split(",")
        return int(round(float(a))), int(round(float(b)))
    if isinstance(value, str) and value.strip():
        t0 = _now(clock)
        hit = probe(value.strip(), spec)
        timings["find_ms"] = round(timings.get("find_ms", 0.0) + _ms(t0, clock), 1)
        if hit is None:
            raise ActError(f"{what} 在屏幕上找不到：{value!r}")
        return int(round(float(hit["cx"]))), int(round(float(hit["cy"])))
    return None


# ---------------------------------------------------------------------------
# 单步：动作
# ---------------------------------------------------------------------------

_XY = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*$")


def _keys(value: Any) -> str:
    """键名可以是 "ctrl+a"，也可以是 ["ctrl", "a"]。以前列表被 str() 成 "['ctrl', 'a']"。"""
    if isinstance(value, (list, tuple)):
        return "+".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "")


_MOUSE_ACTIONS = {"click", "double_click", "right_click", "middle_click", "hover", "move"}
_KEYBOARD_ACTIONS = {"type", "write", "press", "key", "hold", "hold_key", "hotkey", "shortcut"}


def build_action(spec: dict, target: dict) -> tuple[str, dict]:
    """spec → (动作名, 传给输入层的一组参数)。

    单独拆出来是为了能在没有输入通道的机器上测：给定 spec 与 target，得到
    的那组参数是纯数据，可以逐字段断言。
    """
    action = str(spec.get("action") or "click").strip().lower()
    params: dict[str, Any] = {}
    action, params = _build(action, spec, target)
    # 输入方式随动作一起带下去（没给就由输入层按 环境变量 / runtime.json 决定）
    if spec.get("pointer") and action in _MOUSE_ACTIONS | {"drag", "scroll"}:
        params["pointer"] = str(spec["pointer"])
    if spec.get("keys_mode") and action in ("press", "hotkey", "hold"):
        params["keys_mode"] = str(spec["keys_mode"])
    # 前台：输入层发 SendInput 前把目标窗口切到前台（见 server._focus）
    if action != "none":
        win = spec.get("window")
        if not win and _is_window_spec(spec.get("monitor")):
            win = spec.get("monitor")
        if win:
            params["window"] = win
        if spec.get("focus") not in (None, "", "auto"):
            params["focus"] = spec["focus"]
    return action, params


def _is_window_spec(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    low = value.strip().lower()
    return low.startswith("window:") or low.startswith("hwnd:")


def _build(action: str, spec: dict, target: dict) -> tuple[str, dict]:
    params: dict[str, Any] = {}

    if action in _MOUSE_ACTIONS:
        params = {"x": target["cx"], "y": target["cy"]}
        if action in ("click", "double_click", "right_click", "middle_click"):
            params["button"] = {
                "click": "left",
                "double_click": "left",
                "right_click": "right",
                "middle_click": "middle",
            }[action]
            params["count"] = 2 if action == "double_click" else int(spec.get("count") or 1)
            params["delivery"] = str(spec.get("delivery") or "auto")
            params["delay_ms"] = int(spec.get("click_delay_ms") or 40)
        return action, params

    if action == "drag":
        # 起点：默认就是要找的那个元素；也允许 from / from_x+from_y 显式给。
        # 注意这里只认已经解成坐标的写法 —— 「from 写成一段文字」要跑检测，
        # 那件事在 act() 里做（build_action 是纯函数，不碰外部世界）。
        start = spec.get("from")
        if isinstance(start, dict) and start.get("cx") is not None:
            params["x1"] = int(round(float(start["cx"])))
            params["y1"] = int(round(float(start["cy"])))
        elif isinstance(start, (list, tuple)) and len(start) == 2:
            params["x1"] = int(round(float(start[0])))
            params["y1"] = int(round(float(start[1])))
        elif spec.get("from_x") is not None and spec.get("from_y") is not None:
            params["x1"] = int(round(float(spec["from_x"])))
            params["y1"] = int(round(float(spec["from_y"])))
        else:
            params["x1"], params["y1"] = target["cx"], target["cy"]
        params["x2"] = None   # 终点由 finish_drag 补，因为可能要按文字找
        params["y2"] = None
        params["button"] = str(spec.get("button") or "left")
        params["steps"] = int(spec.get("steps") or 12)
        params["duration_ms"] = int(spec.get("duration_ms") or 300)
        return action, params

    if action == "scroll":
        params = {
            "x": target["cx"],
            "y": target["cy"],
            "delta": int(spec.get("delta") or -3),
            "horizontal": bool(spec.get("horizontal")),
        }
        return action, params

    if action in ("type", "write"):
        params = {"text": str(spec.get("value") or spec.get("input") or ""),
                  "delay_ms": int(spec.get("type_delay_ms") or 12)}
        return "type", params

    if action in ("press", "key"):
        params = {"key": _keys(spec.get("key") or spec.get("value") or "")}
        if spec.get("hold_ms"):
            params["hold_ms"] = int(spec["hold_ms"])
        return "press", params

    if action in ("hold", "hold_key"):
        params = {"keys": _keys(spec.get("keys") or spec.get("key") or spec.get("value") or ""),
                  "duration_ms": int(spec.get("duration_ms") or 500)}
        return "hold", params

    if action in ("hotkey", "shortcut"):
        params = {"keys": _keys(spec.get("keys") or spec.get("value") or "")}
        return "hotkey", params

    if action in ("wait", "none", "noop"):
        return "none", {}

    raise ActError(
        f"不认识的动作 {action!r}；可用：click / double_click / right_click / "
        "middle_click / hover / move / drag / scroll / type / press / hold / hotkey"
    )


def finish_drag(params: dict, spec: dict, *, probe: Callable[..., dict | None],
                timings: dict, clock: Callable[[], float]) -> dict:
    """drag 的终点可以写成 ``to_x``/``to_y``、``to``（点或文字）、或 ``to: "某段文字"``。"""
    out = dict(params)
    if out.get("x2") is None or out.get("y2") is None:
        end = _point_from(spec.get("to"), probe=probe, spec=spec, timings=timings,
                          clock=clock, what="拖动终点 to")
        if end is not None:
            out["x2"], out["y2"] = end
        elif spec.get("to_x") is not None and spec.get("to_y") is not None:
            out["x2"] = int(round(float(spec["to_x"])))
            out["y2"] = int(round(float(spec["to_y"])))
        else:
            # 不猜：没给终点就是原地拖，几乎没有意义，早报错比默默点一下好
            raise ActError("drag 需要终点：to / to_x+to_y / 或 to 写成屏幕上的文字")
    return out


def _materialize_points(spec: dict, *, probe: Callable[..., dict | None],
                        timings: dict, clock: Callable[[], float]) -> dict:
    """把 drag 里写成文字的 ``from`` / ``to`` 换成坐标。

    放在 act() 里而不是 build_action 里，是为了让 build_action 保持纯函数 ——
    它只做「参数整形」，不该为了拿一个坐标去跑一次检测。
    """
    out = dict(spec)
    for key, name in (("from", "拖动起点 from"), ("to", "拖动终点 to")):
        value = out.get(key)
        if isinstance(value, str) and value.strip():
            pt = _point_from(value, probe=probe, spec=spec, timings=timings,
                             clock=clock, what=name)
            out[key] = {"cx": pt[0], "cy": pt[1]}
    return out


# ---------------------------------------------------------------------------
# 验证
# ---------------------------------------------------------------------------

def verify_screen(
    before: Any,
    after: Any,
    *,
    x: int,
    y: int,
    compare: Callable[..., float | None],
    radius: int = 90,
) -> dict:
    """比对动作前后点击点周围的像素。

    只看点击点附近而不是整屏，是因为整屏里有太多与本次动作无关的变化
    （时钟、光标闪烁、背景动画）。实测整屏平均差在游戏里静止时就有 2.8。
    x / y 为 None（纯键盘动作、没有目标点）时整屏比，结果里 ``scope: "screen"``。
    """
    diff = compare(before, after, x, y, radius)
    if diff is None:
        return {"kind": "screen", "changed": None, "changed_enough": None,
                "note": "无法比对（截图缺失或尺寸不一致）"}
    return {
        "kind": "screen",
        "changed": round(float(diff), 2),
        "changed_enough": float(diff) > _DEFAULT_CHANGE_THRESHOLD,
        "threshold": _DEFAULT_CHANGE_THRESHOLD,
        "radius": radius if x is not None else None,
        "scope": "point" if x is not None else "screen",
    }


def wait_for_text(
    text: str,
    spec: dict,
    *,
    probe: Callable[..., dict | None],
    timeout_ms: int,
    poll_ms: int,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> dict:
    """轮询等某段文字出现在屏幕上。返回命中元素或超时。

    这是 P2 里最值钱的一个动作：游戏/安装程序里大量时间花在「等某个界面出来」，
    以前只能由模型反复截图来判断，一次往返 8~12 秒。
    """
    start = _now(clock)
    deadline = start + max(timeout_ms, 0) / 1000.0
    polls = 0
    while True:
        polls += 1
        hit = probe(text, spec)
        if hit is not None:
            return {
                "found": True,
                "matched": hit.get("matched"),
                "cx": int(round(float(hit["cx"]))),
                "cy": int(round(float(hit["cy"]))),
                "w": int(round(float(hit.get("w") or 0))),
                "h": int(round(float(hit.get("h") or 0))),
                "conf": round(float(hit.get("conf") or 0), 3),
                "elapsed_ms": _ms(start, clock),
                "polls": polls,
            }
        if _now(clock) >= deadline:
            return {
                "found": False,
                "text": text,
                "elapsed_ms": _ms(start, clock),
                "polls": polls,
                "timeout_ms": timeout_ms,
            }
        sleep(max(poll_ms, 10) / 1000.0)


# ---------------------------------------------------------------------------
# 组合：一次调用
# ---------------------------------------------------------------------------

def act(
    spec: dict,
    *,
    probe: Callable[..., dict | None],
    capture: Callable[[Any], Any],
    perform: Callable[[str, dict], dict],
    compare: Callable[..., float | None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    differ: Callable[[Any, Any], dict | None] | None = None,
) -> dict:
    """定位 → 动作 → 验证，一次调用完成。

    参数说明见模块 docstring。``spec`` 里认识的键：

      action        动作名，默认 click
      text          按文字找目标（推荐）；或给 x / y
      verify        None（不验证）| "screen" | {"text": "...", "timeout_ms": n}
      settle        "auto"（默认）| "stable" | "fixed" | "none"
                    auto：verify="screen" 且有 differ 时等画面停下来（stable），
                    否则固定等 settle_ms。给了 settle_ms 而没给 settle 时按 fixed
      settle_ms     固定等待的毫秒数，默认 220
      settle_timeout_ms  stable 最多等多久，默认 1500
      focus         "auto"（默认）| 窗口写法 | false —— 见输入层，act 只是透传
      on_fail       "abort"（默认）| "continue"，只在 steps 里有意义

    返回的 ``ok`` 只表示流程走完；``verified`` 才表示「确实生效了」。
    """
    timings: dict[str, float] = {}
    t_all = _now(clock)
    monitor = spec.get("monitor")

    # 纯等待 / 空动作没有目标可找 —— 必须在 resolve_target 之前截住。
    # 之前这里漏了，导致 steps 里的 {"action": "wait"} 直接抛「需要 text」，
    # 而它明明是合法的一步（"等界面加载完"）。
    if str(spec.get("action") or "").strip().lower() in ("wait", "none", "noop") \
            and not spec.get("text") and spec.get("x") is None:
        settle_info = None
        ok = True
        if str(spec.get("action")).strip().lower() == "wait" and differ is not None \
                and str(spec.get("until") or "stable").lower() == "stable":
            # 不给文字的 wait = 等画面停下来（加载完、动画放完）
            try:
                settle_info, _ = wait_stable(
                    lambda: capture(monitor), differ, sleep=sleep, clock=clock,
                    timeout_ms=int(spec.get("timeout_ms") or spec.get("settle_timeout_ms") or 3000),
                    min_ms=0)
                ok = bool(settle_info["stable"])
            except Exception as exc:  # noqa: BLE001
                settle_info = {"stable": None, "error": f"{type(exc).__name__}: {exc}"}
                ok = False
            timings["settle_ms"] = settle_info.get("elapsed_ms", 0.0)
        timings["total_ms"] = _ms(t_all, clock)
        out = {
            "ok": ok,
            "stage": "done" if ok else "wait",
            "action": "none",
            "target": None,
            "target_source": None,
            "performed": None,
            "verified": None,
            "timings": timings,
        }
        if settle_info is not None:
            out["settle"] = {"mode": "stable", **settle_info}
            if not ok:
                out["error"] = "画面在时限内一直在变（动画 / 过场 / 视频），没等到静止"
        return out

    act_name = str(spec.get("action") or "click").strip().lower()
    has_target = bool(spec.get("text")) or spec.get("x") is not None or isinstance(spec.get("at"), dict)
    if act_name == "drag" and not has_target:
        # drag 只给了起点（from / from_x+from_y）：起点就是目标
        start = None
        if spec.get("from_x") is not None and spec.get("from_y") is not None:
            start = (int(round(float(spec["from_x"]))), int(round(float(spec["from_y"]))))
        elif spec.get("from") not in (None, ""):
            start = _point_from(spec["from"], probe=probe, spec=spec, timings=timings,
                                clock=clock, what="拖动起点 from")
        if start is not None:
            spec = {**spec, "at": {"text": None, "cx": start[0], "cy": start[1]}}
            has_target = True
    if act_name in _KEYBOARD_ACTIONS and not has_target:
        # 纯键盘动作不需要目标：游戏里「按 ESC」「按住 W」「Ctrl+S」本来就没有
        # 可点的东西。以前这里要求 text 或 x/y，调用方只好随便编一个坐标。
        timings["find_ms"] = 0.0
        target = {"text": None, "cx": None, "cy": None, "w": 0, "h": 0, "conf": 1.0, "score": None}
        info = {"reason": "keyboard"}
    else:
        target, info = resolve_target(spec, probe=probe, timings=timings, clock=clock)
    if target is None:
        timings["total_ms"] = _ms(t_all, clock)
        return {
            "ok": False,
            "stage": "find",
            "error": f"屏幕上没找到 {info.get('text')!r}",
            "target": None,
            "timings": timings,
        }

    action, params = build_action(spec, target)
    result: dict[str, Any] = {
        "ok": True,
        "target": target,
        "target_source": info.get("reason"),
        "action": action,
    }

    # 命中靠哪一档。exact 最可靠，starts 次之（通常就是图标被 OCR 读成了
    # 一个字符，剥掉之后才相等）；contains 在动手路径上已经被 probe 拦掉，
    # 真出现在这里说明调用方自己绕过了 _probe_hit，值得在结果里留痕。
    # 不把它做成"是否成功"的判据 —— 成没成看 verified，那是另一件事。
    if info.get("match_tier"):
        result["match_tier"] = info["match_tier"]

    if action == "none":
        timings["total_ms"] = _ms(t_all, clock)
        result.update({"performed": None, "verified": None, "timings": timings})
        return result

    if action == "drag":
        spec = _materialize_points(spec, probe=probe, timings=timings, clock=clock)
        params = finish_drag(params, spec, probe=probe, timings=timings, clock=clock)

    verify = spec.get("verify")
    before = None
    noise: dict | None = None
    capture_error = None
    if verify == "screen":
        # 抓 before 图失败**不能**中断整次调用。抓屏和输入是两条独立的能力：
        # 在本机的受限进程里，抓屏会被拦（OSError: screen grab failed）而键盘
        # 注入照样可用。以前这里直接抛，结果连"动作到底有没有执行"都报不出去 ——
        # 调用方只看到 500，完全不知道该重试还是该换通道。
        try:
            before = capture(monitor)
            if differ is not None and spec.get("noise_ms", _NOISE_GAP_MS):
                before, noise = sample_noise(
                    before, lambda: capture(monitor), differ, compare,
                    x=target["cx"], y=target["cy"],
                    radius=int(spec.get("verify_radius") or 90),
                    gap_ms=int(spec.get("noise_ms") or _NOISE_GAP_MS), sleep=sleep)
        except Exception as exc:  # noqa: BLE001
            capture_error = f"{type(exc).__name__}: {exc}"

    t0 = _now(clock)
    try:
        performed = perform(action, params)
    except Exception as exc:  # noqa: BLE001 - 输入层抛什么都得变成结构化失败
        timings["act_ms"] = _ms(t0, clock)
        timings["total_ms"] = _ms(t_all, clock)
        result.update({
            "ok": False,
            "stage": "act",
            "error": f"{type(exc).__name__}: {exc}",
            "performed": None,
            "timings": timings,
        })
        # 这条路径也必须带上 capture_error。抓屏失败与动作失败同时发生时，
        # 只说「动作失败」等于把另一半真相扔掉：调用方会去查目标窗口，
        # 而真正的问题可能是本进程根本没有抓屏/注入权限 —— 两件事该查的
        # 方向完全相反。曾经就是这里早退，e2e 里只看到 stage:act 而不知道
        # 连 before 图都没拿到。
        if capture_error:
            result["verify_unavailable"] = capture_error
        return result
    timings["act_ms"] = _ms(t0, clock)
    result["performed"] = performed

    mode = settle_mode(spec, verify, can_diff=differ is not None and before is not None)
    after = None
    if mode == "stable":
        t1 = _now(clock)
        try:
            report, after = wait_stable(
                lambda: capture(monitor), differ, sleep=sleep, clock=clock,
                timeout_ms=int(spec.get("settle_timeout_ms") or _DEFAULT_STABLE_TIMEOUT_MS),
                ignore=noise.get("_mask") if noise else None)
            result["settle"] = {"mode": "stable", **report}
        except Exception as exc:  # noqa: BLE001
            capture_error = capture_error or f"{type(exc).__name__}: {exc}"
            result["settle"] = {"mode": "stable", "stable": None}
        timings["settle_ms"] = _ms(t1, clock)
    elif mode == "fixed":
        settle = int(spec.get("settle_ms") or _DEFAULT_SETTLE_MS)
        if settle > 0:
            sleep(settle / 1000.0)

    checks: list[dict] = []
    if verify == "screen" and before is not None:
        t1 = _now(clock)
        if after is None:
            try:
                after = capture(monitor)
            except Exception as exc:  # noqa: BLE001
                capture_error = capture_error or f"{type(exc).__name__}: {exc}"
                after = None
        if after is not None:
            check = verify_screen(
                before, after, x=target["cx"], y=target["cy"], compare=compare,
                radius=int(spec.get("verify_radius") or 90),
            )
            if noise is not None:
                _relative_to_noise(check, noise)
            if differ is not None:
                _add_regions(check, before, after, differ, target,
                             radius=int(spec.get("verify_radius") or 90), noise=noise)
            if noise is not None:
                check["noise"] = {k: v for k, v in noise.items() if not k.startswith("_")}
            checks.append(check)
        timings["verify_ms"] = _ms(t1, clock)

    elif isinstance(verify, dict) and verify.get("text"):
        t1 = _now(clock)
        wait = wait_for_text(
            str(verify["text"]), spec,
            probe=probe,
            timeout_ms=int(verify.get("timeout_ms") or 3000),
            poll_ms=int(verify.get("poll_ms") or _DEFAULT_POLL_MS),
            sleep=sleep, clock=clock,
        )
        checks.append({"kind": "text", **wait})
        timings["verify_ms"] = _ms(t1, clock)

    result["timings"] = timings
    if checks:
        result["verify"] = checks[0]
        result["verified"] = _checks_pass(checks)
    else:
        result["verify"] = None
        result["verified"] = None
    if capture_error:
        # 把原因说清楚：不是"没生效"，是"没法判断"。这两个对调用方的含义完全不同。
        result["verify_unavailable"] = capture_error

    timings["total_ms"] = _ms(t_all, clock)
    return result


def settle_mode(spec: dict, verify: Any, *, can_diff: bool) -> str:
    """动作之后怎么等：stable（等画面停）/ fixed（固定毫秒）/ none。"""
    raw = str(spec.get("settle") or "").strip().lower()
    if raw in ("none", "no", "off", "0"):
        return "none"
    if raw == "fixed":
        return "fixed"
    if raw == "stable":
        return "stable" if can_diff else "fixed"
    if spec.get("settle_ms") not in (None, "", 0):
        return "fixed"       # 老写法：显式给了毫秒数就照办
    if verify == "screen" and can_diff:
        return "stable"
    return "fixed"


def wait_stable(
    grab: Callable[[], Any],
    differ: Callable[[Any, Any], dict | None],
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    timeout_ms: int = _DEFAULT_STABLE_TIMEOUT_MS,
    min_ms: int = _STABLE_MIN_MS,
    poll_ms: int = _STABLE_POLL_MS,
    quiet_frames: int = 2,
    ignore: Any = None,
) -> tuple[dict, Any]:
    """反复抓帧直到连续 ``quiet_frames`` 次相邻两帧几乎不变。返回 (报告, 最后一帧)。

    最后一帧直接当「动作之后」那张用，不必再抓。一直在动就到时限为止，
    ``stable: false`` —— 这不算失败，验证照常做，只是调用方该知道画面还在动。
    """
    t0 = _now(clock)
    if min_ms > 0:
        sleep(min_ms / 1000.0)
    prev = grab()
    frames, quiet, last_cells = 1, 0, None
    deadline = t0 + max(timeout_ms, 0) / 1000.0
    while quiet < quiet_frames and _now(clock) < deadline:
        sleep(max(poll_ms, 5) / 1000.0)
        cur = grab()
        frames += 1
        d = differ(prev, cur, ignore=ignore) if ignore is not None else differ(prev, cur)
        if d is None:                 # 尺寸变了（切全屏 / 改分辨率）：重新数
            quiet = 0
        else:
            last_cells = d.get("changed_cells")
            quiet = quiet + 1 if griddiff.is_quiet(d) else 0
        prev = cur
    return {
        "stable": quiet >= quiet_frames,
        "elapsed_ms": _ms(t0, clock),
        "frames": frames,
        "last_changed_cells": last_cells,
    }, prev


def _add_regions(check: dict, before: Any, after: Any,
                 differ: Callable[..., dict | None], target: dict, *, radius: int,
                 noise: dict | None = None) -> None:
    """给 screen 验证补上网格差分：哪些区域变了，以及据此能不能判定生效。

    平均差只看点击点周围 180px 的方块：小按钮变色被方块里不变的部分稀释，
    常常过不了阈值。网格差分按 32px 格子算，按钮那一格的变化不会被摊薄。
    """
    mask = noise.get("_mask") if noise else None
    animated = bool(noise and noise.get("animated"))
    try:
        d = differ(before, after, ignore=mask) if mask is not None else differ(before, after)
    except Exception:  # noqa: BLE001 - 差分只是补充信息，失败不影响原判定
        return
    if not d:
        return
    check["changed_cells"] = d.get("changed_cells")
    check["changed_frac"] = d.get("changed_frac")
    check["regions"] = d.get("regions", [])[:5]
    if check.get("changed_enough"):
        check["by"] = "mean"
        return
    cx, cy = target.get("cx"), target.get("cy")
    if cx is not None and cy is not None:
        hit = griddiff.near(d, cx, cy, radius)
        # 动态画面里一格大小的变化多半是飘过的粒子，至少要两格
        if hit is not None and hit["strength"] >= _REGION_STRENGTH \
                and (not animated or hit["cells"] >= 2):
            check["changed_enough"] = True
            check["by"] = "region"
            check["region"] = hit
    elif (d.get("changed_frac") or 0) >= _SCREEN_FRAC:
        check["changed_enough"] = True
        check["by"] = "region"


def sample_noise(first: Any, grab: Callable[[], Any], differ: Callable[..., dict | None],
                 compare: Callable[..., float | None], *, x: Any, y: Any, radius: int,
                 gap_ms: int, sleep: Callable[[float], None]) -> tuple[Any, dict]:
    """动作前再抓两帧，量出画面自己在变的程度。返回 (最新一帧, 噪声)。

    最新一帧当「动作之前」用（离动作最近）。噪声里：
      changed_frac  静止时变化格占比（两次里取大）
      mean          静止时点击点附近（或整屏）的平均差（两次里取大）
      animated      是不是动态画面
      _mask         静止时就在变的格子，外扩一格（粒子会漂）—— 之后的比对忽略它们
    """
    frames = [first]
    for _ in range(2):
        sleep(max(gap_ms, 1) / 1000.0)
        frames.append(grab())
    mask = None
    frac = 0.0
    mean = 0.0
    for a, b in zip(frames, frames[1:]):
        d = differ(a, b)
        if d is None:
            continue
        frac = max(frac, float(d.get("changed_frac") or 0.0))
        m = d.get("_mask")
        if m is not None:
            mask = m.copy() if mask is None else (mask | m)
        v = compare(a, b, x, y, radius)
        if v is not None:
            mean = max(mean, float(v))
    noise = {"changed_frac": round(frac, 4), "mean": round(mean, 2),
             "animated": frac > _ANIMATED_FRAC}
    if mask is not None:
        noise["_mask"] = griddiff.dilate(mask)
    return frames[-1], noise


def _relative_to_noise(check: dict, noise: dict) -> None:
    """平均差的阈值随噪声抬高：变化必须明显超过「什么都不做时」的水平。"""
    if check.get("changed") is None:
        return
    threshold = max(_DEFAULT_CHANGE_THRESHOLD, noise["mean"] * 2 + 1.5)
    check["threshold"] = round(threshold, 2)
    check["changed_enough"] = float(check["changed"]) > threshold


def _checks_pass(checks: list[dict]) -> bool:
    """所有检查都通过才算 verified。无法判定（None）一律算不通过 —— 宁可多说一句
    「没验证上」，也不要让调用方以为已经生效。
    """
    for c in checks:
        if c.get("kind") == "screen" and c.get("changed_enough") is not True:
            return False
        if c.get("kind") == "text" and c.get("found") is not True:
            return False
    return True


# ---------------------------------------------------------------------------
# 组合：多步
# ---------------------------------------------------------------------------

def run_steps(
    steps: list[dict],
    *,
    probe: Callable[..., dict | None],
    capture: Callable[[Any], Any],
    perform: Callable[[str, dict], dict],
    compare: Callable[..., float | None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    differ: Callable[[Any, Any], dict | None] | None = None,
) -> dict:
    """按顺序跑一串 ``act``，任一步失败即停（除非该步写了 ``on_fail: "continue"``）。

    为什么要有这个：像「打开背包 → 切到材料页 → 找到助剂 → 拉到 20 → 确认」
    这种流程，拆成五次模型往返就是五倍延迟。合成一条指令，中间一旦哪步失败，
    返回里直接指出是第几步、卡在哪个动作上 —— 模型据此重规划一次即可。

    每步可额外带：

      ``wait_before``  —— 动作前先等某段文字出现（等界面加载完，比 sleep 靠谱）
                          写成 ``"确认购买"`` 或 ``{"text": "...", "timeout_ms": 8000}``
      ``on_fail``      —— ``"abort"``（默认）| ``"continue"``
    """
    if not isinstance(steps, list) or not steps:
        raise ActError("steps 需要一个非空数组")
    if len(steps) > 40:
        raise ActError("steps 一次最多 40 步；更长的流程请分两次调用")

    t_all = _now(clock)
    done: list[dict] = []
    for i, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise ActError(f"第 {i + 1} 步不是对象")
        step = dict(raw)

        pre = step.get("wait_before")
        pre_result = None
        if pre:
            wait_spec = dict(step)
            if isinstance(pre, str):
                text, timeout = pre, int(step.get("wait_timeout_ms") or 8000)
            else:
                text = str(pre.get("text") or "")
                timeout = int(pre.get("timeout_ms") or step.get("wait_timeout_ms") or 8000)
            if text:
                wait_spec["text"] = text
                pre_result = wait_for_text(
                    text, wait_spec,
                    probe=probe, timeout_ms=timeout,
                    poll_ms=int(step.get("poll_ms") or _DEFAULT_POLL_MS),
                    sleep=sleep, clock=clock,
                )
                if not pre_result["found"]:
                    entry = {
                        "index": i,
                        "ok": False,
                        "stage": "wait_before",
                        "error": f"等 {text!r} 超时（{timeout}ms）",
                        "wait": pre_result,
                    }
                    done.append(entry)
                    if step.get("on_fail") == "continue":
                        continue
                    return _steps_failure(done, i, t_all, clock, "wait_before")

        if str(step.get("action") or "").lower() == "wait" and (step.get("text") or pre_result):
            # 纯等待步骤：不动作，只等某段文字出现。
            # 这是「等界面加载完」最靠谱的写法 —— 比 sleep 固定秒数准得多。
            wait = pre_result
            if wait is None:
                wait = wait_for_text(
                    str(step["text"]), step,
                    probe=probe, timeout_ms=int(step.get("timeout_ms") or 8000),
                    poll_ms=int(step.get("poll_ms") or _DEFAULT_POLL_MS),
                    sleep=sleep, clock=clock,
                )
            entry = {"index": i, "ok": bool(wait["found"]), "stage": "wait", "wait": wait}
            if not entry["ok"]:
                entry["error"] = f"等 {step.get('text')!r} 超时"
            done.append(entry)
            if not entry["ok"] and step.get("on_fail") != "continue":
                return _steps_failure(done, i, t_all, clock, "wait")
            continue

        if str(step.get("action") or "").lower() == "wait" and differ is None:
            raise ActError(f"第 {i + 1} 步是 wait，但没给 text（等画面静止需要抓屏比对）")
        out = act(step, probe=probe, capture=capture, perform=perform,
                  compare=compare, sleep=sleep, clock=clock, differ=differ)
        entry = {"index": i, **out}
        if pre_result is not None:
            entry["wait_before"] = pre_result
        done.append(entry)

        if not out["ok"]:
            if step.get("on_fail") == "continue":
                continue
            return _steps_failure(done, i, t_all, clock, out.get("stage") or "act")

    return {
        "ok": True,
        "stage": "done",
        "steps": done,
        "completed": len(done),
        "timings": {"total_ms": _ms(t_all, clock)},
    }


def _steps_failure(done: list[dict], index: int, t_all: float,
                   clock: Callable[[], float], stage: str) -> dict:
    return {
        "ok": False,
        "stage": stage,
        "failed_index": index,
        "error": f"第 {index + 1} 步失败（{stage}）",
        "steps": done,
        "completed": len([d for d in done if d.get("ok")]),
        "timings": {"total_ms": _ms(t_all, clock)},
    }
