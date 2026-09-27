# -*- coding: utf-8 -*-
"""构建插件自带的素材库（v0.3：全部内置，制卡时不再现场找素材）。

用法：
    python 工具\\构建素材库.py text     # 只建文本素材索引（秒级）
    python 工具\\构建素材库.py audio    # 只收音频（读文本索引，几到十几分钟）
    python 工具\\构建素材库.py all      # 全量（text + audio）

产物：
    源码\\data\\en_materials.json.gz      英语：音标 / 中文释义 / 英文释义 / 例句 / 例句译 / 音频名
    源码\\data\\ja_materials.json.gz      日语：写法 / 读音 / 释义 / 例句 / 例句译 / 音频名
    源码\\data\\materials_manifest.json   音频包的文件名 / sha256 / 大小（插件照着下载校验）
    工具\\素材构建\\exam_materials_audio.zip   音频包（挂 GitHub Release）

硬约束：单词 / 释义 / 音频 / 例句 四项缺失数必须是 0，否则脚本非零退出并打印缺口。
例句译允许留空（构建报告里给补齐率）。
"""

from __future__ import annotations

import bz2
import csv
import datetime
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "源码")
RAW = os.path.join(HERE, "下载")
DATA = os.path.join(SRC, "data")
BUILD = os.path.join(HERE, "素材构建")
MEDIA_STAGE = os.path.join(BUILD, "media")
AUDIO_ZIP = os.path.join(BUILD, "exam_materials_audio.zip")

if SRC not in sys.path:
    sys.path.insert(0, SRC)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import vocab_logic as V  # noqa: E402
import 词典提取 as D  # noqa: E402

EN_CODES = ("cet4", "cet6", "ky", "ielts", "toefl", "gre")
JA_CODES = ("n5", "n4", "n3", "n2", "n1")

FFMPEG = shutil.which("ffmpeg") or r"C:\Users\creep\ffmpeg-x86_64-git-c92304f8c\bin\ffmpeg.exe"
MAX_EXAMPLE = 120


def now() -> str:
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


def user_files() -> str:
    return os.path.join(os.environ["APPDATA"], "Anki2", "addons21", "1045800357", "user_files")


def media_name(language: str, key: str) -> str:
    digest = hashlib.sha1((language + "\x00" + key).encode("utf-8")).hexdigest()[:12]
    return "{}_{}.mp3".format(language, digest)


def read_gz(path: str) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def write_gz(path: str, payload) -> int:
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.GzipFile(path, "wb", compresslevel=9, mtime=0) as handle:
        handle.write(blob)
    return len(blob)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", V.strip_markup(text or "")).strip()


def good_example(text: str, language: str) -> bool:
    text = (text or "").strip()
    if len(text) < 6 or len(text) > MAX_EXAMPLE:
        return False
    if "http" in text or "@" in text or "|" in text:
        return False
    return True


# ------------------------------------------------------------------ 目标词


def english_targets() -> list:
    index = read_gz(os.path.join(DATA, "exam_index.json.gz"))
    words = set(index.get("word_exam") or {})
    words |= set(index.get("group_exam") or {})
    return sorted(w for w in words if w)


def japanese_targets() -> list:
    index = read_gz(os.path.join(DATA, "jlpt_index.json.gz"))
    return sorted(index.get("by_word") or {})


# ------------------------------------------------------------------ 英语文本


def load_wordforge() -> dict:
    path = os.path.join(RAW, "wordforge_words.json")
    out: dict = {}
    with io.open(path, encoding="utf-8") as handle:
        for entry in json.load(handle):
            if not isinstance(entry, list) or not entry:
                continue
            word = V.normalize_english(str(entry[0] or ""))
            if word:
                out.setdefault(word, entry)
    return out


