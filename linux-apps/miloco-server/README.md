# miloco-server（摄像头视频流 + 米家设备开关）

小米摄像头的 HEVC 码流拉下来，在 RK3576 的 VPU 上硬解、NPU 上跑 yolo11n 检测，
以 MJPEG 推给浏览器；同一个服务另外开三个接口做米家设备的开关。板上的语音助手
[`../harness/`](../harness/) 通过 `miloco_mcp.py` 调这些接口——「看一眼摄像头」和
「把客厅的灯打开」两件事都落在这里。

拉流和设备控制都走 [miloco-sdk](https://github.com/dairoot/miloco-sdk)，登录态缓存在
`data/`（含凭据，已在 `.gitignore` 里）。

## 启动

```bash
uv venv --python 3.12 --system-site-packages   # 见下面「VPU 硬解」
uv sync
uv run web.py            # 只推流
uv run web.py yolo       # 带 YOLO 检测
```

启动后按提示选摄像头设备；`DEVICE_DID=xxx uv run web.py` 可以跳过这一步。
默认监听 `0.0.0.0:8180`，浏览器打开就是设备面板（左边实时画面，右边米家设备列表，
每台一个开关）。

| 接口 | 说明 |
| --- | --- |
| `GET /` | 设备面板页面（模板是同目录的 `index.html`，每次请求现读，改样式不用重启） |
| `GET /video_feed` | MJPEG 流 |
| `GET /video_stats` | 帧数、处理耗时、服务器流水线延迟和队列深度 |
| `GET /devices` | 在线设备列表（did / 名字 / 房间 / 型号） |
| `GET /device/power?did=` | 读某台设备的开关状态，读不到返回 `power: null` |
| `POST /device/power` | `{"did": "...", "action": "on" \| "off" \| "toggle"}` |

设备的开关按 spec 自动定位主开关，不用传 siid/piid；传感器、网关这类没有可写 `on`
属性的设备会返回「未能定位主开关」，页面上显示成「不支持开关」。

## VPU 硬解

硬解走 GStreamer 的 `mppvideodec` / `mppjpegenc`，Python 绑定是板子上 apt 装的
`python3-gi`（配 `gstreamer1.0-rockchip1`），pip 里装不出来，所以建 venv 时要
`--system-site-packages` 放行系统包。没放行时 web.py 会自动回退到 PyAV 软解，
1080p 下会卡。

不开 YOLO 时 JPEG 也由 VPU 编（`mppjpegenc`）；开了 YOLO 才需要取 NV12 裸帧到 CPU，
转 BGR、检测、再软编 JPEG。

硬解输出与检测之间有独立的 GStreamer `queue`，最多保留 **1 帧**，队满时丢弃
最旧的已解码帧。YOLO 和 JPEG 编码跑在这个队列的下游线程；处理速度低于摄像头
帧率时降低输出帧率，避免旧画面一直积压。不能只给 `appsink` 设置 `drop=true`：
它的 `new-sample` 回调运行在 streaming thread，直接做推理会阻塞上游。也不能
随意丢弃解码前的 H.265 包，否则会破坏参考帧依赖。

用 `curl -s http://localhost:8180/video_stats` 检查：

- `appsrc_queued_buffers` 应保持接近 0，`decoded_queued_frames` 不超过 1。
- `processing_ms` 是最近一帧的转换、检测和 JPEG 编码耗时。
- `pipeline_latency_ms` 从码流进入服务器算到 JPEG 发布，包含解码、排队和推理；
  **不包含摄像头端、网络传输和浏览器渲染延迟**。CPU 软解时此字段为 `null`。
- `frame_age_ms` 是最近 JPEG 发布至今的时间，可用来发现断流。
- 两次请求的 `received_packets` / `output_frames` 增量除以时间差，可算输入包率
  和输出帧率；包数不保证在所有摄像头上等于帧数。

回归测试不需要登录摄像头或加载模型，用 40 fps 测试源和 100 ms 的慢检测验证
丢帧与延迟上限（需系统 GStreamer 绑定）：

```bash
uv run python -m unittest -v test_video_pipeline
```

## YOLO 模型（NPU）

`rknn_yolo.py` 加载**本目录**下的 `yolo11n_int8.rknn`（`.gitignore` 排掉了模型产物，
仓库里没有，得自己转一份放进来）。板子是 Cortex-A53/A72（ARMv8.0），PyPI 的 torch
aarch64 wheel 按 ARMv8.2+ 编，一跑卷积就 SIGILL，所以板上没有 ultralytics
这条退路，必须走 NPU。

转换在板子上做（`tools/convert_rknn.py`，需要 rknn-toolkit2，另建一个 venv 装，
别混进本项目的 `.venv`）：

```bash
# 1. 取 rknn_model_zoo 的 yolo11n.onnx（不是 ultralytics 直接导出的那个，
#    输出被拆成 3 个尺度 x (box DFL / 类别 / score_sum) 共 9 路，后处理照它写的）
# 2. 准备量化标定集：随便拍 20 张 640x640 的场景图，dataset.txt 一行一个绝对路径
# 3. 转（在放着 yolo11n.onnx / dataset.txt 的目录里跑）
python convert_rknn.py int8   # -> yolo11n_int8.rknn，不带 int8 参数则导出 fp 版
cp yolo11n_int8.rknn <本目录>
```

自测（会打印检测结果、平均耗时，并把画好框的图写到 `rknn_out.jpg`）：

```bash
uv run rknn_yolo.py <一张图.jpg>
```

板上实测（RK3576 NPU 双核，librknnrt 2.3.2）：单帧 **54 ms（18 fps）**。
`KEEP` 里只留了 person / bed / cell phone / cat 四类，其余类别的通道在解码时就丢掉，
后处理会快不少。

模型不在或 NPU 起不来时，`web.py yolo` 不会崩——会打印原因然后**只推流不检测**，
页头徽章如实显示没开 YOLO。
