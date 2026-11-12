"""首次运行准备：让装进 DSH 的续忆也能用中文语义检索。

自动召回本来就是词法、不用模型；这里准备的是模型主动查记忆（xuyi_recall）时的语义部分：
1. 语义依赖：先用 DSH 自带的 Python。macOS 阻止它加载原生库时，下载并校验独立 Python，
   只在独立进程中使用这套依赖，DSH 自己的运行时不变。
2. 量化中文模型：bge-small-zh-v1.5 的 int8 版（24MB），
   多个源按顺序试，每个文件 sha256 校验；全部校验通过才放进 ~/.xuyi/models/，不留半截文件。
进度写在 ~/.xuyi/data/prepare.json，给日记本的设置面板显示。失败不影响聊天（照样词法）。
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from functools import partial
from pathlib import Path

REVISION = "75c43b069aac4d136ba6bc1122f995fedcfd2781"
MODEL_MANIFEST = {
    "repo": "Xenova/bge-small-zh-v1.5",
    "revision": REVISION,
    "upstream": "BAAI/bge-small-zh-v1.5",
    "quantized": "int8（上游 onnx/model_quantized.onnx）",
    "files": {
        "model.onnx": {"path": "onnx/model_quantized.onnx", "bytes": 24010842,
                       "sha256": "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc"},
        "tokenizer.json": {"path": "tokenizer.json", "bytes": 439125,
                           "sha256": "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26"},
        "tokenizer_config.json": {"path": "tokenizer_config.json", "bytes": 367,
                                  "sha256": "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a"},
        "config.json": {"path": "config.json", "bytes": 716,
                        "sha256": "d4193ead3a810fd694fa8a31d7fc72fbaebc0668b603e398734bf2f6538ff42f"},
    },
}
# 国内能直连的在前；每个文件都按 sha256 校验，哪个源给的不对就换下一个。
MODEL_SOURCES = [
    f"https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/{REVISION}/{{path}}",
    "https://www.modelscope.cn/api/v1/models/Xenova/bge-small-zh-v1.5/repo?FilePath={path}",
    f"https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/{REVISION}/{{path}}",
]
PIP_INDEXES = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://pypi.org/simple",
]
PACKAGES = ["onnxruntime==1.31.1", "tokenizers==0.23.3"]

# macOS 13 also needs a compatible ONNX wheel. Keep these out of the DSH Python site.
MAC_PACKAGES = ["numpy==2.5.4", "onnxruntime==1.23.2", "tokenizers==0.23.3"]
MAC_RUNTIME_RELEASE = "20261110"
MAC_RUNTIME_ASSETS = {
    "arm64": ("aarch64", 25237581, "2f11b8aa3726bdbd87dd9eb76568412faddf4f47e26913fa45d6a2e424972acd"),
    "x86_64": ("x86_64", 24957926, "b78a4c479f1964ec484880bee567568266f68da102269fb93a05fe8716b45f91"),
}
MAC_RUNTIME_SOURCES = (
    "https://mirrors.aliyun.com/github/releases/astral-sh/python-build-standalone",
    "https://github.com/astral-sh/python-build-standalone/releases/download",
)


class MacLibraryValidationError(RuntimeError):
    """The host's signed interpreter cannot load third-party native wheels."""


def _host_deps_ready(site, check):
    if check(site):
        return True
    # On a fresh install the import cannot reach dyld yet. Inspect the host
    # signature first to avoid installing the same native wheels twice.
    if sys.platform == "darwin":
        try:
            signature = subprocess.run(
                ["/usr/bin/codesign", "-d", "--verbose=2", "--entitlements", ":-", sys.executable],
                capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        flags = next((line.partition(b"flags=")[2].split(b" hashes=", 1)[0]
                      for line in signature.stderr.splitlines() if b"flags=" in line), b"")
        if (signature.returncode == 0 and b"runtime" in flags
                and b"com.apple.security.cs.disable-library-validation" not in signature.stdout + signature.stderr):
            raise MacLibraryValidationError("macOS 拒绝当前 Python 加载语义检索依赖")
    return False


def mac_runtime_dir(home, arch):
    return Path(home) / ".xuyi" / "semantic-runtime" / f"{MAC_RUNTIME_RELEASE}-{arch}"


def mac_semantic_runtime(home, *, install=False):
    arch = platform.machine()
    asset = MAC_RUNTIME_ASSETS.get(arch) if sys.platform == "darwin" else None
    if not asset:
        raise RuntimeError("当前 Mac 架构没有可用的中文语义检索运行时")
    folder = mac_runtime_dir(home, arch)
    python = folder / "python" / "bin" / "python3.12"
    if not python.is_file() and install:
        target, size, digest = asset
        name = f"cpython-3.12.16%2B{MAC_RUNTIME_RELEASE}-{target}-apple-darwin-install_only_stripped.tar.gz"
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".download-", dir=folder) as tmp:
            archive = Path(tmp) / "python.tar.gz"
            for source in MAC_RUNTIME_SOURCES:
                try:
                    _fetch(f"{source}/{MAC_RUNTIME_RELEASE}/{name}", archive, digest, size, lambda _: None)
                    break
                except (OSError, ValueError):
                    archive.unlink(missing_ok=True)
            else:
                raise RuntimeError("Mac 中文语义检索运行时下载失败，请检查网络后重启 DSH")
            with tarfile.open(archive) as dist:
                dist.extractall(tmp, filter="data")
            extracted = Path(tmp) / "python"
            if not (extracted / "bin" / "python3.12").is_file():
                raise RuntimeError("Mac 中文语义检索运行时不完整")
            try:
                extracted.rename(folder / "python")
            except OSError:
                if not python.is_file():
                    raise
    if not python.is_file():
        raise RuntimeError("Mac 中文语义检索运行时尚未准备好")
    return python, folder / "site"


