#!/usr/bin/env python3
"""Build the augmented training cache from the raw corpus.

Reads  data/manifest.json (+ optional data/manifest_rerec.json with clips
re-recorded through the board's real PDM mic) and data/noise_raw/*.wav
(optional room-noise recordings), produces:

  data/cache.npz    X: (N,150,40) float16 raw log-mel, y: int8,
                    group: int32 (source-clip id, for leak-free splits)
  data/norm.npz     mean/std per mel bin over the whole cache
  data/golden_*.wav + data/golden.npz   two fixed windows for the C parity
                    test (embedded into kws_golden.h by export_c.py)

Window layout: positives place the (possibly speed-warped) phrase so it ends
0.02-0.30 s before the window's end; 30% of positive clips also contribute a
truncated-phrase HARD NEGATIVE so the model only fires on the full phrase.
"""

import json
import zlib
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly, fftconvolve

import kws_common as K


def clip_group(m):
    """Stable split-group id per source utterance.  The re-recorded copy of
    a clip carries a rerec_ prefix but is the SAME utterance — give both
    the same group so one can never train while the other validates.
    """
    stem = Path(m["file"]).stem
    if stem.startswith("rerec_"):
        stem = stem[len("rerec_"):]
    return zlib.crc32(stem.encode()) & 0x7fffffff

ROOT = Path(__file__).parent
DATA = ROOT / "data"
RNG = np.random.default_rng(20260814)

WS = K.WINDOW_SAMPLES          # 24240
SR = K.SR

POS_VARIANTS = 8
NEG_VARIANTS = 4
NOISE_WINDOWS = 700
TONAL_WINDOWS = 300


def load_wav(path):
    pcm, sr = sf.read(path, dtype="float32", always_2d=False)
    if pcm.ndim > 1:
        pcm = pcm.mean(axis=1)
    assert sr == SR, f"{path}: {sr} != {SR}"
    return pcm


def trim_silence(x, thresh_db=35.0, pad_ms=60, return_idx=False):
    """Energy trim relative to peak RMS over 20 ms hops.

    Only reliable on clean TTS audio.  Re-recorded clips sit a mere
    7-10 dB above the room floor, where an energy gate keeps the whole
    file; those are cut by extract_rerec_phrase() instead.
    """
    hop = SR // 50
    n = len(x) // hop
    if n == 0:
        return (x, 0, len(x)) if return_idx else x
    rms = np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    top = rms.max()
    keep = np.flatnonzero(rms > top * 10 ** (-thresh_db / 20))
    if len(keep) == 0:
        return (x, 0, len(x)) if return_idx else x
    pad = pad_ms * SR // 1000
    a = max(0, keep[0] * hop - pad)
    b = min(len(x), (keep[-1] + 1) * hop + pad)
    return (x[a:b], a, b) if return_idx else x[a:b]


def extract_rerec_phrase(rerec, orig):
    """Cut the played-back phrase out of a board re-recording.

    The re-recording is the original clip played through speaker+room+mic
    with an uncertain lead-in (arecord startup jitter).  FFT
    cross-correlation against the original pins the playback offset to
    ~10 ms; the phrase bounds then come from the ORIGINAL's clean energy
    trim, so the room floor never fools the gate.  Returns None when the
    correlation peak is too weak to trust (e.g. silent playback).
    """
    _, a, b = trim_silence(orig, return_idx=True)
    corr = fftconvolve(rerec, orig[::-1], mode="valid")
    lag = int(np.argmax(np.abs(corr)))
    peak = np.abs(corr[lag])
    floor = np.median(np.abs(corr)) + 1e-9
    if peak / floor < 4.0:
        return None
    lo = max(0, lag + a - int(0.10 * SR))
    hi = min(len(rerec), lag + b + int(0.12 * SR))
    if hi - lo < int(0.4 * SR):
        return None
    return rerec[lo:hi]


def speed_warp(x, s):
    """Time-scale by s (>1 = faster/shorter); pitch shifts too, that's fine."""
    p = max(1, int(round(100 / s)))
    return resample_poly(x, p, 100).astype(np.float32)


