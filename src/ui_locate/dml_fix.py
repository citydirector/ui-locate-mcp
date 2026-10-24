"""把模型里的 ``allowzero=1`` 改掉，好让 DirectML 能编译它。

为什么需要这一步
----------------
``omniparser_icon_detect_yolo11m.onnx`` 是 PyTorch 2.11 导出的（opset 18），
里面 8 个 ``Reshape`` 节点都带 opset 14 引入的 ``allowzero`` 属性且值为 1。
DirectML 执行提供程序不支持这个属性，整图编译失败 —— 更糟的是 ONNX Runtime
**不抛异常**，而是静默回退到 CPU，于是「看起来能跑，只是慢 17 倍」。这种失败
很难自己发现，所以这里在加载前直接把属性改掉。

为什么改字节而不是用 onnx 库重存
--------------------------------
省掉 ``onnx`` 依赖（连带 protobuf、flatbuffers 约 12 MB），也省掉重新序列化
77 MB 的开销（实测 174 ms）。改动只发生在 protobuf 的 AttributeProto 里：
``name="allowzero"`` 后面紧跟的 ``i`` 字段。protobuf 编码是
``0x0a 0x09 "allowzero"``（field 1, wire type 2）接 ``0x18 0x01``（field 3,
wire type 0）。把 ``0x01`` 换成 ``0x00``，长度不变，父级长度前缀也不用动。

安全边界（重要）
----------------
``allowzero=1`` 的含义是「shape 里的 0 是字面量 0」；``=0`` 的含义是
「shape 里的 0 表示复制输入的那一维」。**只有 shape 常量不含 0 时两者等价。**
所以这里不是无条件改，而是先精确解析出每个 ``Reshape`` / ``Expand`` 节点的
shape 张量值，确认全都不含 0 才动手；只要有一个含 0 就抛 ``UnsafeModel``，
让用户显式选择 CPU —— 宁可看到明确报错，也不悄悄改变模型语义。

这里的 protobuf 解析是手写的，只认这个场景用得到的几个字段，够用且无依赖。
"""

from __future__ import annotations

import struct

PROPERTY = b"\x0a\x09allowzero"
ON = b"\x18\x01"
OFF = b"\x18\x00"

# 需要检查 shape 的算子：它们第二个输入是目标形状
SHAPE_OPS = ("Reshape", "Expand")


class UnsafeModel(RuntimeError):
    """模型里存在让 allowzero 改写不安全的构造。"""


# ── 最小 protobuf 遍历 ──────────────────────────────────────────────────────


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")


def _fields(buf: bytes):
    """逐个产出 (field_number, wire_type, 值, 值起始偏移)。

    wire type 0 → 值是 int；1 → 8 字节跳过；2 → bytes；5 → 4 字节跳过。
    """
    pos, end = 0, len(buf)
    while pos < end:
        key, pos = _varint(buf, pos)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            val, pos = _varint(buf, pos)
            yield fn, wt, val, pos
        elif wt == 2:
            ln, pos = _varint(buf, pos)
            yield fn, wt, buf[pos : pos + ln], pos
            pos += ln
        elif wt == 1:
            yield fn, wt, buf[pos : pos + 8], pos
            pos += 8
        elif wt == 5:
            yield fn, wt, buf[pos : pos + 4], pos
            pos += 4
        else:
            raise ValueError(f"未知 wire type {wt}")


def _node(buf: bytes) -> dict:
    """解析一个 NodeProto：name / op_type / inputs / allowzero 的字节偏移。

    字段号按 onnx.proto 的 NodeProto 定义：
    1=input(repeated) / 2=output(repeated) / 3=name / 4=op_type / 5=attribute。
    """
    node = {"name": "", "op_type": "", "inputs": [], "allowzero_at": []}
    for fn, wt, val, _pos in _fields(buf):
        if fn == 1 and wt == 2:
            node["inputs"].append(val.decode("utf-8", "replace"))
        elif fn == 3 and wt == 2:
            node["name"] = val.decode("utf-8", "replace")
        elif fn == 4 and wt == 2:
            node["op_type"] = val.decode("utf-8", "replace")
        elif fn == 5 and wt == 2:
            # AttributeProto：1=name / 3=i
            aname, aint = "", None
            for afn, awt, aval, _ in _fields(val):
                if afn == 1 and awt == 2:
                    aname = aval.decode("utf-8", "replace")
                elif afn == 3 and awt == 0:
                    aint = aval
            if aname == "allowzero":
                node["allowzero_at"].append(aint)
    return node


