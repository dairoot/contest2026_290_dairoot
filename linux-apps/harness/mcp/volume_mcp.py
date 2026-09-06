"""本机扬声器音量的 stdio MCP server，通过 pactl 控制 PulseAudio。"""

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP("volume")
_volume_lock = asyncio.Lock()
PA_VOLUME_NORM = 65536


async def _pactl(*args: str) -> str:
    env = os.environ.copy()
    # MCP stdio 的环境白名单可能不包含 XDG_RUNTIME_DIR，补上当前用户的音频 socket 目录。
    runtime = Path(f"/run/user/{os.getuid()}")
    if runtime.is_dir():
        env.setdefault("XDG_RUNTIME_DIR", str(runtime))
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", *args, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise RuntimeError("找不到 pactl，请安装 pulseaudio-utils") from None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise RuntimeError("音量控制超时，请检查当前用户的 PulseAudio 服务") from None
    if proc.returncode:
        reason = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"pactl 执行失败：{reason or proc.returncode}")
    return stdout.decode()


async def _read_volume(sink_name: str | None = None) -> dict:
    if sink_name is None:
        sink_name = (await _pactl("get-default-sink")).strip()
    sinks = json.loads(await _pactl("--format=json", "list", "sinks"))
    sink = next((s for s in sinks if s["name"] == sink_name), None)
    if sink is None:
        raise RuntimeError("找不到默认扬声器输出，请检查音频设备连接")
    channels = list(sink["volume"].values())
    if not channels:
        raise RuntimeError("扬声器没有可用的音量通道")
    return {
        "sink": sink_name,
        "volume_percent": round(max(c["value"] for c in channels) * 100 / PA_VOLUME_NORM),
        "muted": sink["mute"],
    }


@mcp.tool()
async def speaker_volume(
    action: Literal["get", "set", "increase", "decrease", "mute", "unmute"] = "get",
    percent: Annotated[int, Field(ge=0, le=100, strict=True)] | None = None,
) -> dict:
    """Query or change this device's speaker playback volume (not microphone gain).
    Use for 「音量多少」「音量设为50%」「大声一点」「小声一点」「静音」「取消静音」.
    get: read current volume. set: percent is required, an absolute volume from 0 to 100.
    increase/decrease: percent is the number of percentage points, default 10; clamp to 0–100.
    mute/unmute: change only mute state; omit percent. get also takes no percent.
    Setting or adjusting volume preserves mute state; use unmute when sound is explicitly requested.
    Returns the actual volume_percent and muted state after the operation."""
    if action == "set" and percent is None:
        return {"isError": True, "error": "设置音量时需要 percent（0～100）"}
    if action in ("get", "mute", "unmute") and percent is not None:
        return {"isError": True, "error": f"{action} 操作不需要 percent"}
    try:
        async with _volume_lock:
            state = await _read_volume()
            sink = state["sink"]
            if action in ("set", "increase", "decrease"):
                if action == "set":
                    target = percent
                else:
                    step = 10 if percent is None else percent
                    target = state["volume_percent"] + (step if action == "increase" else -step)
                target = max(0, min(100, target))
                await _pactl("set-sink-volume", sink, f"{target}%")
            elif action in ("mute", "unmute"):
                await _pactl("set-sink-mute", sink, "1" if action == "mute" else "0")
            if action != "get":
                # 回读同一个输出设备，避免默认设备切换后误报另一台的状态。
                state = await _read_volume(sink)
            return {"isError": False, **state}
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        return {"isError": True, "error": str(exc)}


if __name__ == "__main__":
    mcp.run()
