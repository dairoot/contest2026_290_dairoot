"""SenseVoiceSmall 的 int8 ONNX 后端（CPU）。

用官方导出的 iic/SenseVoiceSmall-onnx（只有 model_quant.onnx，241 MB），比 PyTorch
那份 936 MB fp32 权重省一大截内存，加载也没有那个 3 GB 的峰值。

前端与解码见 _sense_voice_common；不引入 funasr-onnx（那个包会把 numpy 钉在
<=1.26.4，还要拖 librosa/numba 进来）。
"""

import asyncio
import os
import time

import numpy as np
import onnxruntime
from funasr.utils.postprocess_utils import rich_transcription_postprocess

from asr_model._sense_voice_common import (
    LANGUAGE_IDS,
    MODEL_DIR,
    TEXTNORM_IDS,
    ctc_greedy_decode,
    extract_feats,
)
from config import ASR_ONNX_THREADS
from utils._batch_worker import _ensure_batch_worker

_sess_options = onnxruntime.SessionOptions()
# 0 表示交给 onnxruntime 按核数决定
_sess_options.intra_op_num_threads = ASR_ONNX_THREADS
_sess_options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
_session = onnxruntime.InferenceSession(
    os.path.join(MODEL_DIR, "model_quant.onnx"),
    sess_options=_sess_options,
    providers=["CPUExecutionProvider"],
)
_INPUT_NAMES = [i.name for i in _session.get_inputs()]


class SenseVoiceOnnxWorker:
    """只缓存原始 wav，endpoint 后一次性抽 fbank + 跑完整推理。"""

    _BATCH_SIZE = 4
    model_type = "sense_voice"

    def reset(self) -> None:
        return

    async def get_chunk(self, chunk: np.ndarray) -> np.ndarray:
        return chunk

    def infer_batch(self, wavs: list[np.ndarray]) -> list[dict]:
        feats, feats_len = extract_feats(wavs)
        batch = len(wavs)
        inputs = {
            "speech": feats,
            "speech_lengths": feats_len,
            "language": np.full(batch, LANGUAGE_IDS["zh"], dtype=np.int32),
            "textnorm": np.full(batch, TEXTNORM_IDS["withitn"], dtype=np.int32),
        }
        ctc_logits, encoder_out_lens = _session.run(
            None, {name: inputs[name] for name in _INPUT_NAMES}
        )
        return [
            {"text": ctc_greedy_decode(ctc_logits[i, : int(encoder_out_lens[i])])}
            for i in range(batch)
        ]

    async def generate_text(self, chunks: list[np.ndarray]) -> tuple[str, float, str]:
        full_wav = np.concatenate(chunks).astype(np.float32)
        queue = _ensure_batch_worker(self)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        await queue.put((full_wav, fut))
        t0 = time.perf_counter()
        result = await fut
        elapsed = time.perf_counter() - t0
        return rich_transcription_postprocess(result["text"]), elapsed, ""
