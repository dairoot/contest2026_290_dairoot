# KickPi K7（RK3576）40-pin 排针：引脚定义与电平域

来源：<https://doc.kickpi.cn/Products/Peripherals-and-Interfaces/Pinout/#kickpi-k7>
（官方引脚图 + 电压图转录，2026-08-13）

> **这个排针不是树莓派兼容布局。** GND 在 6/13/20/27/31/34/39，而 RPi 标准的
> 9/14/25/30 在这块板上是 GPIO 或电源。按 RPi 的印象接线会接错。

## 完整对照表

左右两列对应排针的实际排布（奇数在左、偶数在右）。"电平"为该脚所属 IO 域。

| 电平 | 复用功能 | 信号 | # | # | 信号 | 复用功能 | 电平 |
|---|---|---|---|---|---|---|---|
| — | — | VCC_3V3_S0 | 1 | 2 | VCC5V0_SYS | — | — |
| — | — | VCC_3V3_S0 | 3 | 4 | VCC5V0_SYS | — | — |
| 1.8V | UART6_TX_M0 / CAN0_TX_M2 | GPIO4_A4 | 5 | 6 | **GND** | — | — |
| 1.8V | UART6_RX_M0 / CAN0_RX_M2 / *PDM1_CLK0_M1 | GPIO4_A6 | 7 | 8 | GPIO4_B0 | PDM1_CLK1_M1 / SPI4_CLK_M2 | 1.8V |
| 3.3V | *PWM2_CH7_M2 | GPIO2_D7 | 9 | 10 | GPIO4_B1 | PDM1_SDI2_M1 / SPI4_MOSI_M2 | 1.8V |
| 3.3V | *PWM2_CH6_M2 | GPIO2_D6 | 11 | 12 | GPIO4_B2 | PDM1_SDI1_M1 / SPI4_MISO_M2 | 1.8V |
| — | — | **GND** | 13 | 14 | GPIO0_C3 | *PWM0_CH1_M0 | 3.3V |
| — | — | VCC_3V3_S0 | 15 | 16 | GPIO1_D0 | UART10_TX_M1 / SPI_CS | 1.8V |
| 1.8V | UART8_RX_M0 / PWM2_CH2_M3 | GPIO3_C5 | 17 | 18 | GPIO1_D1 | UART10_RX_M1 / I3C0_SDA_PU_M1 | 1.8V |
| 1.8V | UART8_TX_M0 | GPIO3_C6 | 19 | 20 | **GND** | — | — |
| 1.8V | UART5_TX_M0 / I2C3_SDA_M2 | *GPIO3_D5 | 21 | 22 | GPIO3_A0 | *I2C7_SCL_M1 / UART3_TX_M0 | 3.3V |
| 1.8V | UART5_RX_M0 / I2C3_SCL_M2 | *GPIO3_D4 | 23 | 24 | GPIO3_A1 | *I2C7_SDA_M1 / UART3_RX_M0 | 3.3V |
| — | — | VCC_3V3_S0 | 25 | 26 | GPIO1_D2 | *I3C0_SCL_M1 / PWM1_CH3_M1 | 1.8V |
| — | — | **GND** | 27 | 28 | GPIO1_D3 | *I3C0_SDA_M1 / PWM1_CH4_M1 | 1.8V |
| 1.8V | — | *GPIO0_A5 | 29 | 30 | *GPIO3_D0 | PWM1_CH0_M3 | 1.8V |
| — | — | **GND** | 31 | 32 | *GPIO3_A4 | PWM2_CH3_M3 | 1.8V |
| 1.8V | — | *GPIO3_B0 | 33 | 34 | **GND** | — | — |
| 3.3V | DEBUG_UART0_RX | GPIO0_D5 | 35 | 36 | SARADC_VIN4 | — | — |
| 3.3V | DEBUG_UART0_TX | GPIO0_D4 | 37 | 38 | SARADC_VIN5 | — | — |
| — | — | **GND** | 39 | 40 | SARADC_VIN6 | — | — |

`*` 号沿用官方图上的标注。pin 7 的 `PDM1_CLK0_M1` 官方引脚图未标出，但
`arch/arm64/src/rk3576/rk3576_pdm.c` 把 GPIO4_A6 iomux 成 function 3 即
`pdm1m1_clk0`，实测可用。

## 速查

**GND**：6、13、20、27、31、34、39
**电源**：3V3 在 1/3/15/25（`VCC_3V3_S0`），5V 在 2/4（`VCC5V0_SYS`）
**3.3V IO 域的脚**：9、11、14、22、24、35、37 —— 只有这 7 个，其余 GPIO 全是 1.8V
**ADC**：36、38、40（`SARADC_VIN4/5/6`）

排针上**没有 1.8V 电源脚**。需要 1.8V 供电时：从 pin 1 的 3V3 挂一颗 1.8V LDO，
或应急拿一个 1.8V GPIO 当电源（见下）。

## 本项目用到的脚位

| 用途 | 脚位 | 信号 | 备注 |
|---|---|---|---|
| PDM 麦 CLK | **7**（或 8） | GPIO4_A6 = `pdm1m1_clk0`（GPIO4_B0 = `clk1`） | 抢 uart6 / spi4 |
| PDM 麦 DATA | **12** | GPIO4_B2 = `pdm1m1_sdi1` → 内部 path 0 | 唯一选择，`sdi0` 未引出 |
| I2S 麦 SCK | **18** | GPIO1_D1 = `sai2m0` | 抢 i3c0 |
| I2S 麦 WS | **26** | GPIO1_D2 | |
| I2S 麦 SD | **28** | GPIO1_D3 | |
| 麦克风 GND | **27** | GND | |
| 麦克风 L/R | **31** | GND | 接地=选左声道（驱动取左槽） |
| 麦克风 VDD（应急 1.8V） | **30** | GPIO3_D0 | `/sys/class/leds/GPIO3_D0/brightness` |
| openvela NuttShell | **21 / 23** | GPIO3_D5/D4 = UART5，1500000 8N1 | |

**可当 1.8V 电源用的 GPIO**：30（GPIO3_D0）、32（GPIO3_A4）、33（GPIO3_B0），
三个都在设备树里注册成了 led：

```bash
echo 1 | sudo tee /sys/class/leds/GPIO3_D0/brightness   # 拉高
cat /sys/class/leds/GPIO3_D0/brightness                # 查询
```

**每次板子重启后都会回到 0**，麦克风会因此断电——采到全 0 时先查这个。
INMP441 典型 1.4 mA，GPIO 焊盘带得动；耗流更大的器件要用 LDO。

## 电平域的坑

1.8V 域的脚（绝大多数 GPIO，包括 PDM 和 SAI 的全部信号脚）**不能接 3.3V 器件**：
往里灌 3.3V 会损伤焊盘，而器件的 VIH 随自己 VDD 走（≈0.65×VDD），3V3 供电的
器件也认不出板子发出的 1.8V 时钟——两个方向都不成立。跨域必须加电平转换。

SKR0710（本项目用的 PDM 指向性麦）标称就是 1.8V 单电压、推荐时钟 3.072 MHz。
