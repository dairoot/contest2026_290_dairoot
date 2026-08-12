/****************************************************************************
 * board/contest_board/src/rk3576_mic.c
 *
 * AMP microphone demo: openvela owns a digital microphone on the 40-pin
 * header and streams the captured PCM to Linux over the rpmsg channel, where
 * it can be played back through the Linux-owned ES8388 codec/speaker.
 *
 *   microphone --PDM/I2S--> cpu3 (openvela) --rpmsg--> Linux /dev/ttyRPMSG0
 *
 * Two front ends are supported, picked at build time:
 *   CONFIG_RK3576_PDM  PDM1, for a PDM MEMS mic   (CLK GPIO4_A6/GPIO4_B0,
 *                                                  DATA GPIO4_B2)
 *   CONFIG_RK3576_SAI  SAI2, for an I2S MEMS mic  (SCLK/LRCK/SDI on
 *                                                  GPIO1_D1/D2/D3)
 * PDM wins if both are enabled.
 *
 * The same endpoint carries a tiny text protocol so the host can drive it:
 *
 *   echo MIC_START > /dev/ttyRPMSG0    start streaming raw PCM
 *   echo MIC_STOP  > /dev/ttyRPMSG0    stop streaming
 *   echo MIC_INFO  > /dev/ttyRPMSG0    reply with rate/format/health
 *   echo MIC_CH0   > /dev/ttyRPMSG0    take the other channel of the data
 *   echo MIC_CH1   > /dev/ttyRPMSG0      line (PDM only, see MIC_INFO peaks)
 *   anything else                      echoed back (link check)
 *
 * Recording from Linux is then just:
 *   stty -F /dev/ttyRPMSG0 raw -echo
 *   echo MIC_START > /dev/ttyRPMSG0; timeout 5 cat /dev/ttyRPMSG0 > mic.raw
 *   aplay -f S16_LE -r <hz> -c 1 mic.raw
 *
 * SPDX-License-Identifier: Apache-2.0
 ****************************************************************************/

/****************************************************************************
 * Included Files
 ****************************************************************************/

#include <nuttx/config.h>

#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include <nuttx/kthread.h>
#include <nuttx/rpmsg/rpmsg.h>

#ifdef CONFIG_RK3576_PDM
#  include "rk3576_pdm.h"
#else
#  include "rk3576_sai.h"
#endif

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

/* Front-end shim.  Both drivers expose the same shape, so the threads below
 * do not care which microphone is wired up.
 */

#ifdef CONFIG_RK3576_PDM
#  define mic_hw_init()      rk3576_pdm_capture_init()
#  define mic_hw_start()     rk3576_pdm_start()
#  define mic_hw_stop()      rk3576_pdm_stop()
#  define mic_hw_read(b, n)  rk3576_pdm_read(b, n)
#  define mic_hw_overruns()  rk3576_pdm_overruns()
#  define MIC_SAMPLE_BITS    RK3576_PDM_SAMPLE_BITS
#  define MIC_CHANNELS       RK3576_PDM_CHANNELS
#  define MIC_FRONTEND       "pdm"
#else
#  define mic_hw_init()      rk3576_sai_capture_init()
#  define mic_hw_start()     rk3576_sai_start()
#  define mic_hw_stop()      rk3576_sai_stop()
#  define mic_hw_read(b, n)  rk3576_sai_read(b, n)
#  define mic_hw_overruns()  rk3576_sai_overruns()
#  define MIC_SAMPLE_BITS    RK3576_SAI_SAMPLE_BITS
#  define MIC_CHANNELS       RK3576_SAI_CHANNELS
#  define MIC_FRONTEND       "i2s"
#endif

/* An rpmsg buffer is 512 bytes on this link (fixed by the Rockchip master),
 * so keep each PCM chunk comfortably below the usable payload.
 */

#define MIC_CHUNK_SAMPLES   224         /* 448 bytes of 16-bit PCM */

