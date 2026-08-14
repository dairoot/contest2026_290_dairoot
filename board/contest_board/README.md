# contest2026_290_board — KickPi K7 (RK3576) 上的 Linux + openvela AMP

映射到 openvela `vendor/openvela/boards/contest2026_290_board`（由本仓
`contest2026_290_dairoot.xml` 的 `<linkfile>` 注入编译树）。

本目录是 **RK3576（KickPi K7）** 的 openvela 板级适配，实现一个真实的
**AMP（非对称多处理）系统**：8 个 A 核里划出 **cpu3（Cortex-A53）独占运行
openvela**，其余 7 核（3×A53 + 4×A72）继续运行 Linux；两个系统通过共享内存 +
GIC 软中断承载的 **RPMsg** 双向通信。**已上板验证**。

```
   Cluster0 (A53)                  Cluster1 (A72)
  cpu0  cpu1  cpu2 | cpu3      b0  b1  b2  b3
  Linux Linux Linux| openvela  Linux ........
                   |  nsh + rpmsg-tty echo
  ── GIC-400 (GICv2)：distributor 归 Linux，openvela 只碰自己的位 ──
  共享内存: vring0@0x47800000 / vring1@0x47808000 / bufpool@0x47a00000
            心跳块@0x47c00000 (magic "K7AM", version=3)
```

## 一、目录结构

```
board/contest_board/
├── Kconfig                     板级选项（心跳线程开关）
├── CMakeLists.txt / src/CMakeLists.txt / src/Makefile
├── configs/nsh/defconfig       板级 defconfig（RAM_START=0x41800000, GICv2, RPTUN...）
├── include/board.h
├── scripts/
│   ├── ld.script               链接脚本（基址 0x41800000）
│   └── Make.defs
├── src/
│   ├── board_boot.c            UART5 时钟/引脚自举；AMP 握手（等 Linux kick）后拉起 rptun
│   ├── board_appinit.c         boardctl(BOARDIOC_INIT)
│   ├── rk3576_heartbeat.c      心跳块 + GIC 中断防御性重使能
│   └── rk3576_rpmsg_echo.c     裸字节 "rpmsg-tty" 回显端点（对接 Linux /dev/ttyRPMSG0）
└── linux-side/                 ★ Linux 侧配套文件（KickPi Linux SDK 上叠加）
    ├── dts/rk3576-kickpi-k7-amp.dtsi          AMP 资源划分设备树
    ├── dts/rk3576-kickpi-k7-linux-amp.dts     K7 板 AMP 变体
    └── configs/{amp-k7.its, parameter-amp.txt, amp-rpmsg.config, *_amp_defconfig}
```

RK3576 芯片层（arch/arm64/src/rk3576、include/rk3576）与 GICv2 AMP-slave 补丁
（`CONFIG_ARM64_GIC_SLAVE`）位于 openvela **nuttx 公共仓**（照 rk3588/rk3399 模板
新增），随本项目一并提供。

## 二、系统设计要点

| 项 | 值/做法 |
|---|---|
| openvela 装载/入口 | `0x41800000`（8MB 私有区），U-Boot 从 `amp` 分区经 PSCI CPU_ON 拉起，早于内核 |
| 异常等级 | EL1（U-Boot 以 hyp=1 进 EL2，`arm64_head.S` 自降 EL1；`CNTVOFF_EL2=0`） |
| GIC | GIC-400（GICv2）。`ARM64_GIC_SLAVE`：**不初始化 distributor**（归 Linux），只做 banked CPU interface + 自有 SPI 的 set-bit 操作 |
| 控制台 | UART5（0x2ad80000，40-pin GPIO3_D4/D5，1.5M 波特），board 自己配 CRU/IOC 时钟引脚 |
| tick | ARM 虚拟定时器 CNTV（PPI 27），24MHz |
| 心跳 | 每 500ms 写 0x47c00000 计数（Linux 可 devmem 读活性）+ 防御性重使能 SPI 113/172 |
| RPMsg | rptun/OpenAMP（remote 角色）+ 静态资源表；vring 固定地址、64 描述符、仅 NS+CPUNAME 特性；notify 写 `GICD_ISPENDR[173]`，收 SPI 172 |
| 通信握手 | openvela 先等 Linux 首个 kick（172，表示 Linux vring 就绪）再 announce，避免 Linux 崩溃 |

**中断分配**（amp-irqs 用绝对 INTID = GIC_SPI + 32）：

