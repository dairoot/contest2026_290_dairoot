import asyncio
import os
import sys
import time
from html import escape
from threading import Lock

import cv2
import numpy as np
from aiohttp import web
from av.packet import Packet
from av.video.codeccontext import VideoCodecContext

from miloco_sdk import XiaomiClient
from miloco_sdk.cli.utils import print_device_list
from miloco_sdk.utils.types import MIoTCameraVideoQuality

# 全局变量用于视频解码和显示
video_decoder = None
detect_and_draw = None  # yolo 模式下为检测函数：吃 BGR 帧，返回画好框的 BGR 帧
# HTTP 客户端只读取最新 JPEG；解码后的丢帧由 GStreamer 的独立 queue 完成。
frame_lock = Lock()
latest_frame = None
started_at = time.monotonic()
received_packets = 0
video_stats = {
    "output_frames": 0,
    "processing_ms": None,
    "pipeline_latency_ms": None,
    "published_at": None,
}
xiaomi_client = None  # 登录后的 SDK client，设备列表 / 开关接口用它
camera_name = ""  # 正在拉流的摄像头名字，页头徽章用

# Rockchip VPU 硬解输出尺寸（VPU 内部用 RGA 缩放），设成 0 表示保持摄像头原始分辨率。
# 拉的是 LOW 档码流，放大到 1080p 只是让后面的 cvtColor/imencode 为插值出来的像素买单
HW_WIDTH, HW_HEIGHT = 0, 0
Gst = None
GstVideo = None
gst_pipeline = None
gst_appsrc = None  # 为 None 时表示没有硬解，回退到 PyAV 软解

# queue 创建独立的下游线程。只丢已解码的旧帧，不能随意丢 H.265 参考帧。
# 单独设 appsink drop=true 不够：new-sample 回调本身会阻塞它的 streaming thread。
DECODED_QUEUE = (
    "queue name=decoded max-size-buffers=1 max-size-bytes=0 "
    "max-size-time=0 leaky=downstream"
)

# 页面模板放在同目录的 index.html 里，改 UI 不用动 Python。每次请求现读，
# 板子上调样式存盘刷新就行，不用重启（顺带丢掉进程内的一份缓存，文件才 10K）
INDEX_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")


def rockchip_soc() -> str:
    """在 Rockchip 板子上返回芯片名（rk3576），其他机器返回空串。

    /proc/device-tree/compatible 是 NUL 分隔的一串，板子上是
    "rockchip,rk3576-evb1-v10\0rockchip,rk3576"；Mac / x86 上根本没有这个文件。
    """
    try:
        with open("/proc/device-tree/compatible", "rb") as f:
            entries = f.read().decode().split("\0")
    except OSError:
        return ""
    for entry in entries:
        vendor, _, soc = entry.partition(",")
        if vendor == "rockchip" and soc and "-" not in soc:  # 带 -evb1-v10 的是板型不是芯片
            return soc
    return ""


