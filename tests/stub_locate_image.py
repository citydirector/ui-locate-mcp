"""跨仓联调夹具：起一个**真的** HTTP 服务，但引擎是替身。

为什么需要它：`plugin/cua-bridge.mjs`（B 所有）要证明自己的客户端真的能跟这条
路由对话 —— 契约字段、错误码、空匹配、取消 —— 而不必加载 76MB 模型、不必碰
任何一块屏。真引擎的推理由 `tests/test_image_api.py` 与真实模型冒烟覆盖；
这里只负责**线上协议**这一层。

安全边界（刻意设计）：

  · 只监听 127.0.0.1，默认端口 0（系统分配），实际端口打印在 `PORT=<n>` 行。
  · 默认托管模式（`UI_LOCATE_CUA_MANAGED=1`）：`/health` 因此**不会**调
    `screen.describe()`，桌面路由一律 403 —— 夹具连"可能顺手抓屏"的代码路径
    都走不到。B 的客户端只接受 `managed:true`，所以这也是它的前提。
  · 引擎是替身：不加载模型、不读文件、不抓屏。
  · 收到 `quit` 行、SIGTERM 或被杀掉才退出。**stdin 的 EOF 不退出**：用
    `Start-Process` 一类方式拉起时 stdin 可能一开始就是关的，读到 EOF 就退出
    会让服务在测试发第一个请求之前消失。

用法：

    python tests/stub_locate_image.py --scenario ok            # 命中一条
    python tests/stub_locate_image.py --scenario empty         # 200 + matches=[]
    python tests/stub_locate_image.py --scenario error         # 500 engine_error
    python tests/stub_locate_image.py --scenario busy          # 503 busy
    python tests/stub_locate_image.py --scenario ok --delay-ms 3000   # 慢引擎，测取消
    python tests/stub_locate_image.py --scenario stall         # 等价于 --delay-ms 30000

stdout 前两行：

    PORT=51234
    {"ready": true, "base_url": "http://127.0.0.1:51234", "scenario": "ok",
     "managed": true, "engine": "stub"}

各`/compare-images` 不需要场景：它不吃引擎，直接解码请求里的两张图比较，
所以任何 `--scenario` 下都能用（`--delay-ms` 仍对整个 handler 生效，可测取消）。

场景（请求本身非法时仍按契约回 400/413/415，与场景无关）：

  ok     → 200，一条命中：text="Save"、confidence=0.95、
           box={x:40,y:30,width:80,height:30}、center={x:80,y:45}
  empty  → 200，matches=[]
  error  → 500，code="engine_error"
  busy   → 503，code="busy"
  stall  → 睡 30s 再按 ok 返回（客户端中途断开可验证取消不带走服务）

为什么"一个场景一个进程"而不是在请求体里塞开关：让夹具的行为写在启动命令里，
测试读到那一行就知道对端会怎么回答。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from http.server import ThreadingHTTPServer  # noqa: E402

from ui_locate import server as server_mod  # noqa: E402


MATCH_ELEMENT = {
    "x1": 40, "y1": 30, "x2": 120, "y2": 60,
    "cx": 80, "cy": 45, "w": 80, "h": 30,
    "conf": 0.90, "text": "Save", "text_score": 0.95,
}


class StubEngine:
    """`server.Engine` 的最小替身：只实现 `/locate-image` 用到的那部分。"""

    def __init__(self, scenario: str, delay_ms: int = 0) -> None:
        self.scenario = scenario
        self.delay_s = (30000 if scenario == "stall" else delay_ms) / 1000.0
        self.ready = True
        self.provider = "stub"
        self.use_ocr = True
        self.warmup_ms = 0.0
        self.det = SimpleNamespace(active="stub")
        self.frames = SimpleNamespace()
        self.calls = 0

    def run(self, bgr, **kwargs):  # noqa: ANN001, ANN003 - 与真 Engine 同形
        self.calls += 1
        if self.scenario == "error":
            raise RuntimeError("stub engine exploded")
        if self.scenario == "busy":
            raise TimeoutError("stub engine timed out")
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        elements = [] if self.scenario == "empty" else [dict(MATCH_ELEMENT)]
        return {
            "size": (bgr.shape[1], bgr.shape[0]),
            "origin": (0, 0),
            "provider": "stub",
            "tiled": bool(kwargs.get("tiled")),
            "text_mode": kwargs.get("text_mode"),
            "detect_ms": 0.0,
            "ocr_ms": 0.0,
            "total_ms": 0.0,
            "elements": elements,
        }


def main() -> int:
    ap = argparse.ArgumentParser(description="stub HTTP server for /locate-image")
    ap.add_argument("--scenario", "--mode", dest="scenario", default="ok",
                    choices=("ok", "empty", "error", "busy", "stall"))
    ap.add_argument("--delay-ms", type=int, default=0,
                    help="返回前先睡这么久（测取消用；stall 场景固定 30000）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0, help="默认 0：由系统分配")
    ap.add_argument("--standalone", action="store_true",
                    help="不设 UI_LOCATE_CUA_MANAGED（health 会走抓屏分支，联调不需要）")
    args = ap.parse_args()

    if args.standalone:
        os.environ.pop("UI_LOCATE_CUA_MANAGED", None)
    else:
        os.environ["UI_LOCATE_CUA_MANAGED"] = "1"

    server_mod.ENGINE = StubEngine(args.scenario, args.delay_ms)
    httpd = ThreadingHTTPServer((args.host, args.port), server_mod.Handler)
    httpd.daemon_threads = True
    host, port = httpd.server_address[:2]

    print(f"PORT={port}", flush=True)
    print(json.dumps({
        "ready": True,
        "base_url": f"http://{host}:{port}",
        "scenario": args.scenario,
        "delay_ms": args.delay_ms,
        "managed": os.environ.get("UI_LOCATE_CUA_MANAGED") == "1",
        "engine": "stub",
    }), flush=True)

    # stdin 只在收到 ``quit`` 时收工；**EOF 不退出** —— 用 Start-Process 之类的
    # 方式拉起时 stdin 可能一开始就是关的，那种情况下"读到 EOF 就退出"会让服务
    # 在测试发第一个请求之前就没了。父进程要停它，按 PID 杀即可。
    def watch_stdin() -> None:
        try:
            stdin = sys.stdin
            if stdin is None:
                return                      # 没有 stdin：一直服务到被杀
            for line in stdin:
                if line.strip() == "quit":
                    httpd.shutdown()
                    return
            # 走到这里说明 EOF —— **什么都不做**，继续服务。见上面的说明。
        except (KeyboardInterrupt, ValueError, OSError):
            return

    threading.Thread(target=watch_stdin, daemon=True).start()

    try:
        httpd.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