| 用途 | GIC_SPI | INTID | 归属 |
|---|---|---|---|
| UART5 控制台 | 81 | 113 | openvela |
| rpmsg kick Linux→openvela | 140 | 172 | openvela |
| rpmsg kick openvela→Linux | 141 | 173 | Linux |

## 三、复现步骤（评委可照做）

前置：一块 KickPi K7（RK3576），已刷过厂商 Linux 固件可正常开机；一台能跑
openvela 工作区和 KickPi Linux SDK 的 Ubuntu 主机。

> **只想验证效果、不想编译**：直接用预编译固件，见 [§三.0](#0-预编译固件不编译走这条)。

### 0. 预编译固件（不编译走这条）

| 文件 | 用途 | 烧写方式 |
|---|---|---|
| `update.img` | **首刷整包**（含 AMP 分区表、AMP 版 dtb、openvela 固件） | RKDevTool 升级模式烧写 |
| `amp.img` | 仅 openvela 固件（1.4MB），迭代用 | `dd` 到 `amp` 分区，见下方第 3 步 |

下载：<!-- TODO: 网盘链接 --> ；校验：<!-- TODO: sha256 -->

`amp.img` 里的 openvela 版本可在板上核对：`sudo busybox devmem 0x47c00004`
读出的心跳版本号应为 `0x3`。

**注意 `amp.img` 不能单独用于首刷**：`amp` 分区是本作品在 `parameter-amp.txt`
里新增的，厂商原厂固件的分区表中没有 `/dev/disk/by-partlabel/amp`，dtb 也没有
摘除 cpu3、划出保留内存、路由 amp-irqs。必须先用 `update.img` 刷过一次整包，
之后才能只刷 `amp.img` 迭代。

镜像中包含 Rockchip 的 loader / TEE / DDR 初始化等二进制，来自 KickPi 提供的
RK3576 Linux SDK，按其原授权分发；本作品对这部分不主张任何权利。

### 1. 编译 openvela 固件（本仓）

```bash
# 在 openvela 工作区根目录（本仓被 repo sync 进来后的上一级）
# 先把公共仓 nuttx 侧的 RK3576 芯片层补丁打上（详见 ../../nuttx-side/README.md）
(cd nuttx && git am ../contest2026_290_dairoot/nuttx-side/*.patch)

./build.sh vendor/openvela/boards/contest2026_290_board/configs/nsh -j$(nproc)
# 产物：nuttx/nuttx.bin（≈1.4MB，入口 0x41800000）
```

### 2. 在 KickPi Linux SDK 上叠加 Linux 侧改动（一次性）

SDK 由 KickPi/板厂渠道获取（Rockchip 私有授权，不可转发，故本仓只提供叠加文件）。
本作品的开发基线是该 SDK 的 `24a411114`（"feat(dts):update K7 wifi dts name"）。
Linux 内核与 U-Boot 的 C 代码**一行未改**——下面这几个 dts / config / 分区表文件
就是 Linux 侧的全部改动。

把 `linux-side/` 下的文件放进 KickPi RK3576 Linux SDK：

```bash
SDK=<你的 rk3576 linux sdk>
cp linux-side/dts/*.dtsi linux-side/dts/*.dts  $SDK/kernel-6.1/arch/arm64/boot/dts/rockchip/
cp linux-side/configs/amp-rpmsg.config          $SDK/kernel-6.1/arch/arm64/configs/
cp linux-side/configs/amp-k7.its \
   linux-side/configs/parameter-amp.txt \
   linux-side/configs/rockchip_rk3576_kickpi_k7_buildroot_amp_defconfig \
                                                 $SDK/device/rockchip/.chips/rk3576/
# dts Makefile 注册一行：
echo 'dtb-$(CONFIG_ARCH_ROCKCHIP) += rk3576-kickpi-k7-linux-amp.dtb' \
     >> $SDK/kernel-6.1/arch/arm64/boot/dts/rockchip/Makefile
```

关键点（这些 Linux 侧改动与具体 RTOS 无关，任何跑在 cpu3 的 RTOS 通用）：
- `rk3576-kickpi-k7-amp.dtsi`：`rockchip-amp` 节点保活 UART5 时钟/引脚、GICv2 亲和
  掩码、amp-irqs 路由 113/172 到 cpu3；4 块保留内存；`&cpu_l3 { status="fail"; }`
  摘除 cpu3；`rockchip,rpmsg-softirq` 节点（SPI 140/141）
- `amp-k7.its`：把 openvela `nuttx.bin` 打进 `amp.img`（cpu=0x3、load=0x41800000、
  hyp=1）；`linux{}` 节点把内核加载地址挪到 0x42000000
- `parameter-amp.txt`：新增 2MB `amp` 分区

### 3. 打包并烧写

全量出 `update.img` 会走 SDK 的 `device/rockchip/common/scripts/mk-amp.sh`，
它有两个依赖**不在 KickPi 发布的 SDK 包里**，需先补上（补完后与 openvela 无关，
一次性）：

```bash
cd $SDK
# (a) mk-amp.sh 用 $SDK/rtos/bsp/rockchip/tools/mkimage 打 FIT，而 SDK 包无 rtos/；
#     指向 rkbin 里同一个 mkimage 即可
mkdir -p rtos/bsp/rockchip/tools
ln -sf ../../../../rkbin/tools/mkimage rtos/bsp/rockchip/tools/mkimage

# (b) mk-amp.sh 无条件取裸机工具链（get_toolchain AMP arm64 "" none，匹配
#     aarch64-none-<x>-gcc），SDK 包里只有 aarch64-none-linux-gnu，取不到就 exit 1。
#     从 ARM 官网下 gcc-arm-10.3-2021.07-x86_64-aarch64-none-elf 解压到：
#     prebuilts/gcc/linux-x86/aarch64/gcc-arm-10.3-2021.07-x86_64-aarch64-none-elf
```

（本作品的 defconfig 已把 `RK_AMP_RTT_TARGET` / `RK_AMP_HAL_TARGET` 置空，
`amp-k7.its` 也没有 `compile {}` 节点，所以 amp 阶段只做打包、不编译任何东西；
上面 (b) 这个工具链是被脚本无条件索取的，装上即可，不会真被调用。）

```bash
cd $SDK
cp <openvela>/nuttx/nuttx.bin output/rtt3.bin      # openvela 固件即 amp3 镜像
./build.sh rockchip_rk3576_kickpi_k7_buildroot_amp_defconfig
./build.sh                                          # 全量出 update.img
# 用 RKDevTool 烧写 output/firmware 里的 update.img
```

板子已刷过 AMP 版固件后，只迭代 openvela 时可只刷 amp 分区（快，且不需要上面
(a)(b) 两个前置——这条路直接用 rkbin 的 mkimage）：
```bash
# 手动打包 amp.img
cd output && cp <openvela>/nuttx/nuttx.bin rtt3.bin && ln -sf \
  ../device/rockchip/.chips/rk3576/amp-k7.its amp.its
sed -i '/share {/,/}/d;/compile {/,/}/d' amp.its
../rkbin/tools/mkimage -f amp.its -E -p 0xe00 amp.img
# 板上：dd 到 amp 分区
scp amp.img root@<板IP>:/tmp/
ssh root@<板IP> 'dd if=/tmp/amp.img of=/dev/disk/by-partlabel/amp conv=fsync; reboot'
```

### 4. 验收（Linux 终端）

```bash
cat /proc/device-tree/model                # Rockchip RK3576 KICKPI K7 Board (AMP)
nproc                                       # 7（cpu3 已划给 openvela）

# openvela 存活：心跳块，version=3(openvela)，counter 每 500ms +1
sudo busybox devmem 0x47c00004             # 0x3
sudo busybox devmem 0x47c00008; sleep 2; sudo busybox devmem 0x47c00008  # +4

# RPMsg 双向通信
ls /dev/ttyRPMSG0
echo hello_openvela > /dev/ttyRPMSG0        # openvela 原样回显
cat /dev/ttyRPMSG0                          # 收到 "hello_openvela"

# openvela 的 NuttShell 在 UART5（40-pin GPIO3_D4/D5，1500000 8N1）：nsh> 可交互
```

实测结果：心跳精确 500ms、Linux 7 核不受扰、`/dev/ttyRPMSG0` 回显 50/50 零丢失、
UART5 出 openvela `nsh>`。

### 5. openvela 独占数字麦克风

板载麦克风与扬声器共用 ES8388 + SAI1，无法拆给两个系统（同一 I2C 编解码器、
同一控制器、同一中断、同一时钟与电源域，且 ES8388 的数据脚只走到 SAI1）。所以
麦克风走**外接数字麦 + openvela 独占控制器**这条路，扬声器仍归 Linux：

```
数字麦 --PDM--> PDM1 (openvela, cpu3) --RPMsg--> Linux /dev/ttyRPMSG0 --> ES8388 扬声器
```

两种前端二选一，编译期决定（`CONFIG_RK3576_PDM` 优先于 `CONFIG_RK3576_SAI`）：

| 前端 | 控制器 | 40-pin 引脚 | 采样率 | Linux 让出 |
|---|---|---|---|---|
| **I2S MEMS 麦（默认）** | SAI2 | SCK **18**，WS **26**，SD **28** | 15625 Hz | i3c0 |
| PDM 数字麦 | PDM1 | CLK **7** 或 **8**，DATA **12** | 16000 Hz | uart6、spi4 |

默认是 I2S，因为它是**用真实麦克风（INMP441）上板跑通过的那一条**。PDM 那条
在无麦条件下验证到了完整链路（见下），但没有实物 PDM 麦做最终确认。

**接线（INMP441）**：SCK→18、WS→26、SD→28、GND→27、L/R→31（选左声道，
驱动取的就是左槽）、VDD→**1.8V**。

**电平必须是 1.8V。** 这些脚都属于 1.8V IO 域，而 40-pin 上只有 3V3/5V 电源。
3V3 供电会把 3.3V 灌进 1.8V 焊盘，而且麦克风的 VIH 是随自己 VDD 走的
（约 0.65×VDD），也认不出 1.8V 的时钟——两头都不成立。正规做法是从脚 1 的
3V3 挂一颗 1.8V LDO。应急也可以**拿一个 1.8V GPIO 当电源**：脚 30 / 32 / 33
在设备树里注册成了 led，`echo 1 > /sys/class/leds/GPIO3_D0/brightness` 即可
拉高，INMP441 典型只吃 1.4 mA，焊盘带得动。

主机侧用法：

```bash
stty -F /dev/ttyRPMSG0 raw -echo
echo MIC_INFO  > /dev/ttyRPMSG0   # MIC src=i2s rate=15625 bits=16 ch=1 ovr=0 drop=0
echo MIC_START > /dev/ttyRPMSG0; timeout 5 cat /dev/ttyRPMSG0 > mic.raw
echo MIC_STOP  > /dev/ttyRPMSG0
aplay -f S16_LE -r 15625 -c 1 mic.raw   # 走 Linux 的 ES8388 喇叭
```

实测（INMP441，安静房间）：5 秒 160964 字节 = 31848 B/s（理论 31250）；
上电瞬态后 RMS≈3、峰值 14~23、每 0.5 秒有 28~44 个不同取值——正常的
MEMS 麦底噪。对比没接麦时整段只有 1 个取值（恒 -1），差别一目了然。

PDM 那条路的实测（未接麦克风）：31846 B/s（理论 32000），`ovr=0 drop=0`，
`fifomax=64`，样本从上电瞬态按 60Hz 高通的时间常数指数衰减到恒 0，说明时钟、
抽取滤波、高通、增益整条链路都通。一条 PDM 数据线载两个声道（靠时钟的两个
沿区分），`MIC_INFO` 回报的 `peak0/peak1` 能看出麦克风挂在哪一路，
`MIC_CH0`/`MIC_CH1` 切换。

**已知限制**：SAI 没有 FIFO 水位中断，只能轮询，被抢占时会溢出——安静场景下
`ovr=0`，但连续大音量采集时观察到过 `ovr` 增长（单样本级丢失）。彻底解决需要
PL330 DMA，而三个 DMAC 都归 Linux。

### 6. 离线唤醒词「你好，openvela」（`CONFIG_RK3576_KWS`，默认开）

openvela 开源版没有可用的离线唤醒引擎（media_trigger 框架的模型接口
`media_trigger_model.h` 全树无实现），本作品自研了一个并常驻 cpu3：

- **引擎**：400/160 滑窗 → 512 点 FFT → 40 mel → 归一化 → 200×40 特征窗
  （2.0s，覆盖「你好，openvela」连逗号停顿平均 1.65s 的完整时长）
  → ~1.1 万参数 DS-CNN（纯 C float32，仅依赖 libm），每 80ms 推理一次，
  最近 2 次平滑过阈值即检出，2s 不应期。权重由 `tools/kws/` 管线训练导出
  （edge-tts 多音色合成 + 板载真实房噪增强 + 截断/近音/音调硬负例），
  C 实现与 Python 训练前端**同源查表 + 板上金标准对拍**（|Δprob|<1e-7）。
- **数据通路**：采集线程把每个 burst 先喂给 KWS 私有环（0.5s），Linux 不在
  录音时发送环不再空转；KWS 线程（优先级 90）消费推理，单次推理数 ms
  （`KWS_INFO` 的 `us` 字段是实测值）。
- **事件上报**：检出后经 `rpmsg-mic` 端点发 `RPMSG_MIC_EVT_WAKE`
  （arg=概率‰，seq=累计次数），`snd_rpmsg_mic.ko` 转成 `openvela-kws`
  input 设备的 `KEY_WAKEUP` 事件并打 dmesg；tty 端点空闲时同发一行
  `EVT WAKE p=0.9xx`。Linux 侧演示脚本 `tools/kws/deploy/wake_watch.py`
  可挂任意命令（放提示音 / 启动 ASR）。板上已装 `kws-wakesound.service`
  开机自启：唤醒成功经喇叭放「叮咚」双音（`deploy/make_ding.py` 合成；
  纯音调类声音，模型经音调硬负例训练对其免疫，实测回灌不自触发）。
- **运行期控制**（tty 端点文本命令）：`KWS_INFO`（状态/计数/耗时）、
  `KWS_ON`/`KWS_OFF`、`KWS_THR <500-999>`（阈值‰）、`KWS_SCORE`
  （每秒分数流，用于现场校准）、`KWS_RESET`；诊断：`KWS_TEST`（全零/全一
  特征过网络，与主机对拍器输出逐微比对，可判定目标机数值是否损坏）、
  `KWS_PEEK`（引擎实际收到的 PCM 峰值 + 最新特征帧抽样，可判定喂数链路）。
- **三个已踩过的坑**（都已修复/自愈）：①麦克风 VDD 借 GPIO3_D0 当 1.8V 电源，
  **重启即断电**——已装 systemd 单元 `kws-micpower.service` 开机拉高；②本核
  先于 Linux 启动，PDM 时钟/引脚配置会被 Linux 启动过程覆盖——`pdm_start()`
  现在每次启动前重断言 CRU/IOC（nuttx 侧补丁），且常听改为 rpmsg 链路建立后
  才开启；③采集线程带 5 秒全零看门狗，输入死寂自动重初始化。
- **验收**：对板说「你好，openvela」（或 `aplay` 播放
  `tools/kws/data/pos_raw/` 任一正样本经喇叭给麦克风听），然后
  `dmesg | tail` 应有 `wake word detected: p=0.9xx`，`KWS_INFO` 的 `det`
  计数 +1，`wake_watch.py` 收到 `KEY_WAKEUP`。

指标（TTS 整句流式评测，板上实测，阈值见 `tools/kws/checkpoints/config.json`）
与训练复现全流程见 [`tools/kws/README.md`](../../tools/kws/README.md)。

## 四、实现历程与关键难点

“Linux 持有 GIC distributor + openvela 作为同构 A 核 slave”在 NuttX/openvela
生态里是首例级别的适配。因板上无 UART5 物理串口可读，全程用
**RAMLOG + Linux devmem dump** 的无串口调试法定位问题。主要攻克：

1. **GICv2 AMP-slave**：nuttx 的 `arm64_gic_initialize` 非 SMP 下必刷 distributor，
   会打乱 Linux 中断配置 → 新增 `CONFIG_ARM64_GIC_SLAVE` 跳过全局初始化。
2. **静态资源表 5 连坑**：`const` 落只读段（openamp 要写）→ 去 const；rptun 卡等
   DRIVER_OK（Linux 不写资源表）→ 预设；CPUNAME 私有特性 DEBUGASSERT + features
   取 dfeatures&gfeatures → 两者都设；`config_len=0` 致 cpuname 读空、绑定失败 →
   设为 sizeof(config)。
3. **握手时序**：openvela 早于 Linux 启动，announce 过早致 Linux vq 为空崩溃 →
   信号量门控，先等 Linux 首个 kick。
4. **协议兼容**：openvela 的 uart_rpmsg 用 NuttX 私有帧协议，与 Linux rpmsg_tty
   的裸字节不通 → 自建裸字节回显端点。
5. **PDM 是 v2 IP**：RK3576 的 PDM 不是经典 Rockchip PDM。按 `rockchip_pdm.h`
   配好后 FIFO 恒空，扫描 0x000–0x1FC 发现只有 4 个寄存器有响应、0x38 读出
   `0x23023576`。原因是设备树的 `rockchip,rk3576-pdm` 只被 2024 年新增的
   `rockchip_pdm_v2.c` 匹配，寄存器映射与老版本除 0x0 外毫无共同点。改用 v2
   映射后一次跑通；驱动启动时校验 0x38 的版本号，避免再次静默失败。
6. **麦克风时钟不能借音频 PLL**：aupll/audio_frac 归 Linux，它一放音就重调，
   会把 slave 的采样率一起拖走。全部时钟取自 24MHz 晶振：v2 的参考表正好有
   `{clk 24000000, clk_out 2400000}` 一项，落在整数 16000 Hz 上。

详见提交历史与 `logs/` 下的 AI Coding 记录。
