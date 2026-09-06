"""带 web 配置台的语音对话 demo（在 tests/test_run_v2.py 基础上加了 web 配置和查看）。

音频照旧走本机：麦克风 sd.InputStream → ChatBot，TTS 音频 → sd.OutputStream；
web 页面只做两件事：改配置（保存后按新配置重建 ChatBot 生效）和查看 llm.messages。

启动：python examples/web/main.py，然后打开 http://127.0.0.1:8080
"""

import asyncio
import datetime
import json
import logging
import os
import shlex
import shutil
import sys
import urllib.request
import zoneinfo

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)  # server.py
sys.path.insert(0, os.path.dirname(os.path.dirname(BASE_DIR)))  # aichat_sdk

from server import serve

from aichat_sdk import ChatBot, DEFAULT_TTS_ENGINE, get_tts_engines
from aichat_sdk.config import DEFAULT_AGENT_SOUL
from aichat_sdk.llm import mcp as sdk_mcp
from aichat_sdk.llm.mcp import (
    DEFAULT_TOOLS_SERVER,
    get_mcp_server_status,
    register_default_tools_server,
    remove_mcp_server,
)
from aichat_sdk.llm.system_prompt import SKILLS_DIR, load_skills
from aichat_sdk.llm.type_enum import AgentInfo, DeviceInfo
from aichat_sdk.mcp_tool import mcp as tools_mcp

# SDK 默认 10 秒，板上不够用：aichat-tools 子进程光 import aichat_sdk 就要 9.9 秒
# （aarch64，Mac 上 0.8 秒），连上稳定要 10.3 秒，每次都差一点点超时被跳过。
sdk_mcp.MCP_TOOL_TIMEOUT_SECONDS = 20

logger = logging.getLogger("web_console")

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")  # 每轮会话结束后把完整 llm.messages 存一个 json
WEB_HOST = "0.0.0.0"  # 注意：页面能改外部 MCP server 的启动命令（等于能在本机拉进程），绑 0.0.0.0 后同网段都能访问
WEB_PORT = 8080
OUTPUT_SAMPLE_RATE = 24000  # 采样率不放到页面上配，扬声器流建一次就一直用
PIP_INSTALL_TIMEOUT = 300  # 秒；pip 卡在网络上别让「安装中」永远转下去

DEFAULT_CONFIG = {
    "tts_engine": DEFAULT_TTS_ENGINE,
    "mic": "",  # 拾音设备名，空 = 系统默认输入设备
    "send_idle_farewell": False,
    "tool_thinking": True,
    "timezone": 8,
    "city": "深圳",
    "soul": DEFAULT_AGENT_SOUL,
    # aichat-tools 各工具的开关，{工具名: 是否启用}，缺省启用（不走下面的 mcpServers 配置）
    "aichat_tools": {},
    # 技能的开关，{技能名: 是否启用}，缺省启用；停用的不烘进系统提示词
    "skills": {},
    # 外部 MCP server，格式同 AgentInfo.mcp；fetch 用 uvx 起在它自己的环境里——mcp-server-fetch 钉
    # mcp<2，装进本 venv 会跟 SDK 依赖的 fastmcp 要的 mcp>=2 打架（表现为 server 起不来报 ImportError）。
    # 本地 MCP 脚本统一放在 mcp/，依赖在 pyproject 里，直接用本 venv 的 python 起。
    # python 靠 PATH 找到本 venv，脚本路径相对于 harness 工作目录。
    "mcp": {
        "mcpServers": {
            "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
            "米家": {"command": "python", "args": ["mcp/miloco_mcp.py"]},
            "音量": {"command": "python", "args": ["mcp/volume_mcp.py"]},
        }
    },
}


def input_devices() -> list[str]:
    """当前可用的输入设备名，页面下拉用（重名的只留一个，反正也只能按名字选）。"""
    return list(dict.fromkeys(device["name"] for device in sd.query_devices() if device["max_input_channels"] > 0))


