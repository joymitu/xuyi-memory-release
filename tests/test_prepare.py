"""首次运行准备：语义依赖（pip）+ 量化中文模型（多源下载、逐个校验）。

不联网：模型源用本机 HTTP 服务假装，pip 用注入的安装函数。
"""
import hashlib
import json
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from xuyi import prepare


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(folder):
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(folder)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.good = root / "good"
        self.bad = root / "bad"
        files = {"model.onnx": b"onnx-bytes" * 1000, "tokenizer.json": b'{"t":1}', "config.json": b"{}"}
        self.manifest = {"repo": "x/y", "revision": "r", "files": {}}
        for name, data in files.items():
            (self.good / "m").mkdir(parents=True, exist_ok=True)
            (self.good / "m" / name).write_bytes(data)
            (self.bad / "m").mkdir(parents=True, exist_ok=True)
            (self.bad / "m" / name).write_bytes(b"tampered")
            self.manifest["files"][name] = {"path": name, "sha256": hashlib.sha256(data).hexdigest(),
                                            "bytes": len(data)}
        self.servers = []
        for folder in (self.good, self.bad):
            server, url = serve(folder)
            self.addCleanup(server.shutdown)
            self.servers.append(url)
        self.installs = []

    def pip(self, site, packages, index):
        self.installs.append((index, packages))
        Path(site).mkdir(parents=True, exist_ok=True)
        (Path(site) / "installed.txt").write_text("ok", encoding="utf-8")

    def run_prepare(self, sources, deps_ready=False, pip=None):
        return prepare.prepare(self.home, manifest=self.manifest, model_sources=sources,
                               indexes=["https://mirror-a/simple", "https://mirror-b/simple"],
                               pip_install=pip or self.pip, deps_check=lambda site: deps_ready)

    def test_downloads_from_the_first_source_that_verifies_and_writes_a_manifest(self):
        good, bad = self.servers
        missing = "http://127.0.0.1:9/nothing"
        out = self.run_prepare([missing + "/m/{path}", bad + "/m/{path}", good + "/m/{path}"])
        self.assertTrue(out["model"])
        model = prepare.model_dir(self.home)
        for name, meta in self.manifest["files"].items():
            data = (model / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), meta["sha256"])
        # OnnxEmbedder 读 source.json 校验每个文件：清单要写成它认的格式。
        written = json.loads((model / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(set(written["files"]), set(self.manifest["files"]))
        self.assertFalse(list(model.glob("*.part")))
        status = prepare.status(self.home, deps_check=lambda site: True, manifest=self.manifest)
        self.assertTrue(status["model"])

    def test_all_sources_failing_leaves_no_half_file_and_records_the_error(self):
        _, bad = self.servers
        out = self.run_prepare([bad + "/m/{path}"])
        self.assertFalse(out["model"])
        self.assertIn("error", out)
        model = prepare.model_dir(self.home)
        self.assertFalse(model.exists() and any(model.iterdir()))
        progress = json.loads(prepare.progress_file(self.home).read_text(encoding="utf-8"))
        self.assertEqual(progress["stage"], "failed")
        self.assertTrue(progress["error"])

    def test_dependencies_install_once_trying_mirrors_in_order(self):
        good, _ = self.servers
        calls = []

        def flaky(site, packages, index):
            calls.append(index)
            if index.endswith("mirror-a/simple"):
                raise RuntimeError("mirror down")
            self.pip(site, packages, index)

        out = self.run_prepare([good + "/m/{path}"], pip=flaky)
        self.assertTrue(out["deps"])
        self.assertEqual(calls, ["https://mirror-a/simple", "https://mirror-b/simple"])
        self.assertIn("onnxruntime==1.31.1", self.installs[0][1])
        # 已经装好就不再装。
        self.installs.clear()
        self.run_prepare([good + "/m/{path}"], deps_ready=True)
        self.assertEqual(self.installs, [])

    def test_progress_reports_done_and_the_site_dir_for_the_adapter(self):
        good, _ = self.servers
        self.run_prepare([good + "/m/{path}"])
        progress = json.loads(prepare.progress_file(self.home).read_text(encoding="utf-8"))
        self.assertEqual(progress["stage"], "done")
        self.assertEqual(progress["percent"], 100)
        self.assertEqual(Path(progress["site"]), prepare.site_dir(self.home))
        self.assertEqual(Path(progress["model_dir"]), prepare.model_dir(self.home))

    def test_the_shipped_manifest_pins_the_quantized_model(self):
        files = prepare.MODEL_MANIFEST["files"]
        self.assertEqual(files["model.onnx"]["sha256"],
                         "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc")
        self.assertEqual(files["model.onnx"]["path"], "onnx/model_quantized.onnx")
        self.assertEqual(sum(f["bytes"] for f in files.values()) // 1_000_000, 24)
        self.assertTrue(all("{path}" in source for source in prepare.MODEL_SOURCES))


if __name__ == "__main__":
    unittest.main()
