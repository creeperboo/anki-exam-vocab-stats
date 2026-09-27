# -*- coding: utf-8 -*-
"""构建期专用：把本机 Yomitan 词典（zip 或已解压目录）解析成
``{定义, 释义, 例句, 例句译文}``。

运行期插件**不再**读本机词典——素材在构建期一次性抽干、写进内置索引，所以这些
解析规则只留在工具里。解析规则沿用 v0.2 ``resources.py`` 里已经在本机核对过的
那一套（明镜用 data.class 标结构、LDOCE5 用 lang 交替）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import zipfile

_RE_SENTENCE = re.compile(r"[.。！？!?…]")

# 只由标点/分隔符组成的碎片要丢掉，否则会把明镜的「/」当成中文释义
_RE_PUNCT_ONLY = re.compile(
    r"^[\s/・、。，,．\.\-—–~〜～()（）\[\]【】{}〈〉《》|:：;；!！?？*#※▲△▼▽]+$"
)

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "源码")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import vocab_logic as V  # noqa: E402


def looks_like_sentence(text: str) -> bool:
    text = (text or "").strip()
    return len(text) >= 12 and bool(_RE_SENTENCE.search(text))


def _piece(text) -> str:
    out = re.sub(r"^[\s/・￤|]+", "", str(text or "")).strip()
    out = re.sub(r"[\s・￤|]+$", "", out).strip()
    if not out or _RE_PUNCT_ONLY.match(out):
        return ""
    return out


def _piece_list(items, limit: int = 4) -> list[str]:
    out: list[str] = []
    for item in items:
        text = _piece(item)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def flatten(node, out: list, lang: str | None = None, cls: str | None = None,
            in_list: bool = False) -> None:
    """把 Yomitan 的 structured-content 摊平成 [(语言, 文本, class, 是否在列表里), …]。

    ``<ul>`` 里的内容是例句（牛津高阶／朗文都这么排），列表外的是释义。
    早期版本没区分列表层级，导致牛津高阶 10 的例句一条都取不到。
    """
    if isinstance(node, str):
        if node.strip():
            out.append((lang, node, cls, in_list))
        return
    if isinstance(node, list):
        for item in node:
            flatten(item, out, lang, cls, in_list)
        return
    if not isinstance(node, dict):
        return
    if node.get("type") == "structured-content":
        flatten(node.get("content"), out, lang, cls, in_list)
        return
    if node.get("tag") == "br":
        out.append((lang, "\n", cls, in_list))
        return
    here_lang = node.get("lang") or lang
    data = node.get("data") or {}
    here_cls = data.get("class") or cls
    if str(node.get("tag") or "").lower() in ("ul", "ol") and "content" in node:
        # 定义块最外层偶尔也是 ol/ul，只有嵌套在 li 里的 ul 才是例句
        nested = in_list or bool(data.get("list"))
        here_list = nested or str(node.get("tag") or "").lower() == "ul"
        flatten(node["content"], out, here_lang, here_cls, here_list)
        return
    if "content" in node:
        flatten(node["content"], out, here_lang, here_cls, in_list)


def extract_entry(row, language: str) -> dict:
    """Yomitan 词条 row -> {definition, translation, example, example_translation}。"""
    flat: list = []
    glossary = row[5] if len(row) > 5 else None
    flatten(glossary, flat)
    head = str(row[0] or "") if row else ""

    def texts(*classes, in_list=None) -> list[str]:
        wanted = set(classes)
        return [
            t for _l, t, c, il in flat
            if c in wanted and t and (in_list is None or il is in_list)
        ]

    defs_ja = _piece_list(
        t for _l, t, c, _il in flat if c in ("def2", "def") and not (_l or "").startswith("zh")
    )
    # 释义：列表外、非中文；例句：列表里、非中文。分不清时退回到只看语言。
    defs_zh = _piece_list(
        t for _l, t, _c, il in flat if (_l or "").startswith("zh") and not il
    )
    if not defs_zh:
        defs_zh = _piece_list(t for l, t, _c, _il in flat if (l or "").startswith("zh"))
    defs_en = _piece_list(
        t for l, t, c, il in flat
        if (l or "").startswith("en") and c not in ("exen", "ex") and not il
    )
    if not defs_en:
        defs_en = _piece_list(
            t for _l, t, c, il in flat
            if t and not il and not (_l or "").startswith("zh")
            and c not in ("exen", "ex", "exp", "example")
        )

    exjp = _piece_list(texts("exjp"), limit=2)
    excn = _piece_list(texts("excn"), limit=2)
    example = example_translation = ""
    if exjp:
        example = (
            " / ".join(exjp).replace("～", head).replace("〜", head).replace("▲", "").strip()
        )
        example_translation = " / ".join(excn)

    if not example:
        # 牛津高阶／朗文把例句排在第二个 ul 里，没有 lang 也没有 class
        for index, (_lang, text, cls, il) in enumerate(flat):
            if not il or not text or cls in ("excn",):
                continue
            if (_lang or "").startswith("zh"):
                continue
            if not looks_like_sentence(text):
                continue
            candidate = _piece(text)
            if not candidate:
                continue
            example = candidate
            for follow in flat[index + 1:]:
                flag_lang = (follow[0] or "")
                if flag_lang.startswith("zh"):
                    example_translation = _piece(follow[1]) or example_translation
                    break
            break

    if language == "ja":
        definition = defs_ja[0] if defs_ja else ""
        translation = defs_zh[0] if defs_zh else ""
        if not definition and defs_en:
            definition = defs_en[0]
        return {
            "definition": definition,
            "translation": translation,
            "example": example,
            "example_translation": example_translation,
        }

    definition = defs_en[0] if defs_en else ""
    translation = defs_zh[0] if defs_zh else ""
    if not example:
        # 明镜/LDOCE5 这类会把例句单独打上 class（exen / ex），先按 class 找
        for index, (_lang, text, cls, _il) in enumerate(flat):
            if cls not in ("exen", "ex", "exp", "example") or not text:
                continue
            if not looks_like_sentence(text):
                continue
            example = _piece(text)
            if not example:
                continue
            for follow in flat[index + 1:]:
                if (follow[0] or "").startswith("zh"):
                    example_translation = _piece(follow[1]) or example_translation
                    break
            break
    if not example:
        for index, (lang, text, _cls, _il) in enumerate(flat):
            if not (lang or "").startswith("en") or not text:
                continue
            text = text.strip()
            if text == definition or not looks_like_sentence(text):
                continue
            example = _piece(text)
            for follow in flat[index + 1:]:
                if (follow[0] or "").startswith("zh"):
                    example_translation = _piece(follow[1]) or example_translation
                    break
            break
    return {
        "definition": definition,
        "translation": translation,
        "example": example,
        "example_translation": example_translation,
    }


def _bank_key(name: str) -> int:
    match = re.search(r"(\d+)", os.path.basename(name))
    return int(match.group(1)) if match else 0


def _bank_files(path: str) -> list[str]:
    """一个词典（zip 或目录）里的 term_bank_*.json，按编号排序。"""
    if os.path.isdir(path):
        names = [n for n in os.listdir(path) if n.startswith("term_bank") and n.endswith(".json")]
    else:
        with zipfile.ZipFile(path) as zf:
            names = [
                n for n in zf.namelist()
                if os.path.basename(n).startswith("term_bank") and n.endswith(".json")
            ]
    return sorted(names, key=_bank_key)


def _read_member(path: str, name: str):
    if os.path.isdir(path):
        with open(os.path.join(path, name), encoding="utf-8") as handle:
            return json.load(handle)
    with zipfile.ZipFile(path) as zf:
        with zf.open(name) as handle:
            return json.loads(handle.read().decode("utf-8"))


def _score(entry: dict) -> int:
    return sum(1 for value in entry.values() if value)


def read_dictionary(path: str, language: str, wanted: set | None = None) -> dict:
    """读一本 Yomitan 词典，返回 {归一写法: 素材条目}。"""
    norm = (
        (lambda t: V.normalize_japanese(t))
        if language == "ja"
        else (lambda t: V.normalize_english(t))
    )
    out: dict = {}
    for name in _bank_files(path):
        try:
            rows = _read_member(path, name)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, list) or not row:
                continue
            term = norm(str(row[0] or ""))
            if not term or (wanted is not None and term not in wanted):
                continue
            entry = extract_entry(row, language)
            if not any(entry.values()):
                continue
            current = out.get(term)
            if current is None or _score(entry) > _score(current):
                out[term] = entry
    return out


def candidate_dictionaries() -> dict:
    """本机能找到的词典：{显示名: (语言, 路径)}，按优先级顺序。"""
    home = os.path.expanduser("~")
    downloads = os.path.join(home, "Downloads")
    documents = os.path.join(home, "Documents", "dictionaryResources")
    wanted = (
        # 英语：牛津高阶 / 朗文当代 例句最全，排最前
        ("牛津高阶双解 10", "en", os.path.join(downloads, "OALDPE10 (1).zip")),
        ("LDOCE5 英汉", "en", os.path.join(downloads, "LDOCE5 (1).zip")),
        ("柯林斯 COBUILD 2024", "en", os.path.join(downloads, "cobuild2024.zip")),
        ("柯林斯 COBUILD 8", "en", os.path.join(downloads, "COBUILD8.zip")),
        ("柯林斯 COBUILD", "en", os.path.join(downloads, "COBUILD10.zip")),
        ("柯林斯英语词典 CED24", "en", os.path.join(downloads, "CED24.zip")),
        ("牛津英语词典", "en", os.path.join(downloads, "Oxford Dictionary of English (Yomitan).zip")),
        ("牛津搭配词典", "en", os.path.join(downloads, "Oxford Collocations Dictionary [2026-04-07].zip")),
        ("明镜日汉双解", "ja", os.path.join(downloads, "明镜日汉双解词典_Yomitan 1.4.4(1).zip")),
        ("明镜日汉双解 1.4.2", "ja", os.path.join(downloads, "明镜日汉双解词典_Yomitan_民间版 1.4.2.zip")),
    )
    out: dict = {}
    for name, language, path in ordered_unique(wanted):
        if os.path.exists(path):
            out[name] = (language, path)
    return out


def ordered_unique(items):
    seen = set()
    for item in items:
        key = (item[2],)
        if key in seen:
            continue
        seen.add(key)
        yield item
