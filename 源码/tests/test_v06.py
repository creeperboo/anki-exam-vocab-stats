"""应试词汇统计插件 · v0.6 修订的纯逻辑测试（不需要 Anki，也不联网）。

覆盖三件事：

1. ``hook_guard``：Anki 改了钩子参数个数时，跳过而不是崩（本轮那个
   ``browser_menus_did_init`` 只传 browser 一个参数，把卡片浏览器搞崩）；
2. 音频包下载的**断点续传**：第一次传一半断掉、第二次带 Range 续上；
3. 素材库的本地 zip 安装（``resources.install_media``）与各处打包清单一致。

跑法（在本文件夹的上上级执行）：
    python -m unittest discover -s 源码\\tests -p "test_*.py" -v
"""

from __future__ import annotations

import ast
import os
import re
import sys
import tempfile
import unittest
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
PROJECT = os.path.dirname(SRC)
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import hook_guard as HG  # noqa: E402
import resources as R  # noqa: E402
import update_logic as U  # noqa: E402


def read_source(name: str) -> str:
    with open(os.path.join(SRC, name), "r", encoding="utf-8") as handle:
        return handle.read()


def module_constants(path: str) -> dict:
    """只解析模块级字面量常量（不执行那个脚本）。"""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    out: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    out[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    continue
    return out


# ---------------------------------------------------------------- 钩子签名防护


class TestHookGuard(unittest.TestCase):
    def test_bind_ok_accepts_matching_args(self):
        self.assertTrue(HG.bind_ok(lambda a, b: None, (1, 2)))

    def test_bind_ok_rejects_missing_arg(self):
        self.assertFalse(HG.bind_ok(lambda a, b: None, (1,)))

    def test_bind_ok_rejects_extra_arg(self):
        self.assertFalse(HG.bind_ok(lambda a: None, (1, 2)))

    def test_bind_ok_allows_varargs(self):
        self.assertTrue(HG.bind_ok(lambda browser, *args: None, (1, "extra")))

    def test_guard_skips_on_mismatch(self):
        def callback(browser, menu):
            raise AssertionError("不该被调用")

        wrapped = HG.guard(callback)
        self.assertIsNone(wrapped("只有 browser 一个参数"))

    def test_guard_passes_matching_call_through(self):
        def callback(a, b):
            return a + b

        self.assertEqual(HG.guard(callback)(1, 2), 3)

    def test_guard_does_not_swallow_real_errors(self):
        """回调内部真正的报错必须照旧往上抛，否则会把真 bug 藏起来。"""

        def callback(browser):
            raise ValueError("内部真报错")

        with self.assertRaises(ValueError):
            HG.guard(callback)("browser")

    def test_guard_marks_the_wrapper(self):
        self.assertTrue(getattr(HG.guard(lambda: None), "__guarded__", False))

    def test_guarded_browser_menu_accepts_one_argument(self):
        """真实签名是 (browser, *args)，单参数调用不该被判成对不上。"""
        source = read_source("__init__.py")
        self.assertIn("def _browser_menu(browser, *args):", source)
        self.assertTrue(HG.bind_ok(self._varargs_stub(), ("browser",)))
        self.assertTrue(HG.bind_ok(self._varargs_stub(), ("browser", "多余参数")))

    @staticmethod
    def _varargs_stub():
        def stub(browser, *args):
            return None

        return stub


# ---------------------------------------------------------------- 断点续传


class FakeResumeResponse:
    """很薄的一层假响应：支持 status / Content-Length / 中途断线。"""

    def __init__(self, payload: bytes, start: int, fail_after=None, status: int = 206):
        self.payload = payload
        self.pos = start
        self.status = status
        self.fail_after = fail_after
        self.sends = 0
        self.headers = {"Content-Length": str(len(payload) - start)}

    def read(self, size: int = -1):
        if self.fail_after is not None and self.sends >= self.fail_after:
            raise OSError("连接被重置")
        self.sends += 1
        chunk = self.payload[self.pos : self.pos + size] if size >= 0 else self.payload[self.pos :]
        self.pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestResumeDownload(unittest.TestCase):
    def setUp(self):
        self._real = urllib.request.urlopen
        self.addCleanup(self._restore)
        self.dir = tempfile.mkdtemp()

    def _restore(self):
        urllib.request.urlopen = self._real

    def test_resume_completes_the_file(self):
        payload = b"PK" + os.urandom(300 * 1024)
        seen_ranges: list = []

        def fake(request, timeout=None):
            rng = request.headers.get("Range")
            seen_ranges.append(rng)
            start = 0
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
            first = len(seen_ranges) == 1
            return FakeResumeResponse(payload, start, fail_after=1 if first else None)

        urllib.request.urlopen = fake
        path = os.path.join(self.dir, "audio.zip")
        wrote = U.download_to_file(U.materials_url(), path, retries=1, resume=True)

        self.assertEqual(len(seen_ranges), 2, str(seen_ranges))
        self.assertIsNone(seen_ranges[0], "第一次应当是全新下载，不带 Range")
        self.assertTrue(seen_ranges[1] and seen_ranges[1].startswith("bytes="), str(seen_ranges))
        self.assertEqual(wrote, len(payload))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), payload, "续传后的文件必须和原文件字节一致")

    def test_resume_failure_removes_leftovers(self):
        payload = b"PK" + os.urandom(300 * 1024)

        def fake(request, timeout=None):
            rng = request.headers.get("Range")
            start = int(rng.split("=")[1].split("-")[0]) if rng else 0
            return FakeResumeResponse(payload, start, fail_after=1)

        urllib.request.urlopen = fake
        path = os.path.join(self.dir, "audio.zip")
        with self.assertRaises(Exception):
            U.download_to_file(U.materials_url(), path, retries=1, resume=True)
        self.assertFalse(os.path.exists(path), "全部重试都失败后，残留的半截包必须删掉")

    def test_server_ignoring_range_restarts_from_zero(self):
        """服务器不认 Range（回 200 整包）时应当从头写，不能拼出坏文件。"""
        payload = b"PK" + os.urandom(120 * 1024)
        calls = {"n": 0}

        def fake(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                # 先造一个半截文件，再断线
                return FakeResumeResponse(payload, 0, fail_after=1, status=200)
            return FakeResumeResponse(payload, 0, status=200)  # 忽略 Range

        urllib.request.urlopen = fake
        path = os.path.join(self.dir, "audio.zip")
        wrote = U.download_to_file(U.materials_url(), path, retries=1, resume=True)
        self.assertEqual(wrote, len(payload))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), payload)

    def test_no_resume_keeps_old_behaviour(self):
        payload = b"PK" + os.urandom(120 * 1024)

        def fake(request, timeout=None):
            return FakeResumeResponse(payload, 0, status=200)

        urllib.request.urlopen = fake
        path = os.path.join(self.dir, "audio.zip")
        with open(path, "wb") as handle:
            handle.write("旧内容".encode("utf-8"))
        wrote = U.download_to_file(U.materials_url(), path)
        self.assertEqual(wrote, len(payload))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), payload)


