import asyncio
import json
import logging
import mimetypes
import os
import sys

import numpy as np
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

# 仓库根目录加入 sys.path，便于直接 `python tests/asr_ws/server.py` 运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from asr_client import AsrClient

logger = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8086
WS_PATH = "/ws"



def process_request(connection, request):
    if request.path == WS_PATH:
        return None

    path = "/index.html" if request.path in ("/", "") else request.path
    file_path = os.path.normpath(os.path.join(HERE, path.lstrip("/")))
    if not file_path.startswith(HERE) or not os.path.isfile(file_path):
        return Response(404, "Not Found", Headers([("Content-Length", "0")]), b"")

    with open(file_path, "rb") as f:
        body = f.read()
    mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    headers = Headers(
        [("Content-Type", mime), ("Content-Length", str(len(body)))]
    )
    return Response(200, "OK", headers, body)


async def handle_connection(ws) -> None:
    asr_client = AsrClient()
    asr_client._loop = asyncio.get_running_loop()

    @asr_client.on("stt")
    async def handle_stt(payload):
        speaker_name, score = asr_client.speaker.get_speaker_name(
            payload["embedding"], speech_ms=payload.get("speech_ms")
        )

        logger.info("speaker=%s score=%s content=%s", speaker_name, score, payload["content"])
        await ws.send(
            json.dumps(
                {"speaker": speaker_name, "content": payload["content"], "elapsed": payload["elapsed"]},
                ensure_ascii=False,
            )
        )

    task = asyncio.create_task(asr_client.process_audio_chunk())
    try:
        async for message in ws:
            if isinstance(message, (bytes, bytearray)):
                chunk = np.frombuffer(message, dtype=np.float32).copy()
                asr_client.audio_queue.put_nowait(chunk)
    except websockets.ConnectionClosed:
        pass
    finally:
        task.cancel()


async def main() -> None:
    async with websockets.serve(
        handle_connection, "0.0.0.0", PORT, process_request=process_request
    ):
        logger.info("HTTP  http://localhost:%d/", PORT)
        logger.info("WS    ws://localhost:%d%s", PORT, WS_PATH)
        await asyncio.Future()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    asyncio.run(main())
