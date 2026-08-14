# 离线唤醒词「你好，openvela」— 训练与部署管线

openvela（cpu3, AMP slave）上的**全离线**唤醒词引擎：40 维 log-mel 前端 +
约 1.1 万参数 DS-CNN，纯 C / float32 / 零第三方运行时依赖（仅 libm），
推理常驻小核，检出后经 rpmsg 通知 Linux（input `KEY_WAKEUP` 事件）。
唤醒词按大赛规定为**「你好，openvela」**。

```
PDM 麦克风 ──> cpu3 openvela                                Linux (7 核)
              capture 线程(prio 200) ─┬─> 发送环 ──rpmsg──> ALSA 采集卡
                                      └─> KWS 环 (0.5s)      (snd_rpmsg_mic)
              KWS 线程(prio 90):
                400/160 滑窗 -> FFT512 -> 40 mel -> 归一化
                -> 200x40 特征滑窗(2.0s) -> DS-CNN (每 80ms 一次推理)
                -> 2 次平滑 >= 阈值 -> 2s 不应期
                -> RPMSG_MIC_EVT_WAKE ──rpmsg──> KEY_WAKEUP input 事件
                                                  + dmesg + "EVT WAKE" 文本行
```

## 目录

| 文件 | 作用 |
|---|---|
| `kws_common.py` | **前端数值唯一真源**（帧长/FFT/mel/归一化定义） |
| `gen_data.py` | edge-tts 多音色合成语料（321 正 / 264 负句） |
| `augment_and_cache.py` | 变速/加噪/混响/截断硬负例/音调硬负例 → 特征缓存 |
| `train.py` | DS-CNN 训练（按源片段分组防泄漏切分，早停） |
| `export_c.py` | 折叠 BN，产出 `kws_tables.h` / `kws_model_data.h` / 金标准向量 |
| `host/` | 主机/板上评测器：与 Python 逐帧对拍、整句流式批量评测 |
| `deploy/flash_amp.sh` | nuttx.bin → amp.img → dd 刷板（≈10s 迭代） |
| `deploy/wake_watch.py` | Linux 侧演示：监听 KEY_WAKEUP，可挂任意命令 |
| `deploy/make_ding.py` + `kws-wakesound.service` | 唤醒「叮咚」反馈音（合成 + 开机自启服务，板上已装） |
| `data/` `checkpoints/` | 语料与模型产物（gitignore，可全量重建） |

固件侧引擎源码在 `board/contest_board/src/`：`kws_frontend.c`（FFT+mel）、
`kws_nn.c`（DS-CNN 推理）、`kws_engine.c`（流式判决）、`rk3576_kws.c`
（NuttX 线程/环形缓冲/命令），由 `CONFIG_RK3576_KWS`（默认 y）启用。
生成的两个头文件**已提交**，不重训也能直接编固件。

## 复现全流程

```bash
cd tools/kws

# 0) 依赖（无 sudo：用户级 pip + miniconda 的 make/gcc 见根 README）
python3 -m pip install --user numpy scipy soundfile edge-tts av
python3 -m pip install --user torch --index-url https://download.pytorch.org/whl/cpu

# 1) 语料合成（需联网，~2 分钟；失败可重跑，自动续传）
python3 gen_data.py

# 1b) 可选：板上真麦克风录 5 分钟房间底噪，混入增强
ssh <板> 'arecord -D hw:rpmsgmic,0 -f S16_LE -r 16000 -c 1 -d 300 /tmp/n.wav'
scp <板>:/tmp/n.wav data/room.wav   # 再按 60s 切成 data/noise_raw/room_*.wav

# 1c) 信道自适应（强烈建议）：把语料经板载扬声器→空气→PDM 麦重录一遍。
#     纯 TTS 训练的模型对真实声学信道（小喇叭频响+房间混响+CIC 染色）几乎
#     不识别（实测干净语料 0.99 的短语过信道后仅 0.003）；混入重录语料后
#     模型原生适应该信道。全自动，约 40 分钟：
tar cf - --transform 's|.*/||' data/pos_raw/*.wav data/neg_raw/*.wav \
  | ssh <板> 'mkdir -p ~/rerec_src && tar xf - -C ~/rerec_src'
scp rerecord_on_board.py <板>:~/ && ssh <板> 'nohup python3 ~/rerecord_on_board.py > ~/rerec.log 2>&1 &'
# 完成（rerec.log 出现 DONE）后收回并生成 manifest_rerec.json（augment 自动合并）：
ssh <板> 'tar cf - -C ~/rerec_out .' | tar xf - -C data/rerec/

# 2) 增强 + 特征缓存（~4 分钟，≈4900 个 2.0s 窗）
python3 augment_and_cache.py

# 3) 训练（8 核 CPU ~8 分钟，早停）
python3 train.py

# 4) 导出 C 头文件 + 金标准
python3 export_c.py          # --random 可在无语料时先验证 C 实现

# 5) 数值对拍（拿 SDK 交叉编译器静态编译，在板上跑）
cd host && make && scp kws_host <板>:/tmp/
ssh <板> '/tmp/kws_host golden'      # 三组 PASS: |Δfeat|<5e-3 |Δprob|<2e-3

# 6) 整句流式评测（TTS 语料推上板）
tar cf - -C ../data pos_raw neg_raw | ssh <板> 'mkdir -p /tmp/kws_eval && tar xf - -C /tmp/kws_eval'
ssh <板> '/tmp/kws_host batch /tmp/kws_eval/pos_raw 1 | tail -1'
ssh <板> '/tmp/kws_host batch /tmp/kws_eval/neg_raw 0 | tail -1'

# 7) 编固件 + 刷板（工作区根目录）
./build.sh vendor/openvela/boards/contest2026_290_board/configs/nsh -j$(nproc)
tools/kws/deploy/flash_amp.sh        # 备份→打包→dd→重启

# 8) Linux 侧
insmod snd_rpmsg_mic.ko              # 或替换后 rmmod/insmod
python3 deploy/wake_watch.py --exec 'aplay /usr/share/sounds/ding.wav'
```

