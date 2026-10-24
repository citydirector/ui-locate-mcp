"""HTTP 层的回归检查：keep-alive 连接不能被请求体毒化。

为什么单独成文件、而不是并进 `test_act.py`：
  `test_act.py` 是纯逻辑测试，用桩跑、零依赖、几十毫秒结束。这里必须真的
  起一个 HTTP 服务、真的复用同一条 TCP 连接 —— 因为它测的正是「连接复用
  时状态会不会串味」这件事，桩掉连接就等于把被测对象删了。
  `server.py` 顶层 import numpy / cv2 / rapidocr，所以在没有这些包的
  解释器里连导入都过不去，也就不能靠 `unittest discover` 自动收集。
  因此本文件与 `check_bundle.py` 同类：结构自检脚本，需要真实环境。

跑法（用插件的 venv）：
  <venv>\\Scripts\\python.exe tests\\check_http.py

被测的两个缺陷都是实测踩出来的，形态记在这里，免得以后改回去：

【缺陷 1】回包前不读请求体 → 连接被毒化
  `Handler.protocol_version = "HTTP/1.1"` 且响应带 `Content-Length`，于是
  连接会被 keep-alive 复用。只要有一条请求的 body 没读完，那些字节就留在
  socket 里，**下一条请求的请求行会从它们中间开始**。
  实测：同一条连接上先 `POST /nope`（body `{}`）拿到 404，再 `POST /act`
  收到 `501 Unsupported method ('{}POST')` —— 方法名里混着上一条的 body。
  httpx 默认复用连接，所以任何客户端都会踩到，不是边角情况。

【缺陷 2】读到的 body 缓存在实例上却没按请求重置 → 内容串味
  `BaseHTTPRequestHandler` 在 keep-alive 连接上复用**同一个实例**跑
  `handle_one_request()` 的循环，挂在 `self` 上的缓存会跨请求活着。
  实测：`POST /nope {"a":1}` 之后再 `POST /act {"action":"wait"}`，后者报
  「需要 text（按文字找）或 x 与 y（直接给坐标）」—— 它读到的是 `{"a":1}`。
  这个比缺陷 1 更难从表象推回去：连接没坏、状态码也正常（400 而非 501），
  只有内容对不上。

【顺带覆盖】chunked 请求体
  只看 `Content-Length` 的话，`Transfer-Encoding: chunked` 的请求体会被
  当成「没有 body」—— 同样是「没读干净」，只是入口不同。
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "src"))

# 端口挑高位、避开默认 7788：本机很可能已经有一个常驻服务在跑，
# 撞上去会连到旧进程上，测出来的结果毫无意义。
PORT = int(os.environ.get("UI_LOCATE_TEST_PORT") or "7809")
BASE = f"http://127.0.0.1:{PORT}"

failures: list[str] = []
passes: list[str] = []


def ok(msg: str) -> None:
    passes.append(msg)
    print(f"  OK   {msg}")


def bad(msg: str) -> None:
    failures.append(msg)
    print(f"  FAIL {msg}")


def wait_ready(proc: subprocess.Popen[bytes], timeout: float = 120.0) -> bool:
    """等 /health 能通。顺便盯进程有没有提前退出。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=3)
            c.request("GET", "/health")
            r = c.getresponse()
            r.read()
            c.close()
            return True
        except OSError:
            time.sleep(0.5)
    return False


def post(conn: http.client.HTTPConnection, path: str, payload: dict) -> tuple[int, bytes]:
    raw = json.dumps(payload).encode()
    conn.request(
        "POST",
        path,
        body=raw,
        headers={"Content-Type": "application/json", "Content-Length": str(len(raw))},
    )
    r = conn.getresponse()
    return r.status, r.read()