def vary_pause(phrase):
    """Re-time the pause between 你好 and openvela (clean TTS only).

    edge-tts renders a comma as ~0.2 s, every time.  People pause anywhere
    from nothing to half a second, and the model must not learn the TTS
    timing as part of the keyword.  Finds the quietest 20 ms in the middle
    of the phrase and inserts up to 0.35 s of floor-level noise there, or
    removes up to 0.15 s around it when that span is already quiet.
    """
    hop = SR // 50
    n = len(phrase) // hop
    if n < 20:
        return phrase
    rms = np.sqrt((phrase[:n * hop].reshape(n, hop) ** 2).mean(axis=1))
    lo, hi = int(n * 0.25), int(n * 0.65)
    k = lo + int(np.argmin(rms[lo:hi]))
    d = RNG.uniform(-0.15, 0.35)
    if d >= 0:
        floor = max(float(rms[k]), 1e-4)
        gap = RNG.standard_normal(int(d * SR)).astype(np.float32) * floor
        return np.concatenate([phrase[:k * hop], gap, phrase[k * hop:]])
    half = int(-d * SR / 2) // hop
    a, b = max(0, k - half), min(n, k + half + 1)
    if rms[a:b].max() < rms.max() * 10 ** (-25 / 20):   # only cut quiet
        return np.concatenate([phrase[:a * hop], phrase[b * hop:]])
    return phrase


