"""TTS 目录与配置回归：不启动 ChatBot、不访问音频设备或外部服务。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

with patch("dotenv.load_dotenv"):
    import main

from server import create_app


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


if __name__ == "__main__":
    unittest.main()
