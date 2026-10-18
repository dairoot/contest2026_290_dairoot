"""服务端采集后端的离线回归，不加载模型或打开真实麦克风。"""

import asyncio
import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import numpy as np


def load_demo():
    path = Path(__file__).parent / "asr_wsv2" / "server.py"
    spec = importlib.util.spec_from_file_location("asr_wsv2_capture_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def cancel_task(task):
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.demo = load_demo()
        self.demo.MIC_BACKEND = "sounddevice"
        self.demo.MIC_DEVICE = ""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.demo.voiceprints = self.demo.VoiceprintStore(Path(self.directory.name) / "voiceprints.json")
        self.processor_started = asyncio.Event()
        self.processor_stopped = asyncio.Event()

        async def process_audio():
            self.processor_started.set()
            try:
                await asyncio.Future()
            finally:
                self.processor_stopped.set()

        self.client = Mock()
        self.client.on.side_effect = lambda event: lambda handler: handler
        self.client.process_audio_chunk = AsyncMock(side_effect=process_audio)
        self.asr_module = types.ModuleType("asr_client")
        self.asr_module.AsrClient = Mock(return_value=self.client)

        self.stream_started = asyncio.Event()
        self.stream = Mock(device=0)
        self.stream.start.side_effect = self.stream_started.set
        self.sd = types.ModuleType("sounddevice")
        self.sd.PortAudioError = type("PortAudioError", (Exception,), {})
        self.sd.InputStream = Mock(return_value=self.stream)
        self.sd.query_devices = Mock(return_value={"name": "Test microphone"})

        modules = patch.dict(sys.modules, {"asr_client": self.asr_module, "sounddevice": self.sd})
        modules.start()
        self.addCleanup(modules.stop)

    async def start_capture(self):
        task = asyncio.create_task(self.demo.capture_loop())
        self.addAsyncCleanup(cancel_task, task)
        await asyncio.wait_for(self.stream_started.wait(), timeout=1)
        return task

    async def test_platform_defaults(self):
        for platform, backend, device in (
            ("darwin", "sounddevice", ""),
            ("linux", "arecord", "hw:rpmsgmic,0"),
        ):
            with self.subTest(platform=platform), patch.dict(os.environ):
                os.environ.pop("MIC_DEVICE", None)
                with patch.object(sys, "platform", platform), patch("dotenv.load_dotenv"):
                    demo = load_demo()
                self.assertEqual(demo.MIC_BACKEND, backend)
                self.assertEqual(demo.MIC_DEVICE, device)

    async def test_mac_callback_and_cancellation(self):
        with patch.object(self.demo.shutil, "which", side_effect=AssertionError("Mac must not use arecord")):
            task = await self.start_capture()
        options = self.sd.InputStream.call_args.kwargs
        self.assertIsNone(options["device"])
        self.assertEqual((options["samplerate"], options["channels"], options["dtype"]), (16000, 1, "float32"))
        pcm = np.linspace(-1, 1, 3840, dtype=np.float32).reshape(-1, 1)
        await asyncio.to_thread(options["callback"], pcm, len(pcm), None, False)
        self.assertIs(self.client.audio_callback.call_args.args[0], pcm)
        self.assertEqual(self.client.audio_callback.call_args.args[1:], (3840, None, False))
        await cancel_task(task)
        self.stream.close.assert_called_once()
        self.client.clear.assert_called_once()
        self.assertTrue(self.processor_stopped.is_set())

    async def test_mac_device_id_and_name(self):
        for value, expected in (("3", 3), ("USB Microphone", "USB Microphone")):
            with self.subTest(device=value):
                self.demo.MIC_DEVICE = value
                self.stream_started.clear()
                task = await self.start_capture()
                self.assertEqual(self.sd.InputStream.call_args.kwargs["device"], expected)
                await cancel_task(task)

    async def test_mac_start_failure_closes_stream_and_reports_to_clients(self):
        self.stream.start.side_effect = self.sd.PortAudioError("access denied")
        current = MagicMock()
        current.send = AsyncMock()
        self.demo.clients.add(current)
        await self.demo.capture_loop()
        self.stream.close.assert_called_once()
        self.client.clear.assert_called_once()
        message = json.loads(current.send.call_args.args[0])
        self.assertEqual(message["type"], "error")
        self.assertIn("隐私与安全性", message["message"])
        late = MagicMock()
        late.send = AsyncMock()
        late.__aiter__.return_value = []
        await self.demo.handle_connection(late)
        self.assertEqual(json.loads(late.send.call_args.args[0]), message)

    async def test_mac_unexpected_stream_stop(self):
        task = await self.start_capture()
        result = await asyncio.to_thread(self.sd.InputStream.call_args.kwargs["finished_callback"])
        self.assertIsNone(result)  # PortAudio 的 CFFI void 回调要求返回 None。
        await asyncio.wait_for(task, timeout=1)
        self.assertIn("音频流意外停止", self.demo.capture_error)
        self.stream.close.assert_called_once()
        self.assertTrue(self.processor_stopped.is_set())

    async def test_missing_sounddevice_does_not_load_models(self):
        with patch.dict(sys.modules, {"sounddevice": None}):
            await self.demo.capture_loop()
        self.assertIn("uv sync", self.demo.capture_error)
        self.asr_module.AsrClient.assert_not_called()

    async def test_linux_missing_arecord_does_not_load_models(self):
        self.demo.MIC_BACKEND = "arecord"
        with patch.object(self.demo.shutil, "which", return_value=None):
            await self.demo.capture_loop()
        self.assertIn("alsa-utils", self.demo.capture_error)
        self.asr_module.AsrClient.assert_not_called()

    async def test_linux_pcm_and_recorder_failure(self):
        self.demo.MIC_BACKEND = "arecord"
        self.demo.MIC_DEVICE = "hw:rpmsgmic,0"
        pcm = np.tile(np.array([-32768, 0, 32767], dtype="<i2"), 1280)
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        stdout.feed_data(pcm.tobytes())
        stdout.feed_eof()
        stderr.feed_data(b"device busy")
        stderr.feed_eof()
        proc = types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=1, kill=Mock(), wait=AsyncMock())
        spawn = AsyncMock(return_value=proc)
        with patch.object(self.demo.shutil, "which", return_value="/usr/bin/arecord"), patch.object(self.demo.asyncio, "create_subprocess_exec", new=spawn):
            await self.demo.capture_loop()
        options = spawn.call_args.args
        self.assertEqual(options[:3], ("arecord", "-D", "hw:rpmsgmic,0"))
        np.testing.assert_array_equal(self.client.enqueue_audio_chunk.call_args.args[0], pcm.astype(np.float32) / 32768.0)
        self.assertIn("device busy", self.demo.capture_error)
        proc.wait.assert_awaited_once()
        proc.kill.assert_not_called()
        self.client.clear.assert_called_once()
        self.sd.InputStream.assert_not_called()

    async def test_linux_cancellation_stops_recorder(self):
        self.demo.MIC_BACKEND = "arecord"
        proc = types.SimpleNamespace(stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader(), returncode=None, kill=Mock(), wait=AsyncMock())
        with patch.object(self.demo.shutil, "which", return_value="/usr/bin/arecord"), patch.object(self.demo.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)):
            task = asyncio.create_task(self.demo.capture_loop())
            self.addAsyncCleanup(cancel_task, task)
            await asyncio.wait_for(self.processor_started.wait(), timeout=1)
            await cancel_task(task)
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()
        self.client.clear.assert_called_once()
        self.assertTrue(self.processor_stopped.is_set())


if __name__ == "__main__":
    unittest.main()
