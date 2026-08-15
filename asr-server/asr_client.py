import asyncio
import logging
import os
import time
from datetime import datetime

import numpy as np
import scipy.io.wavfile as wavfile

from config import ASR_MODEL_TYPE
from speaker import SpeakerWorker
from vad import VadWorker
from event_emitter import AsyncEventEmitter

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHUNK_SIZE_MS = 240
BLOCK_SIZE = int(CHUNK_SIZE_MS * SAMPLE_RATE / 1000)
MAX_SPEAKER_EMBEDDING_MS = 3000


FAST_REPLY_SILENCE_DURATION_MS = 240


def get_asr_worker(model_type):
    # 各后端都懒加载：选了 NPU 就不该再把 241 MB 的 ONNX 会话拉起来。
    # 想让加载失败在启动时暴露而不是等第一个连接，在服务启动时调一次本函数即可。
    if model_type == "sense_voice_rknn":
        from asr_model.asr_sense_voice_rknn import SenseVoiceRknnWorker

        return SenseVoiceRknnWorker()
    # sense_voice_onnx 是历史别名，PyTorch 那份后端已经删掉
    if model_type in ("sense_voice", "sense_voice_onnx"):
        from asr_model.asr_sense_voice_onnx import SenseVoiceOnnxWorker

        return SenseVoiceOnnxWorker()
    if model_type == "volc":
        from asr_model.asr_volc import VolcAsrWorker

        return VolcAsrWorker()
    raise ValueError(f"Invalid model_type: {model_type}")


