# 实时说话人识别 WebSocket 接口

实时接收客户端上行音频，服务端完成 VAD、ASR、声纹识别后，下行返回说话人与识别文本。

## 连接

- URL：`wss://xiaozhi-dev.weike.fm:8087/ws`
- 子协议：无
- 鉴权：无

## 上行：客户端 → 服务端（二进制）

| 项 | 值 |
| --- | --- |
| 帧类型 | WebSocket **binary frame** |
| 采样率 | `16000 Hz` |
| 声道 | 单声道 |
| 采样格式 | `float32`（小端，范围 `[-1.0, 1.0]`） |

服务端按帧进入内部队列串行消费，块大小不作限制，连续发送即可。

示例（浏览器端）：

```js
const ws = new WebSocket("wss://xiaozhi-dev.weike.fm:8087/ws");
ws.binaryType = "arraybuffer";

const audioCtx = new AudioContext({ sampleRate: 16000 });
// 通过 AudioWorklet 拿到 float32 数据后：
ws.send(float32Array.buffer);
```

示例（Python 客户端）：

```python
import asyncio, numpy as np, websockets

async def main():
    async with websockets.connect("wss://xiaozhi-dev.weike.fm:8087/ws") as ws:
        audio = np.fromfile("sample.f32", dtype=np.float32)  # 16k/mono/float32
        await ws.send(audio.tobytes())
        async for msg in ws:
            print(msg)

asyncio.run(main())
```

## 下行：服务端 → 客户端（文本 JSON）

仅当检测到一段完整语音并完成识别后，服务端推送一条消息：

```json
{"speaker": "说话人1", "content": "你好，今天天气怎么样", "audio_url": "/audio/20260816/121212_你好.wav"}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `speaker` | string | `说话人N`，`N` 从 1 递增；同一声纹会复用已分配的名字 |
| `content` | string | ASR 识别文本 |
| `audio_url` | string \| null | 本段录音的 HTTP 地址，GET 即得 16 kHz 单声道 WAV；落盘失败时为 `null` |

说话人判定：当前分段的声纹与历史记录逐个比对，余弦相似度 `> 0.39`（`SPEAKER_THRESHOLD`）即视为同一人，否则分配新的 `说话人N`。

## 关闭

- 客户端主动 `close()` 即可；服务端会取消后台识别任务并释放资源。
- 服务端不会主动断开，除非进程退出。

## 常见问题

- **采样率对不上**：必须是 `16000`。浏览器端通过 `new AudioContext({ sampleRate: 16000 })` 让浏览器自动重采样。
- **没有回包**：发送端需要持续有语音并带有足够静音（默认 `960 ms`）触发端点判定，否则不会产生 `stt` 事件。
