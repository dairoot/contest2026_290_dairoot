"""把 ASR 和声纹模型导出成 RKNN 能吃的定长 fp32 ONNX。

RKNN 不支持真正的动态 shape，导出时就得把序列长度定死；同时 rknn-toolkit2 不接受
已经 int8 量化的 ONNX（线上用的 iic/SenseVoiceSmall-onnx 就是量化版），所以这里从
PyTorch 权重重新导出 fp32。

只在转换机上跑，板子上不需要这些依赖：

    uv run --with onnx --with onnxscript python tools/export_onnx.py --out-dir rknn_models

产物：
    rknn_models/sensevoice_<sec>s.onnx    [1, N, 560] -> ctc_logits [1, N+4, 25055]
    rknn_models/eres2netv2_<sec>s.onnx    [1, F, 80]  -> embedding  [1, 192]

注意 ASR 那步会加载 936 MB 的 PyTorch 权重，内存峰值约 3 GB，别在板子上跑。
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch

# funasr 的导出走 torch.onnx.export，torch 2.9 默认的 dynamo 导出器会在降 opset 时
# 撞上 "No Adapter To Version 17 for Pad"，强制回退到 TorchScript 导出器。
_orig_onnx_export = torch.onnx.export


def _legacy_onnx_export(*args, **kwargs):
    kwargs.setdefault("dynamo", False)
    return _orig_onnx_export(*args, **kwargs)


torch.onnx.export = _legacy_onnx_export

# fbank 帧移 10 ms、帧长 25 ms；SenseVoice 前端再做 lfr_n=6 的降采样，
# 即每 60 ms 一个模型帧。声纹那边没有 LFR，10 ms 一帧。
FRAME_SHIFT_MS = 10
FRAME_LENGTH_MS = 25
ASR_LFR_N = 6


def _fbank_frames(seconds: float) -> int:
    return int((seconds * 1000 - FRAME_LENGTH_MS) // FRAME_SHIFT_MS + 1)


def _freeze_scalar_inputs(path: str, values: dict[str, int]) -> None:
    """把常量输入固化成 initializer，只给 RKNN 留一个 speech 输入。

    speech_lengths / language / textnorm 在本项目里恒定（整窗长度、中文、带 ITN），
    而 RKNN 对 int32 标量输入的支持很不稳，留着容易转不过去。
    """
    import numpy as np
    import onnx
    from onnx import numpy_helper

    model = onnx.load(path)
    graph = model.graph
    kept = []
    for inp in graph.input:
        if inp.name not in values:
            kept.append(inp)
            continue
        shape = [d.dim_value for d in inp.type.tensor_type.shape.dim]
        tensor = numpy_helper.from_array(
            np.full(shape, values[inp.name], dtype=np.int32), inp.name
        )
        graph.initializer.append(tensor)

    del graph.input[:]
    graph.input.extend(kept)
    onnx.save(model, path)
    print(f"[asr] 固化常量输入: {values}，剩余输入 {[i.name for i in kept]}")


def export_asr(out_dir: str, seconds: float, dynamic_onnx: str | None = None) -> str:
    feats_length = _fbank_frames(seconds) // ASR_LFR_N
    dst = os.path.join(out_dir, f"sensevoice_{seconds:g}s.onnx")

    if dynamic_onnx:
        # 换窗口大小时复用已有的动态 ONNX，省掉一次 3 GB 峰值的 PyTorch 导出
        dynamic_path = dynamic_onnx
    else:
        from funasr import AutoModel

        from utils._model_path import resolve_model_path

        model_dir = resolve_model_path("iic/SenseVoiceSmall")
        print(f"[asr] 加载 PyTorch 权重: {model_dir}")
        model = AutoModel(
            model=model_dir, device="cpu", disable_update=True, disable_pbar=True
        )
        # funasr 把 model.onnx 写在模型目录里，动态 shape
        dynamic_path = os.path.join(
            model.export(type="onnx", quantize=False), "model.onnx"
        )
    print(f"[asr] 动态 ONNX: {dynamic_path}")

    # 两个动态维度分别定死：batch_size=1、feats_length=N
    src = dynamic_path
    for dim_param, dim_value in (("batch_size", 1), ("feats_length", feats_length)):
        subprocess.run(
            [
                sys.executable, "-m", "onnxruntime.tools.make_dynamic_shape_fixed",
                "--dim_param", dim_param, "--dim_value", str(dim_value), src, dst,
            ],
            check=True,
        )
        src = dst
    # 与 asr_model/asr_sense_voice_onnx.py 里的取值保持一致：zh / withitn
    _freeze_scalar_inputs(
        dst, {"speech_lengths": feats_length, "language": 3, "textnorm": 14}
    )
    print(f"[asr] 定长 ONNX: {dst}  feats_length={feats_length}（{seconds:g} 秒）")
    return dst


def export_speaker(out_dir: str, seconds: float) -> str:
    # 要导出的正是 PyTorch 权重，别被 .env 里的 SPEAKER_BACKEND=rknn 带偏
    os.environ["SPEAKER_BACKEND"] = "modelscope"
    import speaker

    frames = _fbank_frames(seconds)
    dst = os.path.join(out_dir, f"eres2netv2_{seconds:g}s.onnx")

    # pipeline 里的 SpeakerVerificationERes2NetV2 = fbank 前端 + embedding_model，
    # 前端留在 Python 侧（见 speaker.py），只导出网络本身。
    embedding_model = speaker.sv_pipeline.model.embedding_model.eval()
    torch.onnx.export(
        embedding_model,
        torch.randn(1, frames, 80),
        dst,
        input_names=["feats"],
        output_names=["embedding"],
        opset_version=13,
        dynamic_axes=None,
    )
    print(f"[speaker] 定长 ONNX: {dst}  frames={frames}（{seconds:g} 秒）")
    return dst


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="rknn_models")
    parser.add_argument("--asr-seconds", type=float, default=10.0, help="ASR 定长窗口秒数")
    parser.add_argument(
        "--speaker-seconds",
        type=float,
        default=3.0,
        help="声纹定长窗口秒数，默认与 MAX_SPEAKER_EMBEDDING_MS 对齐",
    )
    parser.add_argument(
        "--asr-dynamic-onnx",
        help="已有的动态 shape ONNX；给了就跳过 PyTorch 导出，只做定长化。换窗口大小时用",
    )
    parser.add_argument("--skip-asr", action="store_true")
    parser.add_argument("--skip-speaker", action="store_true")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    if not args.skip_speaker:
        export_speaker(args.out_dir, args.speaker_seconds)
    if not args.skip_asr:
        export_asr(args.out_dir, args.asr_seconds, args.asr_dynamic_onnx)


if __name__ == "__main__":
    main()
