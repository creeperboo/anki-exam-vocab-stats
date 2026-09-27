# -*- coding: utf-8 -*-
"""开发用：摸清本机素材（词典 / 音频 / 例句）的结构和覆盖率。

只读，不改任何东西。用法：
    python 工具\\本机素材探针.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys


def user_files() -> str:
    return os.path.join(os.environ["APPDATA"], "Anki2", "addons21", "1045800357", "user_files")


def show_entries_db(base: str) -> None:
    path = os.path.join(base, "entries.db")
    if not os.path.isfile(path):
        print("没有 entries.db")
        return
    con = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)
    print("== entries.db 表 ==")
    for name, sql in con.execute("select name, sql from sqlite_master where type='table'"):
        print("-", name, "::", (sql or "").replace("\n", " ")[:300])
    for (name,) in con.execute("select name from sqlite_master where type='table'"):
        try:
            total = con.execute("select count(*) from " + name).fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            total = f"读取失败 {exc}"
        print(f"  {name}: {total} 行")
    try:
        cols = [d[1] for d in con.execute("pragma table_info(entries)")]
        print("entries 列：", cols)
        print("样例（去 blob）：")
        for row in con.execute("select * from entries limit 2"):
            print("  ", [("<blob %d>" % len(v)) if isinstance(v, bytes) else v for v in row][:8])
        print("source 分布：")
        for row in con.execute("select source, count(*) from entries group by source order by 2 desc"):
            print("  ", row)
    except Exception as exc:  # noqa: BLE001
        print("精读 entries 失败：", exc)
    con.close()


def show_oald(base: str) -> None:
    root = os.path.join(base, "oald10_files", "oald10_files")
    index_path = os.path.join(root, "index.json")
    if not os.path.isfile(index_path):
        print("没有 oald10 index.json")
        return
    with open(index_path, encoding="utf-8") as handle:
        data = json.load(handle)
    with_audio = [row for row in data if row.get("word_pronunciations")]
    print("== OALD10 ==")
    print("词条", len(data), "有音频", len(with_audio))
    print("样例：", json.dumps(with_audio[0], ensure_ascii=False)[:400])
    media = os.path.join(root, "media")
    if os.path.isdir(media):
        names = os.listdir(media)
        print("media 文件数", len(names), "样例", names[:5])


def show_audio_dirs(base: str) -> None:
    print("== 音频目录 ==")
    for entry in sorted(os.listdir(base)):
        path = os.path.join(base, entry)
        if not os.path.isdir(path):
            continue
        candidates = []
        for sub in os.listdir(path):
            inner = os.path.join(path, sub)
            if os.path.isdir(inner):
                candidates.append((sub, len(os.listdir(inner))))
        print(" ", entry, candidates[:3])


def show_downloads() -> None:
    root = os.path.join(os.path.expanduser("~"), "Downloads")
    print("== Downloads ==")
    if not os.path.isdir(root):
        print("没有 Downloads")
        return
    for name in sorted(os.listdir(root)):
        if name.lower().endswith((".zip", ".db", ".7z")):
            path = os.path.join(root, name)
            print("  {}\t{}".format(name, os.path.getsize(path)) if os.path.isfile(path) else "  " + name)


def show_documents() -> None:
    root = os.path.join(os.path.expanduser("~"), "Documents", "dictionaryResources")
    print("== Documents\\dictionaryResources ==")
    if not os.path.isdir(root):
        print("  没有这个目录")
        return
    for name in sorted(os.listdir(root)):
        print("  ", name)


def main() -> int:
    base = user_files()
    print("user_files:", base, os.path.isdir(base))
    show_oald(base)
    show_entries_db(base)
    show_audio_dirs(base)
    show_downloads()
    show_documents()
    return 0


if __name__ == "__main__":
    sys.exit(main())
