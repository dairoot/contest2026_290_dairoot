# ASR WebSocket 服务（Linux 侧语音识别 + 声纹）

RK3576 上 Linux 侧的语音链路，接在 openvela 侧的离线唤醒之后：cpu3 上的 openvela
常听「你好，openvela」，唤醒后由本服务在 Linux 的 7 个核 + NPU 上做实时识别与说话人
辨认。唤醒那一半见 [`../../tools/kws/`](../../tools/kws/) 和
[`../../board/contest_board/`](../../board/contest_board/)。

`server.py` 把 `asr_client.AsrClient` 公开为 WebSocket 服务，每条连接拥有独立的
VAD、ASR 和说话人状态。**ASR 与声纹两个模型都跑在 RK3576 的 6 TOPS NPU 上**，
实测见下面的「NPU（RK3576）」一节。

## 目录结构

以下模块位于本目录（`linux-apps/asr-server/`），以本目录为工作目录运行：

```
asr_client.py         # AsrClient：音频分块、VAD 驱动、快/慢回复策略
server.py             # WebSocket + HTTP 服务入口
config.py             # 后端选择与模型路径（读 .env）
vad.py                # FSMN VAD（PyTorch，跑 CPU）
speaker.py            # 说话人匹配（centroid）+ 后端分流
speaker_rknn.py       # 声纹 NPU 后端
voiceprint_store.py   # asr_wsv2 调试页的具名声纹存储
event_emitter.py      # 极简异步事件总线
asr_model/            # sense_voice（int8 ONNX / RKNN NPU）、volc 三种识别后端
utils/                # 批处理、设备探测、模型缓存路径
tools/                # 模型导出与 RKNN 转换（只在开发机/转换机跑）
tests/                # 单元测试 + 浏览器调试页
```

## 启动

