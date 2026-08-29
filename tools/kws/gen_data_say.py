#!/usr/bin/env python3
"""Extra corpus from the macOS built-in TTS (`say`) — offline, ~1 minute.

edge-tts gives 14 voices from one vendor; Apple ships 19 Chinese voices
(8 neural voices x 大陆/台湾 + the three legacy ones) that sound nothing
like them, and they render 你好，openvela / 欧朋维拉 with their own
letter-to-sound rules.  More renderings of the phrase = less chance the
model keys on one vendor's prosody.

Output: data/say_pos/*.wav, data/say_neg/*.wav (16 kHz mono s16) and
data/manifest_say.json, merged by augment_and_cache.py.  Re-running skips
existing clips.
"""

import json
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from gen_data import NEG_SKIP, NEG_TEXTS, POS_TEXTS, POS_TEXTS_EXTRA, SR

ROOT = Path(__file__).parent
DATA = ROOT / "data"

VOICES = ["Tingting", "Meijia", "Sinji"] + [
    f"{n} (中文（{region}）)"
    for n in ["Eddy", "Flo", "Grandma", "Grandpa", "Reed", "Rocko", "Sandy",
              "Shelley"]
    for region in ["中国大陆", "台湾"]]

RATES = [150, 180, 215]            # words per minute; 180 ≈ default

# The hard negatives: the original 你好/维拉 block at the head of
# NEG_TEXTS plus the round-7 confusables at its tail.
HARD = list(range(0, 18)) + list(range(len(NEG_TEXTS) - 17, len(NEG_TEXTS)))
EASY = [i for i in range(len(NEG_TEXTS)) if i not in HARD]


def say(text, voice, rate, out):
    tmp = out.with_suffix(".tmp.wav")
    subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", str(tmp),
                    "--data-format=LEI16@16000", text], check=True)
    pcm, sr = sf.read(tmp, dtype="float32")
    tmp.unlink()
    assert sr == SR, sr
    if pcm.ndim > 1:
        pcm = pcm.mean(axis=1)
    peak = np.abs(pcm).max()
    if peak < 1e-3 or len(pcm) < SR // 4:
        return False
    if peak > 0.7:                     # same rule as gen_data.decode_mp3
        pcm = pcm * (0.7 / peak)
    sf.write(out, pcm, SR, subtype="PCM_16")
    return True


def main():
    (DATA / "say_pos").mkdir(parents=True, exist_ok=True)
    (DATA / "say_neg").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(20260829)
    manifest = []
    made = 0
    texts = POS_TEXTS + POS_TEXTS_EXTRA
    for vi, voice in enumerate(VOICES):
        for ti, text in enumerate(texts):
            rates = {int(rng.choice(RATES))}
            if ti == 0:
                rates.add(180)
            for rate in sorted(rates):
                name = f"say_pos_v{vi:02d}_t{ti}_r{rate}.wav"
                out = DATA / "say_pos" / name
                if not out.exists():
                    if not say(text, voice, rate, out):
                        continue
                    made += 1
                manifest.append({"file": f"say_pos/{name}", "label": 1,
                                 "voice": f"say:{voice}", "text": text})
        picks = list(rng.choice(HARD, size=6, replace=False)) + \
            list(rng.choice(EASY, size=8, replace=False))
        for si in picks:
            rate = int(rng.choice(RATES))
            name = f"say_neg_s{si:03d}_v{vi:02d}.wav"
            out = DATA / "say_neg" / name
            if NEG_TEXTS[si] in NEG_SKIP:
                continue
            if not out.exists():
                if not say(NEG_TEXTS[si], voice, rate, out):
                    continue
                made += 1
            manifest.append({"file": f"say_neg/{name}", "label": 0,
                             "voice": f"say:{voice}", "text": NEG_TEXTS[si]})
        print(f"{voice}: done", flush=True)

    (DATA / "manifest_say.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1))
    npos = sum(1 for m in manifest if m["label"] == 1)
    print(f"manifest_say.json: {npos} positives, {len(manifest) - npos} "
          f"negatives ({made} newly synthesized)")


if __name__ == "__main__":
    main()
