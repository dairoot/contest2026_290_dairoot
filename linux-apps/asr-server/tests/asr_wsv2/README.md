# 实时说话人识别（板载麦克风版）

和 [../asr_ws](../asr_ws) 是同一个 demo，区别只有音频来源：

| | 音频来源 | 上行 |
| --- | --- | --- |
| `asr_ws` | 浏览器 `getUserMedia` | 页面把 float32 PCM 发给服务端 |
| `asr_wsv2` | 板子上的 `arecord` | 无，页面只收结果 |

因此这一版必须跑在板子上（麦克风在板子上），浏览器可以在任意机器打开。

## 运行

```bash
uv run python tests/asr_wsv2/server.py
```

进程一启动就开始录音和识别，不需要浏览器参与。浏览器打开 `http://<板子IP>:8088/`
只是个观察窗：连上先补发已有结果，之后实时追加；页面没有任何按钮。

## 麦克风

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

- 进程启动即拉起 `arecord` 一直录，与有没有页面连着无关；麦克风是独占设备，
  所以服务跑着的时候别的程序录不了音。
- 识别结果按时间顺序存进 `results` 列表（进程内存，重启即清空）。
- 页面连上时先补发 `results` 里已有的，之后实时广播新的；开多个页面看到的内容一致。
- `arecord` 异常退出（设备被占用、设备名写错）时，页面状态栏显示失败原因，
  后连上的页面也会收到。进程不会自己重试，需要重启。

## 下行：服务端 → 客户端（文本 JSON）

与 `asr_ws` 完全一致：

```json
{"speaker": "说话人1", "content": "你好，今天天气怎么样", "audio_url": "/audio/20260816/121212_你好.wav"}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `speaker` | string | `说话人N`，`N` 从 1 递增；同一声纹会复用已分配的名字。语音短于 `MIN_SV_SPEECH_MS`（1500 ms）时不参与声纹匹配，返回 `unknown` |
| `content` | string | ASR 识别文本 |
| `audio_url` | string \| null | 本段录音的 HTTP 地址，GET 即得 16 kHz 单声道 WAV；落盘失败时为 `null` |

出错时下行 `{"type": "error", "message": "..."}`。
