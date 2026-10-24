"""服务端麦克风版的实时说话人识别 demo。

Linux 板子上用 arecord 读 openvela AMP mic，Mac 上用 sounddevice 读本机
麦克风。浏览器展示结果，也可选择某条消息录入具名声纹，不上传音频。

进程一启动就开始采集并持续识别，与有没有页面连着无关。结果按时间顺序
存进 results 列表：新页面连上时先补发历史，之后实时广播。
"""

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import shutil
import sys
import uuid
from urllib.parse import quote, unquote

import numpy as np
import websockets
from dotenv import load_dotenv
from websockets.datastructures import Headers
from websockets.http11 import Response

# 仓库根目录加入 sys.path，便于直接 `python tests/asr_wsv2/server.py` 运行
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

# AsrClient 改为懒加载后，仍需在读取 MIC_DEVICE 前加载 .env。
load_dotenv()

from voiceprint_store import MIN_ENROLLMENT_SPEECH_MS, VoiceprintStore, default_profile_path

logger = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8088
WS_PATH = "/ws"
AUDIO_URL_PREFIX = "/audio/"
AUDIO_DIR = os.path.join(ROOT, "audio_logs")  # AsrClient 默认的录音落盘目录

MIC_BACKEND = "sounddevice" if sys.platform == "darwin" else "arecord"
# Mac 留空表示系统默认输入；Linux 用稳定的 ALSA 名称，避免声卡重新编号后录错设备。
MIC_DEVICE = os.environ.get("MIC_DEVICE", "" if MIC_BACKEND == "sounddevice" else "hw:rpmsgmic,0")
SAMPLE_RATE = 16000
BLOCK_SAMPLES = 3840  # 240 ms
BLOCK_BYTES = BLOCK_SAMPLES * 2  # arecord 的 S16_LE 单声道

results: list[dict] = []  # 所有识别结果，按时间顺序
clients: set = set()
capture_error: str | None = None
result_embeddings: dict[str, np.ndarray] = {}
voiceprints = VoiceprintStore(default_profile_path())
voiceprint_error: str | None = None


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


async def report_capture_error(message: str) -> None:
    global capture_error
    capture_error = message
    logger.error("%s", message)
    await broadcast(json.dumps({"type": "error", "message": message}, ensure_ascii=False))


def refresh_voiceprints():
    global voiceprint_error
    before = (voiceprints.summary(), voiceprint_error)
    try:
        voiceprints.load()
    except (OSError, ValueError) as exc:
        voiceprint_error = f"无法加载声纹文件，已停用录入以保留原文件: {exc}"
    else:
        voiceprint_error = None
    return before != (voiceprints.summary(), voiceprint_error)


def voiceprint_message(**extra):
    refresh_voiceprints()
    return json.dumps(
        {"type": "voiceprints", "speakers": voiceprints.summary(), "error": voiceprint_error, **extra},
        ensure_ascii=False,
    )


async def handle_enrollment(ws, message) -> None:
    result_id = None
    try:
        if not isinstance(message, str):
            raise ValueError("声纹录入只接受文本 JSON")
        request = json.loads(message)
        if not isinstance(request, dict) or request.get("type") != "enroll":
            raise ValueError("不支持的操作")
        requested_id = request.get("result_id")
        result_id = requested_id if isinstance(requested_id, str) else None
        refresh_voiceprints()
        if voiceprint_error:
            raise ValueError(voiceprint_error)
        if result_id not in result_embeddings:
            raise ValueError("这条消息没有可用声纹，请选择有效语音不少于 1 秒的消息")
        name = voiceprints.validate_name(request.get("name"))
        added = voiceprints.add(name, result_embeddings[result_id])
    except (ValueError, OSError) as exc:
        await ws.send(json.dumps(
            {"type": "enroll_result", "ok": False, "result_id": result_id, "message": str(exc)},
            ensure_ascii=False,
        ))
        return

    for item in results:
        if item["id"] == result_id:
            item["speaker"] = name
            break
    await broadcast(voiceprint_message(result_id=result_id, name=name))
    await ws.send(json.dumps(
        {
            "type": "enroll_result", "ok": True, "result_id": result_id, "name": name,
            "message": f"已录入“{name}”的声纹" if added else f"这条声纹已录入“{name}”，无需重复添加",
        },
        ensure_ascii=False,
    ))


async def handle_stt(asr_client, payload) -> None:
    profiles_changed = refresh_voiceprints()
    try:
        embedding = voiceprints.prepare_embedding(payload["embedding"])
    except ValueError:
        embedding = None
    can_enroll = bool(embedding is not None and (payload.get("speech_ms") or 0) >= MIN_ENROLLMENT_SPEECH_MS)
    speaker_name, score = None, -1.0
    if can_enroll and not voiceprint_error:
        candidates = [sample for sample in voiceprints.samples if sample["embedding"].size == embedding.size]
        if candidates:
            speaker_name, score = asr_client.speaker.classify(embedding, candidates)
    if speaker_name is None:
        if embedding is None:
            speaker_name = "unknown"
        else:
            speaker_name, score = asr_client.speaker.get_speaker_name(
                embedding, speech_ms=payload["speech_ms"]
            )

    logger.info("speaker=%s score=%s content=%s", speaker_name, score, payload["content"])
    result_id = uuid.uuid4().hex
    if can_enroll:
        result_embeddings[result_id] = embedding
    # 私有声纹留在服务端，消息只带一个用于录入的 ID。
    item = {
        "id": result_id, "speaker": speaker_name, "content": payload["content"],
        "elapsed": payload["elapsed"], "audio_url": audio_path_to_url(payload.get("audio_path")),
        "can_enroll": can_enroll,
    }
    # 先入列表再广播，中间没有 await，保证历史补发和实时消息连续。
    results.append(item)
    await broadcast(json.dumps(item, ensure_ascii=False))
    if profiles_changed:
        await broadcast(voiceprint_message())


