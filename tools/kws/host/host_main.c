/* Host-side harness for the KWS engine: builds the exact same C sources
 * that go into the firmware and checks them against the Python pipeline.
 *
 *   ./kws_host golden                 parity vs torch/numpy (kws_golden.h)
 *   ./kws_host wav <f.wav> [thr]      stream one file, print detections
 *   ./kws_host batch <dir> <0|1> [thr]  all wavs in dir, expect label,
 *                                     print detect rate / false alarms
 *
 * WAVs must be 16 kHz mono s16 (what the pipeline produces).
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <dirent.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "kws.h"
#include "kws_tables.h"
#include "kws_golden.h"

#define CHUNK 224          /* one rpmsg PCM chunk, like on the wire */

static int g_detections;
static float g_first_prob;
static double g_first_t;
static double g_now_t;

static void on_detect(float prob, void *arg)
{
  (void)arg;
  if (g_detections == 0)
    {
      g_first_prob = prob;
      g_first_t = g_now_t;
    }

  g_detections++;
  printf("  DETECT t=%.2fs smoothed=%.3f\n", g_now_t, prob);
}

static int16_t *read_wav(const char *path, long *n_out)
{
  FILE *f = fopen(path, "rb");
  unsigned char h[12];
  int16_t *pcm = NULL;
  long n = 0;

  if (f == NULL)
    {
      fprintf(stderr, "cannot open %s\n", path);
      return NULL;
    }

  if (fread(h, 1, 12, f) != 12 || memcmp(h, "RIFF", 4) ||
      memcmp(h + 8, "WAVE", 4))
    {
      fprintf(stderr, "%s: not a RIFF/WAVE\n", path);
      fclose(f);
      return NULL;
    }

  for (; ; )
    {
      unsigned char ch[8];
      unsigned long sz;

      if (fread(ch, 1, 8, f) != 8)
        {
          break;
        }

      sz = (unsigned long)ch[4] | ((unsigned long)ch[5] << 8) |
           ((unsigned long)ch[6] << 16) | ((unsigned long)ch[7] << 24);

      if (!memcmp(ch, "fmt ", 4))
        {
          unsigned char fmt[16];

          if (sz < 16 || fread(fmt, 1, 16, f) != 16)
            {
              break;
            }

          if ((fmt[0] | (fmt[1] << 8)) != 1 || fmt[2] != 1 ||
              (fmt[4] | (fmt[5] << 8) | (fmt[6] << 16)) != KWS_SR ||
              (fmt[14] | (fmt[15] << 8)) != 16)
            {
              fprintf(stderr, "%s: need 16 kHz mono s16 PCM\n", path);
              fclose(f);
              return NULL;
            }

          if (sz > 16)
            {
              fseek(f, (long)sz - 16, SEEK_CUR);
            }
        }
      else if (!memcmp(ch, "data", 4))
        {
          n = (long)sz / 2;
          pcm = malloc(n * sizeof(int16_t));
          if (pcm == NULL || fread(pcm, 2, n, f) != (size_t)n)
            {
              free(pcm);
              pcm = NULL;
            }

          break;
        }
      else
        {
          fseek(f, (long)((sz + 1) & ~1ul), SEEK_CUR);
        }
    }

  fclose(f);
  if (pcm == NULL)
    {
      fprintf(stderr, "%s: no data chunk\n", path);
    }

  *n_out = n;
  return pcm;
}

static void stream(const int16_t *pcm, long n)
{
  static const int16_t silence[CHUNK];
  long i;

  for (i = 0; i < n; i += CHUNK)
    {
      long c = n - i < CHUNK ? n - i : CHUNK;

      g_now_t = (double)i / KWS_SR;
      kws_engine_push(&pcm[i], (size_t)c);
    }

  /* A real microphone keeps running after the phrase; without this tail
   * the score smoother never gets its post-phrase inferences and late
   * detections are lost at end-of-file.
   */

  for (i = 0; i < (long)(KWS_SR * 6 / 10); i += CHUNK)
    {
      g_now_t = (double)(n + i) / KWS_SR;
      kws_engine_push(silence, CHUNK);
    }
}

