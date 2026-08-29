#!/usr/bin/env python3
"""One table over every evaluation set, through the exact streaming chain.

    python3 eval_suite.py [--ckpt checkpoints/model_best.pt] [--tag v7]

Every set is disjoint from training.  The TTS / say / rerec sets are the
clips train.py's group hash holds out (same rule, so the SAME clips no
matter which checkpoint is scored); real-neg and room-noise are time
blocks that are in no manifest at all.

  tts-pos   / tts-neg     held-out edge-tts, original texts / sentences
  tts-pos-x / tts-neg-x   held-out edge-tts, round-7 Mandarin renderings /
                          confusables
  say-pos   / say-neg     held-out macOS voices
  rerec-pos / rerec-neg   held-out clips re-recorded through the board
  real-neg                real-mic speech, data/real_test.txt
  human-pos               human takes off the board mic (record_real.py --board)
  room-noise              held-out minute of the PDM room capture -> wakes/h

Numbers are peak smoothed score per clip (what the board compares with
its threshold): recall for label-1 sets, false-wake rate for label-0.

Synthetic clips (tts-*, say-*) are scored over a -48 dBFS bed of the real
PDM room floor: their silences are digital zeros, which the board never
produces and which put log(eps) frames into the CMN window.  v6 tolerated
zeros only because a quarter of its training windows had them; the real
question is how a clip sounds when played into the room.
"""

import argparse
import json
from pathlib import Path

import numpy as np

import kws_common as K
from augment_and_cache import clip_group
from score_stream import INFER_EVERY, SMOOTH, load_model, load_wav_16k, \
    peak_score

ROOT = Path(__file__).parent
DATA = ROOT / "data"
THRS = (0.85, 0.90, 0.95)
FLOOR_DB = -48.0                       # measured PDM room floor (rms)
SYNTHETIC = ("tts-pos", "tts-pos-x", "tts-neg", "tts-neg-x", "say-pos",
             "say-neg")

_floor = None


def with_floor(pcm_i16):
    """Clean synthetic clip -> the same clip heard over the real room floor."""
    global _floor
    if _floor is None:
        p = DATA / "noise_raw" / "room_0829_train.wav"
        x = load_wav_16k(p).astype(np.float32) if p.exists() else \
            np.random.default_rng(0).standard_normal(K.SR * 20) * 300
        _floor = x / (np.sqrt((x ** 2).mean()) + 1e-9)
    n = len(pcm_i16) + 2 * K.WINDOW_SAMPLES
    start = np.random.default_rng(len(pcm_i16)).integers(0, len(_floor) - n) \
        if len(_floor) > n else 0
    bed = np.tile(_floor, n // len(_floor) + 1)[start:start + n]
    bed = bed * (32768.0 * 10 ** (FLOOR_DB / 20))
    head = K.WINDOW_SAMPLES
    out = bed.copy()
    out[head:head + len(pcm_i16)] += pcm_i16
    return np.clip(out, -32768, 32767).astype(np.int16)


def is_val(m):
    g = np.uint64(clip_group(m))
    return int((g * np.uint64(2654435761)) % np.uint64(2 ** 32)) % 10 == 0


def manifest(name):
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else []


def build_sets():
    def tidx(m):                       # pos_v00_t3_r1_p2 -> 3
        return int(Path(m["file"]).stem.split("_")[2][1:])

    def sidx(m):                       # neg_s012_v03 -> 12
        return int(Path(m["file"]).stem.split("_")[1][1:])

    tts = [m for m in manifest("manifest.json") if is_val(m)]
    say = [m for m in manifest("manifest_say.json") if is_val(m)]
    rr = [m for m in manifest("manifest_rerec.json") if is_val(m)]
    sets = {
        "tts-pos": (1, [m for m in tts if m["label"] == 1 and tidx(m) < 4]),
        "tts-pos-x": (1, [m for m in tts if m["label"] == 1 and tidx(m) >= 4]),
        "tts-neg": (0, [m for m in tts if m["label"] == 0 and sidx(m) < 96]),
        "tts-neg-x": (0, [m for m in tts if m["label"] == 0 and sidx(m) >= 96]),
        "say-pos": (1, [m for m in say if m["label"] == 1]),
        "say-neg": (0, [m for m in say if m["label"] == 0]),
        "rerec-pos": (1, [m for m in rr if m["label"] == 1]),
        "rerec-neg": (0, [m for m in rr if m["label"] == 0]),
    }
    out = {k: (lab, [DATA / m["file"] for m in ms])
           for k, (lab, ms) in sets.items() if ms}
    for name, fname, label in (("real-neg", "real_test.txt", 0),
                               ("human-pos", "real_test_pos.txt", 1)):
        p = DATA / fname
        paths = [DATA / l.strip() for l in p.read_text().splitlines()
                 if l.strip()] if p.exists() else []
        if paths:
            out[name] = (label, paths)
    return out


def room_events(model, mean, std, path):
    """Board-equivalent false wakes (2 s refractory) per threshold."""
    _, p = peak_score(model, mean, std, load_wav_16k(path))
    sm = np.convolve(p, np.ones(SMOOTH) / SMOOTH, mode="valid")
    t = (np.arange(len(sm)) * INFER_EVERY + K.T_FRAMES) * K.HOP / K.SR
    secs = (len(p) * INFER_EVERY + K.T_FRAMES) * K.HOP / K.SR
    ev = {}
    for th in THRS:
        n, last = 0, -10.0
        for ti, s in zip(t, sm):
            if s >= th and ti - last > 2.0:
                n += 1
                last = ti
        ev[th] = n
    return ev, secs, float(sm.max())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", default=str(ROOT / "checkpoints/model_best.pt"))
    ap.add_argument("--tag", help="also save data/eval_<tag>.json")
    args = ap.parse_args()
    model, mean, std = load_model(args.ckpt)

    res = {}
    print(f"{'set':<11}{'n':>5}  " + "".join(f"θ={t:.2f}  " for t in THRS)
          + "  score p50 / worst")
    for name, (label, paths) in build_sets().items():
        sc = []
        for p in paths:
            pcm = load_wav_16k(p)
            if name in SYNTHETIC:
                pcm = with_floor(pcm)
            s, _ = peak_score(model, mean, std, pcm)
            if s is not None:
                sc.append(s)
        a = np.asarray(sc)
        rates = [float((a >= t).mean()) for t in THRS]
        worst = float(a.min()) if label == 1 else float(a.max())
        kind = "recall" if label == 1 else "false "
        print(f"{name:<11}{len(a):>5}  "
              + "".join(f"{r * 100:5.1f}%  " for r in rates)
              + f"  {np.median(a):.3f} / {worst:.3f}  ({kind})")
        res[name] = {"n": int(len(a)), "label": label,
                     "rates": dict(zip(map(str, THRS), rates)),
                     "p50": float(np.median(a)), "worst": worst}

    for path in sorted((DATA / "noise_raw").glob("*_test.wav")):
        ev, secs, mx = room_events(model, mean, std, path)
        print(f"{'room-noise':<11}{secs:>4.0f}s  "
              + "".join(f"{ev[t] * 3600 / secs:4.0f}/h  " for t in THRS)
              + f"  peak {mx:.3f}  ({path.name})")
        res[f"room:{path.stem}"] = {"secs": secs, "peak": mx,
                                    "wakes_per_h": {str(t): ev[t] * 3600 / secs
                                                    for t in THRS}}

    if args.tag:
        out = DATA / f"eval_{args.tag}.json"
        out.write_text(json.dumps(res, indent=1))
        print(f"saved {out}")


if __name__ == "__main__":
    main()
