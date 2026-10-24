"""ONNX Runtime 后端探测：确认本机可用的执行器。"""

import onnxruntime as ort

if __name__ == "__main__":
    print("onnxruntime:", ort.__version__)
    available = ort.get_available_providers()
    print("available providers:", available)
    for p in ("TensorrtExecutionProvider", "CUDAExecutionProvider", "DmlExecutionProvider", "VitisAIExecutionProvider", "CPUExecutionProvider"):
        print(f"  {p:32s} {'YES' if p in available else 'no'}")
