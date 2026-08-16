"""火山引擎大模型流式语音识别（豆包语音）worker。

文档: https://docs.volcengine.com/docs/6561/1354869
端点: wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream
     （流式输入模式：实时发送音频，收到末包后返回高准确率结果）

.env 配置: VOLC_APP_ID / VOLC_ACCESS_TOKEN / VOLC_RESOURCE_ID（可选，默认小时版）
热词配置: VOLC_HOTWORDS（逗号分隔的直传热词）/ VOLC_BOOSTING_TABLE_NAME /
VOLC_BOOSTING_TABLE_ID（自学习平台词表，直传热词优先级更高）

AsrClient 检测到 VAD 语音开始后调用 start_stream，之后 get_chunk 会立即上传
音频；fast/slow reply 调用 generate_text 时只发送末包并等待结果。短停顿后继续
说话会新建一次火山会话，worker 会把同一轮内多次会话的文本合并返回。
"""

import asyncio
import gzip
import json
import logging
import re
import time
import uuid
from collections.abc import Iterable
from typing import Any

import numpy as np
import websockets

from config import (
    VOLC_ACCESS_TOKEN,
    VOLC_APP_ID,
    VOLC_BOOSTING_TABLE_ID,
    VOLC_BOOSTING_TABLE_NAME,
    VOLC_HOTWORDS,
    VOLC_RESOURCE_ID,
)

logger = logging.getLogger(__name__)

WS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"

SAMPLE_RATE = 16000
CHUNK_MS = 200
_BYTES_PER_CHUNK = SAMPLE_RATE * 2 * CHUNK_MS // 1000
_TIMEOUT_S = 15

# 流式输入（nostream）接口的直传热词上限，超出部分会被丢弃。
MAX_HOTWORDS = 5000
_HOTWORD_SEPARATORS = re.compile(r"[,，;；、\r\n]+")

# ---- 二进制协议常量 ----
PROTOCOL_VERSION = 0b0001
HEADER_SIZE = 0b0001  # x4 字节

FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
SERVER_ERROR_RESPONSE = 0b1111

POS_SEQUENCE = 0b0001
NEG_WITH_SEQUENCE = 0b0011

SERIAL_JSON = 0b0001
SERIAL_NONE = 0b0000
COMPRESS_GZIP = 0b0001


def parse_hotwords(hotwords: str | Iterable[str] | None) -> list[str]:
    """把 "热词1,热词2" 或字符串序列整理成去重后的热词列表。"""
    if not hotwords:
        return []

    if isinstance(hotwords, str):
        candidates: Iterable[str] = _HOTWORD_SEPARATORS.split(hotwords)
    else:
        candidates = (str(word) for word in hotwords)

    # dict 去重同时保留顺序：热词表按传入顺序生效，重复词只会浪费额度。
    words = list(dict.fromkeys(word.strip() for word in candidates if word.strip()))
    if len(words) > MAX_HOTWORDS:
        logger.warning("[volc] 热词数量 %d 超过上限 %d，超出部分已丢弃", len(words), MAX_HOTWORDS)
        words = words[:MAX_HOTWORDS]
    return words


def _build_corpus(
    hotwords: list[str],
    boosting_table_name: str = "",
    boosting_table_id: str = "",
) -> dict:
    """构造 request.corpus；无任何热词配置时返回空 dict，请求里就不带该字段。"""
    corpus: dict[str, str] = {}
    if boosting_table_name:
        corpus["boosting_table_name"] = boosting_table_name
    if boosting_table_id:
        corpus["boosting_table_id"] = boosting_table_id
    if hotwords:
        # context 必须是 JSON 字符串；直传热词优先级高于自学习平台的词表。
        corpus["context"] = json.dumps(
            {"hotwords": [{"word": word} for word in hotwords]},
            ensure_ascii=False,
        )
    return corpus


def _build_params(corpus: dict | None = None) -> dict:
    request: dict[str, Any] = {
        "model_name": "bigmodel",
        "enable_punc": True,
        "enable_itn": True,
        "show_utterances": True,
        "enable_speaker_info": True,
        "ssd_version": "200",
    }
    if corpus:
        # 实测只有 request.corpus.context 会被采纳：放在 request 层无效，
        # 传非 JSON 串服务端会报 "fail to unmarshal corpusCtx"。
        request["corpus"] = corpus
    return {
        "user": {"uid": str(uuid.uuid4())},
        "audio": {"format": "pcm", "codec": "raw", "rate": SAMPLE_RATE, "bits": 16, "channel": 1},
        "request": request,
    }


