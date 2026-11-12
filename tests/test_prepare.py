"""首次运行准备：语义依赖（pip）+ 量化中文模型（多源下载、逐个校验）。

不联网：模型源用本机 HTTP 服务假装，pip 用注入的安装函数。
"""
import hashlib
import io
import json
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
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
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            self.servers.append(url)
        self.installs = []

    def pip(self, site, packages, index):
        self.installs.append((index, packages))
        Path(site).mkdir(parents=True, exist_ok=True)
        (Path(site) / "installed.txt").write_text("ok", encoding="utf-8")

    def run_prepare(self, sources, deps_ready=False, pip=None, deps_check=None, inference_check=None, **kwargs):
        return prepare.prepare(self.home, manifest=self.manifest, model_sources=sources,
                               indexes=["https://mirror-a/simple", "https://mirror-b/simple"],
                               pip_install=pip or self.pip,
                               deps_check=deps_check or (lambda site: deps_ready or (site / "installed.txt").is_file()),
                               inference_check=inference_check or (lambda site, model: True), **kwargs)

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
        status = prepare.status(self.home, deps_check=lambda site: True, manifest=self.manifest,
                                inference_check=lambda site, model: True)
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
        self.assertTrue(any(package.startswith("onnxruntime==") for package in self.installs[0][1]))
        # 已经装好就不再装。
        self.installs.clear()
        self.run_prepare([good + "/m/{path}"], deps_ready=True)
        self.assertEqual(self.installs, [])

    def test_progress_reports_done_and_the_site_dir_for_the_adapter(self):
        good, _ = self.servers
        path = self.home / "custom-data" / "prepare.json"
        self.run_prepare([good + "/m/{path}"], progress_path=path, run_id="current")
        progress = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(progress["stage"], "done")
        self.assertEqual(progress["percent"], 100)
        self.assertEqual(progress["run_id"], "current")
        self.assertEqual(Path(progress["site"]), prepare.site_dir(self.home))
        self.assertEqual(Path(progress["model_dir"]), prepare.model_dir(self.home))
        self.assertFalse(prepare.progress_file(self.home).exists())

    def test_successful_pip_install_is_not_ready_if_dependencies_cannot_load(self):
        out = self.run_prepare([], deps_check=lambda site: False)
        self.assertFalse(out["deps"])
        self.assertFalse(out["model"])
        self.assertIn("无法加载", out["error"])
        self.assertEqual(len(self.installs), 1)
        self.assertEqual(json.loads(prepare.progress_file(self.home).read_text(encoding="utf-8"))["stage"], "failed")

    def test_macos_library_validation_switches_install_and_inference_to_the_same_isolated_python(self):
        good, _ = self.servers
        site = prepare.site_dir(self.home)
        site.mkdir(parents=True)
        isolated = self.home / "isolated"
        python = isolated / "bin" / "python3.12"
        checked, installed, inferred = [], [], []

        def check(path, *, python=None):
            if python is None:
                return prepare.deps_ready(path)
            checked.append(python)
            return (path / "installed").exists()

        def install(path, packages, index, *, python=None):
            installed.append((path, packages, python))
            path.mkdir(parents=True)
            (path / "installed").touch()

        def inference(path, model, *, python=None):
            inferred.append((path, model, python))
            return True

        with patch.object(prepare.sys, "platform", "darwin"), patch.object(prepare.subprocess, "run") as run, \
                patch.object(prepare, "mac_semantic_runtime", return_value=(python, isolated)) as runtime:
            run.return_value.returncode = 1
            run.return_value.stderr = b"mapping process and mapped file have different Team IDs"
            out = self.run_prepare([good + "/m/{path}"], deps_check=check, pip=install, inference_check=inference)
        runtime.assert_called_once_with(self.home, install=True)
        self.assertTrue(out["model"])
        self.assertEqual(checked, [python, python])
        self.assertEqual(installed, [(isolated, prepare.MAC_PACKAGES, python)])
        self.assertEqual(inferred, [(isolated, prepare.model_dir(self.home), python)])
        progress = json.loads(prepare.progress_file(self.home).read_text(encoding="utf-8"))
        self.assertEqual((progress["stage"], progress["backend"], progress["site"]),
                         ("done", "mac-standalone", str(isolated)))

    def test_standalone_archive_is_verified_before_extraction_and_reused(self):
        good, bad = self.servers
        filename = "cpython-3.12.16+20261110-aarch64-apple-darwin-install_only_stripped.tar.gz"
        archive = self.good / prepare.MAC_RUNTIME_RELEASE / filename
        archive.parent.mkdir()
        with tarfile.open(archive, "w:gz") as dist:
            entry = tarfile.TarInfo("python/bin/python3.12")
            content = b"synthetic test runtime"
            entry.mode = 0o755
            entry.size = len(content)
            dist.addfile(entry, io.BytesIO(content))
        corrupted = self.bad / prepare.MAC_RUNTIME_RELEASE / filename
        corrupted.parent.mkdir()
        corrupted.write_bytes(b"unverified")
        meta = ("aarch64", archive.stat().st_size, hashlib.sha256(archive.read_bytes()).hexdigest())
        with patch.object(prepare.sys, "platform", "darwin"), patch.object(prepare.platform, "machine", return_value="arm64"), \
                patch.dict(prepare.MAC_RUNTIME_ASSETS, {"arm64": meta}), \
                patch.object(prepare, "MAC_RUNTIME_SOURCES", (bad,)):
            with self.assertRaisesRegex(RuntimeError, "下载失败"):
                prepare.mac_semantic_runtime(self.home, install=True)
            with patch.object(prepare, "MAC_RUNTIME_SOURCES", (bad, good)):
                python, site = prepare.mac_semantic_runtime(self.home, install=True)
            self.assertEqual(python.read_bytes(), content)
            self.assertEqual(site.parent, python.parents[2])
            self.assertEqual(prepare.mac_semantic_runtime(self.home), (python, site))

    def test_fresh_hardened_python_skips_wheel_install_for_the_host(self):
        from subprocess import CompletedProcess

        def signed(entitlements=b""):
            return CompletedProcess([], 0, entitlements, b"CodeDirectory flags=0x10000(runtime) hashes=2")

        with patch.object(prepare.sys, "platform", "darwin"), patch.object(prepare.subprocess, "run") as run:
            run.return_value = signed()
            with self.assertRaises(prepare.MacLibraryValidationError):
                prepare._host_deps_ready(prepare.site_dir(self.home), lambda _: False)
            run.return_value = signed(b"<key>com.apple.security.cs.disable-library-validation</key><true/>")
            self.assertFalse(prepare._host_deps_ready(prepare.site_dir(self.home), lambda _: False))

    def test_cached_files_must_pass_inference_on_every_run(self):
        good, _ = self.servers
        self.assertTrue(self.run_prepare([good + "/m/{path}"])["model"])
        self.installs.clear()

        def failed_inference(site, model):
            progress = json.loads(prepare.progress_file(self.home).read_text(encoding="utf-8"))
            self.assertEqual(progress["stage"], "verify")
            self.assertEqual(model, prepare.model_dir(self.home))
            return False

        out = self.run_prepare([], inference_check=failed_inference)
        self.assertTrue(out["deps"])
        self.assertFalse(out["model"])
        self.assertIn("推理检查", out["error"])
        self.assertEqual(self.installs, [])
        self.assertEqual(json.loads(prepare.progress_file(self.home).read_text(encoding="utf-8"))["stage"], "failed")

    def test_the_shipped_manifest_pins_the_quantized_model(self):
        files = prepare.MODEL_MANIFEST["files"]
        self.assertEqual(files["model.onnx"]["sha256"],
                         "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc")
        self.assertEqual(files["model.onnx"]["path"], "onnx/model_quantized.onnx")
        self.assertEqual(sum(f["bytes"] for f in files.values()) // 1_000_000, 24)
        self.assertTrue(all("{path}" in source for source in prepare.MODEL_SOURCES))


if __name__ == "__main__":
    unittest.main()
