# KickPi K7 (RK3576) 上的 Linux + openvela 异构多核（AMP）适配

> 2026 首届 openvela AI 硬件开发者大赛 · **新硬件适配赛道** · 290 队

## 一、作品简介

把 **openvela** 移植到 **KickPi K7（Rockchip RK3576）** 开发板，并做成一个真实的
**AMP（非对称多处理）系统**：在一颗 SoC 上，让 openvela 独占一个 A 核实时运行，
与 Linux 并存共跑。

- **cpu3（Cortex-A53）独占运行 openvela**（NuttShell 交互、实时任务）
- 其余 **7 核（3×A53 + 4×A72）继续运行 Linux**
- 两个操作系统通过**共享内存 + GIC 软中断承载的 RPMsg** 双向通信，Linux 侧表现为
  标准的 `/dev/ttyRPMSG0` 字符设备
- **openvela 侧全离线唤醒词「你好，openvela」**：PDM 麦克风常听，40 维
  log-mel + DS-CNN（纯 C，仅依赖 libm）在小核上每 80ms 推理一次（实测单次
  53ms），检出后经 rpmsg 通知 Linux（`KEY_WAKEUP` input 事件）——大核可睡、
  小核常听的 AMP 语音入口。训练语料经板载扬声器→真实 PDM 麦重录做信道
  自适应，板麦录到的真人对话/底噪作负例参训（v7：真人语音误触 8%→0.4%，
  底噪 120 次/小时→0）。训练/评测/部署全管线见 [`tools/kws/`](tools/kws/)
- **Linux 侧接力做识别**：唤醒之后，Linux 的 7 个核与 **6 TOPS NPU** 承担实时语音
  识别（SenseVoiceSmall）与说话人辨认（ERes2NetV2），两个模型都转成 RKNN 跑在 NPU
  上——声纹 4589 ms → **519 ms**，识别 702 ms → **378 ms**，端到端「说完到出结果」
  约 620 ms。小核常听、大核 + NPU 出结果，见 [`linux-apps/asr-server/`](linux-apps/asr-server/)

**已上板实测通过**：openvela 在 cpu3 稳定运行（心跳精确 500ms）、Linux 7 核不受
影响、`/dev/ttyRPMSG0` 双向回显 50/50 零丢失、openvela NuttShell 在 UART5 可交互。

这是 openvela/NuttX 生态里**首个** “Linux 持有 GIC distributor + openvela 作为同构
A 核 AMP slave” 的公开适配。

## 二、选题方向

**新硬件适配** —— 为 openvela 新增 RK3576 芯片支持与 KickPi K7 板级支持，并突破
“与 Linux 共享 GIC / 与 Linux 主控 RPMsg 互通” 两大工程难点。核心价值在于：同一颗
RK3576 上，用一套硬件同时获得 **Linux 的丰富生态** 与 **openvela 的实时确定性**，
省一颗 MCU、省一套外围电路。

## 三、目录结构

```
contest2026_290_dairoot/
├── README.md                       本文（作品说明 + 复现导航）
├── contest2026_290_dairoot.xml     repo manifest（board 注入编译树）
├── board/contest_board/            ★ 主交付：openvela 板级适配
│   ├── README.md                   技术设计 + 详细复现步骤
│   ├── configs/nsh/defconfig       板级 defconfig
│   ├── src/                        board 启动、AMP 握手、心跳、rpmsg 回显
│   ├── scripts/, include/, Kconfig
│   └── linux-side/                 Linux 侧配套文件（DTS/its/分区/defconfig）
├── nuttx-side/                     ★ nuttx 公共仓侧的 RK3576 芯片层补丁（git am）
├── tools/kws/                      ★ 离线唤醒词：训练→导出→对拍→评测→烧写全管线
├── linux-apps/                     ★ Linux 侧用户态服务（识别模型都跑 RK3576 NPU）
│   ├── asr-server/                 语音识别 + 声纹（SenseVoice / ERes2NetV2）
│   └── miloco-server/              摄像头视频流 VPU 硬解 + yolo11n 检测 + 米家设备开关
└── logs/                           AI Coding 日志
```

openvela **nuttx 公共仓**内新增的 RK3576 芯片层（`arch/arm64/src/rk3576`）与 GICv2
AMP-slave 补丁（`CONFIG_ARM64_GIC_SLAVE`）照 rk3588/rk3399 模板实现，因不能经
manifest `<linkfile>` 注入，以 patch 系列放在 [`nuttx-side/`](nuttx-side/)（6 个
提交、21 文件、2261 行），在 manifest 所指的 nuttx 基线上 `git am` 可直接应用。

## 四、运行方式（复现）

**完整、可照做的复现步骤在 [`board/contest_board/README.md`](board/contest_board/README.md)。** 概览：