class TestMaterialsConstants(unittest.TestCase):
    def test_materials_timeout_and_retries(self):
        self.assertEqual(U.MATERIALS_TIMEOUT, 60)
        self.assertEqual(U.MATERIALS_RETRIES, 3)
        self.assertGreater(U.MATERIALS_TIMEOUT, U.PACKAGE_TIMEOUT)

    def test_failure_text_points_at_local_zip(self):
        text = U.materials_failure_text(["https://example.com/a.zip"], OSError("没网"))
        self.assertIn("从本地 zip 文件安装", text)
        self.assertIn("https://example.com/a.zip", text)
        self.assertIn("没网", text)


# ---------------------------------------------------------------- 本地 zip 安装


class TestLocalZipInstall(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()
        self.res = R.LocalResources({}, cache_dir=self.cache)

    def _make_zip(self, names) -> str:
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "exam_materials_audio.zip")
        with zipfile.ZipFile(path, "w") as archive:
            for name in names:
                archive.writestr(name, b"\x00\x01")
        return path

    def test_install_media_counts_and_reports_progress(self):
        pack = self._make_zip(["en_a.mp3", "en_b.mp3", "ja_a.mp3", "readme.txt"])
        seen: list = []
        result = self.res.install_media(pack, progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(result["files"], 3, "只数 mp3")
        self.assertEqual(sorted(os.listdir(self.res.media_dir())), ["en_a.mp3", "en_b.mp3", "ja_a.mp3"])
        self.assertTrue(seen, "进度回调应当被调用")
        self.assertEqual(seen[-1], (3, 3))

    def test_media_installed_flips_to_true(self):
        self.assertFalse(self.res.media_installed()["installed"])
        self.res.install_media(self._make_zip(["en_a.mp3"]))
        self.assertTrue(self.res.media_installed()["installed"])

    def test_install_media_rejects_non_zip(self):
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "not_a_zip.zip")
        with open(path, "wb") as handle:
            handle.write("这不是一个 zip 文件".encode("utf-8"))
        with self.assertRaises(Exception):
            self.res.install_media(path)

    def test_sha256_matches_manifest_for_the_real_zip(self):
        """本机真有音频包时，清单里的 sha256 必须对得上（没有就跳过）。"""
        project_zip = os.path.join(PROJECT, "工具", "素材构建", "exam_materials_audio.zip")
        if not os.path.isfile(project_zip):
            self.skipTest("本机没有构建好的音频包")
        manifest = self.res.manifest()
        expect = str(manifest.get("sha256") or "")
        self.assertEqual(len(expect), 64)
        self.assertEqual(R.LocalResources.sha256_of(project_zip), expect)


