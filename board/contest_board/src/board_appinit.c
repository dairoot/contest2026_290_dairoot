/****************************************************************************
 * board/contest_board/src/board_appinit.c
 *
 * SPDX-License-Identifier: Apache-2.0
 ****************************************************************************/

#include <nuttx/config.h>
#include <nuttx/board.h>

#include <sys/types.h>

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/****************************************************************************
 * Name: board_app_initialize
 *
 * Description:
 *   Perform application-level initialization (called via
 *   boardctl(BOARDIOC_INIT) from NSH).  Nothing extra needed yet: the AMP
 *   heartbeat starts from board_late_initialize().
 *
 ****************************************************************************/

int board_app_initialize(uintptr_t arg)
{
  return OK;
}