def pink_noise(n):
    w = RNG.standard_normal(n // 2 + 1) + 1j * RNG.standard_normal(n // 2 + 1)
    f = np.arange(len(w)) + 1.0
    x = np.fft.irfft(w / np.sqrt(f), n=n)
    return (x / (np.abs(x).max() + 1e-9)).astype(np.float32)


class NoiseBank:
    def __init__(self, neg_paths):
        self.neg_paths = neg_paths
        self.room = []
        for p in sorted((DATA / "noise_raw").glob("*.wav")) \
                if (DATA / "noise_raw").is_dir() else []:
            if p.stem.endswith("_test"):      # held out for eval_suite.py
                continue
            try:
                self.room.append(load_wav(p))
            except Exception:
                pass

    def babble(self, n):
        out = np.zeros(n, dtype=np.float32)
        for _ in range(6):
            x = load_wav(self.neg_paths[RNG.integers(len(self.neg_paths))])
            if len(x) < n:
                x = np.tile(x, n // len(x) + 1)
            o = RNG.integers(0, len(x) - n + 1)
            out += x[o:o + n]
        return out / 6.0

    def sample(self, n, kind=None):
        if kind is None:
            if self.room:      # the real PDM floor is what the board hears
                kind = RNG.choice(["white", "pink", "babble", "room"],
                                  p=[0.15, 0.15, 0.25, 0.45])
            else:
                kind = RNG.choice(["white", "pink", "babble"])
        if kind == "white":
            return RNG.standard_normal(n).astype(np.float32) * 0.3, kind
        if kind == "pink":
            return pink_noise(n) * 0.5, kind
        if kind == "babble":
            return self.babble(n), kind
        x = self.room[RNG.integers(len(self.room))]
        if len(x) < n:
            x = np.tile(x, n // len(x) + 1)
        o = RNG.integers(0, len(x) - n + 1)
        return x[o:o + n].copy(), kind


def tonal_window():
    """Chirps / sirens / pure tones: non-speech sounds that fooled an early
    model (a full-band sweep scored 0.85).  All labeled negative.
    """
    n = WS
    t = np.arange(n) / SR
    kind = RNG.integers(4)
    if kind == 0:                       # linear chirp, either direction
        f0, f1 = sorted(RNG.uniform(80, 7500, size=2))
        if RNG.random() < 0.5:
            f0, f1 = f1, f0
        f = f0 + (f1 - f0) * t / t[-1]
    elif kind == 1:                     # pure tone (optional vibrato)
        base = RNG.uniform(100, 4000)
        f = base * (1 + 0.02 * np.sin(2 * np.pi * RNG.uniform(3, 8) * t)
                    * (RNG.random() < 0.5))
    elif kind == 2:                     # siren-like FM warble
        lo, hi = sorted(RNG.uniform(200, 3000, size=2))
        f = lo + (hi - lo) * 0.5 * (
            1 + np.sin(2 * np.pi * RNG.uniform(0.5, 3.0) * t))
    else:                               # dual tone (DTMF-ish)
        f = RNG.uniform(300, 1500)
        f2 = RNG.uniform(600, 3000)
        x = 0.5 * np.sin(2 * np.pi * f * t) + \
            0.5 * np.sin(2 * np.pi * f2 * t)
        return (x * RNG.uniform(0.1, 0.6)).astype(np.float32)
    x = np.sin(2 * np.pi * np.cumsum(f) / SR)
    return (x * RNG.uniform(0.1, 0.6)).astype(np.float32)


def synth_rir():
    n = int(0.25 * SR)
    t = np.arange(n) / SR
    tau = RNG.uniform(0.03, 0.12)
    h = RNG.standard_normal(n).astype(np.float32) * np.exp(-t / tau)
    h[0] = 1.0
    return (h / np.sqrt((h ** 2).sum())).astype(np.float32)


def compose_window(phrase, bank, end_back_s=(0.0, 0.40), reverb_p=0.35,
                   snr_db=(5.0, 35.0), noise_p=1.0):
    """Place `phrase` into a WS window over a noise bed; returns int16.

    Every window gets a noise bed: a live stream never contains digital
    silence, and log(eps) frames make windows the board can never produce
    (the v6 recipe left 25% of windows with an all-zero floor).
    """
    win = np.zeros(WS, dtype=np.float32)
    if phrase is not None and len(phrase):
        limit = WS - int(0.35 * SR)                       # 1.665 s
        if len(phrase) > limit:
            # Real speakers stretch the phrase past the 2.0 s window (the
            # Mandarin 欧朋维拉 renderings run 2.0-2.3 s); v6 squeezed
            # anything longer than 1.67 s down to 1.6 s, so the model never
            # saw a phrase filling the window.  Now: half the time keep the
            # natural pace and let placement clip the head (<= 0.5 s: what
            # the board sees the moment a slow phrase ends), else rescale
            # to a random length that still nearly fills the window.
            if len(phrase) <= WS + int(0.30 * SR) and RNG.random() < 0.5:
                end_back_s = (0.0, 0.20)
            else:
                target = RNG.uniform(1.60, 1.95) * SR
                phrase = speed_warp(phrase, len(phrase) / target)
        end = WS - int(RNG.uniform(*end_back_s) * SR)
        a = max(0, end - len(phrase))
        seg = phrase[max(0, len(phrase) - end):]
        win[a:a + len(seg)] += seg
    if RNG.random() < reverb_p:
        win = fftconvolve(win, synth_rir())[:WS].astype(np.float32)
    if RNG.random() < noise_p:
        noise, _ = bank.sample(WS)
        sig_rms = np.sqrt((win ** 2).mean() + 1e-12)
        noi_rms = np.sqrt((noise ** 2).mean() + 1e-12)
        if sig_rms > 1e-5:
            snr = RNG.uniform(*snr_db)
            noise = noise * (sig_rms / noi_rms) * 10 ** (-snr / 20)
        else:  # noise-only window: just set an absolute level
            noise = noise * (10 ** (RNG.uniform(-45, -18) / 20) / noi_rms)
        win = win + noise
    win = win * 10 ** (RNG.uniform(-8, 3) / 20)
    peak = np.abs(win).max()
    if peak > 0.99:
        win = win * (0.99 / peak)
    return (win * 32767).astype(np.int16)


def main():
    manifest = json.loads((DATA / "manifest.json").read_text())
    for extra in ("manifest_rerec.json", "manifest_say.json",
                  "manifest_real.json"):
        if (DATA / extra).exists():
            manifest += json.loads((DATA / extra).read_text())
            print(f"including {extra}")
    manifest = [m for m in manifest if (DATA / m["file"]).exists()]
    pos = [m for m in manifest if m["label"] == 1]
    neg = [m for m in manifest if m["label"] == 0]
    neg_paths = [DATA / m["file"] for m in neg]
    bank = NoiseBank(neg_paths)
    print(f"{len(pos)} positive / {len(neg)} negative clips; "
          f"room noise files: {len(bank.room)}")

    X, y, group = [], [], []
    golden = {}

    def add(pcm_i16, label, gid):
        feat = K.logmel(pcm_i16)
        assert feat.shape == (K.T_FRAMES, K.NMEL), feat.shape
        X.append(feat.astype(np.float16))
        y.append(label)
        group.append(gid)
        return feat

    rerec_durs = []
    for gi, m in enumerate(pos):
        gid = clip_group(m)
        x = load_wav(DATA / m["file"])
        if m.get("voice") == "board-channel":
            stem = Path(m["file"]).stem[len("rerec_"):]
            orig = load_wav(DATA / m.get("orig", f"pos_raw/{stem}.wav"))
            phrase = extract_rerec_phrase(x, orig)
            if phrase is None:
                continue
            rerec_durs.append(len(phrase) / SR)
        elif m.get("voice") == "human-pdm":
            # a take off the PDM mic, already cut to the utterance (ASR
            # segmentation or record_real.py): speech sits only 7-12 dB
            # over the floor, an energy gate would keep the whole clip
            phrase = x
        else:
            phrase = trim_silence(x)
        if len(phrase) < 0.4 * SR:
            continue
        human = m.get("voice") == "human-pdm"
        clean = not human and m.get("voice") != "board-channel"
        # The handful of human takes are the only positives from the real
        # world: oversample them (x24; x48 made the owner's room floor and
        # voice a wake cue on their own), and go easy on the synthetic
        # noise (they carry the real floor already)
        nvar = POS_VARIANTS * 3 if human else POS_VARIANTS
        for v in range(nvar):
            # up to 1.35x faster: the owner's quick takes run 0.8-1.0 s for
            # the whole phrase, below anything a 1.15x-warped TTS clip gave
            s = RNG.uniform(0.85, 1.35)
            p = vary_pause(phrase) if clean and RNG.random() < 0.5 else phrase
            if human:
                w = compose_window(speed_warp(p, s), bank, reverb_p=0.15,
                                   snr_db=(12.0, 40.0))
            else:
                w = compose_window(speed_warp(p, s), bank)
            feat = add(w, 1, gid)
            if "pos" not in golden and gi % 17 == 3 and v == 0:
                golden["pos"] = (w, feat)
        # truncated phrase = hard negative.  v7a fired on "你好，欧朋" at
        # 0.99: cut every clip, twice, anywhere from 你好 alone to one
        # syllable short of the end — only the complete phrase may fire
        for _ in range(2):
            cut = phrase[:int(len(phrase) * RNG.uniform(0.35, 0.68))]
            add(compose_window(cut, bank), 0, gid)

    for gj, m in enumerate(neg):
        x = load_wav(DATA / m["file"])
        gid = clip_group(m)
        real = m.get("voice") in ("real-mic", "human-pdm")
        text = m.get("text", "")
        hard = text.startswith(("你好", "您好", "喂")) or \
            any(k in text for k in ("维拉", "薇拉", "欧朋", "欧维"))
        if m.get("voice") == "human-pdm":
            # the owner's own non-wake speech, bare "openvela", room
            # floor and knocks: the counterweight to the oversampled
            # human positives (same voice, same floor, label 0)
            nvar = 8
        elif real:
            # real recordings already carry a room and a floor: little
            # extra reverb, gentler noise; long ones (meetings, TV) yield
            # more crops
            nvar = min(12, 2 + int(len(x) / SR / 1.5))
        elif hard:
            # 你好-openers and brand-word confusables: v7a still fired on
            # 你好呀在吗 / 你好，维拉 at 0.98 with 4 variants
            nvar = 2 * NEG_VARIANTS
        else:
            nvar = NEG_VARIANTS
        for v in range(nvar):
            if len(x) > WS:
                o = RNG.integers(0, len(x) - WS + 1)
                seg = x[o:o + WS].copy()
            else:
                seg = x
            if real:
                w = compose_window(seg, bank, end_back_s=(0.0, 0.6),
                                   reverb_p=0.15, snr_db=(10.0, 40.0))
            else:
                w = compose_window(seg, bank, end_back_s=(0.0, 0.6))
            feat = add(w, 0, gid)
            if "neg" not in golden and gj % 13 == 5 and v == 0:
                golden["neg"] = (w, feat)

    for k in range(NOISE_WINDOWS):
        w = compose_window(None, bank)
        add(w, 0, 200000 + k)

    for k in range(TONAL_WINDOWS):
        x = tonal_window()
        if RNG.random() < 0.4:          # sometimes over a noise bed
            noise, _ = bank.sample(WS)
            x = x + noise * RNG.uniform(0.05, 0.4)
        peak = np.abs(x).max()
        if peak > 0.99:
            x = x * (0.99 / peak)
        add((x * 32767).astype(np.int16), 0, 300000 + k)

    if rerec_durs:
        d = np.asarray(rerec_durs)
        print(f"rerec phrases: {len(d)} extracted, "
              f"duration {d.mean():.2f}s mean / {d.max():.2f}s max "
              f"(must match TTS ~1.7s — 4s means the xcorr cut failed)")

    X = np.stack(X)
    y = np.asarray(y, dtype=np.int8)
    group = np.asarray(group, dtype=np.int32)
    Xf = X.astype(np.float32)
    mean = Xf.reshape(-1, K.NMEL).mean(axis=0)
    std = Xf.reshape(-1, K.NMEL).std(axis=0) + 1e-3
    np.savez_compressed(DATA / "cache.npz", X=X, y=y, group=group)
    np.savez(DATA / "norm.npz", mean=mean.astype(np.float32),
             std=std.astype(np.float32))

    for name, (w, feat) in golden.items():
        sf.write(DATA / f"golden_{name}.wav", w, SR, subtype="PCM_16")
    np.savez(DATA / "golden.npz",
             pos_pcm=golden["pos"][0], pos_feat=golden["pos"][1],
             neg_pcm=golden["neg"][0], neg_feat=golden["neg"][1])

    print(f"cache: {X.shape[0]} windows "
          f"({(y == 1).sum()} pos / {(y == 0).sum()} neg), "
          f"{X.nbytes / 1e6:.0f} MB float16")


if __name__ == "__main__":
    main()