与摄像头、harness 配套运行时，见总 README 的
[三个服务启动说明](../../README.md#linux-app-服务启动)。以下命令在本目录执行。

依赖由 [uv](https://docs.astral.sh/uv/) 管理，声明在 `pyproject.toml`，锁定在
`uv.lock`。`uv sync` 会按 `.python-version`（3.11）自动准备解释器并创建 `.venv`：

```bash
uv sync
uv run server.py
```

`uv run` 会在执行前校验环境与锁文件是否一致，不需要手动 `activate`。
增删依赖用 `uv add <包>` / `uv remove <包>`（会同步更新 `pyproject.toml` 和
`uv.lock`），升级用 `uv lock --upgrade-package <包>`。

同一份 `uv.lock` 覆盖开发机（macOS，torch 走 MPS）和 ARM64 Linux 部署环境。
PyPI 上 torch 的 Linux wheel 不分架构地依赖 CUDA 全家桶，而部署环境没有 N 卡，
所以 `pyproject.toml` 把 Linux 侧的 `torch` / `torchaudio` 指到官方 CPU 源取同版本的
`+cpu` 构建，`uv sync` 在部署环境上不会拉 NVIDIA 包。CPU 源的 wheel 是
`manylinux_2_28`，要求 glibc ≥ 2.28（Debian 10+ / Ubuntu 20.04+）。改动依赖后可以
在开发机上先验证部署侧的解析结果：

```bash
uv sync --frozen --dry-run --python-platform aarch64-manylinux_2_28
```

默认监听 `0.0.0.0:8086`，识别后端由 `ASR_MODEL_TYPE` 决定（见下节，板子上用
`sense_voice_rknn` 走 NPU）。需要改监听地址、端口或后端时，可在 Python 中调用
`server.serve(...)`。只有选 `volc` 才需要在 `.env` 里配置火山 ASR 凭据。

调用方通过以下配置连接服务：

```dotenv
ASR_SERVER_WS_URL=ws://127.0.0.1:8086/ws
```

## ASR 后端选择

`ASR_MODEL_TYPE` 决定 `AsrClient` 的默认后端，可写进 `.env` 或临时用环境变量覆盖：

| 取值 | 模型 | 说明 |
| --- | --- | --- |
| `sense_voice_rknn`（默认） | `rknn_models/sensevoice_5s.rknn` | RKNN fp16，跑 RK3576 NPU |
| `sense_voice` | `iic/SenseVoiceSmall-onnx` | int8 ONNX，onnxruntime CPU 推理；没有 NPU 的机器上用这个 |
| `volc` | 火山流式 ASR | 需要 `.env` 里的凭据，走网络 |

`sense_voice_onnx` 是 `sense_voice` 的别名。PyTorch fp32 的 `iic/SenseVoiceSmall`
后端已经删除：936 MB 权重、加载峰值 3 GB，4 GB 的部署板子会被 OOM killer 杀掉。

前端特征沿用 funasr 的 `WavFrontend`，与原 PyTorch 路径一致；解码是 CTC 贪心 +
`tokens.json` 反查，没有引入 `funasr-onnx`（那个包把 numpy 钉在 1.26.4，还要拖
librosa/numba）。删除前在 188 条本地录音上与 PyTorch fp32 做过对比：**80.3% 输出
完全一致，字错率 1.92%**，开发机上推理快 13 倍。int8 在低端 ARM 上提速有限
（Cortex-A72/A53 是 ARMv8.0，没有点积指令），实际收益要在目标板子上实测。

`ASR_ONNX_THREADS` 控制 onnxruntime 的 intra-op 线程数，默认 `0`（按核数自动决定）。

## NPU（RK3576）

ASR 和声纹**默认都跑在 NPU 上**，板子实测（RK3576，8 核 CPU / 6 TOPS NPU）：

| | CPU | NPU | 备注 |
| --- | --- | --- | --- |
| 声纹 ERes2NetV2（3 秒窗口） | 4589 ms | **519 ms** | embedding 与 CPU 余弦 0.999+ |
| ASR SenseVoice（3.7 秒语音） | 702 ms | **378 ms** | 文本与 CPU int8 逐字一致 |
| ASR SenseVoice（4.9 秒语音） | 864 ms | **399 ms** | 同上 |
| ASR SenseVoice（5.3 秒语音） | 953 ms | 776 ms | 超窗切成两段，两次推理 |

当前 NPU 后端使用定长窗口，默认 5 秒，单窗口实测约 390 ms；超窗长句需要多次
推理。`ASR_RKNN_WINDOW_MS` 必须与所加载模型的窗口一致。

**VAD 不上 NPU**：`vad.py` 用 funasr 直接加载 FSMN-VAD
（`iic/speech_fsmn_vad_zh-cn-16k-common-pytorch`），PyTorch 推理、板上就是 CPU。
它逐 chunk 维护 `vad_cache` 来判断端点。当前服务只有 ASR 和声纹两个 RKNN 后端，
没有 VAD RKNN 后端。

### 怎么选后端

NPU 是定长窗口，耗时与音频长短无关；CPU 是变长的，句子越短越快。两个模型的取舍
因此完全不同：

| | NPU | CPU | 交叉点 |
| --- | --- | --- | --- |
| ASR | 恒定 390 ms（5 秒窗） | 约 180 ms / 秒音频 | **约 2.2 秒**，短句 CPU 更快 |
| 声纹 | 恒定 519 ms（3 秒窗） | 3 秒音频 4589 ms | 没有，NPU 恒赢 |

**默认两个模型都走 NPU**，板子上直接 `uv run python server.py` 即可，不需要额外
配置，但需要先准备下文列出的模型。ASR 的网络推理交给 NPU 后，CPU 继续处理音频
前端、VAD 和解码；超窗长句的耗时随窗口数量增加。

如果场景以 2 秒以内的短命令词为主，把 ASR 切回 CPU 会更快一点，还省掉 473 MB 的
ASR RKNN 模型不用加载：

```bash
ASR_MODEL_TYPE=sense_voice uv run python server.py
```

没有 NPU 的机器（开发机）把声纹也回退到 CPU：

```bash
SPEAKER_BACKEND=modelscope uv run python server.py
```

默认配置依赖 `rknn_models/` 下的 `sensevoice_5s.rknn`（473 MB）和
`eres2netv2_3s.rknn`（174 MB）；ASR 切回 CPU 时只需要后者。路径可用 `SPEAKER_RKNN_PATH` / `ASR_RKNN_PATH` 改。
这些文件以官方仓库 Release 附件提供，不进入 Git 历史；下载、校验及额外的
ASR 前端 / VAD 依赖见 [RKNN 模型交付说明](../../docs/rknn-models-2026-09-19.md)。
模型准备、Ubuntu 转换环境、运行库兼容处理与部署验收
统一见 [语音模型 RKNN 迁移 Skill](../../skills/speech-rknn-migration/SKILL.md) 及其
[ASR/声纹执行参考](../../skills/speech-rknn-migration/references/asr-project-workflow.md)。

板上推理依赖 `rknn-toolkit-lite2`，已按 aarch64 marker 写进 `pyproject.toml`，
`uv sync` 会自动安装；还需保证实际加载的 `librknnrt.so`、模型与 NPU 驱动兼容。

### 板子的 swap

3.8 GB 内存跑 ASR + 声纹 + VAD 峰值能到 2.4 GB，没有 swap 很容易被 OOM killer 端掉。
这块板子的 zram 是**编进内核**的（不是模块），发行版的 `zram-tools` 启动流程要求
modprobe，因此改用直接操作 sysfs 的
[zram-swap.service](../../board/contest_board/linux-side/rootfs/etc/systemd/system/zram-swap.service)。
板上安装位置为 `/etc/systemd/system/zram-swap.service`，配置为 3 GiB / zstd、
优先级 100，开机自起，`zramswap.service` 已禁用。安装与检查见
[Linux 服务部署说明](../../skills/openvela-kws-deployment/references/linux-systemd-services.md)；
不要在推理服务占用 swap 时直接 restart 或 reset。

## 测试

```bash
uv run python -m unittest discover -s tests
```

包含调试页声纹存储、embedding 接口、预设声纹配置及使用模拟 ASR 的本地 WebSocket 集成测试，需要允许绑定回环端口；
不会加载真实语音模型或打开麦克风。

`tests/asr_ws/` 是浏览器调试页：`uv run python tests/asr_ws/server.py` 后打开
`http://<板子IP>:8086/`，对着麦克风说话就能看到识别结果和说话人编号。

[`tests/asr_wsv2/`](tests/asr_wsv2/) 自动采集运行服务的机器上的麦克风：Mac 使用本机输入设备，
Linux 使用板载 `arecord`。运行 `uv run tests/asr_wsv2/server.py` 后打开
`http://localhost:8088/` 查看结果，页面不采集音频。
在消息旁点击「添加录入」可将这条语音的声纹保存为指定姓名，重启后继续用于匹配。

`audio_logs/`、`rknn_models/` 等本地录音与模型产物已在 `.gitignore` 中排除。

## 火山 ASR 热词

人名、品牌、专有名词识别不准时，可在 `.env` 中配置热词：

```dotenv
# 直传热词，逗号/分号/换行分隔；流式输入模式最多 5000 个
VOLC_HOTWORDS=豆包,火山引擎,毫米波雷达

# 或使用自学习平台配置好的热词词表（直传热词优先级更高）
VOLC_BOOSTING_TABLE_NAME=
VOLC_BOOSTING_TABLE_ID=
```

需要按会话动态调整时，可以直接构造 `VolcAsrWorker(hotwords=[...])`，或对已有
worker 调用 `set_hotwords(...)`，新热词在下一段语音建连时生效。

`ChatBot.initialize()` 会自动建立连接并维护音频收发，宿主只需把麦克风数据交给
`chat_bot.audio_callback()`，不再需要导入 `AsrClient` 或启动本地处理任务。

## WebSocket 协议

连接地址：

```text
ws://<host>:8086/ws
```

音频上行使用二进制帧，内容为裸 PCM：

- `16000 Hz`
- 单声道
- Float32 little-endian
- 推荐范围 `[-1.0, 1.0]`
- 不包含 WAV 文件头

`server.py` 会逐帧校验并立即把音频交给 `AsrClient`；`AsrClient` 内部缓存并重组
任意 WebSocket 帧边界，再以 3840 个样本（240 ms）为一块送入 VAD / ASR。
最后一句话需要在语音后继续上传足够的静音，VAD 才能输出结果。

每个 `stt` 事件会变成一个 UTF-8 JSON 文本帧：

```json
{
  "type": "stt",
  "speaker_id": "",
  "speaker": "speaker_1",
  "content": "你好",
  "audio_url": "/audio/20260723_210943_%E4%BD%A0%E5%A5%BD.wav",
  "elapsed": 0.21,
  "embedding": [0.1, -0.2],
  "speech_ms": 1680.0
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | string | 消息类型，识别结果固定为 `stt`。 |
| `speaker_id` | string | ASR 模型返回的说话人 ID；模型不提供时为空字符串。 |
| `speaker` | string | 优先返回匹配的预设姓名；未匹配时返回匿名编号，如 `speaker_1`、`speaker_2`；有效语音不足 1500 ms 时为 `unknown`。 |
| `content` | string | 本次语音识别得到的文本。 |
| `audio_url` | string \| null | 本段 WAV 录音的 HTTP 相对地址；录音不在配置目录内时为 `null`。 |
| `elapsed` | number | ASR 推理耗时，单位为秒。 |
| `embedding` | number[] | 说话人声纹向量，可用于说话人匹配或聚类。 |
| `speech_ms` | number | VAD 检测到的有效语音时长，单位为毫秒。 |

主服务在每次 STT 回包前调用 `SpeakerWorker.get_speaker_name()`，优先匹配当前连接的预设姓名；
没有匹配时，同一连接内相似声纹沿用编号，新声纹分配新编号。预设和匿名样本仅保存在
该连接的内存中，重新连接后匿名编号从 `speaker_1` 开始，预设需重新发送。
SDK 与 harness 原样使用返回的 `speaker`，harness 不再进行二次声纹匹配。
独立 `/embedding` 接口仍只返回向量，不分配编号。

除新增 `speaker`、本地 `audio_path` 被转换为 `audio_url` 外，其余字段与 `AsrClient` 的 `stt`
事件一致；NumPy 数组会转换为 JSON 数组。协议或音频格式错误会返回
`{"type":"error", ...}`，不会作为音频送入识别器。

### 设置预设声纹

在同一个 `/ws` 连接内发送 JSON 文本帧，建议在首次上传音频前设置：

```json
{"type":"set_speaker_profiles","profiles":[{"name":"张三","embedding":[1.0,0.0,0.0]}]}
```

示例向量仅演示格式，实际应使用同一声纹模型提取的完整 embedding。同名可传多个样本。
每条消息全量替换当前连接的预设，`profiles: []` 清空；录入、删除后可在连接内再次发送。
服务端校验并归一化全部向量，成功后返回：

```json
{"type":"speaker_profiles_set","ok":true,"speakers":1,"samples":1}
```

客户端收到确认后再上传音频。姓名需为 1～40 个字符且不能包含控制字符；向量需非空、有限且非零，
所有预设向量维度一致、最多 4096 维；每人最多 10 个样本，总计最多 1000 个样本，
同时受 WebSocket 单帧大小限制（默认 1 MiB）。无效请求返回
`{"type":"error","code":"invalid_speaker_profiles","message":"..."}`，原预设保持不变。

配置消息不会加载语音模型或写入文件，只影响当前连接；客户端负责持久化并在重连后重发。
预设匹配使用同名样本的 centroid、余弦阈值和次优分差规则，预设样本不会被识别结果自动补充。
有效语音不足 1.5 秒仍返回 `unknown`；模型维度不同的预设不参与匹配。

## 声纹 embedding 接口

运行 `uv run server.py` 后，连接 `ws://<host>:8086/embedding`。
一个二进制消息发送一段完整音频，服务端只返回该段的声纹向量：

```json
{"embedding":[0.1,-0.2]}
```

输入为 **16 kHz、单声道、Float32 little-endian 裸 PCM**，不含 WAV 头，
样本范围 `[-1, 1]`。每段至少 1.5 秒，建议 2～3 秒；超过 3 秒时只使用开头
3 秒，与 `AsrClient` 的声纹输入窗口一致。不要把一段音频拆成多条消息上传。
连接可复用，每条完整音频对应一条 JSON 响应。

客户端自行保存姓名和 embedding，可通过 `/ws` 设置预设让 ASR 匹配姓名。该接口不保存音频、姓名或声纹资料，
只加载声纹模型，不启动 VAD / ASR。原有 `/ws` 返回的 `stt.embedding` 也可以直接使用。

例如已有一段 `speaker.f32` 音频时：

```python
import asyncio
import json
import numpy as np
import websockets

async def main():
    audio = np.fromfile("speaker.f32", dtype="<f4")
    async with websockets.connect("ws://localhost:8086/embedding") as ws:
        await ws.send(audio.tobytes())
        result = json.loads(await ws.recv())
        print(result["embedding"])

asyncio.run(main())
```

格式或提取失败时返回 `{"type":"error","code":"...","message":"..."}`，
不会关闭连接，可修正音频后重试。`audio_too_short` 表示不足 1.5 秒，
`embedding_failed` 表示模型加载、推理或输出验证失败；其他音频格式校验与 `/ws` 一致。

## 录音 HTTP 接口

`audio_url` 使用 WebSocket 服务相同的主机和端口。例如服务地址为
`ws://192.168.1.100:8086/ws` 时，录音完整地址为：

```text
http://192.168.1.100:8086/audio/20260723_210943_%E4%BD%A0%E5%A5%BD.wav
```

HTTP 服务只允许读取 `audio_save_dir` 中的 `.wav` 文件，不会公开其他本地路径。
