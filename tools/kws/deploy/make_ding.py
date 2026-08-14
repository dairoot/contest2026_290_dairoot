#!/usr/bin/env python3
"""Synthesize the wake-acknowledge chime: a bell-like ding-dong (E5 -> C5).

Pure tones with a couple of harmonics and exponential decay — deliberately
tonal, which the KWS model was hardened against (chirp/tone hard negatives
score ~0.02), so the chime played through the speaker cannot re-trigger
the engine.  Output: 16 kHz mono s16, ~0.9 s.
"""

from pathlib import Path

import numpy as np
import soundfile as sf

SR = 16000


def bell(freq, dur, amp):
    t = np.arange(int(dur * SR)) / SR
    env = np.exp(-t * 6.0)
    x = (1.00 * np.sin(2 * np.pi * freq * t) +
         0.35 * np.sin(2 * np.pi * freq * 2.02 * t) +
         0.15 * np.sin(2 * np.pi * freq * 2.99 * t))
    # soft attack to avoid a click
    a = min(len(t), SR // 200)
    env[:a] *= np.linspace(0, 1, a)
    return amp * env * x


def main():
    ding = bell(659.25, 0.45, 0.55)          # E5
    dong = bell(523.25, 0.55, 0.55)          # C5
    gap = np.zeros(int(0.06 * SR))
    x = np.concatenate([ding, gap, dong])
    x = np.clip(x, -0.95, 0.95)
    out = Path(__file__).parent / "ding.wav"
    sf.write(out, (x * 32767).astype(np.int16), SR, subtype="PCM_16")
    print(f"wrote {out} ({len(x)/SR:.2f}s)")


if __name__ == "__main__":
    main()
