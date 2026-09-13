# FSMN-VAD 有状态迁移与验收

本文件是待实施的迁移方法，**不代表比赛项目已有 VAD RKNN 成果**。当前仓只有 FunASR `VadWorker`，RK3576 上由 CPU 执行。

## 先定位网络与状态

读项目 `linux-apps/asr-server/vad.py`、`asr_client.py` 和当前安装版本的 FunASR 实现。项目模型为 `iic/speech_fsmn_vad_zh-cn-16k-common-pytorch`，revision `v2.0.4`；流入 `VadWorker` 的 PCM 块为 240 ms。

截至 2026-09-13 查看，[FunASR 官方 FSMN-VAD 导出实现](https://github.com/modelscope/FunASR/blob/main/funasr/models/fsmn_vad_streaming/export_meta.py) 将网络表示为 `speech` 加多个 `in_cache` 输入，返回 `logits` 与 `out_cache`；缓存维度由 encoder 配置推导。这证明需要保留显式网络状态，**不证明该 ONNX 已适配 RKNN**。实际实现先核对项目锁定版本，不能照抄上游 main 的缓存个数和尺寸。

状态至少分为四类：

| 状态 | 迁移边界 |
| --- | --- |
| 前端的重叠采样、fbank/CMVN/LFR 缓存 | 保留 CPU 实现，逐块产生与参考一致的特征 |
| FSMN 网络的记忆张量 | 导出为 ONNX/RKNN 的显式输入输出，每块更新 |
| 阈值、静音长度、起止端点等后处理 | 保留 CPU 状态机，输入同语义 logits 与有效长度 |
| 每会话累计时间、输出段缓存 | 保留应用层，reset 时统一清理 |

240 ms PCM 不自动等于 24 个网络帧：帧长、LFR、首块/尾块缓存都会影响特征数。先对实际前端记录每块样本数、有效特征长度和张量 shape。

## 实施阶段

### A. 建立 CPU 连续流基线

选一段含静音、短词、长句、停顿和尾部静音的录音，按实际 transport 分帧，经服务重新拼成 240 ms 块。逐块记录特征、网络缓存、logits、端点以及累计时间。

检查当前参考路径的末包和 reset 语义，不假设 `AutoModel.generate` 一次调用等同于裸网络调用。用户任务是迁移时保持既有行为；发现参考实现缺少末包 flush，单独注明并定义预期，避免拿两种不同语义“对拍”。

### B. 导出网络并在 ONNX 上验证

按实际网络构造 wrapper：`feats, cache_0...cache_n -> scores, next_cache_0...next_cache_n`。固定 batch、每块特征长度及缓存形状，长度不足的块另保留有效长度；填充帧不能被端点状态机误当成真实静音。

迁移循环的语义如下，变量为概念名，不是项目现有 API：

```text
session_state = initialize_from_reference_semantics()
for each transport chunk:
    valid_features = cpu_frontend.push(chunk, session_state.frontend)
    for each model block ready for inference:
        scores, next_caches = network(features, session_state.network_caches)
        session_state.network_caches = next_caches
        events = cpu_endpoint.step(scores, valid_length, absolute_frame_offset)
        emit(events)
flush_final_block_and_endpoint_using_reference_semantics()
```

先比较 PyTorch 与 ONNX 的连续多块输出。只有首块正确不够：缓存错误往往在若干块后累积。不要把缓存固化为零常量，也不要每个 chunk 重新实例化模型。

### C. 转换并对拍 RKNN

优先在 Ubuntu x86_64 上建立隔离转换环境，配置参考 [ASR 执行参考](asr-project-workflow.md)，在专用目录转换该 VAD 图，先跑 fp16 基线。VAD 的内存需求需实测，不直接沿用 ASR 的资源数字。现有 `tools/convert_rknn.py` 可作为定长多输入图加载的起点；它不是完整 VAD 导出器或流式运行器。检查输入顺序、缓存 dtype/layout、输出回传顺序、所用版本的算子支持及长度处理。

逐块比较 ONNX 与 RKNN 的特征输入、scores、next_caches，然后把 scores 接同一 CPU 端点状态机。预先定义可接受的数值误差与端点偏移，基于产品延迟要求制定；没有本项目实测依据时不编造“通用容差”。

int8 作为后续实验：校准特征和缓存应来自真实连续流轨迹，不能所有缓存都是初始零值。先按 Toolkit2 版本核对多输入校准格式，分开保存 fp16/int8 产物。

### D. 接入服务并测收益

保持 `VadWorker` 的 `generate_vad_segments`、`get_silence_duration_ms`、`reset` 等调用语义。每个会话拥有独立的前端、网络缓存和端点状态；若共享 NPU runtime，推理调用应按实际运行库能力串行化或使用独立实例，不能交叉写缓存。

持续维护独立的累计音频时间轴，不能用滚动预卷缓冲区长度代替。项目 `asr_client.py` 已将它们区分，接入 NPU 后端时保留这一约束。

## 必要验收案例

| 案例 | 必须验证的行为 |
| --- | --- |
| 同一长录音逐块输入 | 与 CPU 参考端点对应；缓存不随时间漂移 |
| 语音恰好跨 chunk 边界 | 首字不漏、尾字不截断、端点不重复 |
| 静音后短词与轻声 | 起点、阈值与前端历史匹配 |
| 末块不满、末句无额外长静音 | 有效长度与 flush 正确，不悬挂未闭合段 |
| 一句话中间短停顿 | 状态机保持参考语义，不提前结束 |
| reset 后新会话、多连接交错 | 不继承旧端点/时间/缓存，不串话 |
| 长时间静音和持续讲话 | 计时、缓存大小、内存与端点上限可控 |
| ASR/YOLO 共用 NPU | 含搬运、排队、前后处理的 chunk 总耗时满足输入速率 |

记录起止偏移、端点延迟、漏检/误检、chunk 总耗时、内存与 CPU 占用。网络快但服务端点更慢时，应报告迁移无净收益或继续定位瓶颈。若某个阶段失败，交付具体图/日志和复现条件，保留可用 CPU 基线，不描述为 VAD 已上 NPU。