/* The capture FIFO holds only a couple of milliseconds, far less than a
 * single rpmsg_sendto() can take.  So the capture thread does nothing but
 * drain the FIFO into this ring, and a second thread ships it out; without
 * that split the FIFO overruns on every send.  1/2 second of slack.
 */

#define MIC_RING_SAMPLES    8192

/****************************************************************************
 * Private Data
 ****************************************************************************/

static struct rpmsg_endpoint g_mic_ept;
static bool                  g_mic_bound;
static volatile bool         g_mic_want;      /* host asked us to stream */
static volatile bool         g_mic_running;   /* hardware is actually on */
static volatile uint32_t     g_mic_peer;
static int                   g_mic_rate;

static int16_t               g_mic_ring[MIC_RING_SAMPLES];
static volatile uint32_t     g_mic_head;   /* written by the capture thread */
static volatile uint32_t     g_mic_tail;   /* written by the sender thread  */
static volatile uint32_t     g_mic_dropped;

/****************************************************************************
 * Private Functions
 ****************************************************************************/

/****************************************************************************
 * Name: rk3576_mic_ept_cb
 *
 * Description:
 *   Host -> slave commands.  Anything unrecognised is echoed back, which
 *   keeps the original link-check demo working.
 *
 *   Starting and stopping the hardware is left to the capture thread: the
 *   PDM start sequence has to wait out the microphone's wake-up and the
 *   decimation filter's settling time, which has no business happening in an
 *   rpmsg callback.
 *
 ****************************************************************************/

static int rk3576_mic_ept_cb(FAR struct rpmsg_endpoint *ept, FAR void *data,
                             size_t len, uint32_t src, FAR void *priv)
{
  FAR const char *cmd = data;
  char info[128];

  g_mic_peer = src;

  if (len >= 9 && strncmp(cmd, "MIC_START", 9) == 0)
    {
      g_mic_want = true;
    }
  else if (len >= 8 && strncmp(cmd, "MIC_STOP", 8) == 0)
    {
      g_mic_want = false;
    }
  else if (len >= 7 && strncmp(cmd, "MIC_CH", 6) == 0 &&
           (cmd[6] == '0' || cmd[6] == '1'))
    {
#ifdef CONFIG_RK3576_PDM
      rk3576_pdm_set_channel(cmd[6] - '0');
#endif
    }
  else if (len >= 8 && strncmp(cmd, "MIC_INFO", 8) == 0)
    {
      int n;

#ifdef CONFIG_RK3576_PDM
      uint16_t p0;
      uint16_t p1;

      rk3576_pdm_peaks(&p0, &p1);
      n = snprintf(info, sizeof(info),
                   "MIC src=%s rate=%d bits=%d ch=%d ovr=%lu drop=%lu "
                   "peak0=%u peak1=%u fifomax=%lu\n",
                   MIC_FRONTEND, g_mic_rate, MIC_SAMPLE_BITS, MIC_CHANNELS,
                   (unsigned long)mic_hw_overruns(),
                   (unsigned long)g_mic_dropped,
                   (unsigned)p0, (unsigned)p1,
                   (unsigned long)rk3576_pdm_maxfifo());
#else
      n = snprintf(info, sizeof(info),
                   "MIC src=%s rate=%d bits=%d ch=%d ovr=%lu drop=%lu\n",
                   MIC_FRONTEND, g_mic_rate, MIC_SAMPLE_BITS, MIC_CHANNELS,
                   (unsigned long)mic_hw_overruns(),
                   (unsigned long)g_mic_dropped);
#endif
      rpmsg_sendto(ept, info, n, src);
    }
  else
    {
      /* Plain echo, as before */

      rpmsg_sendto(ept, data, (int)len, src);
    }

  return 0;
}

/****************************************************************************
 * Name: rk3576_mic_capture_thread
 *
 * Description:
 *   Own the capture hardware: apply start/stop requests and drain the RX
 *   FIFO into the ring.  Neither front end has a usable FIFO-level
 *   interrupt, so this polls; that is affordable because this core runs
 *   nothing else, and it keeps the audio path free of the PL330 DMACs
 *   (which all belong to Linux).
 *
 ****************************************************************************/

