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

## TTS 配置

TTS 引擎选项从 SDK 的 `get_tts_engines()` 动态加载，要求 `aichat-sdk>=0.1.1`。
当前可选豆包 v1/v2、微软 Edge 和小米 MiMo；选择后保存配置即重启生效。
MiMo 需在 `.env` 配置 `MIMO_API_KEY`，可选 `MIMO_TTS_VOICE`（默认 `mimo_default`）
和 `MIMO_TTS_INSTRUCTIONS`。

旧页面保存的 `bytedance`、`v3` 启动时分别迁移为 `bytedancev1`、`bytedancev2`，
保存后写回 `config.json`。若曾手动填写 SDK 0.1.0 的 `v2` / `bytedancev2`，
升级后需改选 `bytedancev1` 才能保持原引擎；SDK 0.1.1 的 `v2` 现在指豆包 v2。

本地 MCP 服务脚本集中放在 `mcp/`：

- `mcp/miloco_mcp.py`：米家设备和摄像头，读取 harness 根目录的 `.env`。
- `mcp/volume_mcp.py`：本机扬声器音量。

配置中的脚本路径相对于 harness 目录。搬目录前保存的这两个脚本旧路径会在
启动时自动迁移；保存配置后写入 `config.json`。

## 聊天记录

会话收到结束事件（道别或空闲超时）后，`llm.messages` 中非 `system` 的消息自动保存到
`data/history.sqlite3`。点击“重新开始”、保存配置触发重启或正常退出时，
也会补存尚未保存的会话；只有系统提示词、没有聊天内容的会话不保存。
数据库及目录在首次保存时自动创建，使用 SQLAlchemy ORM 的 `ChatSession`、
`ChatMessage` 模型及事务管理。更新代码后执行 `uv sync` 安装 ORM 依赖。
已有历史记录时，停止 harness 后将旧的 `sessions/` 目录重命名为 `data/`。

`.env` 中的 `DEBUG` 默认是 `false`。设置 `DEBUG=true` 并重启服务后，
每轮会话还会在 `data/` 下保存一个 `<UTC开始时间>-<会话ID>.jsonl` 调试文件，
一行一条完整的 `llm.messages` 原始消息，包括 `system`、`tool_calls`、
`reasoning_content`、`audio_url` 等字段。同秒开始的会话也会各自保留文件。
关闭 `DEBUG` 仅停止生成新的 JSONL 文件，SQLite 聊天记录照常保存。

`sessions` 表保存唯一会话 ID、UTC 开始时间和结束时间；`messages` 表按
`session_id`、从 0 开始的连续 `position` 保存角色和原消息的 `content` 值，
过滤所有 `role=system` 的消息。`content` 字符串原样保存，空值或缺省值存为 SQL NULL，
结构化内容存为 JSON 文本；不再包含外层 `role`、`time`、`tool_calls`、`audio_url` 等字段。
若原 `content` 是包含 `text`、`time`、`round` 的 JSON 字符串，也会原样保留。
token 用量保存在独立的 JSON 字段 `usage`，没有用量数据时为 SQL NULL。
会话和消息在同一事务中写入，同一轮只保存一次。
录音文件仍由 ASR 服务管理。
该目录已在 Git 忽略规则中。

安装了 SQLite 命令行工具时，可在 harness 目录查询最近的会话：

```bash
sqlite3 data/history.sqlite3 'SELECT id, started_at, ended_at FROM sessions ORDER BY started_at DESC LIMIT 10;'
sqlite3 data/history.sqlite3 "SELECT content, usage FROM messages WHERE session_id = '会话ID' ORDER BY position;"
```

数据库和调试文件独立保存，写入失败会记录错误日志，不打断对话；
收到结束事件时保存失败的，会在后续重启或退出时重试已启用且尚未成功的存储。
强制杀进程或断电前尚未结束的会话不会补存。

存储与会话生命周期测试（使用临时数据库和模拟 ChatBot，不访问音频设备或外部服务）：

```bash
uv run python -m unittest -v tests.test_session_store
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