def build_tatoeba_index(wanted: set) -> dict:
    """从 Tatoeba 英文句子库里给每个词挑一条例句（构建期一次性）。"""
    path = os.path.join(RAW, "tatoeba_eng_sentences.tsv.bz2")
    out: dict = {}
    if not os.path.isfile(path):
        return out
    token_re = re.compile(r"[A-Za-z][A-Za-z'-]*")
    remaining = set(wanted)
    # 三轮：先挑短句（更像词典例句），短句里没有的再放宽长度
    for low, high in ((16, MAX_EXAMPLE), (16, 200), (6, 320)):
        if not remaining:
            break
        with bz2.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not remaining:
                    break
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 3 or parts[1] != "eng":
                    continue
                text = parts[2].strip()
                if not (low <= len(text) <= high) or "http" in text or "@" in text or "|" in text:
                    continue
                hit = None
                for token in token_re.findall(text):
                    low_token = token.lower()
                    if low_token in remaining:
                        hit = low_token
                        break
                if hit is None:
                    continue
                out[hit] = text
                remaining.discard(hit)
    return out


def fallback_example(word: str, row: dict, language: str) -> str:
    """几种词典都查不到例句时，用词义拼一句能读的句子兜底。

    用户的要求是「素材不够缝缝补补也凑够」「不要有的有有的没有」，所以宁可给
    一句由词义拼出来的说明句，也不留空。卡片的「来源」列会写明这是兜底句。
    """
    if language == "ja":
        meaning = clean_text(row.get("m") or "")
        if meaning:
            return "「{}」の意味は「{}」です。".format(row.get("k") or word, meaning)
        return "「{}」を使った例文です。".format(row.get("k") or word)
    definition = clean_text((row.get("d") or "").split("/")[0])
    if definition:
        definition = definition.rstrip(".").strip()
        return '{} means "{}".'.format(
            word[:1].upper() + word[1:], definition
        )
    gloss = re.split(r"[;；,，]", clean_text(row.get("t") or ""))[0].strip()
    if gloss:
        return "{} — {}".format(word, gloss)
    return "{}".format(word)


def build_english_text() -> dict:
    words = english_targets()
    wf = load_wordforge()
    lite = read_gz(os.path.join(DATA, "ecdict_lite.json.gz")).get("words", {})

    materials: dict = {}
    for word in words:
        entry = wf.get(word)
        info = lite.get(word) or {}
        row = {"p": "", "t": "", "d": "", "ex": "", "exz": "", "a": "", "src": []}
        if entry is not None:
            row["p"] = clean_text(str(entry[1] or ""))
            row["t"] = clean_text(str(entry[4] or ""))
            row["d"] = clean_text(str(entry[5] or ""))
            row["ex"] = clean_text(str(entry[6] or "")) if len(entry) > 6 else ""
            row["exz"] = clean_text(str(entry[7] or "")) if len(entry) > 7 else ""
            if any((row["p"], row["t"], row["d"])):
                row["src"].append("wordforge")
        if not row["t"]:
            row["t"] = clean_text(info.get("t") or "")
        if not row["d"]:
            row["d"] = clean_text(info.get("d") or "")
        if not row["p"]:
            row["p"] = clean_text(info.get("p") or "")
        if row["t"] or row["d"]:
            row["src"].append("ECDICT")
        if not good_example(row["ex"], "en"):
            row["ex"] = ""
            row["exz"] = ""
        materials[word] = row

    missing_example = [w for w, r in materials.items() if not r["ex"]]
    print("  英语：缺例句", len(missing_example), "→ 用 Tatoeba 补")
    tatoeba = build_tatoeba_index(set(missing_example))
    for word, sentence in tatoeba.items():
        materials[word]["ex"] = sentence
        materials[word]["src"].append("Tatoeba")
    print("  Tatoeba 补上", len(tatoeba), "条，还缺",
          len([w for w in missing_example if not materials[w]["ex"]]))

    still = {w for w, r in materials.items() if not r["ex"] or not r["t"]}
    if still:
        for name, (language, path) in D.candidate_dictionaries().items():
            if language != "en" or not still:
                continue
            try:
                found = D.read_dictionary(path, "en", still)
            except Exception as exc:  # noqa: BLE001
                print("    词典读取失败", name, exc)
                continue
            used = 0
            for word, entry in found.items():
                row = materials.get(word)
                if row is None:
                    continue
                if not row["ex"] and entry.get("example"):
                    row["ex"] = clean_text(entry["example"])
                    row["exz"] = row["exz"] or clean_text(entry.get("example_translation") or "")
                    row["src"].append(name)
                    used += 1
                if not row["t"] and entry.get("translation"):
                    row["t"] = clean_text(entry["translation"])
                    used += 1
                if not row["d"] and entry.get("definition"):
                    row["d"] = clean_text(entry["definition"])
                if not row["p"] and entry.get("phonetic"):
                    row["p"] = clean_text(entry["phonetic"])
            still = {w for w in still if not materials[w]["ex"] or not materials[w]["t"]}
            print("    {}：补上 {} 条，还剩 {} 个待补".format(name, used, len(still)))

    patched = 0
    for word, row in materials.items():
        if not row["ex"]:
            row["ex"] = fallback_example(word, row, "en")
            row["exz"] = row["exz"] or clean_text(row["t"] or "")
            if "词义代例句" not in row["src"]:
                row["src"].append("词义代例句")
            patched += 1
    print("  英语：{} 条完全没有例句来源，已用词义兜底句补齐".format(patched))
    return materials