static int rk3576_mic_capture_thread(int argc, FAR char *argv[])
{
  int16_t burst[64];

  for (; ; )
    {
      size_t got;
      size_t i;

      if (g_mic_want != g_mic_running)
        {
          if (g_mic_want)
            {
              g_mic_head = g_mic_tail = 0;
              mic_hw_start();
            }
          else
            {
              mic_hw_stop();
            }

          g_mic_running = g_mic_want;
        }

      if (!g_mic_running)
        {
          usleep(20 * 1000);
          continue;
        }

      got = mic_hw_read(burst, sizeof(burst) / sizeof(burst[0]));
      if (got == 0)
        {
          /* Nothing yet; come back well before the FIFO could fill up. */

          usleep(500);
          continue;
        }

      for (i = 0; i < got; i++)
        {
          uint32_t next = (g_mic_head + 1) % MIC_RING_SAMPLES;

          if (next == g_mic_tail)
            {
              g_mic_dropped++;    /* host is not draining fast enough */
              break;
            }

          g_mic_ring[g_mic_head] = burst[i];
          g_mic_head = next;
        }
    }

  return 0;
}

static int rk3576_mic_send_thread(int argc, FAR char *argv[])
{
  static int16_t chunk[MIC_CHUNK_SAMPLES];

  for (; ; )
    {
      size_t n = 0;

      if (!g_mic_running || !g_mic_bound)
        {
          usleep(20 * 1000);
          continue;
        }

      while (n < MIC_CHUNK_SAMPLES && g_mic_tail != g_mic_head)
        {
          chunk[n++] = g_mic_ring[g_mic_tail];
          g_mic_tail = (g_mic_tail + 1) % MIC_RING_SAMPLES;
        }

      if (n == 0)
        {
          usleep(2 * 1000);
          continue;
        }

      rpmsg_sendto(&g_mic_ept, chunk, n * sizeof(int16_t), g_mic_peer);
    }

  return 0;
}

/****************************************************************************
 * Name: rk3576_mic_device_created / destroy
 ****************************************************************************/

static void rk3576_mic_device_created(FAR struct rpmsg_device *rdev,
                                      FAR void *priv)
{
  if (strcmp("linux", rpmsg_get_cpuname(rdev)) == 0)
    {
      rpmsg_create_ept(&g_mic_ept, rdev, "rpmsg-tty",
                       RPMSG_ADDR_ANY, RPMSG_ADDR_ANY,
                       rk3576_mic_ept_cb, NULL);
      g_mic_bound = true;
    }
}

static void rk3576_mic_device_destroy(FAR struct rpmsg_device *rdev,
                                      FAR void *priv)
{
  if (strcmp("linux", rpmsg_get_cpuname(rdev)) == 0)
    {
      g_mic_bound = false;
      g_mic_want  = false;
      rpmsg_destroy_ept(&g_mic_ept);
    }
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/****************************************************************************
 * Name: rk3576_mic_init
 *
 * Description:
 *   Bring up the capture front end and register the rpmsg endpoint that
 *   carries the PCM to Linux.  Replaces rk3576_rpmsg_echo_init() when a
 *   microphone front end is configured (the echo behaviour is preserved).
 *
 ****************************************************************************/

int rk3576_mic_init(void)
{
  int ret;

  /* A failure here must not cost us the rpmsg link: register the endpoint
   * anyway so the host still gets its echo channel and can read the reason
   * out of MIC_INFO (rate=0 means the front end did not come up).
   */

  ret = mic_hw_init();
  if (ret > 0)
    {
      g_mic_rate = ret;

      /* Capture must outrank the sender: the FIFO has no slack. */

      kthread_create("amp_mic_cap", 200, 2048,
                     rk3576_mic_capture_thread, NULL);
      kthread_create("amp_mic_tx", 120, 2048,
                     rk3576_mic_send_thread, NULL);
    }

  return rpmsg_register_callback(NULL,
                                 rk3576_mic_device_created,
                                 rk3576_mic_device_destroy,
                                 NULL, NULL);
}
