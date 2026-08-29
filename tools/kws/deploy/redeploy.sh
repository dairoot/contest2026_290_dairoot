#!/usr/bin/env bash
# One shot from a trained checkpoint to a running board, for the topology
# where the firmware builds on a remote host (ubt) that cannot reach the
# board, and this machine can reach both:
#
#   export_c.py -> headers + golden vectors
#   -> rsync to build host, cross-compile kws_host, golden parity ON THE BOARD
#   -> build nuttx.bin on the build host
#   -> flash_amp_relay.sh (amp.img relayed through this machine, dd, reboot)
#
# Usage: deploy/redeploy.sh [threshold]      e.g. deploy/redeploy.sh 0.85
#        (threshold overrides checkpoints/config.json before export)

set -euo pipefail

KWS=$(cd "$(dirname "$0")/.." && pwd)
REPO=$(cd "$KWS/../.." && pwd)
BUILD=${BUILD:-ubt}
BOARD=${BOARD:-kickpi@192.168.3.21}
RBUILD=${RBUILD:-'~/openvela-ws'}                 # build host workspace
RREPO="$RBUILD/contest2026_290_dairoot"

cd "$KWS"
if [ -n "${1:-}" ]; then
  python3 - "$1" <<'EOF'
import json, sys
p = "checkpoints/config.json"; c = json.load(open(p)); c["threshold"] = float(sys.argv[1])
json.dump(c, open(p, "w"), indent=1); print("threshold ->", c["threshold"])
EOF
fi
python3 export_c.py 2>&1 | grep -v Warning

rsync -a "$REPO/board/contest_board/src/kws_tables.h" \
         "$REPO/board/contest_board/src/kws_model_data.h" \
         "$BUILD:$RREPO/board/contest_board/src/"
rsync -a host/host_main.c host/Makefile host/kws_golden.h \
         "$BUILD:$RREPO/tools/kws/host/"

echo "== kws_host (cross) + golden on the board =="
ssh "$BUILD" "export PATH=\$HOME/miniconda3/bin:\$PATH; cd $RREPO/tools/kws/host && make -s clean && make -s"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
scp -q "$BUILD:$RREPO/tools/kws/host/kws_host" "$TMP/kws_host"
scp -q "$TMP/kws_host" "$BOARD":/tmp/kws_host
ssh "$BOARD" '/tmp/kws_host golden' | tee "$TMP/golden.txt"
grep -q FAIL "$TMP/golden.txt" && { echo "golden parity FAILED — not flashing"; exit 1; }

echo "== firmware =="
ssh "$BUILD" "cd $RBUILD && . ~/hosttools/env.sh >/dev/null 2>&1; export PATH=\$HOME/miniconda3/bin:\$PATH; ./build.sh vendor/openvela/boards/contest2026_290_board/configs/nsh -j8 > ~/build_redeploy.log 2>&1; tail -1 ~/build_redeploy.log; ls -la nuttx/nuttx.bin"

exec "$KWS/deploy/flash_amp_relay.sh" "$BUILD" "$BOARD"
