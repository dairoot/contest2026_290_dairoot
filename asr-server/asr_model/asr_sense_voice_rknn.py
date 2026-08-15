"""SenseVoiceSmall 的 RKNN 后端（RK3576 NPU）。

RKNN 不支持动态 shape，窗口在导出时定死（默认 5 秒 = 83 个 LFR 帧）。短音频补零
到满窗——ASR 这边补零是安全的，尾部静音在 CTC 上解成 blank，实测与变长 CPU 模型
逐字一致（声纹那边相反，必须循环填充，见 speaker_rknn.py）。

耗时与音频长短无关，只取决于窗口：5 秒窗口恒定约 390 ms，10 秒窗口约 655 ms。
所以窗口不是越大越好，10 秒窗口下短句还打不过 CPU 的变长 int8 ONNX。

超过窗口的长句按窗口切段再拼接，VAD 闭合后的片段绝大多数一个窗口装得下。

speech_lengths / language / textnorm 三个输入在导出时已经固化成常量，模型只剩一个
speech 输入，见 tools/export_onnx.py。
"""

import asyncio
import re
import time

import numpy as np
from funasr.utils.postprocess_utils import rich_transcription_postprocess

from asr_model._sense_voice_common import ctc_greedy_decode, extract_feats
from config import ASR_RKNN_PATH, ASR_RKNN_WINDOW_MS
from utils._batch_worker import _ensure_batch_worker

SAMPLE_RATE = 16000
WINDOW_SAMPLES = SAMPLE_RATE * ASR_RKNN_WINDOW_MS // 1000
# 尾部新增音频低于这个响度就当静音跳过
SILENCE_DBFS = -45.0


def _load_runtime():
    import os

    from rknnlite.api import RKNNLite

    if not os.path.isfile(ASR_RKNN_PATH):
        raise FileNotFoundError(
            f"找不到 {ASR_RKNN_PATH}，先用 tools/export_onnx.py + tools/convert_rknn.py 生成"
        )

    runtime = RKNNLite()
    if runtime.load_rknn(ASR_RKNN_PATH) != 0:
        raise RuntimeError(f"load_rknn 失败: {ASR_RKNN_PATH}")
    if runtime.init_runtime() != 0:
        raise RuntimeError("init_runtime 失败，检查 librknnrt.so 版本与 NPU 驱动")
    return runtime


_runtime = _load_runtime()
# 与 tools/export_onnx.py 的 _fbank_frames 一致：fbank 10 ms 帧移 / 25 ms 帧长，
# 再按 lfr_n=6 降采样。模型输入帧数必须和这个数对上，对不上 RKNN 会直接报错。
_WINDOW_FRAMES = int((ASR_RKNN_WINDOW_MS - 25) // 10 + 1) // 6
# LFR 之后一帧 60 ms；模型还会在序列最前面插 4 个 prompt 帧（语种/情感/事件/ITN）
_FRAME_MS = 60
_PROMPT_FRAMES = 4


def _infer_window(wav: np.ndarray, skip_ms: int = 0) -> str:
    """跑一个不超过定长窗口的片段，丢掉开头 skip_ms 对应的帧。

    skip_ms 用于长句切段：最后一窗向前对齐成满窗，与上一窗重叠的部分在解码时
    按帧丢掉，既不会漏字也不会重复。
    """
    padded = np.zeros(WINDOW_SAMPLES, dtype=np.float32)
    padded[: min(wav.shape[0], WINDOW_SAMPLES)] = wav[:WINDOW_SAMPLES]
    feats, _ = extract_feats([padded])
    feats = feats[:, :_WINDOW_FRAMES, :]

    ctc_logits = np.asarray(_runtime.inference(inputs=[feats])[0])
    ctc_logits = ctc_logits.reshape(-1, ctc_logits.shape[-1])
    if skip_ms > 0:
        # prompt 帧解出来是 <|zh|> 这类标签，第一窗留着即可，后续窗一并丢掉
        ctc_logits = ctc_logits[_PROMPT_FRAMES + skip_ms // _FRAME_MS :]
    return ctc_greedy_decode(ctc_logits)


def _is_silent(wav: np.ndarray) -> bool:
    if wav.size == 0:
        return True
    rms = float(np.sqrt(np.mean(wav.astype(np.float32) ** 2)))
    return 20 * np.log10(rms + 1e-12) < SILENCE_DBFS


def _infer_utterance(wav: np.ndarray) -> str:
    """整句识别；超过窗口就切段，最后一窗向前对齐避免出现很短的残段。"""
    total = wav.shape[0]
    if total <= WINDOW_SAMPLES:
        return _infer_window(wav)

    texts = []
    start = 0
    while start < total:
        if start + WINDOW_SAMPLES >= total:
            # 尾部新增的这段若本来就是静音，跳过——补零窗口会让模型以为句子在这里
            # 结束，凭空多输出一个句号，还白花一次推理
            if not _is_silent(wav[start:]):
                aligned = max(0, total - WINDOW_SAMPLES)
                overlap_ms = (start - aligned) * 1000 // SAMPLE_RATE
                texts.append(_infer_window(wav[aligned:], overlap_ms))
            break
        texts.append(_infer_window(wav[start : start + WINDOW_SAMPLES]))
        start += WINDOW_SAMPLES

    # 每个窗口末尾都补了零，模型会各自打一个句末标点，接起来就成了「。。」
    return re.sub(r"([。！？])\1+", r"\1", "".join(texts))


class SenseVoiceRknnWorker:
    """接口与 SenseVoiceOnnxWorker 一致；NPU 一次只吃一条，batch 固定为 1。"""

    _BATCH_SIZE = 1
    model_type = "sense_voice"

    def reset(self) -> None:
        return

    async def get_chunk(self, chunk: np.ndarray) -> np.ndarray:
        return chunk

    def infer_batch(self, wavs: list[np.ndarray]) -> list[dict]:
        return [{"text": _infer_utterance(wav)} for wav in wavs]

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
