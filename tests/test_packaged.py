"""装进 DSH 的 npm 包形态：只有 DSH 自带的 Python（无 PyYAML、无 onnxruntime），续忆仍要能存、能查。

真机发现（2026-11-09）：DSH 自带 Python 3.12 没有 PyYAML，ingest 在读人设那一步崩掉；
没有语义模型时 xuyi_recall 直接报错。这里用 `python -S`（不加载 site-packages）模拟那个环境。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

PATCH = """- id: im-qqbot
  name: '@tencent-connect/dsh-qqbot'
  config:
    directPrompt: |
      你是小雅，说话有点傲娇。
"""

EVENT = {"scope": "qq:c2c:t", "event_id": "m1", "sender_id": "u", "role": "user",
         "origin": "quoted", "recorded_at": "2026-11-09T10:00:00+08:00",
         "text": "我最喜欢的饮料是茶"}


def bare_run(pythonpath, db, args, data=b"", profile=None, no_site=True):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(PYTHONPATH=str(pythonpath), PYTHONUTF8="1")
    if profile:
        env["XUYI_PROFILE_DIR"] = str(profile)
    flags = ["-S"] if no_site else []
    return subprocess.run([sys.executable, *flags, "-m", "xuyi", "--db", str(db), *args],
                          input=data, capture_output=True, env=env, timeout=120,
                          cwd=tempfile.gettempdir())


class BareInterpreterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.profile = self.dir / "profile"
        self.profile.mkdir()
        (self.profile / "cordis.patch.yml").write_text(PATCH, encoding="utf-8")
        self.db = self.dir / "m.sqlite"

    def test_ingest_survives_missing_yaml(self):
        out = bare_run(ROOT, self.db, ["ingest", "-"],
                       json.dumps([EVENT], ensure_ascii=False).encode("utf-8"), self.profile)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        self.assertIn("inserted", out.stdout.decode("utf-8"))

    def test_recall_falls_back_to_lexical_when_model_is_missing(self):
        bare_run(ROOT, self.db, ["ingest", "-"], json.dumps([EVENT], ensure_ascii=False).encode("utf-8"))
        out = bare_run(ROOT, self.db, ["recall", "qq:c2c:t", "--focus", "--model",
                                       str(self.dir / "no-model"), "--", "饮料"], no_site=False)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        result = json.loads(out.stdout.decode("utf-8"))
        self.assertIn("茶", result["context"])
        self.assertTrue(any("词法" in w for w in result["warnings"]), result["warnings"])

    def test_recall_falls_back_to_lexical_without_semantic_packages(self):
        model = ROOT / "models/bge-small-zh"
        if not (model / "model.onnx").is_file():
            self.skipTest("本机没有中文模型")
        bare_run(ROOT, self.db, ["ingest", "-"], json.dumps([EVENT], ensure_ascii=False).encode("utf-8"))
        out = bare_run(ROOT, self.db, ["recall", "qq:c2c:t", "--focus", "--model", str(model), "--", "饮料"])
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        self.assertIn("茶", json.loads(out.stdout.decode("utf-8"))["context"])


class PackStageTests(unittest.TestCase):
    def test_stage_carries_python_core_and_vendored_yaml_but_no_private_data(self):
        from pack_adapter import stage
        with tempfile.TemporaryDirectory() as tmp:
            out = stage(Path(tmp) / "stage")
            py = out / "py"
            for rel in ["index.js", "client.js", "rhythm.js", "cordis.patch.yml", "package.json",
                        "py/xuyi/__init__.py", "py/xuyi/board.html", "py/xuyi/fonts/OFL.txt",
                        "py/yaml/__init__.py", "py/yaml/LICENSE"]:
                self.assertTrue((out / rel).is_file(), rel)
            self.assertTrue(list((py / "xuyi/personas").glob("*.json")))
            self.assertTrue(list((py / "xuyi/fonts").glob("*.ttf")))
            names = [p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()]
            for name in names:
                self.assertNotIn("__pycache__", name)
                self.assertFalse(name.endswith((".pyd", ".so", ".pyc", ".sqlite", ".onnx")), name)
                self.assertFalse(name.startswith(("data/", "models/", "py/data/")), name)
            manifest = json.loads((out / "package.json").read_text(encoding="utf-8"))
            self.assertIn("py", manifest["files"])
            # 包里的代码在没有 site-packages 的解释器上能存、能读人设（yaml 来自包内）。
            profile = Path(tmp) / "profile"
            profile.mkdir()
            (profile / "cordis.patch.yml").write_text(PATCH, encoding="utf-8")
            db = Path(tmp) / "m.sqlite"
            ingest = bare_run(py, db, ["ingest", "-"],
                              json.dumps([EVENT], ensure_ascii=False).encode("utf-8"), profile)
            self.assertEqual(ingest.returncode, 0, ingest.stderr.decode("utf-8", "replace"))
            env = {k: v for k, v in os.environ.items() if k != "PYTHONHOME"}
            env.update(PYTHONPATH=str(py), XUYI_PROFILE_DIR=str(profile), PYTHONUTF8="1")
            probe = subprocess.run([sys.executable, "-S", "-c",
                                    "import yaml, xuyi.board_bridge as b; print(yaml.__file__); "
                                    "print(b.find_persona()['text'])"],
                                   capture_output=True, env=env, timeout=60, cwd=tempfile.gettempdir())
            self.assertEqual(probe.returncode, 0, probe.stderr.decode("utf-8", "replace"))
            lines = probe.stdout.decode("utf-8").splitlines()
            self.assertTrue(Path(lines[0]).resolve().is_relative_to(py.resolve()), lines[0])
            self.assertIn("小雅", lines[1])

    def test_generated_text_files_use_lf_on_every_platform(self):
        # Windows 上 write_text 默认写 CRLF，打出的包会和 Mac 验过的包不一致。
        from pack_adapter import stage
        with tempfile.TemporaryDirectory() as tmp:
            out = stage(Path(tmp) / "stage")
            for rel in ["package.json", "py/yaml/LICENSE", "py/yaml/VERSION"]:
                self.assertNotIn(b"\r", (out / rel).read_bytes(), rel)


class ReleaseContentTests(unittest.TestCase):
    """要公开发布的包：插件页要的展示信息齐全，且不带任何本机/个人信息。"""

    def test_package_has_listing_metadata_license_and_no_personal_traces(self):
        import re
        from pack_adapter import stage
        with tempfile.TemporaryDirectory() as tmp:
            out = stage(Path(tmp) / "stage")
            for rel in ["README.md", "LICENSE", "icon.png", "locale/zh.json", "locale/en.json", "images/diary.png"]:
                self.assertTrue((out / rel).is_file(), rel)
            manifest = json.loads((out / "package.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["license"], "MIT")
            self.assertIn("dsh-plugin", manifest["keywords"])
            self.assertEqual(manifest["icon"], "./icon.png")
            for rel in manifest["files"]:
                self.assertTrue((out / rel).exists(), rel)
            for lang in ("zh", "en"):
                meta = json.loads((out / f"locale/{lang}.json").read_text(encoding="utf-8"))["meta"]
                self.assertTrue(meta["title"] and meta["description"])
            self.assertIn("MIT", (out / "LICENSE").read_text(encoding="utf-8"))
            # 本机用户目录不能出现在包里（仓库地址里的 GitHub 用户名是要公开的，不算）。
            personal = re.compile(r"[A-Za-z]:[\/]+Users[\/]+\w|/Users/\w|/home/\w|Documents[\/]+xuyi", re.I)
            for path in out.rglob("*"):
                if path.is_file() and path.suffix in {".py", ".js", ".json", ".md", ".html", ".yml", ".txt"}:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    self.assertIsNone(personal.search(text), f"{path.relative_to(out)}: {personal.search(text)}")


class ManifestLinkTests(unittest.TestCase):
    RELEASE = "https://github.com/joymitu/xuyi-memory-release"

    def test_npm_links_point_at_the_clean_release_repository(self):
        # 研究仓库不公开；npm 页面上的链接只能指向干净发布仓库。
        manifest = json.loads((ROOT / "dsh-adapter/package.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["repository"], {"type": "git", "url": f"git+{self.RELEASE}.git"})
        self.assertEqual(manifest["homepage"], f"{self.RELEASE}#readme")
        self.assertEqual(manifest["bugs"], {"url": f"{self.RELEASE}/issues"})

    def test_market_index_repository_follows_the_manifest(self):
        from make_market_index import build
        plugin = build(1794240000)["plugins"][0]
        self.assertEqual(plugin["repository"], {"url": self.RELEASE})
        self.assertEqual(plugin["homepage"], f"{self.RELEASE}#readme")


class MarketIndexTests(unittest.TestCase):
    def test_index_follows_dsh_format_1_and_points_at_the_npm_package(self):
        from make_market_index import build
        doc = build(1794240000)
        self.assertEqual(doc["format"], 1)
        self.assertIn("en", doc["title"])
        plugin = doc["plugins"][0]
        manifest = json.loads((ROOT / "dsh-adapter/package.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["name"], manifest["name"])
        self.assertEqual(plugin["versions"][0], {"version": manifest["version"], "publishTimestamp": 1794240000,
                                                 "source": {"type": "npm"}})
        self.assertTrue(plugin["icon"].startswith("data:image/png;base64,"))
        self.assertIn(plugin["metadata"]["category"], doc["metadata"]["categories"])
        self.assertTrue(set(plugin["metadata"]["labels"]) <= set(doc["metadata"]["labels"]))
        for field in ("title", "description"):
            self.assertIn("en", plugin[field])


class PublishedIndexTests(unittest.TestCase):
    """仓库里的 market/index.json 是用户添加的公开地址，必须和当前 manifest 一致。"""

    def test_committed_index_matches_the_current_manifest(self):
        from make_market_index import build
        path = ROOT / "market/index.json"
        self.assertNotIn(b"\r", path.read_bytes())
        committed = json.loads(path.read_text(encoding="utf-8"))
        stamp = committed["plugins"][0]["versions"][0]["publishTimestamp"]
        self.assertEqual(committed, build(stamp),
                         "改了版本或简介后要重新运行 scripts/make_market_index.py")

    def test_readme_points_at_the_committed_index(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("https://raw.githubusercontent.com/joymitu/xuyi-memory-release/main/market/index.json", text)


class PrivacyWordingTests(unittest.TestCase):
    def test_readmes_do_not_claim_nothing_leaves_the_computer(self):
        # 召回的原话、写日记用的当天聊天会交给用户配置的模型，不能写「不上传」。
        for rel in ("README.md", "dsh-adapter/README.md"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("不上传", text, rel)
            self.assertIn("你配置的模型", text, rel)


if __name__ == "__main__":
    unittest.main()
