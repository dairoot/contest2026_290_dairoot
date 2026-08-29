# 离线唤醒词「你好，openvela」— 训练与部署管线

openvela（cpu3, AMP slave）上的**全离线**唤醒词引擎：40 维 log-mel 前端 +
约 1.4 万参数 DS-CNN，纯 C / float32 / 零第三方运行时依赖（仅 libm），
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
| `gen_data.py` | edge-tts 14 音色合成语料：11 种唤醒词写法（含欧朋维拉等中文读法）/ 111 负句 |
| `gen_data_say.py` | macOS 自带 19 个中文音色补充语料（离线，`say`） |
| `mk_manifest_real.py` | 真人语料 → `manifest_real.json`：asr-server 落盘的板麦片段（负例）+ `record_real.py --board` 录的样本；按时间段留 30% 做测试集 |
| `record_real.py` | 录真人样本：Mac 麦，或 `--board` 直接用板载 PDM 麦（逐条提示） |
| `cut_takes.py` | 板麦整段自由录音 → 能量切段 → 本地 SenseVoice 转写 → 按文本自动打标 → real_pdm 正/负样本 |
| `augment_and_cache.py` | 变速/停顿扰动/长句头部裁剪/加噪(真实底噪)/混响/截断与你好类硬负例/音调硬负例 → 特征缓存 |
| `train.py` | DS-CNN 训练（分组防泄漏切分、SpecAugment、早停；MPS/CPU 自动） |
| `mk_manifest_rerec.py` | 从 manifest*.json 推导 data/rerec/ 的 manifest_rerec.json |
| `score_stream.py` | 任意 wav 过流式判决链打分（峰值平滑分数，与板上逐位一致） |
| `eval_suite.py` | **统一评测台**：hash 留出集（TTS/say/重录）+ 真人负例 + 底噪误唤醒次/小时 |
| `export_c.py` | 折叠 BN，产出 `kws_tables.h` / `kws_model_data.h` / 金标准向量 |
| `host/` | 主机/板上评测器：与 Python 逐帧对拍、整句流式批量评测 |
| `deploy/flash_amp.sh` | nuttx.bin → amp.img → dd 刷板（构建机能直连板子时，≈10s 迭代） |
| `deploy/flash_amp_relay.sh` | 同上，但构建机够不到板子时经本机中转 |
| `deploy/redeploy.sh` | 一条命令：导出 → ubt 交叉编 kws_host → 板上 golden 对拍 → ubt 编固件 → 中转刷板 |
| `deploy/wake_watch.py` | Linux 侧演示：监听 KEY_WAKEUP，可挂任意命令 |
| `deploy/make_ding.py` + `kws-wakesound.service` | 唤醒「叮咚」反馈音（合成 + 开机自启服务，板上已装） |
| `deploy/kws-cpufreq.service` | 小核簇 min freq 1.8GHz（DVFS 会拖垮实时预算，板上已装） |
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

# 1) 语料合成（需能连 speech.platform.bing.com，~5 分钟；失败可重跑，自动续传。
#    本机连不上时把脚本放到能连的机器上跑，用空文件占位已有 clip，再
#    rsync --min-size=1 拉回，最后本地再跑一次 gen_data.py 只重建 manifest）
python3 gen_data.py
python3 gen_data_say.py      # macOS 自带音色，离线，~5 分钟

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
# 完成（rerec.log 出现 DONE）后收回，生成 manifest_rerec.json（augment 自动合并）：
mkdir -p data/rerec && ssh <板> 'tar cf - -C ~/rerec_out .' | tar xf - -C data/rerec/
python3 mk_manifest_rerec.py
#     macOS 往板子推文件时用 COPYFILE_DISABLE=1 tar ...，否则会带上 ._* 垃圾文件。

