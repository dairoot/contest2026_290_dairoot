import os

from dotenv import load_dotenv

load_dotenv()

# ASR 后端：sense_voice_rknn（RK3576 NPU，默认）/ sense_voice（int8 ONNX，CPU）
# / volc（火山流式）。两个模型默认都走 NPU；短句为主的场景可切回 CPU，取舍见
# README「怎么选后端」。环境不支持 NPU 时自动回退，见 utils/_rknn.py
ASR_MODEL_TYPE = os.environ.get("ASR_MODEL_TYPE", "sense_voice_rknn")
# onnxruntime 的 intra-op 线程数，0 表示按核数自动决定
ASR_ONNX_THREADS = int(os.environ.get("ASR_ONNX_THREADS", "0"))

# ASR 的 NPU 模型；窗口毫秒数必须与 tools/export_onnx.py --asr-seconds 一致
ASR_RKNN_PATH = os.environ.get(
    "ASR_RKNN_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rknn_models", "sensevoice_5s.rknn"),
)
ASR_RKNN_WINDOW_MS = int(os.environ.get("ASR_RKNN_WINDOW_MS", "5000"))

# 声纹后端：rknn（RK3576 NPU，默认）/ modelscope（PyTorch CPU 回退）
SPEAKER_BACKEND = os.environ.get("SPEAKER_BACKEND", "rknn")
SPEAKER_RKNN_PATH = os.environ.get(
    "SPEAKER_RKNN_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rknn_models", "eres2netv2_3s.rknn"),
)

VOLC_APP_ID = os.environ.get("VOLC_APP_ID", "")
VOLC_ACCESS_TOKEN = os.environ.get("VOLC_ACCESS_TOKEN", "")
VOLC_RESOURCE_ID = "volc.seedasr.sauc.duration"

# 热词：直传热词用逗号/分号/换行分隔，例如 VOLC_HOTWORDS="豆包,火山引擎"；
# 词表则在自学习平台配置后填名称或 id。直传热词优先级高于词表。
VOLC_HOTWORDS = os.environ.get("VOLC_HOTWORDS", "")
VOLC_BOOSTING_TABLE_NAME = os.environ.get("VOLC_BOOSTING_TABLE_NAME", "")
VOLC_BOOSTING_TABLE_ID = os.environ.get("VOLC_BOOSTING_TABLE_ID", "")
