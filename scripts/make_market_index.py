"""生成 DeepSeek Harness 插件市场索引（格式 1），给打开了「自定义插件索引」的用户添加。

DSH 目前没有官方插件目录：市场是实验功能，读用户自己添加的索引地址。这份索引把续忆列成
一个 npm 来源的组合包；版本号、标题、简介、图标都从 dsh-adapter/ 读，图标内联（不另外托管）。

用法（发布到 npm 之后）：
    python scripts/make_market_index.py --published 1794240000 --out market/index.json
--published 是该版本在 npm 上的发布时间（Unix 秒）；省略时用 `npm view` 查。
"""
import argparse
import base64
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "dsh-adapter"


def published_at(name, version):
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    done = subprocess.run([npm, "view", f"{name}@{version}", "time", "--json"],
                          capture_output=True, text=True, encoding="utf-8")
    if done.returncode:
        raise SystemExit(f"npm 上查不到 {name}@{version}：先发布，或用 --published 指定时间")
    stamp = json.loads(done.stdout)[version]
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())


def build(published, versions=None):
    manifest = json.loads((ADAPTER / "package.json").read_text(encoding="utf-8"))
    zh = json.loads((ADAPTER / "locale/zh.json").read_text(encoding="utf-8"))["meta"]
    en = json.loads((ADAPTER / "locale/en.json").read_text(encoding="utf-8"))["meta"]
    icon = base64.b64encode((ADAPTER / "icon.png").read_bytes()).decode("ascii")
    if len(base64.b64decode(icon)) > 32 * 1024:
        raise SystemExit("图标超过 32KiB，DSH 不接受内联")
    repo = manifest["repository"]["url"].removeprefix("git+").removesuffix(".git")
    entries = [{"version": manifest["version"], "publishTimestamp": int(published), "source": {"type": "npm"}}]
    for version, stamp in (versions or {}).items():
        if version != manifest["version"]:
            entries.append({"version": version, "publishTimestamp": int(stamp), "source": {"type": "npm"}})
    return {
        "format": 1,
        "title": {"en": "Xuyi plugins", "zh": "续忆插件"},
        "description": {"en": "Plugins by the xuyi-memory project.", "zh": "续忆项目发布的插件。"},
        "metadata": {
            "categories": {"memory": {"title": {"en": "Memory", "zh": "记忆"}}},
            "labels": {"qq": {"title": {"en": "QQ", "zh": "QQ"}},
                       "local": {"title": {"en": "Local-first", "zh": "数据在本地"}}},
        },
        "plugins": [{
            "name": manifest["name"],
            "title": {"en": en["title"], "zh": zh["title"]},
            "description": {"en": en["description"], "zh": zh["description"]},
            "icon": "data:image/png;base64," + icon,
            "author": manifest["author"],
            "homepage": manifest["homepage"],
            "repository": {"url": repo},
            "metadata": {"category": "memory", "labels": ["qq", "local"],
                         "keywords": [k for k in manifest["keywords"] if k not in ("dsh", "dsh-plugin")]},
            "versions": entries,
        }],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--published", type=int, help="该版本在 npm 上的发布时间（Unix 秒）")
    parser.add_argument("--out", default=str(ROOT / "market/index.json"))
    args = parser.parse_args()
    manifest = json.loads((ADAPTER / "package.json").read_text(encoding="utf-8"))
    published = args.published or published_at(manifest["name"], manifest["version"])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build(published), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(out)


if __name__ == "__main__":
    main()