static int run_golden(void)
{
  int fail = 0;
  int gi;

  for (gi = 0; gi < KWS_GOLDEN_COUNT; gi++)
    {
      const struct kws_golden_case *g = &g_kws_golden[gi];
      float feat[KWS_NMEL];
      float maxd = 0.0f;
      float prob;
      float pd;
      int i;
      int m;

      for (i = 0; i < g->nframes; i++)
        {
          kws_frontend_frame(&g->pcm[i * KWS_HOP], feat);

          for (m = 0; m < KWS_NMEL; m++)
            {
              float d = fabsf(feat[m] - g->feat[i * KWS_NMEL + m]);

              if (d > maxd)
                {
                  maxd = d;
                }
            }
        }

      prob = kws_nn_infer(g->feat);      /* python features -> C net */
      pd = fabsf(prob - *g->prob);

      printf("%-6s frames=%d  max|feat C-py|=%.2e  "
             "prob C=%.6f py=%.6f |d|=%.2e  %s\n",
             g->name, g->nframes, maxd, prob, *g->prob, pd,
             (maxd < 5e-3f && pd < 2e-3f) ? "PASS" : "FAIL");

      if (!(maxd < 5e-3f && pd < 2e-3f))
        {
          fail = 1;
        }
    }

  return fail;
}

static void report(const char *path)
{
  kws_stats_t st;

  kws_engine_get_stats(&st);
  printf("%s: detections=%d peak=%.3f last_infer=%uus\n",
         path, g_detections, st.peak, st.infer_us);
}

int main(int argc, char **argv)
{
  if (argc >= 2 && strcmp(argv[1], "golden") == 0)
    {
      return run_golden();
    }

  if (argc >= 2 && strcmp(argv[1], "test") == 0)
    {
      float pz;
      float po;

      kws_engine_selftest(&pz, &po);
      printf("selftest z=%lu o=%lu (micro)\n",
             (unsigned long)(pz * 1e6f + 0.5f),
             (unsigned long)(po * 1e6f + 0.5f));
      return 0;
    }

  if (argc >= 3 && strcmp(argv[1], "wav") == 0)
    {
      long n;
      int16_t *pcm = read_wav(argv[2], &n);

      if (pcm == NULL)
        {
          return 1;
        }

      kws_engine_init(on_detect, NULL);
      if (argc >= 4)
        {
          kws_engine_set_threshold((float)atof(argv[3]));
        }

      g_detections = 0;
      stream(pcm, n);
      report(argv[2]);
      free(pcm);
      return 0;
    }

  if (argc >= 4 && strcmp(argv[1], "batch") == 0)
    {
      int expect = atoi(argv[3]);
      DIR *d = opendir(argv[2]);
      struct dirent *e;
      int total = 0;
      int hit = 0;
      int events = 0;
      double sum_first = 0.0;

      if (d == NULL)
        {
          fprintf(stderr, "cannot open dir %s\n", argv[2]);
          return 1;
        }

      while ((e = readdir(d)) != NULL)
        {
          char path[1024];
          long n;
          int16_t *pcm;

          if (strstr(e->d_name, ".wav") == NULL)
            {
              continue;
            }

          snprintf(path, sizeof(path), "%s/%s", argv[2], e->d_name);
          pcm = read_wav(path, &n);
          if (pcm == NULL)
            {
              continue;
            }

          kws_engine_init(NULL, NULL);
          if (argc >= 5)
            {
              kws_engine_set_threshold((float)atof(argv[4]));
            }

          g_detections = 0;
          kws_engine_init(on_detect, NULL);
          if (argc >= 5)
            {
              kws_engine_set_threshold((float)atof(argv[4]));
            }

          stream(pcm, n);
          total++;
          events += g_detections;
          if (g_detections > 0)
            {
              hit++;
              sum_first += g_first_prob;
              if (expect == 0)
                {
                  printf("  FA %-40s p=%.3f\n", e->d_name, g_first_prob);
                }
            }

          free(pcm);
        }

      closedir(d);
      printf("\n%d files, expect label %d: fired on %d (%.1f%%), "
             "%d events total", total, expect, hit,
             total ? 100.0 * hit / total : 0.0, events);
      if (hit)
        {
          printf(", mean first-prob %.3f", sum_first / hit);
        }

      printf("\n");
      return 0;
    }

  fprintf(stderr,
          "usage: %s golden | wav <f.wav> [thr] | batch <dir> <0|1> [thr]\n",
          argv[0]);
  return 2;
}