def release_json(names_to_ids: dict) -> bytes:
    """造一份 GitHub /releases/latest 的 JSON（测试用，形状和真的一致）。"""
    import json

    assets = [
        {
            "name": name,
            "url": U.asset_api_url(asset_id),
            "browser_download_url": U.release_asset_url(name),
        }
        for name, asset_id in names_to_ids.items()
    ]
    return json.dumps({"tag_name": "v0.3.0", "assets": assets}).encode("utf-8")


def package_bytes() -> bytes:
    """造一个能被 looks_like_package 认下的最小分发包。"""
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("manifest.json", '{"package": "exam_vocab_stats"}')
        archive.writestr("__init__.py", "__version__ = '0.3.0'\n")
        # 补点内容，超过 MIN_PACKAGE_BYTES，免得被当成半截包
        archive.writestr("data/filler.bin", b"\x00" * 2048)
    return buf.getvalue()


class TestGithubApiFallback(unittest.TestCase):
    """连不上 github.com:443 时，走 api.github.com 的附件接口当备用通道。"""

    def setUp(self):
        self.asked: list = []
        self.payload = release_json({U.MATERIALS_ASSET: 4242})

    def fake_json(self, url: str) -> bytes:
        self.asked.append(url)
        return self.payload

    def test_api_asset_url_finds_the_asset(self):
        url = U.api_asset_url(U.MATERIALS_ASSET, fetch_json_fn=self.fake_json)
        self.assertEqual(url, U.asset_api_url(4242))
        self.assertEqual(self.asked, [U.release_api_url()])

    def test_api_asset_url_returns_empty_when_missing(self):
        other = release_json({"something_else.bin": 1})
        url = U.api_asset_url(U.MATERIALS_ASSET, fetch_json_fn=lambda url: other)
        self.assertEqual(url, "")

    def test_api_asset_url_returns_empty_on_network_failure(self):
        def boom(url):
            raise OSError("连不上 api.github.com")

        self.assertEqual(U.api_asset_url(U.MATERIALS_ASSET, fetch_json_fn=boom), "")

    def test_api_asset_url_returns_empty_for_bad_json(self):
        self.assertEqual(
            U.api_asset_url(U.MATERIALS_ASSET, fetch_json_fn=lambda url: b"<html>404"),
            "",
        )

    def test_materials_urls_is_static_then_api_without_duplicates(self):
        urls = U.materials_urls(fetch_json_fn=self.fake_json)
        self.assertEqual(urls[0], U.materials_url())
        self.assertEqual(urls[1], U.asset_api_url(4242))
        self.assertEqual(len(urls), len(set(urls)), "地址不能重复")
        self.assertTrue(urls[0].startswith("https://github.com/"))
        self.assertTrue(urls[1].startswith("https://api.github.com/"))

    def test_materials_urls_degrades_to_one_url_without_api(self):
        urls = U.materials_urls(fetch_json_fn=lambda url: (_ for _ in ()).throw(OSError("没网")))
        self.assertEqual(urls, [U.materials_url()])

    def test_materials_urls_never_calls_the_network_in_tests(self):
        """注入假 fetch_json 后，真网络入口一次都不许被碰。"""
        real = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("不应该真联网")
        )
        try:
            U.materials_urls(fetch_json_fn=self.fake_json)
        finally:
            urllib.request.urlopen = real

    def test_package_urls_with_api_has_three_channels(self):
        urls = U.package_urls_with_api(
            fetch_json_fn=lambda url: release_json({U.UPDATE_ASSET: 4242})
        )
        self.assertEqual(
            urls,
            [
                U.release_asset_url(U.UPDATE_ASSET),
                U.raw_url(U.UPDATE_ASSET),
                U.asset_api_url(4242),
            ],
        )

    def test_fetch_package_falls_back_to_the_api_channel(self):
        payload = package_bytes()
        tried: list = []

        def download(url: str) -> bytes:
            tried.append(url)
            if "api.github.com" not in url:
                raise OSError("连不上 github.com:443")
            return payload

        got = U.fetch_package(
            download=download,
            fetch_json_fn=lambda url: release_json({U.UPDATE_ASSET: 4242}),
        )
        self.assertEqual(got, payload)
        self.assertEqual(len(tried), 3, str(tried))
        self.assertIn("api.github.com", tried[-1])

    def test_fetch_package_reports_the_last_error_when_all_channels_fail(self):
        def download(url: str) -> bytes:
            raise OSError(f"挂了：{url}")

        with self.assertRaises(Exception) as ctx:
            U.fetch_package(download=download)
        self.assertIn("挂了", str(ctx.exception))

    def test_download_to_file_sends_the_accept_header(self):
        payload = b"PK" + b"a" * 1024
        seen: dict = {}

        def fake(request, timeout=None):
            seen["accept"] = request.headers.get("Accept")
            seen["ua"] = request.headers.get("User-agent") or request.headers.get("User-Agent")
            return FakeResumeResponse(payload, 0, status=200)

        real = urllib.request.urlopen
        urllib.request.urlopen = fake
        try:
            path = os.path.join(tempfile.mkdtemp(), "audio.zip")
            wrote = U.download_to_file(
                U.asset_api_url(4242), path, accept=U.BINARY_ACCEPT, version="0.3.0"
            )
        finally:
            urllib.request.urlopen = real
        self.assertEqual(wrote, len(payload))
        self.assertEqual(seen["accept"], U.BINARY_ACCEPT)
        self.assertIn("anki-exam-vocab-stats", seen["ua"] or "")

    def test_source_wires_materials_download_to_the_url_list(self):
        source = read_source("__init__.py")
        self.assertIn("U.materials_urls(", source)
        self.assertIn("accept=U.BINARY_ACCEPT", source)
        self.assertIn("state[\"tried\"]", source)

    def test_source_puts_the_api_channel_first(self):
        """github.com 被挡时静态直链要干等好几分钟，能走的 API 通道要先试。"""
        source = read_source("__init__.py")
        self.assertIn("api_first", source)
        self.assertIn("U.VERSION_TIMEOUT", source)
        self.assertIn("U.host_reachable(", source)


