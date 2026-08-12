# Contest 2026 team 290 board - src
nuttx_add_library(board)

set(SRCS board_boot.c board_appinit.c)

if(CONFIG_RPTUN)
  if(CONFIG_RK3576_PDM OR CONFIG_RK3576_SAI)
    list(APPEND SRCS rk3576_mic.c)
  else()
    list(APPEND SRCS rk3576_rpmsg_echo.c)
  endif()
endif()

if(CONFIG_RK3576_AMP_HEARTBEAT)
  list(APPEND SRCS rk3576_heartbeat.c)
endif()

target_sources(board PRIVATE ${SRCS})