# 1d) 真人语料（v7 起是必需品，见设计要点）。asr-server 落盘的板麦片段是
#     现成的真负例（linux-apps/asr-server/audio_logs/，板上还有一份）；
#     真人唤醒词用板麦录几十条（近/远/快/慢/轻/响 轮转，回车录一条）：
python3 record_real.py pos --n 30 --board kickpi@<板IP>
python3 record_real.py neg --n 20 --board kickpi@<板IP>     # 同一个人的非唤醒句
#     或者更省事：板上整段自由录音，人对着板子说十几遍唤醒词 + 几句别的话，
#     cut_takes.py 用本地 SenseVoice 转写后按文本自动分正/负（表格里 ? 的行
#     用 --pos/--neg/--drop 手工定；每遍之间至少停 0.7 s，否则会粘成一段）：
ssh <板> 'arecord -q -D hw:rpmsgmic,0 -f S16_LE -r 16000 -c 1 -d 240 ~/take.wav'
scp <板>:take.wav data/real_pdm/raw/take5.wav
python3 cut_takes.py data/real_pdm/raw/take5.wav --dry     # 先看表
python3 cut_takes.py data/real_pdm/raw/take5.wav --drop 0,7
python3 mk_manifest_real.py          # 去重；每天最后 30%（时间段）留作测试集；
                                     # real_pdm 每 3 条留 1 条 → eval_suite 的 human-pos

# 1e) 板麦录 3 分钟房间底噪（增强用真实底噪床；*_test.wav 留给评测）
ssh <板> 'arecord -D hw:rpmsgmic,0 -f S16_LE -r 16000 -c 1 -d 180 /tmp/room.wav'
scp <板>:/tmp/room.wav data/noise_raw/room_train.wav     # 再切 60s 存成 room_test.wav

# 2) 增强 + 特征缓存（~8 分钟，≈2.4 万个 2.0s 窗）
python3 augment_and_cache.py

# 3) 训练（MPS ~12 分钟，早停）
python3 train.py

# 3b) 统一评测（每次改 recipe 都跑，和 data/eval_v6.json 等历史结果对比）
python3 eval_suite.py --tag v7

# 4) 导出 C 头文件 + 金标准
python3 export_c.py          # --random 可在无语料时先验证 C 实现

# 5) 数值对拍（拿 SDK 交叉编译器静态编译，在板上跑；Makefile 里 CC 已指向
#    SDK 的 aarch64 gcc，别的机器用 make CC=... 覆盖）
cd host && make && scp kws_host <板>:/tmp/
ssh <板> '/tmp/kws_host golden'      # 三组 PASS: |Δfeat|<5e-3 |Δprob|<2e-3

# 6) 整句流式评测：推 held-out 的重录语料和真人测试集上板（干净 TTS 里有
#    数字静音，不代表板上条件，用 eval_suite.py 评）
COPYFILE_DISABLE=1 tar cf - -C ../data val_rerec_pos val_rerec_neg | ssh <板> 'mkdir -p /tmp/kws_eval && tar xf - -C /tmp/kws_eval'
ssh <板> '/tmp/kws_host batch /tmp/kws_eval/val_rerec_pos 1 | tail -1'
ssh <板> '/tmp/kws_host batch /tmp/kws_eval/val_rerec_neg 0 | tail -1'

# 7) 编固件 + 刷板（工作区根目录）
./build.sh vendor/openvela/boards/contest2026_290_board/configs/nsh -j$(nproc)
tools/kws/deploy/flash_amp.sh        # 备份→打包→dd→重启
tools/kws/deploy/flash_amp_relay.sh  # 构建机够不到板子时在本机跑这个
# 重训后的整套上板（4–8 步合一，golden 不过不刷）：
tools/kws/deploy/redeploy.sh 0.85

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

## 当前指标（v7：v6 recipe + 真人负例 + 中文读法/say 音色 + 长句/停顿增强，2026-08-29）

`eval_suite.py` 统一口径，全部是训练没见过的样本，θ=0.85（v7 出厂值）
对比 v6@0.90（v6 出厂值）；`models/nihao_openvela_v7/eval_v*.json` 存档：

