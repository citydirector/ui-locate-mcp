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
  POST /locate   {"text": "保存"}  只返回坐标，不点击；点由 cua_driver 执行

边界：服务只负责「看得见，说得清坐标」。输入注入交给 cua_driver ——
实测本进程调 SetCursorPos / SendInput 全部静默失败（返回成功、错误码 0、
光标不动），而 cua_driver 的点击一直正常。两边各做各擅长的事。

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

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_click(self, data: dict) -> dict:
        """按文字或坐标返回目标位置——只负责「找到」，点由调用方（cua_driver）做。

        为什么服务自己不下手：实测本进程调 SetCursorPos / SendInput 全部静默失败
        （返回成功、错误码 0、光标不动），而同一台机器上 cua_driver 的输入
        完全正常。输入注入这件事已经有可靠通道，没必要在这里重造一遍。
        服务把边界划在「我给坐标，你给点击」，两边都干净。
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
            hits = find(elements, str(text))
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

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

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
            # 只给坐标，不点。点击交给 cua_driver（它有可用的输入通道）。
            try:
                data = json.loads(self._body() or b"{}")
                self._send(200, self._handle_click(data))
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
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
