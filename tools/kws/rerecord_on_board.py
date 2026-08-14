#!/usr/bin/env python3
"""Board-side corpus re-recording (channel adaptation).

Runs ON the KickPi Linux side.  For every wav in ~/rerec_src/, plays it
through the ES8388 speaker while recording the openvela PDM mic via the
rpmsgmic ALSA card, and stores the recording in ~/rerec_out/rerec_<name>.
stdlib only; survives ssh disconnects when started with nohup.

The recording covers the clip plus lead-in/out; the training augmenter's
energy trimmer handles the alignment, so no DSP is needed here.
"""

import os
import struct
import subprocess
import sys
import time

SRC = os.path.expanduser("~/rerec_src")
OUT = os.path.expanduser("~/rerec_out")
PLAY_DEV = "plughw:rockchipes8388,0"
REC_DEV = "hw:rpmsgmic,0"


def wav_duration(path):
    with open(path, "rb") as f:
        hdr = f.read(64)
    # PCM16 mono 16k from the pipeline: data size at the standard offset
    try:
        n = struct.unpack("<I", hdr[40:44])[0]
        return max(0.5, n / 2 / 16000.0)
    except Exception:
        return 3.0


def main():
    os.makedirs(OUT, exist_ok=True)
    subprocess.run(["amixer", "-c", "rockchipes8388", "sset", "Speaker",
                    "80%"], capture_output=True)
    subprocess.run(["amixer", "-c", "rockchipes8388", "sset", "PCM", "90%"],
                   capture_output=True)
    subprocess.run(["amixer", "-c", "rockchipes8388", "sset", "Speaker",
                    "on"], capture_output=True)

    names = sorted(n for n in os.listdir(SRC) if n.endswith(".wav"))
    done = 0
    t0 = time.time()
    for name in names:
        out = os.path.join(OUT, "rerec_" + name)
        if os.path.exists(out):
            done += 1
            continue
        src = os.path.join(SRC, name)
        dur = wav_duration(src) + 1.2
        rec = subprocess.Popen(
            ["arecord", "-D", REC_DEV, "-f", "S16_LE", "-r", "16000",
             "-c", "1", "-d", str(int(dur + 0.999)), out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.35)
        subprocess.run(["aplay", "-D", PLAY_DEV, src],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rec.wait()
        done += 1
        if done % 25 == 0:
            el = time.time() - t0
            print(f"{done}/{len(names)} elapsed {el:.0f}s", flush=True)

    print(f"DONE {done}/{len(names)}", flush=True)


if __name__ == "__main__":
    main()
