# -*- coding: utf-8 -*-
"""开发用：查看素材缺口清单，判断该从哪里补。"""

from __future__ import annotations

import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "源码", "data")


def read_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "en"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    if which == "en":
        words = read_gz(os.path.join(DATA, "en_materials.json.gz"))["words"]
        bad = [(w, r) for w, r in words.items() if not r.get("ex")]
        print("英语缺例句", len(bad))
        for w, r in bad[:limit]:
            print("  ", repr(w), "| t=", r.get("t", "")[:20], "| p=", r.get("p", "")[:12])
        extra = [w for w, r in bad if " " in w or "-" in w or not w.isalpha()]
        print("  其中含空格/连字符/非纯字母：", len(extra))
        for w in extra[:limit]:
            print("    ", repr(w))
    else:
        words = read_gz(os.path.join(DATA, "ja_materials.json.gz"))["words"]
        bad = [(w, r) for w, r in words.items() if not r.get("m") or not r.get("ex")]
        print("日语缺释义或例句", len(bad))
        for w, r in bad[:limit]:
            print("  ", repr(w), "| k=", r.get("k", ""), "| m=", (r.get("m") or "")[:20],
                  "| ex=", (r.get("ex") or "")[:24])
    return 0


if __name__ == "__main__":
    sys.exit(main())
