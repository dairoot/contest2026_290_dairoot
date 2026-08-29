#!/usr/bin/env python3
"""Real-microphone speech -> negatives (data/manifest_real.json) + test list.

Source: the asr-server's audio_logs — VAD-cut utterances of whatever the
board's PDM mic heard (meetings, TV, people talking to it).  Not one of
them is the wake phrase (transcripts checked), so they are the real-world
negatives the corpus never had: v6 false-woke on 16% of them.

  data/real_mic/<day>/         synced from the board (ASR ran on the NPU)
  linux-apps/asr-server/audio_logs/<day>/   Mac-hosted server runs
                               -> copied to data/real_mic/<day>L/

The ASR logs both a partial and a final cut of the same utterance under
one HHMMSS stamp; keep the longest per stamp.  The LAST 30% of every
day (by time) is held out as data/real_test.txt for eval_suite.py, so
the training clips never share a conversation minute with the test ones.
"""

import json
import shutil
from pathlib import Path

import soundfile as sf

ROOT = Path(__file__).parent
DATA = ROOT / "data"
REAL = DATA / "real_mic"
MAC_LOGS = ROOT.parent.parent / "linux-apps" / "asr-server" / "audio_logs"
TEST_FRAC = 0.30


def main():
    # mirror the Mac-hosted server's logs next to the board's
    if MAC_LOGS.is_dir():
        for day in sorted(p for p in MAC_LOGS.iterdir() if p.is_dir()):
            dst = REAL / f"{day.name}L"
            dst.mkdir(parents=True, exist_ok=True)
            n = 0
            for wav in day.glob("*.wav"):
                if not (dst / wav.name).exists():
                    shutil.copy2(wav, dst / wav.name)
                    n += 1
            if n:
                print(f"copied {n} new clips from {day} -> {dst.name}/")

    train, test = [], []
    for day in sorted(p for p in REAL.iterdir() if p.is_dir()):
        by_stamp = {}
        for wav in day.glob("*.wav"):
            stamp = wav.name[:6]
            if not stamp.isdigit():
                continue
            dur = sf.info(wav).duration
            if stamp not in by_stamp or dur > by_stamp[stamp][1]:
                by_stamp[stamp] = (wav, dur)
        stamps = sorted(by_stamp)
        cut = int(len(stamps) * (1.0 - TEST_FRAC))
        for i, stamp in enumerate(stamps):
            wav, dur = by_stamp[stamp]
            rel = f"real_mic/{day.name}/{wav.name}"
            if i < cut:
                train.append({"file": rel, "label": 0, "voice": "real-mic",
                              "text": wav.stem[7:]})
            else:
                test.append(rel)
        print(f"{day.name}: {len(stamps)} utterances "
              f"({cut} train / {len(stamps) - cut} test)")

    # Human takes recorded by record_real.py --board: label from the
    # folder, every 3rd take held out (takes cycle through conditions, so
    # the test share covers near/far/fast/slow/soft/loud alike).
    test_pos = []
    for kind, label in (("pos", 1), ("neg", 0), ("floor", 0)):
        wavs = sorted((DATA / "real_pdm" / kind).glob("*.wav"))
        for i, wav in enumerate(wavs):
            rel = f"real_pdm/{kind}/{wav.name}"
            if i % 3 == 2:
                (test_pos if label else test).append(rel)
            else:
                train.append({"file": rel, "label": label,
                              "voice": "human-pdm", "text": wav.stem})
        if wavs:
            print(f"real_pdm/{kind}: {len(wavs)} human takes")

    (DATA / "manifest_real.json").write_text(
        json.dumps(train, ensure_ascii=False, indent=1))
    (DATA / "real_test.txt").write_text("\n".join(test) + "\n")
    (DATA / "real_test_pos.txt").write_text("\n".join(test_pos) + "\n")
    npos = sum(1 for m in train if m["label"] == 1)
    print(f"manifest_real.json: {len(train) - npos} negatives, {npos} "
          f"positives; held out: {len(test)} neg, {len(test_pos)} pos")


if __name__ == "__main__":
    main()
