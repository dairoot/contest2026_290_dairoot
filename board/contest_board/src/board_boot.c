/****************************************************************************
 * board/contest_board/src/board_boot.c
 *
 * KickPi K7 (RK3576) AMP slave board bring-up.
 *
 * This image boots BEFORE Linux, so it must program its own UART5 clock
 * gates and pin multiplexing (values match the kernel clk/pinctrl drivers;
 * Rockchip registers use a write-mask in the upper 16 bits).  Once Linux is
 * up, its rockchip-amp DTS node keeps these clocks/pins alive.
 *
 * SPDX-License-Identifier: Apache-2.0
 ****************************************************************************/

/****************************************************************************
 * Included Files
 ****************************************************************************/

#include <nuttx/config.h>
#include <nuttx/arch.h>
#include <nuttx/board.h>

#include <arch/chip/chip.h>

#include "arm64_internal.h"
#include "rk3576_boot.h"
#include "rk3576_soc.h"

#ifdef CONFIG_RPTUN
#include <nuttx/irq.h>
#include <nuttx/kthread.h>
#include <nuttx/semaphore.h>
#include <arch/chip/chip.h>

/* Provided by the chip layer (arch/arm64/src/rk3576/rk3576_rptun.c) */

int rk3576_rptun_init(const char *shmemname, const char *cpuname);

/* Raw-byte "rpmsg-tty" echo endpoint, wire-compatible with Linux's
 * rpmsg_tty (see rk3576_rpmsg_echo.c). */

#if defined(CONFIG_RK3576_PDM) || defined(CONFIG_RK3576_SAI)
int rk3576_mic_init(void);
#else
int rk3576_rpmsg_echo_init(void);
#endif

/* AMP link-up handshake.
 *
 * openvela boots (from U-Boot) BEFORE Linux, so if we announced our rpmsg
 * service immediately the kick would hit Linux while its virtqueues are
 * still NULL (rk_rpmsg_rx_callback -> vring_interrupt(0, NULL) oops).
 *
 * The Rockchip master signals readiness the same way rpmsg-lite's
 * wait_for_link_up expects: once Linux has filled its receive vring it
 * kicks us via GIC SPI (INTID 172).  So we wait for that first kick, then
 * bring rptun up (which announces "rpmsg-tty" and kicks Linux back).
 */

static sem_t g_amp_link_sem;

static int rk3576_amp_link_isr(int irq, FAR void *context, FAR void *arg)
{
  nxsem_post(&g_amp_link_sem);
  return OK;
}

static int rk3576_amp_link_thread(int argc, FAR char *argv[])
{
  nxsem_init(&g_amp_link_sem, 0, 0);

  irq_attach(RK3576_IRQ_RPMSG_RX, rk3576_amp_link_isr, NULL);
  up_prioritize_irq(RK3576_IRQ_RPMSG_RX, RK3576_AMP_IRQ_PRIO_VAL);
  up_enable_irq(RK3576_IRQ_RPMSG_RX);

  /* Block until the Linux master kicks us (its rvq is ready) */

  nxsem_wait(&g_amp_link_sem);

  up_disable_irq(RK3576_IRQ_RPMSG_RX);
  irq_detach(RK3576_IRQ_RPMSG_RX);

  /* Register our raw echo endpoint BEFORE bringing rptun up, so the
   * device_created callback fires as soon as the rpmsg device appears.
   */

#if defined(CONFIG_RK3576_PDM) || defined(CONFIG_RK3576_SAI)
  rk3576_mic_init();
#else
  rk3576_rpmsg_echo_init();
#endif

  /* Linux is ready: rptun creates the rpmsg device, which triggers our
   * echo endpoint to announce "rpmsg-tty" to Linux.
   */

  rk3576_rptun_init("rpmsg_shm", "linux");
  return 0;
}
#endif

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/****************************************************************************
 * Name: rk3576_board_initialize
 *
 * Description:
 *   Called from arm64_chip_boot() right after the MMU is enabled and
 *   before the early serial init.  Programs UART5 clocks and pinmux.
 *
 ****************************************************************************/

void rk3576_board_initialize(void)
{
  /* UART5 console clocks:
   *   PCLK_UART5   CLKGATE_CON13 bit14
   *   SCLK_UART5   CLKSEL_CON64 mux[10:8]=3 (xin24m), div[7:0]=1
   *                CLKGATE_CON14 bit15
   */

  rk3576_clk_gate(13, 14, true);
  rk3576_clk_set_mux_div(64, 8, 3, 3, 0, 8, 1);
  rk3576_clk_gate(14, 15, true);

  /* uart5_rx_m0 / uart5_tx_m0 on GPIO3_D4/D5 (40-pin header), func 9 */

  rk3576_iomux(RK3576_GPIO_BANK3, RK3576_PIN_D(4), 9);
  rk3576_iomux(RK3576_GPIO_BANK3, RK3576_PIN_D(5), 9);
  rk3576_pull(RK3576_GPIO_BANK3, RK3576_PIN_D(4), RK3576_PULL_UP);

#ifdef CONFIG_RK3576_AMP_HEARTBEAT
  /* Route our SPIs to this core before any driver enables them (GICv2
   * reset leaves SPI targets empty and the slave skips distributor init).
   */

  {
    extern void rk3576_amp_irq_route(void);
    rk3576_amp_irq_route();
  }
#endif
}

/****************************************************************************
 * Name: board_late_initialize
 *
 * Description:
 *   Board late init: start the AMP heartbeat / defensive IRQ re-enable
 *   thread (see rk3576_heartbeat.c).
 *
 ****************************************************************************/

#ifdef CONFIG_BOARD_LATE_INITIALIZE
void board_late_initialize(void)
{
#ifdef CONFIG_RK3576_AMP_HEARTBEAT
  extern int rk3576_heartbeat_start(void);
  rk3576_heartbeat_start();
#endif

#ifdef CONFIG_RPTUN
  /* Defer rptun bring-up until Linux signals its rpmsg vrings are ready
   * (see rk3576_amp_link_thread).  Announcing earlier would crash the Linux
   * host (vq not yet created).
   */

  kthread_create("amp_link", 100, 2048, rk3576_amp_link_thread, NULL);
#endif
}
#endif

