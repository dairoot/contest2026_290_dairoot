#!/usr/bin/env python3
"""Watch for wake-word events on the KickPi K7 Linux side.

The AMP slave's KWS engine reports detections as KEY_WAKEUP presses on the
"openvela-kws" input device (registered by snd_rpmsg_mic.ko).  This script
needs only the Python stdlib: it parses /proc/bus/input/devices to find the
event node and reads raw input_event structs from it.

Usage:  sudo python3 wake_watch.py [--exec CMD]

--exec CMD   run CMD (shell) on every wake, e.g. an aplay chime or the
             start of an ASR pipeline.
"""

import argparse
import struct
import subprocess
import sys
import time

EV_KEY = 0x01
KEY_WAKEUP = 143

# aarch64: struct input_event = struct timeval (16) + u16 type + u16 code
# + s32 value
EVFMT = "llHHi"
EVSIZE = struct.calcsize(EVFMT)


def find_event_node(name="openvela-kws"):
    dev = None
    with open("/proc/bus/input/devices") as f:
        block = []
        for line in f:
            if line.strip() == "":
                block = []
                continue
            block.append(line)
            if line.startswith("H:") and any(name in b for b in block):
                for tok in line.split():
                    if tok.startswith("event"):
                        return "/dev/input/" + tok
    return dev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exec", dest="cmd", default=None,
                    help="shell command to run on each wake")
    ap.add_argument("--device", default=None,
                    help="event node override, e.g. /dev/input/event3")
    args = ap.parse_args()

    node = args.device or find_event_node()
    if not node:
        print("openvela-kws input device not found — is snd_rpmsg_mic.ko "
              "loaded?", file=sys.stderr)
        return 1

    print(f"listening on {node} (KEY_WAKEUP from the openvela KWS engine)",
          flush=True)
    n = 0
    with open(node, "rb", buffering=0) as f:
        while True:
            data = f.read(EVSIZE)
            if len(data) != EVSIZE:
                break
            _sec, _usec, etype, code, value = struct.unpack(EVFMT, data)
            if etype == EV_KEY and code == KEY_WAKEUP and value == 1:
                n += 1
                stamp = time.strftime("%H:%M:%S")
                print(f"[{stamp}] 你好，openvela!  wake #{n}", flush=True)
                if args.cmd:
                    subprocess.Popen(args.cmd, shell=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
