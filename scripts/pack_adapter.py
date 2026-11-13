"""把适配器打成能直接装进 DSH 的 npm 包：JS 适配器 + Python 核心 + 纯 Python 版 PyYAML。

装进 DSH 之后，适配器用 DSH 自带的 Python 跑包里的 `py/`，用户不用装 Python、不用建虚拟环境。
不打进包的：语义模型（91MB，首次运行后另行下载）、data/（真实聊天）、编译产物。

用法：python scripts/pack_adapter.py            # 产出 packages/dsh-xuyi-memory-<版本>.tgz
      python scripts/pack_adapter.py --stage-only # 只生成 build/adapter/ 目录
"""
import argparse
import json
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_FILES = ["index.js", "logic.js", "client.js", "rhythm.js", "cordis.patch.yml", "package.json",
                 "icon.png", "locale/zh.json", "locale/en.json"]
REPO_URL = "https://github.com/joymitu/xuyi-memory-release"
# README 里用到的图（都在 docs/images/ 下），打包时一起带上，npm 页面从 jsDelivr 取。
README_IMAGES = ["eat.png", "diary.png", "memory.png"]


def package_readme(version):
    """npm 页面用的 README：就是仓库首页那份，只把相对路径换成绝对地址。

    npm 不会从安装包里取图片，相对路径的图在 npm 页面上是空的；图片改用 jsDelivr 上
    这个版本的包里的文件（国内也打得开）。LICENSE 和字体许可证的路径也按包里的位置改。
    """
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    cdn = f"https://cdn.jsdelivr.net/npm/dsh-xuyi-memory@{version}/images/"
    replacements = {
        "](docs/images/": f"]({cdn}",
        "](LICENSE)": f"]({REPO_URL}/blob/main/LICENSE)",
        "`xuyi/fonts/OFL.txt`": "`py/xuyi/fonts/OFL.txt`",
    }
    for old, new in replacements.items():
        if old not in text:
            raise SystemExit(f"README 里找不到 {old}，打包前先核对 package_readme 的替换规则")
        text = text.replace(old, new)
    return text
SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".pyd", ".so", ".dylib"}


def _copy_tree(src, dst, keep=lambda path: True):
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part in SKIP_DIRS for part in rel.parts) or path.suffix in SKIP_SUFFIXES:
            continue
        if path.is_file() and keep(path):
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def _yaml_source():
    import yaml
    folder = Path(yaml.__file__).resolve().parent
    license_text = None
    dist = metadata.distribution("PyYAML")
    for name in ("licenses/LICENSE", "LICENSE"):
        for file in dist.files or []:
            if file.as_posix().endswith(name):
                license_text = Path(dist.locate_file(file)).read_text(encoding="utf-8")
                break
        if license_text:
            break
    if not license_text:
        raise SystemExit("找不到 PyYAML 的许可证文件，拒绝打包（MIT 要求随附许可证）")
    return folder, license_text, dist.version


def stage(out=None):
    out = Path(out or ROOT / "build/adapter")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name in ADAPTER_FILES:
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "dsh-adapter" / name, out / name)
    shutil.copyfile(ROOT / "LICENSE", out / "LICENSE")
    version = json.loads((ROOT / "dsh-adapter/package.json").read_text(encoding="utf-8"))["version"]
    (out / "README.md").write_text(package_readme(version), encoding="utf-8", newline="\n")
    # README 里的示例截图（虚构数据，scripts/demo_screenshot.py 生成）。
    (out / "images").mkdir()
    for image in README_IMAGES:
        shutil.copyfile(ROOT / "docs/images" / image, out / "images" / image)
    py = out / "py"
    _copy_tree(ROOT / "xuyi", py / "xuyi")
    folder, license_text, version = _yaml_source()
    # 只要纯 Python 部分；yaml/__init__ 找不到 C 扩展时自动用纯 Python 实现。
    _copy_tree(folder, py / "yaml", keep=lambda path: path.suffix == ".py")
    # newline="\n"：Windows 上打包也写 LF，与 Mac 打出的包逐字节一致。
    (py / "yaml/LICENSE").write_text(license_text, encoding="utf-8", newline="\n")
    (py / "yaml/VERSION").write_text(f"PyYAML {version}（纯 Python 部分，MIT）\n", encoding="utf-8", newline="\n")
    manifest_path = out / "package.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = sorted(set(manifest.get("files", [])) | {"py"})
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument("--stage-dir", help="暂存目录（默认 build/adapter）")
    parser.add_argument("--out", default=str(ROOT / "packages"))
    args = parser.parse_args()
    out = stage(args.stage_dir)
    if args.stage_only:
        print(out)
        return 0
    dest = Path(args.out)
    dest.mkdir(parents=True, exist_ok=True)
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise SystemExit("找不到 npm")
    done = subprocess.run([npm, "pack", "--pack-destination", str(dest)], cwd=out,
                          capture_output=True, text=True, encoding="utf-8")
    if done.returncode:
        sys.stderr.write(done.stderr)
        return done.returncode
    print(dest / done.stdout.strip().splitlines()[-1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