async def capture_arecord(asr_client) -> None:
    """Linux：读取板载 ALSA 麦克风，转成 float32 单声道。"""
    proc = await asyncio.create_subprocess_exec(
        "arecord", "-D", MIC_DEVICE, "-f", "S16_LE", "-r", str(SAMPLE_RATE),
        "-c", "1", "-t", "raw", "-q", "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        while True:
            buf = await proc.stdout.readexactly(BLOCK_BYTES)
            chunk = np.frombuffer(buf, dtype="<i2").astype(np.float32) / 32768.0
            asr_client.enqueue_audio_chunk(chunk)
    except asyncio.IncompleteReadError:
        reason = (await proc.stderr.read()).decode(errors="replace").strip()
        raise RuntimeError(f"arecord 已退出: {reason or '无错误输出'}") from None
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        await proc.wait()


async def capture_sounddevice(asr_client, sd) -> None:
    """macOS：PortAudio 回调通过 AsrClient 将本机麦克风音频送回事件循环。"""
    try:
        device = int(MIC_DEVICE)
    except ValueError:
        device = MIC_DEVICE or None

    loop = asyncio.get_running_loop()
    finished = asyncio.Event()

    def callback(indata, frames, time_info, status):
        if status:
            logger.warning("麦克风采集状态: %s", status)
        asr_client.audio_callback(indata, frames, time_info, status)

    def on_finished():
        # PortAudio 的 void 回调必须返回 None，不能直接返回 call_soon_threadsafe 的 Handle。
        loop.call_soon_threadsafe(finished.set)

    try:
        stream = sd.InputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=BLOCK_SAMPLES,
            callback=callback,
            finished_callback=on_finished,
        )
        try:
            stream.start()
            logger.info("MIC   %s", sd.query_devices(stream.device, "input")["name"])
            await finished.wait()
            raise RuntimeError("本机麦克风音频流意外停止")
        finally:
            # 即使 start() 因权限等原因失败，也要释放已创建的流。
            stream.close()
    except (sd.PortAudioError, ValueError) as exc:
        raise RuntimeError(
            f"无法打开本机麦克风: {exc}。请检查系统输入设备和 MIC_DEVICE；"
            "若未授权，请在 macOS 系统设置 → 隐私与安全性 → 麦克风中允许启动服务的终端或应用访问。"
        ) from exc


async def capture_loop() -> None:
    """从麦克风持续读音频喂给 AsrClient，把识别结果广播出去。"""
    sd = None
    if MIC_BACKEND == "sounddevice":
        try:
            import sounddevice as sd
        except (ImportError, OSError) as exc:
            await report_capture_error(f"无法加载本机麦克风后端: {exc}；请在 asr-server 目录执行 uv sync。")
            return
    elif shutil.which("arecord") is None:
        await report_capture_error(
            "未找到 arecord：Linux 板载麦克风采集需要 ALSA 工具，请安装 alsa-utils 并检查 PATH。"
        )
        return

    # 确认采集后端可用后再加载模型。
    from asr_client import AsrClient

    asr_client = AsrClient()
    asr_client._loop = asyncio.get_running_loop()

    @asr_client.on("stt")
    async def on_stt(payload):
        await handle_stt(asr_client, payload)

    task = asyncio.create_task(asr_client.process_audio_chunk())
    try:
        if sd is not None:
            await capture_sounddevice(asr_client, sd)
        else:
            await capture_arecord(asr_client)
    except Exception as exc:
        await report_capture_error(f"麦克风采集失败（{MIC_BACKEND}）: {exc}")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        asr_client.clear()


async def handle_connection(ws) -> None:
    clients.add(ws)
    history = list(results)  # 与上面配对：这两行之间没有 await
    try:
        for item in history:
            await ws.send(json.dumps(item, ensure_ascii=False))
        await ws.send(voiceprint_message())
        if capture_error:
            await ws.send(json.dumps({"type": "error", "message": capture_error}, ensure_ascii=False))
        async for message in ws:  # 音频仍在服务端采集；页面仅发送声纹录入命令。
            await handle_enrollment(ws, message)
    except websockets.ConnectionClosed:
        pass
    finally:
        clients.discard(ws)


async def main() -> None:
    refresh_voiceprints()
    if voiceprint_error:
        logger.error("%s", voiceprint_error)
    async with websockets.serve(
        handle_connection, "0.0.0.0", PORT, process_request=process_request
    ):
        logger.info("HTTP  http://localhost:%d/", PORT)
        logger.info("WS    ws://localhost:%d%s", PORT, WS_PATH)
        logger.info("MIC   %s (%s)", MIC_DEVICE or "系统默认输入", MIC_BACKEND)
        # 采集与页面无关：进程起来就一直录、一直识别。
        await capture_loop()
        # 采集失败后保留观察窗，让当前与后连上的页面都能看到失败原因。
        await asyncio.Future()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