def _make_header(message_type, flags, serial=SERIAL_JSON, compression=COMPRESS_GZIP):
    return bytes([
        (PROTOCOL_VERSION << 4) | HEADER_SIZE,
        (message_type << 4) | flags,
        (serial << 4) | compression,
        0x00,
    ])


def _make_full_client_request(seq: int, params: dict) -> bytes:
    payload = gzip.compress(json.dumps(params).encode("utf-8"))
    msg = bytearray(_make_header(FULL_CLIENT_REQUEST, POS_SEQUENCE))
    msg.extend(seq.to_bytes(4, "big", signed=True))
    msg.extend(len(payload).to_bytes(4, "big"))
    msg.extend(payload)
    return bytes(msg)


def _make_audio_request(seq: int, chunk: bytes, is_last: bool) -> bytes:
    payload = gzip.compress(chunk)
    flags = NEG_WITH_SEQUENCE if is_last else POS_SEQUENCE
    if is_last:
        seq = -seq
    msg = bytearray(_make_header(AUDIO_ONLY_REQUEST, flags, serial=SERIAL_NONE))
    msg.extend(seq.to_bytes(4, "big", signed=True))
    msg.extend(len(payload).to_bytes(4, "big"))
    msg.extend(payload)
    return bytes(msg)


def _parse_response(res: bytes) -> dict:
    header_size = res[0] & 0x0F
    message_type = res[1] >> 4
    flags = res[1] & 0x0F
    serial = res[2] >> 4
    compression = res[2] & 0x0F

    payload = res[header_size * 4:]
    out = {"message_type": message_type, "is_last_package": False, "seq": None, "payload_msg": None}

    if flags & 0x01:  # 带序列号
        out["seq"] = int.from_bytes(payload[:4], "big", signed=True)
        payload = payload[4:]
    if flags & 0x02:  # 末包
        out["is_last_package"] = True

    if message_type == SERVER_ERROR_RESPONSE:
        out["code"] = int.from_bytes(payload[:4], "big", signed=False)
        payload = payload[4:]

    if len(payload) >= 4:
        size = int.from_bytes(payload[:4], "big", signed=True)
        body = payload[4:4 + size]
        if compression == COMPRESS_GZIP and body:
            body = gzip.decompress(body)
        if serial == SERIAL_JSON and body:
            out["payload_msg"] = json.loads(body.decode("utf-8"))
        elif body:
            out["payload_msg"] = body.decode("utf-8", "replace")
    return out


