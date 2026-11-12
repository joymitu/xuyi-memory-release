import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from xuyi.setup import (backup_profile, find_profile, install_adapter, model_ready, newest_adapter,
                        run, update_package_json)


def make_tgz(path, version="9.9.9"):
    with tarfile.open(path, "w:gz") as archive:
        for name, data in {
            "package/package.json": json.dumps({"name": "dsh-xuyi-memory", "version": version}),
            "package/index.js": "export const name = 'xuyi-memory';\n",
            "package/cordis.patch.yml": "[]\n",
        }.items():
            raw = data.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.profile = self.home / ".dsh" / "profiles" / "desktop"
        self.profile.mkdir(parents=True)
        (self.profile / "cordis.patch.yml").write_text(
            "- id: im-qqbot\n  config:\n    appId: test\n", encoding="utf-8")
        (self.profile / "package.json").write_text(json.dumps(
            {"name": "dsh-profile-desktop", "dependencies": {"@tencent-connect/dsh-qqbot": "0.5.5"},
             "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}}}, indent=2), encoding="utf-8")
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "packages").mkdir(parents=True)
        make_tgz(self.root / "packages" / "dsh-xuyi-memory-0.4.17.tgz", "0.4.17")
        make_tgz(self.root / "packages" / "dsh-xuyi-memory-0.4.9.tgz", "0.4.9")

    def tearDown(self):
        self.tmp.cleanup()

    def test_it_finds_the_profile_that_actually_has_the_qq_bot(self):
        empty = self.home / ".dsh" / "profiles" / "other"
        empty.mkdir()
        (empty / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
        self.assertEqual(find_profile(home=self.home), self.profile)
        self.assertIsNone(find_profile(explicit=self.home / "nope", home=self.home))

    def test_it_backs_up_before_touching_anything(self):
        dest = backup_profile(self.profile, home=self.home)
        self.assertTrue((dest / "cordis.patch.yml").is_file())
        self.assertTrue((dest / "package.json").is_file())

    def test_it_picks_the_newest_adapter(self):
        self.assertEqual(newest_adapter(self.root / "packages").name, "dsh-xuyi-memory-0.4.17.tgz")

    def test_it_installs_the_adapter_and_wires_the_profile(self):
        target = install_adapter(self.profile, self.root / "packages" / "dsh-xuyi-memory-0.4.17.tgz")
        self.assertTrue((target / "index.js").is_file())
        self.assertTrue((self.profile / "vendor" / "dsh-xuyi-memory-0.4.17.tgz").is_file())
        data = json.loads((self.profile / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(data["dependencies"]["dsh-xuyi-memory"], "file:vendor/dsh-xuyi-memory-0.4.17.tgz")
        self.assertIn("dsh-xuyi-memory", data["dsh"]["profile"]["bundles"])
        self.assertIn("@deepseek-ai/dsh-base", data["dsh"]["profile"]["bundles"])
        self.assertIn("@tencent-connect/dsh-qqbot", data["dependencies"])
        # 再跑一次不会重复加 bundle
        update_package_json(self.profile, "dsh-xuyi-memory-0.4.17.tgz")
        data = json.loads((self.profile / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(data["dsh"]["profile"]["bundles"].count("dsh-xuyi-memory"), 1)

    def test_missing_model_is_not_fatal(self):
        self.assertFalse(model_ready(self.root))
        (self.root / "models" / "bge-small-zh").mkdir(parents=True)
        (self.root / "models" / "bge-small-zh" / "model.onnx").write_bytes(b"x")
        self.assertTrue(model_ready(self.root))

    def test_one_command_run_installs_everything(self):
        result = run(home=self.home, root=self.root, skip_env=True, skip_register=True,
                     check_location=False, log=lambda *a: None)
        steps = {step["步骤"]: step["结果"] for step in result["步骤"]}
        self.assertEqual(steps["适配器"].split(" → ")[0], "dsh-xuyi-memory-0.4.17.tgz")
        self.assertTrue((self.profile / "node_modules" / "dsh-xuyi-memory" / "index.js").is_file())
        self.assertEqual(steps["模型"], "缺失")
        self.assertIn("备份", steps)
        data = json.loads((self.profile / "package.json").read_text(encoding="utf-8"))
        self.assertIn("dsh-xuyi-memory", data["dsh"]["profile"]["bundles"])


if __name__ == "__main__":
    unittest.main()