| 集合（n） | 含义 | v6 @0.90 | **v7 @0.85** | v7 @0.90 |
|---|---|---|---|---|
| real-neg (226) | 板麦录的真人对话/电视，误触 | 8.0% | **0.4%** | 0.0% |
| room-noise (60 s) | 板麦房间底噪，误唤醒次/小时 | 120/h | **0/h** | 0/h |
| tts-neg (90) / tts-neg-x (17) | TTS 日常句 / 你好类混淆句，误触 | 4.4% / — | **0% / 0%** | 0% / 0% |
| rerec-neg (v6 时 90 → 114) | 经板载喇叭重录的负句，误触 | 1.1% | **0.9%** | 0.9% |
| rerec-pos 原 43 条 | 经板载喇叭重录的「你好，openvela」，召回 | 88.4% | **88.4%** | 83.7% |
| rerec-pos 中文读法 32 条 | 重录的「你好，欧朋维拉」等，召回 | 37.5% | **81.2%** | 78.1% |
| tts-pos (43) / tts-pos-x (32) | 干净 TTS 英文读法 / 中文读法，召回 | 95.3% / — | **93.0% / 100%** | 93.0% / 96.9% |
| say-pos (26) | macOS 音色，召回 | — | **100%** | 100% |

- **板上 C 引擎复核**（`kws_host batch`，同一批 held-out 重录 43/90）：
  θ=0.85 → 86.0% / 1.1%；θ=0.90 → 83.7% / 1.1%；真人测试集 226 条
  θ=0.85 误触 1 条（0.4%，「你好你好喂喂喂」p=0.935）。数值对拍
  `|Δfeat| < 5e-6`、`|Δprob| < 1e-6`，3/3 PASS。
- **刷板后端到端**（板载喇叭播放→PDM 麦→cpu3 引擎→dmesg）：英文读法
  p=0.897、「你好，欧朋维拉」0.922、macOS 音色 0.930、「你好，open维拉」
  0.909 全部唤醒，「你好。」不触发；`KWS_INFO` thr=0.850、单次 70 ms、
  drop=0。
- 取舍：v7 在原 43 条重录上比 v6 少 1–2 条召回，换来真人语音误触
  8%→0.4%、底噪 120/h→0、中文读法召回 37%→81%。出厂阈值 0.85 偏向召回；
  现场误触多就 `KWS_THR 900`（real-neg 0%、say 音色你好类硬负例 13%→4%）。
- **v7i（板上当前版本，`models/nihao_openvela_v7/`）= v7b + 作者本人
  55 条板麦唤醒词（7 段自由录音经 `cut_takes.py`，37 训 / 18 留出，每条
  24 窗）+ 本人 56 句非唤醒语音 / 单说的 openvela / 敲击声 + 380 个本人
  房间底噪窗 + 5 分钟远处谈话（负例，8×/3×）+ 变速上限 1.35×，真人负例
  混噪 SNR 0–30 dB**。本人留出 18 条召回 83%@0.85（v7g 61%、v7b 38%），
  本人非唤醒语音 0/56、远处谈话测试集 0 次/小时（v7g 12 次/小时：真人
  负例只在 ≥10 dB SNR 混噪，模型学成「很轻的说话声=唤醒」）；real-neg
  1.1%、tts-neg 1.1%。代价：rerec-pos 101 条 71%、你好类混淆句 3/17、
  rerec-neg 5.3%、edge-tts 英文读法经喇叭回放只有 0.5–0.7——1.4 万参数
  正被真人数据拉偏，模型已明显偏向作者的读法（vi拉）。板上实测：v7i 各轮
  18–19 遍命中 8–16 遍，逐轮波动大，没中的全是 -43~-46 dBFS 的轻声/快速
  说法；随口说话与远处谈话 0 误触；加滞回后无双响。多录几批、换个人再
  录（`data/real_pdm/` 已有 9 段录音 72 条），`cut_takes.py` +
  `deploy/redeploy.sh` 一轮 40 分钟。