def site_dir(home):
    return Path(home) / ".xuyi" / "py-site"


def model_dir(home):
    return Path(home) / ".xuyi" / "models" / "bge-small-zh-q8"


def progress_file(home):
    return Path(home) / ".xuyi" / "data" / "prepare.json"


def _write_progress(home, path, run_id, **data):
    path.parent.mkdir(parents=True, exist_ok=True)
    data.setdefault("site", str(site_dir(home)))
    data.update(model_dir=str(model_dir(home)), at=time.strftime("%Y-%m-%dT%H:%M:%S"), run_id=run_id)
    stream = tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False)
    tmp = Path(stream.name)
    try:
        with stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def deps_ready(site, *, python=None):
    """在一个干净的子进程里试着导入（不污染当前进程）。"""
    if not Path(site).is_dir():
        return False
    env = {**os.environ, "PYTHONPATH": str(site), "PYTHONNOUSERSITE": "1"}
    if python:
        env.pop("PYTHONHOME", None)
    try:
        done = subprocess.run([python or sys.executable, "-s", "-c", "import numpy, onnxruntime, tokenizers"],
                              env=env, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    stderr = done.stderr.lower()
    if sys.platform == "darwin" and done.returncode and (
            b"different team ids" in stderr or b"library load disallowed" in stderr):
        raise MacLibraryValidationError("macOS 拒绝当前 Python 加载语义检索依赖")
    return done.returncode == 0


def check_inference(site, model, *, python=None):
    """用同一个解释器加载正式模型，并实际执行一次推理。"""
    source = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(map(str, (source, site))), "PYTHONNOUSERSITE": "1"}
    if python:
        env.pop("PYTHONHOME", None)
    code = ("import sys; from xuyi.embeddings import OnnxEmbedder; "
            "OnnxEmbedder(sys.argv[1]).encode(['检索测试'], query=True)")
    try:
        done = subprocess.run([python or sys.executable, "-s", "-c", code, str(model)],
                              env=env, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def pip_install(site, packages, index, *, python=None):
    env = {**os.environ, "PYTHONNOUSERSITE": "1"}
    env.pop("PYTHONPATH", None)
    if python:
        env.pop("PYTHONHOME", None)
    done = subprocess.run(
        [python or sys.executable, "-m", "pip", "install", "--target", str(site), "--upgrade", "--no-deps",
         "--only-binary=:all:", "--disable-pip-version-check", "--no-input", "-i", index, *packages],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    if done.returncode:
        raise RuntimeError((done.stderr or done.stdout).strip().splitlines()[-1:][0] if (done.stderr or done.stdout).strip() else "pip 失败")


def model_ready(home, manifest=MODEL_MANIFEST):
    folder = model_dir(home)
    try:
        written = json.loads((folder / "source.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if written.get("files", {}).keys() != manifest["files"].keys():
        return False
    return all((folder / name).is_file() and (folder / name).stat().st_size == meta["bytes"]
               for name, meta in manifest["files"].items())


def status(home, deps_check=deps_ready, manifest=MODEL_MANIFEST, inference_check=check_inference):
    try:
        progress = json.loads(progress_file(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        progress = None
    site, model = site_dir(home), model_dir(home)
    result = {"deps": False, "model": False,
              "site": str(site), "model_dir": str(model), "progress": progress}
    try:
        try:
            result["deps"] = _host_deps_ready(site, deps_check)
        except MacLibraryValidationError:
            python, site = mac_semantic_runtime(home)
            result.update(site=str(site), backend="mac-standalone", deps=bool(deps_check(site, python=python)))
            inference_check = partial(inference_check, python=python)
        if result["deps"] and model_ready(home, manifest):
            result["model"] = bool(inference_check(site, model))
    except RuntimeError as exc:
        result["error"] = str(exc)
    return result


def _fetch(url, target, expect_sha, expect_bytes, report):
    request = urllib.request.Request(url, headers={"User-Agent": "xuyi-memory"})
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=60) as response, open(target, "wb") as out:
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > expect_bytes * 2 + 1024:
                raise ValueError("文件比预期大太多")
            digest.update(chunk)
            out.write(chunk)
            report(size)
    if size != expect_bytes or digest.hexdigest() != expect_sha:
        raise ValueError("校验不符")


def _download_model(home, manifest, sources, progress):
    folder = model_dir(home)
    staging = folder.parent / (folder.name + ".part")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    total = sum(meta["bytes"] for meta in manifest["files"].values())
    done_bytes = 0
    try:
        for name, meta in manifest["files"].items():
            target = staging / name
            reasons = []
            for source in sources:
                url = source.replace("{path}", meta["path"])
                try:
                    _fetch(url, target, meta["sha256"], meta["bytes"],
                           lambda got: progress(done_bytes + got, total))
                    break
                except Exception as exc:  # 换下一个源；最后汇总原因
                    reasons.append(f"{url.split('/')[2]}: {type(exc).__name__}")
                    target.unlink(missing_ok=True)
            else:
                raise RuntimeError(f"{name} 下载失败（{'；'.join(reasons)}）")
            done_bytes += meta["bytes"]
        written = {key: value for key, value in manifest.items() if key != "files"}
        written["files"] = {name: {"sha256": meta["sha256"], "bytes": meta["bytes"], "path": meta["path"]}
                            for name, meta in manifest["files"].items()}
        (staging / "source.json").write_text(json.dumps(written, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.rmtree(folder, ignore_errors=True)
        os.replace(staging, folder)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _ensure_dependencies(site, packages, indexes, install, check):
    if check(site):
        return
    reasons = []
    for index in indexes:
        try:
            install(site, packages, index)
            break
        except Exception as exc:
            reasons.append(f"{index.split('/')[2]}: {str(exc)[:120]}")
    else:
        raise RuntimeError("语义依赖安装失败（" + "；".join(reasons) + "）")
    if not check(site):
        raise RuntimeError("当前 Python 无法加载已安装的语义检索依赖")


def prepare(home=None, manifest=MODEL_MANIFEST, model_sources=MODEL_SOURCES, indexes=PIP_INDEXES,
            pip_install=pip_install, deps_check=deps_ready, inference_check=check_inference,
            progress_path=None, run_id=None):
    home = Path(home or Path.home())
    path = Path(progress_path) if progress_path else progress_file(home)

    backend = None
    site = site_dir(home)

    def write_progress(**data):
        if backend:
            data.update(backend=backend, site=str(site))
        _write_progress(home, path, run_id, **data)

    result = {"deps": False, "model": False}
    try:
        write_progress(stage="deps", percent=0)
        try:
            packages = MAC_PACKAGES[1:] if sys.platform == "darwin" else PACKAGES
            _ensure_dependencies(site, packages, indexes, pip_install, partial(_host_deps_ready, check=deps_check))
        except MacLibraryValidationError:
            backend = "mac-standalone"
            write_progress(stage="runtime", percent=10)
            python, site = mac_semantic_runtime(home, install=True)
            _ensure_dependencies(site, MAC_PACKAGES, indexes, partial(pip_install, python=python),
                                 partial(deps_check, python=python))
            inference_check = partial(inference_check, python=python)
        result["deps"] = True
        write_progress(stage="model", percent=40)
        if not model_ready(home, manifest):
            last = [0.0]

            def report(got, total):
                now = time.monotonic()
                if now - last[0] > 0.5:
                    last[0] = now
                    write_progress(stage="model", percent=40 + int(50 * got / max(total, 1)))

            _download_model(home, manifest, model_sources, report)
        write_progress(stage="verify", percent=95)
        if not inference_check(site, model_dir(home)):
            raise RuntimeError("中文模型未通过本地推理检查")
        result["model"] = True
        if backend:
            result["backend"] = backend
        write_progress(stage="done", percent=100)
    except Exception as exc:
        result["error"] = str(exc)[:400]
        write_progress(stage="failed", percent=0, error=result["error"])
    return result
