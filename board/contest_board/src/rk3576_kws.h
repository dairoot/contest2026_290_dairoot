/****************************************************************************
 * board/contest_board/src/rk3576_kws.h
 *
 * Wiring between the microphone capture path (rk3576_mic.c) and the
 * offline wake-word engine (kws_engine.c + rk3576_kws.c).
 *
 * SPDX-License-Identifier: Apache-2.0
 ****************************************************************************/

#ifndef __BOARD_CONTEST_BOARD_SRC_RK3576_KWS_H
#define __BOARD_CONTEST_BOARD_SRC_RK3576_KWS_H

#include <nuttx/config.h>

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Implemented by rk3576_kws.c */

int  rk3576_kws_init(void);

/* True while the wake-word engine wants the microphone running (always-on
 * listening).  ORed into mic_want().  Gated on the rpmsg link so the PDM
 * front end never starts before Linux's clock tree is up.
 */

bool rk3576_kws_active(void);

/* Called from the rpmsg device created/destroyed callbacks. */

void rk3576_kws_link(bool up);

/* Called after a capture re-init so the restart transient cannot fire. */

void rk3576_kws_mute_restart(void);

/* Called from the capture thread with every burst of fresh samples. */

void rk3576_kws_feed(FAR const int16_t *pcm, size_t n);

/* Text control protocol, delegated from the "rpmsg-tty" endpoint:
 * KWS_INFO / KWS_ON / KWS_OFF / KWS_THR <500-999> / KWS_SCORE / KWS_RESET.
 * Formats a reply into rsp and returns its length (0 = no reply).
 */

int  rk3576_kws_command(FAR const char *cmd, size_t len,
                        FAR char *rsp, size_t rsplen);

/* Implemented by rk3576_mic.c (transport back to Linux) */

void rk3576_mic_wake_event(float prob, uint32_t count);
void rk3576_mic_kws_line(FAR const char *line);

#endif /* __BOARD_CONTEST_BOARD_SRC_RK3576_KWS_H */