## 运行期调试（经 /dev/ttyRPMSG0 文本端点）

```bash
stty -F /dev/ttyRPMSG0 raw -echo
echo KWS_INFO  > /dev/ttyRPMSG0; head -1 /dev/ttyRPMSG0   # 状态/计数/耗时
echo KWS_THR 900 > /dev/ttyRPMSG0                          # 阈值（‰）
echo KWS_SCORE > /dev/ttyRPMSG0                            # 每秒回报分数流
echo KWS_OFF   > /dev/ttyRPMSG0 / KWS_ON                   # 关/开常听
```

检出时 Linux 侧可见三路信号：`dmesg`（`wake word detected: p=0.xxx`）、
`openvela-kws` input 设备的 `KEY_WAKEUP`、以及 tty 端点的 `EVT WAKE` 文本行
（仅当 tty 未被用作 PCM 流时）。

## 当前指标（交付版：CMN + 信道自适应模型，KickPi K7 板上实测）

- **数值对拍**：C 引擎 vs torch/numpy，`|Δfeat| < 7e-5`、`|Δprob| < 3e-6`
  （aarch64 实机，-O3 -ffast-math 编译）
- **真实声学信道整句流式**（语料经板载扬声器→空气→PDM 麦重录后全速流放）：
  θ=0.85 → 召回 89.4% / 负句误触发 4.7%；θ=0.95 → 78.5% / 1.6%；
  出厂阈值 0.90（`KWS_THR` 可运行期调整）
- **推理开销**：单次 53ms @ 2.0GHz A53（80ms 周期，约 60% 单核占用）
- 口径说明：评测句子的增强变体参与过训练，绝对数字偏乐观；对完全陌生
  说话人的表现见上方 TODO 的提升路径。历史教训（纯 TTS 训练在真信道上
  召回为 0、无 CMN 时误报 67%）详见设计要点。

## 设计要点

- **数值一致性是硬约束**：C 与 Python 前端同为 float32、同一套查表
  （Hann/旋转因子/mel 权重/归一化均由 `export_c.py` 从 `kws_common.py`
  生成），金标准测试卡 5e-3/2e-3 容差，改任何常量必须重训重导出。
- **判决链路**：120ms 一次推理 → 最近 3 次平均 → 阈值 → 2s 不应期；
  阈值出厂值来自训练时的验证集扫描（`checkpoints/config.json`）。
- **硬负例**：截断唤醒词（防半句触发）、「你好」系近音句、纯音/扫频/
  警报（防音调误触发）、真实房噪床。
- **算力/内存**（cpu3 A53 实测）：单次推理数 ms 级（`KWS_INFO` 的 `us`
  字段），CPU 占用 ~10%；权重+表 ~200KB rodata、激活 576KB bss，8MB
  分区内绰绰有余。
- **信道自适应是必需品而非可选项**：纯 TTS 模型在真实「扬声器→空气→
  PDM 麦」信道上近乎失聪（0.99 → 0.003）；`rerecord_on_board.py` 重录
  语料混入训练后模型原生适应（重录正样本用 FFT 互相关按原始 TTS 边界
  切割——低信噪比下能量裁剪会把整段 4s 塞给变速压缩，训成「加速语音」）。
  TTS 版与重录版同句共用 split group，不会互相泄漏。特征做**窗内均值
  归一（CMN）**消掉信道/电平维度（无 CMN 时模型学「糊的中文≈唤醒」捷径，
  真信道误报 67%→4.7%）。

## TODO：准确率提升（暂缓，先交付可用版）

当前板上真信道实测：θ=0.85 → 召回 89.4% / 误报 4.7%；θ=0.95 → 78.5% / 1.6%；
出厂 θ=0.90。备好的提升路径（按性价比排序）：

1. **均衡信道负例重训**：`data/rerec/` 已含 321 正 + 768 负全量真信道语料，
   round-9 特征缓存（12565 窗）已生成——直接 `python3 train.py` 即可。
2. **真人样本**：录若干真人「你好，openvela」（近/远场、快/慢），命名进
   `data/rerec/` 并补 `manifest_rerec.json` 条目即可参训。
3. **模型加宽** ch 48→56：容量 +36%，推理约 53→70ms（80ms 周期内仍够）。
4. **PDM 硬件增益**：驱动 CIC scale 可再抬，改善入口信噪比。
5. **现场校准**：`KWS_SCORE` 流实测环境分数分布后用 `KWS_THR` 定档。
