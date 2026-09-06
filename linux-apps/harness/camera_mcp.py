"""摄像头画面理解的 stdio MCP server：从 camera-server 的 MJPEG 流截一帧，交给 OpenAI 格式的视觉模型描述。

配到 mcpServers 里即可（cwd 和 PATH 跟着 harness 走）：
    {"camera": {"command": "python", "args": ["camera_mcp.py"]}}
"""

import base64
import logging
import os
import sys
import time
import urllib.request

from dotenv import load_dotenv
from fastmcp import FastMCP
from openai import OpenAI
from pydantic import Field

# stdio 子进程只继承 PATH/HOME 这几个白名单变量（mcp.client.stdio 的 DEFAULT_INHERITED_ENV_VARS），
# harness 进程里的 OPENAI_* 传不过来，所以自己读同目录的 .env。
load_dotenv()

VIDEO_FEED_URL = "http://localhost:8180/video_feed"
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
# 不能复用 harness 的 OPENAI_MODEL：deepseek-v4-flash 是纯文本的，图片会被丢掉，
# 它只会回「我无法查看图片」。带视觉的是 deepseek-v4-flash-vision-exp。
MODEL = os.getenv("OPENAI_VISION_MODEL", "deepseek-v4-flash-vision-exp")

# 这个视觉模型默认开思考：实测 1600 字推理换来慢 3 倍（9.7s vs 3.0s），描述画面用不上。
# 字段名各家不一样，跟 SDK 的 openai_client 保持一致按厂商分。
THINKING_OFF = {"thinking": {"type": "disabled"}} if "deepseek" in MODEL.lower() else {"enable_thinking": False}

SOI, EOI = b"\xff\xd8", b"\xff\xd9"  # JPEG 起止标记
MAX_FRAME_BYTES = 8 * 1024 * 1024  # 一直读不到 EOI 时的兜底上限

mcp = FastMCP("camera")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    # stdout 是 MCP 的 JSON-RPC 通道，日志只能往 stderr 写；harness 会把子进程的 stderr 透传出来
    stream=sys.stderr,
)
logger = logging.getLogger("camera")


def grab_frame(timeout: float = 10) -> bytes:
    """从 MJPEG 流里截一帧完整 JPEG。multipart 的分隔头不解析，直接找 SOI/EOI。"""
    with urllib.request.urlopen(VIDEO_FEED_URL, timeout=timeout) as resp:
        buf = b""
        while len(buf) < MAX_FRAME_BYTES:
            chunk = resp.read(8192)
            if not chunk:
                break
            buf += chunk
            start = buf.find(SOI)
            end = buf.find(EOI, start + 2) if start != -1 else -1
            if end != -1:
                return buf[start : end + 2]
    raise RuntimeError("流里没读到完整的 JPEG 帧")


@mcp.tool()
def view_camera(
    question: str = Field(default="画面里有什么？", description="What to ask about the current camera view"),
) -> dict:
    """Look at the live camera view right now and answer a question about it.
    Use when: user asks what the camera sees, who or what is in the room, or anything about the current scene.
    NOT for: recorded video, past footage, or images from anywhere other than this camera.
    Slow (grabbing a frame plus the vision model takes several seconds): ALWAYS say one short spoken
    line first, e.g. 「我看看啊」「我瞅一眼摄像头」, then call it — otherwise the user sits in silence
    and thinks it hung."""
    logger.info(f"收到提问：{question}")
    if not os.getenv("OPENAI_API_KEY"):
        logger.error("没有 OPENAI_API_KEY，视觉模型调不了")
        return {"isError": True, "error": "没有 OPENAI_API_KEY，视觉模型调不了"}

    t0 = time.perf_counter()
    try:
        jpeg = grab_frame()
    except Exception as e:
        logger.error(f"取帧失败：{e}（{VIDEO_FEED_URL}）")
        return {"isError": True, "error": f"取帧失败（{e}），确认 camera-server 的 {VIDEO_FEED_URL} 在跑"}
    logger.info(f"取到一帧 {len(jpeg) / 1024:.0f}KB，用时 {time.perf_counter() - t0:.1f}s")

    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    logger.info(f"视觉模型（{MODEL}）调用中...")
    t1 = time.perf_counter()
    try:
        completion = OpenAI(base_url=BASE_URL).chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            extra_body=THINKING_OFF,
        )
    except Exception as e:
        logger.error(f"视觉模型（{MODEL}）调用失败：{e}")
        return {"isError": True, "error": f"视觉模型（{MODEL}）调用失败：{e}"}

    description = completion.choices[0].message.content
    logger.info(f"视觉模型返回，用时 {time.perf_counter() - t1:.1f}s，{len(description)} 字：{description[:40]}…")
    return {"isError": False, "description": description}


if __name__ == "__main__":
    logger.info(f"camera server 启动：feed={VIDEO_FEED_URL}，模型={MODEL}，base_url={BASE_URL}")
    mcp.run()
