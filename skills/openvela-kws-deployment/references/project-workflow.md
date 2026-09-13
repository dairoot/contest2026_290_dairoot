# KWS 项目执行参考

对应比赛仓 `4f5d6d6` 的代码基线，整理日期 2026-09-13。路径相对项目根；后续使用先核对实际代码。所列脚本随项目提供，不随独立 Skill 自动复制。

## 已有素材与前置

- `tools/kws/README.md`：数据与历次模型实验；其中部分参数和 TODO 已过时。
- `tools/kws/models/nihao_openvela_v7/`：历史权重、配置及评测 JSON。该目录不等于 `checkpoints/`，导出脚本默认读取后者。
- `board/contest_board/src/kws_tables.h`、`kws_model_data.h` 及 `tools/kws/host/kws_golden.h` 已入仓，复用当前固件不需要重训。
- 重训依赖 Python、NumPy、SciPy、SoundFile、PyTorch；生成 TTS 还需 edge-tts/网络或 macOS `say`。`cut_takes.py` 另依赖本地 ASR 环境；先读脚本导入，不把模型转换环境混进训练环境。
- `export_c.py` 默认同时要求 `checkpoints/model_best.pt`、`checkpoints/config.json` 和 `data/golden.npz`。仅有存档 `.pt` 不足以完成默认导出。

先把 `CONTEST_ROOT` 设置为实际项目绝对路径；调用硬件步骤前设置实际 `BOARD_TARGET`，不要复用脚本的历史默认 IP。

## 数据准备与训练

在训练机从项目根进入：

```bash
cd "$CONTEST_ROOT/tools/kws"
python3 gen_data.py
# macOS 可追加离线音色：python3 gen_data_say.py
```

真实板麦采样示例（交互录制，需要设备可达）：

```bash
python3 record_real.py pos --n 30 --speaker s0 --board "$BOARD_TARGET"
python3 record_real.py neg --n 20 --speaker s0 --board "$BOARD_TARGET"
mkdir -p data/real_mic
python3 mk_manifest_real.py
```

30/20 条是启动样本量，不是充足性证明。更换说话人时更换 `--speaker`；`mk_manifest_real.py` 的日志导入会把既有 ASR 片段当作负例，先确认里面没有唤醒词。

需要声学重录时使用 `rerecord_on_board.py`，但它固定读取板上 `~/rerec_src/`、写 `~/rerec_out/`，使用 `rockchipes8388` 播放卡与 `rpmsgmic` 录音卡，并调整播放音量。先核对声卡名与音量，再传入原始 WAV、在板上运行、收回到 `data/rerec/` 并执行 `mk_manifest_rerec.py`。仅运行 manifest 脚本不会采集音频。

训练主链：

```bash
python3 augment_and_cache.py
python3 train.py
python3 eval_suite.py --tag candidate
python3 export_c.py
```

运行前决定候选模型保存位置，避免覆盖需要保留的 checkpoint。`run_pipeline.sh` 只串联 TTS、增强、训练、导出，不包含真人录制、完整评测或部署验收。

数据切分检查：

1. `augment_and_cache.py:clip_group` 按原片段名归组，去掉重录名前缀；`train.py` 与 `eval_suite.py` 使用一致的组哈希规则。
2. `mk_manifest_real.py` 对日志按日内时间留出，对真人录音另有逐条留出规则；这不保证说话人独立或整段录音独立。需要相应泛化结论时，改为按说话人/原录音分组。
3. 当前 `augment_and_cache.py` 在分组切分前对全缓存求 `mean/std`，验证组会影响统计。严格训练/测试流程应先按组切分，再只用训练组拟合统计并重导出；已有历史结果须标明此限制。
4. 原始数据、缓存、归一化参数、checkpoint 和生成文件绑定同一版本；改变切分不能继续使用旧统计。

## 当前数值与判决契约

执行常量是 16 kHz、帧长 400、帧移 160、FFT 512、40 mel、200 帧特征窗（32240 个 PCM 样本）。源码头部仍有 150 帧旧注释，以赋值为准。

`kws.h` 当前为每 8 帧推理、3 次平滑、200 帧不应期、0.50 重武装阈值、1000 帧启动静默期。`host/Makefile` 用 `KWS_WARMUP_FRAMES=0` 禁用静默期以评测短片段；这次对拍不覆盖真实启动抑制逻辑。

