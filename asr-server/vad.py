from typing import Any

import numpy as np
from funasr import AutoModel

from utils._device import get_device
from utils._model_path import resolve_model_path

_VAD_MODEL_ID = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
_VAD_REVISION = "v2.0.4"
_VAD_MODEL_PATH = resolve_model_path(_VAD_MODEL_ID, _VAD_REVISION)

vad_model = AutoModel(
    model=_VAD_MODEL_PATH,
    model_revision=_VAD_REVISION,
    max_end_silence_time=240,  # 端点判定的最大尾部静音时长，单位毫秒
    speech_noise_thres=0.9,  # 语音和噪声的判定阈值，越高越严格
    disable_update=True,  # 禁止运行时自动检查或更新模型
    disable_pbar=True,  # 禁用模型加载和推理过程中的进度条
    device=get_device(),
)


class VadWorker:

    def __init__(self, chunk_size: int):
        self.chunk_size = chunk_size

        self.vad_cache: dict = {}
        self.vad_cached_segments: list[Any] = []
        self.vad_last_pos_ms = 0

    def generate_vad_segments(self, chunk: np.ndarray):

        res = vad_model.generate(
            input=chunk,
            cache=self.vad_cache,
            chunk_size=self.chunk_size,
        )

        if not res or not res[0].get("value"):
            return

        self.vad_cached_segments.extend(res[0]["value"])
        self.vad_last_pos_ms = self.vad_cached_segments[-1][1]

        # print(self.vad_cached_segments)

    def get_silence_duration_ms(self, audio_elapsed_ms: float):
        if self.vad_last_pos_ms == -1:
            return 0

        silence_duration = audio_elapsed_ms - self.vad_last_pos_ms
        # print(
        #     f"==== vad 长度 {len(self.vad_cached_segments)} 音频时长 {audio_elapsed_ms / 1000}s，静音时长 {silence_duration / 1000}s ===="
        # )
        return silence_duration

    def reset(self):
        self.vad_cache = {}
        self.vad_last_pos_ms = 0
        self.vad_cached_segments = []
