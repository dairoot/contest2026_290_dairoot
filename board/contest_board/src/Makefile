############################################################################
# board/contest_board/src/Makefile
#
# SPDX-License-Identifier: Apache-2.0
############################################################################

include $(TOPDIR)/Make.defs

CSRCS = board_boot.c board_appinit.c

ifeq ($(CONFIG_RPTUN),y)
ifneq ($(CONFIG_RK3576_PDM)$(CONFIG_RK3576_SAI),)
CSRCS += rk3576_mic.c
else
CSRCS += rk3576_rpmsg_echo.c
endif
endif

ifeq ($(CONFIG_RK3576_KWS),y)
CSRCS += kws_frontend.c kws_nn.c kws_engine.c rk3576_kws.c

# The DS-CNN needs NEON-vectorized float loops: at the config's default
# -Os the inference takes ~200 ms on a 408 MHz-parked A53 and the engine
# can not keep up with its 80 ms cadence.  A trailing -O3 overrides the
# earlier -Os (last -O wins) for this library only; -ffast-math lets GCC
# vectorize the accumulation loops (parity vs Python stays ~1e-4, well
# inside the golden-test tolerance).
CFLAGS += -O3 -ffast-math
endif

ifeq ($(CONFIG_RK3576_AMP_HEARTBEAT),y)
CSRCS += rk3576_heartbeat.c
endif

include $(TOPDIR)/boards/Board.mk
