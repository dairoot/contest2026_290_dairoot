/****************************************************************************
 * board/contest_board/src/kws_nn.c
 *
 * DS-CNN inference, float32, weights from the generated kws_model_data.h
 * (BatchNorm already folded into the conv weights by export_c.py).
 *
 *   input   1 x KWS_IN_T x KWS_IN_M   normalized log-mel
 *   conv1   KWS_CH ch, KWS_C1_KT x KWS_C1_KM, stride 2x2  + ReLU
 *   3 x ( depthwise 3x3 + ReLU ; pointwise 1x1 + ReLU )
 *   global average pool ; fc -> 2 logits ; P(wake) = sigmoid(l1 - l0)
 *
 * ~20 M MAC per inference; scalar float on the dedicated A53 finishes in a
 * few ms, far under the 80 ms inference period.  The two ping-pong
 * activation buffers (288 KB each) are heap-allocated on first use: as
 * static .bss they landed inside the objcopy'd raw image as 576 KB of
 * zero padding, overflowing the board's 2 MB amp partition.
 *
 * SPDX-License-Identifier: Apache-2.0
 ****************************************************************************/

#include <math.h>
#include <stdlib.h>

#include "kws.h"
#include "kws_model_data.h"

/****************************************************************************
 * Private Data
 ****************************************************************************/

static float *g_act_a;
static float *g_act_b;

/* Input after per-window cepstral mean normalization (CMN): subtracting
 * each mel bin's mean over the window kills the channel/level dimension
 * (speaker-distance, mic gain, room coloring) that a 10k-parameter net
 * otherwise latches onto.  Training applies the identical transform.
 */

static float g_nn_in[KWS_IN_T * KWS_IN_M];

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static void conv1(const float *feat, float *out)
{
  int oc;

  for (oc = 0; oc < KWS_CH; oc++)
    {
      const float *w = &g_kws_conv1_w[oc * KWS_C1_KT * KWS_C1_KM];
      const float b = g_kws_conv1_b[oc];
      float *dst = &out[oc * KWS_PIX];
      int oh;

      for (oh = 0; oh < KWS_C1_H; oh++)
        {
          int ow;

          for (ow = 0; ow < KWS_C1_W; ow++)
            {
              float acc = b;
              int kt;

              for (kt = 0; kt < KWS_C1_KT; kt++)
                {
                  int t = oh * KWS_C1_ST - KWS_C1_PT + kt;
                  const float *frow;
                  const float *wrow;
                  int km;

                  if (t < 0 || t >= KWS_IN_T)
                    {
                      continue;
                    }

                  frow = &feat[t * KWS_IN_M];
                  wrow = &w[kt * KWS_C1_KM];

                  for (km = 0; km < KWS_C1_KM; km++)
                    {
                      int m = ow * KWS_C1_SM - KWS_C1_PM + km;

                      if (m >= 0 && m < KWS_IN_M)
                        {
                          acc += frow[m] * wrow[km];
                        }
                    }
                }

              *dst++ = acc > 0.0f ? acc : 0.0f;
            }
        }
    }
}

static void depthwise3x3(const float *in, const float *w, const float *b,
                         float *out)
{
  int c;

  for (c = 0; c < KWS_CH; c++)
    {
      const float *src = &in[c * KWS_PIX];
      const float *k = &w[c * 9];
      float *dst = &out[c * KWS_PIX];
      int oh;

      for (oh = 0; oh < KWS_C1_H; oh++)
        {
          int ow;

          for (ow = 0; ow < KWS_C1_W; ow++)
            {
              float acc = b[c];
              int kt;

              for (kt = 0; kt < 3; kt++)
                {
                  int t = oh - 1 + kt;
                  int km;

                  if (t < 0 || t >= KWS_C1_H)
                    {
                      continue;
                    }

                  for (km = 0; km < 3; km++)
                    {
                      int m = ow - 1 + km;

                      if (m >= 0 && m < KWS_C1_W)
                        {
                          acc += src[t * KWS_C1_W + m] * k[kt * 3 + km];
                        }
                    }
                }

              dst[oh * KWS_C1_W + ow] = acc > 0.0f ? acc : 0.0f;
            }
        }
    }
}

/* Pointwise 1x1: out[oc] = relu(b + sum_ic w[oc][ic] * in[ic]), streamed
 * over pixels so every inner loop walks contiguous memory.
 */

static void pointwise(const float *in, const float *w, const float *b,
                      float *out)
{
  int oc;
  int ic;
  int p;

  for (oc = 0; oc < KWS_CH; oc++)
    {
      float *dst = &out[oc * KWS_PIX];
      const float bias = b[oc];

      for (p = 0; p < KWS_PIX; p++)
        {
          dst[p] = bias;
        }

      for (ic = 0; ic < KWS_CH; ic++)
        {
          const float wv = w[oc * KWS_CH + ic];
          const float *src = &in[ic * KWS_PIX];

          for (p = 0; p < KWS_PIX; p++)
            {
              dst[p] += wv * src[p];
            }
        }

      for (p = 0; p < KWS_PIX; p++)
        {
          if (dst[p] < 0.0f)
            {
              dst[p] = 0.0f;
            }
        }
    }
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

float kws_nn_infer(const float *feat)
{
  float pooled[KWS_CH];
  float l0;
  float l1;
  int blk;
  int c;

  int t;
  int m2;

  if (g_act_a == NULL)
    {
      g_act_a = malloc(KWS_CH * KWS_PIX * sizeof(float));
      g_act_b = malloc(KWS_CH * KWS_PIX * sizeof(float));
      if (g_act_a == NULL || g_act_b == NULL)
        {
          free(g_act_a);
          free(g_act_b);
          g_act_a = NULL;
          g_act_b = NULL;
          return 0.0f;
        }
    }

  /* Per-window CMN, mirrored exactly by train.py */

  for (m2 = 0; m2 < KWS_IN_M; m2++)
    {
      float mean = 0.0f;

      for (t = 0; t < KWS_IN_T; t++)
        {
          mean += feat[t * KWS_IN_M + m2];
        }

      mean /= (float)KWS_IN_T;

      for (t = 0; t < KWS_IN_T; t++)
        {
          g_nn_in[t * KWS_IN_M + m2] = feat[t * KWS_IN_M + m2] - mean;
        }
    }

  conv1(g_nn_in, g_act_a);

  for (blk = 0; blk < KWS_BLOCKS; blk++)
    {
      depthwise3x3(g_act_a, g_kws_dw_w[blk], g_kws_dw_b[blk], g_act_b);
      pointwise(g_act_b, g_kws_pw_w[blk], g_kws_pw_b[blk], g_act_a);
    }

  for (c = 0; c < KWS_CH; c++)
    {
      const float *src = &g_act_a[c * KWS_PIX];
      float acc = 0.0f;
      int p;

      for (p = 0; p < KWS_PIX; p++)
        {
          acc += src[p];
        }

      pooled[c] = acc / (float)KWS_PIX;
    }

  l0 = g_kws_fc_b[0];
  l1 = g_kws_fc_b[1];

  for (c = 0; c < KWS_CH; c++)
    {
      l0 += g_kws_fc_w[c] * pooled[c];
      l1 += g_kws_fc_w[KWS_CH + c] * pooled[c];
    }

  /* softmax over two logits == sigmoid of the difference */

  return 1.0f / (1.0f + expf(l0 - l1));
}
