# 实时说话人识别（服务端麦克风版）

沿用 [../asr_ws](../asr_ws) 的识别链路，改为服务端采集，并支持从消息录入声纹：

| | 音频来源 | 上行 |
| --- | --- | --- |
| `asr_ws` | 浏览器 `getUserMedia` | 页面把 float32 PCM 发给服务端 |
| `asr_wsv2`（macOS） | 服务进程通过 `sounddevice` 采集本机麦克风 | 声纹录入命令，不上传音频 |
| `asr_wsv2`（Linux） | 板子上的 `arecord` | 声纹录入命令，不上传音频 |

采集的是**运行服务那台机器**的麦克风，浏览器可以在任意机器打开。

## 运行

以下命令在 `linux-apps/asr-server/` 目录执行，Mac 和 Linux 使用同一个入口：

```bash
uv run tests/asr_wsv2/server.py
```

`uv run` 会按 `uv.lock` 同步依赖。Mac 上会安装 `sounddevice`；Linux 板子需要另行安装
`arecord`（`alsa-utils`）。

模型加载完成后自动开始录音和识别，不需要浏览器参与。Mac 本机打开
`http://localhost:8088/`，远程打开 `http://<服务端IP>:8088/`。
页面连上先补发已有结果，之后实时追加；每条消息旁可点击「添加录入」。

## 声纹录入

1. 对着运行服务的机器说一句完整的话，建议至少 2～3 秒。
2. 在下方消息列表中选择这条语音，点击「添加录入」。
3. 填写姓名，或选择已录入的姓名，点击「保存声纹」。

保存成功后，所选消息立即显示该姓名，后续语音优先与已录入声纹匹配；未匹配时
继续使用自动分配的 `speaker_N`。有效语音短于 1000 ms 或声纹无效的消息不能录入。
同一姓名最多保留 10 条不同消息的声纹；重复添加同一声纹不增加样本数。

声纹保存在服务目录的 `speaker_profiles/voiceprints.json`，重启自动加载，
该目录已加入 `.gitignore`。可用环境变量 `SPEAKER_PROFILE_PATH` 指定其他路径。
存储的是姓名和声纹向量，不会额外复制录音；原有 `audio_logs/` 落盘行为不变。
文件无法读取或格式错误时，页面显示错误并停用录入，保留原文件。

消息列表和待录入样本只保存在当前进程内存中，重启后清空；已保存的具名声纹继续保留。
多个页面会同步收到录入后的姓名和样本数量。

主服务 `uv run server.py` 提供独立的 [embedding 提取接口](../../README.md#声纹-embedding-接口)，
由调用方保存声纹资料；本调试页的录入和本地文件仅供 demo 使用。

## 麦克风

### macOS

默认使用 macOS 系统输入设备，通过 `sounddevice` 采集 `16000 Hz` / 单声道 / float32。
首次运行时允许启动服务的终端或应用访问麦克风；如未授权，在「系统设置 → 隐私与安全性 →
麦克风」中开启对应权限后重启服务。

列出本机音频设备：

```bash
uv run python -m sounddevice
```

用 `MIC_DEVICE` 指定输入设备编号或名称（也可写进 `.env`）：

```bash
MIC_DEVICE=0 uv run tests/asr_wsv2/server.py
MIC_DEVICE='MacBook Pro Microphone' uv run tests/asr_wsv2/server.py
```

设备编号和名称以本机枚举结果为准。启动日志会打印实际打开的设备名称。

### Linux 板子

`arecord -l` 里的 card 3：

```
card 3: rpmsgmic [openvela AMP mic], device 0: rpmsg-mic [AMP microphone]
```

即 `hw:3,0`。代码里默认用名字形式 `hw:rpmsgmic,0`，声卡重新编号也不会录错设备；
换设备用环境变量覆盖：

```bash
MIC_DEVICE=hw:2,0 uv run python tests/asr_wsv2/server.py
```

该设备只支持 `S16_LE` / 单声道 / `16000 Hz`，正好是 `AsrClient` 需要的格式，不做重采样。

## 采集与结果

- 模型加载完成后即持续采集：Mac 使用本机麦克风，Linux 使用板载 `arecord`，
  与有没有页面连着无关。Linux 的 `hw:` 麦克风是独占设备。
- 识别结果按时间顺序存进 `results` 列表（进程内存，重启即清空）。
- 页面连上时先补发 `results` 里已有的，之后实时广播新的；开多个页面看到的内容一致。
- 采集依赖缺失、设备打开失败（设备被占用、名称写错、Mac 未授权）或采集异常退出时，
  终端和页面状态栏显示失败原因；缺少采集依赖时不会加载识别模型。
  后连上的页面也会收到。进程不会自己重试，需要重启。
- Ctrl+C 会关闭音频流或 `arecord` 子进程，并停止识别任务。

## 离线回归

在 `linux-apps/asr-server/` 下执行：

```bash
uv run python -m unittest discover -s tests -p 'test_asr_wsv2_capture.py' -v
uv run python -m unittest discover -s tests -p 'test_voiceprint_enrollment.py' -v
```

用模拟采集后端验证平台选择、设备编号/名称、回调转发、错误通知和资源释放，
并验证声纹录入、持久化、重复样本处理、保存失败回滚和后续姓名匹配；
不加载模型或打开真实麦克风，不能代替实际设备上的识别验收。

## 下行：服务端 → 客户端（文本 JSON）

识别结果：

```json
{"id": "消息ID", "speaker": "张三", "content": "你好，今天天气怎么样", "elapsed": 0.2, "audio_url": "/audio/20260816/121212_你好.wav", "can_enroll": true}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 本次消息 ID，录入时回传给服务端 |
| `speaker` | string | 已录入姓名；未匹配时为 `speaker_N`。语音短于 1000 ms 时返回 `unknown` |
| `content` | string | ASR 识别文本 |
| `elapsed` | number | ASR 推理耗时，单位为秒 |
| `audio_url` | string \| null | 本段录音的 HTTP 地址，GET 即得 16 kHz 单声道 WAV；落盘失败时为 `null` |
| `can_enroll` | boolean | 该条消息是否包含可录入的声纹 |

出错时下行 `{"type": "error", "message": "..."}`。

录入时页面上行：

```json
{"type": "enroll", "result_id": "消息ID", "name": "张三"}
```

返回 `{"type":"enroll_result","ok":true,"result_id":"消息ID","name":"张三","message":"..."}`；
失败时 `ok` 为 `false`，`message` 给出原因。
页面连接后和录入成功后还会收到 `voiceprints` 消息，包含姓名、样本数量和可选的消息姓名更新，
不包含原始声纹向量。
