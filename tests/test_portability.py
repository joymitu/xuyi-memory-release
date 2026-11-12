"""跨平台护栏：Windows 上虚拟环境路径不同、标准流默认不是 UTF-8。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from xuyi.doctor import venv_python

ROOT = Path(__file__).resolve().parents[1]


class VenvPythonTests(unittest.TestCase):
    def test_platform_specific_interpreter_path(self):
        self.assertEqual(venv_python("/p", "win32").as_posix(), "/p/.venv/Scripts/python.exe")
        self.assertEqual(venv_python("/p", "darwin").as_posix(), "/p/.venv/bin/python")


class CliUtf8Tests(unittest.TestCase):
    def test_chinese_roundtrips_through_stdio_under_legacy_codepage(self):
        # 模拟宿主（Node）以 UTF-8 字节写入、读回，而子进程处在非 UTF-8 的默认编码下。
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        env.update(PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
        event = {"scope": "qq:c2c:t", "event_id": "m1", "sender_id": "u", "role": "user",
                 "origin": "human_direct", "recorded_at": "2026-11-09T10:00:00+08:00",
                 "text": "我家的猫叫年糕"}
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "m.sqlite")
            run = lambda args, data=b"": subprocess.run(
                [sys.executable, "-m", "xuyi", "--db", db, *args], input=data,
                capture_output=True, cwd=ROOT, env=env, timeout=60)
            ingest = run(["ingest", "-"], json.dumps([event], ensure_ascii=False).encode("utf-8"))
            self.assertEqual(ingest.returncode, 0, ingest.stderr.decode("utf-8", "replace"))
            got = run(["get", "qq:c2c:t", "m1"])
            self.assertEqual(got.returncode, 0, got.stderr.decode("utf-8", "replace"))
            self.assertIn("我家的猫叫年糕", got.stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()


class RegisterTests(unittest.TestCase):
    def test_register_writes_the_pointer_the_adapter_reads_and_doctor_sees_it(self):
        from xuyi.doctor import doctor, register, registered_project
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as root:
            check = lambda: next(c for c in doctor(root, home)['检查'] if c['检查'] == '宿主能找到本项目')
            self.assertFalse(check()['通过'])
            self.assertIn('xuyi register', check()['说明'])
            out = register(root, home)
            self.assertEqual(registered_project(home), Path(root).resolve())
            data = json.loads((Path(home) / '.xuyi/project.json').read_text(encoding='utf-8'))
            self.assertEqual(Path(data['projectDir']), Path(root).resolve())  # 适配器读同一个键
            self.assertTrue(check()['通过'])
            self.assertEqual(out['状态'], '已登记')
