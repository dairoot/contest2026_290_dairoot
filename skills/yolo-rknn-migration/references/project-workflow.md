# YOLO 项目执行参考

代码基线：比赛仓 `4f5d6d6`，整理日期 2026-09-13。路径相对 `linux-apps/miloco-server/`。

## 现有实现

| 文件 | 契约 |
| --- | --- |
| `tools/convert_rknn.py` | 读当前目录 `yolo11n.onnx`，RK3576，mean=0/std=255；`int8` 时另读 `dataset.txt` |
| `rknn_yolo.py:RknnYolo` | 默认相对当前目录加载 `yolo11n_int8.rknn`，也接受构造参数 `model_path` |
| `rknn_yolo.py:_letterbox` | 640×640，填充 114，保留比例/补边 |
| `rknn_yolo.py:_decode_branch` | 先按 KEEP 类别分筛候选，再算 16-bin DFL |
| `rknn_yolo.py:_nms` | 每类单独抑制，IoU 阈值 0.45 |
| `web.py` | VPU 解码、NPU 检测、MJPEG 发布；检测初始化失败可能只推流 |
| `test_video_pipeline.py` | 模拟慢检测验证丢帧/排队，无真实 NPU 精度验证 |

## 模型来源与环境

从 [Rockchip Model Zoo YOLO11 示例](https://github.com/airockchip/rknn_model_zoo/blob/main/examples/yolo11/README.md) 的预训练入口或匹配的优化导出方式取得 ONNX，记录下载来源、版本与哈希。官方示例说明优化图的输出与原版不同；自训练模型沿对应导出器生成，并检查类别定义。不要只将任意 ONNX 改名来满足脚本。

Toolkit2 负责转换，Lite2 负责板上 Python 推理，用户态 runtime 还需与驱动兼容，见 [RKNN 工具链说明](https://github.com/airockchip/rknn-toolkit2/blob/master/README.md)。项目记录的成功基线为 2.3.2；实际新环境先验证所用版本组合。转换环境独立于服务 `.venv`。

本项目在 RK3576 上转换过 YOLO11n；更大 YOLO 或不同优化图需重新评估内存，可以移至 Linux 构建机。不要沿用语音大模型的资源数字作为 YOLO 的必需内存。

模型来源、校准与转换方法统一在本 Skill 维护。RK3576 板上的 Cortex-A53/A72 曾与所用 torch aarch64 wheel 的指令集要求不兼容，触发 SIGILL，因此本项目的 ARM 板运行路径不依赖 Ultralytics CPU 回退；这不是关于所有版本 wheel 的通用限制。

## 转换命令

先设置绝对路径：`CONTEST_ROOT` 为源码根，`YOLO_CONVERT_DIR` 为含 `yolo11n.onnx` 的专用目录，`RKNN_PYTHON` 为转换环境 Python 可执行文件。

```bash
cd "$YOLO_CONVERT_DIR"
"$RKNN_PYTHON" "$CONTEST_ROOT/linux-apps/miloco-server/tools/convert_rknn.py"
# 产物：yolo11n_fp.rknn
```

生成校准集时按部署端同样的 letterbox/颜色语义准备图像；`dataset.txt` 每行一个绝对图像路径，确认工具读取后的张量和推理预处理一致。校准集与精度验证集分开，项目旧文档的“20 张”仅用于启动实验。

```bash
"$RKNN_PYTHON" "$CONTEST_ROOT/linux-apps/miloco-server/tools/convert_rknn.py" int8
# 产物：yolo11n_int8.rknn
```

脚本没有 `--target`、`--onnx` 或 `--output` 选项，也不会创建缺失的校准清单。不同目标或模型参数先修改/参数化脚本并检查导出图，不在说明里编造参数。

## 输入输出核对

推理输入为 `[1,640,640,3]` uint8 RGB（NHWC）。`std_values=[[255,255,255]]` 已配置缩放，Python `detect` 不做 `/255`。ONNX 原图输入布局与 Lite2 提交布局可能不同，分别记录，不能据此删掉 `data_format=['nhwc']`。

当前九路输出按三个尺度分组，典型网格为 80×80、40×40、20×20；每组 box/类别/score_sum 通道分别为 64/80/1。实际张量需运行后验证：当前代码假设输出是 NCHW，且第 `i*3`、`i*3+1` 为对应 box/类别分数。

`CLASSES` 保持 COCO 80 类顺序，`KEEP_IDS` 取目标类别的原始下标。输出框为原图尺度 xyxy；当前代码没有显式裁剪到图像边界，下游保存/裁图前需按用途处理越界坐标。

## 板上图像验证

在板上原地转换时，将 `yolo11n_int8.rknn` 复制到实际服务目录；在其他 Linux 机器转换时，用 `scp` 传到该目录并核对哈希。服务和命令行入口均按当前工作目录找默认模型，从 `linux-apps/miloco-server/` 启动；不要把 `yolo11n_fp.rknn` 改名为 int8 文件来满足默认路径。

确认板上 runtime/驱动兼容，并已将模型放入实际运行目录。命令行入口固定加载 `yolo11n_int8.rknn`，在目录内执行：

```bash
cd "$CONTEST_ROOT/linux-apps/miloco-server"
uv run rknn_yolo.py "$TEST_IMAGE"
```

`TEST_IMAGE` 指向板上可读取图片，先确认能被 OpenCV 解码。程序打印检测和 30 次 `detect` 平均耗时，写 `rknn_out.jpg`；它不是纯 NPU benchmark，也不输出 mAP。

对 fp16 先走显式模型路径，避免将 fp16 改名伪装成 int8：

```bash
TEST_IMAGE="$TEST_IMAGE" YOLO_MODEL_FILE="$FP16_MODEL_FILE" uv run python - <<'PY'
import os
import cv2
from rknn_yolo import RknnYolo

img = cv2.imread(os.environ["TEST_IMAGE"])
if img is None:
    raise SystemExit("图片读取失败")
model = RknnYolo(model_path=os.environ["YOLO_MODEL_FILE"])
try:
    boxes, classes, scores = model.detect(img)
    print(boxes, classes, scores)
    cv2.imwrite("rknn_fp_result.jpg", model.detect_and_draw(img))
finally:
    model.rknn.release()
PY
```

上面的 `TEST_IMAGE` 为板上图片路径，`FP16_MODEL_FILE` 为板上绝对模型路径。进一步验证时同图、同阈值比较 ONNX/fp16/int8，保存原图与数值检测结果；不要只看画框截图。

## 视频接入（按任务需要）

系统 GStreamer 的 `mppvideodec/mppjpegenc` 及 Python GI 绑定来自板子系统包。项目使用与系统 Python 匹配的环境并放行系统包，例如已验证板上的 Python 3.12 配置：

```bash
uv venv --python 3.12 --system-site-packages
uv sync
uv run web.py yolo
```

已有环境先核对，不直接覆盖。切换 Python 小版本后确认 GI 扩展 ABI；`uv sync` 不会替代系统 Rockchip GStreamer 插件安装。启动摄像头服务还需要 miloco-sdk 的设备与登录环境，单图验证不需要摄像头。

解码后队列是 `max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream`，为推理/编码创建下游线程。检查 `/video_stats`：输入缓冲接近零、解码后队列不超过一帧、输出仍持续更新，流水线延迟不随时间积累。

可运行现有模拟回归：

```bash
uv run python -m unittest -v test_video_pipeline
```

测试用 40 fps 测试源和 100 ms 的慢检测，不加载真实模型；缺 GStreamer 会跳过，跳过不算通过。真实服务还应验证 NPU 初始化和目标检测。`pipeline_latency_ms` 只覆盖服务器内部，不含摄像头、网络到达前及浏览器渲染延迟。
