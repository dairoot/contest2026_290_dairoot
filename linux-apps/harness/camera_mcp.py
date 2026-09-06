"""摄像头画面理解的 stdio MCP server：从 camera-server 的 MJPEG 流截一帧，交给 OpenAI 格式的视觉模型描述。

配到 mcpServers 里即可（cwd 和 PATH 跟着 harness 走）：
    {"camera": {"command": "python", "args": ["camera_mcp.py"]}}
"""

import base64
import os
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

SOI, EOI = b"\xff\xd8", b"\xff\xd9"  # JPEG 起止标记
MAX_FRAME_BYTES = 8 * 1024 * 1024  # 一直读不到 EOI 时的兜底上限

mcp = FastMCP("camera")


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
    if not os.getenv("OPENAI_API_KEY"):
        return {"isError": True, "error": "没有 OPENAI_API_KEY，视觉模型调不了"}

    try:
        jpeg = grab_frame()
    except Exception as e:
        return {"isError": True, "error": f"取帧失败（{e}），确认 camera-server 的 {VIDEO_FEED_URL} 在跑"}

    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
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
        )
    except Exception as e:
        return {"isError": True, "error": f"视觉模型（{MODEL}）调用失败：{e}"}

    return {"isError": False, "description": completion.choices[0].message.content}


if __name__ == "__main__":
    mcp.run()
