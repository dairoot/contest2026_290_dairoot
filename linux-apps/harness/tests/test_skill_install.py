"""技能上传、命令生命周期及 HTTP 接口回归；不修改真实 skills 目录。"""

import asyncio
import copy
import io
from pathlib import Path
import shlex
import stat
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from starlette.testclient import TestClient

import skill_install
from skill_install import install_skill, run_skill_command

with patch("dotenv.load_dotenv"):
    import main

from server import create_app


def manifest(name="demo"):
    return f"---\nname: {name}\ndescription: Test skill\n---\nRead scripts/run.py.\n".encode()


def archive(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for name, content in files:
            z.writestr(name, content)
    return data.getvalue()


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.skills = Path(self.temp.name) / "skills"

    def test_zip_keeps_resources_and_executable_scripts(self):
        script = zipfile.ZipInfo("repo/skills/demo/scripts/run.py")
        script.external_attr = (stat.S_IFREG | 0o755) << 16
        data = archive([("repo/skills/demo/SKILL.md", manifest()), (script, b"print('ok')")])
        result = install_skill("demo.zip", data, self.skills, set())
        self.assertEqual(result["name"], "demo")
        self.assertEqual(Path(result["location"]).read_bytes(), manifest())
        executable = self.skills / "demo/scripts/run.py"
        self.assertEqual(executable.read_bytes(), b"print('ok')")
        self.assertTrue(executable.stat().st_mode & 0o111)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.skills])

    def test_standalone_and_duplicate_do_not_overwrite(self):
        install_skill("SKILL.md", manifest(), self.skills, set())
        with self.assertRaisesRegex(ValueError, "已存在"):
            install_skill("SKILL.md", manifest(), self.skills, set())
        self.assertEqual((self.skills / "demo/SKILL.md").read_bytes(), manifest())
        with self.assertRaisesRegex(ValueError, "已存在"):
            install_skill("SKILL.md", manifest("other"), self.skills, {"other"})
        self.assertFalse((self.skills / "other").exists())

    def test_invalid_uploads_leave_no_skill(self):
        cases = [
            ("wrong.txt", manifest()), ("SKILL.md", b""), ("SKILL.md", b"no metadata"),
            ("SKILL.md", manifest("../escape")), ("SKILL.md", manifest("[a, b]")),
            ("SKILL.md", b"---\nname: demo\ndescription: [bad\n---"),
            ("SKILL.md", b"\xff"), ("bad.zip", b"broken"),
            ("empty.zip", archive([("README.md", b"empty")])),
            ("multi.zip", archive([("a/SKILL.md", manifest()), ("b/SKILL.md", manifest("b"))])),
        ]
        for filename, content in cases:
            with self.subTest(filename=filename, content=content[:40]), self.assertRaises(ValueError):
                install_skill(filename, content, self.skills, set())
            self.assertFalse(self.skills.exists())

    def test_archive_paths_symlinks_and_limits(self):
        symlink = zipfile.ZipInfo("link")
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        for path in ("../outside", "/absolute", "C:/outside", "a\\escape", symlink):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "不安全"):
                install_skill("demo.zip", archive([("SKILL.md", manifest()), (path, b"target")]), self.skills, set())
        with patch.object(skill_install, "MAX_UPLOAD_BYTES", 10), self.assertRaisesRegex(ValueError, "10 MB"):
            install_skill("SKILL.md", manifest(), self.skills, set())
        with patch.object(skill_install, "MAX_UNPACKED_BYTES", 10), self.assertRaisesRegex(ValueError, "40 MB"):
            install_skill("demo.zip", archive([("SKILL.md", manifest())]), self.skills, set())
        self.assertFalse(self.skills.exists())


class CommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_output_unicode_exit_code_and_literal_arguments(self):
        output = []
        command = shlex.join([sys.executable, "-c", "import sys; print('中文', sys.argv[1]); sys.exit(3)", "$(touch /tmp/should-not-run)"])
        code = await run_skill_command(command, tempfile.gettempdir(), output.append)
        self.assertEqual(code, 3)
        self.assertIn("中文 $(touch /tmp/should-not-run)", "".join(output))

    async def test_noninteractive_environment_and_working_directory(self):
        output = []
        script = "import os, sys; print(os.getcwd(), os.environ['npm_config_yes'], repr(sys.stdin.read()))"
        with tempfile.TemporaryDirectory() as temp:
            code = await run_skill_command(shlex.join([sys.executable, "-c", script]), temp, output.append)
            self.assertEqual(code, 0)
            self.assertIn(f"{Path(temp).resolve()} true ''", "".join(output))

    async def test_invalid_missing_and_timed_out_commands(self):
        for command in ("", [], "echo '", "echo yes && echo no", "/missing-command"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                await run_skill_command(command, tempfile.gettempdir(), lambda text: None)
        with patch.object(skill_install, "COMMAND_TIMEOUT", 0.1), self.assertRaisesRegex(ValueError, "已停止"):
            await run_skill_command(shlex.join([sys.executable, "-c", "import time; time.sleep(30)"]), tempfile.gettempdir(), lambda text: None)


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.skills = Path(self.temp.name) / "skills"
        self.client = main.AIClient(copy.deepcopy(main.DEFAULT_CONFIG))
        for target, value in (("SKILLS_DIR", self.skills), ("load_skills", self.scan)):
            mock = patch.object(main, target, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.web = TestClient(create_app(self.client))
        self.addCleanup(self.web.close)

    def scan(self):
        return [{"name": path.parent.name, "description": "Test skill", "location": str(path)}
                for path in self.skills.glob("*/SKILL.md")]

    def test_upload_refreshes_list_without_restarting_or_saving(self):
        with patch.object(self.client, "restart", new=AsyncMock()) as restart, patch.object(main, "save_config") as save:
            response = self.web.post("/api/skills/install?filename=SKILL.md", content=manifest())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["skills"][0]["name"], "demo")
        self.assertEqual(self.web.get("/api/state").json()["status"]["skills"][0]["name"], "demo")
        restart.assert_not_awaited()
        save.assert_not_called()
        self.assertEqual(self.web.post("/api/skills/install?filename=SKILL.md", content=manifest()).status_code, 400)
        with patch("server.MAX_UPLOAD_BYTES", 10):
            self.assertEqual(self.web.post("/api/skills/install?filename=SKILL.md", content=manifest()).status_code, 413)

    def test_command_success_failure_invalid_json_and_concurrency(self):
        async def run(command, cwd, on_output):
            on_output("installed\n")
            install_skill("SKILL.md", manifest(), self.skills, set())
            return 0

        with patch.object(main, "run_skill_command", new=AsyncMock(side_effect=run)):
            response = self.web.post("/api/skills/command", json={"command": "npx @larksuite/cli@latest install"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["installed"], ["demo"])
        self.assertIn("installed", response.json()["output"])
        self.assertFalse(self.client.live_status()["skill_install"]["running"])
        with patch.object(main, "run_skill_command", new=AsyncMock(return_value=2)):
            response = self.web.post("/api/skills/command", json={"command": "bad-install"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("退出码 2", response.json()["error"])
        for content in (b"bad json", b"[]"):
            self.assertEqual(self.web.post("/api/skills/command", content=content).status_code, 400)
        asyncio.run(self.client._skill_install_lock.acquire())
        try:
            response = self.web.post("/api/skills/command", json={"command": "npx example"})
            self.assertEqual(response.status_code, 400)
            self.assertIn("正在安装", response.json()["error"])
        finally:
            self.client._skill_install_lock.release()


if __name__ == "__main__":
    unittest.main()
