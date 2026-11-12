"""一条命令装好续忆：找宿主 profile → 备份 → 装适配器 → 建环境 → 登记 → 选人设。

只做确定性的步骤，每一步都能单独重跑；任何写入宿主之前先备份。
用法：python -m xuyi setup [--profile 目录] [--persona 预设名] [--yes]
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_RANGE = (3, 11, 3, 13)


def find_profile(explicit=None, home=None):
    """找到真正配置了 QQ 机器人的那个 profile。显式给了就用它，不再猜。"""
    home = Path(home or Path.home())
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    else:
        env = os.environ.get("XUYI_PROFILE_DIR")
        if env:
            candidates.append(Path(env))
        default = home / ".dsh" / "profiles" / "desktop"
        if default.is_dir():
            candidates.append(default)
        base = home / ".dsh" / "profiles"
        if base.is_dir():
            candidates += sorted((folder for folder in base.iterdir() if folder.is_dir()),
                                 key=lambda folder: (folder / "cordis.patch.yml").stat().st_mtime
                                 if (folder / "cordis.patch.yml").is_file() else 0, reverse=True)
    for folder in candidates:
        patch = folder / "cordis.patch.yml"
        if not patch.is_file():
            continue
        try:
            text = patch.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "im-qqbot" in text:
            return folder
    return None


def backup_profile(profile, home=None):
    """写宿主之前，先把要动的两个文件复制走。"""
    home = Path(home or Path.home())
    dest = home / ".xuyi" / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("cordis.patch.yml", "package.json"):
        source = Path(profile) / name
        if source.is_file():
            shutil.copy2(source, dest / name)
    return dest


def newest_adapter(packages=None):
    folder = Path(packages or ROOT / "packages")

    def version(path):
        try:
            return tuple(int(part) for part in path.stem.split("-")[-1].split("."))
        except ValueError:
            return (0,)

    found = sorted(folder.glob("dsh-xuyi-memory-*.tgz"), key=version, reverse=True)
    return found[0] if found else None


def install_adapter(profile, tgz):
    """把 tgz 放进 vendor、解压进 node_modules，并更新 profile 的 package.json。"""
    profile = Path(profile)
    vendor = profile / "vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tgz, vendor / tgz.name)
    target = profile / "node_modules" / "dsh-xuyi-memory"
    with tempfile.TemporaryDirectory() as temp:
        with tarfile.open(tgz) as archive:
            archive.extractall(temp, filter="data")
        package = Path(temp) / "package"
        if not package.is_dir():
            raise RuntimeError("适配器包结构不对：缺 package/ 目录")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(package, target)
    update_package_json(profile, tgz.name)
    return target


def update_package_json(profile, tgz_name):
    path = Path(profile) / "package.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"name": "dsh-profile", "private": True}
    dependencies = data.setdefault("dependencies", {})
    dependencies["dsh-xuyi-memory"] = f"file:vendor/{tgz_name}"
    profile_cfg = data.setdefault("dsh", {}).setdefault("profile", {})
    bundles = profile_cfg.setdefault("bundles", [])
    if "dsh-xuyi-memory" not in bundles:
        bundles.append("dsh-xuyi-memory")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def model_ready(root=None):
    return (Path(root or ROOT) / "models" / "bge-small-zh" / "model.onnx").is_file()


def ensure_venv(venv=None, python=None):
    """没有虚拟环境就建一个并装依赖；已经在里面就返回 None。"""
    venv = Path(venv or ROOT / ".venv")
    if Path(sys.prefix).resolve() == venv.resolve():
        return None
    python = python or sys.executable
    if not venv.exists():
        subprocess.check_call([python, "-m", "venv", str(venv)])
    target = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.check_call([str(target), "-m", "pip", "install", "-q", "--upgrade", "pip"])
    subprocess.check_call([str(target), "-m", "pip", "install", "-q",
                           "-r", str(ROOT / "requirements-semantic-tested.txt"), "pyyaml", "-e", str(ROOT)])
    return target


def run(profile_dir=None, persona=None, yes=False, home=None, root=None,
        skip_adapter=False, skip_env=False, skip_register=False, skip_persona=False,
        check_location=True, log=print):
    """返回每一步的结果，供 CLI 打印和测试断言。"""
    root = Path(root or ROOT)
    result = {"步骤": [], "项目目录": str(root)}

    major, minor = sys.version_info[:2]
    if not (11 <= minor <= 13 and major == 3):
        raise RuntimeError(f"需要 Python 3.11–3.13，现在是 {major}.{minor}")
    if check_location and os.name == "nt" and "appdata" in str(root).lower():
        raise RuntimeError("项目不能放在 AppData 下：会被宿主虚拟化，续忆调用会全部失败")

    if not skip_env:
        launched = ensure_venv()
        if launched:
            log(f"已建好虚拟环境：{launched}")
            log("请用同一条命令再跑一次（这次会用虚拟环境）。")
            result["步骤"].append({"步骤": "虚拟环境", "结果": str(launched), "下一步": "重跑 setup"})
            return result
        result["步骤"].append({"步骤": "虚拟环境", "结果": "已就绪"})
    else:
        result["步骤"].append({"步骤": "虚拟环境", "结果": "跳过"})

    profile = find_profile(profile_dir, home=home)
    if not profile:
        raise RuntimeError("没找到配置了 QQ 机器人的 DSH profile；用 --profile 指定")
    result["profile"] = str(profile)

    backup = backup_profile(profile, home=home)
    result["步骤"].append({"步骤": "备份", "结果": str(backup)})

    if not skip_adapter:
        tgz = newest_adapter(root / "packages")
        if not tgz:
            raise RuntimeError("packages/ 里没有适配器 tgz，先在 dsh-adapter/ 下 npm pack")
        target = install_adapter(profile, tgz)
        result["步骤"].append({"步骤": "适配器", "结果": f"{tgz.name} → {target}"})
    else:
        result["步骤"].append({"步骤": "适配器", "结果": "跳过"})

    if not model_ready(root):
        log("没找到中文语义模型（models/bge-small-zh）。仍可用，但中文召回会退回关键词。")
        result["步骤"].append({"步骤": "模型", "结果": "缺失"})
    else:
        result["步骤"].append({"步骤": "模型", "结果": "就绪"})

    if not skip_register:
        from .doctor import register
        result["步骤"].append({"步骤": "登记", "结果": register(root, home=home)["登记文件"]})
    else:
        result["步骤"].append({"步骤": "登记", "结果": "跳过"})

    if persona and not skip_persona:
        from .personas import apply_persona
        applied = apply_persona(persona, profile, backup=False)
        result["步骤"].append({"步骤": "人设", "结果": applied["applied"]})
    else:
        result["步骤"].append({"步骤": "人设", "结果": "跳过（用 xuyi persona <名> --apply 随时设置）"})

    log("")
    log("装好了。下一步：")
    log("1. 完全退出 DSH，从开始菜单重新启动；")
    log("2. 用手机 QQ 发一条消息，确认她能回；")
    log(f"3. 看日记：{root}/data/board-launch.cmd（Windows）或 python -m xuyi --db data/dsh-qq.sqlite board")
    return result
