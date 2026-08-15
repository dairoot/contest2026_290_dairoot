import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import websockets


SAMPLE_RATE = 16000
CHUNK_SAMPLES = SAMPLE_RATE * 240 // 1000


# Load the transport module directly so these protocol tests don't initialize
# the large VAD / ASR / speaker models imported by asr_client.
SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("asr_websocket_server", SERVER_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
asr_client_stub = types.ModuleType("asr_client")
asr_client_stub.AsrClient = object
original_asr_client = sys.modules.get("asr_client")
sys.modules["asr_client"] = asr_client_stub
try:
    SPEC.loader.exec_module(server)
finally:
    if original_asr_client is None:
        sys.modules.pop("asr_client", None)
    else:
        sys.modules["asr_client"] = original_asr_client


class FakeAsrClient:
    def __init__(self, processed: asyncio.Event, **kwargs):
        self.audio_queue = asyncio.Queue()
        self.handlers = {}
        self.processed = processed
        self.chunks = []
        self.chunk_end_timestamps = []
        self.clear_called = False
        self.options = kwargs
        self._loop = None

    def on(self, event_name):
        def register(handler):
            self.handlers[event_name] = handler
            return handler

        return register

    async def process_audio_chunk(self):
        while True:
            chunk = await self.audio_queue.get()
            self.chunks.append(chunk)
            audio_root = server._audio_root(self.options.get("audio_save_dir", ""))
            await self.handlers["stt"](
                {
                    "speaker_id": "",
                    "content": "测试",
                    "audio_path": str(audio_root / "测试.wav"),
                    "elapsed": np.float32(0.25),
                    "embedding": np.array([0.1, 0.2], dtype=np.float32),
                    "speech_ms": 1200.0,
                }
            )
            self.processed.set()

    def enqueue_audio_chunk(self, chunk, *, end_ts_ms=None):
        self.chunk_end_timestamps.append(end_ts_ms)
        self.audio_queue.put_nowait(chunk)

    def clear(self):
        self.clear_called = True


class FakeWebSocket:
    def __init__(self, messages, *, path="/ws", wait_for=None):
        self.messages = messages
        self.request = types.SimpleNamespace(path=path)
        self.wait_for = wait_for
        self.sent = []
        self.closed = None

    async def send(self, message):
        self.sent.append(message)

    async def close(self, *, code, reason):
        self.closed = (code, reason)

    async def __aiter__(self):
        for message in self.messages:
            yield message
        if self.wait_for is not None:
            await asyncio.wait_for(self.wait_for.wait(), timeout=1)


class ServerProtocolTest(unittest.IsolatedAsyncioTestCase):
    def test_logging_configuration_replaces_existing_handlers(self):
        with patch.object(server.logging, "basicConfig") as basic_config:
            server.configure_logging()

        basic_config.assert_called_once_with(
            level=server.logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            force=True,
        )

    async def test_real_http_audio_response(self):
        with tempfile.TemporaryDirectory() as audio_dir:
            audio = Path(audio_dir) / "测试.wav"
            audio.write_bytes(b"RIFF-test-audio")

            async def handler(websocket):
                await websocket.close()

            async with websockets.serve(
                handler,
                "127.0.0.1",
                0,
                process_request=server.create_http_handler(audio_dir),
            ) as ws_server:
                port = ws_server.sockets[0].getsockname()[1]
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(
                    (
                        "GET /audio/%E6%B5%8B%E8%AF%95.wav HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{port}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode()
                )
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), timeout=1)
                writer.close()
                await writer.wait_closed()

        self.assertIn(b"HTTP/1.1 200 OK", response)
        self.assertIn(b"Content-Type: audio/x-wav", response)
        self.assertTrue(response.endswith(b"RIFF-test-audio"))

    async def test_real_websocket_round_trip(self):
        processed = asyncio.Event()
        created = []

        def factory(**kwargs):
            client = FakeAsrClient(processed, **kwargs)
            created.append(client)
            return client

        async def handler(websocket):
            await server.handle_connection(websocket, client_factory=factory)

        async with websockets.serve(handler, "127.0.0.1", 0) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as websocket:
                await websocket.send(
                    np.zeros(CHUNK_SAMPLES, dtype="<f4").tobytes()
                )
                response = json.loads(await asyncio.wait_for(websocket.recv(), timeout=1))

        self.assertEqual(response["type"], "stt")
        self.assertEqual(response["content"], "测试")
        self.assertEqual(response["audio_url"], "/audio/%E6%B5%8B%E8%AF%95.wav")
        self.assertNotIn("audio_path", response)
        self.assertEqual(len(created[0].chunks), 1)
        self.assertEqual(created[0].options["model_type"], "volc")
        self.assertTrue(created[0].clear_called)

    async def test_forwards_each_audio_frame_without_reblocking(self):
        processed = asyncio.Event()
        created = []

        def factory(**kwargs):
            client = FakeAsrClient(processed, **kwargs)
            created.append(client)
            return client

        samples = np.linspace(-1.2, 1.2, 1000, dtype="<f4")
        websocket = FakeWebSocket(
            [samples.tobytes()],
            path="/ws?client=test",
            wait_for=processed,
        )

        await server.handle_connection(
            websocket,
            model_type="volc",
            slow_reply_silence_duration_ms=450,
            audio_save_dir="/tmp/asr-audio",
            client_factory=factory,
        )

        self.assertEqual(len(created), 1)
        client = created[0]
        self.assertEqual(client.options["model_type"], "volc")
        self.assertEqual(client.options["slow_reply_silence_duration_ms"], 450)
        self.assertEqual(client.options["audio_save_dir"], "/tmp/asr-audio")
        self.assertTrue(client.clear_called)
        self.assertEqual(len(client.chunks), 1)
        np.testing.assert_allclose(client.chunks[0], np.clip(samples, -1.0, 1.0))

        self.assertEqual(len(websocket.sent), 1)
        response = json.loads(websocket.sent[0])
        self.assertEqual(response["type"], "stt")
        self.assertEqual(response["content"], "测试")
        self.assertEqual(response["audio_url"], "/audio/%E6%B5%8B%E8%AF%95.wav")
        self.assertNotIn("audio_path", response)
        self.assertEqual(response["embedding"], [0.10000000149011612, 0.20000000298023224])
        self.assertEqual(response["elapsed"], 0.25)

    async def test_multi_chunk_frame_is_forwarded_as_one_frame(self):
        processed = asyncio.Event()
        created = []

        def factory(**kwargs):
            client = FakeAsrClient(processed, **kwargs)
            created.append(client)
            return client

        samples = np.zeros(CHUNK_SAMPLES * 2, dtype="<f4")
        raw = samples.tobytes()
        websocket = FakeWebSocket([raw], wait_for=processed)
        with patch.object(server.time, "time", return_value=1000.0):
            await server.handle_connection(websocket, client_factory=factory)

        self.assertEqual(len(created[0].chunks), 1)
        np.testing.assert_array_equal(created[0].chunks[0], samples)
        self.assertEqual(created[0].chunk_end_timestamps, [1000000.0])

    async def test_reports_text_and_malformed_binary_frames(self):
        processed = asyncio.Event()
        client = FakeAsrClient(processed)
        websocket = FakeWebSocket(["not audio", b"\x00\x01\x02"])

        await server.handle_connection(
            websocket,
            client_factory=lambda **kwargs: client,
        )

        errors = [json.loads(message) for message in websocket.sent]
        self.assertEqual(
            [error["code"] for error in errors],
            ["binary_audio_required", "invalid_audio_size"],
        )
        self.assertEqual(client.chunks, [])
        self.assertTrue(client.clear_called)

    async def test_rejects_unknown_websocket_path_before_creating_client(self):
        websocket = FakeWebSocket([], path="/not-asr")

        await server.handle_connection(
            websocket,
            client_factory=lambda **kwargs: self.fail("client must not be created"),
        )

        self.assertEqual(websocket.closed, (1008, "WebSocket path must be /ws"))

    def test_serialize_stt_rejects_non_finite_values(self):
        with self.assertRaises(ValueError):
            server.serialize_stt({"elapsed": np.float32(np.nan)})

    def test_http_handler_rejects_path_traversal_and_non_wav_files(self):
        with tempfile.TemporaryDirectory() as audio_dir:
            handler = server.create_http_handler(audio_dir)

            traversal = handler(
                None,
                types.SimpleNamespace(path="/audio/%2E%2E/secret.wav"),
            )
            non_wav = handler(
                None,
                types.SimpleNamespace(path="/audio/notes.txt"),
            )

        self.assertEqual(traversal.status_code, 403)
        self.assertEqual(non_wav.status_code, 404)


if __name__ == "__main__":
    unittest.main()
