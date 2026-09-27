"""开发用：打印三个词库索引的结构和样例，方便写测试时确认键名。

用法（在本文件夹里执行）：
    python 工具\查看词库索引.py
"""

from __future__ import annotations

import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "源码", "data")


def describe(value) -> str:
    if isinstance(value, dict):
        sample = list(value.items())[:2]
        return f"dict[{len(value)}] 例：{json.dumps(sample, ensure_ascii=False)[:160]}"
    if isinstance(value, list):
        return f"list[{len(value)}] 例：{json.dumps(value[:3], ensure_ascii=False)[:160]}"
    return f"{type(value).__name__} {str(value)[:80]}"


def main() -> int:
    for name in ("exam_index", "lemma_index", "jlpt_index"):
        path = os.path.join(DATA, name + ".json.gz")
        if not os.path.isfile(path):
            print(f"缺少：{path}")
            return 1
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        print(f"=== {name}.json.gz ({os.path.getsize(path)} 字节) ===")
        for key, value in data.items():
            print(f"  {key}: {describe(value)}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
