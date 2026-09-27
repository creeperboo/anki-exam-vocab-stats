# -*- coding: utf-8 -*-
"""开发用：看 Tatoeba 到底能给剩下的缺口补多少（宽松匹配 / 词元匹配）。"""

from __future__ import annotations

import bz2
import gzip
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "源码", "data")
RAW = os.path.join(HERE, "下载")


def read_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    words = read_gz(os.path.join(DATA, "en_materials.json.gz"))["words"]
    missing = sorted(w for w, r in words.items() if not r.get("ex"))
    print("缺例句", len(missing))
    forms = read_gz(os.path.join(DATA, "exam_index.json.gz")).get("forms", {})

    exact = {}
    loose = {}
    lemma = {}
    by_lemma = {}
    for word in missing:
        by_lemma.setdefault(word, set()).add(word)
        for form in (forms.get(word) or []):
            by_lemma.setdefault(word, set()).add(form)
            lemma.setdefault(form, set()).add(word)

    token_re = re.compile(r"[A-Za-z][A-Za-z'-]*")
    remaining = set(missing)
    path = os.path.join(RAW, "tatoeba_eng_sentences.tsv.bz2")
    with bz2.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not remaining and not lemma:
                break
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3 or parts[1] != "eng":
                continue
            text = parts[2].strip()
            if "http" in text or "@" in text or len(text) > 300:
                continue
            for token in token_re.findall(text):
                low = token.lower()
                if low in remaining and low not in exact:
                    exact[low] = text
                    remaining.discard(low)
                if low in lemma and len(text) <= 160:
                    for word in lemma[low]:
                        loose.setdefault(word, text)
    print("严格放宽长度后能补", len(exact))
    print("剩", len(remaining))
    print("用词元形式再补", len(loose))
    print("最终还缺", len([w for w in missing if w not in exact and w not in loose]))
    sample = [w for w in missing if w not in exact and w not in loose]
    print("样例", sample[:40])


if __name__ == "__main__":
    sys.exit(main())
