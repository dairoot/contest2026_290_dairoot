"""在板子上运行：uv run python -m unittest -v test_video_pipeline。

用 40 fps 的实时测试源模拟摄像头，故意让检测耗时 100 ms，验证旧帧被丢弃，
而不是阻塞输入或让延迟随时间累积。不需要摄像头登录态或 NPU 模型。
"""
import time
import unittest
from unittest.mock import patch

try:
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstVideo", "1.0")
    from gi.repository import Gst, GstVideo
except (ImportError, ValueError):
    Gst = GstVideo = None


@unittest.skipIf(Gst is None, "需要系统 GStreamer Python 绑定")
class LatestFramePipelineTest(unittest.TestCase):
    def test_slow_detection_drops_old_decoded_frames(self):
        import web

        Gst.init(None)
        pipeline = Gst.parse_launch(
            "videotestsrc is-live=true num-buffers=80 pattern=black "
            "! video/x-raw,format=NV12,width=64,height=64,framerate=40/1 "
            f"! {web.DECODED_QUEUE} "
            "! appsink name=sink sync=false max-buffers=1 drop=true enable-last-sample=false"
        )
        stats = {"output_frames": 0}
        published = []
        input_frames = []

        def slow_detection(frame):
            time.sleep(0.1)
            return frame

        def observe_input(pad, info):
            input_frames.append(time.monotonic())
            return Gst.PadProbeReturn.OK

        original_publish = web.publish_frame

        def observe_output(*args):
            original_publish(*args)
            published.append(dict(stats))

        pipeline.get_by_name("decoded").get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, observe_input
        )
        sink = pipeline.get_by_name("sink")
        sink.set_property("emit-signals", True)
        sink.connect("new-sample", web.on_hw_sample)

        with patch.multiple(
            web, Gst=Gst, GstVideo=GstVideo, gst_pipeline=pipeline,
            video_stats=stats, latest_frame=None, detect_and_draw=slow_detection,
            publish_frame=observe_output,
        ):
            try:
                pipeline.set_state(Gst.State.PLAYING)
                message = pipeline.get_bus().timed_pop_filtered(
                    8 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR
                )
                self.assertIsNotNone(message, "流水线没有及时结束")
                if message.type == Gst.MessageType.ERROR:
                    self.fail(str(message.parse_error()))
                self.assertEqual(len(input_frames), 80)
                self.assertLess(input_frames[-1] - input_frames[0], 3.0, "检测阻塞了输入")
                self.assertGreater(len(published), 10)
                self.assertLess(len(published), 35, "没有丢弃来不及处理的旧帧")
                self.assertTrue(web.latest_frame.startswith(b"\xff\xd8"))
                latencies = [s["pipeline_latency_ms"] for s in published]
                self.assertLess(max(latencies), 500, "输出帧已经过时")
                self.assertLess(latencies[-1] - latencies[0], 200, "延迟持续累积")
            finally:
                pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    unittest.main()
