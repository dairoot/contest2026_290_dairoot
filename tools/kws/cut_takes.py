#!/usr/bin/env python3
"""Board-mic session recording -> labeled real_pdm clips.

Record a free-form session on the board (say the wake phrase a dozen
times at different distances, then some other sentences):

    ssh <板> 'arecord -q -D hw:rpmsgmic,0 -f S16_LE -r 16000 -c 1 -d 300 ~/take.wav'
    scp <板>:take.wav data/real_pdm/raw/take3.wav
    python3 cut_takes.py data/real_pdm/raw/take3.wav --speaker s0

Utterances are found by energy (7 dB over the floor), transcribed with the
asr-server's local SenseVoice (its .venv), and labeled from the text:
你好 + something vela-ish -> positive, other speech -> negative, breaths /
garbage (very short or no text) dropped.  Prints the table so you can
override with --pos / --neg index lists.  Clips land in
data/real_pdm/{pos,neg}/ and join training via mk_manifest_real.py.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).parent
ASR = ROOT.parent.parent / "linux-apps" / "asr-server"
SR = 16000

POS_RE = re.compile(r"你好.*(o|欧|噢|喔|范|微|薇|维|没了|米啦|啦|拉|了)", re.I)


def read_raw_wav(path):
    """arecord output; the header length is stale if it was killed."""
    raw = Path(path).read_bytes()
    return np.frombuffer(raw[44:], dtype=np.int16)


def segments(pcm, gate_db=7.0, gap_s=0.6, min_s=0.35):
    x = pcm.astype(np.float32) / 32768
    hop = SR // 20
    n = len(x) // hop
    e = 20 * np.log10(np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(1))
                      + 1e-9)
    floor = np.percentile(e, 10)
    act = e > floor + gate_db
    out, s, last = [], None, None
    for i, a in enumerate(act):
        if a:
            if s is None:
                s = i
            last = i
        elif s is not None and i - last > gap_s * 20:
            out.append((s * hop, (last + 1) * hop))
            s = None
    if s is not None:
        out.append((s * hop, (last + 1) * hop))
    return floor, [(a, b) for a, b in out if (b - a) / SR >= min_s]


def transcribe(pcm, segs):
    """Run SenseVoice in the asr-server venv (separate interpreter)."""
    tmp = ROOT / "data" / "real_pdm" / "raw" / "_asr_tmp.npz"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    x = pcm.astype(np.float32) / 32768
    clips = []
    for a, b in segs:
        c = x[max(0, a - int(0.4 * SR)):b + int(0.4 * SR)]
        clips.append(c / (np.abs(c).max() + 1e-9) * 0.5)
    np.savez(tmp, **{f"c{i}": c for i, c in enumerate(clips)})
    code = (
        "import sys, numpy as np, json; sys.path.insert(0, %r)\n"
        "from asr_model.asr_sense_voice_onnx import SenseVoiceOnnxWorker\n"
        "z = np.load(%r); w = SenseVoiceOnnxWorker()\n"
        "out = [w.infer_batch([z[f'c{i}']])[0]['text'].split('|>')[-1] "
        "for i in range(len(z.files))]\n"
        "print('##' + json.dumps(out, ensure_ascii=False))"
        % (str(ASR), str(tmp)))
    r = subprocess.run([str(ASR / ".venv" / "bin" / "python"), "-c", code],
                       capture_output=True, text=True, cwd=str(ASR))
    tmp.unlink(missing_ok=True)
    for line in r.stdout.splitlines():
        if line.startswith("##"):
            return json.loads(line[2:])
    sys.exit("ASR failed:\n" + r.stderr[-2000:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--speaker", default="s0")
    ap.add_argument("--pos", default="", help="force these indices positive")
    ap.add_argument("--neg", default="", help="force these indices negative")
    ap.add_argument("--drop", default="", help="skip these indices")
    ap.add_argument("--floor", type=int, default=0, metavar="N",
                    help="also cut N speech-free 2.1 s windows into "
                         "real_pdm/floor/ (negatives: the owner's room "
                         "floor must not become a wake cue)")
    ap.add_argument("--dry", action="store_true",
                    help="table only (floor clips are still written)")
    args = ap.parse_args()
    force_pos = {int(i) for i in args.pos.split(",") if i}
    force_neg = {int(i) for i in args.neg.split(",") if i}
    force_drop = {int(i) for i in args.drop.split(",") if i}

    pcm = read_raw_wav(args.wav)
    floor, segs = segments(pcm)
    texts = transcribe(pcm, segs)
    tag = Path(args.wav).stem
    pos_dir, neg_dir = ROOT / "data/real_pdm/pos", ROOT / "data/real_pdm/neg"
    pos_dir.mkdir(parents=True, exist_ok=True)
    neg_dir.mkdir(parents=True, exist_ok=True)
    npos = nneg = 0
    print(f"{Path(args.wav).name}: {len(pcm) / SR:.0f} s, floor "
          f"{floor:.1f} dBFS, {len(segs)} utterances")
    for i, ((a, b), text) in enumerate(zip(segs, texts)):
        x = pcm[a:b].astype(np.float32) / 32768
        lvl = 20 * np.log10(np.sqrt((x ** 2).mean()) + 1e-9)
        if i in force_drop:
            lab = "drop"
        elif i in force_pos:
            lab = "pos"
        elif i in force_neg:
            lab = "neg"
        elif len(text.strip("。，？！ ")) < 2:
            lab = "drop"
        elif POS_RE.search(text):
            lab = "pos"
        elif "你好" in text:
            lab = "?"                      # 你好 + garbage: review
        else:
            lab = "neg"
        print(f"{i:3d} {a / SR:6.1f}-{b / SR:6.1f}s {lvl:6.1f} dBFS  "
              f"{lab:4s} {text}")
        if args.dry or lab in ("drop", "?"):
            continue
        # keep the same 0.4 s of context the ASR saw: the energy gate
        # regularly misses a soft 你, and a clip that starts at 好 or at
        # openvela is a mislabeled positive
        pad = int(0.4 * SR)
        clip = pcm[max(0, a - pad):min(len(pcm), b + pad)]
        if lab == "pos":
            sf.write(pos_dir / f"pos_{args.speaker}_{tag}_{i:03d}.wav", clip,
                     SR, subtype="PCM_16")
            npos += 1
        else:
            sf.write(neg_dir / f"neg_{args.speaker}_{tag}_{i:03d}.wav", clip,
                     SR, subtype="PCM_16")
            nneg += 1
    print(f"wrote {npos} pos / {nneg} neg clips  "
          f"(re-run with --pos/--neg to override '?' rows)")

    if args.floor:
        # windows at least 0.5 s clear of any detected utterance (and of
        # the ASR-dropped blips), sampled with a fixed seed per recording
        rng = np.random.default_rng(zlib_crc(tag))
        win = int(2.1 * SR)
        busy = np.zeros(len(pcm), dtype=bool)
        for a, b in segs:
            busy[max(0, a - SR):b + SR] = True
        fdir = ROOT / "data/real_pdm/floor"
        fdir.mkdir(parents=True, exist_ok=True)
        got, tries = 0, 0
        while got < args.floor and tries < 20000:
            tries += 1
            s = int(rng.integers(0, len(pcm) - win))
            if busy[s:s + win].any():
                continue
            sf.write(fdir / f"floor_{args.speaker}_{tag}_{got:03d}.wav",
                     pcm[s:s + win], SR, subtype="PCM_16")
            got += 1
        print(f"wrote {got} floor clips to {fdir.relative_to(ROOT)}")


def zlib_crc(s):
    import zlib
    return zlib.crc32(s.encode()) & 0x7fffffff


if __name__ == "__main__":
    main()
