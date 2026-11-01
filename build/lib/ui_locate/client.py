"""ui-locate 的 Python 客户端。让调用方三行代码拿到坐标。

    from ui_locate.client import Locator
    loc = Locator()
    el = loc.find_one("保存")
    print(el["cx"], el["cy"])

也可以直接用命令行：
    uv run python -m ui_locate.client --find 保存 --find 取消

服务地址取 ``UI_LOCATE_BASE``，缺省 ``http://127.0.0.1:7788``。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from ui_locate import config
from ui_locate.ocr_match import find


class Locator:
    """指向常驻服务的客户端。服务没起时给清晰提示，不静默失败。"""

    def __init__(self, base: str | None = None, timeout: float = 30.0) -> None:
        self.base = (base or config.base_url()).rstrip("/")
        self.timeout = timeout

    def _req(self, method: str, path: str, body: bytes | None = None) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"连不上 ui-locate 服务（{self.base}）。"
                f"先启动：uv run python -m ui_locate.server --port 7788\n"
                f"底层错误：{e}"
            ) from e

    def health(self) -> dict:
        return self._req("GET", "/health")

    def detect(
        self,
        image: str | Path | bytes | None = None,
        conf: float = 0.3,
        iou: float = 0.5,
        tiled: bool = True,
        max_side: int | None = None,
        text: bool = True,
        text_mode: str = "mosaic",
    ) -> dict:
        """检测一张图。image 可为路径、图片字节，或 None（抓当前屏幕）。

        text_mode: "mosaic" 只认检测框里的裁片（默认，快 2~3 倍）；"full" 整页 OCR
        （实测快 2.8 倍，但检测器漏掉的文字就丢了，召回略低）。
        """
        if max_side is None:
            max_side = config.max_side()
        q = (
            f"/detect?conf={conf}&iou={iou}"
            f"&tiled={1 if tiled else 0}&max_side={max_side}"
            f"&text={1 if text else 0}&text_mode={text_mode}"
        )
        if isinstance(image, (str, Path)):
            body = json.dumps({"path": str(image)}).encode("utf-8")
        elif isinstance(image, bytes):
            body = image
        else:
            body = b"{}"
        return self._req("POST", q, body)

    def find(self, text: str, tiled: bool = True, conf: float = 0.3) -> list[dict]:
        """按文字找元素，抓当前屏幕检测。"""
        return self.query(text, None, conf)

    def query(
        self,
        text: str,
        image: str | Path | None = None,
        conf: float = 0.3,
        text_mode: str = "mosaic",
    ) -> list[dict]:
        """在指定图上按文字找元素。不给图则抓屏。

        匹配与排序复用 ``ui_locate.ocr_match.find``，与服务端、MCP 层
        保持同一套规则 —— 否则同一个查询在三条路径上会给出不同答案。
        """
        res = self.detect(image, conf=conf, tiled=True, text_mode=text_mode)
        return find(res["elements"], text)

    def find_one(
        self, text: str, image: str | Path | None = None, conf: float = 0.3
    ) -> dict | None:
        hits = self.query(text, image, conf)
        return hits[0] if hits else None


def _force_utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> None:
    _force_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--image", type=Path, default=None, help="不给则抓当前屏幕")
    ap.add_argument("--conf", type=float, default=0.3)
    ap.add_argument("--tiled", action="store_true", default=True)
    ap.add_argument("--no-tiled", dest="tiled", action="store_false")
    ap.add_argument("--no-text", dest="text", action="store_false")
    ap.add_argument(
        "--text-mode",
        choices=("full", "mosaic"),
        default="full",
        help="full=整页 OCR（召回高）；mosaic=只认检测框裁片（快 2~3 倍）",
    )
    ap.add_argument("--find", action="append", default=[], metavar="TEXT")
    ap.add_argument("--json-out", type=Path, default=None)
    ap.add_argument("--list", action="store_true", help="列出全部元素")
    args = ap.parse_args()

    loc = Locator(args.base)
    h = loc.health()
    print(
        f"service  : {h['active']}  ocr={h['ocr']}  "
        f"warmup={h['warmup_ms'] / 1000:.1f}s"
    )

    t0 = time.perf_counter()
    res = loc.detect(
        args.image,
        conf=args.conf,
        tiled=args.tiled,
        text=args.text,
        text_mode=args.text_mode,
    )
    wall = (time.perf_counter() - t0) * 1000

    print(f"image    : {res['size'][0]}x{res['size'][1]}")
    print(f"server   : detect={res['detect_ms']:.1f}ms  ocr={res['ocr_ms']:.1f}ms")
    print(f"wall     : {wall:.1f} ms（含 HTTP 往返）")
    print(f"elements : {len(res['elements'])}\n")

    if args.list:
        for i, it in enumerate(res["elements"]):
            tag = f"  {it['text']!r}" if it.get("text") else ""
            print(
                f"  [{i:2d}] ({it['cx']:7.1f}, {it['cy']:7.1f})  "
                f"{it['w']:5.1f}x{it['h']:5.1f}  conf={it['conf']:.2f}{tag}"
            )
        print()

    for q in args.find:
        # 复用上面那次检测结果，不再重复调服务（之前每个 --find 都重跑一遍）
        # 匹配逻辑统一走 ocr_match.find()，避免这里和服务端各有一套排序、行为不一致
        hits = find(res["elements"], q)
        if not hits:
            print(f"find {q!r} -> 没找到")
            continue
        for h2 in hits[:3]:
            print(
                f"find {q!r} -> ({h2['cx']:.1f}, {h2['cy']:.1f})  "
                f"{h2['w']:.0f}x{h2['h']:.0f}  {h2.get('text')!r}"
            )

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\njson -> {args.json_out}")


if __name__ == "__main__":
    main()