# ------------------------------------------------------------------ 日语文本


def build_japanese_text() -> dict:
    words = japanese_targets()
    index = read_gz(os.path.join(DATA, "jlpt_index.json.gz"))
    jlpt_meanings = index.get("meanings") or {}
    materials: dict = {
        word: {
            "k": word,
            "r": "",
            # m = 中文释义（优先本机《明镜日汉双解》，见下面的词典那一段）；
            # m_en = 英文/日文原文释义（eggrolls / OpenJLPT 给的那种，留给卡片当第二行）
            "m": "",
            "m_en": clean_text(jlpt_meanings.get(word, "")),
            "ex": "",
            "exz": "",
            "a": "",
            "src": [],
        }
        for word in words
    }

    path = os.path.join(RAW, "eggrolls_notes.csv")
    filled = 0
    with io.open(path, encoding="utf-8", newline="") as handle:
        for line in handle:
            line = line.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 39:
                continue
            kanji = clean_text(cols[3])
            furi = clean_text(cols[6])
            for candidate in (V.normalize_japanese(kanji), V.normalize_japanese(furi)):
                row = materials.get(candidate)
                if row is None:
                    continue
                if kanji:
                    row["k"] = kanji
                if furi:
                    row["r"] = furi
                meaning = clean_text(cols[7])
                if meaning and not row["m_en"]:
                    row["m_en"] = meaning
                example = clean_text(cols[12])
                if example and not row["ex"]:
                    row["ex"] = example
                    row["exz"] = clean_text(cols[14])
                if "eggrolls" not in row["src"]:
                    row["src"].append("eggrolls")
                filled += 1
    print("  eggrolls 命中", filled, "次")

    for level in JA_CODES:
        p = os.path.join(RAW, "openjlpt-{}.csv".format(level))
        if not os.path.isfile(p):
            continue
        with io.open(p, encoding="utf-8-sig", newline="") as handle:
            for rowcsv in csv.DictReader(handle):
                word = V.normalize_japanese(rowcsv.get("word") or "")
                material = materials.get(word)
                if material is None:
                    continue
                if not material["r"]:
                    material["r"] = clean_text(rowcsv.get("reading") or "")
                if not material["m_en"]:
                    material["m_en"] = clean_text(rowcsv.get("meanings") or "")
                if not material["ex"]:
                    example = clean_text(rowcsv.get("example_ja") or "")
                    if example:
                        material["ex"] = example
                        material["exz"] = clean_text(rowcsv.get("example_en") or "")
                if "OpenJLPT" not in material["src"]:
                    material["src"].append("OpenJLPT")

    for level in JA_CODES:
        p = os.path.join(RAW, "{}.csv".format(level))
        if not os.path.isfile(p):
            continue
        with io.open(p, encoding="utf-8-sig", newline="") as handle:
            for rowcsv in csv.DictReader(handle):
                for raw in re.split(r"[;；]", rowcsv.get("expression") or ""):
                    word = V.normalize_japanese(raw)
                    material = materials.get(word)
                    if material is None:
                        continue
                    if not material["r"]:
                        reading = (rowcsv.get("reading") or "").split(";")[0]
                        material["r"] = clean_text(reading)
                    if not material["m_en"]:
                        material["m_en"] = clean_text(rowcsv.get("meaning") or "")
                    if "tanos" not in material["src"]:
                        material["src"].append("tanos")

    # 中文释义优先：不管公开词表给的是英文还是日文，都先拿本机《明镜日汉双解》
    # 的中文补一遍（覆盖 92%+）；没补上的最后退回 m_en。这样卡片上和英语轴一样，
    # 第一行中文、第二行原文，不会「有的有中文、有的只有英文」。
    targets = set(materials)
    for name, (language, p) in D.candidate_dictionaries().items():
        if language != "ja" or not targets:
            continue
        try:
            found = D.read_dictionary(p, "ja", targets)
        except Exception as exc:  # noqa: BLE001
            print("    词典读取失败", name, exc)
            continue
        used = 0
        for word, entry in found.items():
            row = materials.get(word)
            if row is None:
                continue
            chinese = clean_text(entry.get("translation") or "")
            if chinese and not row["m"]:
                row["m"] = chinese
                used += 1
            if not row["ex"] and entry.get("example"):
                row["ex"] = clean_text(entry["example"])
                row["exz"] = clean_text(entry.get("example_translation") or "")
            if used and name not in row["src"]:
                row["src"].append(name)
        remaining = sum(1 for r in materials.values() if not r["m"])
        print("    {}：中文释义补上 {} 个，还差 {} 个".format(name, used, remaining))

    patched = 0
    for word, row in materials.items():
        if not row["m"]:
            row["m"] = clean_text(row.get("m_en") or "")
        if not row["ex"]:
            row["ex"] = fallback_example(word, row, "ja")
            if "词义代例句" not in row["src"]:
                row["src"].append("词义代例句")
            patched += 1
    print("  日语：{} 条完全没有例句来源，已用词义兜底句补齐".format(patched))
    return materials


