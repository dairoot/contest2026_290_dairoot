import asyncio

import numpy as np

from config import SPEAKER_BACKEND

_MODEL_ID = "iic/speech_eres2netv2w24s4ep4_sv_zh-cn_16k-common"

if SPEAKER_BACKEND == "rknn":
    # RK3576 的 NPU：板子 CPU 上一次 4.6 秒，NPU 0.47 秒
    from speaker_rknn import generate_embedding as _generate_embedding

    sv_pipeline = None
else:
    from modelscope.pipelines import pipeline

    from utils._device import get_device
    from utils._model_path import resolve_model_path

    _device = get_device()
    # modelscope pipeline 只支持 cpu/cuda/gpu，mps 回退到 cpu
    _pipeline_device = _device if _device in ("cpu", "cuda") else "cpu"

    sv_pipeline = pipeline(
        task="speaker-verification",
        model=resolve_model_path(_MODEL_ID),
        device=_pipeline_device,
        disable_update=True,
    )

    def _generate_embedding(audio: np.ndarray) -> np.ndarray:
        return sv_pipeline([audio], output_emb=True)["embs"][0]


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


SAME_SPEAKER_THRESHOLD = 0.50
SPEAKER_MARGIN = 0.05
ADD_SAMPLE_THRESHOLD = 0.60
MAX_SAMPLES_PER_SPEAKER = 10
# 音频时长低于该阈值的样本，embedding 方差大，不参与匹配也不入库，避免污染 centroid
MIN_SV_SPEECH_MS = 1500
UNKNOWN_SPEAKER = "unknown"


class SpeakerWorker:

    def __init__(self) -> None:
        self.speaker_list: list[dict] = []

    async def generate_embedding(self, audio_buffer: np.ndarray) -> np.ndarray:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _generate_embedding, audio_buffer)

    @classmethod
    def classify(
        cls,
        embedding: np.ndarray,
        speaker_list: list[dict],
        threshold: float = SAME_SPEAKER_THRESHOLD,
        margin: float = SPEAKER_MARGIN,
    ) -> tuple[str | None, float]:
        """在已知说话人列表中为新 embedding 找到最匹配的说话人。

        按 name 将 speaker_list 聚合后，用该说话人的 centroid
        （平均 embedding）与新样本计算余弦相似度。要求：
        1. best_score > threshold；
        2. best_score 与第二名差距 ≥ margin（避免被两类同时近似时强行归类）。

        返回 (name, best_score)。name 为 None 表示新说话人。

        使用 centroid 而非"任一样本超过阈值"的原因：
        某个说话人样本越多，偶然出现一条与新样本相似的 embedding
        的概率越高，会导致新说话人被错误归类到样本量大的那一类。
        """
        if not speaker_list:
            return None, -1.0

        groups: dict[str, list[np.ndarray]] = {}
        for item in speaker_list:
            groups.setdefault(item["name"], []).append(item["embedding"])

        query = np.asarray(embedding, dtype=np.float32)
        scores: dict[str, float] = {}
        for name, embs in groups.items():
            stacked = np.stack([np.asarray(e, dtype=np.float32) for e in embs])
            # 先 L2 归一化再求均值，避免模长大的样本（通常是长音频）主导 centroid
            norms = np.linalg.norm(stacked, axis=1, keepdims=True)
            stacked_norm = stacked / (norms + 1e-12)
            centroid = stacked_norm.mean(axis=0)
            scores[name] = cosine_similarity(centroid, query)

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best_name, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else -1.0
        print(f"best_name: {best_name}, best_score: {best_score}", f"second_score: {second_score}")

        if best_score > threshold and (best_score - second_score) >= margin:
            return best_name, best_score

        return None, best_score

    @classmethod
    def remember(
        cls,
        speaker_list: list[dict],
        name: str,
        embedding: np.ndarray,
        score: float,
        add_threshold: float = ADD_SAMPLE_THRESHOLD,
        max_samples: int = MAX_SAMPLES_PER_SPEAKER,
    ) -> None:
        """把样本写入 speaker_list，受两条规则约束以避免 centroid 漂移：

        1. 仅当为新说话人，或匹配分数 ≥ add_threshold 时入库；
           边界样本（threshold ~ add_threshold）不污染 centroid。
        2. 每个说话人最多保留 max_samples 条，达到上限后丢弃；
           防止某个说话人样本无限增长后分布扩散。
        """
        existing_count = sum(1 for i in speaker_list if i["name"] == name)
        if existing_count == 0:
            speaker_list.append({"name": name, "embedding": embedding})
            return
        if score < add_threshold:
            return
        if existing_count >= max_samples:
            return
        speaker_list.append({"name": name, "embedding": embedding})

    def get_speaker_name(
        self,
        embedding: np.ndarray,
        offset: int = 0,
        speech_ms: float | None = None,
    ) -> tuple[str, float]:
        """根据 embedding 找匹配的说话人。

        speech_ms 为该 embedding 对应音频时长（毫秒，由上游 ASR 提供）。
        低于 MIN_SV_SPEECH_MS 时视为不可靠，返回 UNKNOWN_SPEAKER 且不入库。
        不传 speech_ms 时退化为老行为，保持向后兼容。
        """
        if speech_ms is not None and speech_ms < MIN_SV_SPEECH_MS:
            return UNKNOWN_SPEAKER, -1.0

        matched, score = self.classify(embedding, self.speaker_list)
        if matched is not None:
            speaker_name = matched
        else:
            speaker_count = len({i["name"] for i in self.speaker_list}) + 1 + offset
            speaker_name = f"speaker_{speaker_count}"

        self.remember(self.speaker_list, speaker_name, embedding, score)
        return speaker_name, score if matched is not None else -1.0
