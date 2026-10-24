# harness

## 启动

先启动 ASR 服务；需要摄像头和米家设备功能时，同时启动 miloco-server。
完整命令见总 README 的[三个服务启动说明](../../README.md#linux-app-服务启动)。
在本目录（`linux-apps/harness/`）执行：

```bash
cp .env.example .env   # 首次配置；已有 .env 时跳过，先填写所选 LLM / TTS 的凭据
uv sync
uv run main.py
```

[`.env.example`](.env.example) 包含 ASR 地址、LLM 和各 TTS 后端配置。对话与看摄像头共用 `OPENAI_MODEL`。
`main.py` 自动读取 `.env` 并覆盖同名进程环境变量，修改后重启服务生效。

配置台位于 `http://<板子 IP>:8080`。以可访问板上麦克风和桌面音频服务的用户运行，
在配置台选择正确的麦克风；系统还需安装 `libportaudio2` 和 `pulseaudio-utils`。
同机部署时，在本目录 `.env` 中配置
`ASR_SERVER_WS_URL=ws://127.0.0.1:8086/ws`，并准备所选 LLM / TTS 后端的个人配置。

## 声纹

配置台的当前语音消息旁提供“添加录入”：选择一句至少 1 秒有效语音，输入姓名并保存。
可选择已有姓名补充样本，每人保留最近 10 个不同样本；重复录入同一句不会增加样本。
聊天区右上角“声纹”可查看姓名、样本数并删除声纹。历史会话和纯文本消息不能录入。

姓名和归一化后的 embedding 保存在 **harness 本机**的 `data/history.sqlite3`，
使用 SQLAlchemy ORM 的 `Voiceprint` 模型及 `voiceprints` 表（`name`、`embeddings`、`updated_at`），
与聊天记录共用数据库，重启后自动读取。浏览器不保存声纹；ASR 只在当前连接内存中保留预设。
数据库及当前全部表在首次保存会话或录入声纹时自动创建。

harness 在 ASR WebSocket 建连后发送 `set_speaker_profiles`，收到确认后开始上传音频。
录入、删除后会全量更新预设，断线重连后重新发送；ASR 不会把预设写入磁盘。
声纹弹窗显示同步状态，保存成功但同步失败时会提示原因。

harness 直接使用 ASR `/ws` 的 STT 消息中的 `embedding`、`speech_ms` 和 `audio_url`，
无需重复请求 `/embedding`。需使用当前的 [asr-server](../asr-server/README.md)。
只有点击录入的样本会持久化；本轮最近 200 句的临时 embedding 留在内存，重开会话即清空。
embedding 不发给浏览器或 LLM。

说话人以 ASR 返回的 `speaker` 为准，SDK 原样写入用户消息 JSON 的 `speaker`，
页面显示并随聊天记录保存。harness 不进行二次声纹匹配，也不使用本地录入样本改写该字段。
录入同步成功后，从后续语音开始由 ASR 匹配并返回姓名，既有消息保持原结果；打字消息不带 `speaker`。

依赖已发布的 `aichat-sdk>=0.1.2`，执行 `uv sync` 即可安装，无需配置 `PYTHONPATH`。
预设同步由 harness 的 `PresetAsrClient` 接入，沿用 SDK 的音频收发和 STT 调度。

配置台内部接口（均由 harness 处理）：

- `POST /api/voiceprints`：`{"name":"小明","audio_url":"消息中的录音地址"}`，从当前缓存录入。
- `DELETE /api/voiceprints`：`{"name":"小明"}`，删除该姓名全部样本。
- `GET /api/state` 和 `/api/events` 的 `status.voiceprints` 返回姓名、样本数、`sync` 同步状态及当前消息的 ASR 说话人/录入状态，不含 embedding。

离线回归（模拟 ASR/LLM，不访问真实模型、麦克风或外部服务）：

```bash
uv run python -m unittest -v tests.test_voiceprint tests.test_asr_preset_sync tests.test_state_events tests.test_session_store
```

`test_asr_preset_sync` 会监听本机随机回环端口，使用真实 ASR WebSocket 协议与匹配逻辑、模拟 embedding，
验证建连、录入、删除和重连同步，不加载模型。

## TTS 配置

TTS 引擎选项从 SDK 的 `get_tts_engines()` 动态加载，项目使用 `aichat-sdk>=0.1.2`。
当前可选豆包 v1/v2、微软 Edge 和小米 MiMo；选择后保存配置即重启生效。
MiMo 需在 `.env` 配置 `MIMO_API_KEY`，可选 `MIMO_TTS_VOICE`（默认 `mimo_default`）
和 `MIMO_TTS_INSTRUCTIONS`。

启动前会检查所选 TTS 引擎的必填项。豆包 v1/v2 缺少 `BYTEDANCE_TTS_APP_ID` 或
`BYTEDANCE_TTS_APP_KEY`、MiMo 缺少 `MIMO_API_KEY` 时，页面会明确提示 TTS 未配置及缺失项，
填写 `.env` 后需重启服务；Edge 无需这些凭据。连接失败时分别提示 ASR 或所选 TTS 引擎，
不再把所有初始化失败统一显示为“ASR / TTS 连接失败”。

`config.json` 在启动时按当前配置格式校验，TTS 名称使用当前 SDK 目录。

本地 MCP 服务脚本集中放在 `mcp/`：

- `mcp/miloco_mcp.py`：米家设备和摄像头，读取 harness 根目录的 `.env`。
- `mcp/volume_mcp.py`：本机扬声器音量。

配置中的脚本路径相对于 harness 目录；MCP 服务列表以保存的配置为准。

## 聊天记录

会话收到结束事件（道别或空闲超时）后，`llm.messages` 中非 `system` 的消息自动保存到
`data/history.sqlite3`。点击“重新开始”、保存配置触发重启或正常退出时，
也会补存尚未保存的会话；只有系统提示词、没有聊天内容的会话不保存。
数据库及目录在首次保存时自动创建，使用 SQLAlchemy ORM 的 `ChatSession`、
`ChatMessage` 模型及事务管理。更新代码后执行 `uv sync` 安装 ORM 依赖。

`.env` 中的 `DEBUG` 默认是 `false`。设置 `DEBUG=true` 并重启服务后，
每轮会话还会在 `data/` 下保存一个 `<UTC开始时间>-<会话ID>.jsonl` 调试文件，
一行一条完整的 `llm.messages` 原始消息，包括 `system`、`tool_calls`、
`reasoning_content`、`audio_url` 等字段。同秒开始的会话也会各自保留文件。
关闭 `DEBUG` 仅停止生成新的 JSONL 文件，SQLite 聊天记录照常保存。

`sessions` 表保存唯一会话 ID、UTC 开始时间和结束时间；`messages` 表按
`session_id`、从 0 开始的连续 `position` 保存角色和正文，过滤所有 `role=system` 的消息。
`role=user` 的 `content` 为包含字符串 `text` 的 JSON 对象或 JSON 字符串时，
正文只保存 `text`，例如 `{"text":"查一下天气。","speaker":"unknown"}` 保存为 `查一下天气。`。
其他正文字符串原样保存，空值或缺省值存为 SQL NULL，结构化内容存为 JSON 文本；
正文不包含外层 `role`、`time`、`tool_calls`、`audio_url` 等字段。
token 用量保存在独立的 JSON 字段 `usage`，没有用量数据时为 SQL NULL。
发送时间保存在独立的文本字段 `time`，优先使用消息顶层 `time`，缺失时提取
`content` JSON 对象中的 `time`；保留原时间文本和时区偏移，没有时间时为 SQL NULL。
说话人保存在独立的文本字段 `speaker`，优先使用消息顶层 `speaker`，缺失时提取
`content` JSON 对象中的 `speaker`；没有说话人时为 SQL NULL。
表结构由新数据库创建，不自动迁移旧数据库。
历史摘要和详情直接使用已入库的正文文本，不再解析正文中的 JSON。
会话和消息在同一事务中写入，同一轮只保存一次。
录音文件仍由 ASR 服务管理。
该目录已在 Git 忽略规则中。

安装了 SQLite 命令行工具时，可在 harness 目录查询最近的会话：

```bash
sqlite3 data/history.sqlite3 'SELECT id, started_at, ended_at FROM sessions ORDER BY started_at DESC LIMIT 10;'
sqlite3 data/history.sqlite3 "SELECT time, speaker, content, usage FROM messages WHERE session_id = '会话ID' ORDER BY position;"
```

数据库和调试文件独立保存，写入失败会记录错误日志，不打断对话；
收到结束事件时保存失败的，会在后续重启或退出时重试已启用且尚未成功的存储。
强制杀进程或断电前尚未结束的会话不会补存。

配置台聊天区点击“历史会话”，可分页查看已保存的会话，按消息内容或会话 ID 搜索，
并打开会话查看按顺序保存的消息、开始/结束时间和 token 用量。
搜索包含用户、助手和工具消息；留空并点击“查询”即可刷新全部会话。
时间以浏览器本地时区显示，列表按开始时间从新到旧排列，每页 20 个会话。
查询直接读取 `data/history.sqlite3`，不依赖 `DEBUG` 或 JSONL 文件，也不打断当前对话。
尚未结束、尚未保存的会话仍在实时聊天区查看。

历史查询接口：

- `GET /api/sessions?q=关键词&limit=20&offset=0`：返回 `sessions` 摘要列表、`total`、`limit` 和 `offset`。
  `q` 同时匹配会话 ID 和任意消息内容，最长 200 字符；`limit` 为 1～100，`offset` 为非负整数。
- `GET /api/sessions/{session_id}`：返回 `session` 及按 `position` 排序的 `messages`，
  每条消息包含 `position`、`role`、`speaker`、`time`、`content`、`usage`。会话不存在时返回 404。

数据库尚不存在时返回空列表，不创建文件；数据库不可读时返回 503，详细原因记录在服务日志。

存储与会话生命周期测试（使用临时数据库和模拟 ChatBot，不访问音频设备或外部服务）：

```bash
uv run python -m unittest -v tests.test_session_store tests.test_session_query
```

## Skills 安装

配置台的 Skills 标题右侧点击“+ 安装”，支持两种方式：

- **上传安装**：选择 ZIP 技能包（含一个 `SKILL.md` 及其脚本、资源，可带外层目录），
  或直接选择 `SKILL.md`。文件最大 10 MB，ZIP 解压后最大 40 MB / 1000 个条目。
  技能安装到 SDK 的 `~/.agents/skills/<name>/`，同名技能或目录不会被覆盖。
- **命令安装**：输入单条安装命令，例如 `npx @larksuite/cli@latest install`，
  或点击“填入飞书安装命令”后执行。命令以 harness 当前用户身份、在 harness 目录运行，
  输出实时显示在页面；设备需要有 Node.js / npx 等对应工具。
  npx 的包下载确认自动同意，标准输入关闭，不支持终端交互、管道及 shell 拼接。
  命令最长运行 300 秒，超时停止整个进程组；需要交互的安装请改在终端运行。

`SKILL.md` 的 YAML frontmatter 必须包含 `name` 和非空 `description`；
上传技能的 name 限 1～64 位字母、数字、短横线、下划线，以字母或数字开头。
ZIP 上传会校验路径，拒绝目录穿越、符号链接、重复路径及加密或损坏的压缩包。
命令安装的文件位置及覆盖行为由该安装工具决定；页面完成后重新扫描 SDK 技能目录，
若没有新增技能会提示检查安装位置。安装不会自动重启当前对话；勾选所需技能，
点击“保存并重启”生效。页面保留尚未保存的配置和已有技能开关。

安装功能测试（使用临时目录及本地模拟命令，不安装真实软件）：

```bash
uv run python -m unittest -v tests.test_skill_install
```

## 扬声器音量 MCP

`mcp/volume_mcp.py` 使用 `pactl` 控制当前用户的 PulseAudio 默认输出设备。
需要系统安装 `pulseaudio-utils`，并以运行桌面音频服务的用户启动 harness。

`main.py` 启动时先把默认输出设备设到 `DEFAULT_SPEAKER_VOLUME`（80%），
所以每次重启都会覆盖上一轮对话调过的音量；`pactl` 缺失或音频服务没起来时
只记一条 warning，不影响对话启动。

“音量”服务默认启用，已有 `config.json` 缺少该项时也会自动补上。
如果在配置台明确设置 `enabled: false`，启动时会保留禁用状态。对应配置为：

```json
"音量": {"command": "python", "args": ["mcp/volume_mcp.py"]}
```

服务提供一个 `speaker_volume` 工具：

| action | percent | 用途 |
| --- | --- | --- |
| `get` | 不传 | 查询当前音量、静音状态 |
| `set` | 0～100，必填 | 设置绝对音量 |
| `increase` / `decrease` | 0～100，默认 10 | 调大 / 调小指定百分点，结果限制在 0～100% |
| `mute` / `unmute` | 不传 | 静音 / 取消静音，保留原音量 |

例如可以说“音量调到 50%”“小声一点”“静音”。设置或调整百分比保留静音状态，
需要出声时使用 `unmute`。控制对象是扬声器输出，不改变麦克风增益。
工具使用参数列表调用系统命令，执行超时会终止子进程；返回的是操作后的实测状态。

测试（模拟 PulseAudio，不改变实际音量）：

```bash
uv run python -m unittest -v tests.test_volume_mcp
```

## 测试

回归测试集中在 `tests/`，在本目录运行全部测试：

```bash
uv run python -m unittest discover -v
```
