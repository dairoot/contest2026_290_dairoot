"""WebSocket transport for :class:`asr_client.AsrClient`.

Clients send raw 16 kHz mono Float32LE PCM in binary WebSocket messages.  The
transport validates and forwards each message as it arrives; ``AsrClient``
normalizes arbitrary message boundaries into 240 ms processing blocks.  Every
``stt`` event is returned as a JSON text message.
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

from asr_client import AsrClient

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8086
WS_PATH = "/ws"
AUDIO_URL_PREFIX = "/audio/"
DEFAULT_AUDIO_SAVE_DIR = Path(__file__).resolve().parent / "audio_logs"
SAMPLE_BYTES = np.dtype("<f4").itemsize

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
        if request_path == WS_PATH:
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
    return AsrClient(
        model_type=model_type,
        slow_reply_silence_duration_ms=slow_reply_silence_duration_ms,
        audio_save_dir=audio_save_dir,
    )


async def handle_connection(
    websocket: Any,
    *,
    model_type: str = "volc",
    slow_reply_silence_duration_ms: int = 960,
    audio_save_dir: str = "",
    client_factory: AsrClientFactory | None = None,
) -> None:
    """Serve one WebSocket connection with an isolated ``AsrClient``."""
    if _request_path(websocket) != WS_PATH:
        await websocket.close(code=1008, reason=f"WebSocket path must be {WS_PATH}")
        return

    factory = client_factory or _create_asr_client
    asr_client = factory(
        model_type=model_type,
        slow_reply_silence_duration_ms=slow_reply_silence_duration_ms,
        audio_save_dir=audio_save_dir,
    )
    asr_client._loop = asyncio.get_running_loop()
    remote_address = getattr(websocket, "remote_address", None)
    logger.info("ASR WebSocket connected: %s", remote_address)

    @asr_client.on("stt")
    async def send_stt(payload: dict[str, Any]) -> None:
        await websocket.send(serialize_stt(payload, audio_save_dir=audio_save_dir))

    processor_task = asyncio.create_task(
        asr_client.process_audio_chunk(),
        name="asr-websocket-audio-processor",
    )
    try:
        async for message in websocket:
            if not isinstance(message, (bytes, bytearray, memoryview)):
                await websocket.send(
                    json.dumps(
                        {
                            "type": "error",
                            "code": "binary_audio_required",
                            "message": "send audio as binary Float32LE PCM",
                        },
                        ensure_ascii=False,
                    )
                )
                continue

            if len(message) % SAMPLE_BYTES:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "error",
                            "code": "invalid_audio_size",
                            "message": "binary audio length must be a multiple of 4 bytes",
                        },
                        ensure_ascii=False,
                    )
                )
                continue

            chunk = np.frombuffer(message, dtype="<f4").astype(np.float32, copy=True)
            if not np.isfinite(chunk).all():
                await websocket.send(
                    json.dumps(
                        {
                            "type": "error",
                            "code": "invalid_audio_value",
                            "message": "audio samples must be finite float32 values",
                        },
                        ensure_ascii=False,
                    )
                )
                continue

            # Slight overshoots are common after resampling and shouldn't
            # destabilize VAD or speaker embedding inference.
            np.clip(chunk, -1.0, 1.0, out=chunk)
            asr_client.enqueue_audio_chunk(chunk, end_ts_ms=time.time() * 1000)
    except websockets.ConnectionClosed:
        pass
    finally:
        processor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await processor_task
        # VolcAsrWorker.reset() also closes an in-flight upstream session.
        asr_client.clear()
        logger.info("ASR WebSocket disconnected: %s", remote_address)


async def serve(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    model_type: str = "sense_voice",
    slow_reply_silence_duration_ms: int = 960,
    audio_save_dir: str = "",
    max_frame_bytes: int = 1024 * 1024,
) -> None:
    """Run the ASR WebSocket service until it is cancelled."""

    async def handler(websocket: Any) -> None:
        try:
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