# ------------------------------------------------------------------ 音频


def oald_index() -> dict:
    root = os.path.join(user_files(), "oald10_files", "oald10_files")
    path = os.path.join(root, "index.json")
    media = os.path.join(root, "media")
    out: dict = {}
    if not os.path.isfile(path):
        return out
    names = set(os.listdir(media)) if os.path.isdir(media) else set()
    with io.open(path, encoding="utf-8") as handle:
        rows = json.load(handle)
    for row in rows:
        word = V.normalize_english(str(row.get("entry_word") or ""))
        if not word or word in out:
            continue
        picks = {}
        for group in row.get("word_pronunciations") or ():
            for pron in group.get("pronunciations") or ():
                name = pron.get("audio_file") or ""
                if not name or (names and name not in names):
                    continue
                accent = (pron.get("accent") or "").upper()
                picks.setdefault(accent, (name, pron.get("phonetic") or ""))
        best = picks.get("US") or picks.get("UK")
        if best:
            out[word] = {"file": os.path.join(media, best[0]), "phonetic": best[1],
                         "source": "OALD10"}
    return out


def japanese_audio_index() -> dict:
    path = os.path.join(user_files(), "entries.db")
    out: dict = {}
    if not os.path.isfile(path):
        return out
    order = {"nhk16": 0, "shinmeikai8": 1, "forvo": 2, "jpod": 3}
    con = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select expression, reading, source, file from entries"
        ).fetchall()
    finally:
        con.close()
    for expression, reading, source, rel in rows:
        rank = order.get(source)
        if rank is None:
            continue
        full = os.path.join(user_files(), source + "_files", str(rel).replace("/", os.sep))
        if not os.path.isfile(full):
            continue
        for key in (V.normalize_japanese(expression or ""), V.normalize_japanese(reading or "")):
            if not key:
                continue
            current = out.get(key)
            if current is None or rank < current["rank"]:
                out[key] = {"file": full, "source": source, "rank": rank}
    return out


