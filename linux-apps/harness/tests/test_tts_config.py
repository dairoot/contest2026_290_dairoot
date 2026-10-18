"""TTS 目录与配置回归：不启动 ChatBot、不访问音频设备或外部服务。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

with patch("dotenv.load_dotenv"):
    import main

from server import create_app
from aichat_sdk import ChatBot as SDKChatBot


class TtsConfigTest(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(main.DEFAULT_CONFIG)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        config_path = patch.object(main, "CONFIG_PATH", str(self.path))
        config_path.start()
        self.addCleanup(config_path.stop)

    def test_new_config_uses_sdk_default(self):
        with patch.object(main, "detect_location", return_value={}):
            self.assertEqual(main.load_config()["tts_engine"], main.DEFAULT_TTS_ENGINE)

    def test_saved_legacy_names_migrate_without_remapping_new_v2(self):
        for old, expected in (
            ("bytedance", "bytedancev1"), (" V3 ", "bytedancev2"),
            ("bytedancev3", "bytedancev2"), ("v2", "v2"),
            ("bytedancev2", "bytedancev2"), ("mimo", "mimo"),
            ("unknown", "unknown"),
        ):
            with self.subTest(engine=old):
                self.path.write_text(json.dumps({"tts_engine": old}), encoding="utf-8")
                loaded = main.load_config()
                self.assertEqual(loaded["tts_engine"], expected)
                main.save_config(loaded)
                self.assertEqual(main.load_config()["tts_engine"], expected)

    def test_sdk_names_and_aliases_are_saved_as_canonical_names(self):
        for engine in main.get_tts_engines():
            for name in [engine["name"], *engine["aliases"]]:
                with self.subTest(engine=name):
                    raw = {**self.config, "tts_engine": f" {name.upper()} "}
                    self.assertEqual(main.validate_config(raw)["tts_engine"], engine["name"])

    def test_api_and_validation_follow_sdk_catalog_changes(self):
        engines = [{"name": "future", "label": "新引擎", "aliases": ["new"], "default": True}]
        with patch.object(main, "get_tts_engines", return_value=engines), \
                patch.object(main, "input_devices", return_value=["Test mic"]):
            client = main.AIClient(self.config)
            with TestClient(create_app(client)) as web:
                response = web.get("/api/config")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["tts_engines"], engines)
            self.assertEqual(response.json()["mics"], ["Test mic"])
            self.assertNotIn("tts_engines", client.config)
            self.assertEqual(main.validate_config({**self.config, "tts_engine": "new"})["tts_engine"], "future")
            with self.assertRaisesRegex(ValueError, "future"):
                main.validate_config({**self.config, "tts_engine": "edge"})

    def test_api_saves_mimo_and_rejects_unknown_before_restart(self):
        client = main.AIClient(self.config)

        async def restart(config):
            client.config = config

        with patch.object(main, "input_devices", return_value=[]), \
                patch.object(client, "restart", new=AsyncMock(side_effect=restart)) as restart_mock:
            with TestClient(create_app(client)) as web:
                response = web.post("/api/config", json={**self.config, "tts_engine": " MiMo "})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["config"]["tts_engine"], "mimo")
                self.assertEqual(response.json()["config"]["tts_engines"], main.get_tts_engines())
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.assertEqual(saved["tts_engine"], "mimo")
                self.assertNotIn("tts_engines", saved)
                restart_mock.assert_awaited_once_with(saved)
                for invalid in ("", "not-an-engine"):
                    response = web.post("/api/config", json={**self.config, "tts_engine": invalid})
                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(response.json()["ok"])
                self.assertEqual(restart_mock.await_count, 1)
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), saved)


class StartupErrorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = copy.deepcopy(main.DEFAULT_CONFIG)
        for name in ("BYTEDANCE_TTS_APP_ID", "BYTEDANCE_TTS_APP_KEY", "MIMO_API_KEY"):
            setting = patch.object(main.sdk_config, name, "")
            setting.start()
            self.addCleanup(setting.stop)

    async def start_failing_bot(self, initialize, engine="edge"):
        client = main.AIClient({**self.config, "tts_engine": engine})
        bot = SimpleNamespace(initialize=initialize)
        with patch.object(main, "ChatBot", return_value=bot), \
                patch.object(main, "load_skills", return_value=[]), \
                patch.object(main, "register_default_tools_server"), \
                self.assertLogs(level="ERROR"):
            await client._start_bot()
        self.assertIsNone(client.chat_bot)
        return client

    async def test_missing_bytedance_settings_are_reported_before_connecting(self):
        with patch.object(main, "ChatBot") as constructor:
            for engine in ("bytedancev1", "bytedancev2", "v1", " V2 "):
                with self.subTest(engine=engine):
                    client = main.AIClient({**self.config, "tts_engine": engine})
                    with self.assertLogs("web_console", level="ERROR"):
                        await client._start_bot()
                    self.assertIn("TTS（豆包", client.last_error)
                    self.assertIn("尚未配置", client.last_error)
                    self.assertIn("BYTEDANCE_TTS_APP_ID", client.last_error)
                    self.assertIn("BYTEDANCE_TTS_APP_KEY", client.last_error)
                    self.assertIn(".env", client.last_error)
                    self.assertNotIn("ASR", client.last_error)
            constructor.assert_not_called()

    async def test_only_missing_settings_are_listed_without_values(self):
        with patch.object(main.sdk_config, "BYTEDANCE_TTS_APP_KEY", "private-test-key"), \
                patch.object(main.sdk_config, "BYTEDANCE_TTS_APP_ID", "  "):
            error = main.tts_configuration_error("bytedancev2")
        self.assertIn("BYTEDANCE_TTS_APP_ID", error)
        self.assertNotIn("BYTEDANCE_TTS_APP_KEY", error)
        self.assertNotIn("private-test-key", error)

    async def test_mimo_only_requires_its_own_key(self):
        client = main.AIClient({**self.config, "tts_engine": "mimo"})
        with self.assertLogs("web_console", level="ERROR"):
            await client._start_bot()
        self.assertIn("TTS（小米 MiMo）尚未配置", client.last_error)
        self.assertIn("MIMO_API_KEY", client.last_error)
        self.assertNotIn("BYTEDANCE", client.last_error)
        with patch.object(main.sdk_config, "MIMO_API_KEY", "private-mimo-key"):
            self.assertEqual(main.tts_configuration_error("mimo"), "")

    async def test_edge_starts_without_tts_credentials(self):
        bot = SimpleNamespace(initialize=AsyncMock(), close=AsyncMock(), llm=SimpleNamespace(messages=[]))
        client = main.AIClient({**self.config, "tts_engine": "edge-tts"})
        with patch.object(main, "ChatBot", return_value=bot), \
                patch.object(main, "load_skills", return_value=[]), \
                patch.object(main, "register_default_tools_server"), \
                patch.object(client, "play_audio", new=AsyncMock()):
            await client._start_bot()
            self.assertEqual(client.last_error, "")
            self.assertIs(client.chat_bot, bot)
            bot.initialize.assert_awaited_once()
            await client._stop_bot()

    async def test_sdk_asr_failure_is_identified_from_original_exception(self):
        from aichat_sdk.asr.asr_client import AsrWebSocketClient

        sdk = SimpleNamespace(
            reg_llm=AsyncMock(), reg_tts=AsyncMock(), reg_asr=AsyncMock(),
            asr=AsrWebSocketClient("ws://127.0.0.1:8086/ws"),
            tts=SimpleNamespace(connect=AsyncMock(), close=AsyncMock()),
        )

        async def initialize():
            with patch("aichat_sdk.asr.asr_client.websockets.connect", new=AsyncMock(
                side_effect=ConnectionRefusedError("ASR unavailable"),
            )):
                await SDKChatBot.initialize(sdk)

        client = await self.start_failing_bot(initialize)
        self.assertIn("语音识别（ASR）服务连接失败", client.last_error)
        self.assertIn("ASR_SERVER_WS_URL", client.last_error)
        self.assertNotIn("TTS", client.last_error)
        sdk.tts.connect.assert_not_awaited()

    async def test_sdk_tts_failure_names_selected_engine_without_asr(self):
        from aichat_sdk.tts.bytedancev2.bidirection import BytedanceTTSClient

        sdk = SimpleNamespace(
            reg_llm=AsyncMock(), reg_tts=AsyncMock(), reg_asr=AsyncMock(),
            asr=SimpleNamespace(connect=AsyncMock(), close=AsyncMock()),
            tts=BytedanceTTSClient(app_id="test-app-id", app_key="private-test-key"),
        )

        async def initialize():
            with patch("aichat_sdk.tts.bytedancev2.bidirection.websockets.connect", new=AsyncMock(
                side_effect=ConnectionRefusedError("TTS unavailable"),
            )):
                await SDKChatBot.initialize(sdk)

        with patch.object(main.sdk_config, "BYTEDANCE_TTS_APP_ID", "test-app-id"), \
                patch.object(main.sdk_config, "BYTEDANCE_TTS_APP_KEY", "private-test-key"):
            client = await self.start_failing_bot(initialize, "bytedancev2")
        self.assertIn("TTS（豆包 v2）连接失败", client.last_error)
        self.assertNotIn("ASR", client.last_error)
        self.assertNotIn("private-test-key", client.last_error)
        sdk.asr.connect.assert_awaited_once()

    async def test_unknown_exit_does_not_guess_asr_or_tts(self):
        client = await self.start_failing_bot(AsyncMock(side_effect=SystemExit(1)))
        self.assertIn("对话服务初始化失败", client.last_error)
        self.assertNotIn("ASR", client.last_error)
        self.assertNotIn("TTS", client.last_error)

    async def test_restart_reports_missing_tts_without_opening_microphone(self):
        client = main.AIClient({**self.config, "tts_engine": "bytedancev1"})
        with patch.object(main, "input_devices", return_value=[]), \
                patch.object(client, "_open_input_stream") as open_mic, \
                patch.object(client, "_close_input_stream") as close_mic, \
                self.assertLogs("web_console", level="ERROR"):
            with TestClient(create_app(client)) as web:
                response = web.post("/api/restart")
                state = web.get("/api/state").json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        self.assertIn("TTS（豆包 v1）尚未配置", response.json()["error"])
        self.assertEqual(state["status"]["error"], response.json()["error"])
        self.assertFalse(client.is_restarting)
        open_mic.assert_not_called()
        close_mic.assert_called_once()


if __name__ == "__main__":
    unittest.main()
