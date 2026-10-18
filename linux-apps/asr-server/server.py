"""WebSocket transport for :class:`asr_client.AsrClient`.

Clients send raw 16 kHz mono Float32LE PCM in binary WebSocket messages.  The
transport validates and forwards each message as it arrives; ``AsrClient``
normalizes arbitrary message boundaries into 240 ms processing blocks.  Every
``stt`` event is returned as a JSON text message.
STT messages include a preset name or connection-local anonymous speaker label. The separate
/embedding endpoint extracts only an embedding from one complete audio clip.
Clients can set connection-local speaker profiles; persistent storage stays on the client.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import numpy as np
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

from config import ASR_MODEL_TYPE
from voiceprint_store import VoiceprintStore, MAX_SAMPLES_PER_NAME

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8086
WS_PATH = "/ws"
EMBEDDING_WS_PATH = "/embedding"
AUDIO_URL_PREFIX = "/audio/"
DEFAULT_AUDIO_SAVE_DIR = Path(__file__).resolve().parent / "audio_logs"
SAMPLE_BYTES = np.dtype("<f4").itemsize
SAMPLE_RATE = 16000
MIN_EMBEDDING_SAMPLES = SAMPLE_RATE * 1500 // 1000
MAX_EMBEDDING_SAMPLES = SAMPLE_RATE * 3
MAX_PRESET_SAMPLES = 1000
MAX_PRESET_DIMENSION = 4096

AsrClientFactory = Callable[..., Any]


def _request_path(websocket: Any) -> str:
    """Read the request path across websockets 12+ server APIs."""
    request = getattr(websocket, "request", None)
    raw_path = getattr(request, "path", None) or getattr(websocket, "path", "")
    return urlparse(raw_path).path


def _json_value(value: Any) -> Any:
    """Convert NumPy values from an ``stt`` event into JSON values."""
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _audio_root(audio_save_dir: str) -> Path:
    root = Path(audio_save_dir).expanduser() if audio_save_dir else DEFAULT_AUDIO_SAVE_DIR
    return root.resolve()


def audio_path_to_url(audio_path: str | None, audio_save_dir: str = "") -> str | None:
    """Map a saved recording to its HTTP URL without exposing local paths."""
    if not audio_path:
        return None

    path = Path(audio_path).expanduser().resolve()
    root = _audio_root(audio_save_dir)
    try:
        relative_path = path.relative_to(root)
    except ValueError:
        logger.warning("audio path is outside audio_save_dir: %s", path)
        return None

    return AUDIO_URL_PREFIX + quote(relative_path.as_posix(), safe="/")


def serialize_stt(payload: dict[str, Any], *, audio_save_dir: str = "") -> str:
    """Serialize an ``AsrClient`` event and replace its local audio path."""
    message = dict(payload)
    audio_path = message.pop("audio_path", None)
    message["audio_url"] = audio_path_to_url(audio_path, audio_save_dir)
    message["type"] = "stt"
    return json.dumps(_json_value(message), ensure_ascii=False, allow_nan=False)


def _http_response(
    status_code: int,
    reason: str,
    body: bytes = b"",
    *,
    content_type: str = "text/plain; charset=utf-8",
) -> Response:
    headers = Headers(
        [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("Access-Control-Allow-Origin", "*"),
            ("Connection", "close"),
        ]
    )
    return Response(status_code, reason, headers, body)


def create_http_handler(audio_save_dir: str = "") -> Callable[[Any, Any], Response | None]:
    """Create a websockets ``process_request`` handler for saved recordings."""
    root = _audio_root(audio_save_dir)

    def process_request(connection: Any, request: Any) -> Response | None:
        request_path = urlparse(request.path).path
        if request_path in (WS_PATH, EMBEDDING_WS_PATH):
            return None
        if not request_path.startswith(AUDIO_URL_PREFIX):
            return _http_response(404, "Not Found")

        relative_path = unquote(request_path[len(AUDIO_URL_PREFIX) :])
        if not relative_path:
            return _http_response(404, "Not Found")

        file_path = (root / relative_path).resolve()
        try:
            file_path.relative_to(root)
        except ValueError:
            return _http_response(403, "Forbidden")
        if file_path.suffix.lower() != ".wav" or not file_path.is_file():
            return _http_response(404, "Not Found")

        body = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        return _http_response(200, "OK", body, content_type=content_type)

    return process_request


def _create_asr_client(
    *,
    model_type: str,
    slow_reply_silence_duration_ms: int,
    audio_save_dir: str,
) -> Any:
    # 收到首个有效音频帧后再初始化模型。
    from asr_client import AsrClient

    return AsrClient(
        model_type=model_type,
        slow_reply_silence_duration_ms=slow_reply_silence_duration_ms,
        audio_save_dir=audio_save_dir,
    )


def _create_speaker_worker() -> Any:
    from speaker import SpeakerWorker

    return SpeakerWorker()


def _decode_audio(message: Any) -> tuple[np.ndarray | None, dict[str, str] | None]:
    if not isinstance(message, (bytes, bytearray, memoryview)):
        code, reason = "binary_audio_required", "send audio as binary Float32LE PCM"
    elif len(message) % SAMPLE_BYTES:
        code, reason = "invalid_audio_size", "binary audio length must be a multiple of 4 bytes"
    else:
        chunk = np.frombuffer(message, dtype="<f4").astype(np.float32, copy=True)
        if np.isfinite(chunk).all():
            np.clip(chunk, -1.0, 1.0, out=chunk)
            return chunk, None
        code, reason = "invalid_audio_value", "audio samples must be finite float32 values"
    return None, {"type": "error", "code": code, "message": reason}


def validate_speaker_profiles(profiles: Any) -> list[dict]:
    """Validate the entire replacement before changing any connection state."""
    if not isinstance(profiles, list) or len(profiles) > MAX_PRESET_SAMPLES:
        raise ValueError(f"profiles must be a list with at most {MAX_PRESET_SAMPLES} samples")
    samples, counts, dimension = [], {}, None
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError("each profile must contain name and embedding")
        name = VoiceprintStore.validate_name(profile.get("name"))
        vector = VoiceprintStore.prepare_embedding(profile.get("embedding"))
        if vector.size > MAX_PRESET_DIMENSION:
            raise ValueError(f"embedding dimension must not exceed {MAX_PRESET_DIMENSION}")
        if dimension is not None and vector.size != dimension:
            raise ValueError("all preset embeddings must have the same dimension")
        dimension = vector.size
        counts[name] = counts.get(name, 0) + 1
        if counts[name] > MAX_SAMPLES_PER_NAME:
            raise ValueError(f"each name supports at most {MAX_SAMPLES_PER_NAME} samples")
        samples.append({"name": name, "embedding": vector})
    return samples


async def handle_embedding_connection(
    websocket: Any, *, worker_factory: Callable[[], Any] | None = None,
) -> None:
    """One binary message is a complete clip; return its speaker embedding only."""
    if _request_path(websocket) != EMBEDDING_WS_PATH:
        await websocket.close(code=1008, reason=f"WebSocket path must be {EMBEDDING_WS_PATH}")
        return
    worker = None
    try:
        async for message in websocket:
            audio, error = _decode_audio(message)
            if error is None and audio.size < MIN_EMBEDDING_SAMPLES:
                error = {
                    "type": "error", "code": "audio_too_short",
                    "message": "send at least 1.5 seconds of 16 kHz mono Float32LE PCM in one message",
                }
            if error is not None:
                await websocket.send(json.dumps(error))
                continue
            try:
                if worker is None:
                    worker = (worker_factory or _create_speaker_worker)()
                # 与 AsrClient 的声纹输入一致，最多使用开头 3 秒。
                embedding = np.asarray(
                    await worker.generate_embedding(audio[:MAX_EMBEDDING_SAMPLES]), dtype=np.float32,
                )
                if embedding.ndim != 1 or not embedding.size or not np.isfinite(embedding).all():
                    raise ValueError("speaker model returned an invalid embedding")
                response = {"embedding": embedding.tolist()}
            except Exception as exc:
                logger.exception("Speaker embedding extraction failed")
                response = {"type": "error", "code": "embedding_failed", "message": str(exc)}
            await websocket.send(json.dumps(response, ensure_ascii=False, allow_nan=False))
    except websockets.ConnectionClosed:
        pass


async def handle_connection(
    websocket: Any,
    *,
    model_type: str = ASR_MODEL_TYPE,
    slow_reply_silence_duration_ms: int = 960,
    audio_save_dir: str = "",
    client_factory: AsrClientFactory | None = None,
) -> None:
    """Serve one WebSocket connection with an isolated ``AsrClient``."""
    if _request_path(websocket) != WS_PATH:
        await websocket.close(code=1008, reason=f"WebSocket path must be {WS_PATH}")
        return

    factory = client_factory or _create_asr_client
    asr_client = None
    preset_speakers = []
    processor_task = None
    remote_address = getattr(websocket, "remote_address", None)
    logger.info("ASR WebSocket connected: %s", remote_address)

    async def send_stt(payload: dict[str, Any]) -> None:
        speaker_name, _ = asr_client.speaker.get_speaker_name(
            payload["embedding"], speech_ms=payload.get("speech_ms"),
            preset_speakers=preset_speakers,
        )
        await websocket.send(serialize_stt(
            {**payload, "speaker": speaker_name}, audio_save_dir=audio_save_dir,
        ))

    try:
        async for message in websocket:
            if isinstance(message, str):
                try:
                    command = json.loads(message)
                except ValueError:
                    command = None
                if isinstance(command, dict) and command.get("type") == "set_speaker_profiles":
                    try:
                        replacement = validate_speaker_profiles(command.get("profiles"))
                    except ValueError as exc:
                        response = {"type": "error", "code": "invalid_speaker_profiles", "message": str(exc)}
                    else:
                        preset_speakers = replacement
                        response = {
                            "type": "speaker_profiles_set", "ok": True,
                            "speakers": len({item["name"] for item in replacement}), "samples": len(replacement),
                        }
                    await websocket.send(json.dumps(response, ensure_ascii=False))
                    continue
            chunk, error = _decode_audio(message)
            if error is not None:
                await websocket.send(json.dumps(error))
                continue
            if asr_client is None:
                asr_client = factory(
                    model_type=model_type,
                    slow_reply_silence_duration_ms=slow_reply_silence_duration_ms,
                    audio_save_dir=audio_save_dir,
                )
                asr_client._loop = asyncio.get_running_loop()
                asr_client.on("stt")(send_stt)
                processor_task = asyncio.create_task(
                    asr_client.process_audio_chunk(),
                    name="asr-websocket-audio-processor",
                )
            asr_client.enqueue_audio_chunk(chunk, end_ts_ms=time.time() * 1000)
    except websockets.ConnectionClosed:
        pass
    finally:
        if processor_task is not None:
            processor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await processor_task
        # VolcAsrWorker.reset() also closes an in-flight upstream session.
        if asr_client is not None:
            asr_client.clear()
        logger.info("ASR WebSocket disconnected: %s", remote_address)


async def serve(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    model_type: str = ASR_MODEL_TYPE,
    slow_reply_silence_duration_ms: int = 960,
    audio_save_dir: str = "",
    max_frame_bytes: int = 1024 * 1024,
) -> None:
    """Run the ASR WebSocket service until it is cancelled."""

    async def handler(websocket: Any) -> None:
        try:
            if _request_path(websocket) == EMBEDDING_WS_PATH:
                await handle_embedding_connection(websocket)
                return
            await handle_connection(
                websocket,
                model_type=model_type,
                slow_reply_silence_duration_ms=slow_reply_silence_duration_ms,
                audio_save_dir=audio_save_dir,
            )
        except Exception:
            logger.exception("ASR WebSocket connection failed")
            with contextlib.suppress(websockets.ConnectionClosed):
                await websocket.close(code=1011, reason="ASR server error")

    async with websockets.serve(
        handler,
        host,
        port,
        max_size=max_frame_bytes,
        process_request=create_http_handler(audio_save_dir),
    ):
        logger.info("ASR WebSocket listening on ws://%s:%d%s", host, port, WS_PATH)
        logger.info("Speaker embedding available on ws://%s:%d%s", host, port, EMBEDDING_WS_PATH)
        logger.info(
            "ASR recordings available on http://%s:%d%s",
            host,
            port,
            AUDIO_URL_PREFIX,
        )
        await asyncio.Future()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def main() -> None:
    configure_logging()
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