- 中间版本教训：v7d/v7e 只加本人正样本（×48）→ 本人底噪签名和嗓音成了
  唤醒线索，说完一遍后窗口只剩底噪也停在 0.85 以上，每 2 s 不应期一过就
  再触发；v7f 存盘时上下文只留 0.1 s，轻声的「你」被能量门切掉，正样本
  退化成单说 openvela 与负例冲突。

### v6 口径（2026-08-15，留作对照）

- **真实声学信道整句流式（held-out）**：43 正/90 负句按组哈希与训练严格
  隔离，语料经板载扬声器→空气→PDM 麦重录后全速流放：θ=0.85 → 召回
  90.7% / 误触 3.3%；θ=0.90 → 88.4% / 1.1%；θ=0.95 → 86.0% / 0%。
  同口径旧 recipe（ch48 无 SpecAugment）θ=0.95 仅 67.4%/1.1%。但 v6 的
  负例全是 TTS：真人语音误触 8–16%、房间底噪 120 次/小时（见设计要点）。
- **推理开销**：cpu3 实测 66ms @ 2.0GHz（`KWS_INFO` 的 us 字段）。注意
  cpu3 与 Linux 小核同簇同 PLL：schedutil 空闲降到 1.416GHz 时单次涨到
  123ms > 80ms 周期，引擎丢 ~30% 音频（`drop` 计数持续增长）。
  `deploy/kws-cpufreq.service`（板上已装并 enable）把小核簇 min freq 钉在
  1.8GHz，空闲态 66ms 稳定零丢帧——常听场景空闲即主态，此服务是必需品。
- v5 交付版口径（全集评测，训练见过其增强变体，偏乐观）：θ=0.85 →
  89.4%/4.7%。历史教训（纯 TTS 训练真信道召回≈0、无 CMN 误报 67%）
  详见设计要点。

## 设计要点

- **数值一致性是硬约束**：C 与 Python 前端同为 float32、同一套查表
  （Hann/旋转因子/mel 权重/归一化均由 `export_c.py` 从 `kws_common.py`
  生成），金标准测试卡 5e-3/2e-3 容差，改任何常量必须重训重导出。
- **判决链路**：80ms 一次推理 → 最近 3 次平均 → 阈值 → 2s 不应期 →
  **滞回重武装**（触发后要等平滑分回落到 0.5 以下才能再触发，
  `KWS_REARM_THRESHOLD`）。常量见 kws.h，120ms×3 曾实测拖垮召回；滞回是
  v7 加的：真人说完后分数常在 0.85 以上拖 3–4 s，2 s 不应期一过就再响一次
  （板上回归文件「一遍+5 s 底噪」3 次→1 次）。用数据修（把「已说完 0.9–1.8 s」
  的窗当负例，v7j）会让真人召回掉 20 个百分点，弃。阈值出厂值来自流式
  评测台，不是窗级 sweep（`checkpoints/config.json`）。
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
- **真人/真环境负例也是必需品（v7 教训）**：v6 的负例全是 TTS，在板麦录的
  真人对话/电视声上误唤醒 8–16%、纯房间底噪 120 次/小时，而它在 TTS 留出
  集上只有 1%——域差不在音色（VTLP 探针 α 0.85–1.15 召回不掉），而在
  真人韵律/远场/背景。把 asr-server 落盘的板麦片段当负例参训后，真人测试
  集误触降到 0%。语料必须按**时间段**切测试集（同一分钟的对话互相泄漏）。
- **窗内不能有数字静音**：真实麦克风流永远有底噪，log(1e-6) 的零帧会把
  CMN 拉偏，是板上不可能出现的输入。v7 每个训练窗都铺噪声床（45% 用板麦
  真实底噪），代价是评测干净 TTS 时也得垫一层 -48 dBFS 底噪（`eval_suite.py`
  已内置，`host_main.c` 的文件尾也改成伪随机底噪）——否则干净 TTS 召回会
  假性掉到 40%，而那不是板上的条件。
