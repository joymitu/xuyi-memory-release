"""Setup doctor: what stands between install and a working companion.

One read-only command that checks every layer — Python env, semantic model,
archive, DSH profile wiring, QQ binding, persona — and prints exactly what
is missing, in install order. Never modifies anything.
"""
import json
import shutil
import sys
from pathlib import Path

from .personas import list_personas


def _check(name, ok, detail="", blocking=True):
    return {"检查": name, "通过": bool(ok), "说明": detail, "阻塞": blocking}


def venv_python(root, platform=None):
    """项目虚拟环境的解释器路径：Windows 是 Scripts/python.exe，其余是 bin/python。"""
    windows = (platform or sys.platform) == "win32"
    return Path(root) / (".venv/Scripts/python.exe" if windows else ".venv/bin/python")


def registered_project(home):
    """适配器找项目目录用的登记文件（~/.xuyi/project.json）。"""
    try:
        return Path(json.loads((Path(home) / '.xuyi/project.json').read_text(encoding='utf-8'))['projectDir'])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def register(project_root=None, home=None):
    """写登记文件；这是 doctor 之外唯一会写用户目录的安装步骤，只写这一个文件。"""
    home = Path(home or Path.home())
    root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
    target = home / '.xuyi/project.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({'projectDir': str(root)}, ensure_ascii=False), encoding='utf-8')
    return {'状态': '已登记', '项目目录': str(root), '登记文件': str(target)}


def doctor(project_root=None, home=None):
    home = Path(home or Path.home())
    root = Path(project_root or Path(__file__).resolve().parents[1])
    checks = []
    python = venv_python(root)
    checks.append(_check("Python 虚拟环境", python.is_file(), str(python)))
    legacy = (home / 'Documents/Codex/xuyi-memory').resolve()
    registered = registered_project(home)
    found = root.resolve() in {legacy, registered.resolve() if registered else None}
    checks.append(_check('宿主能找到本项目', found,
                         str(root) if found else '未登记：运行 xuyi register（或设环境变量 XUYI_PROJECT_DIR）'))
    model = root / "models/bge-small-zh/model.onnx"
    checks.append(_check("中文语义模型", model.is_file(),
                         "可选；缺失时用词法模式" if not model.is_file() else str(model),
                         blocking=False))
    db = root / "data/dsh-qq.sqlite"
    checks.append(_check("记忆档案", db.is_file(),
                         "首次私聊后自动创建" if not db.is_file() else str(db),
                         blocking=False))
    profile = home / ".dsh/profiles/desktop"
    adapter = profile / "node_modules/dsh-xuyi-memory"
    checks.append(_check("DSH 适配器已安装", adapter.exists(),
                         str(adapter) if adapter.exists() else "pnpm install 未完成"))
    try:
        manifest = json.loads((adapter / "package.json").read_text(encoding="utf-8"))
        checks.append(_check("适配器版本", True, manifest.get("version", "?")))
    except OSError:
        pass
    prefs = home / ".dsh-qqbot/model-prefs.json"
    bound = []
    if prefs.is_file():
        try:
            mapping = json.loads(prefs.read_text(encoding="utf-8")).get("sessionIds", {})
            bound = [key for key in mapping if ":c2c:" in key]
        except (ValueError, OSError):
            pass
    checks.append(_check("QQ 绑定（私聊）", bool(bound),
                         bound[0] if bound else "DSH 内扫码后自动写入"))
    personas = list_personas()
    checks.append(_check("人设预设", bool(personas), f"{len(personas)} 个可用"))
    return {"就绪": all(item["通过"] for item in checks if item["阻塞"]), "检查": checks,
            "下一步": _next_step(checks)}


def _next_step(checks):
    for item in checks:
        if not item["通过"] and item["阻塞"]:
            if "虚拟环境" in item["检查"]:
                return "在项目目录执行: python3 -m venv .venv && 用其中的 pip 安装 '.[semantic]'（Windows 为 .venv/Scripts/pip）"
            if '宿主能找到' in item['检查']:
                return '在项目目录执行: python -m xuyi register'
            if "语义模型" in item["检查"]:
                return "按 README 下载 bge-small-zh（可跳过，词法模式可用）"
            if "适配器" in item["检查"]:
                return "在 ~/.dsh/profiles/desktop 执行 pnpm install（依赖指向 packages/ 的 tgz）"
            if "QQ 绑定" in item["检查"]:
                return "打开 DeepSeek Harness 左侧「QQ 机器人」页扫码绑定"
    return "全部就绪；人设用 xuyi persona <名> 查看，粘贴进宿主 directPrompt 即可"
