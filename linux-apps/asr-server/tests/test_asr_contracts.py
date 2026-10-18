"""当前音频队列与火山流式调用的离线回归，不加载模型或连接外部服务。"""

import asyncio
import contextlib
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import scipy.io.wavfile  # 在隔离 sys.modules 前加载原生扩展，避免反复初始化。

from asr_model import asr_volc


def load_client():
    path = Path(__file__).resolve().parents[1] / "asr_client.py"
    spec = importlib.util.spec_from_file_location("asr_contract_client", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "config": SimpleNamespace(ASR_MODEL_TYPE="fake", ASR_RKNN_PATH="unused"),
        "speaker": SimpleNamespace(SpeakerWorker=Mock()),
        "vad": SimpleNamespace(VadWorker=Mock()),
    }), patch("utils._rknn.rknn_available", return_value=False):
        spec.loader.exec_module(module)
    return module


class AudioQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_enqueued_audio_retains_capture_timestamp_when_processed(self):
        module = load_client()
        with patch.object(module, "get_asr_worker", return_value=Mock()):
            client = module.AsrClient()
        processed = asyncio.Event()

        async def receive(chunk, *, end_ts_ms):
            processed.set()

        chunk = np.zeros(3840, dtype=np.float32)
        with patch.object(client, "_send_audio_chunk", new=AsyncMock(side_effect=receive)) as send:
            client.enqueue_audio_chunk(chunk, end_ts_ms=123456.5)
            processor = asyncio.create_task(client.process_audio_chunk())
            try:
                await asyncio.wait_for(processed.wait(), timeout=1)
                send.assert_awaited_once_with(chunk, end_ts_ms=123456.5)
            finally:
                processor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await processor

    async def test_current_onnx_backend_loads_and_removed_alias_is_rejected(self):
        module = load_client()
        backend = SimpleNamespace(SenseVoiceOnnxWorker=Mock(return_value=object()))
        with patch.dict(sys.modules, {"asr_model.asr_sense_voice_onnx": backend}):
            self.assertIs(module.get_asr_worker("sense_voice"), backend.SenseVoiceOnnxWorker.return_value)
            with self.assertRaisesRegex(ValueError, "Invalid model_type"):
                module.get_asr_worker("sense_voice_onnx")
            backend.SenseVoiceOnnxWorker.assert_called_once_with()


class VolcStreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        credentials = patch.multiple(asr_volc, VOLC_APP_ID="test", VOLC_ACCESS_TOKEN="test")
        credentials.start()
        self.addCleanup(credentials.stop)
        self.worker = asr_volc.VolcAsrWorker()

    async def test_without_stream_does_not_upload_cached_audio(self):
        with patch.object(asr_volc.websockets, "connect", new=AsyncMock()) as connect:
            result = await self.worker.generate_text([np.zeros(3840, dtype=np.float32)])
        self.assertEqual(result, ("", 0.0, ""))
        connect.assert_not_called()

    async def test_stream_finishes_once_and_completed_result_is_reused(self):
        self.worker._ws = object()

        async def finish():
            self.worker._ws = None
            return {"result": {"text": "查一下天气。", "utterances": [{"additions": {"speaker": "1"}}]}}

        with patch.object(self.worker, "_finish_stream", new=AsyncMock(side_effect=finish)) as finish_stream, \
                patch.object(asr_volc.websockets, "connect", new=AsyncMock()) as connect:
            first = await self.worker.generate_text([])
            second = await self.worker.generate_text([])
        self.assertEqual(first[0], "查一下天气。")
        self.assertEqual(first[2], "1")
        self.assertEqual(first, second)
        finish_stream.assert_awaited_once_with()
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