def resolve_mic(name: str) -> int | None:
    """设备名 → 设备号；空名字或设备已经不在（拔了/改名了）都返回 None，即用系统默认。"""
    if name:
        for device in sd.query_devices():
            if device["max_input_channels"] > 0 and device["name"] == name:
                return device["index"]
    return None


def detect_location() -> dict:
    """按出口 IP 定位时区和城市（https://ipinfo.io/json），config.json 不存在时用；失败返回空 dict 走默认值。"""
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=5) as resp:
            info = json.load(resp)
        offset = datetime.datetime.now(zoneinfo.ZoneInfo(info["timezone"])).utcoffset().total_seconds() / 3600
    except Exception as exc:
        logger.warning("IP 定位失败（%s），时区/城市用默认值", exc)
        return {}
    detected = {"timezone": int(offset) if offset.is_integer() else offset}
    if str(info.get("city", "")).strip():
        detected["city"] = str(info["city"]).strip()
    logger.info("IP 定位：%s（UTC%+g）", detected.get("city", "未知城市"), offset)
    return detected


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            config.update(json.load(f))
    else:
        config.update(detect_location())
    # SDK 0.1.1 重命名了豆包引擎；兼容旧页面保存的名称。
    # v2 / bytedancev2 在新 SDK 中仍有效，不能按旧含义重写。
    engine = str(config["tts_engine"]).strip().lower()
    config["tts_engine"] = {
        "bytedance": "bytedancev1", "v3": "bytedancev2", "bytedancev3": "bytedancev2",
    }.get(engine, engine)
    for key in ("aichat_tools", "skills"):  # 兼容手改坏的配置
        if not isinstance(config.get(key), dict):
            config[key] = {}
    # 音量工具默认提供，也补到旧配置里；显式 enabled=false 的设置保留。
    servers = config.setdefault("mcp", {}).setdefault("mcpServers", {})
    servers.setdefault("音量", {"command": "python", "args": ["mcp/volume_mcp.py"]})
    # 兼容搬目录前保存的配置；自定义 cwd 的外部脚本不改。
    for spec in servers.values():
        if isinstance(spec, dict) and not spec.get("cwd"):
            if spec.get("args") in (["miloco_mcp.py"], ["volume_mcp.py"]):
                spec["args"] = [f"mcp/{spec['args'][0]}"]
    return config


def save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _load_tools_catalog() -> list[dict]:
    """aichat-tools 的完整工具目录（含被停用的），从进程内的同一注册表拿，页面渲染开关用。"""
    tools = asyncio.run(tools_mcp.list_tools())
    return sorted(
        (
            {"name": tool.name, "description": tool.description or ""}
            for tool in tools
            if not ((tool.meta or {}).get("aichat_sdk") or {}).get("internal")
        ),
        key=lambda tool: tool["name"],
    )


TOOLS_CATALOG = _load_tools_catalog()


def disabled_aichat_tools(config: dict) -> set[str]:
    return {name for name, on in config["aichat_tools"].items() if not on}


def normalize_mcp_servers(mcp: dict) -> dict:
    """取出 {name: spec}（外层 mcpServers 可省略，兼容 Claude Desktop 写法），顺手逐个校验。"""
    servers = mcp.get("mcpServers", mcp)
    if not isinstance(servers, dict):
        raise ValueError("mcpServers 要是 JSON 对象")

    for name, spec in servers.items():
        if not name.strip():
            raise ValueError("server 名称不能为空")
        if isinstance(spec, str):
            if not spec.strip():
                raise ValueError(f"server「{name}」要填命令或 URL")
        elif isinstance(spec, dict):
            if not (spec.get("command") or spec.get("url")):
                raise ValueError(f"server「{name}」要填命令或 URL")
        else:
            raise ValueError(f"server「{name}」的配置要是字符串或 JSON 对象")
    return servers


def effective_mcp(mcp: dict) -> dict:
    """真正交给 AgentInfo 的那份：去掉停用的 server，以及只给页面用的 enabled 字段。"""
    servers = {}
    for name, spec in mcp.get("mcpServers", {}).items():
        if isinstance(spec, dict):
            if spec.get("enabled") is False:
                continue
            spec = {key: value for key, value in spec.items() if key != "enabled"}
        servers[name] = spec
    return {"mcpServers": servers}


