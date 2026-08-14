#!/usr/bin/env python3
"""Rebuild data/manifest_rerec.json from the recordings in data/rerec/.

Each rerec_<name>.wav inherits label/text from <name> in manifest.json and
is tagged voice=board-channel (augment_and_cache.py keys on that to run the
xcorr phrase extraction for positives).  Safe to re-run.
"""

import json
from pathlib import Path

DATA = Path(__file__).parent / "data"

by_stem = {Path(m["file"]).stem: m
           for m in json.loads((DATA / "manifest.json").read_text())}

out = []
missing = 0
for wav in sorted((DATA / "rerec").glob("rerec_*.wav")):
    src = by_stem.get(wav.stem[len("rerec_"):])
    if src is None:
        missing += 1
        continue
    out.append({"file": f"rerec/{wav.name}", "label": src["label"],
                "voice": "board-channel", "text": src["text"]})

(DATA / "manifest_rerec.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1))
pos = sum(1 for m in out if m["label"] == 1)
print(f"manifest_rerec.json: {pos} positives, {len(out) - pos} negatives"
      + (f", {missing} without a manifest.json source (skipped)"
         if missing else ""))
