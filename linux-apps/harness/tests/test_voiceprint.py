"""声纹持久化、ASR 说话人透传与配置台接口；不调用模型或真实音频设备。"""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from aichat_sdk.llm import LlmAgent
from aichat_sdk.utils.event_emitter import AsyncEventEmitter
from server import create_app
from session_store import get_session, save_session
from voiceprint import MAX_SAMPLES, Voiceprint, VoiceprintChatBot, VoiceprintClient, VoiceprintStore


def utterance(url="/audio/one.wav", embedding=None, speech_ms=2000, speaker="speaker_1"):
    return {"audio_url": url, "embedding": [1, 0, 0] if embedding is None else embedding,
            "content": "你好，帮我打开客厅的灯", "speech_ms": speech_ms, "speaker": speaker}


class VoiceprintTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "data" / "history.sqlite3")
        self.client = VoiceprintClient(self.path)

    def test_empty_read_creates_no_database(self):
        self.assertEqual(self.client.status(), {"profiles": [], "utterances": {}, "error": ""})
        self.assertFalse(Path(self.path).parent.exists())

    def test_enrollment_survives_restart_and_coexists_with_chat_history(self):
        now = datetime.now(timezone.utc)
        save_session(self.path, "chat-1", now, now, [{"role": "user", "content": "你好"}])
        # 模拟升级前只有聊天表的数据库。
        engine = create_engine("sqlite:///" + self.path)
        try:
            Voiceprint.__table__.drop(engine)
        finally:
            engine.dispose()
        self.assertEqual(self.client.status()["profiles"], [])
        self.client.capture(utterance())
        self.client.enroll("/audio/one.wav", " 小明 ")
        restarted = VoiceprintClient(self.path)
        restarted.capture(utterance("/audio/two.wav", [0.95, 0.1, 0]))
        self.assertEqual(restarted.status()["utterances"]["/audio/two.wav"]["speaker"], "speaker_1")
        self.assertEqual(restarted.status()["profiles"], [{"name": "小明", "samples": 1}])
        self.assertEqual(get_session(self.path, "chat-1")["messages"][0]["content"], "你好")
        engine = create_engine("sqlite:///" + self.path)
        try:
            with Session(engine) as session:
                row = session.scalar(select(Voiceprint))
                self.assertEqual(row.embeddings, [[1, 0, 0]])
                self.assertTrue(row.updated_at)
        finally:
            engine.dispose()

    def test_duplicate_enrollment_is_idempotent_and_cannot_use_another_name(self):
        self.client.capture(utterance())
        self.client.enroll("/audio/one.wav", "小明")
        self.client.enroll("/audio/one.wav", "小明")
        self.assertEqual(self.client.status()["profiles"][0]["samples"], 1)
        with self.assertRaisesRegex(ValueError, "已录入为"):
            self.client.enroll("/audio/one.wav", "小红")

    def test_sample_limit_and_name_validation(self):
        for index in range(MAX_SAMPLES + 2):
            self.client.store.add("小明", [1, index / 100, 0])
        self.assertEqual(VoiceprintStore(self.path).summary()[0]["samples"], MAX_SAMPLES)
        for name in ("", " ", "名" * 41, None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.client.store.add(name, [1, 0, 0])

    def test_changed_model_dimensions_cannot_be_enrolled(self):
        self.client.store.add("小明", [1, 0, 0])
        self.client.store.add("小红", [0, 1, 0])
        with self.assertRaisesRegex(ValueError, "维度"):
            self.client.store.add("新名字", [1, 0])

    def test_missing_invalid_or_short_embeddings_cannot_be_enrolled(self):
        for payload in (utterance(speech_ms=1499), utterance(speech_ms=float("nan")),
                        utterance(speech_ms=float("inf")), utterance(embedding=[0, 0, 0]),
                        utterance(embedding=[float("nan"), 0, 0]), utterance(embedding=[[1, 0, 0]]),
                        {"audio_url": "/audio/one.wav"}):
            with self.subTest(payload=payload):
                self.client.capture(payload)
                with self.assertRaises(ValueError):
                    self.client.enroll("/audio/one.wav", "小明")
        self.assertFalse(Path(self.path).exists())

    def test_restart_clears_utterances_and_embeddings_never_reach_status(self):
        self.client.capture(utterance())
        snapshot = json.dumps(self.client.status(), ensure_ascii=False)
        self.assertNotIn("embedding", snapshot)
        self.assertNotIn("vector", snapshot)
        self.client.utterances.clear()
        with self.assertRaisesRegex(ValueError, "失效"):
            self.client.enroll("/audio/one.wav", "小明")

    def test_delete_persists_and_does_not_change_asr_speaker(self):
        self.client.capture(utterance())
        self.client.enroll("/audio/one.wav", "小明")
        self.client.remove("小明")
        self.assertEqual(VoiceprintStore(self.path).summary(), [])
        self.client.capture(utterance("/audio/two.wav"))
        self.assertEqual(self.client.status()["utterances"]["/audio/two.wav"]["speaker"], "speaker_1")
        self.assertEqual(self.client.status()["utterances"]["/audio/one.wav"]["enrolled_name"], "")

    def test_bad_database_does_not_change_asr_speaker_or_get_overwritten(self):
        Path(self.path).parent.mkdir()
        Path(self.path).write_bytes(b"invalid sqlite database")
        with self.assertLogs("web_console", "ERROR"):
            self.client.capture(utterance())
            status = self.client.status()
        self.assertTrue(self.client.status()["error"])
        self.assertEqual(status["utterances"]["/audio/one.wav"]["speaker"], "speaker_1")
        with self.assertRaises(ValueError):
            self.client.enroll("/audio/one.wav", "小明")
        self.assertEqual(Path(self.path).read_bytes(), b"invalid sqlite database")

    def test_api_enrolls_cached_audio_only_and_deletes(self):
        self.client.capture(utterance())
        with TestClient(create_app(SimpleNamespace(voiceprints=self.client))) as web:
            response = web.post("/api/voiceprints", json={"name": "小明", "audio_url": "/audio/one.wav"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["voiceprints"]["profiles"], [{"name": "小明", "samples": 1}])
            for body in ({"name": "小红", "audio_url": "http://example.com/arbitrary.wav"},
                         {"name": [], "audio_url": "/audio/one.wav"}, [], {"name": ""}):
                self.assertEqual(web.post("/api/voiceprints", json=body).status_code, 400)
            response = web.request("DELETE", "/api/voiceprints", json={"name": "小明"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["voiceprints"]["profiles"], [])

    def test_database_write_failure_does_not_enroll_in_memory(self):
        self.client.capture(utterance())
        with patch("voiceprint.create_engine", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.client.enroll("/audio/one.wav", "小明")
        self.assertEqual(self.client.status()["profiles"], [])
        self.assertEqual(self.client.status()["utterances"]["/audio/one.wav"]["enrolled_name"], "")


class VoiceprintSdkTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = VoiceprintClient(str(Path(self.temp.name) / "history.sqlite3"))
        self.client.store.add("小明", [1, 0, 0])
        self.client.store.add("小红", [0, 1, 0])
        llm = object.__new__(LlmAgent)
        AsyncEventEmitter.__init__(llm)
        llm.timezone = timezone.utc
        llm.round, llm.tool_thinking, llm.messages = 0, True, []

        async def chat_stream(*args, **kwargs):
            yield "text", "好的"

        llm.llm_client = SimpleNamespace(chat_stream=chat_stream)
        with patch("aichat_sdk.LlmAgent", return_value=llm), \
                patch("aichat_sdk.get_tts_client", return_value=SimpleNamespace(
                    abort=AsyncMock(), set_last_context_text=Mock(),
                )), patch("aichat_sdk.OpusStreamEncoder"):
            self.bot = VoiceprintChatBot(voiceprints=self.client)
        await self.bot.reg_asr()  # 不连接 ASR、LLM、TTS；使用 SDK 的真实 STT 调度。

    async def speech(self, payload):
        await self.bot.asr.emit("stt", payload)
        await asyncio.gather(*self.bot._asr_dispatch_tasks)

    async def test_asr_speaker_is_forwarded_without_local_matching(self):
        # 已有本地样本也不读取或匹配，ASR 姓名与本地样本姓名故意不同。
        with patch.object(self.client.store, "_load", side_effect=AssertionError("must not match local profiles")):
            await self.speech(utterance(speaker="ASR 张三"))
        await self.speech(utterance("/audio/two.wav", [0, 1, 0], speaker="ASR 李四"))
        await self.speech(utterance("/audio/unknown.wav", [0, 0, 1], speaker="speaker_3"))
        await self.speech(utterance("/audio/short.wav", [1, 0, 0], speech_ms=500, speaker="unknown"))
        await self.bot.send_text("打字发送")
        payloads = [json.loads(m["content"]) for m in self.bot.llm.messages if m["role"] == "user"]
        self.assertEqual([p.get("speaker") for p in payloads], ["ASR 张三", "ASR 李四", "speaker_3", "unknown", None])
        self.assertTrue(all(set(p) <= {"text", "time", "round", "speaker"} for p in payloads))
        self.assertNotIn("embedding", json.dumps(self.bot.llm.messages))

    async def test_overlapping_dispatch_keeps_independent_names(self):
        seen = []

        async def send(bot, text, *args, audio_url=None, speaker=None):
            await asyncio.sleep(0)
            # SDK 的 send_text 会再建一层 task；确认上下文能穿过 task 边界。
            async def child():
                await asyncio.sleep(0)
                seen.append((text, speaker))
            await asyncio.create_task(child())

        with patch.object(self.bot, "send_text", new=send.__get__(self.bot)):
            await asyncio.gather(
                self.bot.asr.emit("stt", utterance(speaker="ASR 张三")),
                self.bot.asr.emit("stt", {**utterance("/audio/two.wav"), "content": "第二句", "speaker": "ASR 李四"}),
            )
            await asyncio.gather(*self.bot._asr_dispatch_tasks)
        self.assertEqual(seen, [("你好，帮我打开客厅的灯", "ASR 张三"), ("第二句", "ASR 李四")])


if __name__ == "__main__":
    unittest.main()
