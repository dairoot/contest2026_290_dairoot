"""板载麦克风版的实时说话人识别 demo。

与 tests/asr_ws 的区别只有音频来源：那边由浏览器采集后上行，这边直接在
板子上用 arecord 读 openvela AMP mic，浏览器只负责展示结果。

进程一启动就开始采集并持续识别，与有没有页面连着无关。结果按时间顺序
存进 results 列表：新页面连上时先补发历史，之后实时广播。
"""

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import sys
from urllib.parse import quote, unquote

import numpy as np
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

# 仓库根目录加入 sys.path，便于直接 `python tests/asr_wsv2/server.py` 运行
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from asr_client import AsrClient

logger = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8088
WS_PATH = "/ws"
AUDIO_URL_PREFIX = "/audio/"
AUDIO_DIR = os.path.join(ROOT, "audio_logs")  # AsrClient 默认的录音落盘目录

# card 3 "openvela AMP mic"，即 hw:3,0。用名字而不是编号，避免声卡重新编号后录错设备。
# 该设备只支持 S16_LE / 单声道 / 16000 Hz，正好是 AsrClient 需要的格式，不用重采样。
MIC_DEVICE = os.environ.get("MIC_DEVICE", "hw:rpmsgmic,0")
SAMPLE_RATE = 16000
BLOCK_BYTES = 3840 * 2  # 240 ms 的 S16_LE 单声道

results: list[dict] = []  # 所有识别结果，按时间顺序
clients: set = set()
capture_error: str | None = None


def audio_path_to_url(audio_path):
    """把本地录音路径映射成 /audio/ 下的 URL，不向前端暴露磁盘路径。"""
    if not audio_path:
        return None
    relative_path = os.path.relpath(audio_path, AUDIO_DIR)
    if relative_path.startswith(".."):
        return None
    return AUDIO_URL_PREFIX + quote(relative_path.replace(os.sep, "/"))


def process_request(connection, request):
    if request.path == WS_PATH:
        return None

    if request.path.startswith(AUDIO_URL_PREFIX):
        root = AUDIO_DIR
        path = unquote(request.path[len(AUDIO_URL_PREFIX) :])
    else:
        root = HERE
        path = "/index.html" if request.path in ("/", "") else request.path
    file_path = os.path.normpath(os.path.join(root, path.lstrip("/")))
    if not file_path.startswith(root + os.sep) or not os.path.isfile(file_path):
        return Response(404, "Not Found", Headers([("Content-Length", "0")]), b"")

    with open(file_path, "rb") as f:
        body = f.read()
    mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    headers = Headers(
        [("Content-Type", mime), ("Content-Length", str(len(body)))]
    )
    return Response(200, "OK", headers, body)


async def broadcast(message: str) -> None:
    for ws in list(clients):
        with contextlib.suppress(websockets.ConnectionClosed):
            await ws.send(message)


async def capture_loop() -> None:
    """从麦克风持续读音频喂给 AsrClient，把识别结果广播出去。"""
    asr_client = AsrClient()
    asr_client._loop = asyncio.get_running_loop()

    @asr_client.on("stt")
    async def handle_stt(payload):
        speaker_name, score = asr_client.speaker.get_speaker_name(
            payload["embedding"], speech_ms=payload.get("speech_ms")
        )

        logger.info("speaker=%s score=%s content=%s", speaker_name, score, payload["content"])
        # 先入列表再广播，中间没有 await：新页面补发的历史和实时广播因此不会重复也不会漏。
        results.append(
            {
                "speaker": speaker_name,
                "content": payload["content"],
                "elapsed": payload["elapsed"],
                "audio_url": audio_path_to_url(payload.get("audio_path")),
            }
        )
        await broadcast(json.dumps(results[-1], ensure_ascii=False))

    proc = await asyncio.create_subprocess_exec(
        "arecord", "-D", MIC_DEVICE, "-f", "S16_LE", "-r", str(SAMPLE_RATE),
        "-c", "1", "-t", "raw", "-q", "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    task = asyncio.create_task(asr_client.process_audio_chunk())
    try:
        while True:
            buf = await proc.stdout.readexactly(BLOCK_BYTES)
            chunk = np.frombuffer(buf, dtype="<i2").astype(np.float32) / 32768.0
            asr_client.enqueue_audio_chunk(chunk)
    except asyncio.IncompleteReadError:
        # arecord 退出了：设备被占用、设备名写错等，都会走到这里。
        global capture_error
        reason = (await proc.stderr.read()).decode(errors="replace").strip()
        capture_error = f"麦克风打开失败: {reason}"
        logger.error("arecord 退出: %s", reason or "无输出")
        await broadcast(json.dumps({"type": "error", "message": capture_error}, ensure_ascii=False))
    finally:
        task.cancel()
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        asr_client.clear()


async def handle_connection(ws) -> None:
    clients.add(ws)
    history = list(results)  # 与上面配对：这两行之间没有 await
    try:
        for item in history:
            await ws.send(json.dumps(item, ensure_ascii=False))
        if capture_error:
            await ws.send(json.dumps({"type": "error", "message": capture_error}, ensure_ascii=False))
        async for _ in ws:  # 页面不上行任何数据，这里只是等它断开
            pass
    except websockets.ConnectionClosed:
        pass
    finally:
        clients.discard(ws)


async def main() -> None:
    async with websockets.serve(
        handle_connection, "0.0.0.0", PORT, process_request=process_request
    ):
        logger.info("HTTP  http://localhost:%d/", PORT)
        logger.info("WS    ws://localhost:%d%s", PORT, WS_PATH)
        logger.info("MIC   %s", MIC_DEVICE)
        # 采集与页面无关：进程起来就一直录、一直识别。
        asyncio.create_task(capture_loop())
        await asyncio.Future()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    asyncio.run(main())
