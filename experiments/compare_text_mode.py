"""对比 mosaic 与 full 两种 OCR 模式对短控件名的召回差异。

背景：查一个短词时，mosaic 模式只命中了另一处含该词的长文本，而真正的
短按钮没被检出；full 模式能检出。要弄清是检测框的问题还是 OCR 裁片的问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ui_locate import config

BASE = config.base_url()


def probe(mode: str, monitor: str, conf: float) -> None:
    r = httpx.post(
        f"{BASE}/detect",
        json={"monitor": monitor, "conf": conf, "text_mode": mode, "smart": True},
        timeout=180.0,
        trust_env=False,
    )
    r.raise_for_status()
    res = r.json()
    els = res.get("elements", [])
    hits = [
        (e["text"], round(e["cx"]), round(e["cy"]), e["conf"])
        for e in els
        if e.get("text") and "插件" in e["text"]
    ]
    print(f"\n{mode} (monitor={monitor}, conf={conf}): {len(els)} 元素, "
          f"{res['total_ms']:.0f}ms")
    print(f"  含「插件」的命中 {len(hits)} 个:")
    for h in hits:
        print(f"    {h[0]!r:22} ({h[1]},{h[2]})  conf={h[3]}")


def main() -> None:
    for mode in ("mosaic", "full"):
        probe(mode, "1", 0.25)
    # 放宽 conf 看是否能救回
    probe("mosaic", "1", 0.15)


if __name__ == "__main__":
    main()
