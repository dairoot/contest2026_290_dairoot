/****************************************************************************
 * board/contest_board/include/rk3576_mic_proto.h
 *
 * Wire protocol for the "rpmsg-mic" endpoint: openvela (cpu3) captures PCM
 * and ships it to Linux, where snd-rpmsg-mic turns it into a real ALSA
 * capture card.
 *
 * This header is compiled by BOTH sides — NuttX and the Linux kernel module.
 * Keep the copy in linux-side/snd-rpmsg-mic/ byte-identical.
 *
 * Everything is little-endian; both cores are AArch64 LE.  All structures
 * are packed so the layout does not depend on either compiler's padding.
 *
 * Flow:
 *
 *   Linux                                   openvela
 *     |-- CMD_CAPS ------------------------>|
 *     |<------------------------- RSP_CAPS -|  rate/bits/channels/chunk
 *     |-- CMD_CHAN (arg = 0|1) ------------>|  PDM only, picks the data slot
 *     |-- CMD_START ----------------------->|  hardware starts
 *     |<------------------------- RSP_DATA -|  seq 0, PCM payload
 *     |<------------------------- RSP_DATA -|  seq 1, ...
 *     |-- CMD_STOP ------------------------>|
 *
 * A gap in RSP_DATA.seq means rpmsg dropped a packet; a jump in
 * RSP_DATA.dropped means the slave's ring overflowed because Linux was not
 * draining fast enough.  Both are reported to ALSA as an overrun.
 *
 * SPDX-License-Identifier: Apache-2.0
 ****************************************************************************/

#ifndef __BOARD_CONTEST_BOARD_INCLUDE_RK3576_MIC_PROTO_H
#define __BOARD_CONTEST_BOARD_INCLUDE_RK3576_MIC_PROTO_H

/****************************************************************************
 * Included Files
 ****************************************************************************/

#ifdef __NuttX__
#  include <stdint.h>
#else
#  include <linux/types.h>   /* the Linux kernel module builds this too */
#endif

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

/* Endpoint name announced to Linux.  The legacy text/echo endpoint keeps
 * its own name ("rpmsg-tty", handled by the kernel's rpmsg_tty driver), so
 * both interfaces can be used at the same time.
 */

#define RPMSG_MIC_EPT_NAME      "rpmsg-mic"

/* "MIC1" as a little-endian u32 */

#define RPMSG_MIC_MAGIC         0x3143494du

/* Message types.  Commands come from Linux, responses from openvela. */

#define RPMSG_MIC_CMD_CAPS      0x01
#define RPMSG_MIC_CMD_START     0x02
#define RPMSG_MIC_CMD_STOP      0x03
#define RPMSG_MIC_CMD_CHAN      0x04    /* hdr.arg = 0 or 1 */
#define RPMSG_MIC_RSP_CAPS      0x81
#define RPMSG_MIC_RSP_DATA      0x82

/* caps.flags */

#define RPMSG_MIC_FLAG_PDM      (1u << 0) /* CMD_CHAN is meaningful */

/* One rpmsg buffer is 512 bytes, 16 of which are the rpmsg header, so 496
 * are usable.  24 (our header) + 448 (PCM) = 472 leaves margin.
 */

#define RPMSG_MIC_CHUNK_SAMPLES 224
#define RPMSG_MIC_CHUNK_BYTES   (RPMSG_MIC_CHUNK_SAMPLES * 2)

/****************************************************************************
 * Public Types
 ****************************************************************************/

/* Every message on the endpoint starts with this. */

struct rpmsg_mic_hdr
{
  uint32_t magic;       /* RPMSG_MIC_MAGIC */
  uint16_t type;        /* RPMSG_MIC_CMD_* / RPMSG_MIC_RSP_* */
  uint16_t arg;         /* CMD_CHAN: slot; otherwise 0 */
  uint32_t seq;         /* RSP_DATA: chunk counter, resets on START */
  uint32_t dropped;     /* RSP_DATA: cumulative ring drops on the slave */
  uint64_t ts_us;       /* RSP_DATA: slave monotonic clock, microseconds */
} __attribute__((packed));

/* Payload of RSP_CAPS. */

struct rpmsg_mic_caps
{
  uint32_t rate;            /* Hz; 0 means the front end failed to init */
  uint16_t bits;            /* 16 */
  uint16_t channels;        /* 1 */
  uint32_t chunk_samples;   /* samples per RSP_DATA */
  uint32_t overruns;        /* cumulative hardware FIFO overruns */
  uint32_t flags;           /* RPMSG_MIC_FLAG_* */
} __attribute__((packed));

#define RPMSG_MIC_MAX_MSG   (sizeof(struct rpmsg_mic_hdr) + \
                             RPMSG_MIC_CHUNK_BYTES)

#endif /* __BOARD_CONTEST_BOARD_INCLUDE_RK3576_MIC_PROTO_H */
