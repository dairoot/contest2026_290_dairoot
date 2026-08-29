#!/usr/bin/env python3
"""Record real-human clips at 16 kHz mono — from the Mac mic, or straight
off the board's PDM mic over ssh (the channel the model actually runs on).

The whole corpus is synthetic speech (edge-tts + macOS voices), partly
re-recorded through the board's speaker->air->PDM channel.  No human has
ever been in the training set.  This records some.

    python3 record_real.py diag           # 12 takes, ~3 min: is there a gap?
    python3 record_real.py pos  --n 60    # positives for retraining
    python3 record_real.py neg  --n 40    # non-wake speech, SAME speaker
    python3 record_real.py pos --n 60 --board kickpi@192.168.3.21
                                          # same, but recorded by the board:
                                          # stand where you normally speak

Files land in data/real/<kind>/ (Mac mic) or data/real_pdm/<kind>/
(board mic), named <kind>_<speaker>_<cond>_<i>.wav.  The real_pdm ones
join training via mk_manifest_real.py (label from the folder).
"""

import argparse
import queue
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

import kws_common as K

ROOT = Path(__file__).parent
OUT = ROOT / "data" / "real"
BOARD = None                    # user@host when recording through the board

WAKE = "你好，openvela"

# (condition tag, what to do) — spread over distance, speed and loudness so
# one session covers the range a user actually speaks in.
POS_CONDS = [
    ("near", f"正常语速，距麦克风 20-30cm：「{WAKE}」"),
    ("far", f"退到 1-1.5m，正常音量：「{WAKE}」"),
    ("fast", f"说快一点，略含糊：「{WAKE}」"),
    ("slow", f"说慢一点，字字清楚：「{WAKE}」"),
    ("soft", f"小声、气声：「{WAKE}」"),
    ("loud", f"提高音量，像隔房间喊：「{WAKE}」"),
]

# Non-wake speech from the SAME voice. Without these, adding human positives
# just teaches the model "human timbre => wake" and false alarms get worse.
NEG_PROMPTS = [
    "你好", "你好啊", "你好吗", "您好", "你好，帮我开灯", "你好，小维",
    "维拉", "欧维拉", "你好，薇拉", "open the door", "vanilla",
    "今天天气怎么样", "把灯打开", "现在几点了", "播放一首歌",
    "声音大一点", "帮我订个闹钟", "明天要下雨吗", "这个方案再讨论一下",
    "代码已经提交了", "会议改到下午三点", "外卖到楼下了",
    # half-phrase: must NOT fire
    "你好，open", "openvela", "你好，欧朋",
]


def record_take(seconds, sr=K.SR):
    if BOARD:
        # arecord on the board, raw s16 streamed back over ssh stdout
        raw = subprocess.run(
            ["ssh", BOARD, "arecord -q -D hw:rpmsgmic,0 -f S16_LE -r 16000 "
             f"-c 1 -d {int(seconds + 0.999)} -t raw -"],
            capture_output=True, check=True).stdout
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return x[:int(seconds * sr)]

    import sounddevice as sd
    q = queue.Queue()

    def cb(indata, frames, t, status):
        if status:
            print(status, file=sys.stderr)
        q.put(indata.copy())

    frames = []
    with sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                        callback=cb, blocksize=1024):
        need = int(seconds * sr)
        got = 0
        while got < need:
            b = q.get()
            frames.append(b)
            got += len(b)
    x = np.concatenate(frames)[:int(seconds * sr), 0]
    return x


def save(x, path):
    peak = float(np.abs(x).max())
    sf.write(path, np.clip(x, -1.0, 1.0), K.SR, subtype="PCM_16")
    return peak


def session(kind, prompts, speaker, seconds):
    d = OUT / kind
    d.mkdir(parents=True, exist_ok=True)
    print(f"\n{len(prompts)} takes, {seconds:.0f}s each. Enter=录, s=跳过, q=停\n")
    done = 0
    for i, (cond, text) in enumerate(prompts):
        name = f"{kind}_{speaker}_{cond}_{i:03d}.wav"
        path = d / name
        if path.exists():
            print(f"[{i + 1}/{len(prompts)}] {name} 已存在，跳过")
            done += 1
            continue
        ans = input(f"[{i + 1}/{len(prompts)}] {text}  > ").strip().lower()
        if ans == "q":
            break
        if ans == "s":
            continue
        print("   录音中...", end="", flush=True)
        x = record_take(seconds)
        peak = save(x, path)
        flag = "  ⚠ 太轻/没录到" if peak < 0.02 else ("  ⚠ 削顶" if peak > 0.99 else "")
        print(f" ok  peak={peak:.2f}{flag}")
        done += 1
    print(f"\n{done} clips in {d}")
    return d


def main():
    global BOARD, OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["diag", "pos", "neg"])
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--speaker", default="s0",
                    help="speaker tag; use a new one per person")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--board", metavar="USER@HOST",
                    help="record with the board's PDM mic over ssh")
    args = ap.parse_args()

    if args.board:
        BOARD = args.board
        OUT = ROOT / "data" / "real_pdm"
        subprocess.run(["ssh", BOARD, "arecord -l | grep -q rpmsgmic"],
                       check=True)
        print("input device: board PDM mic (hw:rpmsgmic,0) via", BOARD)
    else:
        import sounddevice as sd
        print("input device:", sd.query_devices(kind="input")["name"])

    if args.kind == "neg":
        prompts = [(f"n{j:02d}", f"说：「{NEG_PROMPTS[j % len(NEG_PROMPTS)]}」")
                   for j in range(args.n)]
    elif args.kind == "pos":
        prompts = [(POS_CONDS[j % len(POS_CONDS)][0],
                    POS_CONDS[j % len(POS_CONDS)][1]) for j in range(args.n)]
    else:  # diag: 6 positives across conditions + 6 hard negatives
        prompts = [(c, t) for c, t in POS_CONDS] + \
                  [(f"n{j:02d}", f"说：「{NEG_PROMPTS[j]}」") for j in range(6)]

    session(args.kind, prompts, args.speaker, args.seconds)
    rel = (OUT / args.kind).relative_to(ROOT)
    lab = 0 if args.kind == "neg" else 1
    print(f"\n评分：  python3 score_stream.py --label {lab} {rel}/*.wav")
    if BOARD and args.kind != "diag":
        print("参训：  python3 mk_manifest_real.py && "
              "python3 augment_and_cache.py && python3 train.py")


if __name__ == "__main__":
    main()
