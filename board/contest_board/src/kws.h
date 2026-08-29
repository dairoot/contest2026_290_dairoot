/****************************************************************************
 * board/contest_board/src/kws.h
 *
 * Offline wake-word ("你好，openvela") engine: public API and the contract
 * between the three translation units:
 *
 *   kws_frontend.c  int16 frame -> normalized 40-dim log-mel
 *                   (tables in generated kws_tables.h)
 *   kws_nn.c        150x40 features -> P(wake)  (weights in generated
 *                   kws_model_data.h, BatchNorm pre-folded)
 *   kws_engine.c    streaming glue: sample buffer, feature ring, inference
 *                   cadence, smoothing, threshold, refractory period
 *
 * The same sources build on the development host (tools/kws/host) where
 * they are verified frame-by-frame against the Python training pipeline.
 * No malloc, no C++, float32 only; needs libm (logf/expf).
 *
 * SPDX-License-Identifier: Apache-2.0
 ****************************************************************************/

#ifndef __BOARD_CONTEST_BOARD_SRC_KWS_H
#define __BOARD_CONTEST_BOARD_SRC_KWS_H

#include <stddef.h>
#include <stdint.h>

/* Decision cadence: one inference per KWS_INFER_EVERY new frames (10 ms
 * each); the published score is the mean of the last KWS_SMOOTH inference
 * outputs; after a detection the engine holds off for KWS_REFRACT_FRAMES.
 *
 * Tuning note: training marks a window positive only while the complete
 * phrase ends within the last ~0.4 s (earlier truncations are hard
 * NEGATIVES), so the high-score regime after the phrase lasts ~0.4 s.
 * The cadence and smoothing depth must fit 2 inferences into that span
 * with margin — 80 ms x 2 does; 120 ms x 3 provably did not (62% hit
 * rate on the streaming eval vs >90% after this change).
 */

#ifndef KWS_INFER_EVERY
#  define KWS_INFER_EVERY   8           /* 80 ms */
#endif
#ifndef KWS_SMOOTH
#  define KWS_SMOOTH        3
#endif
#ifndef KWS_REFRACT_FRAMES
#  define KWS_REFRACT_FRAMES 200        /* 2 s */
#endif

/* Hysteresis: after a detection the engine stays disarmed until the
 * smoothed score has fallen below KWS_REARM_THRESHOLD, on top of the
 * refractory period.  The v7 models keep scoring a just-spoken phrase
 * above the threshold for up to 3-4 s while it slides out of the window
 * (owner's takes, 2026-08-29), which produced a second "ding" the moment
 * the 2 s refractory expired.  Two genuine wakes are always separated by
 * a dip in the score, so nothing real is lost.
 */

#ifndef KWS_REARM_THRESHOLD
#  define KWS_REARM_THRESHOLD 0.50f
#endif

/* Detections are suppressed for the first KWS_WARMUP_FRAMES of AUDIO
 * after (re)init.  The Linux boot deterministically pops the speaker
 * twice while initializing the ES8388 (observed at 3.7 s and 8.8 s of
 * audio time, scoring 0.95+ every boot); the engine is re-initialized on
 * rpmsg link-up so this window is anchored to the real capture start.
 */

#ifndef KWS_WARMUP_FRAMES
#  define KWS_WARMUP_FRAMES 1000        /* 10 s of audio */
#endif

typedef void (*kws_detect_cb_t)(float prob, void *arg);

typedef struct
{
  uint32_t frames;        /* feature frames produced */
  uint32_t inferences;
  uint32_t detections;
  float    last_prob;     /* raw output of the last inference */
  float    smoothed;      /* smoothed score at the last inference */
  float    peak;          /* max smoothed score since reset_peak */
  uint32_t infer_us;      /* wall time of the last inference */
} kws_stats_t;

void  kws_engine_init(kws_detect_cb_t cb, void *arg);
void  kws_engine_push(const int16_t *pcm, size_t n);
void  kws_engine_set_threshold(float th);
float kws_engine_threshold(void);

/* Suppress detections for the next `frames` feature frames — used around
 * capture restarts, whose filter-settle transient deterministically
 * scores far above any sane threshold.
 */

void  kws_engine_mute(uint32_t frames);
void  kws_engine_get_stats(kws_stats_t *out);
void  kws_engine_reset_peak(void);

/* Diagnostics: constant feature windows through the net (weights-only
 * fingerprint) and a peek at what the engine actually ingests.
 */

void  kws_engine_selftest(float *p_zero, float *p_one);
void  kws_engine_peek(int16_t *pcm_peak, float feat[3]);

/* Internal, exposed for the host-side parity harness */

void  kws_frontend_frame(const int16_t *pcm, float *out);
float kws_nn_infer(const float *feat);

#endif /* __BOARD_CONTEST_BOARD_SRC_KWS_H */
