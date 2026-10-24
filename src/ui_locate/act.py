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

import time
from typing import Any, Callable

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
        }, {"reason": "text"}

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

_MOUSE_ACTIONS = {"click", "double_click", "right_click", "middle_click", "hover", "move"}


def build_action(spec: dict, target: dict) -> tuple[str, dict]:
    """spec → (动作名, 传给输入层的一组参数)。

    单独拆出来是为了能在没有输入通道的机器上测：给定 spec 与 target，得到
    的那组参数是纯数据，可以逐字段断言。
    """
    action = str(spec.get("action") or "click").strip().lower()
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
        params = {"key": str(spec.get("key") or spec.get("value") or "")}
        return "press", params

    if action in ("hotkey", "shortcut"):
        params = {"keys": str(spec.get("keys") or spec.get("value") or "")}
        return "hotkey", params

    if action in ("wait", "none", "noop"):
        return "none", {}

    raise ActError(
        f"不认识的动作 {action!r}；可用：click / double_click / right_click / "
        "middle_click / hover / move / drag / scroll / type / press / hotkey"
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
        "radius": radius,
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
) -> dict:
    """定位 → 动作 → 验证，一次调用完成。

    参数说明见模块 docstring。``spec`` 里认识的键：

      action        动作名，默认 click
      text          按文字找目标（推荐）；或给 x / y
      verify        None（不验证）| "screen" | {"text": "...", "timeout_ms": n}
      settle_ms     动作后等多久再验证，默认 220
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
        timings["total_ms"] = _ms(t_all, clock)
        return {
            "ok": True,
            "stage": "done",
            "action": "none",
            "target": None,
            "target_source": None,
            "performed": None,
            "verified": None,
            "timings": timings,
        }

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

    if action == "none":
        timings["total_ms"] = _ms(t_all, clock)
        result.update({"performed": None, "verified": None, "timings": timings})
        return result

    if action == "drag":
        spec = _materialize_points(spec, probe=probe, timings=timings, clock=clock)
        params = finish_drag(params, spec, probe=probe, timings=timings, clock=clock)

    verify = spec.get("verify")
    before = None
    capture_error = None
    if verify == "screen":
        # 抓 before 图失败**不能**中断整次调用。抓屏和输入是两条独立的能力：
        # 在本机的受限进程里，抓屏会被拦（OSError: screen grab failed）而键盘
        # 注入照样可用。以前这里直接抛，结果连"动作到底有没有执行"都报不出去 ——
        # 调用方只看到 500，完全不知道该重试还是该换通道。
        try:
            before = capture(monitor)
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
        return result
    timings["act_ms"] = _ms(t0, clock)
    result["performed"] = performed

    settle = int(spec.get("settle_ms") or _DEFAULT_SETTLE_MS)
    if settle > 0:
        sleep(settle / 1000.0)

    checks: list[dict] = []
    if verify == "screen" and before is not None:
        t1 = _now(clock)
        try:
            after = capture(monitor)
        except Exception as exc:  # noqa: BLE001
            capture_error = capture_error or f"{type(exc).__name__}: {exc}"
            after = None
        if after is not None:
            checks.append(verify_screen(
                before, after, x=target["cx"], y=target["cy"], compare=compare,
                radius=int(spec.get("verify_radius") or 90),
            ))
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

        if str(step.get("action") or "").lower() == "wait":
            # 纯等待步骤：不动作，只等某段文字出现。
            # 这是「等界面加载完」最靠谱的写法 —— 比 sleep 固定秒数准得多。
            wait = pre_result
            if wait is None:
                if not step.get("text"):
                    raise ActError(f"第 {i + 1} 步是 wait，但没给 text")
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

        out = act(step, probe=probe, capture=capture, perform=perform,
                  compare=compare, sleep=sleep, clock=clock)
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