def case_poisoned_after_404() -> None:
    """缺陷 1 的回归：404 之后再打 /act 必须正常。"""
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
    try:
        s1, d1 = post(conn, "/nope", {"a": 1})
        if s1 != 404:
            bad(f"未知路径应回 404，实际 {s1}: {d1[:120]}")
        else:
            ok("未知路径回 404")

        s2, d2 = post(conn, "/act", {"action": "wait"})
        if b"Unsupported method" in d2:
            bad(
                "同一条连接上 404 之后再 POST /act 被毒化："
                f"{d2[:160]!r}（回包前没把请求体读干净）"
            )
        elif s2 != 200:
            bad(f"404 之后的 /act 应回 200，实际 {s2}: {d2[:160]}")
        else:
            ok("404 之后同连接复用仍可正常 POST /act")
    finally:
        conn.close()


def case_repeated_unknown_then_act() -> None:
    """连打三次未知路径，再打 /act —— 残留字节会累积，更容易暴露。"""
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
    try:
        for i in range(3):
            post(conn, "/nope", {"i": i})
        s, d = post(conn, "/act", {"action": "wait"})
        if s == 200:
            ok("连续三次 404 之后 /act 仍然正常")
        else:
            bad(f"连续三次 404 之后 /act 失败（{s}）: {d[:160]}")
    finally:
        conn.close()


def case_body_not_reused_across_requests() -> None:
    """缺陷 2 的回归：上一条请求的 body 不能串到下一条。"""
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
    try:
        post(conn, "/nope", {"a": 1})
        s, d = post(conn, "/act", {"action": "wait"})
        body = d.decode("utf-8", "replace")
        if s != 200:
            bad(f"body 缓存串味：/act 回 {s}，内容 {body[:180]}")
        elif "需要 text" in body:
            bad(
                "body 缓存跨请求残留：/act 拿到了上一条请求的体 "
                f"（回的是「需要 text 或 x 与 y」而不是纯等待结果）"
            )
        else:
            ok("每条请求读到的都是自己的 body")
    finally:
        conn.close()


def case_chunked_body() -> None:
    """chunked 请求体要能读进来，不能当成「没有 body」。"""
    payload = json.dumps({"action": "wait"}).encode()
    s = socket.create_connection(("127.0.0.1", PORT), timeout=60)
    try:
        head = (
            f"POST /act HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
            "Content-Type: application/json\r\n"
            "Transfer-Encoding: chunked\r\n\r\n"
        ).encode()
        s.sendall(
            head
            + hex(len(payload))[2:].encode()
            + b"\r\n"
            + payload
            + b"\r\n0\r\n\r\n"
        )
        buf = b""
        deadline = time.monotonic() + 60
        while b"\r\n\r\n" not in buf and time.monotonic() < deadline:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        status_line = buf.split(b"\r\n", 1)[0].decode("latin-1")
        if "200" in status_line:
            ok("chunked 请求体被正确读取")
        else:
            bad(f"chunked 请求体没读进来：{status_line}")
    finally:
        s.close()


def main() -> int:
    print(f"[1] 启动服务（端口 {PORT}）…")
    # 子进程输出一律落文件：本机受限环境里 subprocess 建管道会报
    # WinError 5，用 PIPE 直接起不来。
    log_path = Path(tempfile.gettempdir()) / "ui-locate-check-http.log"
    log = open(log_path, "wb")
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(PKG / "src"))
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "ui_locate.server", "--port", str(PORT)],
        cwd=str(PKG),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    try:
        if not wait_ready(proc):
            tail = ""
            try:
                log.close()
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
            except OSError:
                pass
            print(f"FAIL 服务没能就绪（exit={proc.poll()}）")
            if tail:
                print(f"--- 服务输出 ---\n{tail}")
            return 1
        print("  OK   服务就绪\n")

        print("[2] keep-alive 连接的行为")
        case_poisoned_after_404()
        case_repeated_unknown_then_act()
        case_body_not_reused_across_requests()

        print("\n[3] 请求体编码")
        case_chunked_body()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            log.close()
        except OSError:
            pass

    print(f"\n{'=' * 60}")
    if failures:
        print(f"FAILED  {len(failures)} 项失败 / {len(passes)} 项通过")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASS  {len(passes)} 项全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
