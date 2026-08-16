"""声纹 embedding 的 RKNN 后端（RK3576 NPU）。

ERes2NetV2 是纯 CNN，在板子 CPU 上跑一次要 4.6 秒，NPU 上 0.47 秒，fp16 与 CPU
的 embedding 余弦相似度 0.999+。前端（kaldi fbank + 减均值）留在 Python 侧，与
modelscope pipeline 里的实现逐行一致。

RKNN 不支持动态 shape，模型窗口定死在 3 秒（298 帧）。短音频循环填充到满窗——
实测 1 秒音频补零后与变长参考的余弦只有 0.84~0.90，循环填充能到 0.98~0.99。

板子上需要额外装运行时（不进 pyproject，只有 aarch64 有包）：

    uv pip install rknn-toolkit-lite2

librknnrt.so 的版本必须 >= 转换时用的 rknn-toolkit2 版本。系统自带的可能偏旧，
放一份新的到家目录再用 LD_LIBRARY_PATH 指过去即可，不必动 /usr/lib。
"""

import os

import numpy as np
import torch
import torchaudio.compliance.kaldi as Kaldi

from config import SPEAKER_RKNN_PATH

SAMPLE_RATE = 16000
# 与 tools/export_onnx.py --speaker-seconds 对齐
WINDOW_MS = 3000
WINDOW_SAMPLES = SAMPLE_RATE * WINDOW_MS // 1000
FEATURE_DIM = 80


def _load_runtime():
    from rknnlite.api import RKNNLite

    if not os.path.isfile(SPEAKER_RKNN_PATH):
        raise FileNotFoundError(
            f"找不到 {SPEAKER_RKNN_PATH}，先用 tools/export_onnx.py + tools/convert_rknn.py 生成"
        )

    runtime = RKNNLite()
    if runtime.load_rknn(SPEAKER_RKNN_PATH) != 0:
        raise RuntimeError(f"load_rknn 失败: {SPEAKER_RKNN_PATH}")
    if runtime.init_runtime() != 0:
        raise RuntimeError("init_runtime 失败，检查 librknnrt.so 版本与 NPU 驱动")
    return runtime


_runtime = _load_runtime()


def _fit_window(audio: np.ndarray) -> np.ndarray:
    """把音频对齐到定长窗口：超长截断，不足则循环填充。"""
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if audio.shape[0] >= WINDOW_SAMPLES:
        return audio[:WINDOW_SAMPLES]
    repeats = int(np.ceil(WINDOW_SAMPLES / max(audio.shape[0], 1)))
    return np.tile(audio, repeats)[:WINDOW_SAMPLES].astype(np.float32)


def _extract_feature(audio: np.ndarray) -> np.ndarray:
    """与 modelscope SpeakerVerificationERes2NetV2.__extract_feature 一致。"""
    feature = Kaldi.fbank(torch.from_numpy(audio).unsqueeze(0), num_mel_bins=FEATURE_DIM)
    feature = feature - feature.mean(dim=0, keepdim=True)
    return feature.unsqueeze(0).numpy()


def generate_embedding(audio: np.ndarray) -> np.ndarray:
    feature = _extract_feature(_fit_window(audio))
    outputs = _runtime.inference(inputs=[feature])
    return np.asarray(outputs[0], dtype=np.float32).reshape(-1)