def missing_python_module(spec, error: str) -> tuple[str, str] | None:
    """server 连不上的原因是「python -m 的模块没装」时，返回 (解释器, 模块名)，页面据此给一键安装按钮。

    `python -m 没装的模块` 会往 stderr 打一行「No module named xxx」然后退出，
    _connect_external 把它带进了 error；模块装上了但缺依赖也是这句（引号包着依赖名），
    重装模块本身的包会顺带补齐依赖，一样能治。
    """
    if "No module named" not in error:
        return None
    if isinstance(spec, str):
        parts = shlex.split(spec)
        command, args = (parts[0] if parts else ""), parts[1:]
    elif isinstance(spec, dict):
        command, args = spec.get("command") or "", list(spec.get("args") or [])
    else:
        return None
    if not os.path.basename(command).lower().startswith("python"):
        return None  # uvx / npx 这些自带安装逻辑，装不动的原因五花八门，不掺和
    if "-m" in args and args.index("-m") + 1 < len(args):
        module = args[args.index("-m") + 1]
        if module and not module.startswith("-"):
            return command, module
    return None


async def run_install_command(args: list[str]) -> tuple[int, str]:
    """跑一条安装命令，返回 (退出码, stdout+stderr)；超时杀掉进程并抛 ValueError。"""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=PIP_INSTALL_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ValueError(f"「{' '.join(args)}」超过 {PIP_INSTALL_TIMEOUT} 秒没跑完，已放弃")
    return proc.returncode, output.decode(errors="replace")


def validate_config(raw: dict) -> dict:
    """校验页面提交的配置，返回干净的一份；不合法就抛 ValueError，消息直接显示给页面。"""
    if not isinstance(raw, dict):
        raise ValueError("配置必须是 JSON 对象")

    engine = str(raw.get("tts_engine", "")).strip().lower()
    engines = get_tts_engines()
    match = next((item for item in engines if engine == item["name"] or engine in item["aliases"]), None)
    if match is None:
        raise ValueError(f"tts_engine 只能是：{'、'.join(item['name'] for item in engines)}")
    engine = match["name"]

    soul = str(raw.get("soul", "")).strip()
    if not soul:
        raise ValueError("角色人设不能为空")

    try:
        timezone = float(raw.get("timezone", 8))
    except (TypeError, ValueError):
        raise ValueError("时区要填数字，比如 8")

    tool_toggles = raw.get("aichat_tools") or {}
    if not isinstance(tool_toggles, dict):
        raise ValueError("aichat_tools 要是 JSON 对象（{工具名: 是否启用}）")

    skill_toggles = raw.get("skills") or {}
    if not isinstance(skill_toggles, dict):
        raise ValueError("skills 要是 JSON 对象（{技能名: 是否启用}）")

    mcp = raw.get("mcp") or {}
    if isinstance(mcp, str):  # 页面 JSON 模式直接给一段文本，交给这里解析，报错信息统一
        try:
            mcp = json.loads(mcp) if mcp.strip() else {}
        except json.JSONDecodeError as e:
            raise ValueError(f"MCP 配置不是合法 JSON：{e}")
    if not isinstance(mcp, dict):
        raise ValueError("MCP 配置要是 JSON 对象")
    mcp = {"mcpServers": normalize_mcp_servers(mcp)}

    return {
        "tts_engine": engine,
        "mic": str(raw.get("mic", "")).strip(),
        "send_idle_farewell": bool(raw.get("send_idle_farewell")),
        "tool_thinking": bool(raw.get("tool_thinking")),
        "timezone": int(timezone) if timezone.is_integer() else timezone,
        "city": str(raw.get("city", "")).strip(),
        "soul": soul,
        "aichat_tools": {str(name): bool(on) for name, on in tool_toggles.items()},
        "skills": {str(name): bool(on) for name, on in skill_toggles.items()},
        "mcp": mcp,
    }