def _tensor_int64(buf: bytes) -> list[int] | None:
    """从 TensorProto 取 int64 一维数据；不是 int64 就返回 None。"""
    dtype, raw, ints = None, None, []
    for fn, wt, val, _pos in _fields(buf):
        if fn == 2 and wt == 0:
            dtype = val
        elif fn == 9 and wt == 2:
            raw = val
        elif fn == 7 and wt == 2:
            # int64_data 是 packed repeated
            pos = 0
            while pos < len(val):
                v, pos = _varint(val, pos)
                ints.append(v - (1 << 64) if v >= (1 << 63) else v)
        elif fn == 7 and wt == 0:
            ints.append(val - (1 << 64) if val >= (1 << 63) else val)
    if dtype != 7:  # 7 = INT64
        return None
    if raw is not None:
        return list(struct.unpack(f"<{len(raw) // 8}q", raw))
    return ints


def inspect(raw: bytes) -> dict:
    """扫一遍模型，返回 allowzero 现状与所有 shape 张量的值。"""
    graph = None
    for fn, wt, val, _pos in _fields(raw):
        if fn == 7 and wt == 2:  # ModelProto.graph
            graph = val
            break
    if graph is None:
        raise UnsafeModel("这不是一个 ONNX 模型：找不到 GraphProto")

    nodes, inits = [], {}
    for fn, wt, val, _pos in _fields(graph):
        if fn == 1 and wt == 2:  # GraphProto.node
            nodes.append(_node(val))
        elif fn == 5 and wt == 2:  # GraphProto.initializer
            name, vals = None, None
            for tfn, twt, tval, _ in _fields(val):
                if tfn == 8 and twt == 2:
                    name = tval.decode("utf-8", "replace")
            vals = _tensor_int64(val)
            if name is not None:
                inits[name] = vals

    shapes: dict[str, list[tuple[str, list[int] | None]]] = {}
    for n in nodes:
        if n["op_type"] in SHAPE_OPS and len(n["inputs"]) >= 2:
            label = n["name"] or f"{n['op_type']}[{len(shapes)}]"
            shapes.setdefault(label, []).append((n["inputs"][1], inits.get(n["inputs"][1])))
    return {
        "nodes": len(nodes),
        "allowzero_on": sum(1 for n in nodes for a in n["allowzero_at"] if a == 1),
        "allowzero_off": sum(1 for n in nodes for a in n["allowzero_at"] if a != 1),
        "shapes": shapes,
    }


def patch(raw: bytes) -> tuple[bytes, int]:
    """返回 (改好的字节, 改动处数)。改写不安全时抛 ``UnsafeModel``。

    模型里本来就没有 ``allowzero=1`` 时原样返回 0 —— 换了不带该属性的模型也能用。
    """
    if PROPERTY + ON not in raw:
        return raw, 0

    info = inspect(raw)
    suspects = [
        (node, target, vals)
        for node, pairs in info["shapes"].items()
        for target, vals in pairs
        if vals and 0 in vals
    ]
    if suspects:
        detail = "; ".join(
            f"{node} 的 shape({target})={vals}" for node, target, vals in suspects[:3]
        )
        raise UnsafeModel(
            "模型里有 shape 常量包含 0，此时 allowzero=1 与 =0 语义不同，"
            "自动改写会改变模型行为，所以拒绝动手。"
            f"可疑节点：{detail}。"
            "请设 UI_LOCATE_PROVIDER=cpu，或自行改写模型后另存。"
        )

    return raw.replace(PROPERTY + ON, PROPERTY + OFF), info["allowzero_on"]


def load_patched(path) -> tuple[bytes, int]:
    """读文件并返回 (可喂给 InferenceSession 的 model_bytes, 改动处数)。"""
    raw = path.read_bytes() if hasattr(path, "read_bytes") else open(path, "rb").read()
    return patch(raw)