def nv12_from_buffer(buffer, info, width, height):
    """把 VPU 的 NV12 buffer 取成紧凑的 (height*3/2, width) 数组。

    VPU 写出来的每行是对齐过的——848 宽的帧实际每行 960 字节，右边是 padding；
    帧高不是 16 的倍数时行数也会多出来。真实行宽和两个平面的偏移都在 GstVideoMeta
    里，照它取再把 padding 切掉，按 caps 的 width/height 直接 reshape 会 ValueError。
    """
    meta = GstVideo.buffer_get_video_meta(buffer)
    if meta is None:
        # 理论上不会走到：带 padding 的 buffer 必须附 meta，否则下游没法解释它
        y_stride = uv_stride = width
        y_off, uv_off = 0, width * height
    else:
        y_stride, uv_stride = meta.stride[0], meta.stride[1]
        y_off, uv_off = meta.offset[0], meta.offset[1]

    buf = np.frombuffer(info.data, dtype=np.uint8)
    y = buf[y_off : y_off + y_stride * height].reshape(height, y_stride)[:, :width]
    uv = buf[uv_off : uv_off + uv_stride * (height // 2)].reshape(height // 2, uv_stride)[:, :width]
    return np.vstack((y, uv))


def publish_frame(frame_data, processing_started, pipeline_latency_ms=None):
    """一次发布 JPEG 和对应统计，客户端不会排队等历史帧。"""
    global latest_frame

    now = time.monotonic()
    with frame_lock:
        latest_frame = frame_data
        video_stats["output_frames"] += 1
        video_stats["processing_ms"] = (now - processing_started) * 1000
        video_stats["pipeline_latency_ms"] = pipeline_latency_ms
        video_stats["published_at"] = now


def on_hw_sample(sink):
    """在 decoded queue 的下游线程推理，不阻塞 VPU 继续解码和丢弃旧帧。"""
    sample = sink.emit("pull-sample")
    if sample is None:
        return Gst.FlowReturn.OK

    processing_started = time.monotonic()
    buffer = sample.get_buffer()
    ok, info = buffer.map(Gst.MapFlags.READ)
    if not ok:
        return Gst.FlowReturn.OK
    try:
        if detect_and_draw:
            # YOLO 分支：VPU 出的是 NV12 裸帧，转成 BGR 后检测，再软编 JPEG
            caps = sample.get_caps().get_structure(0)
            width, height = caps.get_value("width"), caps.get_value("height")
            nv12 = nv12_from_buffer(buffer, info, width, height)
            bgr_frame = detect_and_draw(cv2.cvtColor(nv12, cv2.COLOR_YUV2BGR_NV12))
            _, buf = cv2.imencode(".jpg", bgr_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            frame_data = buf.tobytes()
        else:
            # VPU 已经编好 JPEG，直接用
            frame_data = bytes(info.data)
    finally:
        buffer.unmap(info)

    # appsrc 的 do-timestamp 为包到达服务器时的流水线时间；不含摄像头和网络耗时。
    latency_ms = None
    clock = gst_pipeline.get_clock() if gst_pipeline is not None else None
    if clock is not None and buffer.pts != Gst.CLOCK_TIME_NONE:
        running_time = clock.get_time() - gst_pipeline.get_base_time()
        latency_ms = max(0, running_time - buffer.pts) / Gst.MSECOND
    publish_frame(frame_data, processing_started, latency_ms)
    return Gst.FlowReturn.OK


def init_hw_decoder():
    """尝试启动 Rockchip VPU 硬解流水线，不可用时打出卡在哪一步并返回 None。"""
    global Gst, GstVideo, gst_pipeline

    try:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gst as _Gst
        from gi.repository import GstVideo as _GstVideo
    except (ImportError, ValueError) as e:
        # 板子上 python3-gi 是 apt 装在系统 python 里的，venv 不放行系统包就 import 不到
        print(f"硬解不可用：GStreamer 的 Python 绑定导不进来（{e}）")
        return None

    Gst, GstVideo = _Gst, _GstVideo
    Gst.init(None)
    if Gst.ElementFactory.make("mppvideodec") is None:
        print("硬解不可用：GStreamer 里没有 mppvideodec 元件（缺 gstreamer1.0-rockchip1）")
        return None

    # 有 YOLO 时要拿裸帧做检测，否则让 VPU 顺手把 JPEG 也编好
    tail = "video/x-raw,format=NV12" if detect_and_draw else "mppjpegenc q-factor=80"
    gst_pipeline = Gst.parse_launch(
        "appsrc name=src is-live=true do-timestamp=true format=time "
        'caps="video/x-h265,stream-format=byte-stream,alignment=au,parsed=true" '
        f"! mppvideodec width={HW_WIDTH} height={HW_HEIGHT} ! {DECODED_QUEUE} ! {tail} "
        "! appsink name=sink sync=false max-buffers=1 drop=true enable-last-sample=false"
    )
    sink = gst_pipeline.get_by_name("sink")
    sink.set_property("emit-signals", True)
    sink.connect("new-sample", on_hw_sample)
    gst_pipeline.set_state(Gst.State.PLAYING)
    size = f"{HW_WIDTH}x{HW_HEIGHT}" if HW_WIDTH and HW_HEIGHT else "原始分辨率"
    print(f"已启用 Rockchip VPU 硬解，输出 {size}")
    return gst_pipeline.get_by_name("src")


async def on_raw_video(did: str, data: bytes, ts: int, seq: int, channel: int):
    global video_decoder, received_packets

    received_packets += 1

    if gst_appsrc is not None:
        # 硬解：只把码流丢给 VPU，事件循环不做任何解码工作
        gst_appsrc.emit("push-buffer", Gst.Buffer.new_wrapped(data))
        return

    # 首次调用时创建 HEVC 解码器
    if video_decoder is None:
        video_decoder = VideoCodecContext.create("hevc", "r")
        print("已创建 HEVC 视频解码器")

    # 解码视频帧
    pkt = Packet(data)
    frames = video_decoder.decode(pkt)

    for frame in frames:
        processing_started = time.monotonic()
        # 转换为 BGR 格式 (OpenCV 使用 BGR)
        bgr_frame = frame.to_ndarray(format="bgr24")

        if detect_and_draw:
            bgr_frame = detect_and_draw(bgr_frame)

        # 将帧编码为 JPEG
        _, buffer = cv2.imencode(".jpg", bgr_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])

        publish_frame(buffer.tobytes(), processing_started)


async def video_stats_handler(request):
    """性能快照，连续比较 output_frames / received_packets 可算区间帧率。"""
    now = time.monotonic()
    with frame_lock:
        stats = dict(video_stats)
    published_at = stats.pop("published_at")
    stats.update(
        uptime_s=round(now - started_at, 3),
        received_packets=received_packets,
        frame_age_ms=round((now - published_at) * 1000, 1) if published_at is not None else None,
        decoder="vpu" if gst_appsrc is not None else "cpu",
        yolo=detect_and_draw is not None,
    )
    if gst_appsrc is not None:
        queue = gst_pipeline.get_by_name("decoded")
        stats.update(
            appsrc_queued_buffers=gst_appsrc.get_property("current-level-buffers"),
            appsrc_queued_bytes=gst_appsrc.get_property("current-level-bytes"),
            decoded_queued_frames=queue.get_property("current-level-buffers"),
        )
    return web.json_response(stats)


async def index_handler(request):
    """返回 HTML 页面。

    页头几个徽章按进程实际情况现渲染：硬解还是软解、开没开 YOLO 都是启动时才定的，
    写死在模板里必然有对不上的时候（原来的标题就一直挂着「YOLO 检测」）。
    """
    badges = [f"📷 {camera_name}" if camera_name else "📷 未选设备"]
    badges.append("VPU 硬解" if gst_appsrc is not None else "CPU 软解")
    if detect_and_draw:
        badges.append("YOLO 检测")
    with open(INDEX_HTML, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__BADGES__", "".join(f'<span class="badge">{escape(b)}</span>' for b in badges))
    return web.Response(text=html, content_type="text/html")


async def video_feed_handler(request):
    """MJPEG 视频流处理"""
    response = web.StreamResponse()
    response.headers["Content-Type"] = "multipart/x-mixed-replace; boundary=frame"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Accel-Buffering"] = "no"
    await response.prepare(request)

    last_sent = None
    try:
        while True:
            # 检查客户端是否已断开连接
            if request.transport is None or request.transport.is_closing():
                break

            try:
                # 获取最新帧
                with frame_lock:
                    frame_data = latest_frame

                # 帧没更新就不重复发，1080p 下能省一半带宽
                if frame_data is not None and frame_data is not last_sent:
                    last_sent = frame_data
                    # 发送 MJPEG 帧
                    boundary = b"--frame\r\n"
                    content_type = b"Content-Type: image/jpeg\r\n\r\n"
                    await response.write(boundary + content_type + frame_data + b"\r\n")

                # 控制帧率，避免过快
                await asyncio.sleep(0.033)  # 约 30 FPS
            except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError) as e:
                # 客户端断开连接，正常退出
                break
            except Exception as e:
                # 其他错误，记录但不中断
                if "closing transport" not in str(e).lower():
                    print(f"视频流错误: {e}")
                break
    except asyncio.CancelledError:
        # 任务被取消，正常退出
        pass
    except Exception as e:
        if "closing transport" not in str(e).lower():
            print(f"视频流处理错误: {e}")
    finally:
        try:
            if not response._closed:
                await response.write_eof()
        except Exception:
            pass

    return response


async def devices_handler(request):
    """在线设备列表，did 用于调 /device/power"""
    device_list = await asyncio.to_thread(xiaomi_client.home.get_device_list)
    return web.json_response(
        [
            {
                "did": d.get("did"),
                "name": d.get("name"),
                "model": d.get("model"),
                "room": d.get("room_name"),
            }
            for d in device_list
            if d.get("isOnline", False)
        ]
    )


async def device_state_handler(request):
    """读设备当前开关状态：GET /device/power?did=xxx

    页面上每行的开关得先知道现在是开是关才能摆对位置。读不到（设备不支持、离线）
    时 power 为 null，前端把那一行置灰。
    """
    did = request.query.get("did")
    if not did:
        return web.json_response({"error": "需要 did"}, status=400)
    try:
        power = await asyncio.to_thread(xiaomi_client.device.get_power, did)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

    return web.json_response({"did": did, "power": power})


async def device_power_handler(request):
    """设备开关：POST {"did": "xxx", "action": "on" | "off" | "toggle"}

    不传 siid/piid，SDK 按 spec 自动定位主开关（插座的指示灯、倒计时那些不会被误选）；
    多路开关想指定某一路，得先 find_switch_list 查出来再显式传，这里不做。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    did, action = body.get("did"), body.get("action")
    if not did or action not in ("on", "off", "toggle"):
        return web.json_response({"error": '需要 did 和 action（"on" / "off" / "toggle"）'}, status=400)

    device = xiaomi_client.device
    try:
        # SDK 是同步的（requests 调米家云，首次还要拉 spec，超时 10s），必须扔线程里跑：
        # 卡在事件循环上视频流会跟着断，硬解那条路还会把码流回调一起堵死
        if action == "toggle":
            # 反转得先知道现在是什么。SDK 的 toggle 内部也是这么读一次，自己读只是为了拿到结果值
            current = await asyncio.to_thread(device.get_power, did)
            if current is None:
                return web.json_response({"error": f"读不到当前开关状态（设备可能离线），did={did}"}, status=400)
            action = "off" if current else "on"

        on = action == "on"
        await asyncio.to_thread(device.turn_on if on else device.turn_off, did)
    except Exception as e:
        # 设备离线、不支持开关、定位不到主开关都走这里，消息直接透给调用方
        return web.json_response({"error": str(e)}, status=400)

    # 下发失败 set_prop 会抛（上面已经拦住），走到这里状态就是刚写进去的值，不用再查一次
    return web.json_response({"did": did, "power": on})


async def run():
    global camera_name, gst_appsrc, xiaomi_client

    # 硬解放在最前面：板子上没有 VPU 就不该往下走，别等登录、挑完设备才报错
    gst_appsrc = init_hw_decoder()
    if gst_appsrc is None:
        soc = rockchip_soc()
        if soc:
            # 板子的 CPU 软解扛不住这路码流（当初上 VPU 就是为这个），与其让画面一直卡着不如不起
            raise SystemExit(
                f"这是 {soc}，VPU 硬解没起来，拒绝用 CPU 软解启动。按上面那行原因排查：\n"
                "  venv 没放行系统包的话重建：uv venv --python 3.12 --system-site-packages && uv sync\n"
                "  元件在不在：gst-inspect-1.0 mppvideodec"
            )
        print("未检测到 Rockchip VPU（mppvideodec），回退到 CPU 软解，高分辨率下可能卡顿")

    client = xiaomi_client = XiaomiClient()
    client.login()
    device_list = client.home.get_device_list()
    online_devices = [d for d in device_list if d.get("isOnline", False)]

    if not online_devices:
        print("\n设备列表: 暂无在线设备")
        return

    print_device_list(online_devices)
    env_did = os.getenv("DEVICE_DID")
    if env_did:
        print(f"使用环境变量 DEVICE_DID={env_did}")
        device_info = next((d for d in online_devices if d.get("did") == env_did), None)
        if not device_info:
            print(f"未找到 did 为 {env_did} 的在线设备")
            return
    else:
        while True:
            try:
                index = int(input("请输入摄像头设备序号: "))
            except ValueError:
                print(f"输入无效，请输入 1～{len(online_devices)} 之间的整数")
                continue
            except EOFError:
                print("\n输入已结束，退出设备选择")
                return
            if not 1 <= index <= len(online_devices):
                print(f"序号超出范围，请输入 1～{len(online_devices)} 之间的整数")
                continue
            device_info = online_devices[index - 1]
            break

    camera_name = device_info.get("name", "")

    # 创建 web 应用
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/video_feed", video_feed_handler)
    app.router.add_get("/video_stats", video_stats_handler)
    app.router.add_get("/devices", devices_handler)
    app.router.add_get("/device/power", device_state_handler)
    app.router.add_post("/device/power", device_power_handler)

    # 启动 web 服务器
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8180)
    await site.start()

    print("\nWeb 服务器已启动: http://localhost:8180")
    print("请在浏览器中打开上述地址查看视频流")

    # 启动视频流
    stream_task = asyncio.create_task(
        client.miot_camera_stream.run_stream(
            device_info["did"], 0, on_raw_video_callback=on_raw_video, video_quality=MIoTCameraVideoQuality.LOW
        )
    )

    try:
        # 等待流数据
        await client.miot_camera_stream.wait_for_data()
    except KeyboardInterrupt:
        print("\n正在关闭...")
    finally:
        stream_task.cancel()
        if gst_pipeline is not None:
            gst_pipeline.set_state(Gst.State.NULL)
        await runner.cleanup()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "yolo":
        # 这块板子是 ARMv8.0，PyPI 的 torch aarch64 wheel 一跑卷积就 SIGILL，优先走 NPU
        try:
            from rknn_yolo import RknnYolo

            detect_and_draw = RknnYolo().detect_and_draw
            print("YOLO 跑在 RK3576 NPU 上")
        except (ImportError, RuntimeError) as e:
            # 先把 NPU 为什么不行打出来再去试回退：板子上按 ARMv8.0 故意没装 ultralytics，
            # 回退这一步必炸，原来的写法会让「加载模型失败: yolo11n_int8.rknn」这句真正的
            # 原因被 ModuleNotFoundError 顶掉，看着像缺包，其实是模型文件没放上去
            print(f"NPU 不可用：{e}")
            try:
                from ultralytics import YOLO

                _yolo = YOLO("yolo11n.pt")

                def detect_and_draw(frame):
                    return _yolo(frame, verbose=False)[0].plot()

                print("YOLO 回退到 ultralytics")
            except ImportError:
                # 检测没了流还得推，别让整个服务起不来；页头徽章会如实显示没开 YOLO
                print("ultralytics 也没有（板子上不装：torch 的 aarch64 wheel 按 ARMv8.2+ 编，一跑卷积就 SIGILL）")
                print("本次不做检测，只推流。要检测就把 yolo11n_int8.rknn 放到本目录再起")
    asyncio.run(run())
