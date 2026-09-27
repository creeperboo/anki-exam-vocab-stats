"""应试词汇统计插件 · GitHub 自动更新的纯逻辑测试（不需要 Anki，也不联网）。

跑法（在本文件夹的上上级执行）：
    python -m unittest discover -s 源码\\tests -p "test_*.py" -v

这里把「更新」这条链路上所有会出错的判断都钉住：版本号怎么比、两条下载地址
长什么样、半截包怎么识别、raw 挂了会不会换 Release、打包清单和代码是否对得上。
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import sys
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
PROJECT = os.path.dirname(SRC)
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import update_logic as U  # noqa: E402

DATA = os.path.join(SRC, "data")


def make_package(filler: int = 3000) -> bytes:
    """造一个「像样的」.ankiaddon：真 zip，且含 manifest.json 与 __init__.py。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("__init__.py", "x = 1")
        # 用随机字节填充：零字节压得太狠，包会小到撞上「最小体积」那道闸
        archive.writestr("data/pad.bin", os.urandom(filler))
    return buffer.getvalue()


def load_packager_constants() -> dict:
    """把根目录「安装到Anki.py」里的模块级常量抠出来（只解析，不执行）。"""
    path = os.path.join(PROJECT, "安装到Anki.py")
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    out[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    continue
    return out


class TestVersionCompare(unittest.TestCase):
    def test_tuple_from_dotted(self):
        self.assertEqual(U.version_tuple("1.2.3"), (1, 2, 3))
        self.assertEqual(U.version_tuple("0.2.0"), (0, 2, 0))

    def test_tuple_ignores_noise(self):
        self.assertEqual(U.version_tuple("v1.2.3"), (1, 2, 3))
        self.assertEqual(U.version_tuple("  1.10 "), (1, 10))

    def test_tuple_of_garbage_is_zero(self):
        self.assertEqual(U.version_tuple(""), (0,))
        self.assertEqual(U.version_tuple(None), (0,))
        self.assertEqual(U.version_tuple("abc"), (0,))

    def test_numeric_not_lexical(self):
        # 字符串比较会把 "0.10.0" 判成比 "0.9.0" 小，这里必须按数字比
        self.assertTrue(U.is_newer("0.10.0", "0.9.0"))
        self.assertFalse(U.is_newer("0.9.0", "0.10.0"))

    def test_equal_versions_are_not_newer(self):
        self.assertFalse(U.is_newer("0.2.0", "0.2.0"))
        self.assertFalse(U.is_newer("1.0", "1.0.0"))
        self.assertFalse(U.is_newer("", "0.2.0"))

    def test_parse_version_from_source(self):
        text = 'x = 1\n__version__ = "0.2.0"\ny = 2\n'
        self.assertEqual(U.parse_version(text), "0.2.0")
        self.assertEqual(U.parse_version("__version__ = '1.5.2'"), "1.5.2")
        self.assertEqual(U.parse_version("没有版本号"), "")


class TestUrLs(unittest.TestCase):
    def test_raw_url(self):
        self.assertEqual(
            U.raw_url("version.txt"),
            f"https://raw.githubusercontent.com/{U.UPDATE_REPO}/{U.UPDATE_BRANCH}/version.txt",
        )

    def test_release_asset_url(self):
        self.assertEqual(
            U.release_asset_url(U.UPDATE_ASSET),
            f"https://github.com/{U.UPDATE_REPO}/releases/latest/download/{U.UPDATE_ASSET}",
        )

    def test_download_urls_order(self):
        urls = U.download_urls("version.txt")
        self.assertEqual(len(urls), 2)
        self.assertIn("raw.githubusercontent.com", urls[0])
        self.assertIn("releases/latest/download", urls[1])

    def test_package_urls_release_first(self):
        """分发包不进仓库（12.5MB 超过 5MB 闸门），所以 Release 是主路、raw 是兜底。"""
        urls = U.package_urls()
        self.assertEqual(len(urls), 2)
        self.assertIn("releases/latest/download", urls[0])
        self.assertIn(U.UPDATE_ASSET, urls[0])
        self.assertIn("raw.githubusercontent.com", urls[1])
        self.assertEqual(U.package_urls(repo="TODO/x"), [])

    def test_repo_ready(self):
        self.assertTrue(U.repo_ready("someone/something"))
        self.assertFalse(U.repo_ready(""))
        self.assertFalse(U.repo_ready("TODO/owner"))
        self.assertEqual(U.download_urls("version.txt", repo="TODO/x"), [])

    def test_release_url_adds_v_prefix(self):
        self.assertEqual(
            U.release_url("0.2.0"),
            f"https://github.com/{U.UPDATE_REPO}/releases/tag/v0.2.0",
        )
        self.assertEqual(
            U.release_url("v0.2.0"),
            f"https://github.com/{U.UPDATE_REPO}/releases/tag/v0.2.0",
        )
        self.assertEqual(
            U.release_url(""), f"https://github.com/{U.UPDATE_REPO}/releases"
        )


class TestPackageShape(unittest.TestCase):
    def test_real_package_is_accepted(self):
        self.assertTrue(U.looks_like_package(make_package()))

    def test_garbage_is_rejected(self):
        self.assertFalse(U.looks_like_package(b""))
        self.assertFalse(U.looks_like_package(b"<html>404</html>" * 100))

    def test_truncated_package_is_rejected(self):
        data = make_package()
        self.assertFalse(U.looks_like_package(data[:100]))
        self.assertFalse(U.looks_like_package(data[: len(data) // 2]))

    def test_zip_without_manifest_is_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("readme.txt", "0" * 4000)
        self.assertFalse(U.looks_like_package(buffer.getvalue()))


class TestFetchLatestVersion(unittest.TestCase):
    def test_raw_wins(self):
        seen = []

        def download(url):
            seen.append(url)
            return b"0.3.0\n"

        self.assertEqual(U.fetch_latest_version(download=download), "0.3.0")
        self.assertEqual(len(seen), 1)
        self.assertIn("raw.githubusercontent.com", seen[0])

    def test_falls_back_to_release_asset(self):
        seen = []

        def download(url):
            seen.append(url)
            if "raw.githubusercontent.com" in url:
                raise OSError("连接被重置")
            return b"0.3.0"

        self.assertEqual(U.fetch_latest_version(download=download), "0.3.0")
        self.assertEqual(len(seen), 2)
        self.assertIn("releases/latest/download", seen[1])

    def test_both_channels_failing_is_quiet(self):
        def download(url):
            raise OSError("没网")

        self.assertEqual(U.fetch_latest_version(download=download), "")

    def test_blank_version_counts_as_failure(self):
        self.assertEqual(U.fetch_latest_version(download=lambda url: b"   "), "")


class TestFetchPackage(unittest.TestCase):
    def test_release_wins(self):
        payload = make_package()
        seen = []

        def download(url):
            seen.append(url)
            return payload

        self.assertEqual(U.fetch_package(download=download), payload)
        self.assertEqual(len(seen), 1)
        self.assertIn("releases/latest/download", seen[0])

    def test_any_channel_can_supply(self):
        payload = make_package()
        self.assertEqual(U.fetch_package(download=lambda url: payload), payload)

    def test_half_package_falls_back(self):
        good = make_package()
        seen = []

        def download(url):
            seen.append(url)
            if "releases/latest/download" in url:
                return good[:100]  # 传到一半断了
            return good

        self.assertEqual(U.fetch_package(download=download), good)
        self.assertEqual(len(seen), 2)
        self.assertIn("raw.githubusercontent.com", seen[1])

    def test_all_channels_failing_raises(self):
        def download(url):
            raise OSError("没网")

        with self.assertRaises(Exception):
            U.fetch_package(download=download)

    def test_no_repo_raises(self):
        with self.assertRaises(Exception):
            U.fetch_package(repo="")


class TestPromptText(unittest.TestCase):
    def test_prompt_mentions_versions_and_release(self):
        text = U.update_prompt_text("0.3.0", "0.2.0")
        self.assertIn("0.3.0", text)
        self.assertIn("0.2.0", text)
        self.assertIn("releases/tag/v0.3.0", text)

    def test_pending_restart_text(self):
        text = U.pending_restart_text("0.3.0", "0.2.0")
        self.assertIn("0.3.0", text)
        self.assertIn("0.2.0", text)
        self.assertIn("重启", text)

    def test_describe_state(self):
        self.assertIn("还没查过", U.describe_state({}, "0.2.0"))
        text = U.describe_state(
            {"checked_at": 1790000000, "latest": "0.3.0"}, "0.2.0"
        )
        self.assertIn("0.2.0", text)
        self.assertIn("0.3.0", text)


class TestProjectConsistency(unittest.TestCase):
    """代码、版本号文件、打包清单三者必须对得上，否则自动更新会静默失灵。"""

    def setUp(self):
        self.packager = load_packager_constants()

    def test_version_file_matches_init(self):
        with open(os.path.join(SRC, "version.txt"), "r", encoding="utf-8") as fh:
            from_file = fh.read().strip()
        with open(os.path.join(SRC, "__init__.py"), "r", encoding="utf-8") as fh:
            from_code = U.parse_version(fh.read())
        self.assertTrue(from_file, "源码/version.txt 是空的")
        self.assertEqual(from_file, from_code)

    def test_root_version_file_is_the_published_one(self):
        """仓库根目录的 version.txt 是插件更新时要读的那个，必须和源码里的一致。"""
        root_file = os.path.join(PROJECT, "version.txt")
        if not os.path.isfile(root_file):
            self.skipTest("还没跑过 安装到Anki.py，根目录 version.txt 尚未生成")
        with open(root_file, "r", encoding="utf-8") as fh:
            root_text = fh.read().strip()
        with open(os.path.join(SRC, "version.txt"), "r", encoding="utf-8") as fh:
            src_text = fh.read().strip()
        self.assertEqual(root_text, src_text)

    def test_asset_name_matches_packaged_file(self):
        name = self.packager.get("PACKAGE_NAME", "")
        self.assertTrue(name, "安装到Anki.py 里没有 PACKAGE_NAME")
        self.assertEqual(name, U.UPDATE_ASSET)

    def test_folder_manifest_and_asset_agree(self):
        folder = self.packager.get("FOLDER_NAME")
        self.assertTrue(folder, "安装到Anki.py 里没有 FOLDER_NAME")
        with open(os.path.join(SRC, "manifest.json"), "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest.get("package"), folder)
        self.assertTrue(
            U.UPDATE_ASSET.startswith(folder),
            "分发包文件名要和插件目录名对得上，否则更新装不上",
        )
        self.assertTrue(U.UPDATE_REPO, "更新仓库地址不能为空")

    def test_addon_files_all_exist(self):
        files = self.packager.get("ADDON_FILES") or []
        self.assertIn("update_logic.py", files)
        self.assertIn("version.txt", files)
        for rel in files:
            path = os.path.join(SRC, *rel.split("/"))
            self.assertTrue(os.path.isfile(path), f"插件包里列了但磁盘上没有：{rel}")

    def test_source_extras_exist(self):
        for rel in self.packager.get("SOURCE_EXTRA") or []:
            path = os.path.join(PROJECT, *rel.split("/"))
            self.assertTrue(os.path.isfile(path), f"源码包里列了但磁盘上没有：{rel}")

    def test_default_config_checks_updates(self):
        with open(os.path.join(SRC, "config.json"), "r", encoding="utf-8") as fh:
            config = json.load(fh)
        self.assertTrue(bool(config.get("update_check")), "更新检查默认应当是开着的")

    def test_update_logic_is_anki_free(self):
        with open(os.path.join(SRC, "update_logic.py"), "r", encoding="utf-8") as fh:
            source = fh.read()
        imports = [
            line.strip()
            for line in source.splitlines()
            if re.match(r"\s*(import|from)\s+\S", line)
        ]
        for line in imports:
            self.assertFalse(
                re.match(r"(import|from)\s+(aqt|anki)\b", line),
                f"纯逻辑层不能 import Anki：{line}",
            )

    def test_network_import_is_lazy(self):
        """urllib 只在真下载时才 import：测试与离线使用都不该碰网络模块。"""
        with open(os.path.join(SRC, "update_logic.py"), "r", encoding="utf-8") as fh:
            top_level = [
                line
                for line in fh.read().splitlines()
                if re.match(r"(import|from)\s+\S", line)
            ]
        self.assertNotIn("import urllib.request", top_level)


if __name__ == "__main__":
    unittest.main()
