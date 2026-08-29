#!/usr/bin/env bash
# flash_amp.sh, split for the case where the build host cannot reach the
# board (board on the home LAN, build host in the cloud): pack amp.img on
# the build host, relay it through this machine, dd it on the board.
#
# Usage: ./flash_amp_relay.sh [build-host] [board]
#        defaults: ubt  kickpi@192.168.3.21
# Prereq: nuttx.bin already built on the build host (see README §7).

set -euo pipefail

BUILD=${1:-ubt}
BOARD=${2:-kickpi@192.168.3.21}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

ssh "$BUILD" 'bash -s' <<'EOF'
set -euo pipefail
WS=$HOME/openvela-ws
REPO=$WS/contest2026_290_dairoot
SDK=$HOME/rk3576-sdk/rk3576-linux
NUTTX_BIN=$WS/nuttx/nuttx.bin
ITS=$REPO/board/contest_board/linux-side/configs/amp-k7.its
[ -f "$NUTTX_BIN" ] || { echo "no $NUTTX_BIN — build first"; exit 1; }
OUT=$(mktemp -d)
cp "$NUTTX_BIN" "$OUT/rtt3.bin"
sed '/share {/,/}/d;/compile {/,/}/d' "$ITS" > "$OUT/amp.its"
cd "$OUT"
export PATH=$SDK/kernel/scripts/dtc:$PATH
"$SDK/rkbin/tools/mkimage" -f amp.its -E -p 0xe00 amp.img >/dev/null
cp amp.img "$HOME/amp.img"
ls -la "$HOME/amp.img"
rm -rf "$OUT"
EOF

scp -q "$BUILD":amp.img "$TMP/amp.img"
scp -q "$TMP/amp.img" "$BOARD":/tmp/amp.img
ssh -t "$BOARD" '
  set -e
  STAMP=$(date +%Y%m%d_%H%M)
  sudo dd if=/dev/disk/by-partlabel/amp of="$HOME/amp_backup_$STAMP.img" \
      bs=1M status=none && echo "backup: ~/amp_backup_$STAMP.img"
  sudo dd if=/tmp/amp.img of=/dev/disk/by-partlabel/amp conv=fsync \
      status=none
  echo "amp partition flashed — rebooting"
  sudo reboot
'
