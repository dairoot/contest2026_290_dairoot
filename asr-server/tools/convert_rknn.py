"""把 tools/export_onnx.py 产出的定长 ONNX 转成 RK3576 的 .rknn。

只能在 x86_64 Linux 上跑（rknn-toolkit2 没有 macOS / aarch64 的包）：

    pip install rknn-toolkit2
    python tools/convert_rknn.py --model-dir rknn_models

默认走 fp16（do_quantization=False），不需要校准集，精度基本无损；RK3576 的 NPU
同时支持 int8，但 SenseVoice 这类 transformer 量化后掉字明显，要试的话用
--quantize 并准备 --dataset。

转换机上 rknn-toolkit2 的版本必须和板子上 librknnrt.so 的版本一致，这是最常见的
翻车点。板子上查：

    cat /proc/rknpu/version 2>/dev/null || cat /sys/kernel/debug/rknpu/version
    strings /usr/lib/librknnrt.so | grep -i "librknnrt version"

本脚本未经实机验证——手上没有 x86 转换机，也没有板子。
"""

import argparse
import glob
import os
import sys


def _onnx_io(path: str) -> tuple[list[str], list[list[int]]]:
    """从 ONNX 里读输入名和定长 shape，避免和导出脚本里的数字对不上。"""
    import onnx

    model = onnx.load(path, load_external_data=False)
    initializers = {t.name for t in model.graph.initializer}
    names, shapes = [], []
    for inp in model.graph.input:
        if inp.name in initializers:
            continue
        shape = [d.dim_value for d in inp.type.tensor_type.shape.dim]
        if any(d <= 0 for d in shape):
            raise SystemExit(
                f"{path} 的输入 {inp.name} shape={shape} 不是定长，RKNN 不支持动态 shape。"
                "请用 tools/export_onnx.py 重新导出。"
            )
        names.append(inp.name)
        shapes.append(shape)
    return names, shapes


def convert(onnx_path: str, target: str, quantize: bool, dataset: str | None) -> str:
    from rknn.api import RKNN

    rknn_path = os.path.splitext(onnx_path)[0] + ".rknn"
    names, shapes = _onnx_io(onnx_path)
    print(f"\n=== {os.path.basename(onnx_path)} -> {os.path.basename(rknn_path)}")
    print(f"    inputs: {list(zip(names, shapes))}")

    rknn = RKNN(verbose=True)
    # 特征已经在 Python 侧算好，不需要 RKNN 做图像那套均值/归一化
    rknn.config(target_platform=target, optimization_level=3)

    if rknn.load_onnx(model=onnx_path, inputs=names, input_size_list=shapes) != 0:
        raise SystemExit("load_onnx 失败")
    if rknn.build(do_quantization=quantize, dataset=dataset) != 0:
        raise SystemExit("build 失败")
    if rknn.export_rknn(rknn_path) != 0:
        raise SystemExit("export_rknn 失败")
    rknn.release()

    print(f"    ok: {rknn_path} ({os.path.getsize(rknn_path) / 1e6:.0f} MB)")
    return rknn_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", default="rknn_models")
    parser.add_argument("--target", default="rk3576")
    parser.add_argument("--quantize", action="store_true", help="int8 量化，需要 --dataset")
    parser.add_argument("--dataset", help="校准集清单文件，每行一个 .npy 路径")
    args = parser.parse_args()

    if args.quantize and not args.dataset:
        sys.exit("--quantize 需要同时给 --dataset")

    onnx_files = sorted(glob.glob(os.path.join(args.model_dir, "*.onnx")))
    if not onnx_files:
        sys.exit(f"{args.model_dir} 下没有 .onnx，先跑 tools/export_onnx.py")

    for path in onnx_files:
        convert(path, args.target, args.quantize, args.dataset)


if __name__ == "__main__":
    main()
