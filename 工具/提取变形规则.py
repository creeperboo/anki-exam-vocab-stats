"""把本机 fushi 应用的变形规则表抽成插件能直接吃的数据文件。

fushi 不是插件，是本机的一个独立应用（``%LOCALAPPDATA%\\Hibiki\\fushi.exe``）。它自带
一整套 Yomitan 规范的变形规则（英语 ``en.json`` 16 组、日语 ``ja.json`` 54 组 834 条）。
我们自己不重写这套表，直接把「词尾替换」这一层抽出来当成候选生成器：

* 英语那套规则只有 100 多条，已经手抄进 ``源码/vocab_logic.py`` 的 ``EN_SUFFIX_RULES``
  （需要按优先级手工排序，放代码里更好维护）；
* 日语那套 800 多条太长，不适合塞进代码，这里产出 ``源码/data/ja_deform.json.gz``，
  运行时按词尾查表生成候选，**只采纳能命中词表的结果**，所以规则放宽也不会算错。

用法（PowerShell）：
    $py = "C:\\Program Files\\Lenovo\\ModelMgr\\Plugins\\Image\\python.exe"
    & $py "工具\\提取变形规则.py"
    & $py "工具\\提取变形规则.py" --source "D:\\其它\\transforms"   # 指定规则目录
    & $py "工具\\提取变形规则.py" --check                          # 只看统计，不写文件
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
DATA_DIR = os.path.join(PROJECT, "源码", "data")

DEFAULT_SOURCES = (
    os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Hibiki", "data", "flutter_assets",
        "assets", "transforms",
    ),
    os.path.join(PROJECT, "工具", "下载", "transforms"),
)


def find_source(explicit: str = "") -> str:
    if explicit:
        if not os.path.isdir(explicit):
            raise SystemExit(f"指定的规则目录不存在：{explicit}")
        return explicit
    for candidate in DEFAULT_SOURCES:
        if candidate and os.path.isfile(os.path.join(candidate, "ja.json")):
            return candidate
    raise SystemExit(
        "没找到 fushi 的变形规则目录。请用 --source 指定 "
        "（目录里要有 ja.json / en.json）。"
    )


def extract_pairs(path: str) -> list[list[str]]:
    """读一份 Yomitan 变形表，抽出全部 suffix 规则，去重后按词尾长度降序排。"""
    with open(path, "r", encoding="utf-8") as fh:
        blob = json.load(fh)
    pairs: list[tuple[str, str]] = []
    for transform in (blob.get("transforms") or {}).values():
        for rule in transform.get("rules") or ():
            if rule.get("type") != "suffix":
                continue
            source = rule.get("fromSuffix") or ""
            target = rule.get("toSuffix") or ""
            if not source:
                continue
            item = (source, target)
            if item not in pairs:
                pairs.append(item)
    # 长的词尾优先：いませんでした 要排在 ません 前面，否则永远轮不到它
    pairs.sort(key=lambda p: (-len(p[0]), p[0], p[1]))
    return [[a, b] for a, b in pairs]


def main() -> int:
    parser = argparse.ArgumentParser(description="抽取 fushi 的变形规则表")
    parser.add_argument("--source", default="", help="transforms 目录（含 ja.json / en.json）")
    parser.add_argument("--check", action="store_true", help="只打印统计，不写文件")
    args = parser.parse_args()

    source = find_source(args.source)
    ja_path = os.path.join(source, "ja.json")
    en_path = os.path.join(source, "en.json")
    ja_pairs = extract_pairs(ja_path)
    en_pairs = extract_pairs(en_path) if os.path.isfile(en_path) else []
    print(f"规则目录：{source}")
    print(f"日语 suffix 规则：{len(ja_pairs)} 条（去重后）")
    print(f"英语 suffix 规则：{len(en_pairs)} 条（仅统计，正式规则在 vocab_logic.py）")
    if args.check:
        return 0

    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {
        "meta": {
            "source": "fushi (Hibiki) transforms/ja.json",
            "license": "仅本机抽取，用于候选生成；不复制任何词典内容",
            "version": "v0.3",
            "rules": len(ja_pairs),
        },
        "pairs": ja_pairs,
    }
    dest = os.path.join(DATA_DIR, "ja_deform.json.gz")
    with gzip.open(dest, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"已写出 {dest}（{os.path.getsize(dest)} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
