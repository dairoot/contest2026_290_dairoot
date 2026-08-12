/****************************************************************************
 * board/contest_board/src/rk3576_heartbeat.c
 *
 * AMP heartbeat + defensive interrupt re-enable.
 *
 * Shared block at 0x47c00000 (read from Linux via devmem):
 *   +0x00 u32 magic   0x4b37414d ("K7AM")
 *   +0x04 u32 version 3 = openvela (1 = bare-metal, 2 = RT-Thread)
 *   +0x08 u64 counter (+1 every 500ms — proof of life & scheduling)
 *   +0x10 u64 cntpct  (generic timer timestamp of last beat)
 *   +0x18 u32 cntfrq
 *
 * The defensive re-enable exists because the Linux GIC init clears
 * GICD_ISENABLER bits once at boot (its vendor AMP patches preserve only
 * routing/priority for amp-irqs).  GICD_ISENABLER is write-1-to-set, so
 * re-asserting our own SPIs every 500ms is atomic and race-free.
 * Lesson learned (the hard way) on the RT-Thread port of this same board.
 *
 * SPDX-License-Identifier: Apache-2.0
 ****************************************************************************/

/****************************************************************************
 * Included Files
 ****************************************************************************/

#include <nuttx/config.h>
#include <nuttx/arch.h>
#include <nuttx/kthread.h>
#include <nuttx/signal.h>

#include <stdint.h>

#include <arch/chip/chip.h>

#include "arm64_internal.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define HB_MAGIC        0x4b37414d
#define HB_VERSION      3           /* openvela */

#define HB32(off)       (*(volatile uint32_t *)(RK3576_HEARTBEAT_BASE + (off)))
#define HB64(off)       (*(volatile uint64_t *)(RK3576_HEARTBEAT_BASE + (off)))

#define GICD_ISENABLER(n) \
  (CONFIG_GICD_BASE + 0x100 + ((n) / 32) * 4)
#define GICD_IPRIORITYR_B(n)  (CONFIG_GICD_BASE + 0x400 + (n))
#define GICD_ITARGETSR_B(n)   (CONFIG_GICD_BASE + 0x800 + (n))

#define RK3576_AMP_CPU_MASK   0x08    /* physical cpu3 (GICv2 target byte) */
#define RK3576_AMP_IRQ_PRIO   0xd0    /* matches Linux amp-irqs priority   */

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static inline uint64_t read_cntpct(void)
{
  uint64_t v;
  __asm__ volatile("isb; mrs %0, cntpct_el0" : "=r"(v));
  return v;
}

static inline uint64_t read_cntfrq(void)
{
  uint64_t v;
  __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(v));
  return v;
}

static void hb_reenable_irq(int intid)
{
  /* Byte-granular writes: only OUR interrupt is touched.  Routing and
   * priority must be programmed by us before Linux boots (GICv2 reset
   * leaves ITARGETSR at 0 = no target, and the slave never runs the
   * distributor init).  Linux later re-applies the same values from its
   * amp-irqs device-tree property.
   */

  putreg8(RK3576_AMP_CPU_MASK, GICD_ITARGETSR_B(intid));
  putreg8(RK3576_AMP_IRQ_PRIO, GICD_IPRIORITYR_B(intid));
  putreg32(1u << (intid % 32), GICD_ISENABLER(intid));
}

/****************************************************************************
 * Name: rk3576_amp_irq_route
 *
 * Description:
 *   Early routing of the SPIs owned by this core (called from board init,
 *   before drivers attach their interrupts).
 *
 ****************************************************************************/

void rk3576_amp_irq_route(void)
{
  hb_reenable_irq(RK3576_IRQ_UART5);

  /* NOTE: RK3576_IRQ_RPMSG_RX (172) is routed/enabled by the rptun layer
   * once a handler is attached (V2).  Enabling it earlier would deliver
   * Linux's first_notify kick into an unexpected-IRQ panic.
   */
}

static int rk3576_heartbeat_thread(int argc, char *argv[])
{
  uint64_t counter = 0;

  HB32(0x00) = HB_MAGIC;
  HB32(0x04) = HB_VERSION;
  HB32(0x18) = (uint32_t)read_cntfrq();

  for (; ; )
    {
      HB64(0x08) = ++counter;
      HB64(0x10) = read_cntpct();
      __asm__ volatile("dsb sy" ::: "memory");

      /* Defensive: our SPI routing/priority/enable must survive the Linux
       * GIC init window.  Linux's gic_dist_init clears every GICD_ISENABLER
       * bit (and its amp-irqs patch only restores routing+priority, not the
       * enable), so without this the rpmsg kick (172) that Linux sends after
       * filling its rvq is silently dropped and the link never comes up.
       */

      hb_reenable_irq(RK3576_IRQ_UART5);
      hb_reenable_irq(RK3576_IRQ_RPMSG_RX);

      nxsig_usleep(500 * 1000);
    }

  return 0;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int rk3576_heartbeat_start(void)
{
  return kthread_create("amp_hb", 100, 2048, rk3576_heartbeat_thread, NULL);
}
