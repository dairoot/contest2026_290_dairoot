/****************************************************************************
 * board/contest_board/src/rk3576_kws.c
 *
 * Always-on offline wake word ("你好，openvela") on the AMP core.
 *
 * The capture thread copies every microphone burst into a private ring;
 * this file's worker thread drains it into the streaming KWS engine.  On
 * detection the engine's callback ships a wake event to Linux over both
 * rpmsg endpoints (binary RPMSG_MIC_EVT_WAKE + a human-readable line).
 *
 * The worker runs below the capture (200) and sender (120) threads: one
 * DS-CNN inference takes a few milliseconds, and the 0.5 s ring absorbs it.
 *
 * SPDX-License-Identifier: Apache-2.0
 ****************************************************************************/

#include <nuttx/config.h>

#include <stdio.h>
#include <string.h>
#include <syslog.h>
#include <unistd.h>

#include <nuttx/kthread.h>

#include "kws.h"
#include "rk3576_kws.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define KWS_RING_SAMPLES  8192          /* 0.5 s at 16 kHz */
#define KWS_CHUNK         256
#define KWS_THREAD_PRIO   90
#define KWS_THREAD_STACK  8192

/****************************************************************************
 * Private Data
 ****************************************************************************/

static int16_t           g_ring[KWS_RING_SAMPLES];
static volatile uint32_t g_head;        /* written by capture thread */
static volatile uint32_t g_tail;        /* written by the KWS thread */
static volatile uint32_t g_ring_drop;

static volatile bool     g_enabled = true;
static volatile bool     g_link_up;
static volatile bool     g_score_stream;

/****************************************************************************
 * Private Functions
 ****************************************************************************/

/* Milli-units helper so no float printf is needed anywhere */

static unsigned milli(float v)
{
  int m = (int)(v * 1000.0f + 0.5f);

  if (m < 0)
    {
      m = 0;
    }
  else if (m > 999)
    {
      m = 999;
    }

  return (unsigned)m;
}

static void kws_on_detect(float prob, FAR void *arg)
{
  kws_stats_t st;

  (void)arg;
  kws_engine_get_stats(&st);
  syslog(LOG_INFO, "kws: wake word detected p=0.%03u (#%lu)\n",
         milli(prob), (unsigned long)st.detections);
  rk3576_mic_wake_event(prob, st.detections);
}

