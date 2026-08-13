# SPDX-License-Identifier: GPL-2.0
#
# Out-of-tree build of the AMP microphone ALSA driver.
#
#   make KDIR=<kernel build tree> CROSS_COMPILE=<prefix>-
#
# The defaults point at the KickPi RK3576 Linux SDK as it is laid out on the
# machine this was developed on; override them for any other tree.

obj-m += snd_rpmsg_mic.o

SDK   ?= $(HOME)/rk3576-sdk/rk3576-linux
KDIR  ?= $(SDK)/kernel
ARCH  ?= arm64
CROSS_COMPILE ?= $(SDK)/prebuilts/gcc/linux-x86/aarch64/gcc-arm-10.3-2021.07-x86_64-aarch64-none-linux-gnu/bin/aarch64-none-linux-gnu-

PWD := $(shell pwd)

all:
	$(MAKE) -C $(KDIR) M=$(PWD) ARCH=$(ARCH) CROSS_COMPILE=$(CROSS_COMPILE) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) ARCH=$(ARCH) CROSS_COMPILE=$(CROSS_COMPILE) clean

.PHONY: all clean
