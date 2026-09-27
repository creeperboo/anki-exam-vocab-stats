# -*- coding: utf-8 -*-
"""开发用：每个词表各缺多少例句/释义。"""

from __future__ import annotations

import gzip
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "源码", "data")


def read(path):
    with gzip.open(os.path.join(DATA, path), "rt", encoding="utf-8") as fh:
        return json.load(fh)


def main():
    index = read("exam_index.json.gz")
    mats = read("en_materials.json.gz")["words"]
    groups = index.get("group_exam") or {}
    word_exam = index.get("word_exam") or {}

    per_code_missing = {}
    for code in ("cet4", "cet6", "ky", "ielts", "toefl", "gre"):
        words = {w for w, tags in word_exam.items() if code in tags.split()}
        words |= {w for w, tags in groups.items() if code in tags.split()}
        miss_ex = [w for w in words if not (mats.get(w) or {}).get("ex")]
        miss_t = [w for w in words if not ((mats.get(w) or {}).get("t") or (mats.get(w) or {}).get("d"))]
        per_code_missing[code] = (len(words), len(miss_ex), len(miss_t))
        print("{:6s} 共 {:5d} 缺例句 {:4d} 缺释义 {:3d}".format(code, *per_code_missing[code]))

    ja = read("jlpt_index.json.gz")
    jm = read("ja_materials.json.gz")["words"]
    for code in ("n5", "n4", "n3", "n2", "n1"):
        words = {w for w, tags in (ja.get("by_word") or {}).items() if code in tags.split()}
        miss_m = [w for w in words if not (jm.get(w) or {}).get("m")]
        miss_ex = [w for w in words if not (jm.get(w) or {}).get("ex")]
        print("{:6s} 共 {:5d} 缺释义 {:4d} 缺例句 {:4d}".format(code, len(words), len(miss_m), len(miss_ex)))


if __name__ == "__main__":
    main()
