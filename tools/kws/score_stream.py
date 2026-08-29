#!/usr/bin/env python3
"""Score wav files through the exact streaming decision chain, in Python.

Mirrors kws_engine.c: a 200-frame (2.0 s) window advances 8 frames (80 ms)
per inference, the published score is the mean of the last 3 inferences.
Prints the peak smoothed score per clip — the number the board's threshold
is compared against.

    python3 score_stream.py data/pos_raw/*.wav
    python3 score_stream.py --label 1 rec/*.wav
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from scipy.signal import resample_poly

import kws_common as K
from train import DSCNN

ROOT = Path(__file__).parent
INFER_EVERY = 8      # KWS_INFER_EVERY
SMOOTH = 3           # KWS_SMOOTH


def load_model(ckpt):
    blob = torch.load(ckpt, map_location="cpu", weights_only=False)
    model = DSCNN(blob["arch"])
    model.load_state_dict(blob["state_dict"])
    model.eval()
    return model, blob["mean"], blob["std"]


def load_wav_16k(path):
    pcm, sr = sf.read(path, dtype="float32", always_2d=False)
    if pcm.ndim > 1:
        pcm = pcm.mean(axis=1)
    if sr != K.SR:
        from math import gcd
        g = gcd(sr, K.SR)
        pcm = resample_poly(pcm, K.SR // g, sr // g).astype(np.float32)
    return (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16)


def pad_for_stream(pcm_i16):
    """Grow clips shorter than one 2.0 s window, so they can be scored at all.

    Pad material is the clip's own quietest 100 ms (its room floor) tiled,
    NOT digital silence: a stream never contains true zeros, and log(eps)
    frames make CMN produce windows that cannot occur on the board — those
    fake windows scored high and inflated the false-alarm rate 10x.
    """
    need = K.WINDOW_SAMPLES + 16 * K.HOP
    if len(pcm_i16) >= need:
        return pcm_i16
    seg = K.SR // 10
    if len(pcm_i16) >= seg:
        n = len(pcm_i16) // seg
        blocks = pcm_i16[:n * seg].reshape(n, seg)
        floor = blocks[np.argmin((blocks.astype(np.float32) ** 2).mean(axis=1))]
    else:
        floor = np.zeros(seg, dtype=np.int16)
    grow = need - len(pcm_i16)
    tile = np.tile(floor, grow // seg + 2)
    head = grow // 2
    return np.concatenate([tile[:head], pcm_i16, tile[:grow - head]])


def peak_score(model, mean, std, pcm_i16):
    """Peak smoothed wake probability over the clip (None if too short)."""
    feat = K.logmel(pad_for_stream(pcm_i16))
    if len(feat) < K.T_FRAMES:
        return None, None
    feat = (feat - mean) / std
    starts = range(0, len(feat) - K.T_FRAMES + 1, INFER_EVERY)
    wins = np.stack([feat[s:s + K.T_FRAMES] for s in starts])
    wins = wins - wins.mean(axis=1, keepdims=True)      # per-window CMN
    with torch.no_grad():
        p = torch.softmax(
            model(torch.from_numpy(wins).unsqueeze(1)), dim=1)[:, 1].numpy()
    if len(p) < SMOOTH:
        return float(p.max()), p
    sm = np.convolve(p, np.ones(SMOOTH) / SMOOTH, mode="valid")
    return float(sm.max()), p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wavs", nargs="+")
    ap.add_argument("--ckpt", default=str(ROOT / "checkpoints/model_best.pt"))
    ap.add_argument("--label", type=int, choices=[0, 1],
                    help="1=should wake, 0=should not; enables hit-rate summary")
    ap.add_argument("--thr", type=float, default=0.90)
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="summary only, no per-clip lines")
    args = ap.parse_args()

    model, mean, std = load_model(args.ckpt)
    scores = []
    for w in args.wavs:
        s, _ = peak_score(model, mean, std, load_wav_16k(w))
        if s is None:
            print(f"{Path(w).name:<40} SKIP (< 2.0 s)", file=sys.stderr)
            continue
        scores.append(s)
        if not args.quiet:
            hit = "WAKE" if s >= args.thr else "    "
            print(f"{Path(w).name:<40} {s:.3f}  {hit}")

    if not scores:
        return
    a = np.asarray(scores)
    print(f"\nn={len(a)}  peak-score  min {a.min():.3f}  "
          f"p50 {np.median(a):.3f}  mean {a.mean():.3f}  max {a.max():.3f}")
    if args.label is not None:
        for th in (0.70, 0.80, 0.85, 0.90, 0.95):
            r = float((a >= th).mean())
            name = "recall" if args.label == 1 else "false-wake"
            print(f"  θ={th:.2f}  {name} {r * 100:5.1f}%  ({int((a >= th).sum())}/{len(a)})")


if __name__ == "__main__":
    main()
