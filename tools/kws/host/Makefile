# Host build of the firmware KWS sources, for parity tests and batch
# evaluation.  Requires the generated headers (run export_c.py first).
#
# No native gcc on the dev box: default to the RK3576 Linux SDK cross
# compiler and run the (static) binary on the board over ssh.  Override CC
# for any other toolchain, e.g. CC=gcc STATIC= for a real host build.

BOARD := ../../../board/contest_board/src
SDKGCC := $(HOME)/rk3576-sdk/rk3576-linux/prebuilts/gcc/linux-x86/aarch64/gcc-arm-10.3-2021.07-x86_64-aarch64-none-linux-gnu/bin/aarch64-none-linux-gnu-gcc
CC ?= $(SDKGCC)
STATIC ?= -static
# Warmup suppresses the first 10 s after init to mask the Linux boot-time
# speaker pops (see kws.h).  The eval clips are shorter than that, so with
# the warmup left in, wav/batch modes can never fire — disable it here.
CFLAGS := -O2 -Wall -Wextra $(STATIC) -DKWS_WARMUP_FRAMES=0 -I$(BOARD) -I.

SRCS := host_main.c \
        $(BOARD)/kws_frontend.c \
        $(BOARD)/kws_nn.c \
        $(BOARD)/kws_engine.c

kws_host: $(SRCS) $(BOARD)/kws.h $(BOARD)/kws_tables.h \
          $(BOARD)/kws_model_data.h kws_golden.h
	$(CC) $(CFLAGS) -o $@ $(SRCS) -lm

clean:
	rm -f kws_host

.PHONY: clean
