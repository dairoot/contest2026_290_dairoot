"""Single source of truth for the KWS front-end numerics.

The C implementation in board/contest_board/src/kws_frontend.c mirrors this
file exactly; export_c.py emits the lookup tables (Hann window, mel filter
bank, normalization stats) from here so the two can never drift.  Any change
to a constant below invalidates the trained model AND the exported tables.

Front-end spec (frozen):
  16 kHz mono int16 -> frames of WIN=400 (25 ms) every HOP=160 (10 ms)
  per frame: DC removal -> Hann(400) -> zero-pad to NFFT=512 -> rFFT
             -> power spectrum (no scaling) -> 40 mel (HTK, 20..7600 Hz)
             -> ln(x + LOG_EPS) -> (x - MEAN[m]) * STD_INV[m]
  model input: last T=150 frames (1.5 s) of 40-dim normalized log-mel
"""

import numpy as np

SR = 16000
WIN = 400
HOP = 160
NFFT = 512
NBIN = NFFT // 2 + 1          # 257
NMEL = 40
FMIN = 20.0
FMAX = 7600.0
LOG_EPS = 1e-6

# 2.0 s of context.  The phrase itself (你好，openvela with its natural
# comma pause) averages 1.65 s across voices and rates — a 1.5 s window
# could not contain most complete renditions and streaming recall capped
# at ~40-50% because of it.
T_FRAMES = 200
WINDOW_SAMPLES = (T_FRAMES - 1) * HOP + WIN   # 32240 samples per window


def hann_window():
    """Periodic Hann, matches the C table."""
    n = np.arange(WIN)
    return (0.5 - 0.5 * np.cos(2.0 * np.pi * n / WIN)).astype(np.float32)


def _hz_to_mel(f):
    return 2595.0 * np.log10(1.0 + np.asarray(f, dtype=np.float64) / 700.0)


def _mel_to_hz(m):
    return 700.0 * (10.0 ** (np.asarray(m, dtype=np.float64) / 2595.0) - 1.0)


def mel_filterbank():
    """(NMEL, NBIN) triangular HTK-style filter bank, un-normalized."""
    edges = _mel_to_hz(np.linspace(_hz_to_mel(FMIN), _hz_to_mel(FMAX),
                                   NMEL + 2))
    bin_hz = np.arange(NBIN) * (SR / NFFT)
    fb = np.zeros((NMEL, NBIN))
    for m in range(NMEL):
        lo, ctr, hi = edges[m], edges[m + 1], edges[m + 2]
        up = (bin_hz - lo) / (ctr - lo)
        down = (hi - bin_hz) / (hi - ctr)
        fb[m] = np.clip(np.minimum(up, down), 0.0, None)
    return fb.astype(np.float32)


_HANN = hann_window()
_FB = mel_filterbank()


def logmel(pcm_i16):
    """int16 PCM -> (nframes, NMEL) raw log-mel (no normalization).

    Streaming-equivalent: frame i covers samples [i*HOP, i*HOP+WIN).
    float32 throughout, mirroring the C code bit-for-bit as far as numpy
    allows (differences stay < 1e-4 after the log).
    """
    x = (np.asarray(pcm_i16, dtype=np.float32) / np.float32(32768.0))
    nframes = 0 if len(x) < WIN else (len(x) - WIN) // HOP + 1
    out = np.empty((nframes, NMEL), dtype=np.float32)
    buf = np.zeros(NFFT, dtype=np.float32)
    for i in range(nframes):
        fr = x[i * HOP:i * HOP + WIN]
        fr = fr - np.float32(fr.mean())          # DC removal
        buf[:WIN] = fr * _HANN
        spec = np.fft.rfft(buf)                  # complex64
        power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
        out[i] = np.log(_FB @ power + np.float32(LOG_EPS))
    return out


def normalize(feat, mean, std):
    """feat: (n, NMEL) raw log-mel; mean/std: (NMEL,) training-set stats."""
    return (feat - mean) / std


def windows_from_clip(pcm_i16):
    """How many full T_FRAMES windows fit; helper for dataset building."""
    return max(0, len(pcm_i16) - WINDOW_SAMPLES) // HOP + 1
