"""SenseVoice 各后端共用的前端与解码。

ONNX（CPU）和 RKNN（NPU）两条路只有推理那一步不同：特征都是 funasr 的
WavFrontend（torchaudio kaldi fbank + LFR + CMVN），解码都是 CTC 贪心 +
tokens.json 反查。模型目录取 iic/SenseVoiceSmall-onnx——config.yaml / am.mvn /
tokens.json 都在里面，NPU 后端也只借这三个文件，权重走各自的 .rknn。
"""

import json
import os

import numpy as np
import torch
import yaml
from funasr.frontends.wav_frontend import WavFrontend

from utils._model_path import ensure_model_dir

MODEL_ID = "iic/SenseVoiceSmall-onnx"
MODEL_DIR = ensure_model_dir(MODEL_ID)

# 与 funasr_onnx.SenseVoiceSmall 一致的 prompt id
LANGUAGE_IDS = {"auto": 0, "zh": 3, "en": 4, "yue": 7, "ja": 11, "ko": 12, "nospeech": 13}
TEXTNORM_IDS = {"withitn": 14, "woitn": 15}
BLANK_ID = 0
# sentencepiece 的词首标记
_SPACE_TOKEN = "▁"

with open(os.path.join(MODEL_DIR, "config.yaml"), encoding="utf-8") as _f:
    _frontend_conf = dict(yaml.safe_load(_f)["frontend_conf"])
_frontend_conf["cmvn_file"] = os.path.join(MODEL_DIR, "am.mvn")
frontend = WavFrontend(**_frontend_conf)

with open(os.path.join(MODEL_DIR, "tokens.json"), encoding="utf-8") as _f:
    _tokens = json.load(_f)


def extract_feats(wavs: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """批量抽特征。WavFrontend 按 lengths 裁掉补零，逐条算完再 pad。"""
    lengths = torch.as_tensor([w.shape[0] for w in wavs], dtype=torch.int32)
    padded = torch.zeros(len(wavs), int(lengths.max()), dtype=torch.float32)
    for i, wav in enumerate(wavs):
        padded[i, : wav.shape[0]] = torch.from_numpy(wav)
    feats, feats_len = frontend(padded, lengths)
    return feats.numpy(), feats_len.numpy().astype(np.int32)


def ctc_greedy_decode(logits: np.ndarray) -> str:
    """先取 argmax，去连续重复，再去 blank。"""
    yseq = logits.argmax(axis=-1)
    yseq = yseq[np.concatenate(([True], np.diff(yseq) != 0))]
    token_ids = yseq[yseq != BLANK_ID]
    text = "".join(_tokens[i] for i in token_ids)
    return text.replace(_SPACE_TOKEN, " ").strip()