0. **（可选）不编译，直接烧预编译固件** —— `update.img` 首刷 / `amp.img` 迭代，
   下载与说明见 board README §三.0。
1. **编译 openvela 固件**
   ```bash
   # 先给公共仓 nuttx 打上 RK3576 芯片层补丁
   (cd nuttx && git am ../contest2026_290_dairoot/nuttx-side/*.patch)
   ./build.sh vendor/openvela/boards/contest2026_290_board/configs/nsh -j$(nproc)
   # 产物 nuttx/nuttx.bin，入口 0x41800000
   ```
2. **在 KickPi Linux SDK 上叠加 Linux 侧改动**（`board/contest_board/linux-side/`
   的 DTS / its / 分区表 / defconfig；这些与具体 RTOS 无关，一次性叠加即可）。
3. **打包烧写**：把 `nuttx.bin` 作为 amp 分区固件打进 `update.img` 烧录；后续只迭代
   openvela 时可只 `dd` 刷 amp 分区（约 10 秒）。
4. **验收**（Linux 终端）：
   ```bash
   cat /proc/device-tree/model            # ...KICKPI K7 Board (AMP)
   nproc                                   # 7
   sudo busybox devmem 0x47c00004          # 0x3  = openvela 心跳版本
   echo hello > /dev/ttyRPMSG0             # openvela 回显
   cat  /dev/ttyRPMSG0                     # -> hello
   # 离线唤醒（对板说「你好，openvela」，或 aplay 播放正样本）
   echo KWS_INFO > /dev/ttyRPMSG0 && head -1 /dev/ttyRPMSG0   # 引擎状态
   dmesg | grep 'wake word'                # snd_rpmsg_mic: p=0.9xx
   sudo python3 tools/kws/deploy/wake_watch.py                # KEY_WAKEUP 事件
   # 唤醒之后的识别服务（默认就走 NPU）
   cd linux-apps/asr-server && uv sync && uv run python tests/asr_ws/server.py
   # 浏览器打开 http://<板子IP>:8086/ 说话，看识别结果与说话人编号
   ```

## 五、关键技术难点（详见 board README 与提交历史）

| 难点 | 解决 |
|---|---|
| GICv2 被 Linux 独占 distributor | 新增 `CONFIG_ARM64_GIC_SLAVE`，openvela 不初始化 distributor，只碰自有中断位 |
| OpenAMP 静态资源表与 Rockchip 主控互通 | 逐一攻克 const 只读段 / 预设 DRIVER_OK / CPUNAME 特性 / config_len 等 5 处坑 |
| openvela 早于 Linux 启动的握手时序 | 信号量门控，先等 Linux 首个 kick（vring 就绪）再 announce |
| uart_rpmsg 私有帧协议与 Linux rpmsg_tty 裸字节不兼容 | 自建裸字节回显端点，wire-compatible |
| 板上无串口可读 | 发明 “RAMLOG + Linux devmem dump” 无串口调试法定位全部问题 |
| openvela 开源版无离线唤醒引擎（media_trigger 仅留接口） | 自研纯 C log-mel+DS-CNN 引擎：Python 训练管线与 C 实现同源查表、板上金标准对拍（&#124;Δprob&#124;<1e-7），常听于 cpu3，唤醒事件走 rpmsg 变成 Linux input 事件 |
| RKNN 不支持动态 shape，而语音长度天然可变 | 定长窗口导出 + 按帧对齐切段：窗口大小直接等于耗时（5 s 窗恒定 390 ms、10 s 窗 655 ms，选大了短句反而打不过 CPU）；长句最后一窗向前对齐成满窗、重叠部分按帧丢弃，不漏字不重复 |
| 3.8 GB 内存跑不动 936 MB fp32 权重 | 本地 ASR 全面转 int8 ONNX / RKNN fp16，加载峰值 2993 MB → 1300 MB，常驻 2.0 GB |

## 六、AI Coding 使用说明

本作品的**全过程**（调研 → 芯片层移植 → GICv2 AMP-slave 补丁 → OpenAMP 资源表
逐坑调试 → 无串口内存诊断 → 协议兼容 → 交付清理）均由 **Claude Code** 结对完成：
AI 负责阅读 openvela/NuttX 源码、编写与迭代所有 C 代码、通过 ssh+devmem 远程读板载
内存做无串口诊断、逐轮定位并修复 7 个关键 bug 直至上板双向通信成功。完整对话与工具
调用记录见 [`logs/`](logs/)。

> 提交前请把 `logs/your-github-login/` 替换为你的真实 GitHub 登录名目录，并按
> 《AI Coding 日志归集与提交手册》导出本次 Claude Code 会话的 `.jsonl` 到该目录。
