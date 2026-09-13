# ASR/声纹项目执行参考

代码基线：比赛仓 `4f5d6d6`，整理日期 2026-09-13。下面路径相对 `linux-apps/asr-server/`。

## 代码与接口

| 文件 | 作用 |
| --- | --- |
| `tools/export_onnx.py` | 导出 ASR/声纹定长 fp32 ONNX；没有 VAD 导出功能 |
| `tools/convert_rknn.py` | 读取输入名/shape，将指定目录的所有 `.onnx` 转换成 `.rknn` |
| `asr_model/_sense_voice_common.py` | CPU/NPU 共用前端与 CTC 解码、词表 |
| `asr_model/asr_sense_voice_rknn.py` | 短句补零、跨窗拼接、RKNN 推理 |
| `config.py` | `ASR_RKNN_PATH`、`ASR_RKNN_WINDOW_MS`、后端设置 |
| `vad.py`、`asr_client.py` | 流式 VAD 状态、240 ms PCM 分块与应用时间轴 |
| `utils/_rknn.py` | 检查包/文件存在；不证明 NPU 运行成功 |

默认 ASR 接口：`speech [1,83,560]` → `ctc_logits [1,87,25055]` 和长度输出。运行器使用第一个输出；接口应以本次 ONNX 为准，不能靠文件名判断。5 s 对应 `(floor((5000-25)/10)+1)//6 = 83` 个 LFR 帧；模型另插 4 个 prompt 帧。

导出器固化 `speech_lengths=83`、`language=3`（中文）、`textnorm=14`（withitn）。前端依赖匹配的 `config.yaml`、`am.mvn` 和 `tokens.json`；仅拷贝 `.rknn` 并不能完成离线交付。

## 推荐 Ubuntu 转换，分开导出与转换环境

**默认推荐 Ubuntu x86_64 转换机**，使用独立 Toolkit2 环境，产出后再复制到 RK3576 板上运行。ASR 和声纹的转换方法统一在本文维护；服务 README 只保留运行配置与此 Skill 的入口。

项目已验证的 Ubuntu x86_64 转换基线为 Python 3.10、Toolkit2 2.3.2、CPU torch 2.4.0、onnx 1.16.1、setuptools<81。它们用于复现历史成功环境，不代表所有新版本都需要这些限制。

原因：旧 Toolkit2 依赖 `pkg_resources` 与 `onnx.mapping`，并限制 torch 版本。模型导出环境遵循项目 `pyproject.toml`，其中 FunASR 固定 1.3.14；转换环境单独创建，避免互相降级。

在 Ubuntu x86_64 转换机创建隔离环境（`CONVERT_ENV` 设为该机实际路径）：

```bash
uv venv "$CONVERT_ENV" --python 3.10
uv pip install --python "$CONVERT_ENV/bin/python" 'torch==2.4.0' --index-url https://download.pytorch.org/whl/cpu
uv pip install --python "$CONVERT_ENV/bin/python" 'rknn-toolkit2==2.3.2' 'onnx==1.16.1' 'setuptools<81'
```

