"""miloco 的 stdio MCP server：看摄像头画面 + 开关米家设备，两件事都走 miloco-server 的 HTTP 接口。

配到 mcpServers 里即可（cwd 和 PATH 跟着 harness 走）：
    {"米家": {"command": "python", "args": ["miloco_mcp.py"]}}
"""

import base64
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request

from dotenv import load_dotenv
from fastmcp import FastMCP
from openai import OpenAI
from pydantic import Field

# stdio 子进程只继承 PATH/HOME 这几个白名单变量（mcp.client.stdio 的 DEFAULT_INHERITED_ENV_VARS），
# harness 进程里的 OPENAI_* 传不过来，所以自己读同目录的 .env。
load_dotenv()

SERVER_URL = "http://localhost:8180"  # miloco-server，和 harness 同机
VIDEO_FEED_URL = f"{SERVER_URL}/video_feed"
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
# 不能复用 harness 的 OPENAI_MODEL：deepseek-v4-flash 是纯文本的，图片会被丢掉，
# 它只会回「我无法查看图片」。带视觉的是 deepseek-v4-flash-vision-exp。
MODEL = os.getenv("OPENAI_VISION_MODEL", "deepseek-v4-flash-vision-exp")

# 这个视觉模型默认开思考：实测 1600 字推理换来慢 3 倍（9.7s vs 3.0s），描述画面用不上。
# 字段名各家不一样，跟 SDK 的 openai_client 保持一致按厂商分。
THINKING_OFF = {"thinking": {"type": "disabled"}} if "deepseek" in MODEL.lower() else {"enable_thinking": False}

SOI, EOI = b"\xff\xd8", b"\xff\xd9"  # JPEG 起止标记
MAX_FRAME_BYTES = 8 * 1024 * 1024  # 一直读不到 EOI 时的兜底上限

mcp = FastMCP("miloco")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    # stdout 是 MCP 的 JSON-RPC 通道，日志只能往 stderr 写；harness 会把子进程的 stderr 透传出来
    stream=sys.stderr,
)
logger = logging.getLogger("miloco")


def api(path: str, payload: dict | None = None, timeout: float = 30) -> dict | list:
    """调 miloco-server 的 JSON 接口，payload 非空即 POST。

    超时给到 30s：某台设备第一次开关时，服务端要现去 miot-spec.org 拉一份 spec。
    """
    req = urllib.request.Request(
        SERVER_URL + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        # 400 的 body 里写着「不支持开关」「设备离线」这类真正的原因，比状态码有用
        try:
            reason = json.loads(e.read()).get("error")
        except ValueError:
            reason = None
        raise RuntimeError(reason or f"HTTP {e.code}") from None


DEVICE_CACHE_TTL = 60  # 秒。设备增减很少见，但每次开灯都重列一遍要多花一轮米家云往返
_device_cache: tuple[float, list] = (0.0, [])


def get_devices() -> list:
    """在线设备列表，短期缓存。"""
    global _device_cache

    ts, devices = _device_cache
    if time.time() - ts > DEVICE_CACHE_TTL:
        devices = api("/devices")
        _device_cache = (time.time(), devices)
        logger.info(f"取到 {len(devices)} 台在线设备")
    return devices


def brief(device: dict) -> dict:
    """交给模型看的字段。did 只在本文件里用来调接口，给模型看是噪音"""
    return {k: device.get(k) for k in ("name", "room", "model")}


def match_device(name: str, devices: list) -> list:
    """按用户说的名字挑设备，返回候选（正好一个才会真去开关）。

    只做「完全一样」和「包含」两档，不做模糊匹配：开关是物理动作，宁可回头多问一句，
    也不能猜错一台——把卧室的灯当成客厅的灯关掉，比说一句「没找到」糟得多。
    """
    key = "".join(name.split()).lower()
    hits = [d for d in devices if "".join((d.get("name") or "").split()).lower() == key]
    if not hits:
        # 「客厅插座」这种连房间一起说的，拿房间+名字再比一次
        hits = [d for d in devices if key in ((d.get("room") or "") + (d.get("name") or "")).lower()]
    return hits


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
        return {"isError": True, "error": f"取帧失败（{e}），确认 miloco-server 的 {VIDEO_FEED_URL} 在跑"}
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


@mcp.tool()
def list_devices() -> dict:
    """List the smart-home devices (米家 / Mi Home) that are online right now.
    Use when: user asks what devices exist, what is in a room, or you need a device's exact name
    before switching it.
    Returns name / room / model. It does NOT include on-off state — there is no tool that reads it."""
    try:
        devices = get_devices()
    except Exception as e:
        logger.error(f"取设备列表失败：{e}")
        return {"isError": True, "error": f"取设备列表失败（{e}），确认 miloco-server 的 {SERVER_URL} 在跑"}

    return {"isError": False, "devices": [brief(d) for d in devices]}


@mcp.tool()
def switch_device(
    name: str = Field(description="Device name, ideally exactly as list_devices returned it, e.g. 客厅落地灯"),
    action: str = Field(description='One of "on", "off", "toggle"'),
) -> dict:
    """Turn a smart-home device on or off (lights, plugs, fans, ...).
    Use when: user asks to turn something on or off, or to toggle it.
    NOT for: anything that is not a plain on-off switch (brightness, color, temperature, mode),
    and not for the camera stream itself.
    When the name does not land on exactly one device this switches NOTHING and returns the
    candidates instead — read them out and ask which one is meant.
    Takes a few seconds: ALWAYS say one short spoken line first, e.g.「这就去开」, then call it."""
    logger.info(f"开关请求：{name} -> {action}")
    try:
        devices = get_devices()
    except Exception as e:
        logger.error(f"取设备列表失败：{e}")
        return {"isError": True, "error": f"取设备列表失败（{e}），确认 miloco-server 的 {SERVER_URL} 在跑"}

    hits = match_device(name, devices)
    if len(hits) != 1:
        # 没命中就把候选原样交回去，让模型（或用户）自己挑，别替他们选一台
        logger.info(f"「{name}」命中 {len(hits)} 台，交回候选")
        return {
            "isError": True,
            "error": f"没有正好一台叫「{name}」的设备" if not hits else f"「{name}」对上了 {len(hits)} 台设备",
            "candidates": [brief(d) for d in (hits or devices)],
        }

    device = hits[0]
    t0 = time.perf_counter()
    try:
        result = api("/device/power", {"did": device["did"], "action": action})
    except Exception as e:
        # 这句是要被念出来的：SDK 那条「未能定位主开关…请显式传入 siid / piid」对用户毫无意义，
        # 换成人话，原文留在日志里
        reason = "不支持开关" if "未能定位主开关" in str(e) else str(e)
        logger.error(f"{device['name']} {action} 失败：{e}")
        return {"isError": True, "error": f"{device['name']}：{reason}"}

    logger.info(f"{device['name']} 现在是 {'开' if result['power'] else '关'}，用时 {time.perf_counter() - t0:.1f}s")
    return {"isError": False, "device": device["name"], "power": result["power"]}


if __name__ == "__main__":
    logger.info(f"miloco mcp 启动：server={SERVER_URL}，模型={MODEL}，base_url={BASE_URL}")
    mcp.run()