- **慢句要靠头部裁剪**：真人说「你好，欧朋维拉」常超过 2.0 s 的窗（中文
  读法 TTS 平均 2.1 s），板上永远看不到整句。训练时超长句一半保持原速、
  只裁头（≤0.5 s，「好，欧朋维拉」也算正）、一半整体缩放到 1.6–1.95 s；
  相应地「欧朋维拉」单独不再当负例（`NEG_SKIP`），但截断句「你好，欧朋」
  每条正样本切两个当硬负例，你好开头/含维拉的负句变体翻倍。
- **停顿扰动**：edge-tts 逗号永远 0.2 s，真人 0–0.5 s 不等；`vary_pause`
  在你好与 openvela 之间随机增删 -0.15~+0.35 s（填底噪不填零）。
- **真人正样本才是最后一公里**：v7（只有真人负例）在作者本人的板麦录音上
  近处 0.45–0.95、远处 0.5–0.8，只有 2/17 过 0.85——真人负例把「真人说话」
  整体推向了负例；v6 近处反而 0.95+（远处同样 0.5–0.7）。板麦收到的人声只有
  -40~-47 dBFS（底噪 -53，93% 能量在 300 Hz 以下的低频嗡，CIC 已 24 dB，
  抬增益无益），信噪比 7–12 dB。真人的 vela 读成 /vɪlə/「vi拉」，TTS 没有
  这种读法。26 条本人录音以 48 倍窗参训后见指标表。
- **同行调研（2026-08-29，465 个 contest2026 仓库 README）**：428 个还是
  模板；没有任何队伍做出端侧「你好，openvela」模型——130 队（ESP32-S3-EYE）
  用火山云 ASR 转写做文本闸门，430 队写明「唤醒词模型未训练，VAD 兜底」，
  271 队（R528）TFLite Micro KWS 但不提交权重/现场录音，113 队的端侧指令
  模型「只面向已采集的说话人」。本仓的 ASR 服务（NPU 上的 SenseVoice）
  把作者 17 遍唤醒词全部转写成「你好，open villa/oppo没了/…」，Linux 侧
  加文本模糊闸门是现成的兜底路线。

## TODO：准确率提升

1. ~~均衡信道负例重训~~ **完成（v6，2026-08-15）**。
2. ~~模型加宽 ch 48→56 + SpecAugment~~ **完成（v6）**：2×2 消融证明必须
   组合（只 SpecAugment 跨 seed 方差大，只加宽校准崩）。
3. ~~真人/真环境负例~~ **完成（v7，2026-08-29）**：asr-server 落盘的 738 段
   板麦真人片段 + 板麦底噪参训，真人误触 8%→0.4%、底噪 120/h→0。
4. **真人正样本（最大的剩余缺口）**：训练集里的真人只有负例，真人
   「你好，openvela」召回还没测过。`record_real.py pos --n 30 --board <板>`
   录 30 条（近/远/快/慢/轻/响轮转）+ `neg --n 20`（同一人的非唤醒句，
   否则模型学「这个人的嗓音=唤醒」），`mk_manifest_real.py` 自动接入并
   留 1/3 做 `eval_suite` 的 human-pos。多录几个人更好。
5. **v7 少掉的 1–2 条重录召回**：`rerec_pos_v07_t1_r2_p2`（无逗号的
   「你好 openvela」快速版）v6 0.98 → v7 0.24，可能是截断硬负例（35–68%
   裁切）压过了头；可试 40–65% 或每条只切 1 个，用 `eval_suite` 复核。
6. **PDM 硬件增益**：驱动 CIC scale 可再抬，改善入口信噪比（真人远场
   录音 rms 只有 -35~-40 dBFS，底噪 -50）。
7. **现场校准**：`KWS_SCORE` 流实测环境分数分布后用 `KWS_THR` 定档。
