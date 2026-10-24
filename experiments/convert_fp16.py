"""OmniParser icon_detect 模型 FP16 转换。

YOLO 这类 CNN 结构几乎全是 Conv，动态 INT8 量化会生成 ConvInteger(10) 节点，
而 ONNX Runtime 的 CPU / CUDA EP 都不实现它（实测直接 NOT_IMPLEMENTED）。
CNN 的正确量化路线是「带校准集的静态 QDQ」，成本高收益有限。

CUDA 路线上更划算的是 FP16：模型体积减半，Tensor Core 上通常再快 1.3~2x。

坑：onnxconverter_common 的 convert_float_to_float16 会把 Resize 的 scales
输入一起转成 FP16，而 ONNX 规范要求 scales/roi 必须是 FP32，于是模型加载即报
  Type Error: Type (tensor(float)) of output arg (upsample_nearest2d) of node
  (node_upsample_nearest2d_output_cast0) does not match expected type (tensor(float16))
这里在转换后把 Resize 的 scales/roi 输入强制还原成 FP32，并在其前插入 Cast。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper
from onnxconverter_common import float16

from ui_locate import config  # noqa: E402

MODEL_DIR = config.model_path().parent
SRC = MODEL_DIR / "omniparser_icon_detect_yolo11m.onnx"
OUT_DIR = Path(__file__).resolve().parents[1] / ".cache" / "models"
DST = OUT_DIR / "omniparser_icon_detect_yolo11m_fp16.onnx"


def fix_resize_inputs(model: onnx.ModelProto) -> tuple[onnx.ModelProto, int]:
    """把 Resize 的 roi / scales 输入（input[1], input[2]）保证为 FP32。

    返回 (修正后的模型, 修正节点数)。
    """
    inits = {i.name: i for i in model.graph.initializer}
    value_info: dict[str, int] = {}
    for vi in list(model.graph.value_info) + list(model.graph.input):
        if vi.type.HasField("tensor_type"):
            value_info[vi.name] = vi.type.tensor_type.elem_type

    nodes = list(model.graph.node)
    new_nodes: list[onnx.NodeProto] = []
    extra_inits: list[onnx.TensorProto] = []
    cast_serial = 0
    fixed = 0

    for node in nodes:
        if node.op_type not in ("Resize", "Upsample"):
            new_nodes.append(node)
            continue

        new_inputs = list(node.input)
        changed = False
        for slot in (1, 2):  # 1 = roi, 2 = scales；空字符串表示不适用
            if slot >= len(new_inputs):
                continue
            name = new_inputs[slot]
            if not name:
                continue

            if name in inits:
                t = inits[name]
                if t.data_type == TensorProto.FLOAT16:
                    arr = numpy_helper.to_array(t).astype(np.float32)
                    inits[name] = numpy_helper.from_array(arr, name)
                    changed = True
            elif value_info.get(name) == TensorProto.FLOAT16:
                cast_serial += 1
                cast_out = f"__resize_scale_fp32_{cast_serial}"
                new_nodes.append(
                    helper.make_node("Cast", [name], [cast_out], to=TensorProto.FLOAT)
                )
                new_inputs[slot] = cast_out
                changed = True

        if changed:
            fixed += 1
            node = helper.make_node(
                node.op_type,
                new_inputs,
                list(node.output),
                name=node.name,
                mode=next((a.s.decode() for a in node.attribute if a.name == "mode"), "nearest"),
            )

        new_nodes.append(node)

    del model.graph.node[:]
    model.graph.node.extend(new_nodes)
    # 重新排列 initializer（被替换的保持原位置）
    keep = [inits[i.name] for i in model.graph.initializer if i.name in inits]
    del model.graph.initializer[:]
    model.graph.initializer.extend(keep + extra_inits)
    return model, fixed


def dedupe_value_info(model: onnx.ModelProto) -> tuple[onnx.ModelProto, int]:
    """清理 value_info 里同名的重复条目，再用 shape inference 重建。

    坑：convert_float_to_float16 会**追加**新的 value_info 而不覆盖旧的，
    于是 `upsample_nearest2d` 同时以 FLOAT16(10) 和 FLOAT(1) 出现两次。
    ORT 取到第一条 FLOAT16，但实际 Cast 输出是 FLOAT，加载即报
      Type Error: Type (tensor(float)) of output arg (upsample_nearest2d) of node
      (node_upsample_nearest2d_output_cast0) does not match expected type
    最省事的修法是把 value_info 整个丢掉，交给 shape_inference 重新推断。
    """
    before = len(model.graph.value_info)
    del model.graph.value_info[:]
    inferred = onnx.shape_inference.infer_shapes(model, strict_mode=False)
    return inferred, before


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=SRC)
    ap.add_argument("--dst", type=Path, default=DST)
    args = ap.parse_args()

    args.dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"source     : {args.src.name} ({args.src.stat().st_size / 1e6:.1f} MB)")

    model = onnx.load(str(args.src))
    # keep_io_types=True：外面仍按 FP32 喂入/取出，调用方无需改代码
    fp16_model = float16.convert_float_to_float16(
        model,
        keep_io_types=True,
        disable_shape_infer=False,
    )
    fp16_model, fixed = fix_resize_inputs(fp16_model)
    fp16_model, stale_vi = dedupe_value_info(fp16_model)
    onnx.save(fp16_model, str(args.dst))

    print(f"resize fix : {fixed} node(s)")
    print(f"value_info : dropped {stale_vi} stale entr(ies), re-inferred")
    print(f"fp16       : {args.dst.name} ({args.dst.stat().st_size / 1e6:.1f} MB)")
    print(f"compression: {args.src.stat().st_size / args.dst.stat().st_size:.2f}x")


if __name__ == "__main__":
    main()
