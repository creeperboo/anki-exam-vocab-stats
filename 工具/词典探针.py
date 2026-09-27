# -*- coding: utf-8 -*-
"""开发用：看看本机各词典到底能不能给出例句。"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import zipfile

HOME = os.path.expanduser("~")
DL = os.path.join(HOME, "Downloads")
DOC = os.path.join(HOME, "Documents", "dictionaryResources")
APP = os.path.join(os.environ["APPDATA"], "Anki2", "addons21", "1045800357", "user_files")

PROBE = ["abscission", "abstractly", "acarpous", "adulate", "analytics", "amortize"]


def head(title):
    print("\n=== " + title + " ===")


def probe_oald_index():
    head("OALD10 index.json 结构")
    path = os.path.join(APP, "oald10_files", "oald10_files", "index.json")
    if not os.path.isfile(path):
        print("  没有", path)
        return
    with io.open(path, encoding="utf-8") as handle:
        rows = json.load(handle)
    print("  条数", len(rows))
    print("  顶层键：", sorted(rows[0].keys()))
    for row in rows:
        if str(row.get("entry_word", "")).lower() == "abscission":
            print("  abscission 示例：", json.dumps(row, ensure_ascii=False)[:1500])
            break
    else:
        print("  index 里没有 abscission")


def _bank_no(name):
    digits = "".join(c for c in os.path.basename(name) if c.isdigit())
    return int(digits or 0)


def probe_zip(name, words):
    head("zip 词典：" + name)
    path = os.path.join(DL, name)
    if not os.path.isfile(path):
        print("  没有", path)
        return
    with zipfile.ZipFile(path) as zf:
        banks = [n for n in zf.namelist() if "term_bank" in os.path.basename(n)]
        print("  term_bank 文件数：", len(banks))
        if not banks:
            for n in zf.namelist()[:15]:
                print("   ", n)
            return
        with zf.open(sorted(banks, key=_bank_no)[0]) as handle:
            rows = json.loads(handle.read().decode("utf-8"))
        if isinstance(rows, list) and rows:
            print("  样例 row:", json.dumps(rows[0], ensure_ascii=False)[:700])
        found = {}
        for bank in banks:
            with zf.open(bank) as handle:
                try:
                    rows = json.loads(handle.read().decode("utf-8"))
                except Exception:
                    continue
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, list) and row and str(row[0]).lower() in words:
                    found.setdefault(str(row[0]).lower(), json.dumps(row, ensure_ascii=False)[:900])
        for word in words:
            print("  ", word, "->", found.get(word, "（没有）")[:900])


def probe_fushi_dir(name):
    head("fushi 目录：" + name)
    path = os.path.join(DOC, name)
    if not os.path.isdir(path):
        print("  没有", path)
        return
    for entry in sorted(os.listdir(path))[:20]:
        full = os.path.join(path, entry)
        size = os.path.getsize(full) if os.path.isfile(full) else "-"
        print("   ", entry, size)
    index = os.path.join(path, "index.json")
    if os.path.isfile(index):
        with io.open(index, encoding="utf-8") as handle:
            try:
                rows = json.load(handle)
            except Exception as exc:
                print("  index.json 读取失败", exc)
                return
        print("  index.json 类型", type(rows).__name__, "长度", len(rows))
        sample = rows if isinstance(rows, list) else list(rows.items())[:3]
        print("  样例：", json.dumps(sample, ensure_ascii=False)[:800])


def probe_android_db():
    head("android_english.db")
    path = os.path.join(DL, "android_english.db")
    if not os.path.isfile(path):
        print("  没有", path)
        return
    con = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)
    try:
        print("  表：", con.execute(
            "select name from sqlite_master where type='table'").fetchall())
        print("  entries 行数：", con.execute("select count(*) from entries").fetchone())
        print("  列：", [d[1] for d in con.execute("pragma table_info(entries)")])
        row = con.execute("select * from entries limit 1").fetchone()
        print("  样例：", str(row)[:800])
    finally:
        con.close()


def main():
    probe_oald_index()
    probe_zip("LDOCE5 (1).zip", set(PROBE))
    probe_zip("OALDPE10 (1).zip", set(PROBE))
    probe_fushi_dir("OALDPE10")
    probe_fushi_dir("LDOCE5")
    probe_android_db()


if __name__ == "__main__":
    main()
