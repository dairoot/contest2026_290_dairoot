import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent / "mcp"))
import volume_mcp


class VolumeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.percent = 30
        self.muted = False
        self.pactl = AsyncMock(side_effect=self.fake_pactl)
        self.patch = patch.multiple(volume_mcp, _pactl=self.pactl, _volume_lock=asyncio.Lock())
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def fake_pactl(self, *args):
        await asyncio.sleep(0)  # 让并发测试确实能交错执行
        if args == ("get-default-sink",):
            return "speaker\n"
        if args == ("--format=json", "list", "sinks"):
            return json.dumps([{
                "name": "speaker", "mute": self.muted,
                "volume": {"front-left": {"value": round(self.percent * 65536 / 100)}},
            }])
        if args[:2] == ("set-sink-volume", "speaker"):
            self.percent = int(args[2].removesuffix("%"))
            return ""
        if args[:2] == ("set-sink-mute", "speaker"):
            self.muted = args[2] == "1"
            return ""
        self.fail(f"Unexpected pactl command: {args}")

    async def test_get_is_read_only(self):
        result = await volume_mcp.speaker_volume()
        self.assertEqual(result, {"isError": False, "sink": "speaker", "volume_percent": 30, "muted": False})
        self.assertEqual(self.pactl.await_count, 2)

    async def test_set_reads_back_and_preserves_mute(self):
        self.muted = True
        result = await volume_mcp.speaker_volume("set", 45)
        self.assertEqual(result["volume_percent"], 45)
        self.assertTrue(result["muted"])
        self.pactl.assert_any_await("set-sink-volume", "speaker", "45%")
        self.assertEqual(self.pactl.await_args_list[-1].args, ("--format=json", "list", "sinks"))

    async def test_adjustment_defaults_and_limits(self):
        self.assertEqual((await volume_mcp.speaker_volume("increase"))["volume_percent"], 40)
        self.assertEqual((await volume_mcp.speaker_volume("decrease", 0))["volume_percent"], 40)
        self.assertEqual((await volume_mcp.speaker_volume("increase", 100))["volume_percent"], 100)
        self.assertEqual((await volume_mcp.speaker_volume("decrease", 100))["volume_percent"], 0)

    async def test_concurrent_adjustments_do_not_lose_updates(self):
        results = await asyncio.gather(*(volume_mcp.speaker_volume("increase", 1) for _ in range(10)))
        self.assertTrue(all(not r["isError"] for r in results))
        self.assertEqual(self.percent, 40)

    async def test_mute_preserves_volume(self):
        self.assertTrue((await volume_mcp.speaker_volume("mute"))["muted"])
        self.assertFalse((await volume_mcp.speaker_volume("unmute"))["muted"])
        self.assertEqual(self.percent, 30)

    async def test_invalid_combinations_do_not_touch_audio(self):
        self.assertTrue((await volume_mcp.speaker_volume("set"))["isError"])
        self.assertTrue((await volume_mcp.speaker_volume("get", 50))["isError"])
        self.pactl.assert_not_awaited()

    async def test_backend_error_is_reported(self):
        self.pactl.side_effect = RuntimeError("PulseAudio unavailable")
        result = await volume_mcp.speaker_volume("set", 20)
        self.assertTrue(result["isError"])
        self.assertIn("PulseAudio unavailable", result["error"])

    async def test_mcp_validation_rejects_invalid_values_before_commands(self):
        async with Client(volume_mcp.mcp) as client:
            tools = await client.list_tools()
            self.assertEqual([t.name for t in tools], ["speaker_volume"])
            for args in ({"action": "set", "percent": -1}, {"action": "set", "percent": 101},
                         {"action": "set", "percent": "50; touch /tmp/no"}, {"action": "invalid"}):
                result = await client.call_tool("speaker_volume", args, raise_on_error=False)
                self.assertTrue(result.is_error)
            self.pactl.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