def sapi_voice_id(want: str):
    import win32com.client

    voice = win32com.client.Dispatch("SAPI.SpVoice")
    voices = voice.GetVoices()
    for index in range(voices.Count):
        if want.lower() in voices.Item(index).GetDescription().lower():
            return voice, index
    return voice, None


def synthesize(text: str, out_mp3: str, want: str) -> bool:
    try:
        import win32com.client
    except Exception:  # noqa: BLE001
        return False
    wav = out_mp3[:-4] + ".wav"
    try:
        voice, index = sapi_voice_id(want)
        if index is None:
            return False
        voice.Voice = voice.GetVoices().Item(index)
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        # 22 = 22kHz 16bit mono；不显式设格式时某些机器上 Speak 会报「拒绝访问」
        stream.Format.Type = 22
        stream.Open(wav, 3, False)
        voice.AudioOutputStream = stream
        voice.Speak(text)
        stream.Close()
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-i", wav, "-codec:a", "libmp3lame",
             "-b:a", "64k", out_mp3],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            if os.path.isfile(wav):
                os.remove(wav)
        except OSError:
            pass


def collect_audio(en: dict, ja: dict) -> dict:
    """把音频收进暂存目录，返回 {语言: {词: 文件名}} 与统计。"""
    os.makedirs(MEDIA_STAGE, exist_ok=True)
    oald = oald_index()
    ja_audio = japanese_audio_index()
    report = {
        "en": {"real": 0, "tts": 0, "missing": []},
        "ja": {"real": 0, "tts": 0, "missing": []},
    }
    names: dict = {"en": {}, "ja": {}}

    print("  英语音频：OALD10 索引", len(oald), "个词")
    for word, row in en.items():
        target = os.path.join(MEDIA_STAGE, media_name("en", word))
        pick = oald.get(word)
        if pick and os.path.isfile(pick["file"]):
            shutil.copyfile(pick["file"], target)
            names["en"][word] = os.path.basename(target)
            row["a"] = names["en"][word]
            if not row["p"] and pick.get("phonetic"):
                row["p"] = pick["phonetic"].strip("/")
            report["en"]["real"] += 1
            continue
        speak = word.replace("-", " ")
        if synthesize(speak, target, "Zira") and os.path.getsize(target) > 1000:
            names["en"][word] = os.path.basename(target)
            row["a"] = names["en"][word]
            if "语音合成" not in row["src"]:
                row["src"].append("语音合成")
            report["en"]["tts"] += 1
        else:
            report["en"]["missing"].append(word)

    print("  日语音频：entries.db 索引", len(ja_audio), "个词")
    for word, row in ja.items():
        target = os.path.join(MEDIA_STAGE, media_name("ja", word))
        pick = ja_audio.get(word) or ja_audio.get(V.normalize_japanese(row.get("r") or ""))
        if pick and os.path.isfile(pick["file"]):
            shutil.copyfile(pick["file"], target)
            row["a"] = os.path.basename(target)
            if pick["source"] not in row["src"]:
                row["src"].append(pick["source"])
            report["ja"]["real"] += 1
            continue
        speak = row.get("k") or word
        if synthesize(speak, target, "Haruka") and os.path.getsize(target) > 1000:
            row["a"] = os.path.basename(target)
            if "语音合成" not in row["src"]:
                row["src"].append("语音合成")
            report["ja"]["tts"] += 1
        else:
            report["ja"]["missing"].append(word)
    return report


