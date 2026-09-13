"""把上传的技能包校验后放入 SDK 技能目录，不执行包内脚本。"""

import asyncio
import io
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import stat
import tempfile
import zipfile

import yaml

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UNPACKED_BYTES = 40 * 1024 * 1024
MAX_FILES = 1000
COMMAND_TIMEOUT = 300
MAX_OUTPUT_CHARS = 32000


async def run_skill_command(command: str, cwd: str, on_output) -> int:
    """执行单条命令（不经过 shell），输出交给页面；超时/取消时清理整个进程组。"""
    if not isinstance(command, str) or not command.strip() or len(command) > 4096:
        raise ValueError("请输入安装命令（最多 4096 字符）")
    try:
        args = shlex.split(command)
    except ValueError as e:
        raise ValueError("命令引号不匹配") from e
    if not args or any(token in {"&&", "||", ";", "|", ">", ">>", "<", "&"} for token in args):
        raise ValueError("请填写一条安装命令，不支持管道或多条命令拼接")
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "npm_config_yes": "true", "NO_COLOR": "1"},
            start_new_session=True,
        )
    except OSError as e:
        raise ValueError(f"无法启动 {args[0]}，请确认已安装并在 PATH 中：{e}") from e

    async def read_output():
        import codecs
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while chunk := await proc.stdout.read(4096):
            on_output(decoder.decode(chunk))
        on_output(decoder.decode(b"", final=True))
        return await proc.wait()

    try:
        return await asyncio.wait_for(read_output(), COMMAND_TIMEOUT)
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        if isinstance(e, asyncio.CancelledError):
            raise
        raise ValueError(f"安装超过 {COMMAND_TIMEOUT} 秒，已停止；需要交互的命令请在终端运行") from e


def _unpack(data: bytes, target: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES or sum(info.file_size for info in entries) > MAX_UNPACKED_BYTES:
            raise ValueError("ZIP 解压后不能超过 40 MB 或 1000 个文件")
        seen = set()
        for info in entries:
            path = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            if (path.is_absolute() or ".." in path.parts or "\\" in info.filename
                    or ":" in info.filename or "\x00" in info.orig_filename
                    or not path.parts or stat.S_ISLNK(mode)
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError("ZIP 包含不安全的路径或特殊文件")
            if path in seen:
                raise ValueError("ZIP 包含重复路径")
            seen.add(path)
            output = target.joinpath(*path.parts)
            if info.is_dir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(archive.read(info))
                # 保留脚本的执行位，其余权限统一为当前用户可写、其他用户只读。
                output.chmod(0o755 if mode & 0o111 else 0o644)


def _metadata(path: Path) -> dict:
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---") or (end := content.find("---", 3)) == -1:
        raise ValueError("SKILL.md 需要 YAML frontmatter，包含 name 和 description")
    try:
        metadata = yaml.safe_load(content[3:end])
    except yaml.YAMLError as e:
        raise ValueError("SKILL.md 的 YAML frontmatter 格式错误") from e
    if not isinstance(metadata, dict):
        raise ValueError("SKILL.md 的 YAML frontmatter 必须是对象")
    name, description = metadata.get("name"), metadata.get("description")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("技能 name 需为 1～64 位字母、数字、短横线或下划线，且以字母或数字开头")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("SKILL.md 需要非空的 description 文本")
    return {"name": name, "description": description}


def install_skill(filename: str, data: bytes, skills_dir: Path, existing_names: set[str]) -> dict:
    """只接受单个技能；先完整校验再原子移动，不覆盖任何已有目录。"""
    if not data:
        raise ValueError("请选择非空的技能文件")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("技能文件不能超过 10 MB")
    if filename != "SKILL.md" and not filename.lower().endswith(".zip"):
        raise ValueError("请选择 ZIP 技能包或 SKILL.md 文件")
    skills_dir = Path(skills_dir).expanduser()
    try:
        skills_dir.parent.mkdir(parents=True, exist_ok=True)
        # 临时目录放在技能目录外，避免 SDK 扫到尚未安装的 SKILL.md。
        with tempfile.TemporaryDirectory(prefix=".skill-upload-", dir=skills_dir.parent) as temp:
            unpacked = Path(temp) / "content"
            unpacked.mkdir()
            if filename == "SKILL.md":
                (unpacked / "SKILL.md").write_bytes(data)
            else:
                _unpack(data, unpacked)
            manifests = list(unpacked.rglob("SKILL.md"))
            if len(manifests) != 1:
                raise ValueError("每个 ZIP 必须包含且仅包含一个 SKILL.md，请按技能分别上传")
            manifest = manifests[0]
            metadata = _metadata(manifest)
            destination = skills_dir / metadata["name"]
            if metadata["name"] in existing_names or os.path.lexists(destination):
                raise ValueError(f"技能「{metadata['name']}」已存在，未覆盖已有文件")
            skills_dir.mkdir(parents=True, exist_ok=True)
            manifest.parent.rename(destination)
            return {**metadata, "location": str(destination / "SKILL.md")}
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as e:
        raise ValueError("ZIP 无法读取，请使用未加密的有效 ZIP 技能包") from e
    except UnicodeError as e:
        raise ValueError("SKILL.md 必须使用 UTF-8 编码") from e
    except OSError as e:
        raise ValueError(f"安装失败，无法读写技能目录：{e}") from e
