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

ifeq ($(CONFIG_RK3576_AMP_HEARTBEAT),y)
CSRCS += rk3576_heartbeat.c
endif

include $(TOPDIR)/boards/Board.mk