class TestHostReachable(unittest.TestCase):
    """先探一下主机通不通，别在死掉的地址上耗满超时。"""

    def test_unreachable_host_is_false(self):
        def connect(addr, timeout):
            raise OSError("timed out")

        self.assertFalse(
            U.host_reachable("https://github.com/x/y.zip", connect=connect, proxies={})
        )

    def test_reachable_host_is_true_and_closed(self):
        closed = {"n": 0}

        class Conn:
            def close(self):
                closed["n"] += 1

        seen = {}

        def connect(addr, timeout):
            seen["addr"] = addr
            seen["timeout"] = timeout
            return Conn()

        self.assertTrue(
            U.host_reachable("https://api.github.com/x", connect=connect, proxies={})
        )
        self.assertEqual(seen["addr"], ("api.github.com", 443))
        self.assertEqual(closed["n"], 1, "探完要关掉连接")

    def test_proxy_short_circuits_without_probing(self):
        def connect(addr, timeout):
            raise AssertionError("有代理时不该直连探测")

        self.assertTrue(
            U.host_reachable(
                "https://github.com/x", connect=connect, proxies={"https": "http://127.0.0.1:1"}
            )
        )

    def test_url_without_host_is_false(self):
        self.assertFalse(U.host_reachable("", connect=lambda a, t: None, proxies={}))

    def test_http_url_defaults_to_port_80(self):
        seen = {}

        class Conn:
            def close(self):
                pass

        def connect(addr, timeout):
            seen["addr"] = addr
            return Conn()

        U.host_reachable("http://example.com/a", connect=connect, proxies={})
        self.assertEqual(seen["addr"], ("example.com", 80))