static int kws_thread(int argc, FAR char *argv[])
{
  int16_t chunk[KWS_CHUNK];
  uint64_t last_score = 0;

  for (; ; )
    {
      size_t n = 0;

      if (!g_enabled)
        {
          usleep(100 * 1000);
          continue;
        }

      while (n < KWS_CHUNK && g_tail != g_head)
        {
          chunk[n++] = g_ring[g_tail];
          g_tail = (g_tail + 1) % KWS_RING_SAMPLES;
        }

      if (n == 0)
        {
          usleep(4 * 1000);
          continue;
        }

      kws_engine_push(chunk, n);

      if (g_score_stream)
        {
          kws_stats_t st;

          kws_engine_get_stats(&st);
          if (st.frames - last_score >= 100)     /* every second */
            {
              char line[96];

              last_score = st.frames;
              snprintf(line, sizeof(line),
                       "KWS p=0.%03u peak=0.%03u inf=%lu us=%lu\n",
                       milli(st.smoothed), milli(st.peak),
                       (unsigned long)st.inferences,
                       (unsigned long)st.infer_us);
              rk3576_mic_kws_line(line);
            }
        }
    }

  return 0;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

bool rk3576_kws_active(void)
{
  /* Only listen once the rpmsg link is up, i.e. Linux has finished
   * booting.  This core boots long before Linux; starting the PDM
   * front end that early proved fatal — the Linux boot re-parents the
   * shared CRU clock tree and silently kills an already-running PDM
   * interface, and with the want-flag already true there is no edge
   * left to restart it.  Gating on the link both avoids that and stops
   * the engine chewing on a dead microphone.
   */

  return g_enabled && g_link_up;
}

void rk3576_kws_link(bool up)
{
  if (up && !g_link_up)
    {
      float th = kws_engine_threshold();

      g_tail = g_head;                  /* drop pre-link stale audio */

      /* Fresh engine state so the warm-up mute covers the Linux boot's
       * deterministic speaker pops from the ES8388 init.
       */

      kws_engine_init(kws_on_detect, NULL);
      kws_engine_set_threshold(th);
    }

  g_link_up = up;
}

void rk3576_kws_feed(FAR const int16_t *pcm, size_t n)
{
  size_t i;

  if (!g_enabled)
    {
      return;
    }

  for (i = 0; i < n; i++)
    {
      uint32_t next = (g_head + 1) % KWS_RING_SAMPLES;

      if (next == g_tail)
        {
          g_ring_drop++;
          return;
        }

      g_ring[g_head] = pcm[i];
      g_head = next;
    }
}

int rk3576_kws_command(FAR const char *cmd, size_t len,
                       FAR char *rsp, size_t rsplen)
{
  kws_stats_t st;

  if (len >= 8 && strncmp(cmd, "KWS_INFO", 8) == 0)
    {
      kws_engine_get_stats(&st);
      return snprintf(rsp, rsplen,
                      "KWS on=%d thr=0.%03u frames=%lu inf=%lu det=%lu "
                      "last=0.%03u smooth=0.%03u peak=0.%03u us=%lu "
                      "drop=%lu\n",
                      g_enabled ? 1 : 0, milli(kws_engine_threshold()),
                      (unsigned long)st.frames,
                      (unsigned long)st.inferences,
                      (unsigned long)st.detections,
                      milli(st.last_prob), milli(st.smoothed),
                      milli(st.peak), (unsigned long)st.infer_us,
                      (unsigned long)g_ring_drop);
    }

  if (len >= 7 && strncmp(cmd, "KWS_OFF", 7) == 0)
    {
      g_enabled = false;
      return snprintf(rsp, rsplen, "KWS off\n");
    }

  if (len >= 6 && strncmp(cmd, "KWS_ON", 6) == 0)
    {
      float th = kws_engine_threshold();

      g_tail = g_head;                  /* flush stale audio */
      kws_engine_init(kws_on_detect, NULL);
      kws_engine_set_threshold(th);
      g_enabled = true;
      return snprintf(rsp, rsplen, "KWS on\n");
    }

  if (len >= 8 && strncmp(cmd, "KWS_THR ", 8) == 0)
    {
      int v = 0;
      size_t i;

      for (i = 8; i < len && cmd[i] >= '0' && cmd[i] <= '9'; i++)
        {
          v = v * 10 + (cmd[i] - '0');
        }

      if (v >= 500 && v <= 999)
        {
          kws_engine_set_threshold((float)v / 1000.0f);
          return snprintf(rsp, rsplen, "KWS thr=0.%03d\n", v);
        }

      return snprintf(rsp, rsplen, "KWS thr must be 500..999\n");
    }

  if (len >= 9 && strncmp(cmd, "KWS_SCORE", 9) == 0)
    {
      g_score_stream = !g_score_stream;
      return snprintf(rsp, rsplen, "KWS score stream %s\n",
                      g_score_stream ? "on" : "off");
    }

  if (len >= 9 && strncmp(cmd, "KWS_RESET", 9) == 0)
    {
      kws_engine_reset_peak();
      return snprintf(rsp, rsplen, "KWS peak reset\n");
    }

  if (len >= 8 && strncmp(cmd, "KWS_TEST", 8) == 0)
    {
      float pz;
      float po;

      kws_engine_selftest(&pz, &po);
      return snprintf(rsp, rsplen, "KWS test z=%lu o=%lu (micro)\n",
                      (unsigned long)(pz * 1e6f + 0.5f),
                      (unsigned long)(po * 1e6f + 0.5f));
    }

  if (len >= 8 && strncmp(cmd, "KWS_PEEK", 8) == 0)
    {
      int16_t pk;
      float f[3];

      kws_engine_peek(&pk, f);
      return snprintf(rsp, rsplen,
                      "KWS peek pcm=%d f0=%d f10=%d f39=%d (milli)\n",
                      (int)pk, (int)(f[0] * 1000.0f),
                      (int)(f[1] * 1000.0f), (int)(f[2] * 1000.0f));
    }

  return snprintf(rsp, rsplen,
                  "KWS commands: INFO ON OFF THR<500-999> SCORE RESET\n");
}

void rk3576_kws_mute_restart(void)
{
  kws_engine_mute(300);                 /* 3 s of audio */
}

int rk3576_kws_init(void)
{
  int ret;

  kws_engine_init(kws_on_detect, NULL);

  ret = kthread_create("amp_kws", KWS_THREAD_PRIO, KWS_THREAD_STACK,
                       kws_thread, NULL);
  if (ret < 0)
    {
      syslog(LOG_ERR, "kws: thread create failed %d\n", ret);
      return ret;
    }

  syslog(LOG_INFO, "kws: engine up, thr=0.%03u, listening\n",
         milli(kws_engine_threshold()));
  return OK;
}
