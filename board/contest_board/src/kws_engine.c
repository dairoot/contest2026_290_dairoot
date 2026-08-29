/****************************************************************************
 * board/contest_board/src/kws_engine.c
 *
 * Streaming glue around the front end and the network:
 *
 *   push(pcm) -> 400-sample sliding frames every 160 samples
 *             -> feature ring (KWS_RING frames of 40)
 *             -> every KWS_INFER_EVERY frames, run the DS-CNN on the last
 *                KWS_T frames, average the last KWS_SMOOTH outputs, fire
 *                the callback when the average crosses the threshold,
 *                then hold off for KWS_REFRACT_FRAMES.
 *
 * Single consumer thread; the threshold may be poked from another thread
 * (word-sized float store, worst case one late decision).
 *
 * SPDX-License-Identifier: Apache-2.0
 ****************************************************************************/

#include <stdbool.h>
#include <string.h>
#include <time.h>

#include "kws.h"
#include "kws_tables.h"

#define KWS_RING (KWS_T + 2 * KWS_INFER_EVERY)

/****************************************************************************
 * Private Data
 ****************************************************************************/

static int16_t g_sbuf[KWS_WIN];
static int     g_sfill;

static float   g_fring[KWS_RING][KWS_NMEL];
static int     g_fw;                     /* next write slot */
static uint32_t g_frames;

static float   g_input[KWS_T * KWS_NMEL];
static float   g_smooth[KWS_SMOOTH];
static int     g_smooth_n;

static volatile float g_threshold = KWS_DEFAULT_THRESHOLD;
static int     g_holdoff;
static bool    g_armed;                  /* re-arms once score < REARM */

static kws_detect_cb_t g_cb;
static void           *g_cb_arg;

static kws_stats_t g_stats;

static int16_t g_pcm_peak;      /* max |sample| seen since last peek */

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static uint32_t now_us(void)
{
  struct timespec ts;

  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint32_t)(ts.tv_sec * 1000000ll + ts.tv_nsec / 1000);
}

static void run_inference(void)
{
  uint32_t t0 = now_us();
  int start = g_fw - KWS_T;
  float prob;
  float sum;
  int i;
  int n;

  if (start < 0)
    {
      start += KWS_RING;
    }

  for (i = 0; i < KWS_T; i++)
    {
      int idx = start + i;

      if (idx >= KWS_RING)
        {
          idx -= KWS_RING;
        }

      memcpy(&g_input[i * KWS_NMEL], g_fring[idx],
             KWS_NMEL * sizeof(float));
    }

  prob = kws_nn_infer(g_input);

  g_smooth[g_stats.inferences % KWS_SMOOTH] = prob;
  if (g_smooth_n < KWS_SMOOTH)
    {
      g_smooth_n++;
    }

  sum = 0.0f;
  n = g_smooth_n;
  for (i = 0; i < n; i++)
    {
      sum += g_smooth[i];
    }

  sum /= (float)n;

  g_stats.inferences++;
  g_stats.last_prob = prob;
  g_stats.smoothed = sum;
  g_stats.infer_us = now_us() - t0;
  if (sum > g_stats.peak)
    {
      g_stats.peak = sum;
    }

  if (!g_armed && sum < KWS_REARM_THRESHOLD)
    {
      g_armed = true;
    }

  if (g_armed && g_holdoff == 0 && sum >= g_threshold)
    {
      g_stats.detections++;
      g_holdoff = KWS_REFRACT_FRAMES;
      g_armed = false;
      if (g_cb != NULL)
        {
          g_cb(sum, g_cb_arg);
        }
    }
}

static void push_frame(void)
{
  kws_frontend_frame(g_sbuf, g_fring[g_fw]);
  g_fw = (g_fw + 1) % KWS_RING;
  g_frames++;
  g_stats.frames = g_frames;

  if (g_holdoff > 0)
    {
      g_holdoff--;
    }

  if (g_frames >= KWS_T && (g_frames % KWS_INFER_EVERY) == 0)
    {
      run_inference();
    }
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

void kws_engine_init(kws_detect_cb_t cb, void *arg)
{
  memset(&g_stats, 0, sizeof(g_stats));
  memset(g_smooth, 0, sizeof(g_smooth));
  g_sfill = 0;
  g_fw = 0;
  g_frames = 0;
  g_smooth_n = 0;
  g_holdoff = KWS_WARMUP_FRAMES;
  g_armed = true;
  g_cb = cb;
  g_cb_arg = arg;
}

void kws_engine_push(const int16_t *pcm, size_t n)
{
  size_t i;

  for (i = 0; i < n; i++)
    {
      int16_t v = pcm[i] < 0 ? -pcm[i] : pcm[i];

      if (v > g_pcm_peak)
        {
          g_pcm_peak = v;
        }
    }

  while (n > 0)
    {
      size_t take = (size_t)(KWS_WIN - g_sfill);

      if (take > n)
        {
          take = n;
        }

      memcpy(&g_sbuf[g_sfill], pcm, take * sizeof(int16_t));
      g_sfill += (int)take;
      pcm += take;
      n -= take;

      if (g_sfill == KWS_WIN)
        {
          push_frame();
          memmove(g_sbuf, &g_sbuf[KWS_HOP],
                  (KWS_WIN - KWS_HOP) * sizeof(int16_t));
          g_sfill = KWS_WIN - KWS_HOP;
        }
    }
}

void kws_engine_mute(uint32_t frames)
{
  if ((uint32_t)g_holdoff < frames)
    {
      g_holdoff = (int)frames;
    }
}

void kws_engine_set_threshold(float th)
{
  if (th < 0.50f)
    {
      th = 0.50f;
    }
  else if (th > 0.999f)
    {
      th = 0.999f;
    }

  g_threshold = th;
}

float kws_engine_threshold(void)
{
  return g_threshold;
}

void kws_engine_get_stats(kws_stats_t *out)
{
  *out = g_stats;
}

void kws_engine_reset_peak(void)
{
  g_stats.peak = 0.0f;
}

/****************************************************************************
 * Name: kws_engine_selftest / kws_engine_peek
 *
 * Description:
 *   Diagnostics.  selftest pushes all-zero and all-one feature windows
 *   straight through the network — the two probabilities are functions of
 *   the weights alone, so a mismatch against the host harness pinpoints
 *   broken numerics on the target (libm, fast-math, ...).  peek exposes
 *   what the engine actually receives: the raw-PCM peak since the last
 *   call and three coefficients of the newest feature frame.
 *
 ****************************************************************************/

void kws_engine_selftest(float *p_zero, float *p_one)
{
  int i;

  for (i = 0; i < KWS_T * KWS_NMEL; i++)
    {
      g_input[i] = 0.0f;
    }

  *p_zero = kws_nn_infer(g_input);

  for (i = 0; i < KWS_T * KWS_NMEL; i++)
    {
      g_input[i] = 1.0f;
    }

  *p_one = kws_nn_infer(g_input);
}

void kws_engine_peek(int16_t *pcm_peak, float feat[3])
{
  int last = g_fw - 1;

  if (last < 0)
    {
      last += KWS_RING;
    }

  *pcm_peak = g_pcm_peak;
  g_pcm_peak = 0;
  feat[0] = g_fring[last][0];
  feat[1] = g_fring[last][10];
  feat[2] = g_fring[last][39];
}
