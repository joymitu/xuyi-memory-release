import subprocess
from pathlib import Path

import unittest

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = (".py", ".js", ".mjs", ".json", ".html", ".md", ".toml", ".yml", ".yaml", ".css", ".txt")


class EncodingTests(unittest.TestCase):
    """源码不能带 UTF-8 BOM。

    Windows PowerShell 5.1 的 `Set-Content -Encoding UTF8` 会偷偷加 BOM：
    Python 能容忍，但 Node 的 JSON.parse / JS 加载会崩。真机踩过：profile 的 package.json
    被写成带 BOM，DSH 启动第一步就挂了。
    """

    def test_tracked_sources_have_no_bom(self):
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True)
        if out.returncode:
            self.skipTest("不在 git 仓库里")
        bad = []
        for name in out.stdout.decode("utf-8").split("\0"):
            if not name or not name.endswith(SUFFIXES):
                continue
            path = ROOT / name
            if not path.is_file():
                continue
            if path.read_bytes()[:3] == b"\xef\xbb\xbf":
                bad.append(name)
        self.assertEqual(bad, [], f"这些文件带 BOM，用无 BOM 写出：{bad}")


if __name__ == "__main__":
    unittest.main()