def _logid_from_ws(ws: Any) -> str:
    """从握手响应头读取 X-Tt-Logid，便于向火山排查问题。"""
    response = getattr(ws, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        return ""
    return headers.get("X-Tt-Logid") or headers.get("x-tt-logid") or ""


async def _recognize(pcm: bytes, corpus: dict | None = None) -> dict:
    chunks = [pcm[i:i + _BYTES_PER_CHUNK] for i in range(0, len(pcm), _BYTES_PER_CHUNK)] or [b""]
    headers = {
        "X-Api-App-Key": VOLC_APP_ID,
        "X-Api-Access-Key": VOLC_ACCESS_TOKEN,
        "X-Api-Resource-Id": VOLC_RESOURCE_ID,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }
    params = _build_params(corpus)

    async with websockets.connect(WS_URL, additional_headers=headers, max_size=None) as ws:
        logger.info("[volc] connected logid=%s", _logid_from_ws(ws))
        seq = 1
        await ws.send(_make_full_client_request(seq, params))
        for i, chunk in enumerate(chunks):
            seq += 1
            await ws.send(_make_audio_request(seq, chunk, is_last=(i == len(chunks) - 1)))

        final: dict = {}
        async for msg in ws:
            r = _parse_response(msg)
            if r["message_type"] == SERVER_ERROR_RESPONSE:
                raise RuntimeError(f"火山 ASR 服务端错误 code={r.get('code')}: {r.get('payload_msg')}")
            pm = r.get("payload_msg") or {}
            if pm:
                final.update(pm)
            if r["is_last_package"]:
                break
        return final


class VolcAsrWorker:
    """火山 ASR worker，支持 VAD 驱动的实时流式上传。"""

    model_type = "volc"
    supports_streaming_input = True

    def __init__(
        self,
        hotwords: str | Iterable[str] | None = None,
        boosting_table_name: str = "",
        boosting_table_id: str = "",
    ) -> None:
        """hotwords 等参数缺省时回落到 .env 配置；显式传入可按会话定制热词。"""
        self._hotwords = parse_hotwords(VOLC_HOTWORDS if hotwords is None else hotwords)
        self._boosting_table_name = boosting_table_name or VOLC_BOOSTING_TABLE_NAME
        self._boosting_table_id = boosting_table_id or VOLC_BOOSTING_TABLE_ID
        self._ws: Any | None = None
        self._receiver_task: asyncio.Task[dict] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seq = 0
        self._pending_pcm = bytearray()
        self._completed_texts: list[str] = []
        self._last_speaker_id = ""
        self._last_elapsed = 0.0
        self._streaming_turn_started = False
        self._generation = 0

    @property
    def is_streaming(self) -> bool:
        return self._ws is not None

    @property
    def hotwords(self) -> list[str]:
        return list(self._hotwords)

    def set_hotwords(self, hotwords: str | Iterable[str] | None) -> None:
        """更新直传热词；下一次建连（下一段语音）开始生效。"""
        self._hotwords = parse_hotwords(hotwords)

    def _check_credentials(self) -> None:
        if not VOLC_APP_ID or not VOLC_ACCESS_TOKEN:
            raise RuntimeError("缺少 VOLC_APP_ID / VOLC_ACCESS_TOKEN，请在 .env 中配置")

    def _headers(self) -> dict[str, str]:
        return {
            "X-Api-App-Key": VOLC_APP_ID,
            "X-Api-Access-Key": VOLC_ACCESS_TOKEN,
            "X-Api-Resource-Id": VOLC_RESOURCE_ID,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

    def _corpus(self) -> dict:
        return _build_corpus(self._hotwords, self._boosting_table_name, self._boosting_table_id)

    def _params(self) -> dict:
        return _build_params(self._corpus())

    def reset(self) -> None:
        """立即丢弃当前轮状态，并在线程安全地关闭尚未完成的连接。"""
        self._generation += 1
        self._completed_texts = []
        self._last_speaker_id = ""
        self._last_elapsed = 0.0
        self._streaming_turn_started = False
        self._pending_pcm.clear()

        ws = self._ws
        receiver_task = self._receiver_task
        loop = self._loop
        self._ws = None
        self._receiver_task = None
        self._seq = 0

        if loop is None or not loop.is_running() or (ws is None and receiver_task is None):
            return

        def cleanup() -> None:
            if receiver_task is not None and not receiver_task.done():
                receiver_task.cancel()
            if ws is not None:
                asyncio.create_task(ws.close())

        # clear() 也会从 sounddevice 的音频回调线程调用。
        loop.call_soon_threadsafe(cleanup)

    async def start_stream(self, pre_roll_chunks: Iterable[np.ndarray] = ()) -> None:
        """建立识别会话，并先上传 VAD 检出语音前保留的预卷音频。"""
        if self.is_streaming:
            return

        self._check_credentials()
        self._loop = asyncio.get_running_loop()
        generation = self._generation
        ws = await websockets.connect(
            WS_URL,
            additional_headers=self._headers(),
            max_size=None,
        )
        if generation != self._generation:
            await ws.close()
            return

        self._ws = ws
        self._seq = 1
        self._pending_pcm.clear()
        self._streaming_turn_started = True
        logger.info("[volc] connected logid=%s", _logid_from_ws(ws))

        try:
            params = self._params()
            # 排查热词不生效时，用 DEBUG 日志确认实际发给火山的配置。
            logger.debug("[volc] start_stream corpus=%s", params["request"].get("corpus"))
            await ws.send(_make_full_client_request(self._seq, params))
            self._receiver_task = asyncio.create_task(self._receive_responses(ws))
            for chunk in pre_roll_chunks:
                await self._buffer_and_send(chunk)
        except BaseException:
            await self._close_session(ws)
            raise

    async def _receive_responses(self, ws: Any) -> dict:
        final: dict = {}
        async for msg in ws:
            if not isinstance(msg, bytes):
                raise RuntimeError(f"火山 ASR 返回了非二进制消息: {type(msg).__name__}")
            response = _parse_response(msg)
            if response["message_type"] == SERVER_ERROR_RESPONSE:
                raise RuntimeError(
                    f"火山 ASR 服务端错误 code={response.get('code')}: "
                    f"{response.get('payload_msg')}"
                )
            payload = response.get("payload_msg") or {}
            if payload:
                final.update(payload)
            if response["is_last_package"]:
                break
        return final

    @staticmethod
    def _chunk_to_pcm(chunk: np.ndarray) -> bytes:
        return (np.clip(chunk, -1.0, 1.0) * 32767).astype("<i2").tobytes()

    async def _send_audio_packet(self, ws: Any, pcm: bytes, *, is_last: bool) -> None:
        self._seq += 1
        await ws.send(_make_audio_request(self._seq, pcm, is_last=is_last))

    async def _buffer_and_send(self, chunk: np.ndarray) -> None:
        ws = self._ws
        if ws is None:
            return

        self._pending_pcm.extend(self._chunk_to_pcm(chunk))
        # 未知哪个包是末包，因此始终保留不超过 200ms，结束时给它打末包标记。
        while len(self._pending_pcm) > _BYTES_PER_CHUNK:
            pcm = bytes(self._pending_pcm[:_BYTES_PER_CHUNK])
            del self._pending_pcm[:_BYTES_PER_CHUNK]
            await self._send_audio_packet(ws, pcm, is_last=False)

    async def get_chunk(self, chunk: np.ndarray) -> np.ndarray:
        if self.is_streaming:
            await self._buffer_and_send(chunk)
        return chunk

    async def _close_session(self, ws: Any) -> None:
        receiver_task = self._receiver_task
        if receiver_task is not None and not receiver_task.done():
            receiver_task.cancel()
            await asyncio.gather(receiver_task, return_exceptions=True)
        await ws.close()

        if self._ws is ws:
            self._ws = None
            self._receiver_task = None
            self._seq = 0
            self._pending_pcm.clear()

    async def _finish_stream(self) -> dict:
        ws = self._ws
        receiver_task = self._receiver_task
        if ws is None or receiver_task is None:
            return {}

        generation = self._generation
        final: dict = {}
        try:
            await self._send_audio_packet(ws, bytes(self._pending_pcm), is_last=True)
            self._pending_pcm.clear()
            try:
                final = await asyncio.wait_for(receiver_task, timeout=_TIMEOUT_S)
            except asyncio.CancelledError:
                # reset() 会取消接收任务；只有当前 generate_text 自身被取消时才继续抛出。
                if generation == self._generation:
                    raise
        finally:
            await self._close_session(ws)

        if generation != self._generation:
            return {}
        return final

    @staticmethod
    def _extract_speaker_id(final: dict) -> str:
        utterances = (final.get("result") or {}).get("utterances") or []
        if not utterances:
            return ""
        additions = utterances[0].get("additions") or {}
        # 文档字段为 speaker；部分版本也可能返回 speaker_id
        speaker_id = str(additions.get("speaker_id") or additions.get("speaker") or "")
        return speaker_id

    def _append_result(self, final: dict) -> None:
        text = (final.get("result") or {}).get("text") or ""
        if text:
            self._completed_texts.append(text)
        speaker_id = self._extract_speaker_id(final)
        if speaker_id:
            self._last_speaker_id = speaker_id

    async def generate_text(self, chunks: list[np.ndarray]) -> tuple[str, float, str]:
        """结束实时会话并返回本轮累计文本与 speaker_id；无会话时兼容原批量调用。"""
        self._check_credentials()
        if self.is_streaming:
            t0 = time.perf_counter()
            final = await self._finish_stream()
            self._append_result(final)
            self._last_elapsed = time.perf_counter() - t0
        elif not self._streaming_turn_started:
            t0 = time.perf_counter()
            full_wav = np.concatenate(chunks)
            pcm = self._chunk_to_pcm(full_wav)
            final = await asyncio.wait_for(_recognize(pcm, self._corpus()), timeout=_TIMEOUT_S)
            text = (final.get("result") or {}).get("text") or ""
            speaker_id = self._extract_speaker_id(final)
            return text, time.perf_counter() - t0, speaker_id

        return "".join(self._completed_texts), self._last_elapsed, self._last_speaker_id
