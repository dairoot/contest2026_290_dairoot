"""把 rknn_model_zoo 的 yolo11n.onnx 转成 RK3576 的 .rknn。"""
import sys

from rknn.api import RKNN

QUANT = len(sys.argv) > 1 and sys.argv[1] == "int8"
OUT = "yolo11n_int8.rknn" if QUANT else "yolo11n_fp.rknn"

rknn = RKNN(verbose=False)
# 输入直接喂 uint8 RGB，归一化交给 NPU 做（/255）
rknn.config(mean_values=[[0, 0, 0]], std_values=[[255, 255, 255]], target_platform="rk3576")

if rknn.load_onnx(model="yolo11n.onnx") != 0:
    sys.exit("load_onnx 失败")

kwargs = {"do_quantization": True, "dataset": "dataset.txt"} if QUANT else {"do_quantization": False}
if rknn.build(**kwargs) != 0:
    sys.exit("build 失败")

if rknn.export_rknn(OUT) != 0:
    sys.exit("export 失败")
print(f"OK -> {OUT}")
rknn.release()