class AIClient:
    def __init__(self, config: dict):
        self.config = config
        self.chat_bot: ChatBot | None = None
        self.skills: list[dict] = []
        self.last_error = ""
        self.is_playing = False
        self.is_session_closed = False
        self.is_restarting = False
        self.installing = ""  # 正在 pip install 依赖的 server 名，页面按钮显示「安装中」并防连点
        self.mic_device = ""  # 实际拾音的输入设备名（配的设备没了就是回退到的那个），页面挂个 badge 显示
        self._session_started_at: datetime.datetime | None = None
        self._messages_saved = False
        self._play_task: asyncio.Task | None = None
        self._text_tasks: set[asyncio.Task] = set()
        self._output_stream: sd.OutputStream | None = None
        self._input_stream: sd.InputStream | None = None
        self._lock = asyncio.Lock()

    # ---------------- web 层用到的接口 ----------------

    def config_payload(self) -> dict:
        """给页面的配置：附 SDK 引擎目录和当前麦克风列表。"""
        return {**self.config, "tts_engines": get_tts_engines(), "mics": input_devices()}

    def status(self) -> dict:
        return {
            "running": self.chat_bot is not None,
            "restarting": self.is_restarting,
            "session_closed": self.is_session_closed,
            "speaking": self.is_playing,
            "round": self.chat_bot.llm.round if self.chat_bot else 0,
            "tts_engine": self.config["tts_engine"],
            "mic": self.mic_device,
            "error": self.last_error,
            "mcp": self.mcp_status(),
            "skills": [{**skill, "enabled": self.config["skills"].get(skill["name"], True)} for skill in self.skills],
            "skills_dir": str(SKILLS_DIR),
            "compression": self.compression_status(),
        }

    def compression_status(self) -> dict:
        """上下文压缩状态：页面在消息列表插「已压缩」分隔线、头部挂「压缩中」badge 用。"""
        if self.chat_bot is None:
            return {}
        comp = self.chat_bot.llm.llm_client.compressor
        return {"compressing": comp.compressing, "cursor": comp.cursor, "summary": comp.summary or ""}

    def mcp_status(self) -> list[dict]:
        """每个外部 MCP server 一行：配置里的启用状态 + 上次启动时的连接结果，页面挂状态用。"""
        states = {state["name"]: state for state in get_mcp_server_status()}
        rows = []
        for name, spec in self.config["mcp"].get("mcpServers", {}).items():
            state = states.pop(name, {})
            missing = missing_python_module(spec, state.get("error", ""))
            rows.append(
                {
                    "name": name,
                    "enabled": not (isinstance(spec, dict) and spec.get("enabled") is False),
                    "connected": bool(state.get("connected")),
                    "error": state.get("error", ""),
                    "tools": state.get("tools", []),
                    # 缺 pip 包连不上的，给页面报可安装的包名（PyPI 上 _ 和 - 等价，展示用 - 的写法）
                    "install": missing[1].replace("_", "-") if missing else "",
                    "installing": name == self.installing,
                }
            )
        # SDK 自带工具的 server 不走 mcpServers 配置，每个工具由 aichat_tools 开关控制；
        # 工具列表用完整目录（停用的不在注册表的列表里，但行上要显示才能再启用）
        if DEFAULT_TOOLS_SERVER not in self.config["mcp"].get("mcpServers", {}):
            state = states.pop(DEFAULT_TOOLS_SERVER, {})
            toggles = self.config["aichat_tools"]
            rows.append(
                {
                    "name": DEFAULT_TOOLS_SERVER,
                    "enabled": True,
                    "connected": bool(state.get("connected")),
                    "error": state.get("error", ""),
                    "tools": [{**tool, "enabled": toggles.get(tool["name"], True)} for tool in TOOLS_CATALOG],
                    "readonly": True,
                    "toggle": True,
                }
            )
        # 其他默认注册、不在配置里的 server，页面上显示为只读行
        for name, state in states.items():
            rows.append(
                {
                    "name": name,
                    "enabled": True,
                    "connected": bool(state.get("connected")),
                    "error": state.get("error", ""),
                    "tools": state.get("tools", []),
                    "readonly": True,
                }
            )
        return rows

    async def send_text(self, text: str) -> None:
        """页面手打的文本，走跟 ASR 识别结果一样的链路（照样会出声）；不合法就抛 ValueError 给页面。"""
        text = text.strip()
        if not text:
            raise ValueError("消息不能为空")
        chat_bot = self.chat_bot
        if chat_bot is None or self.is_restarting:
            raise ValueError("ChatBot 没在运行，先修好配置或点「重新开始」")
        if self.is_session_closed:
            raise ValueError("会话已结束，点「重新开始」再聊")

        # 立刻停拾音，别让接下来扬声器里的回答被麦克风收回去（play_audio 收到 stt 也会置位，
        # 但那要等 send_text 真的跑起来）
        self.is_playing = True
        # send_text 要等整轮回答说完才返回，这里不等：HTTP 立刻返回，页面照旧轮询看消息
        task = asyncio.create_task(chat_bot.send_text(text))
        self._text_tasks.add(task)
        task.add_done_callback(self._finish_text_task)

    def _finish_text_task(self, task: asyncio.Task) -> None:
        self._text_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.is_playing = False  # 兜底：这轮没能正常播完，别把拾音一直关着
            logger.error("发送文本失败", exc_info=(type(error), error, error.__traceback__))

    def messages(self) -> list[dict]:
        return self.chat_bot.llm.messages if self.chat_bot else []

    async def apply_config(self, raw: dict) -> None:
        """校验并保存页面提交的配置，再按新配置重建 ChatBot（人设/MCP 都是建 LlmAgent 时烘进系统提示词的，只能重建）。"""
        config = validate_config(raw)
        save_config(config)
        await self.restart(config)

    async def install_mcp_package(self, name: str) -> str:
        """给缺 Python 包连不上的 MCP server 装包，装完重启 ChatBot 重连，返回装的包名。

        用 server 配置里的那个解释器跑 pip，包才装进它启动时用的环境；解释器没带 pip
        （uv 建的 venv 默认就没有）就退回 `uv pip install --python 该解释器` 装进同一环境。
        不合法/装失败抛 ValueError 给页面。
        """
        if self.installing:
            raise ValueError(f"正在给「{self.installing}」装依赖，等它装完")
        spec = self.config["mcp"].get("mcpServers", {}).get(name)
        if spec is None:
            raise ValueError(f"配置里没有叫「{name}」的 MCP server")
        state = next((row for row in get_mcp_server_status() if row["name"] == name), {})
        missing = missing_python_module(spec, state.get("error", ""))
        if missing is None:
            raise ValueError(f"「{name}」连不上不是因为缺 Python 包，没法一键安装")
        interpreter, module = missing
        package = module.replace("_", "-")

        self.installing = name
        try:
            logger.info("pip install %s（%s）", package, interpreter)
            code, output = await run_install_command([interpreter, "-m", "pip", "install", package])
            if code != 0 and "No module named pip" in output:
                uv = shutil.which("uv")
                if uv is None:
                    raise ValueError(f"{interpreter} 没带 pip，也找不到 uv；手动装：uv pip install {package}")
                logger.info("解释器没带 pip，改用 uv 装 %s", package)
                code, output = await run_install_command([uv, "pip", "install", "--python", interpreter, package])
            if code != 0:
                tail = "\n".join(output.strip().splitlines()[-3:])
                raise ValueError(f"安装 {package} 失败：{tail}")
        finally:
            self.installing = ""

        logger.info("已安装 %s，重启 ChatBot 重连「%s」", package, name)
        await self.restart()  # _start_bot 会让工具列表重建，连不上的 server 这次会重试
        return package

    async def restart(self, config: dict | None = None) -> None:
        async with self._lock:
            self.is_restarting = True
            try:
                await self._stop_bot()
                if config is not None:
                    self._sync_mcp_registry(config)  # 必须在 self.config 被换掉之前，要拿旧配置做差异
                    self.config = config
                await self._start_bot()
                self._open_input_stream()  # 放在 _start_bot 之后：它开头会清空 last_error，反过来麦克风的报错就没了
            finally:
                self.is_restarting = False

    # ---------------- ChatBot 生命周期 ----------------

    async def _start_bot(self) -> None:
        """起不来就把原因记进 last_error（chat_bot 保持 None），页面还能接着改配置重试。"""
        self.last_error = ""
        config = self.config
        # 技能是建 LlmAgent 时扫 SKILLS_DIR 烘进系统提示词的，这里同步扫一份给页面渲染开关；
        # 停用的不传给 AgentInfo.skills，模型就看不到它
        self.skills = load_skills()
        enabled_skills = [skill["name"] for skill in self.skills if config["skills"].get(skill["name"], True)]
        # SDK 自带工具的开关：停用的工具作为 --disable 传给 aichat-tools server，不对模型提供
        register_default_tools_server(disabled_tools=sorted(disabled_aichat_tools(config)))
        try:
            chat_bot = ChatBot(
                tts_engine=config["tts_engine"],
                output_sample_rate=OUTPUT_SAMPLE_RATE,
                send_idle_farewell=config["send_idle_farewell"],
                user_info=DeviceInfo(timezone=config["timezone"], city=config["city"]),
                agent_info=AgentInfo(
                    soul=config["soul"],
                    mcp=effective_mcp(config["mcp"]),
                    skills=enabled_skills,
                    tool_thinking=config["tool_thinking"],
                ),
            )
            await chat_bot.initialize()
        except SystemExit:  # initialize() 里 ASR/TTS 连不上会直接 exit(1)，web 端不能跟着退出
            self.last_error = "ASR / TTS 连接失败，检查 asr_server 是否启动、.env 是否配好"
            logger.error(self.last_error)
            return
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.exception("启动 ChatBot 失败")
            return

        self.chat_bot = chat_bot
        self.is_session_closed = False
        self.is_playing = False
        self._session_started_at = datetime.datetime.now()
        self._messages_saved = False
        self._play_task = asyncio.create_task(self.play_audio(chat_bot))

    def save_messages(self, chat_bot: ChatBot | None = None) -> str:
        """把这一轮会话完整的 llm.messages 落到 sessions/ 下的一个 jsonl 文件（一行一条），返回文件路径。

        会话结束（收到 close）时存一次；没等到 close 就被重启/退出打断的，
        由 _stop_bot 补存，_messages_saved 保证同一轮只写一个文件。
        """
        chat_bot = chat_bot or self.chat_bot
        if chat_bot is None or self._messages_saved:
            return ""
        messages = chat_bot.llm.messages
        if len(messages) <= 1:  # 只有 system prompt，等于没聊，不留空文件
            self._messages_saved = True
            return ""

        started_at = self._session_started_at or datetime.datetime.now()
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        path = os.path.join(SESSIONS_DIR, f"{started_at:%Y%m%d-%H%M%S}.jsonl")
        try:
            with open(path, "w", encoding="utf-8") as f:
                for message in messages:
                    f.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
        except OSError:  # 存记录失败不该影响会话本身
            logger.exception("保存会话记录失败")
            return ""
        self._messages_saved = True
        logger.info("[session] 会话记录已保存：%s（%d 条 message）", path, len(messages))
        return path

    def _sync_mcp_registry(self, new_config: dict) -> None:
        """只注销删掉的和改过的 server（stdio 子进程随之退出）。

        没动过的留着不碰：重建 LlmAgent 时 load_mcp_servers 照旧注册它们，
        _collect_tools 直接复用已连上的 client，不用重拉子进程。
        """
        old_servers = effective_mcp(self.config["mcp"])["mcpServers"]
        new_servers = effective_mcp(new_config["mcp"])["mcpServers"]
        for name, spec in old_servers.items():
            if new_servers.get(name) != spec:
                remove_mcp_server(name)
        # 自带工具的开关变了也要重开 aichat-tools（--disable 在启动命令里，复用旧连接供的还是旧工具集）
        if disabled_aichat_tools(self.config) != disabled_aichat_tools(new_config):
            remove_mcp_server(DEFAULT_TOOLS_SERVER)

    async def _stop_bot(self) -> None:
        task, chat_bot = self._play_task, self.chat_bot
        self._play_task, self.chat_bot = None, None
        for text_task in self._text_tasks:
            text_task.cancel()
        if self._text_tasks:
            await asyncio.gather(*self._text_tasks, return_exceptions=True)
        self._text_tasks.clear()
        self.save_messages(chat_bot)  # 没等到 close 就被重启/退出打断的，在这里补存
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if chat_bot is not None:
            await chat_bot.close()

    # ---------------- 音频 ----------------

    async def play_audio(self, chat_bot: ChatBot):
        opus_decoder = chat_bot.create_opus_decoder()
        prime = True  # 每句开头先垫一段静音，见下面 audio 分支

        while True:
            item = await chat_bot.receive_item()
            if item.get("type") == "close":
                # 会话结束不退进程：页面上显示「会话已结束」，点重新开始再建一个
                self.save_messages(chat_bot)
                self.is_session_closed = True
                self.is_playing = False
                logger.info("[session] 会话结束，停止拾音；页面点「重新开始」再开一轮")
                return

            elif item.get("type") == "audio_finish":
                await asyncio.sleep(0.3)
                self.is_playing = False
                prime = True

            elif item.get("type") == "stt":
                self.is_playing = True

            elif item.get("type") == "audio":
                self.is_playing = True
                pcm = opus_decoder.decode(item["data"])
                audio_array = np.frombuffer(pcm, dtype=np.int16)
                if prime:
                    # 句首 TTS 网络流还没跑到实时速率，先写 200ms 静音把输出缓冲
                    # 灌到安全水位——供数毛刺落在静音里，而不是打碎前几个字
                    await asyncio.to_thread(
                        self._output_stream.write,
                        np.zeros(int(OUTPUT_SAMPLE_RATE * 0.2), dtype=np.int16))
                    prime = False
                # 写扬声器按实时速率阻塞，放线程里，不然一说话 web 就卡住不响应
                await asyncio.to_thread(self._output_stream.write, audio_array)

    def _open_input_stream(self) -> None:
        """按配置的设备开拾音流（换设备只能关了重开）；打不开就记进 last_error，页面还能再挑一个。"""
        self._close_input_stream()
        try:
            stream = sd.InputStream(
                device=resolve_mic(self.config["mic"]),
                callback=self.audio_callback,
                channels=1,
                samplerate=16000,
                blocksize=int(240 * 16000 / 1000),
            )
            stream.start()
        except Exception as e:
            self.mic_device = ""
            self.last_error = f"麦克风打不开：{type(e).__name__}: {e}"
            logger.exception("打开麦克风失败")
            return
        self._input_stream = stream
        self.mic_device = sd.query_devices(stream.device)["name"]

    def _close_input_stream(self) -> None:
        stream, self._input_stream = self._input_stream, None
        if stream is not None:
            stream.stop()
            stream.close()

    def audio_callback(self, indata: np.ndarray, frames: int, time, status) -> None:
        chat_bot = self.chat_bot
        # 会话结束后就别再拾音了：bot 还留着（页面要看 llm.messages），但 play_audio 已经退出，
        # 再往里送音频只会开新一轮、音频包全堆在队列里没人消费，表现就是"能说话但没声音"
        if chat_bot is None or self.is_playing or self.is_session_closed:
            return

        chat_bot.audio_callback(indata, frames, time, status)

    # ---------------- 入口 ----------------

    async def run(self):
        self._output_stream = sd.OutputStream(samplerate=OUTPUT_SAMPLE_RATE, channels=1, dtype=np.int16)
        self._output_stream.start()
        try:
            await self.restart()  # 拾音流在里面按配置开，换设备保存后一起重开
            print(f"web 配置台: http://{WEB_HOST}:{WEB_PORT}    聆听中... (Ctrl+C 退出)")
            await serve(self, WEB_HOST, WEB_PORT)
        finally:
            async with self._lock:
                await self._stop_bot()
            self._close_input_stream()
            self._output_stream.stop()
            self._output_stream.close()


if __name__ == "__main__":
    aiclient = AIClient(load_config())
    asyncio.run(aiclient.run())