阈值来自 `checkpoints/config.json` 并写入生成表。`eval_suite.py` 目前比较 0.85/0.90/0.95；测试其他阈值要同步扩展评测口径，不能只改部署值。

## C 对拍

训练机有原生 C 编译器时：

```bash
cd "$CONTEST_ROOT/tools/kws/host"
make clean
make CC=cc STATIC=
./kws_host golden
```

`make` 默认是作者 SDK 的交叉编译器路径，必须显式覆盖。上板对拍另用目标架构编译器，`CROSS_CC` 为其实际路径：

```bash
make clean
make CC="$CROSS_CC" STATIC=-static
scp kws_host "$BOARD_TARGET:/tmp/kws_host"
ssh "$BOARD_TARGET" '/tmp/kws_host golden'
```

工具链没有静态 C 库时改用动态链接并核对板上 ABI/库；不要把开发机原生二进制发到 ARM 板。

`golden` 返回非零代表失败；正常训练导出有正例、负例、合成信号三组。每组需 `max feature error < 5e-3` 且 `probability error < 2e-3`。它分别检查 C 前端特征和“Python 参考特征 → C 网络”的概率，不是完整 C 流式链路测试。随后用 `kws_host batch <wav目录> <0或1> [阈值]` 跑独立正负集合。金标准用于验证实现一致，不用于证明识别准确率。

本次整理在 macOS 主机用入仓的生成文件完成原生 C 对拍，3/3 通过：最大特征误差 `2.24e-5`、最大概率误差 `2.05e-8`。这是 2026-09-13 的本机验证，不替代重新导出后的对拍或目标板验收。

## 构建与部署

openvela 工作区记为 `OPENVELA_WS`，已应用本项目 nuttx 补丁并具备板配置映射。构建示例：

```bash
cd "$OPENVELA_WS"
./build.sh vendor/openvela/boards/contest2026_290_board/configs/nsh -j8
```

仅在构建命令成功、输出为新固件时继续。核对 ELF/map、文件时间与固件哈希；禁止用目录中遗留的 `nuttx.bin` 掩盖失败。

现有部署脚本可以作为参考，但有以下约束：

| 脚本 | 使用前需要处理的限制 |
| --- | --- |
| `deploy/flash_amp.sh` | 第一个参数是目标板，`SDK` 可覆盖；工作区假设是仓库上级；临时备份在板上 `/tmp`，刷写前另存到持久目录或主机 |
| `deploy/flash_amp_relay.sh` | 参数可指定构建机和板子，但远端工作区、仓名、SDK 路径仍写死在脚本里 |
| `deploy/redeploy.sh` | 虽可覆盖 `BUILD/BOARD/RBUILD`，后续 relay 不继承所有路径；远端构建串行命令未严格传播 `build.sh` 失败，后面的 `tail/ls` 可能掩盖失败 |

因此默认按“导出 → 两端对拍 → 单独构建并检查退出码 → 打包 → 备份 → 烧录 → 重启验收”分阶段执行。若使用一键脚本，先在任务范围内修正这些问题，再复核命令。不要仅设置环境变量就把旧脚本视为通用部署器。

AMP 镜像只写已存在且容量足够的 `/dev/disk/by-partlabel/amp`；首次整包适配参考 `board/contest_board/README.md`。异常退出时先读构建/烧录日志并核对板状态，不循环重刷。

## 验收与结果记录

当前固件默认启用 PDM，板级 README 的默认 I2S 描述是旧状态。实测时读当前 defconfig 和 `MIC_INFO`。TTY 文本端点需要 raw 模式、读取超时，并避免与 PCM 读者抢数据。

记录 `KWS_INFO` 的 `thr/us/drop/det`、`KWS_PEEK` 的实际 PCM/特征、`KWS_TEST` 的网络常量输入结果。分别排查采集、数值、分类、流式事件与 RPMsg 出口。

输出至少带：代码/模型版本、数据划分、每个集合样本数、阈值与判决常量、真人召回、负句误触、连续背景音总时长与事件数、对拍误差、真机推理耗时及丢样数。历史 v7i 指标偏向作者读法，不能作为其他说话人或新板型的验收结果。
