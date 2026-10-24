"""OmniParser icon_detect 模型 INT8 动态量化。

注意：模型目录可能被安全软件保护（只读），所以量化产物先落到工程
.cache/，再由外部复制进模型目录。

ORT 自带的 shape-infer 在非系统盘路径下会失败（找不到 -inferred.onnx 中间文件），
这里先手工 onnx.shape_inference 落盘，再对推断后的模型量化。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import onnx
from onnxruntime.quantization import QuantType, quantize_dynamic

from ui_locate import config  # noqa: E402

MODEL_DIR = config.model_path().parent
SRC = MODEL_DIR / "omniparser_icon_detect_yolo11m.onnx"
OUT_DIR = Path(__file__).resolve().parents[1] / ".cache" / "models"
DST = OUT_DIR / "omniparser_icon_detect_yolo11m_int8.onnx"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=SRC)
    ap.add_argument("--dst", type=Path, default=DST)
    args = ap.parse_args()

    args.dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"source      : {args.src} ({args.src.stat().st_size / 1e6:.1f} MB)")

    with tempfile.TemporaryDirectory() as tmp:
        inferred_path = Path(tmp) / "inferred.onnx"
        model = onnx.load(str(args.src))
        onnx.save(onnx.shape_inference.infer_shapes(model), str(inferred_path))
        print(f"shape-infer : ok ({inferred_path.stat().st_size / 1e6:.1f} MB)")

        quantize_dynamic(
            model_input=str(inferred_path),
            model_output=str(args.dst),
            weight_type=QuantType.QInt8,
            per_channel=True,
            reduce_range=False,
        )

    print(f"quantized   : {args.dst} ({args.dst.stat().st_size / 1e6:.1f} MB)")
    print(f"compression : {args.src.stat().st_size / args.dst.stat().st_size:.2f}x")


if __name__ == "__main__":
    main()
