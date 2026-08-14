#!/usr/bin/env bash
# Pack the freshly built openvela nuttx.bin into amp.img and flash the
# board's amp partition over ssh (the ~10 s iteration path from the board
# README §3).  Keeps a one-shot backup of the current partition content.
#
# Usage: ./flash_amp.sh [board-user@board-ip]     (default kickpi@172.19.5.62)

set -euo pipefail

BOARD=${1:-kickpi@172.19.5.62}
REPO=$(cd "$(dirname "$0")/../../.." && pwd)           # contest repo
WS=$(cd "$REPO/.." && pwd)                             # openvela workspace
SDK=${SDK:-$HOME/rk3576-sdk/rk3576-linux}
MKIMAGE=$SDK/rkbin/tools/mkimage
OUT=$(mktemp -d)

NUTTX_BIN=$WS/nuttx/nuttx.bin
ITS=$REPO/board/contest_board/linux-side/configs/amp-k7.its

[ -f "$NUTTX_BIN" ] || { echo "no $NUTTX_BIN — build first"; exit 1; }
[ -f "$ITS" ] || { echo "no $ITS (amp FIT description)"; exit 1; }
[ -x "$MKIMAGE" ] || { echo "no mkimage at $MKIMAGE"; exit 1; }

cp "$NUTTX_BIN" "$OUT/rtt3.bin"
# the rockchip FIT source carries share{}/compile{} blocks its mk-amp.sh
# normally strips; do the same before handing it to bare mkimage
sed '/share {/,/}/d;/compile {/,/}/d' "$ITS" > "$OUT/amp.its"
cd "$OUT"
# mkimage shells out to dtc; the SDK kernel tree carries a built one
export PATH=$SDK/kernel/scripts/dtc:$PATH
"$MKIMAGE" -f amp.its -E -p 0xe00 amp.img >/dev/null
ls -la amp.img

scp -q amp.img "$BOARD":/tmp/amp.img
ssh -t "$BOARD" '
  set -e
  if [ ! -e /tmp/amp_backup.img ]; then
    sudo dd if=/dev/disk/by-partlabel/amp of=/tmp/amp_backup.img bs=1M \
        count=8 status=none && echo "backup: /tmp/amp_backup.img"
  fi
  sudo dd if=/tmp/amp.img of=/dev/disk/by-partlabel/amp conv=fsync \
      status=none
  echo "amp partition flashed — rebooting"
  sudo reboot
'
rm -rf "$OUT"