ASR 导出历史峰值约 3 GB，转换需要 8 GB 以上内存；给转换机留余量。macOS 可以承担支持的 PyTorch 导出，项目的 Toolkit2 转换流程默认放在 Ubuntu；其他 Linux 环境需另核对依赖兼容性。RKNN-Lite2 是板上推理包，不能替代转换工具。官方组件划分见 [RKNN-Toolkit2](https://github.com/airockchip/rknn-toolkit2/blob/master/README.md)。

Ubuntu 精简 Python 用 `python3 -m venv` 可能遇到 `ensurepip is not available`；上面的 `uv venv --python 3.10` 可准备独立解释器与环境。模型体积要计入中间产物：默认两模型的 ONNX 约 937 MB + 214 MB，RKNN 约 473 MB + 174 MB，产物目录至少留 3 GB，另外给环境、下载缓存和转换临时图留空间。

## 仅迁移 ASR 的命令

把 `ASR_EXPORT_DIR` 设为本次专用产物目录；不要混入声纹、VAD 或 Toolkit 检查中间图，转换器会扫描所有 `.onnx`。

```bash
cd "$CONTEST_ROOT/linux-apps/asr-server"
uv run --with onnx --with onnxscript python tools/export_onnx.py \
  --skip-speaker --asr-seconds 5 --out-dir "$ASR_EXPORT_DIR"
```

导出器设置 `dynamo=False` 来兼容项目的 FunASR 导出方式。复用 `--asr-dynamic-onnx` 时只传未经量化、接口匹配的动态 ONNX；CPU 后端下载的 int8 模型不是替代品。

先用 ONNX 检查器和参考推理核对图、张量和解码，再在 Ubuntu 转换机运行。若在 macOS 导出，先传输 ONNX 及其引用的外部权重文件，保留相对目录结构，并将 `CONTEST_ROOT`、`ASR_EXPORT_DIR` 设置为 Ubuntu 上的实际路径：

```bash
cd "$CONTEST_ROOT/linux-apps/asr-server"
"$CONVERT_ENV/bin/python" tools/convert_rknn.py \
  --model-dir "$ASR_EXPORT_DIR" --target rk3576
```

默认 `do_quantization=False`。要评估 int8 时复制到另一份产物目录，再加 `--quantize --dataset "$CALIBRATION_LIST"`；本项目清单每行一个匹配 speech 输入的 `.npy`。保留 fp16 产物并用同一测试集比较。

新模型若存在多输入，不直接套用单输入校准清单；先核对所用 Toolkit2 的多输入数据格式与对应输入顺序。

转换日志中的 `value smaller than -3e+38` 曾对应注意力 mask 的负无穷常量；项目历史样本输出正确，但新模型仍需对比有限值、logits 和文本，不能一概忽略。转换器还可能在工作目录产生 `check*.onnx` 临时图，确认用途后清理或隔离，避免下次被目录扫描当成输入模型。

## 同时准备 ASR 与声纹

需要完整服务的两份默认模型时，将 `SPEECH_EXPORT_DIR` 设为专用产物目录，在导出机执行：

```bash
cd "$CONTEST_ROOT/linux-apps/asr-server"
SPEAKER_BACKEND=modelscope uv run --with onnx --with onnxscript python tools/export_onnx.py \
  --asr-seconds 5 --speaker-seconds 3 --out-dir "$SPEECH_EXPORT_DIR"
```

传到 Ubuntu 转换机并设置该机路径后执行：

```bash
cd "$CONTEST_ROOT/linux-apps/asr-server"
"$CONVERT_ENV/bin/python" tools/convert_rknn.py \
  --model-dir "$SPEECH_EXPORT_DIR" --target rk3576
```

| 模型 | 默认 ONNX → RKNN | 网络输入 → 输出 |
| --- | --- | --- |
| SenseVoiceSmall | `sensevoice_5s.onnx` → `sensevoice_5s.rknn` | `[1,83,560]` → `[1,87,25055]` logits 及长度 |
| ERes2NetV2 | `eres2netv2_3s.onnx` → `eres2netv2_3s.rknn` | `[1,298,80]` fbank 特征 → `[1,192]` embedding |

两个模型都从未量化权重导出，前端留在 Python。只导出声纹时使用 `--skip-asr`，只导出 ASR 时使用 `--skip-speaker`；转换脚本没有相应 skip 参数，而是处理目录里的全部 ONNX。

声纹曾在 3.8 GB RK3576 板上原地转换成功，转换阶段约需 2 GB 内存；这是一条资源允许时的可选路径，默认仍用 Ubuntu x86_64。板上历史环境为 Toolkit2 2.3.2、onnx 1.16.1、torch 2.2.0，不能直接假设 x86_64 的 torch wheel 或已安装环境在板上可用。历史样本 embedding 与 CPU 的余弦为 0.99869；它是参考记录，新模型仍需独立比较。

`SPEAKER_BACKEND=modelscope` 表达导出原始权重的意图，避免旧版本被 `.env` 的 RKNN 后端带偏；当前 `export_speaker` 也会设置此值。不要因为声纹能在板上转换，就在同一板上启动 ASR 转换。

## 模型传输

`BOARD_TARGET` 为实际 SSH 目标，`BOARD_MODEL_DIR` 为已准备好的板上模型绝对目录（通常是 ASR 服务目录下的 `rknn_models/`）。转换机可直达板子时：

```bash
scp "$SPEECH_EXPORT_DIR/sensevoice_5s.rknn" \
    "$SPEECH_EXPORT_DIR/eres2netv2_3s.rknn" \
    "$BOARD_TARGET:$BOARD_MODEL_DIR/"
```

只转换 ASR 时只传该次生成的 ASR 文件。Ubuntu 转换机在公网、板子在内网时，经能访问两端的开发机中转，先取回明确文件，再发送到板上；比较发送前后的哈希。不要依赖历史 SSH 别名或用 `/tmp/*.rknn` 混入其他实验产物。

## 部署与后端验证

部署前确认 target 与板子一致，记录转换器、Lite2、实际加载 runtime 和驱动版本。项目曾遇到预装 runtime 过旧，以及 Lite2 实际从 `/usr/lib/librknnrt.so` 加载的情况；以当前进程加载路径/版本为证，不能仅设置 `LD_LIBRARY_PATH` 就认定已切换。需要替换系统库时先做可恢复备份，使用有明确版本的兼容构件，不默认从 master 下载后覆盖。

将产物复制到实际板上 `rknn_models/` 或指定绝对路径。以下在板上的 ASR 服务目录运行，`ASR_MODEL_FILE` 指向该板本地文件：

```bash
ASR_MODEL_TYPE=sense_voice_rknn ASR_RKNN_PATH="$ASR_MODEL_FILE" \
ASR_RKNN_WINDOW_MS=5000 uv run python tests/asr_ws/server.py
```

这个调试服务还会初始化 VAD 和声纹；确认其依赖/模型已准备好。声纹已有独立 RKNN 产物时沿用配置，没有时设置 `SPEAKER_BACKEND=modelscope` 并准备参考权重。这一步不会生成或启用 VAD RKNN。

验证实际 `SenseVoiceRknnWorker`、`load_rknn` 与 `init_runtime` 成功，检查没有回退日志。工作类的 `model_type` 字符串仍是 `sense_voice`，不能单凭该日志区分 CPU/NPU。当前 `asr_client.py` 还可能在模块导入阶段预加载可用的 RKNN 模型；做纯 CPU 内存/启动对照时应在隔离运行配置里避开此预加载。

浏览器调试页或离线固定音频都可做初测；模型验收另按下面矩阵执行。源码里没有完整的 ASR 数值/跨窗对照测试套件，不把调试页打开等同于全部测试通过。

## 验证矩阵

| 情况 | 观察 |
| --- | --- |
| 短于 5 s | 补零不产生额外词；记录与 fp32 ONNX 及 CPU int8 的差异 |
| 刚好 5 s、略超 5 s、多窗口 | 首尾词、窗口接缝、重复字、prompt 标签、标点 |
| 超窗后只剩静音 | 当前尾窗静音判据为 -45 dBFS，确认不会吞掉轻声尾词 |
| 空音频/全静音 | 服务有明确处理，不制造转写或无效网络输入 |
| 中文、用户所需其他语种 | 固定语种输入与实际需求一致 |
| ASR/VAD/声纹与 YOLO 并行 | 排队、端点等待、吞吐与峰值内存，不只测空载网络 |

历史“约 390 ms”属于 RK3576 的特定 5 s 窗模型，不能用来证明任意句长的整句耗时恒定；长句可能运行多个窗口。

## 声纹经验的适用范围

`speaker_rknn.py` 对短音频循环填充到 3 s，ASR 则补零。这说明填充策略取决于模型语义；不能将声纹策略照搬到 VAD。声纹窗口当前在源码固定为 3000 ms，换 `--speaker-seconds` 还需同步运行器，而不是只改环境变量。声纹已完成的转换不代表 VAD 已迁移。
