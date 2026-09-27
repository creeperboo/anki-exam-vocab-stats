# -*- coding: utf-8 -*-
"""开发用：把英语缺例句的词分个类，看看有多少是词表噪音。"""

from __future__ import annotations

import gzip
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "源码", "data")


def main():
    with gzip.open(os.path.join(DATA, "en_materials.json.gz"), "rt", encoding="utf-8") as fh:
        words = json.load(fh)["words"]
    miss = sorted(w for w, r in words.items() if not r.get("ex"))
    multi = [w for w in miss if " " in w or "-" in w or not re.fullmatch(r"[a-z']+", w)]
    single = [w for w in miss if w not in set(multi)]
    print("总", len(miss), "多词/连字符/非纯字母", len(multi), "纯单词", len(single))
    print("\n--- 多词/连字符 ---")
    print(" | ".join(multi))
    print("\n--- 纯单词（全列）---")
    print(" | ".join(single))


if __name__ == "__main__":
    main()
