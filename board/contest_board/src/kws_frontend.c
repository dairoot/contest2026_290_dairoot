/****************************************************************************
 * board/contest_board/src/kws_frontend.c
 *
 * One 25 ms frame of int16 PCM -> 40 normalized log-mel coefficients.
 * Mirrors tools/kws/kws_common.py:logmel() in float32:
 *
 *   x = pcm/32768; x -= mean(x); x *= hann(400); zero-pad to 512
 *   P = |rfft(x)|^2 ; mel = FB @ P ; out = (ln(mel+eps) - mean) * istd
 *
 * The FFT is a plain iterative radix-2 complex transform (the real-input
 * optimization is not worth the code on a dedicated A53).  All tables come
 * from the generated kws_tables.h.
 *
 * SPDX-License-Identifier: Apache-2.0
 ****************************************************************************/

#include <math.h>

#include "kws.h"
#include "kws_tables.h"

/****************************************************************************
 * Private Data
 ****************************************************************************/

static uint16_t g_bitrev[KWS_NFFT];
static int      g_fft_ready;

/* Scratch: single-threaded by design (only the KWS thread calls in) */

static float    g_re[KWS_NFFT];
static float    g_im[KWS_NFFT];

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static void fft_prepare(void)
{
  int i;

  for (i = 0; i < KWS_NFFT; i++)
    {
      unsigned v = i;
      unsigned r = 0;
      int b;

      for (b = 0; b < 9; b++)             /* 512 = 2^9 */
        {
          r = (r << 1) | (v & 1u);
          v >>= 1;
        }

      g_bitrev[i] = (uint16_t)r;
    }

  g_fft_ready = 1;
}

/* In-place DIT radix-2; twiddles applied as e^(-j*2*pi*k/N) */

static void fft512(float *re, float *im)
{
  int len;
  int i;

  for (i = 0; i < KWS_NFFT; i++)
    {
      int j = g_bitrev[i];

      if (j > i)
        {
          float t;

          t = re[i]; re[i] = re[j]; re[j] = t;
          t = im[i]; im[i] = im[j]; im[j] = t;
        }
    }

  for (len = 2; len <= KWS_NFFT; len <<= 1)
    {
      int half = len >> 1;
      int step = KWS_NFFT / len;

      for (i = 0; i < KWS_NFFT; i += len)
        {
          int k;

          for (k = 0; k < half; k++)
            {
              const float c = g_kws_tw_cos[k * step];
              const float s = g_kws_tw_sin[k * step];
              float xr = re[i + k + half];
              float xi = im[i + k + half];
              float tr = xr * c + xi * s;
              float ti = xi * c - xr * s;

              re[i + k + half] = re[i + k] - tr;
              im[i + k + half] = im[i + k] - ti;
              re[i + k] += tr;
              im[i + k] += ti;
            }
        }
    }
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

void kws_frontend_frame(const int16_t *pcm, float *out)
{
  float mean = 0.0f;
  int i;
  int m;

  if (!g_fft_ready)
    {
      fft_prepare();
    }

  for (i = 0; i < KWS_WIN; i++)
    {
      mean += (float)pcm[i];
    }

  mean *= (1.0f / 32768.0f) / (float)KWS_WIN;

  for (i = 0; i < KWS_WIN; i++)
    {
      g_re[i] = ((float)pcm[i] * (1.0f / 32768.0f) - mean) * g_kws_hann[i];
      g_im[i] = 0.0f;
    }

  for (; i < KWS_NFFT; i++)
    {
      g_re[i] = 0.0f;
      g_im[i] = 0.0f;
    }

  fft512(g_re, g_im);

  /* Power spectrum re-uses g_re[0..NBIN); bins above NFFT/2 are conjugate
   * copies we never read.
   */

  for (i = 0; i < KWS_NBIN; i++)
    {
      g_re[i] = g_re[i] * g_re[i] + g_im[i] * g_im[i];
    }

  for (m = 0; m < KWS_NMEL; m++)
    {
      const float *w = &g_kws_mel_w[g_kws_mel_off[m]];
      const float *p = &g_re[g_kws_mel_start[m]];
      float acc = 0.0f;
      int n = g_kws_mel_len[m];

      for (i = 0; i < n; i++)
        {
          acc += w[i] * p[i];
        }

      out[m] = (logf(acc + KWS_LOG_EPS) - g_kws_norm_mean[m]) *
               g_kws_norm_istd[m];
    }
}
