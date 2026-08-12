/****************************************************************************
 * board/contest_board/src/rk3576_rpmsg_echo.c
 *
 * Raw-byte rpmsg echo endpoint named "rpmsg-tty".
 *
 * NuttX's built-in uart_rpmsg ("rpmsg-tty") uses a private framed protocol
 * (uart_rpmsg_write_s with a command/response header) that is NOT
 * wire-compatible with Linux's drivers/tty/rpmsg_tty.c, which sends raw
 * bytes.  So instead of uart_rpmsg we register our own endpoint on the
 * "rpmsg-tty" name that simply echoes whatever raw bytes Linux sends,
 * demonstrating the bidirectional AMP link:
 *
 *   Linux:  echo hello > /dev/ttyRPMSG0 ; cat /dev/ttyRPMSG0   -> "hello"
 *
 * SPDX-License-Identifier: Apache-2.0
 ****************************************************************************/

/****************************************************************************
 * Included Files
 ****************************************************************************/

#include <nuttx/config.h>

#include <string.h>

#include <nuttx/rpmsg/rpmsg.h>

/****************************************************************************
 * Private Data
 ****************************************************************************/

static struct rpmsg_endpoint g_echo_ept;

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static int rk3576_echo_ept_cb(FAR struct rpmsg_endpoint *ept, FAR void *data,
                              size_t len, uint32_t src, FAR void *priv)
{
  /* Echo the raw payload straight back to the sender. */

  rpmsg_sendto(ept, data, (int)len, src);
  return 0;
}

static void rk3576_echo_device_created(FAR struct rpmsg_device *rdev,
                                       FAR void *priv)
{
  if (strcmp("linux", rpmsg_get_cpuname(rdev)) == 0)
    {
      rpmsg_create_ept(&g_echo_ept, rdev, "rpmsg-tty",
                       RPMSG_ADDR_ANY, RPMSG_ADDR_ANY,
                       rk3576_echo_ept_cb, NULL);
    }
}

static void rk3576_echo_device_destroy(FAR struct rpmsg_device *rdev,
                                       FAR void *priv)
{
  if (strcmp("linux", rpmsg_get_cpuname(rdev)) == 0)
    {
      rpmsg_destroy_ept(&g_echo_ept);
    }
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int rk3576_rpmsg_echo_init(void)
{
  return rpmsg_register_callback(NULL,
                                 rk3576_echo_device_created,
                                 rk3576_echo_device_destroy,
                                 NULL, NULL);
}
