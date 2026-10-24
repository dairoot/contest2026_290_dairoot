"""在本地 WebSocket 上验证 embedding 提取与原 ASR 协议，不加载真实模型。"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import websockets

import server as asr_server
from event_emitter import AsyncEventEmitter


def load_speaker_worker():
    """使用真实匹配逻辑，只替换模型后端，禁止加载模型或调用 NPU。"""
    spec = importlib.util.spec_from_file_location(
        "speaker_transport_test", Path(__file__).resolve().parents[1] / "speaker.py",
    )
    module = importlib.util.module_from_spec(spec)
    backend = SimpleNamespace(generate_embedding=Mock(side_effect=AssertionError("model must not run")))
    with patch("config.SPEAKER_BACKEND", "rknn"), \
            patch("utils._rknn.rknn_available", return_value=True), \
            patch.dict(sys.modules, {"speaker_rknn": backend}):
        spec.loader.exec_module(module)
    return module.SpeakerWorker


SpeakerWorker = load_speaker_worker()


class FakeAsrClient(AsyncEventEmitter):
    def __init__(self, **kwargs):
        super().__init__()
        self.queue = asyncio.Queue()
        self.chunks = []
        self.results = []
        self.speaker = SpeakerWorker()
        self.clear = Mock()

    def enqueue_audio_chunk(self, chunk, *, end_ts_ms):
        self.chunks.append((chunk.copy(), end_ts_ms))
        self.queue.put_nowait(chunk)

    async def process_audio_chunk(self):
        while True:
            await self.queue.get()
            payload = self.results.pop(0) if self.results else {
                "content": "模拟识别语句", "speaker_id": "original-id",
                "embedding": np.array([1, 0, 0]), "speech_ms": 2000, "elapsed": 0.2,
            }
            await self.emit("stt", payload)


class EmbeddingWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.worker = SimpleNamespace(generate_embedding=AsyncMock(return_value=np.array([0.1, 0.2, 0.3])))
        self.speaker_factory = Mock(return_value=self.worker)
        self.asr_client = FakeAsrClient()
        self.asr_factory = Mock(return_value=self.asr_client)

        async def handler(ws):
            if asr_server._request_path(ws) == asr_server.EMBEDDING_WS_PATH:
                await asr_server.handle_embedding_connection(ws, worker_factory=self.speaker_factory)
            else:
                await asr_server.handle_connection(ws, client_factory=self.asr_factory)

        self.listener = await websockets.serve(
            handler, "127.0.0.1", 0, process_request=asr_server.create_http_handler(),
        )
        self.url = f"ws://127.0.0.1:{self.listener.sockets[0].getsockname()[1]}"
        self.addAsyncCleanup(self.stop_server)

    async def stop_server(self):
        self.listener.close()
        await self.listener.wait_closed()

    async def receive(self, ws):
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=2))

    def pcm(self):
        return np.zeros(16000, dtype="<f4").tobytes()

    async def test_extracts_embedding_only_and_uses_same_three_second_window(self):
        async with websockets.connect(self.url + "/embedding", proxy=None) as ws:
            self.speaker_factory.assert_not_called()
            audio = np.tile(np.array([2, -2, 0.5], dtype="<f4"), 32000)
            await ws.send(audio.tobytes())
            result = await self.receive(ws)
            self.assertEqual(set(result), {"embedding"})
            np.testing.assert_allclose(result["embedding"], [0.1, 0.2, 0.3])
            actual = self.worker.generate_embedding.await_args.args[0]
            np.testing.assert_array_equal(actual, np.clip(audio[:48000], -1, 1))
            self.assertEqual(actual.dtype, np.float32)
            self.speaker_factory.assert_called_once()
            self.asr_factory.assert_not_called()

    async def test_invalid_or_short_audio_does_not_load_models(self):
        async with websockets.connect(self.url + "/embedding", proxy=None) as ws:
            for message, code in (
                ("text", "binary_audio_required"),
                (b"\x00\x01", "invalid_audio_size"),
                (np.array([np.nan], dtype="<f4").tobytes(), "invalid_audio_value"),
                (b"", "audio_too_short"),
                (np.zeros(100, dtype="<f4").tobytes(), "audio_too_short"),
                (np.zeros(15999, dtype="<f4").tobytes(), "audio_too_short"),
            ):
                with self.subTest(code=code):
                    await ws.send(message)
                    self.assertEqual((await self.receive(ws))["code"], code)
            self.speaker_factory.assert_not_called()
            self.asr_factory.assert_not_called()
            await ws.send(self.pcm())
            self.assertIn("embedding", await self.receive(ws))

    async def test_repeated_requests_reuse_worker_without_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "voiceprints.json"
            profile.write_text("must not be read or modified")
            with patch.dict(os.environ, {"SPEAKER_PROFILE_PATH": str(profile)}):
                async with websockets.connect(self.url + "/embedding", proxy=None) as ws:
                    for _ in range(2):
                        await ws.send(self.pcm())
                        self.assertEqual(set(await self.receive(ws)), {"embedding"})
            self.assertEqual(profile.read_text(), "must not be read or modified")
            self.assertEqual(list(Path(directory).iterdir()), [profile])
        self.speaker_factory.assert_called_once()
        self.assertEqual(self.worker.generate_embedding.await_count, 2)
        self.asr_factory.assert_not_called()

    async def test_inference_failure_can_be_retried(self):
        self.worker.generate_embedding.side_effect = [RuntimeError("test failure"), np.array([1, 2, 3])]
        async with websockets.connect(self.url + "/embedding", proxy=None) as ws:
            with self.assertLogs(asr_server.logger, level="ERROR"):
                await ws.send(self.pcm())
                self.assertEqual((await self.receive(ws))["code"], "embedding_failed")
            await ws.send(self.pcm())
            self.assertEqual((await self.receive(ws))["embedding"], [1, 2, 3])

    async def test_invalid_model_output_is_reported(self):
        self.worker.generate_embedding.return_value = np.array([np.nan])
        async with websockets.connect(self.url + "/embedding", proxy=None) as ws:
            with self.assertLogs(asr_server.logger, level="ERROR"):
                await ws.send(self.pcm())
                self.assertEqual((await self.receive(ws))["code"], "embedding_failed")

    async def test_asr_protocol_keeps_original_fields_and_no_profile_commands(self):
        async with websockets.connect(self.url + "/ws", proxy=None) as ws:
            await ws.send(json.dumps({"type": "voiceprints.list"}))
            self.assertEqual((await self.receive(ws))["code"], "binary_audio_required")
            await ws.send(b"\x00\x01")
            self.assertEqual((await self.receive(ws))["code"], "invalid_audio_size")
            await ws.send(np.array([np.nan], dtype="<f4").tobytes())
            self.assertEqual((await self.receive(ws))["code"], "invalid_audio_value")
            self.asr_factory.assert_not_called()
            await ws.send(np.array([2, -2, 0.5], dtype="<f4").tobytes())
            result = await self.receive(ws)
            self.assertEqual(result["type"], "stt")
            self.assertEqual(result["speaker_id"], "original-id")
            self.assertEqual(result["speaker"], "speaker_1")
            self.assertEqual(result["embedding"], [1, 0, 0])
            for field in ("id", "username", "can_enroll"):
                self.assertNotIn(field, result)
            np.testing.assert_array_equal(self.asr_client.chunks[0][0], [1, -1, 0.5])
            self.assertGreater(self.asr_client.chunks[0][1], 0)
            self.speaker_factory.assert_not_called()
        await self.stop_server()
        self.asr_client.clear.assert_called_once()

    async def test_speaker_labels_match_within_connection_and_short_speech_stays_unknown(self):
        self.asr_client.results = [
            {"content": "模拟语句", "embedding": np.array(vector), "speech_ms": duration}
            for vector, duration in (
                ([1, 0, 0], 999), ([1, 0, 0], 1000), ([0.99, 0.1, 0], 2000),
                ([0, 1, 0], 2000), ([0.1, 0.99, 0], 2000),
            )
        ]
        async with websockets.connect(self.url + "/ws", proxy=None) as ws:
            labels = []
            for _ in range(5):
                await ws.send(self.pcm())
                result = await self.receive(ws)
                labels.append(result["speaker"])
                self.assertNotIn("username", result)
            self.assertEqual(labels, ["unknown", "speaker_1", "speaker_1", "speaker_2", "speaker_2"])
        self.assertEqual({item["name"] for item in self.asr_client.speaker.speaker_list},
                         {"speaker_1", "speaker_2"})

    async def test_new_connection_starts_a_separate_speaker_list(self):
        first, second = FakeAsrClient(), FakeAsrClient()
        self.asr_factory.side_effect = [first, second]
        first.results = [
            {"content": "模拟语句", "embedding": np.array(vector), "speech_ms": 2000}
            for vector in ([1, 0, 0], [0, 1, 0])
        ]
        second.results = [{"content": "新连接语句", "embedding": np.array([0, 0, 1]), "speech_ms": 2000}]
        async with websockets.connect(self.url + "/ws", proxy=None) as ws:
            for expected in ("speaker_1", "speaker_2"):
                await ws.send(self.pcm())
                self.assertEqual((await self.receive(ws))["speaker"], expected)
        async with websockets.connect(self.url + "/ws", proxy=None) as ws:
            await ws.send(self.pcm())
            self.assertEqual((await self.receive(ws))["speaker"], "speaker_1")
        self.assertEqual(len({item["name"] for item in first.speaker.speaker_list}), 2)
        self.assertEqual(len({item["name"] for item in second.speaker.speaker_list}), 1)

    async def set_profiles(self, ws, profiles):
        await ws.send(json.dumps({"type": "set_speaker_profiles", "profiles": profiles}))
        return await self.receive(ws)

    async def test_set_presets_before_audio_returns_real_name_then_clear_restores_anonymous(self):
        async with websockets.connect(self.url + "/ws", proxy=None) as ws:
            result = await self.set_profiles(ws, [
                {"name": " 张三 ", "embedding": [3, 0, 0]},
                {"name": "张三", "embedding": [1, 0.1, 0]},
            ])
            self.assertEqual(result, {"type": "speaker_profiles_set", "ok": True, "speakers": 1, "samples": 2})
            self.asr_factory.assert_not_called()
            self.speaker_factory.assert_not_called()
            await ws.send(self.pcm())
            self.assertEqual((await self.receive(ws))["speaker"], "张三")
            self.assertEqual(self.asr_client.speaker.speaker_list, [])  # 预设不污染自动学习。
            self.assertEqual((await self.set_profiles(ws, []))["samples"], 0)
            await ws.send(self.pcm())
            self.assertEqual((await self.receive(ws))["speaker"], "speaker_1")

    async def test_invalid_preset_replacement_preserves_previous_names(self):
        async with websockets.connect(self.url + "/ws", proxy=None) as ws:
            await self.set_profiles(ws, [{"name": "张三", "embedding": [1, 0, 0]}])
            for invalid in (
                None, {}, [None], [{"name": "", "embedding": [1]}],
                [{"name": "名" * 41, "embedding": [1]}],
                [{"name": "张三", "embedding": [0, 0, 0]}],
                [{"name": "张三", "embedding": [float("nan"), 0]}],
                [{"name": "张三", "embedding": [1, 0]}, {"name": "李四", "embedding": [1]}],
                [{"name": "张三", "embedding": [1] * 4097}],
                [{"name": "张三", "embedding": [1, 0, 0]}] * 11,
                [{"name": "张三", "embedding": [1]}] * 1001,
            ):
                with self.subTest(invalid=str(invalid)[:80]):
                    self.assertEqual((await self.set_profiles(ws, invalid))["code"], "invalid_speaker_profiles")
            self.asr_factory.assert_not_called()
            await ws.send(self.pcm())
            self.assertEqual((await self.receive(ws))["speaker"], "张三")

    async def test_presets_override_existing_anonymous_matches_and_respect_short_speech(self):
        async with websockets.connect(self.url + "/ws", proxy=None) as ws:
            await ws.send(self.pcm())
            self.assertEqual((await self.receive(ws))["speaker"], "speaker_1")
            await self.set_profiles(ws, [{"name": "张三", "embedding": [1, 0, 0]}])
            self.asr_client.results = [
                {"content": "短句", "embedding": np.array([1, 0, 0]), "speech_ms": 999},
                {"content": "完整语句", "embedding": np.array([1, 0, 0]), "speech_ms": 1000},
                {"content": "其他人", "embedding": np.array([0, 1, 0]), "speech_ms": 2000},
            ]
            for expected in ("unknown", "张三", "speaker_2"):
                await ws.send(self.pcm())
                self.assertEqual((await self.receive(ws))["speaker"], expected)

    async def test_preset_names_do_not_leak_to_other_connections(self):
        self.asr_factory.side_effect = lambda **kwargs: FakeAsrClient()
        async with websockets.connect(self.url + "/ws", proxy=None) as first:
            await self.set_profiles(first, [{"name": "张三", "embedding": [1, 0, 0]}])
            await first.send(self.pcm())
            self.assertEqual((await self.receive(first))["speaker"], "张三")
            async with websockets.connect(self.url + "/ws", proxy=None) as second:
                await second.send(self.pcm())
                self.assertEqual((await self.receive(second))["speaker"], "speaker_1")
                # 同一个向量可以在不同客户端配置为不同姓名，更新和清空也互不影响。
                await self.set_profiles(second, [{"name": "李四", "embedding": [1, 0, 0]}])
                await first.send(self.pcm())
                self.assertEqual((await self.receive(first))["speaker"], "张三")
                await second.send(self.pcm())
                self.assertEqual((await self.receive(second))["speaker"], "李四")
                await self.set_profiles(first, [])
                await first.send(self.pcm())
                self.assertEqual((await self.receive(first))["speaker"], "speaker_1")
                await second.send(self.pcm())
                self.assertEqual((await self.receive(second))["speaker"], "李四")

    async def test_wrong_websocket_path_is_rejected(self):
        with self.assertRaises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(self.url + "/other", proxy=None):
                pass
        self.asr_factory.assert_not_called()
        self.speaker_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
