"""常驻视觉定位服务：启动时付一次预热代价，之后每次请求只要十几毫秒。

为什么必须常驻：
  CUDA 后端下，每个**新进程**的首次 Conv 都要约 35 秒（cuDNN 要为 113 个
  卷积层逐一枚举算法，8GB 卡上无法跳过；EXHAUSTIVE/HEURISTIC、workspace
  开关都不影响这个数字）。同一进程内第二次建 session 只花约 1 秒，因为
  cuDNN 的算法选择结果在进程级缓存。
  DirectML 后端没有这个问题（建会话 1.5 秒，首次推理直接就是稳态速度），
  但常驻的结构照旧 —— 换后端不该改架构，而且常驻还顺带省掉了每次调用重新
  读 77MB 模型的 IO。

端点（全部 JSON）：
  GET  /health                     存活与预热状态
  POST /detect   {"path": "..."}   检测指定图片
  POST /detect                    body 直接是 PNG/JPEG 字节
  GET  /find?text=保存             按文字找元素（服务内部先检测+OCR）
  POST /locate   {"text": "保存"}  只返回坐标，不点击
  POST /act      {"text": "...", "action": "click", "verify": "screen"}
                                   定位 → 动作 → 验证，一次请求走完
  POST /steps    {"steps": [ ... ]} 一串 act 顺序执行，任一步失败即停

关于「谁来做动作」：本服务确实会调 SendInput / PostMessage（`actions.py`），
但**不保证生效**。实测两种截然不同的情况：桌面程序在前台时光标移动通道正常
（`cursor_moved: true`），而全屏游戏捕获鼠标时 SetCursorPos 全部失败——所以
「锁光标」是前台那个程序造成的，不是本进程的权限问题，换一个前台就可能变。
每个动作都可以用 `delivery` 指定通道，并且 `/act` 一律回带独立的验证结果 ——
调用方据 `verified` 判断到底成没成，而不是信 `ok`。

启动：
  uv run python -m ui_locate.server --port 7788
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from ui_locate import config, cuda_dlls

cuda_dlls.register()

from ui_locate import act as act_mod  # noqa: E402
from ui_locate import actions  # noqa: E402
from ui_locate import frames as frames_mod  # noqa: E402
from ui_locate import screen  # noqa: E402
from ui_locate.detect import Detector, default_model  # noqa: E402
from ui_locate.locate import grab_screen  # noqa: E402
from ui_locate.mosaic import build_mosaic, map_back  # noqa: E402
from ui_locate.ocr_match import attach_text, find  # noqa: E402
from ui_locate.tiled import detect_tiled  # noqa: E402


class Engine:
    """检测 + OCR 的常驻封装。所有 GPU 调用串行化（一把锁）。"""

    def __init__(self, model: Path | None, provider: str, use_ocr: bool) -> None:
        self.lock = threading.Lock()
        self.provider = provider
        self.use_ocr = use_ocr
        self.ready = False
        self.warmup_ms = 0.0

        # 无损帧记忆。和检测器一样是常驻状态，但**不放同一把锁里** ——
        # 存一帧只是内存操作（微秒级），不该排在几十毫秒的推理后面。
        self.frames = frames_mod.FrameStore()

        t0 = time.perf_counter()
        self.det = Detector(model, provider)
        self.ocr = None
        if use_ocr:
            from ui_locate.locate import Ocr

            # OCR 固定走 CPU：PP-OCR 模型小，在 CUDA 上每帧搬运开销
            # 远大于算力收益（实测 2135ms vs 442ms，慢 5 倍）。
            self.ocr = Ocr(cuda=False)

        # 预热：这一步会吃掉首次 Conv 的算法搜索成本
        blank = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.det.detect(blank, 0.3, 0.5)
        if self.ocr is not None:
            self.ocr.lines(blank)

        self.warmup_ms = (time.perf_counter() - t0) * 1000
        self.ready = True

    def run(
        self,
        bgr: np.ndarray,
        conf: float = 0.3,
        iou: float = 0.5,
        tiled: bool = False,
        max_side: int | None = None,
        overlap: float = 0.15,
        text: bool = True,
        text_mode: str = "mosaic",
        smart: bool = True,
        origin: tuple[int, int] = (0, 0),
    ) -> dict:
        """text_mode: mosaic = 只 OCR 检测框裁片（默认，快、省）；full = 整页 OCR（召回高、慢）

        origin：这张图左上角的屏幕绝对坐标。检测器给的是图内坐标，加 origin
        之后才是能交给 cua_driver 点击的坐标 —— 副屏原点为负，这一步不能省。
        """
        if max_side is None:
            max_side = config.max_side()

        with self.lock:
            t0 = time.perf_counter()
            if tiled:
                items, tstats = detect_tiled(
                    self.det.sess, self.det.iname, self.det.onames, bgr,
                    conf, iou, max_side=max_side, overlap=overlap, smart=smart,
                )
            else:
                items, _ = self.det.detect(bgr, conf, iou)
                tstats = None
            det_ms = (time.perf_counter() - t0) * 1000

            ocr_ms = 0.0
            if text and self.ocr is not None:
                t1 = time.perf_counter()
                if text_mode == "mosaic":
                    sheet, slots, scale = build_mosaic(bgr, items, 1.2)
                    lines = map_back(self.ocr.lines(sheet), slots, scale)
                else:
                    lines = self.ocr.lines(bgr)
                attach_text(items, lines)
                ocr_ms = (time.perf_counter() - t1) * 1000

        # 图内坐标 → 屏幕绝对坐标。所有下游（find / locate / 调用方）拿到的
        # 都是绝对坐标，不必再自己加偏移 —— 少一层换算就少一次出错机会。
        ox, oy = origin
        if ox or oy:
            for it in items:
                for k in ("x1", "x2"):
                    it[k] = round(it[k] + ox, 1)
                for k in ("y1", "y2"):
                    it[k] = round(it[k] + oy, 1)
                it["cx"] = round(it["cx"] + ox, 1)
                it["cy"] = round(it["cy"] + oy, 1)

        labeled = [it for it in items if it.get("text")]
        unlabeled = [it for it in items if not it.get("text")]
        ordered = labeled + unlabeled
        out = {
            "size": [bgr.shape[1], bgr.shape[0]],
            "origin": [ox, oy],
            "provider": self.det.active,
            "tiled": tiled,
            "text_mode": text_mode,
            "detect_ms": round(det_ms, 2),
            "ocr_ms": round(ocr_ms, 2),
            "total_ms": round(det_ms + ocr_ms, 2),
            "elements": ordered,
        }
        if tstats is not None:
            out["tiles"] = tstats
        return out


ENGINE: Engine | None = None


def _as_bool(v: object, default: bool) -> bool:
    """把 JSON / query 里各种写法判成布尔。

    必须单独写：`bool("0")` 是 True，query 传 `tiled=0` 会被当成开启。
    """
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def detect_monitors(
    mon_spec: object,
    conf: float,
    iou: float,
    tiled: bool,
    max_side: int,
    overlap: float,
    text: bool,
    text_mode: str,
    smart: bool,
) -> tuple[list[dict], dict]:
    """按**真实显示器几何**逐个检测，不是在整个虚拟桌面的外接矩形上铺网格。

    为什么这个区别重要：虚拟桌面的外接矩形往往远大于各屏实际像素之和。
    开发机上实测过一个双屏布局 —— 外接矩形 1184 万像素，两块屏合计只有
    726 万像素，**38.7% 是显示器之间根本不存在的空白**。在整块矩形上均匀
    切块，这些空白照样要被切成块送进模型，纯属白烧算力。

    按屏分别切，每块屏都从 (0,0) 开始规划，网格不会跨屏错位，也不会碰到空白；
    各屏结果各自加上自己的 origin（副屏是负数）就得到统一的屏幕绝对坐标。

    mon_spec 为 None / "all" 时遍历全部屏；否则只处理指定的那一块。
    """
    assert ENGINE is not None

    target = screen.resolve_monitor(mon_spec)
    mons = [target] if target is not None else screen.list_monitors()

    all_items: list[dict] = []
    per: list[dict] = []
    detect_ms = ocr_ms = 0.0

    for m in mons:
        bgr, origin = screen.grab(m.index)
        res = ENGINE.run(
            bgr, conf=conf, iou=iou, tiled=tiled, max_side=max_side,
            overlap=overlap, text=text, text_mode=text_mode, smart=smart,
            origin=origin,
        )
        all_items.extend(res["elements"])
        detect_ms += res["detect_ms"]
        ocr_ms += res["ocr_ms"]
        entry = {
            "index": m.index,
            "device": m.device,
            "size": res["size"],
            "origin": res["origin"],
            "elements": len(res["elements"]),
            "detect_ms": res["detect_ms"],
        }
        if "tiles" in res:
            entry["tiles"] = res["tiles"]
        per.append(entry)

    # 全量元素按置信度重排，带文字的优先（下游 find 只看顺序，不关心屏）
    labeled = [it for it in all_items if it.get("text")]
    unlabeled = [it for it in all_items if not it.get("text")]
    ordered = labeled + unlabeled

    stats = {
        "monitors": per,
        "detect_ms": round(detect_ms, 2),
        "ocr_ms": round(ocr_ms, 2),
        "total_ms": round(detect_ms + ocr_ms, 2),
    }
    return ordered, stats


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # 静默访问日志
        pass

    def handle_one_request(self) -> None:
        """每条请求开始时清掉上一条读到的 body。

        `BaseHTTPRequestHandler` 在 keep-alive 连接上**复用同一个实例**跑
        `handle_one_request()` 的循环，所以挂在 `self` 上的 `_body_cache`
        会跨请求活着。不清就会拿到上一条请求的体：实测形态是同一条连接上
        `POST /nope {"a":1}` 之后再 `POST /act {"action":"wait"}`，后者报
        「需要 text（按文字找）或 x 与 y（直接给坐标）」—— 它读到的是
        `{"a": 1}`。

        这类 bug 很难从表象推回去：连接没坏、状态码也正常（400 而非 501），
        只有内容对不上。放在这里重置，是因为这是**唯一**保证每条请求都经过
        且在所有 `do_*` 之前的入口。
        """
        self._body_done = False
        self._body_cache = b""
        super().handle_one_request()

    def _send(self, code: int, obj: dict) -> None:
        # 回包前一律把请求体读干净。放在出口上而不是各个分支里，是因为
        # 「记得每个分支都要读」这种事迟早会漏 —— 而漏一次就让连接坏掉。
        # 已经读过的话 _body() 直接返回缓存，不会二次读流，代价为零。
        # 细节与实测形态见 _drain()。
        self._drain()
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_click(self, data: dict) -> dict:
        """按文字或坐标返回目标位置——只负责「找到」，不碰输入。

        与 `/act` 的分工：`/act` 会真的按下去，`/locate` 只回坐标。留着这条
        只查不动的路，是因为它的成本低得多（不做 full 回退、接受 contains
        档），适合「人看着屏幕，只想让工具告诉我在哪」的场合。

        至于为什么 `/act` 不直接复用这里的宽松匹配：实测过一次掉到 contains
        档、点中任务栏上另一个窗口，屏幕确实变了、`verified: true` —— 报告
        成功但点错了地方。会动手的路径宁可报「没找到」，所以 `_probe_hit`
        单独收紧到 starts 以上，并在 mosaic 空手时回退到 full。
        """
        assert ENGINE is not None
        conf = float(data.get("conf", 0.3))
        text_mode = str(data.get("text_mode", "mosaic"))
        smart = _as_bool(data.get("smart"), True)

        text = data.get("text")
        if text:
            elements, stats = detect_monitors(
                data.get("monitor"), conf, 0.5, True, config.max_side(), 0.15,
                True, text_mode, smart,
            )
            # min_tier 由调用方给：`/locate` 只回坐标、由人看着点，所以宽松
            # 一些也无妨；但 `/act` 会真的点下去，必须收紧到 starts 以上，
            # 否则会像实测那次——掉到 contains 档点中任务栏上另一个窗口，
            # 还得回一个 verified:true。详见 ocr_match.find 的 docstring。
            hits = find(elements, str(text), min_tier=str(data.get("min_tier") or "contains"))
            if not hits:
                return {
                    "ok": False,
                    "error": f"没找到 {text!r}",
                    "elements": len(elements),
                    "monitors": stats["monitors"],
                }
            target = hits[0]
            return {
                "ok": True,
                "matched": target.get("text"),
                "match_tier": target.get("match_tier"),
                "score": round(float(target.get("conf", 0)), 3),
                "x": int(target["cx"]),
                "y": int(target["cy"]),
                "w": int(target["w"]),
                "h": int(target["h"]),
                "elements": len(elements),
                "total_ms": stats["total_ms"],
                "monitors": stats["monitors"],
            }

        if "x" in data and "y" in data:
            return {"ok": True, "x": int(data["x"]), "y": int(data["y"])}

        return {"ok": False, "error": "需要 text，或 x 与 y"}

    # ── act 层的四个注入点 ──────────────────────────────────────────────────
    #
    # act.py 是全纯的编排逻辑（定位 → 动作 → 验证），外部世界一律靠参数注入。
    # 这里就是把「参数」接成「真东西」的地方。分成四个小方法而不是一个大函数，
    # 是因为它们的失败语义完全不同：probe 找不到只是没命中，perform 抛异常
    # 说明输入通道不可用，compare 拿不到图说明抓屏出了问题。

    def _probe_hit(self, text: str, spec: dict) -> dict | None:
        """按文字找元素，返回 act 层认的那几个字段（找不到就 None）。

        注意返回的是**归一化**过的字典，不是 find() 的原始元素：act 层只关心
        「中心点 + 尺寸 + 置信度 + 实际匹配到的文字 + 靠哪一档命中」，多给它
        字段只会让两边的耦合变紧。

        **动手前比只查时更严**，两处收紧：

          1. 不接受 contains 档。实测过一次查「插件」，那一帧 OCR 恰好没认出
             侧栏按钮，于是掉到 contains 档命中任务栏上「视觉定位MCP插件开发」
             ——点下去、窗口获得焦点、屏幕确实变了、`verified: true`。工具报告
             成功，实际点错了地方。会动手的路径宁可报「没找到」。
          2. mosaic 没找到就自动退回 full 再找一遍。mosaic 只对**检测框内**的
             裁片做 OCR，检测器一旦漏框，那段文字就根本没进 OCR —— 这正是上面
             那次漏掉按钮的原因。full 是整页扫描，召回更全但慢三倍；只在
             「马上要动手、而省的那点时间会换来点错」时才值得付这个代价。
             只查不点的 `/locate` 不做这个回退，快更重要。
        """
        def scan(text_mode: str) -> dict | None:
            elements, _stats = detect_monitors(
                spec.get("monitor"),
                float(spec.get("conf", 0.25)),
                0.5,
                _as_bool(spec.get("tiled"), True),
                int(spec.get("max_side") or config.max_side()),
                0.15,
                True,
                text_mode,
                _as_bool(spec.get("smart"), True),
            )
            hits = find(elements, text, min_tier="starts")
            if not hits:
                return None
            top = hits[0]
            return {
                "matched": top.get("text"),
                "match_tier": top.get("match_tier"),
                "cx": top["cx"],
                "cy": top["cy"],
                "w": top.get("w") or 0,
                "h": top.get("h") or 0,
                "conf": float(top.get("conf") or 0.0),
            }

        wanted = str(spec.get("text_mode") or "mosaic")
        hit = scan(wanted)
        if hit is None and wanted != "full":
            hit = scan("full")
        return hit

    def _capture(self, monitor: object) -> tuple[np.ndarray, tuple[int, int]]:
        """抓一张图，连同它的屏幕原点一起返回。

        origin 必须跟着图走：验证阶段要在图里找「点击点周围那一小片」，
        而点击点是屏幕绝对坐标、图内坐标是相对坐标，副屏原点为负时差得
        很远。把它和图绑成一个不透明值传给 compare，act 层不必知道这件事。

        **顺便存进帧记忆**。这一步是有意的副作用：抓屏本来就是 act 流程里
        最贵的一环（2880x1800 一次 20~40ms），既然图已经在手上，扔掉就等于
        下次要用时再抓一遍。存一帧是纯内存操作，微秒级，不会拖慢它。
        """
        bgr, origin = screen.grab(monitor)
        turn = getattr(self, "_turn", None)
        if turn is not None:
            # 回合内第 0 帧是「动作之前」、第 1 帧是「动作之后」—— 这是 act
            # 流程的固定形状，标上省得调用方自己数。再往后只是「又抓了一张」
            # （steps 里每步都可能抓），不再硬套 before/after 的名字。
            seq = getattr(self, "_frame_seq", 0)
            self._frame_seq = seq + 1
            label = "before" if seq == 0 else "after" if seq == 1 else f"frame{seq}"
            ENGINE.frames.put(turn, bgr, origin, label)
        return bgr, origin

    def _perform(self, action_name: str, params: dict) -> dict:
        """把动作名分派到输入层（`ui_locate.actions`）。"""
        if action_name in ("click", "double_click", "right_click", "middle_click"):
            return actions.click(
                int(params["x"]), int(params["y"]),
                button=str(params.get("button") or "left"),
                count=int(params.get("count") or 1),
                delivery=str(params.get("delivery") or "auto"),
                delay_ms=int(params.get("delay_ms") or 40),
            )
        if action_name in ("hover", "move"):
            return actions.move(int(params["x"]), int(params["y"]))
        if action_name == "drag":
            return actions.drag(
                int(params["x1"]), int(params["y1"]),
                int(params["x2"]), int(params["y2"]),
                button=str(params.get("button") or "left"),
                steps=int(params.get("steps") or 12),
                duration_ms=int(params.get("duration_ms") or 300),
            )
        if action_name == "scroll":
            return actions.scroll(
                int(params["x"]), int(params["y"]),
                delta=int(params.get("delta") or -3),
                horizontal=bool(params.get("horizontal")),
            )
        if action_name == "type":
            return actions.type_text(str(params.get("text") or ""),
                                     delay_ms=int(params.get("delay_ms") or 12))
        if action_name == "press":
            return actions.press(str(params.get("key") or ""))
        if action_name == "hotkey":
            return actions.hotkey(str(params.get("keys") or ""))
        raise actions.InputError(f"服务端不认识的动作 {action_name!r}")

    def _compare(self, before: object, after: object, x: int, y: int,
                 radius: int) -> float | None:
        """比对点击点周围 `radius` 像素的方块，返回平均绝对差（0~255）。

        为什么只看一小片：整屏里有太多与本次动作无关的变化 —— 时钟跳秒、
        光标闪烁、背景粒子。实测游戏里什么都不做连抓两张，整屏平均差就有
        2.8；敲一次 ESC 才 4.3。框住点击点附近，信噪比才够。
        """
        try:
            img_a, org_a = before  # type: ignore[misc]
            img_b, org_b = after   # type: ignore[misc]
        except (TypeError, ValueError):
            return None
        if img_a.shape != img_b.shape or org_a != org_b:
            return None

        ox, oy = org_a
        h, w = img_a.shape[:2]
        x0 = max(0, min(w, x - ox - radius))
        x1 = max(0, min(w, x - ox + radius))
        y0 = max(0, min(h, y - oy - radius))
        y1 = max(0, min(h, y - oy + radius))
        if x1 <= x0 or y1 <= y0:
            return None

        a = img_a[y0:y1, x0:x1].astype(np.float32)
        b = img_b[y0:y1, x0:x1].astype(np.float32)
        return float(np.abs(a - b).mean())

    def _act(self, data: dict) -> dict:
        """跑一次 act / steps。两个端点共用，差别只在 spec 的形状。

        开一个回合：本次调用里抓到的每一帧都记到同一个 turn 下，结果里带回
        `turn`。这样调用方之后想「回看刚才那一下到底发生了什么」，只要拿
        turn 号去 `/frame` 取，不必自己保管截图 —— 它手里本来也没有。
        """
        common = {
            "probe": self._probe_hit,
            "capture": self._capture,
            "perform": self._perform,
            "compare": self._compare,
        }
        turn = ENGINE.frames.begin_turn()
        self._turn = turn
        self._frame_seq = 0
        try:
            if "steps" in data:
                out = act_mod.run_steps(data["steps"], **common)
            else:
                out = act_mod.act(data, **common)
        finally:
            self._turn = None
            self._frame_seq = 0

        # 每个回合至少留一张牌：没有 verify:"screen" 的动作（纯 move、滚动、
        # 纯按键）本来一帧都不会抓，于是「刚才那一下之后屏幕长什么样」事后
        # 完全问不到 —— 而这正是 VISTA 那条经验里最值钱的部分：存帧，不存结论。
        # 只在**这一回合确实一帧都没有**时才补抓一次，所以带验证的动作不会
        # 多付一次抓屏开销。
        if not ENGINE.frames.turns_of(turn):
            try:
                ENGINE.frames.put(
                    turn, *self._capture(data.get("monitor")), label="after")
            except Exception:  # noqa: BLE001 - 补抓失败不该把已完成的结果变成错误
                pass

        out["turn"] = turn
        return out

    def _body(self) -> bytes:
        """读请求体。**必须读干净**，理由见 `_drain` 的注释。

        支持 `Transfer-Encoding: chunked`：只看 `Content-Length` 的话，分块
        编码的请求体会被当成「没有 body」，于是 `json.loads(b"")` 抛
        JSONDecodeError、同时那些 chunk 字节全留在 socket 里 —— 与 404 分支
        不读 body 是同一种污染，只是入口不同。

        读到的东西缓存住：`_send` 会再看一眼有没有读完，而 `rfile` 是流，
        第二次 `read(n)` 会一直等到下一条请求的数据才返回。
        """
        if getattr(self, "_body_done", False):
            return getattr(self, "_body_cache", b"")
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            data = self._read_chunked()
        else:
            n = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(n) if n > 0 else b""
        self._body_done = True
        self._body_cache = data
        return data

    def _read_chunked(self) -> bytes:
        """按 chunked 编码逐块读到 0 长度块为止。"""
        out: list[bytes] = []
        while True:
            line = self.rfile.readline(65537)
            if not line:
                break
            size_text = line.split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError:
                break
            if size == 0:
                # 吃掉 trailer（如果有）直到空行
                while True:
                    tail = self.rfile.readline(65537)
                    if tail in (b"\r\n", b"\n", b""):
                        break
                break
            out.append(self.rfile.read(size))
            self.rfile.read(2)  # 每块后面跟一个 CRLF
        return b"".join(out)

    def _drain(self) -> None:
        """把请求体读掉再回包。

        为什么非做不可：本类 `protocol_version = "HTTP/1.1"`，响应带
        `Content-Length`，于是连接会被 keep-alive 复用。只要有一条请求的
        body 没读完，那些字节就留在 socket 里，**下一条请求的请求行会从它们
        中间开始**。

        实测形态非常迷惑：同一条连接上先 `POST /nope`（带 body `{}`）拿到
        404，再 `POST /act` 就会收到
        `501 Unsupported method ('{}POST')` —— 方法名里混着上一条的 body。
        httpx 默认复用连接，所以这不是边角情况，任何客户端都会踩到。

        代价是 404 分支会多读几个字节；换来的是连接可以安全复用。
        """
        try:
            self._body()
        except Exception:  # noqa: BLE001 - 读不干净也不能把回包挡住
            self.close_connection = True

    def _image_from(self, raw: bytes) -> np.ndarray:
        """从 JSON {"path"} 或原始图片字节取图。"""
        if raw[:1] in (b"\x89", b"\xff", b"G") or raw[:2] == b"\xff\xd8":
            arr = np.frombuffer(raw, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("图片字节解码失败")
            return img

        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"请求体既不是图片也不是合法 JSON: {e}") from e

        path = data.get("path")
        if path:
            img = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError(f"无法读取: {path}")
            return img

        from ui_locate.locate import grab_screen

        return grab_screen()

    def do_GET(self) -> None:
        assert ENGINE is not None
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/health":
            self._send(200, {
                "ok": True,
                "ready": ENGINE.ready,
                "provider": ENGINE.provider,
                "active": ENGINE.det.active,
                "ocr": ENGINE.use_ocr,
                "warmup_ms": round(ENGINE.warmup_ms, 1),
                "screen": screen.describe(),
            })
            return

        if u.path == "/monitors":
            self._send(200, screen.describe())
            return

        if u.path == "/frames":
            # 只报「有哪些帧可取」，不报像素。模型得先知道有什么才能决定
            # 取哪一帧，而这个摘要本身不该变成新的上下文负担。
            self._send(200, {
                "turns": ENGINE.frames.turns(limit=int((q.get("limit") or ["20"])[0])),
                "stats": ENGINE.frames.stats(),
            })
            return

        if u.path == "/frame":
            code, meta, png = self._frame_bytes(q)
            if png is None:
                self._send(code, meta)
                return
            # 图片响应不能走 _send（那个只发 JSON）。但请求体还是要读干净，
            # 否则 keep-alive 上会污染下一条请求。
            self._drain()
            self.send_response(code)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            # 元信息塞进响应头：客户端拿得到图，也就该拿得到「这块对应屏幕
            # 哪一段、放大了几倍」，否则它没法把图里的位置换算回屏幕坐标。
            self.send_header("X-Frame-Turn", str(meta.get("turn", "")))
            self.send_header("X-Frame-Index", str(meta.get("frame", "")))
            self.send_header("X-Frame-Box", ",".join(str(v) for v in meta.get("box", [])))
            self.send_header("X-Frame-Scale", str(meta.get("scale", "")))
            self.end_headers()
            self.wfile.write(png)
            return

        if u.path == "/find":
            text = (q.get("text") or [""])[0]
            if not text:
                self._send(400, {"error": "缺少 text 参数"})
                return
            try:
                mon = (q.get("monitor") or [None])[0]
                elements, stats = detect_monitors(
                    mon,
                    float((q.get("conf") or ["0.3"])[0]),
                    0.5,
                    True,
                    int((q.get("max_side") or [str(config.max_side())])[0]),
                    0.15,
                    True,
                    str((q.get("text_mode") or ["mosaic"])[0]),
                    _as_bool((q.get("smart") or ["1"])[0], True),
                )
                hits = find(elements, text)
                self._send(200, {
                    "query": text,
                    "hits": hits,
                    "monitors": stats["monitors"],
                    "total_ms": stats["total_ms"],
                    "elements": len(elements),
                })
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return

        self._send(404, {"error": "unknown path"})

    def _frame_bytes(self, q: dict) -> tuple[int, dict, bytes | None]:
        """按 query 取一帧或它的一块，返回 (状态码, 元信息, PNG 字节)。

        单独抽出来是因为它有两套输出：正常情况回图片，出错时回 JSON。
        `_send` 只会发 JSON，所以这里自己写响应。

        参数检查放在查帧之前：`box=1,2,3` 这种写错的请求，不管那一帧存不存在
        都是「请求本身有错」，该回 400 而不是 404。反过来先查帧，调用方就得先
        猜对 turn 才能看到自己的参数写错了。
        """
        try:
            turn = int((q.get("turn") or [str(ENGINE.frames.latest_turn())])[0])
        except ValueError:
            return 400, {"error": "turn 必须是整数"}, None

        try:
            frame_index = int((q.get("frame") or ["-1"])[0])
        except ValueError:
            return 400, {"error": "frame 必须是整数"}, None

        box = None
        raw_box = (q.get("box") or [""])[0]
        if raw_box:
            parts = [p for p in raw_box.replace(" ", "").split(",") if p]
            if len(parts) != 4:
                return 400, {"error": "box 需要四个数：x1,y1,x2,y2（屏幕绝对坐标）"}, None
            try:
                box = tuple(int(float(p)) for p in parts)  # type: ignore[assignment]
            except ValueError:
                return 400, {"error": "box 的四个值必须是数字"}, None

        raw_scale = (q.get("scale") or [""])[0]
        scale = None
        if raw_scale:
            try:
                scale = float(raw_scale)
            except ValueError:
                return 400, {"error": "scale 必须是数字"}, None

        frame = ENGINE.frames.get(turn, frame_index)
        if frame is None:
            return 404, {
                "error": "没有这一帧",
                "turn": turn,
                "frame": frame_index,
                "available": [t["turn"] for t in ENGINE.frames.turns(limit=50)],
                "stats": ENGINE.frames.stats(),
            }, None

        piece, meta = ENGINE.frames.crop(frame, box=box, scale=scale)
        if piece is None:
            return 400, meta, None

        ok, buf = cv2.imencode(".png", piece)
        if not ok:
            return 500, {"error": "PNG 编码失败"}, None
        return 200, meta, buf.tobytes()


    def do_POST(self) -> None:
        assert ENGINE is not None
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/detect":
            try:
                raw = self._body()
                # 参数既可以从 URL query 给，也可以放进 JSON body —— MCP 客户端习惯
                # 后者，而 URL 里塞长参数很别扭。body 优先，query 兜底。
                try:
                    body = json.loads(raw or b"{}") if raw[:1] in (b"{", b"\n", b" ") else {}
                except json.JSONDecodeError:
                    body = {}
                if not isinstance(body, dict):
                    body = {}

                def pick(key: str, default):
                    if key in body:
                        return body[key]
                    return (q.get(key) or [default])[0]

                conf = float(pick("conf", "0.3"))
                iou = float(pick("iou", "0.5"))
                tiled = _as_bool(pick("tiled", "1"), True)
                max_side = int(pick("max_side", config.max_side()))
                overlap = float(pick("overlap", "0.15"))
                want_text = _as_bool(pick("text", "1"), True)
                text_mode = str(pick("text_mode", "mosaic"))
                smart = _as_bool(pick("smart", "1"), True)

                # 给了 path 就读文件：图内坐标即文件坐标，origin 为 0，
                # 也不涉及「哪块屏」——直接走单图检测。
                if body.get("path") or (q.get("path") or [None])[0]:
                    bgr = self._image_from(raw)
                    res = ENGINE.run(
                        bgr, conf=conf, iou=iou, tiled=tiled, max_side=max_side,
                        overlap=overlap, text=want_text, text_mode=text_mode,
                        smart=smart, origin=(0, 0),
                    )
                    res["monitor"] = None
                    self._send(200, res)
                    return

                mon = body.get("monitor") if "monitor" in body else (q.get("monitor") or [None])[0]
                elements, stats = detect_monitors(
                    mon, conf, iou, tiled, max_side, overlap,
                    want_text, text_mode, smart,
                )
                self._send(200, {
                    "size": stats["monitors"][0]["size"] if len(stats["monitors"]) == 1
                    else None,
                    "origin": stats["monitors"][0]["origin"] if len(stats["monitors"]) == 1
                    else None,
                    "provider": ENGINE.det.active,
                    "tiled": tiled,
                    "text_mode": text_mode,
                    "monitors": stats["monitors"],
                    "detect_ms": stats["detect_ms"],
                    "ocr_ms": stats["ocr_ms"],
                    "total_ms": stats["total_ms"],
                    "elements": elements,
                })
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return

        if u.path == "/locate":
            # 只给坐标，不点。
            try:
                data = json.loads(self._body() or b"{}")
                self._send(200, self._handle_click(data))
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return

        if u.path in ("/act", "/steps"):
            # 定位 + 动作 + 验证一次走完。act.py 会重试、等待、比对，
            # 但**不会**替调用方判断成败 —— 结果里的 verified 才是判据。
            try:
                data = json.loads(self._body() or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("请求体必须是 JSON 对象")
            except Exception as e:
                self._send(400, {"error": f"{type(e).__name__}: {e}"})
                return
            try:
                self._send(200, self._act(data))
            except act_mod.ActError as e:
                # 指令本身写错了，重试没有意义 → 400 而不是 500
                self._send(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return

        self._send(404, {"error": "unknown path"})


def main() -> None:
    global ENGINE

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=config.host(), help="监听地址，缺省读 UI_LOCATE_HOST")
    ap.add_argument("--port", type=int, default=config.port(), help="监听端口，缺省读 UI_LOCATE_PORT")
    ap.add_argument("--model", type=Path, default=None, help="模型文件；缺省按 ui_locate.config 规则查找")
    ap.add_argument(
        "--provider",
        choices=["auto", "cuda", "dml", "cpu"],
        default=config.provider(),
        help="auto 按 cuda → dml → cpu 的顺序取第一个可用的 provider",
    )
    ap.add_argument("--no-ocr", action="store_true")
    args = ap.parse_args()

    print(f"[ui-locate] 加载模型并预热（CUDA 首次约 35 秒；DirectML 约 2 秒）…", flush=True)
    ENGINE = Engine(args.model, config.resolve_provider(args.provider), not args.no_ocr)
    print(f"[ui-locate] 就绪，预热耗时 {ENGINE.warmup_ms / 1000:.1f}s，"
          f"provider={ENGINE.det.active}", flush=True)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"[ui-locate] 监听 http://{args.host}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[ui-locate] 停止", flush=True)


if __name__ == "__main__":
    main()