class AsrClient(AsyncEventEmitter):

    def __init__(
        self,
        audio_save_dir: str = "",
        model_type: str = ASR_MODEL_TYPE,
        slow_reply_silence_duration_ms: int = 960,
    ) -> None:
        super().__init__()
        self.vad = VadWorker(CHUNK_SIZE_MS)
        self.asr = get_asr_worker(model_type)
        self.speaker = SpeakerWorker()

        self.audio_queue: asyncio.Queue[np.ndarray | tuple[np.ndarray, float]] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        # WebSocket / sounddevice 都可以提交任意长度的音频。先在这里重组为
        # VAD 需要的 240 ms 块，transport 不再负责切块。
        self._input_buffer = np.array([], dtype=np.float32)
        self.audio_buffer = np.array([], dtype=np.float32)
        self.audio_buffer_end_ts_ms: float | None = None
        # 未检测到语音时 audio_buffer 只保留预卷块，但流式 VAD 缓存持续累积；
        # 因此 VAD 必须使用独立时间轴，不能用滚动后的 audio_buffer 长度计算静音。
        self.vad_elapsed_ms: float = 0.0
        self.asr_chunks: list[np.ndarray] = []
        self.fast_vad_cached_segments_length = 0
        self.content = ""
        self.elapsed = 0.0
        self.speaker_id = ""
        self.last_speech_end_ts_ms = 0
        self.fast_reply_checked = False
        self.slow_reply_silence_duration_ms = slow_reply_silence_duration_ms
        self.audio_save_dir = audio_save_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_logs")

    async def process_audio_chunk(self) -> None:
        while True:
            try:
                item = await self.audio_queue.get()
                if isinstance(item, tuple):
                    chunk, end_ts_ms = item
                else:
                    # 兼容仍直接向 audio_queue 写入 ndarray 的旧宿主。
                    chunk, end_ts_ms = item, time.time() * 1000
                await self._send_audio_chunk(chunk, end_ts_ms=end_ts_ms)
            except Exception:
                self.clear()
                logger.exception("process_audio_chunk failed")

    def enqueue_audio_chunk(self, chunk: np.ndarray, *, end_ts_ms: float | None = None) -> None:
        """入队音频，并保存末样点的 Unix 时间戳（毫秒）。"""
        if end_ts_ms is None:
            end_ts_ms = time.time() * 1000
        self.audio_queue.put_nowait((chunk, end_ts_ms))

    def clear(self, keep_first_packet=False):
        self._input_buffer = np.array([], dtype=np.float32)
        if keep_first_packet:
            # 保留最后一个块，避免首字符丢失；方案B 的 sample_cache 也要保留，下一块继续累积帧
            self.audio_buffer = self.audio_buffer[-BLOCK_SIZE:]
            self.asr_chunks = self.asr_chunks[-1:]
        else:
            self.audio_buffer = np.array([], dtype=np.float32)
            self.audio_buffer_end_ts_ms = None
            self.asr_chunks = []
            self.asr.reset()
            self.vad.reset()
            self.vad_elapsed_ms = 0.0

        self.fast_vad_cached_segments_length = 0  # 重置快速回复的 VAD 缓存长度
        self.fast_reply_checked = False
        self.content = ""
        self.elapsed = 0.0
        self.speaker_id = ""

    def audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if self._loop is not None:
            item = (indata[:, 0].copy(), time.time() * 1000)
            self._loop.call_soon_threadsafe(self.audio_queue.put_nowait, item)

    def is_question(self, content: str) -> bool:
        content = content.rstrip("。")
        return content.endswith(("吗", "嘛", "么", "呢", "吧", "啦", "？", "?", "拜拜", "再见", "晚安", "退下"))

    def _save_audio(self, audio: np.ndarray, filepath: str) -> None:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        wavfile.write(filepath, SAMPLE_RATE, (audio * 32767).astype(np.int16))


    def _get_speech_duration_ms(self) -> float:
        """返回 VAD 检测到的实际语音时长，不用于裁剪声纹输入音频。"""
        active_start_ms: float | None = None
        speech_ms = 0.0
        for start_ms, end_ms in self.vad.vad_cached_segments:
            if start_ms is not None and start_ms >= 0:
                active_start_ms = float(start_ms)
            if end_ms is not None and end_ms >= 0 and active_start_ms is not None:
                speech_ms += max(0.0, float(end_ms) - active_start_ms)
                active_start_ms = None

        # 正常 reply 发生在 VAD 闭合后；这里兼容达到 60 秒上限时仍未闭合的语音段。
        if active_start_ms is not None:
            speech_ms += max(0.0, self.vad_elapsed_ms - active_start_ms)

        audio_total_ms = self.audio_buffer.shape[0] * 1000.0 / SAMPLE_RATE
        return min(speech_ms, audio_total_ms)

    async def reply(self, speaker_id: str, content: str, reply_type: str, asr_elapsed: float) -> None:

        if not content or not content.strip(" \t\r\n。"):
            logger.info("[asr] drop empty result type=%s", reply_type)
            self.clear()
            return

        asr_elapsed_ms = round(asr_elapsed * 1000)
        reply_duration_ms = round(time.time() * 1000 - self.last_speech_end_ts_ms)
        logger.info(
            "[asr] asr_reply type=%s asr_elapsed_ms=%d reply_duration_ms=%d content=%r",
            reply_type,
            asr_elapsed_ms,
            reply_duration_ms,
            content,
        )

        audio_filename = f"{datetime.now().strftime('%Y%m%d/%H%M%S')}_{content[:20]}.wav"
        audio_path = os.path.join(self.audio_save_dir, audio_filename)
        audio = self.audio_buffer.copy()
        if self._loop:
            await self._loop.run_in_executor(None, self._save_audio, audio, audio_path)

        speech_ms = self._get_speech_duration_ms()
        # 时间锚点在音频入队时记录，不受 ASR/声纹耗时影响。
        audio_ms = audio.shape[0] * 1000.0 / SAMPLE_RATE
        end_ts = self.audio_buffer_end_ts_ms
        if end_ts is None:
            end_ts = time.time() * 1000
        start_ts = end_ts - audio_ms
        max_embedding_samples = int(MAX_SPEAKER_EMBEDDING_MS * SAMPLE_RATE / 1000)
        embedding_audio = audio[:max_embedding_samples]
        embedding = await self.speaker.generate_embedding(embedding_audio)
        logger.info(
            "[asr] speaker_embedding audio_ms=%.0f speech_ms=%.0f", audio_ms, speech_ms
        )

        self.clear()

        await self.emit(
            "stt",
            {
                "speaker_id": speaker_id,
                "content": content,
                "audio_path": audio_path,
                "elapsed": asr_elapsed,
                "embedding": embedding,
                "speech_ms": speech_ms,
                "start_ts": start_ts,
                "end_ts": end_ts,
            },
        )

    def calc_dbfs(self, chunk: np.ndarray) -> float:
        # 计算 RMS（均方根）
        rms = np.sqrt(np.mean(chunk.astype(np.float32) ** 2))
        # 加一个极小值，避免 log(0)
        return 20 * np.log10(rms + 1e-12)

    def get_audio_elapsed_ms(self):
        return self.audio_buffer.shape[0] / (SAMPLE_RATE / 1000)

    def get_silence_duration_ms(self) -> float:
        silence_duration_ms = self.vad.get_silence_duration_ms(self.vad_elapsed_ms)
        return silence_duration_ms

    async def _send_audio_chunk(self, chunk: np.ndarray, *, end_ts_ms: float | None = None) -> None:
        """缓存任意长度的输入，并按 240 ms 块送入 VAD / ASR。"""
        if end_ts_ms is None:
            end_ts_ms = time.time() * 1000

        chunk = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if chunk.size == 0:
            return

        # 先把成员缓存移到局部变量。_process_audio_block() 可能在一句话结束时
        # 调用 clear()；局部变量可确保同一输入中尚未处理的后续块不会被清掉。
        buffered = np.concatenate([self._input_buffer, chunk])
        self._input_buffer = np.array([], dtype=np.float32)
        while buffered.shape[0] >= BLOCK_SIZE:
            block = buffered[:BLOCK_SIZE].copy()
            buffered = buffered[BLOCK_SIZE:]
            remaining_ms = buffered.shape[0] * 1000.0 / SAMPLE_RATE
            await self._process_audio_block(
                block,
                end_ts_ms=end_ts_ms - remaining_ms,
            )

        self._input_buffer = buffered.copy()

    async def _process_audio_block(
        self,
        chunk: np.ndarray,
        *,
        end_ts_ms: float,
    ) -> None:
        """处理一个长度固定为 240 ms 的音频块。"""
        self.audio_buffer_end_ts_ms = end_ts_ms
        self.vad.generate_vad_segments(chunk)
        self.vad_elapsed_ms += chunk.shape[0] * 1000.0 / SAMPLE_RATE
        self.audio_buffer = np.concatenate([self.audio_buffer, chunk])

        # 火山 ASR 在 VAD 首次检出活动语音时建连。此时先补发上一块预卷音频，
        # 当前块及后续块由 get_chunk 立即上传；本地 ASR 仍只在 VAD 结束后推理。
        # is_streaming 只有 VolcAsrWorker 有，model_type 必须先判，否则本地 ASR 会 AttributeError
        if (
            self.asr.model_type == "volc"
            and self.vad.vad_last_pos_ms == -1
            and not self.asr.is_streaming
        ):
            await self.asr.start_stream(self.asr_chunks[-1:])
        self.asr_chunks.append(await self.asr.get_chunk(chunk))

        # 还没检测到任何语音：持续清空，仅保留最后一块避免首字丢失
        if len(self.vad.vad_cached_segments) == 0:
            self.clear(keep_first_packet=True)
            return

        # 当前仍在说话中（最后一段未闭合），等待静音
        if self.vad.vad_last_pos_ms == -1:
            return

        # VAD 闭合只表示出现短停顿，不能在这里取文本：generate_text 会发末包并关闭
        # 火山会话，说话人继续说就得新建会话，一句话被拆成多段识别，导致截断、
        # 重复和吞词。会话保持到 _slow_reply 一次性结束。
        audio_elapsed_ms = self.get_audio_elapsed_ms()
        silence_duration = self.get_silence_duration_ms()

        self.last_speech_end_ts_ms = end_ts_ms - silence_duration

        if self._vad_has_new_speech():
            self.fast_reply_checked = False

        # 音频长度超过 1 分钟，直接触发解析回复，避免长时间累积
        if audio_elapsed_ms >= 60000:
            await self._slow_reply()
            return

        if silence_duration >= self.slow_reply_silence_duration_ms:
            await self._slow_reply()
        elif silence_duration >= FAST_REPLY_SILENCE_DURATION_MS and not self.fast_reply_checked and self.asr.model_type != "volc":
            await self._fast_reply()

    def _vad_has_new_speech(self) -> bool:
        """相较上次 ASR，VAD 段数是否变化；未变化说明无新语音，可跳过重复识别"""
        return self.fast_vad_cached_segments_length != len(self.vad.vad_cached_segments)

    async def _slow_reply(self) -> None:
        """长静音兜底回复：无条件回复，防止卡住"""
        if not self._vad_has_new_speech():
            await self.reply(self.speaker_id, self.content, "slow", self.elapsed)
            return

        self.content, self.elapsed, self.speaker_id = await self.asr.generate_text(self.asr_chunks)
        await self.reply(self.speaker_id, self.content, "slow", self.elapsed)

    async def _fast_reply(self, *, use_cached_result: bool = False) -> None:
        """快速抢答：仅在识别出问句时才真正回复"""
        self.fast_reply_checked = True
        self.fast_vad_cached_segments_length = len(self.vad.vad_cached_segments)
        if not use_cached_result:
            self.content, self.elapsed, self.speaker_id = await self.asr.generate_text(self.asr_chunks)
        if self.is_question(self.content):
            await self.reply(self.speaker_id, self.content, "fast", self.elapsed)
