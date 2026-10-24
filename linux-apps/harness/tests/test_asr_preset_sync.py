"""真实本地 WS 链路：SQLite 录入 → ASR 预设 → 姓名回包，不加载语音模型。"""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
import websockets

from aichat_sdk.utils.event_emitter import AsyncEventEmitter
from voiceprint import PresetAsrClient, VoiceprintClient


def load_asr():
    root = Path(__file__).resolve().parents[2] / "asr-server"
    config = SimpleNamespace(ASR_MODEL_TYPE="fake", SPEAKER_BACKEND="rknn", SPEAKER_RKNN_PATH="unused")
    backend = SimpleNamespace(generate_embedding=Mock(side_effect=AssertionError("model must not run")))
    with patch.object(sys, "path", [str(root), *sys.path]), \
            patch.dict(sys.modules, {"config": config, "speaker_rknn": backend}), \
            patch("utils._rknn.rknn_available", return_value=True):
        modules = []
        for name in ("server", "speaker"):
            spec = importlib.util.spec_from_file_location("preset_sync_asr_" + name, root / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules.append(module)
    return modules


asr_server, speaker_module = load_asr()


class FakeAsrClient(AsyncEventEmitter):
    def __init__(self, **kwargs):
        super().__init__()
        self.speaker = speaker_module.SpeakerWorker()
        self.queue = asyncio.Queue()
        self.count = 0
        self.clear = Mock()

    def enqueue_audio_chunk(self, chunk, **kwargs):
        self.queue.put_nowait(chunk)

    async def process_audio_chunk(self):
        while True:
            chunk = await self.queue.get()
            self.count += 1
            await self.emit("stt", {
                "content": "测试语句", "embedding": np.array(chunk[:3]), "speech_ms": 2000,
                "audio_path": str(asr_server.DEFAULT_AUDIO_SAVE_DIR / f"preset-{self.count}.wav"),
            })


class PresetSyncTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profiles = VoiceprintClient(str(Path(self.temp.name) / "db.sqlite3"))
        self.connections = []
        self.results = asyncio.Queue()

        async def handler(ws):
            self.connections.append(ws)
            await asr_server.handle_connection(ws, client_factory=FakeAsrClient)

        self.listener = await websockets.serve(handler, "127.0.0.1", 0)
        self.addAsyncCleanup(self.stop_listener)
        self.url = f"ws://127.0.0.1:{self.listener.sockets[0].getsockname()[1]}/ws"
        self.client = PresetAsrClient(self.url, self.profiles, reconnect_delay=0.01)
        self.client.on("stt")(self.profiles.capture)
        self.client.on("stt")(self.results.put_nowait)
        self.addAsyncCleanup(self.client.close)

    async def stop_listener(self):
        self.listener.close()
        await self.listener.wait_closed()

    async def until(self, condition):
        async def wait():
            while not condition():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout=3)

    async def synced(self):
        await self.until(lambda: self.profiles.sync_state["connected"] and not self.profiles.sync_state["pending"])

    async def speech(self, vector=(1, 0, 0)):
        await self.client.send_audio(np.array(vector, dtype=np.float32))
        return await asyncio.wait_for(self.results.get(), timeout=3)

    async def test_enroll_and_delete_change_actual_asr_speaker_without_local_matching(self):
        await self.client.connect()
        await self.synced()
        first = await self.speech()
        self.assertEqual(first["speaker"], "speaker_1")
        self.profiles.enroll(first["audio_url"], "张三")
        await self.synced()
        self.assertEqual((await self.speech((0.99, 0.1, 0)))["speaker"], "张三")
        self.profiles.remove("张三")
        await self.synced()
        self.assertEqual((await self.speech())["speaker"], "speaker_1")

    async def test_saved_profiles_are_sent_on_start_and_again_after_reconnect(self):
        self.profiles.store.add("张三", [1, 0, 0])
        await self.client.connect()
        await self.synced()
        self.assertEqual((await self.speech())["speaker"], "张三")
        await self.connections[0].close()
        await self.until(lambda: len(self.connections) == 2)
        await self.synced()
        self.assertEqual((await self.speech())["speaker"], "张三")

    async def test_rejected_presets_are_visible_and_no_audio_is_sent_before_confirmation(self):
        await self.stop_listener()
        received = []

        async def old_server(ws):
            async for message in ws:
                received.append(message)
                await ws.send(json.dumps({"type": "error", "code": "binary_audio_required"}))

        self.listener = await websockets.serve(old_server, "127.0.0.1", 0)
        self.client.url = f"ws://127.0.0.1:{self.listener.sockets[0].getsockname()[1]}/ws"
        with self.assertLogs("aichat_sdk", "ERROR"):
            await self.client.connect()
            await self.client.send_audio(np.array([1, 0, 0], dtype=np.float32))
            await self.until(lambda: bool(self.profiles.sync_state["error"]))
            await self.client.close()
        self.assertIn("请更新 ASR", self.profiles.sync_state["error"])
        self.assertTrue(self.profiles.sync_state["pending"])
        self.assertTrue(received)
        self.assertTrue(all(isinstance(message, str) for message in received))


if __name__ == "__main__":
    unittest.main()
