# -*- coding: utf-8 -*-
"""内置素材层（v0.3）：制卡素材全部来自插件自带的构建产物，运行期不扫本机词典。

设计约定：

* ``data/en_materials.json.gz`` / ``data/ja_materials.json.gz`` 里是**词表内每一个
  词**的单词 / 音标 / 释义 / 例句 / 例句译 / 音频文件名，构建期已经补齐（见
  ``工具\\构建素材库.py``）。
* 音频本体太大（约 300MB），放在 GitHub Release 的 ``exam_materials_audio.zip``，
  用户点一次「一键构建素材库」下载解压到 ``<用户配置目录>\\exam_vocab_stats\\media``。
  插件目录里不写任何用户数据。
* 本模块不 import Anki，纯逻辑测试可以直接跑。
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import zipfile

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ADDON_DIR, "data")
MEDIA_DIR_NAME = "exam_vocab_stats"

EN_MATERIALS = "en_materials.json.gz"
JA_MATERIALS = "ja_materials.json.gz"
MANIFEST = "materials_manifest.json"

# 素材来源的中文名，卡片底部和设置页都要用
SOURCE_LABELS = {
    "wordforge": "wordforge 词书",
    "ECDICT": "ECDICT（MIT）",
    "Tatoeba": "Tatoeba（CC BY 2.0 FR）",
    "词义代例句": "词义代例句",
    "语音合成": "本机语音合成",
    "eggrolls": "eggrolls JLPT10k",
    "OpenJLPT": "OpenJLPT",
    "tanos": "tanos JLPT 词表",
    "nhk16": "NHK16 真人音",
    "shinmeikai8": "新明解 真人音",
    "forvo": "Forvo 真人音",
    "jpod": "JapanesePod101 真人音",
    "OALD10": "OALD10 真人音",
    "明镜日汉双解": "明镜日汉双解（本机）",
    "明镜日汉双解 1.4.2": "明镜日汉双解（本机）",
}


def _label(name: str) -> str:
    return SOURCE_LABELS.get(name, name)


# ECDICT 的释义字段里带着**字面的** ``\n``（反斜杠 + n 两个字符，不是真换行），
# 直接落进卡片字段就会在卡面上原样显示，所以查素材时统一还原一次。
_LITERAL_ESCAPE = re.compile(r"\\[nrt]")
_ESCAPE_MAP = {"n": "\n", "r": "\n", "t": " "}


def _clean_text(text: str) -> str:
    """把字面转义（``\\n`` / ``\\r`` / ``\\t``）还原成真实字符；其它一律不动。"""
    if not text or "\\" not in text:
        return text or ""
    return _LITERAL_ESCAPE.sub(lambda m: _ESCAPE_MAP[m.group(0)[1]], text)


def read_gz(path: str) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


class LocalResources:
    """读内置素材索引，给出制卡需要的一条素材。"""

    def __init__(self, config: dict | None = None, cache_dir: str | None = None,
                 roots: dict | None = None):
        self.config = dict(config or {})
        # cache_dir 传进来的是 Anki 的用户配置目录（addons21 之外），音频解压在那儿
        self.cache_dir = cache_dir or ""
        # roots 是旧版本接口留下的参数，现在用不到了，收着不报错
        self.roots = dict(roots or {})
        self._en: dict | None = None
        self._ja: dict | None = None
        self._meta: dict = {}
        self._errors: dict = {}

    # ----------------------------------------------------------- 内置索引

    def _load(self, name: str) -> dict:
        path = os.path.join(DATA_DIR, name)
        if not os.path.isfile(path):
            self._errors[name] = "缺少内置素材索引"
            return {}
        try:
            payload = read_gz(path)
        except Exception as exc:  # noqa: BLE001
            self._errors[name] = str(exc)
            return {}
        self._meta[name] = payload.get("meta") or {}
        return payload.get("words") or {}

    def en_materials(self) -> dict:
        if self._en is None:
            self._en = self._load(EN_MATERIALS)
        return self._en

    def ja_materials(self) -> dict:
        if self._ja is None:
            self._ja = self._load(JA_MATERIALS)
        return self._ja

    # ----------------------------------------------------------- 音频包

    def media_dir(self) -> str:
        """音频去向：``<用户配置目录>\\exam_vocab_stats\\media``。

        cache_dir 传进来的就是插件的用户目录（v0.2 起一直这么做），没传就用插件目录兜底。
        """
        base = self.cache_dir or os.path.join(ADDON_DIR, MEDIA_DIR_NAME)
        return os.path.join(base, "media")

    def media_installed(self) -> dict:
        folder = self.media_dir()
        if not os.path.isdir(folder):
            return {"installed": False, "files": 0, "dir": folder}
        files = 0
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.is_file() and entry.name.endswith(".mp3"):
                    files += 1
        return {"installed": files > 0, "files": files, "dir": folder}

    def manifest(self) -> dict:
        path = os.path.join(DATA_DIR, MANIFEST)
        if not os.path.isfile(path):
            return {}
        try:
            with io.open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:  # noqa: BLE001
            return {}

    def install_media(self, zip_path: str, progress=None) -> dict:
        """把音频包解压到用户配置目录。返回 {files, dir}。"""
        folder = self.media_dir()
        os.makedirs(folder, exist_ok=True)
        count = 0
        with zipfile.ZipFile(zip_path) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".mp3")]
            total = len(names)
            for index, name in enumerate(names, 1):
                target = os.path.join(folder, os.path.basename(name))
                with archive.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                count += 1
                if progress and (index % 200 == 0 or index == total):
                    progress(index, total)
        return {"files": count, "dir": folder}

    @staticmethod
    def sha256_of(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    # ----------------------------------------------------------- 查素材

    def lookup(self, word: str, language: str, reading: str = "",
               dict_entries: dict | None = None) -> dict:
        """查一个词的音标 / 释义 / 例句 / 音频。签名和 v0.2 保持一致。"""
        out = {
            "word": word,
            "language": language,
            "reading": reading,
            "phonetic": "",
            "definition": "",
            "translation": "",
            "example": "",
            "example_translation": "",
            "audio_path": "",
            "audio_name": "",
            "audio_source": "",
            "sources": [],
        }
        if not word:
            return out

        if language == "ja":
            index = self.ja_materials()
            row = index.get(word)
            if row is None:
                # 写法查不到时用读音再试一次
                for key, value in index.items():
                    if value.get("r") and value["r"] == word:
                        row = value
                        break
            if row is None:
                return out
            out["reading"] = reading or row.get("r") or ""
            out["definition"] = row.get("m_en") or ""
            out["translation"] = row.get("m") or ""
            out["example"] = row.get("ex") or ""
            out["example_translation"] = row.get("exz") or ""
        else:
            row = self.en_materials().get(word)
            if row is None:
                return out
            out["phonetic"] = row.get("p") or ""
            out["translation"] = row.get("t") or ""
            out["definition"] = row.get("d") or ""
            out["example"] = row.get("ex") or ""
            out["example_translation"] = row.get("exz") or ""

        # 字面转义（\n / \t）还原成真字符，免得卡面上直接显示「\n」
        for key in ("phonetic", "definition", "translation", "example", "example_translation"):
            out[key] = _clean_text(out[key])

        name = row.get("a") or ""
        if name:
            out["audio_name"] = name
            full = os.path.join(self.media_dir(), name)
            if os.path.isfile(full):
                out["audio_path"] = full
                source = "本机语音合成" if "语音合成" in (row.get("src") or []) else "内置音频包"
                out["audio_source"] = source
        out["sources"] = [_label(s) for s in (row.get("src") or [])]
        if language == "ja" and not out["sources"]:
            out["sources"] = ["eggrolls JLPT10k"]
        return out

    def dictionary_lookup(self, terms, language: str) -> dict:
        """v0.2 会去扫本机 Yomitan 词典；v0.3 素材已经在构建期并进内置索引，这里恒为空。"""
        return {}

    # ----------------------------------------------------------- 设置页状态

    def describe(self) -> dict:
        en = self.en_materials()
        ja = self.ja_materials()
        media = self.media_installed()
        return {
            "en_words": len(en),
            "ja_words": len(ja),
            "en_built": (self._meta.get(EN_MATERIALS) or {}).get("built", ""),
            "ja_built": (self._meta.get(JA_MATERIALS) or {}).get("built", ""),
            "shared_dir": os.path.dirname(media["dir"]),
            "errors": dict(self._errors),
        }
