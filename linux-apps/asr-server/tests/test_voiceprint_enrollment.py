"""声纹持久化与从识别消息录入的回归，不加载模型或使用麦克风。"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import numpy as np

from voiceprint_store import MAX_SAMPLES_PER_NAME, VoiceprintStore


class VoiceprintStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "profiles" / "voiceprints.json"
        self.store = VoiceprintStore(self.path)

    def test_round_trip_and_multiple_samples(self):
        self.store.add(" 张三 ", [3, 4, 0])
        self.store.add("张三", [2, 4, 1])
        restored = VoiceprintStore(self.path)
        restored.load()
        self.assertEqual(restored.summary(), [{"name": "张三", "samples": 2}])
        np.testing.assert_allclose(restored.samples[0]["embedding"], [0.6, 0.8, 0])

    def test_duplicate_sample_and_conflicting_names(self):
        self.assertTrue(self.store.add("张三", [1, 2, 3]))
        self.assertFalse(self.store.add("张三", [2, 4, 6]))
        with self.assertRaisesRegex(ValueError, "已录入"):
            self.store.add("李四", [1, 2, 3])
        self.assertEqual(self.store.summary(), [{"name": "张三", "samples": 1}])

    def test_bad_names_vectors_and_incompatible_dimensions(self):
        for name in (None, "", " \t ", "a" * 41, "张\n三"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.add(name, [1, 0])
        for vector in ([], [0, 0], [float("nan"), 1], [float("inf"), 1], [[1, 2]]):
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                self.store.add("张三", vector)
        self.store.add("张三", [1, 0])
        with self.assertRaisesRegex(ValueError, "维度"):
            self.store.add("张三", [1, 0, 0])

    def test_sample_limit(self):
        for index in range(MAX_SAMPLES_PER_NAME):
            self.store.add("张三", [1, index + 1])
        with self.assertRaisesRegex(ValueError, "10 条"):
            self.store.add("张三", [1, 100])

    def test_save_failure_preserves_file_and_memory(self):
        self.store.add("张三", [1, 0])
        original = self.path.read_bytes()
        with patch("voiceprint_store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.add("李四", [0, 1])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.store.summary(), [{"name": "张三", "samples": 1}])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_corrupt_file_is_not_overwritten(self):
        self.path.parent.mkdir()
        self.path.write_text('{"version": 1, "speakers": [null]}')
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.load()
        self.assertEqual(self.path.read_bytes(), original)


class EnrollmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = Path(__file__).parent / "asr_wsv2" / "server.py"
        spec = importlib.util.spec_from_file_location("voiceprint_demo", path)
        self.demo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.demo)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.demo.voiceprints = VoiceprintStore(Path(self.directory.name) / "voiceprints.json")
        self.worker = Mock()
        self.worker.get_speaker_name.return_value = ("speaker_1", -1.0)
        self.worker.classify.return_value = ("张三", 0.95)
        self.client = SimpleNamespace(speaker=self.worker)
        self.ws = MagicMock()
        self.ws.send = AsyncMock()
        self.demo.clients.add(self.ws)

    async def add_message(self, **overrides):
        payload = {
            "embedding": np.array([1, 0, 0], dtype=np.float32),
            "speech_ms": 2000, "content": "你好，这是一段声纹录入测试", "elapsed": 0.2,
            **overrides,
        }
        await self.demo.handle_stt(self.client, payload)
        return self.demo.results[-1]

    def replies(self):
        return [json.loads(call.args[0]) for call in self.ws.send.call_args_list]

    async def enroll(self, result_id, name="张三"):
        await self.demo.handle_enrollment(self.ws, json.dumps({
            "type": "enroll", "result_id": result_id, "name": name,
        }))
        return self.replies()[-1]

    async def test_enroll_selected_message_and_match_future_speech(self):
        result = await self.add_message()
        self.assertTrue(result["can_enroll"])
        self.assertNotIn("embedding", result)
        reply = await self.enroll(result["id"])
        self.assertTrue(reply["ok"])
        self.assertEqual(result["speaker"], "张三")
        restored = VoiceprintStore(self.demo.voiceprints.path)
        restored.load()
        self.assertEqual(restored.summary(), [{"name": "张三", "samples": 1}])
        next_result = await self.add_message()
        self.assertEqual(next_result["speaker"], "张三")
        self.worker.classify.assert_called_once()
        self.worker.get_speaker_name.assert_called_once()
        candidates = self.worker.classify.call_args.args[1]
        self.assertEqual(candidates[0]["name"], "张三")
        for message in self.replies():
            self.assertNotIn("embedding", json.dumps(message))

    async def test_short_speech_and_invalid_embedding_cannot_be_enrolled(self):
        for overrides in ({"speech_ms": 1000}, {"embedding": [0, 0, 0]}):
            with self.subTest(overrides=overrides):
                result = await self.add_message(**overrides)
                self.assertFalse(result["can_enroll"])
                self.assertNotIn(result["id"], self.demo.result_embeddings)
                reply = await self.enroll(result["id"])
                self.assertFalse(reply["ok"])
        self.assertFalse(self.demo.voiceprints.path.exists())

    async def test_saved_profiles_match_after_restart(self):
        result = await self.add_message()
        await self.enroll(result["id"])
        restored = VoiceprintStore(self.demo.voiceprints.path)
        restored.load()
        self.demo.voiceprints = restored
        self.demo.results.clear()
        self.demo.result_embeddings.clear()
        self.worker.get_speaker_name.reset_mock()
        next_result = await self.add_message()
        self.assertEqual(next_result["speaker"], "张三")
        self.worker.get_speaker_name.assert_not_called()

    async def test_unmatched_and_short_speech_do_not_use_registered_name(self):
        self.demo.voiceprints.add("张三", [1, 0, 0])
        self.worker.classify.return_value = (None, 0.2)
        result = await self.add_message(embedding=[0, 1, 0])
        self.assertEqual(result["speaker"], "speaker_1")
        self.worker.classify.reset_mock()
        self.worker.get_speaker_name.return_value = ("unknown", -1.0)
        short_result = await self.add_message(speech_ms=500)
        self.assertEqual(short_result["speaker"], "unknown")
        self.worker.classify.assert_not_called()

    async def test_unknown_id_and_malformed_commands(self):
        for command in (b"audio", "{", "null", "[]", '{}', '{"type":"enroll","result_id":[]}', '{"type":"enroll","result_id":"missing","name":"张三"}'):
            with self.subTest(command=command):
                await self.demo.handle_enrollment(self.ws, command)
                self.assertFalse(self.replies()[-1]["ok"])
        self.assertFalse(self.demo.voiceprints.path.exists())

    async def test_failed_save_does_not_relabel_message(self):
        result = await self.add_message()
        with patch.object(self.demo.voiceprints, "_save", side_effect=OSError("disk full")):
            reply = await self.enroll(result["id"])
        self.assertFalse(reply["ok"])
        self.assertEqual(result["speaker"], "speaker_1")
        self.assertEqual(self.demo.voiceprints.summary(), [])

    async def test_existing_file_error_disables_enrollment(self):
        result = await self.add_message()
        self.demo.voiceprints.path.write_text("invalid json")
        reply = await self.enroll(result["id"])
        self.assertFalse(reply["ok"])
        self.assertIn("无法加载声纹文件", reply["message"])

    async def test_new_connections_receive_history_and_enrollment_updates(self):
        result = await self.add_message()
        peer = MagicMock()
        peer.send = AsyncMock()
        peer.__aiter__.return_value = [json.dumps({"type": "enroll", "result_id": result["id"], "name": "张三"})]
        await self.demo.handle_connection(peer)
        messages = [json.loads(call.args[0]) for call in peer.send.call_args_list]
        self.assertEqual(messages[0]["id"], result["id"])
        self.assertEqual(messages[1]["type"], "voiceprints")
        update = [message for message in self.replies() if message.get("type") == "voiceprints"][-1]
        self.assertEqual(update["name"], "张三")
        self.assertEqual(update["speakers"], [{"name": "张三", "samples": 1}])


if __name__ == "__main__":
    unittest.main()
