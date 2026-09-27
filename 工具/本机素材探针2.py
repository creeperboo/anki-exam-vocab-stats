# -*- coding: utf-8 -*-
"""开发用：摸清音频/例句素材细节（OALD 覆盖、android_english.db、daijisen 索引）。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys


def user_files() -> str:
    return os.path.join(os.environ["APPDATA"], "Anki2", "addons21", "1045800357", "user_files")


def show_dirs(base: str) -> None:
    for name in ("daijisen_files", "ozk5_files", "taas_files", "jpod_files", "tts_files"):
        root = os.path.join(base, name)
        print("==", name, "==")
        if not os.path.isdir(root):
            print("  不存在")
            continue
        for entry in sorted(os.listdir(root))[:6]:
            full = os.path.join(root, entry)
            if os.path.isdir(full):
                inner = os.listdir(full)
                print("  [dir]", entry, len(inner), inner[:3])
            else:
                print("  [file]", entry, os.path.getsize(full))


def show_android_db() -> None:
    path = os.path.join(os.path.expanduser("~"), "Downloads", "android_english.db")
    if not os.path.isfile(path):
        print("没有 android_english.db")
        return
    con = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)
    print("== android_english.db ==")
    for name, sql in con.execute("select name, sql from sqlite_master where type in ('table','index')"):
        print("-", name, "::", (sql or "").replace("\n", " ")[:260])
    for (name,) in con.execute("select name from sqlite_master where type='table'"):
        try:
            print("  ", name, con.execute("select count(*) from " + name).fetchone()[0])
        except Exception as exc:  # noqa: BLE001
            print("  ", name, "读取失败", exc)
    for table in ("entries", "android"):
        try:
            cols = [d[1] for d in con.execute("pragma table_info(%s)" % table)]
            print(table, "列：", cols)
            row = con.execute("select * from %s limit 1" % table).fetchone()
            if row:
                print("  样例:", [("<blob %d>" % len(v)) if isinstance(v, bytes) else str(v)[:60] for v in row])
        except Exception as exc:  # noqa: BLE001
            print(table, "读取失败", exc)
    con.close()


def oald_coverage() -> None:
    base = os.path.join(user_files(), "oald10_files", "oald10_files")
    with open(os.path.join(base, "index.json"), encoding="utf-8") as handle:
        data = json.load(handle)
    media = os.path.join(base, "media")
    names = set(os.listdir(media))
    missing = 0
    total = 0
    index: dict[str, list] = {}
    for row in data:
        word = str(row.get("entry_word") or "").strip().lower()
        files = []
        for group in row.get("word_pronunciations") or ():
            for pron in group.get("pronunciations") or ():
                name = pron.get("audio_file")
                if name:
                    files.append((pron.get("accent") or "", name, pron.get("phonetic") or ""))
        if word and files:
            index.setdefault(word, []).extend(files)
    for _word, files in index.items():
        for _accent, name, _ph in files:
            total += 1
            if name not in names:
                missing += 1
    print("== OALD 音频索引 ==")
    print("有音频的词头数", len(index), "引用 mp3 次数", total, "磁盘缺失", missing)
    print("样例 index['apple']:", index.get("apple"))


def main() -> int:
    base = user_files()
    show_dirs(base)
    show_android_db()
    oald_coverage()
    return 0


if __name__ == "__main__":
    sys.exit(main())