# ---------------------------------------------------------------- 打包清单


class TestPackagingLists(unittest.TestCase):
    def setUp(self):
        self.installer = module_constants(os.path.join(PROJECT, "安装到Anki.py"))
        self.probe = module_constants(os.path.join(PROJECT, "工具", "run_anki_probe.py"))

    def test_hook_guard_is_packaged(self):
        self.assertIn("hook_guard.py", self.installer.get("ADDON_FILES") or [])
        self.assertIn("hook_guard.py", self.probe.get("ADDON_FILES") or [])
        self.assertTrue(os.path.isfile(os.path.join(SRC, "hook_guard.py")))

    def test_two_lists_agree(self):
        install_files = sorted(self.installer.get("ADDON_FILES") or [])
        probe_files = sorted(self.probe.get("ADDON_FILES") or [])
        self.assertEqual(install_files, probe_files, "两份打包清单必须一致，否则探针验的不是真装的那份")

    def test_every_listed_file_exists(self):
        for rel in self.installer.get("ADDON_FILES") or []:
            self.assertTrue(
                os.path.isfile(os.path.join(SRC, *rel.split("/"))), f"清单里有、磁盘上没有：{rel}"
            )

    def test_test_v06_is_in_source_zip(self):
        self.assertIn("源码/tests/test_v06.py", self.installer.get("SOURCE_EXTRA") or [])


# ---------------------------------------------------------------- 源码级回归


class TestSourceGuards(unittest.TestCase):
    """这些是「不 import Anki 也能钉住」的源码级断言，防同一类 bug 回归。"""

    def setUp(self):
        self.source = read_source("__init__.py")

    def test_add_hook_goes_through_guard(self):
        self.assertIn("hook.append(HG.guard(callback))", self.source)
        self.assertIn("hook.append(HG.guard(_on_profile_did_open))", self.source)

    def test_browser_menu_hooked_with_varargs_signature(self):
        self.assertIn('_add_hook("browser_menus_did_init", _browser_menu)', self.source)
        self.assertIn("def _browser_menu(browser, *args):", self.source)
        self.assertNotIn("def _browser_menu(browser, menu):", self.source)

    def test_browser_menu_has_object_name_guard(self):
        self.assertIn('BROWSER_MENU_OBJECT = "exam_vocab_stats_menu"', self.source)
        self.assertIn("_find_menu(bar, BROWSER_MENU_OBJECT)", self.source)

    def test_menubar_fallback_chain(self):
        self.assertIn("form.menubar", self.source)
        self.assertIn("getattr(browser, \"menuBar\", None)", self.source)

    def test_materials_zip_config_key(self):
        self.assertIn('"materials_zip": ""', self.source)
        self.assertIn("从本地 zip 文件安装", self.source)
        self.assertIn("install_materials_from_zip", self.source)

    def test_legacy_config_keys_are_dropped(self):
        for key in (
            "custom_wordlists",
            "dictionary_paths",
            "audio_sources",
            "network_materials",
            "tts_fallback",
        ):
            self.assertIn(f'"{key}"', self.source, f"清理名单里应当有 {key}")
        self.assertIn("LEGACY_CONFIG_KEYS", self.source)

    def test_version_is_still_0_3_0(self):
        self.assertIn('__version__ = "0.3.0"', self.source)
        with open(os.path.join(SRC, "version.txt"), "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "0.3.0")

    def test_manifest_mod_is_a_positive_int(self):
        import json

        with open(os.path.join(SRC, "manifest.json"), "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertIsInstance(manifest.get("mod"), int)
        self.assertGreater(manifest["mod"], 0)

    def test_update_logic_still_imports_no_anki(self):
        source = read_source("update_logic.py")
        imports = [
            line.strip() for line in source.splitlines() if re.match(r"\s*(import|from)\s+\S", line)
        ]
        for line in imports:
            self.assertFalse(
                re.match(r"(import|from)\s+(aqt|anki)\b", line), f"纯逻辑层不能 import Anki：{line}"
            )


if __name__ == "__main__":
    unittest.main()
