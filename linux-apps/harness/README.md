# harness

在本目录运行 `uv run main.py`，配置台位于 `http://<板子 IP>:8080`。

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
uv run python -m unittest -v test_skill_install
```

## 扬声器音量 MCP

`mcp/volume_mcp.py` 使用 `pactl` 控制当前用户的 PulseAudio 默认输出设备。
需要系统安装 `pulseaudio-utils`，并以运行桌面音频服务的用户启动 harness。

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
uv run python -m unittest -v test_volume_mcp
```
