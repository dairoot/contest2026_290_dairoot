#!/usr/bin/env bash
# One-shot corpus -> cache -> train -> export chain, safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"
python3 gen_data.py
python3 augment_and_cache.py
python3 train.py
python3 export_c.py
echo PIPELINE-DONE