def zip_media() -> dict:
    if os.path.isfile(AUDIO_ZIP):
        os.remove(AUDIO_ZIP)
    names = sorted(os.listdir(MEDIA_STAGE))
    with zipfile.ZipFile(AUDIO_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name in names:
            zf.write(os.path.join(MEDIA_STAGE, name), "media/" + name)
    return {"files": len(names), "bytes": os.path.getsize(AUDIO_ZIP)}


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------ 校验与落盘


def audit(en: dict, ja: dict, audio_report: dict) -> list:
    """硬约束：单词 / 释义 / 音频 一个都不能缺（缺了就不进补漏队列）。

    例句若来自兜底句，不算缺（会在报告里单列数量），因为卡片上仍然有内容，
    用户能看出一句是什么、来源是哪。
    """
    problems = []
    for word, row in en.items():
        if not word:
            problems.append(("en", word, "没单词"))
        if not (row.get("t") or row.get("d")):
            problems.append(("en", word, "没释义"))
        if not row.get("a"):
            problems.append(("en", word, "没音频"))
        if not row.get("ex"):
            problems.append(("en", word, "没例句（连兜底句都没有）"))
    for word, row in ja.items():
        if not (row.get("k") or word):
            problems.append(("ja", word, "没单词"))
        if not row.get("m"):
            problems.append(("ja", word, "没释义"))
        if not row.get("a"):
            problems.append(("ja", word, "没音频"))
        if not row.get("ex"):
            problems.append(("ja", word, "没例句（连兜底句都没有）"))
    return problems


def count_fallback(rows: dict) -> int:
    return sum(1 for r in rows.values() if "词义代例句" in (r.get("src") or ()))


def write_report(en: dict, ja: dict, audio_report: dict, problems: list, pack: dict) -> None:
    def rate(rows, field):
        total = len(rows)
        got = sum(1 for r in rows.values() if r.get(field))
        return "{}/{}（{:.1f}%）".format(got, total, (got / total * 100) if total else 0)

    lines = [
        "# 素材库构建记录",
        "",
        "- 构建时间：{}".format(now()[:19].replace("T", " ")),
        "- 生成脚本：`工具\\构建素材库.py`",
        "",
        "## 英语（{} 个词）".format(len(en)),
        "",
        "- 音标：" + rate(en, "p"),
        "- 中文释义：" + rate(en, "t"),
        "- 英文释义：" + rate(en, "d"),
        "- 例句：" + rate(en, "ex"),
        "- 例句译：" + rate(en, "exz"),
        "- 其中用词义兜底句补的例句：{} 条".format(count_fallback(en)),
        "- 真人音频（OALD10）：{}　语音合成：{}".format(
            audio_report.get("en", {}).get("real", 0), audio_report.get("en", {}).get("tts", 0)
        ),
        "",
        "## 日语（{} 个词）".format(len(ja)),
        "",
        "- 读音：" + rate(ja, "r"),
        "- 中文释义：" + rate(ja, "m"),
        "- 例句：" + rate(ja, "ex"),
        "- 例句译：" + rate(ja, "exz"),
        "- 其中用词义兜底句补的例句：{} 条".format(count_fallback(ja)),
        "- 真人音频（NHK16 / 新明解 / Forvo / JPod）：{}　语音合成：{}".format(
            audio_report.get("ja", {}).get("real", 0), audio_report.get("ja", {}).get("tts", 0)
        ),
        "",
        "## 音频包",
        "",
        "- 文件：{}；压缩包 {:,} 字节".format(pack.get("files", 0), pack.get("bytes", 0)),
        "",
    ]
    if problems:
        lines.append("## 缺口（硬约束未满足）")
        lines.append("")
        for language, word, why in problems[:200]:
            lines.append("- {} {} {}".format(language, word, why))
        lines.append("")
    else:
        lines.append("## 缺口")
        lines.append("")
        lines.append("单词 / 释义 / 音频 / 例句 缺失数全部为 0。")
        lines.append("")
    with io.open(os.path.join(HERE, "素材库构建记录.md"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))


def build_text_stage() -> tuple:
    print("构建英语文本素材…")
    en = build_english_text()
    print("构建日语文本素材…")
    ja = build_japanese_text()
    keep_audio(en, os.path.join(DATA, "en_materials.json.gz"))
    keep_audio(ja, os.path.join(DATA, "ja_materials.json.gz"))
    return en, ja


def keep_audio(rows: dict, path: str) -> None:
    """重跑文本阶段时，别把上一轮收好的音频文件名冲掉。"""
    if not os.path.isfile(path):
        return
    try:
        old = read_gz(path).get("words", {})
    except Exception:  # noqa: BLE001
        return
    for word, row in rows.items():
        name = (old.get(word) or {}).get("a")
        if name:
            row["a"] = name


def main(argv: list) -> int:
    stage = argv[0] if argv else "all"
    os.makedirs(BUILD, exist_ok=True)
    if stage in ("text", "all"):
        en, ja = build_text_stage()
        write_gz(os.path.join(DATA, "en_materials.json.gz"),
                 {"meta": {"built": now(), "words": len(en), "source": "wordforge / ECDICT / Tatoeba / 本机词典"},
                  "words": en})
        write_gz(os.path.join(DATA, "ja_materials.json.gz"),
                 {"meta": {"built": now(), "words": len(ja), "source": "eggrolls JLPT10k / OpenJLPT / tanos / 明镜日汉双解"},
                  "words": ja})
        print("文本索引已写入。")
        if stage == "text":
            return 0
    else:
        en = read_gz(os.path.join(DATA, "en_materials.json.gz")).get("words", {})
        ja = read_gz(os.path.join(DATA, "ja_materials.json.gz")).get("words", {})

    print("收集音频（真人音优先，缺的用本机语音合成）…")
    audio_report = collect_audio(en, ja)
    print("  英语：真人音 {}，合成 {}，失败 {}".format(
        audio_report["en"]["real"], audio_report["en"]["tts"], len(audio_report["en"]["missing"])))
    print("  日语：真人音 {}，合成 {}，失败 {}".format(
        audio_report["ja"]["real"], audio_report["ja"]["tts"], len(audio_report["ja"]["missing"])))
    print("打包音频 zip…")
    pack = zip_media()

    write_gz(os.path.join(DATA, "en_materials.json.gz"),
             {"meta": {"built": now(), "words": len(en), "source": "wordforge / ECDICT / Tatoeba / 本机词典"},
              "words": en})
    write_gz(os.path.join(DATA, "ja_materials.json.gz"),
             {"meta": {"built": now(), "words": len(ja), "source": "eggrolls JLPT10k / OpenJLPT / tanos / 明镜日汉双解"},
              "words": ja})
    manifest = {
        "built": now(),
        "file": os.path.basename(AUDIO_ZIP),
        "sha256": sha256_of(AUDIO_ZIP),
        "bytes": pack["bytes"],
        "files": pack["files"],
        "repo": "creeperboo/anki-exam-vocab-stats",
    }
    with io.open(os.path.join(DATA, "materials_manifest.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)

    problems = audit(en, ja, audio_report)
    write_report(en, ja, audio_report, problems, pack)
    if problems:
        print("硬约束未满足：{} 项缺口，详见 工具\\素材库构建记录.md".format(len(problems)))
        for item in problems[:20]:
            print("   ", item)
        return 1
    print("完成：音频包 {} 个文件，{:,} 字节".format(pack["files"], pack["bytes"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
