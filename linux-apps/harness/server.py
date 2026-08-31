"""web 配置台的 HTTP 层：一个页面 + 五个接口 + 一个录音代理，只跟 AIClient 打交道。

- GET  /             页面
- GET  /api/config   当前配置 + 可选的麦克风列表（页面加载时填表单，之后不再轮询，免得覆盖正在编辑的内容）
- POST /api/config   保存配置并按新配置重建 ChatBot
- POST /api/restart  用当前配置重开一轮会话
- POST /api/send     发一句文本给 ChatBot（等同说话，照样会出声）
- GET  /api/state    状态 + llm.messages（页面每秒轮询）
- GET  /audio/...    转发到 ASR 服务上的用户录音（见 get_audio）
"""

import asyncio
import contextlib
import os
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from aichat_sdk.config import ASR_SERVER_WS_URL

INDEX_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

# ASR 服务的 http 根，跟 AsrWebSocketClient 一样按 ws 地址推出来
_ASR = urlsplit(ASR_SERVER_WS_URL)
ASR_HTTP_BASE = urlunsplit(("https" if _ASR.scheme == "wss" else "http", _ASR.netloc, "", "", ""))


def create_app(client) -> Starlette:
    http = httpx.AsyncClient(timeout=10)  # 跟进程同生死，不单独关

    async def index(request: Request):
        return FileResponse(INDEX_HTML)

    async def get_config(request: Request):
        return JSONResponse(client.config_payload())

    async def post_config(request: Request):
        try:
            await client.apply_config(await request.json())
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return _result(client)

    async def post_restart(request: Request):
        await client.restart()
        return _result(client)

    async def post_send(request: Request):
        try:
            await client.send_text((await request.json()).get("text", ""))
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    async def get_state(request: Request):
        return JSONResponse({"status": client.status(), "messages": client.messages()})

    async def get_audio(request: Request):
        """代理 ASR 服务上的用户录音。

        ASR 给的 audio_url 是它自己的地址（默认 http://127.0.0.1:8086），页面从别的机器打开时
        浏览器会去请求自己的 localhost，放不出来。让页面走同源的这条路由，谁打开都能放。
        """
        try:
            upstream = await http.get(ASR_HTTP_BASE + quote(request.url.path, safe="/"))
        except httpx.HTTPError as e:
            return JSONResponse({"error": f"取录音失败：{e}"}, status_code=502)
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "audio/wav"),
        )

    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/config", get_config),
            Route("/api/config", post_config, methods=["POST"]),
            Route("/api/restart", post_restart, methods=["POST"]),
            Route("/api/send", post_send, methods=["POST"]),
            Route("/api/state", get_state),
            Route("/audio/{path:path}", get_audio),
        ]
    )


def _result(client) -> JSONResponse:
    return JSONResponse({"ok": not client.last_error, "error": client.last_error, "config": client.config_payload()})


async def serve(client, host: str, port: int) -> None:
    """跑到外面把这个协程 cancel 掉（Ctrl+C）为止。"""
    server = uvicorn.Server(uvicorn.Config(create_app(client), host=host, port=port, log_level="warning"))
    # 不让 uvicorn 接管 SIGINT，Ctrl+C 照常打断主协程去关 ChatBot 和音频流
    server.capture_signals = contextlib.nullcontext
    task = asyncio.create_task(server.serve())
    try:
        await asyncio.shield(task)
    finally:
        # 直接 cancel 的话 uvicorn 会吐一大段 CancelledError，让它自己收尾
        server.should_exit = True
        await task
